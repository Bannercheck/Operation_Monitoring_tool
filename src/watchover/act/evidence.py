"""Evidence, slots and provenance of a case (docs/design/action-agent.md §4.3; C2, C3, C8, R1, I10, I18).

Every item is written once: scrubbed with `privacy.scrub_strict` (structured fields with `scrub_fields`, user-like values
pseudonymised), cut to 400 characters, hashed, numbered E1…, folded and checked against QUARANTINE_V2 before any model sees it.
Output of `privacy: raw` templates is never evidence text: only its parser's structured captures are kept.

Who delivered an item (its *source*) and whether its content can be trusted (the *provenance* of the values taken from it):

* trusted         a line or metric whose `agent_id` belongs to an agent record named like the case host (or an inventory alias of
                  it), a diagnose result from that host's actuator, a server-side probe; its text content is `log`
* trusted_signed  a diagnose result signed by the host's actuator in the current run (Ed25519, WP11c checks the signature)
* claimed         no agent_id (legacy shared key, open mode), another host's agent, or re-attributed by ingest_guard
                  (`host.reported` keeps the claimed name: "Beyan edilen kaynak: db-01"); display only, no slots
* derived         engine output (incident, anomaly without an identified sender)
* human           operator notes and pasted output (parsed captures count as `log`)

Inventory fields are trusted *facts* (host, env, criticality slots) but are not evidence that the triggering condition happened,
so they never satisfy I10's "trusted item for H" on their own (an inventoried host named in a spoofed line stays at L1).

Slots are the only values runbook bindings `from: evidence.<slot>` may use; each slot row (kind 'slot') points at the item it
came from (`source_eid`) and carries that item's value provenance."""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import privacy
from .types import now, sha256_hex, stored_ts

QUARANTINE_VERSION = 2
TEXT_MAX = 400
LEVEL_OF = {"claimed": 0, "human": 0, "derived": 1, "log": 1, "trusted": 2, "trusted_signed": 3}
USER_KEYS = ("user", "username", "user_name", "login", "email", "mail", "account", "principal", "uid", "kullanici")

# ---------------------------------------------------------------- QUARANTINE_V2 (patterns run on NFKC + confusable-folded text)
QUARANTINE_V2: Tuple[Tuple[str, "re.Pattern"], ...] = (
    ("instruction_en", re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|previous|above|rules?)\b")),
    ("instruction_tr", re.compile(r"(?i)(önceki|yukarıdaki)\s+(talimat|kural)")),
    ("instruction_tr", re.compile(r"(?i)talimat(ları)?\s+(yok\s*say|unut)")),
    ("role_marker", re.compile(r"(?i)\b(system|assistant|user)\s*:")),
    ("you_are_now", re.compile(r"(?i)\byou are now\b")),
    ("sen_artik", re.compile(r"(?i)\bsen\s+art[ıi]k\b")),
    ("run_following", re.compile(r"(?i)(run the following|çalıştır\s*:)")),
    ("output_json", re.compile(r'"(choice|runbook|params)"\s*:')),
    ("shell_rm", re.compile(r"rm\s+-rf")),
    ("shell_pipe", re.compile(r"curl\s+\S+\s*\|\s*(ba)?sh")),
    ("shell_wget", re.compile(r"wget\s")),
    ("shell_ps", re.compile(r"(?i)powershell(\.exe)?\s+-e(nc(odedcommand)?)?\b")),
    ("shell_iex", re.compile(r"(?i)Invoke-Expression")),
    ("shell_b64", re.compile(r"base64\s+-d")),
    ("shell_sh", re.compile(r";\s*sh\b")),
    ("shell_subst", re.compile(r"\$\(")),
    ("shell_backtick", re.compile(r"`")),
    ("base64_run", re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")),
)
_LATIN = re.compile(r"[A-Za-z]")
_OTHER = re.compile(r"[\u0370-\u03ff\u0400-\u04ff]")          # Greek, Cyrillic


def quarantine(text: str, terms: Iterable[str] = ()) -> List[str]:
    """Reasons a line must not reach a model (empty list = clean). Folding first (R1): NFKC, zero-width / bidi controls, Latin
    skeleton of look-alikes; then QUARANTINE_V2, this installation's runbook keys / template ids, size and script checks."""
    if not text:
        return []
    out: List[str] = []
    raw = str(text)
    f = privacy.fold(raw)
    if "zero_width_bidi" in f.hits:
        out.append("zero_width_bidi")
    t = f.text
    for code, rx in QUARANTINE_V2:
        if rx.search(t) and code not in out:
            out.append(code)
    low = t.lower()
    for term in terms:
        if term and len(term) >= 4 and re.search(r"(?<![\w.-])" + re.escape(term.lower()) + r"(?![\w-])", low):
            out.append("install_term")
            break
    if len(raw.encode("utf-8", "ignore")) > 4096:
        out.append("too_long")
    if raw and sum(1 for c in raw if not c.isprintable() and c not in "\t\n\r") > 0.3 * len(raw):
        out.append("non_printable")
    nf = privacy.fold(raw, confusables=False).text
    for w in re.findall(r"\w+", nf):
        if _LATIN.search(w) and _OTHER.search(w):
            out.append("mixed_script")
            break
    return out


# ---------------------------------------------------------------- slot extractors (deterministic)
RX_NOSPACE = re.compile(r"(?i)(?:no space left on device|disk (?:is )?full|ENOSPC|disk quota exceeded)[^(\n]*\((/[^)\s]+)\)")
RX_NOSPACE2 = re.compile(r"(?i)(/[\w.@+-]+(?:/[\w.@+-]+)+)\s*[:,]?\s*(?:write failed: )?(?:no space left on device|ENOSPC)")
RX_PATH = re.compile(r"(?<![\w:/])(/(?:[\w.@+-]+/)*[\w.@+-]+)")
RX_UNIT = re.compile(r"\b([\w@.-]+\.service)\b")
RX_WIN_SVC = re.compile(r"(?i)\bthe (.{1,80}?) service (?:terminated unexpectedly|terminated with)")
RX_POOL = re.compile(r"(?i)application pool '([^']{1,64})'")
RX_SAP = re.compile(r"/usr/sap/([A-Z][A-Z0-9]{2})/[A-Z]+(\d{2})\b")


def _dirname(p: str) -> str:
    p = p.rstrip("/")
    return p.rsplit("/", 1)[0] or "/" if "/" in p else ""


def log_slots(text: str, fields: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """(slot, value, type) from a line's text and parsed fields; provenance `log` (attacker-controllable content)."""
    out: List[Tuple[str, str, str]] = []
    t = text or ""
    m = RX_NOSPACE.search(t) or RX_NOSPACE2.search(t)
    if m:
        d = _dirname(m.group(1))
        if d:
            out.append(("log_dir", d, "path"))
    seen = set()
    for p in RX_PATH.findall(t)[:3]:
        if p not in seen and len(p) > 1:
            seen.add(p)
            out.append(("path", p, "path"))
    for u in RX_UNIT.findall(t)[:2]:
        out.append(("unit", u, "unit"))
    m = RX_WIN_SVC.search(t)
    if m:
        out.append(("win_service", m.group(1).strip(), "win_service"))
    m = RX_POOL.search(t)
    if m:
        out.append(("app_pool", m.group(1), "iis_pool"))
    m = RX_SAP.search(t)
    if m:
        out += [("sap_sid", m.group(1), "sap_sid"), ("sap_nr", m.group(2), "sap_nr")]
    svc = (fields or {}).get("service")
    if svc and svc != "-":
        out.append(("service", str(svc), "ident"))
    return out


# diagnose templates → slots of their structured output (§4.3 table; C8: mount / use_pct from linux.disk.usage)
DIAG_SLOTS: Dict[str, Tuple[Tuple[str, str, str], ...]] = {
    "linux.disk.usage": (("mount", "mount", "path"), ("use_pct", "use_pct", "int")),
    "win.disk.usage": (("mount", "drive", "path"), ("use_pct", "use_pct", "int")),
    "linux.service.status": (("unit", "unit", "unit"),),
    "win.service.status": (("win_service", "name", "win_service"),),
    "iis.apppool.state": (("app_pool", "pool", "iis_pool"),),
    "sap.wp.table": (("wp_no", "wp_no", "int"), ("wp_runtime_s", "wp_runtime_s", "int"), ("wp_program", "wp_program", "ident")),
}


def diag_slots(template_id: str, fields: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    out = []
    for slot, key, vt in DIAG_SLOTS.get(template_id or "", ()):
        v = (fields or {}).get(key)
        if v not in (None, ""):
            out.append((slot, str(v), vt))
    pid, start = (fields or {}).get("pid"), (fields or {}).get("start_time")
    if pid not in (None, "") and start not in (None, ""):             # pid only as a (pid, start_time) pair from a diagnose result
        out.append(("pid", f"{int(pid)}:{start}", "pid"))
    return out


def _pseudonymise(fields: Any) -> Any:
    if isinstance(fields, dict):
        return {k: (privacy.pseudonym(str(v)) if str(k).lower() in USER_KEYS and isinstance(v, str) and v else _pseudonymise(v))
                for k, v in fields.items()}
    if isinstance(fields, list):
        return [_pseudonymise(x) for x in fields]
    return fields


class Evidence:
    """The evidence book of the act layer (svc.act.evidence); CaseRouter._add_evidence writes through it."""

    def __init__(self, act):
        self.act = act
        self.db = act.db
        self._terms: Tuple[float, List[str]] = (0.0, [])
        self._names: Dict[Any, Tuple[float, Optional[str]]] = {}

    # ---------------------------------------------------------------- identity
    def agent_name(self, agent_id) -> Optional[str]:
        if agent_id in (None, ""):
            return None
        try:
            aid = int(agent_id)
        except (TypeError, ValueError):
            return None
        hit = self._names.get(aid)
        if hit and time.monotonic() - hit[0] < 30:
            return hit[1]
        svc = getattr(self.act, "svc", None)
        name = None
        try:
            rec = svc.agents.get(aid) if svc is not None and getattr(svc, "agents", None) is not None else None
            if rec and not rec.get("revoked"):
                name = str(rec.get("name") or "") or None
        except Exception:  # noqa: BLE001
            name = None
        self._names[aid] = (time.monotonic(), name)
        return name

    def same_host(self, a: str, b: str) -> bool:
        """Equal names, or two names of one inventory record (hostname, short name, alias, IP)."""
        a, b = (a or "").strip().lower(), (b or "").strip().lower()
        if not a or not b:
            return False
        if a == b or a.split(".")[0] == b.split(".")[0]:
            return True
        svc = getattr(self.act, "svc", None)
        try:
            ra = svc.inventory.resolve(a) if svc is not None else None
            rb = svc.inventory.resolve(b) if svc is not None else None
        except Exception:  # noqa: BLE001
            return False
        return bool(ra and rb and ra.get("hostname") == rb.get("hostname"))

    def classify(self, ev: Dict[str, Any], target: str) -> Dict[str, str]:
        """{"source", "provenance", "reported"} of one incoming item for the case host `target`."""
        kind = str(ev.get("kind") or "log")
        attrs = ev.get("attributes") or ev.get("attrs") or {}
        reported = str(ev.get("reported") or attrs.get("host.reported") or "")
        host = str(ev.get("host") or target or "")
        if kind in ("note", "pasted", "human"):
            return {"source": "human", "provenance": "human", "reported": reported}
        if kind in ("incident", "derived"):
            return {"source": "derived", "provenance": "derived", "reported": reported}
        if kind in ("inventory", "probe", "bizflow"):
            return {"source": "trusted", "provenance": "trusted", "reported": ""}
        aid = ev.get("agent_id", attrs.get("agent_id"))
        if kind == "diag":
            from_actuator = aid is not None and self.same_host(self.agent_name(aid) or str(ev.get("actuator_host") or ""), target)
            if not from_actuator:
                return {"source": "claimed", "provenance": "claimed", "reported": reported}
            signed = bool(ev.get("signed")) and bool(ev.get("run_id"))
            return {"source": "trusted_signed" if signed else "trusted", "provenance": "trusted_signed" if signed else "trusted",
                    "reported": ""}
        if reported:                                                   # ingest_guard re-attributed it: the claim is display only
            return {"source": "claimed", "provenance": "claimed", "reported": reported}
        name = self.agent_name(aid)
        if name is None or not self.same_host(name, host) or (target and not self.same_host(host, target)):
            return {"source": "claimed", "provenance": "claimed", "reported": host if host and target and host != target else ""}
        return {"source": "trusted", "provenance": "trusted" if kind == "metric" else "log", "reported": ""}

    # ---------------------------------------------------------------- quarantine terms (this installation's keys and ids)
    def terms(self) -> List[str]:
        at, cached = self._terms
        if time.monotonic() - at < 60:
            return cached
        out: List[str] = []
        try:
            out += [r["key"] for r in self.db._exec("SELECT key FROM act_runbooks")]
            out += sorted({r["id"] for r in self.db._exec("SELECT id FROM act_templates")})
        except Exception:  # noqa: BLE001
            pass
        self._terms = (time.monotonic(), out)
        return out

    # ---------------------------------------------------------------- write
    def _eid(self, cid: int) -> str:
        n = self.db._exec("SELECT COUNT(*) AS n FROM act_evidence WHERE case_id=? AND kind<>'slot'", (cid,))[0]["n"]
        k = int(n) + 1
        while self.db._exec("SELECT 1 AS x FROM act_evidence WHERE case_id=? AND eid=?", (cid, f"E{k}")):
            k += 1
        return f"E{k}"

    def add(self, cid: int, ev: Dict[str, Any], target: str = "") -> Optional[str]:
        """Write one item (and its slots) inside the caller's transaction; returns its id E<n>."""
        kind = str(ev.get("kind") or "log")
        cls = self.classify(ev, target)
        raw = str(ev.get("privacy") or "") == "raw"
        text = "" if raw else privacy.scrub_strict(str(ev.get("text") or ""))[:TEXT_MAX]
        fields = ev.get("captures") if raw else ev.get("fields")
        fields = _pseudonymise(privacy.scrub_fields(fields or {}))
        reasons = quarantine(str(ev.get("text") or ""), self.terms()) if (text and kind not in ("diag", "metric")) else []
        eid = self._eid(cid)
        attrs = ev.get("attributes") or ev.get("attrs") or {}
        aid = ev.get("agent_id", attrs.get("agent_id"))
        ref = dict(ev.get("ref") or {})
        if raw:
            ref["raw"] = True                                          # display-only output stays with its step, never here
        for k in ("run_id", "step_id", "template_id"):
            if ev.get(k) not in (None, ""):
                ref[k] = ev[k]
        self.db._exec("INSERT INTO act_evidence (case_id, eid, kind, ts, host, agent, provenance, text, fields, ref, quarantined, qreason, "
                      "sha256, source, reported) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, eid, kind, stored_ts(ev.get("ts") or None), str(ev.get("host") or target or ""), "" if aid is None else str(aid),
                       cls["provenance"], text, json.dumps(fields, ensure_ascii=False, default=str, sort_keys=True),
                       json.dumps(ref, ensure_ascii=False, default=str, sort_keys=True), int(bool(reasons)), ",".join(reasons)[:200],
                       sha256_hex(text + "\n" + json.dumps(fields, sort_keys=True, default=str)), cls["source"], cls["reported"][:200]))
        if reasons:
            self._flag(cid, eid, reasons)
            return eid
        slots: List[Tuple[str, str, str, str]] = []
        if cls["source"] in ("claimed",):
            pass                                                       # display only: a claimed line feeds no slot
        elif kind == "diag":
            slots = [(s, v, t, cls["provenance"]) for s, v, t in diag_slots(str(ev.get("template_id") or ""), fields)]
        elif kind == "metric":
            for s in ("metric", "value", "limit"):
                if fields.get(s) not in (None, ""):
                    slots.append((s, str(fields[s]), "number" if s != "metric" else "ident", "trusted" if cls["source"] == "trusted" else "derived"))
        elif kind == "bizflow":
            slots = [(s, str(fields[s]), "flow", "trusted") for s in ("flow_id", "step", "stuck") if fields.get(s) not in (None, "")]
        elif kind in ("log", "human", "note", "pasted", "incident"):
            prov = "log" if cls["source"] in ("trusted", "human") else cls["provenance"]
            slots = [(s, v, t, prov) for s, v, t in log_slots(text, fields)]
        for i, (slot, value, vtype, prov) in enumerate(slots, 1):
            self.db._exec("INSERT INTO act_evidence (case_id, eid, kind, ts, host, provenance, slot, value, vtype, source_eid, sha256, source) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, f"{eid}.{i}", "slot", now(), str(ev.get("host") or target or ""), prov, slot, value[:300], vtype, eid,
                           sha256_hex(f"{slot}={value}"), cls["source"]))
        return eid

    def add_host_facts(self, cid: int, host: str, inv: Dict[str, Any], env: str = "") -> None:
        """Trusted host slots from the inventory (facts, not evidence of the condition)."""
        if not host:
            return
        have = {r["slot"] for r in self.db._exec("SELECT slot FROM act_evidence WHERE case_id=? AND kind='slot' AND source_eid='inv'", (cid,))}
        facts = {"host": host, "env": env or inv.get("env"), "os": inv.get("os"), "host_class": inv.get("cluster") or inv.get("role"),
                 "criticality": inv.get("criticality")}
        for i, (slot, v) in enumerate(facts.items(), 1):
            if v in (None, "") or slot in have:
                continue
            self.db._exec("INSERT INTO act_evidence (case_id, eid, kind, ts, host, provenance, slot, value, vtype, source_eid, sha256, source) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, f"inv.{slot}", "slot", now(), host, "trusted", slot, str(v)[:200], "host" if slot == "host" else "enum", "inv",
                           sha256_hex(f"{slot}={v}"), "trusted"))

    def _flag(self, cid: int, eid: str, reasons: List[str]) -> None:
        row = self.db._exec("SELECT injection_suspect, team_id FROM actions WHERE id=?", (cid,))
        if not row:
            return
        if not row[0].get("injection_suspect"):
            self.db._exec("UPDATE actions SET injection_suspect=1 WHERE id=?", (cid,))
        self.act.audit.write("inject_flag", object_kind="case", object_id=cid, team_id=row[0].get("team_id"),
                             payload={"eid": eid, "reasons": reasons, "quarantine": QUARANTINE_VERSION})

    # ---------------------------------------------------------------- read
    def items(self, cid: int) -> List[dict]:
        rows = self.db._exec("SELECT eid, kind, ts, host, agent, source, provenance, reported, text, fields, ref, quarantined, qreason "
                             "FROM act_evidence WHERE case_id=? AND kind<>'slot' ORDER BY id", (cid,))
        for r in rows:
            for k in ("fields", "ref"):
                try:
                    r[k] = json.loads(r[k] or "{}")
                except ValueError:
                    r[k] = {}
            r["quarantined"] = bool(r["quarantined"])
            if r.get("reported"):
                r["reported_label"] = {"tr": f"Beyan edilen kaynak: {r['reported']}", "en": f"Claimed source: {r['reported']}"}
        return rows

    def slots(self, cid: int, min_level: int = 0) -> Dict[str, List[dict]]:
        """slot -> [{value, vtype, provenance, eid}] (strongest provenance first); quarantined items feed none."""
        out: Dict[str, List[dict]] = {}
        for r in self.db._exec("SELECT slot, value, vtype, provenance, source_eid FROM act_evidence WHERE case_id=? AND kind='slot' ORDER BY id",
                               (cid,)):
            if LEVEL_OF.get(r["provenance"], 0) < min_level:
                continue
            out.setdefault(r["slot"], []).append({"value": r["value"], "vtype": r["vtype"], "provenance": r["provenance"], "eid": r["source_eid"]})
        for v in out.values():
            v.sort(key=lambda x: -LEVEL_OF.get(x["provenance"], 0))
        return out

    def cap(self, cid: int, run_id: Optional[int] = None) -> Dict[str, Any]:
        """I10 (the L ≤ 2 parts): no trusted observation for the case host → L1; ingest-token evidence alone → ≤ L2; a quarantined
        line → L1 (§4.3). L3 / L4 need a `trusted_signed` diagnose of the host in the run (`run_id`; re-measure check: WP16)."""
        c = self.db._exec("SELECT host, injection_suspect FROM actions WHERE id=?", (cid,))
        if not c:
            raise KeyError(cid)
        host = c[0]["host"] or ""
        reasons = []
        if c[0].get("injection_suspect"):
            reasons.append("injection_suspect")
        rows = self.db._exec("SELECT host, source, ref FROM act_evidence WHERE case_id=? AND kind<>'slot' AND quarantined=0 "
                             "AND source IN ('trusted','trusted_signed')", (cid,))
        mine = [r for r in rows if not host or self.same_host(r["host"], host)]
        if not host:
            reasons.append("no_host")
        elif not mine:
            reasons.append("no_trusted_item")
        signed = [r for r in mine if r["source"] == "trusted_signed"
                  and (run_id is None or str(json.loads(r["ref"] or "{}").get("run_id")) == str(run_id))]
        level = 1 if reasons else (4 if signed else 2)
        if level == 2:
            reasons.append("no_signed_diagnose")
        return {"level": level, "reasons": reasons, "trusted": len(mine), "signed": len(signed)}
