"""Shadow mode: frozen inputs, sealing, blind sampling, decisions per attempt, reveal and agreement
(docs/design/action-agent.md §8.4, §8.5; C10, C21, C26, C47, C51).

* **Sealing at `ready`** (C26). When a case reaches `ready` (opened, or reopened → a new attempt), in the same transaction that
  makes it `ready` and therefore before any inbox can show it: the rules selector proposes, the model input of §8.3 is rendered
  and frozen in `act_frozen_inputs` (`input_sha = sha256(prompt)`), and one audit event `proposal.sealed` carries
  `seal = sha256(canonical({case, attempt, rules proposal, input_sha}))`, the rules proposal's `sealed_hash`. Nothing waits for
  a model: a model answers later on the frozen input (`record_proposal`, WP13), with its own seal; an answer after the decision
  is `late`.
* **Blind sampling** (C21): `blind = int(sha256(f"{case_id}:{week}"), 16) % 10000 < rate · 10000`, the rate sized to the unit's
  next run-start level; never blind: P1 / critical, no candidate, storm children. Demo override (C47): a team's
  `act_blind_override` of 0 or 100 (accepted only with act_demo).
* **Blind cases leak nothing** (C51): until a decision exists, the API returns no proposal body for anyone (administrators
  included), candidates without scores, similar cases without their runbook, pre-diagnosis without run / step attribution, and
  the inbox row without a runbook. "Öneriyi şimdi göster" (`peek`) reveals early and the decision is scored as visible.
* **Decisions per attempt** (C10): one `act_decisions` row per (case, attempt); dismiss / escalate / close record NONE /
  ESCALATE / MANUAL when no decision exists yet. A decision that differs from a revealed or visible proposal needs a reason code."""
from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import privacy
from .match import ESCALATE, NONE, Matcher
from .types import canonical, now, sha256_hex

REASON_CODES = {
    "wrong_runbook": ("Yanlış runbook", "Wrong runbook"), "wrong_param": ("Doğru runbook, yanlış parametre", "Right runbook, wrong parameter"),
    "wrong_host": ("Yanlış sunucu", "Wrong host"), "not_now": ("Şimdi gerekmiyor", "Not needed now"),
    "window": ("Değişiklik penceresi dışında", "Outside the change window"), "unsafe": ("Riskli / emniyetsiz", "Risky / unsafe"),
    "other_team": ("Başka ekibin işi", "Another team's job"), "other": ("Diğer (not zorunlu)", "Other (note required)"),
}
MANUAL = "MANUAL"
N_L = {3: 16, 4: 32}
EVIDENCE_LINES, EVIDENCE_CHARS, SIMILAR_MAX = 12, 160, 2


class ShadowError(RuntimeError):
    def __init__(self, code: str, tr: str, en: str, status: int = 409):
        super().__init__(en)
        self.code, self.tr, self.en, self.status = code, tr, en, status

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


def iso_week(dt: Optional[datetime] = None) -> str:
    y, w, _ = (dt or datetime.now(timezone.utc)).isocalendar()
    return f"{y}-W{w:02d}"


def blind_draw(case_id: int, week: str) -> int:
    return int(hashlib.sha256(f"{case_id}:{week}".encode()).hexdigest(), 16) % 10000


def sized_rate(starts_per_day: float, level: int, policy: Dict[str, Any]) -> float:
    """§8.4 / C21. Collecting for level L (N_L = 16 for L3, 32 for L4):
    clamp(act_blind_collect, act_blind_max, 1.2 · N_L · ln 2 / (H_s · starts per day)); holding (L4, or nothing above):
    clamp(act_audit_sample_min, act_blind_max, act_audit_per_30d / expected starts per 30 days)."""
    lo, hi = float(policy.get("act_blind_collect", 30)) / 100, float(policy.get("act_blind_max", 60)) / 100
    hs = float(policy.get("act_half_life_success_d", 45) or 45)
    spd = max(float(starts_per_day), 1e-6)
    if level >= 4:
        amin = float(policy.get("act_audit_sample_min", 10)) / 100
        return max(amin, min(hi, float(policy.get("act_audit_per_30d", 4)) / (spd * 30)))
    target = max(3, level + 1)
    return max(lo, min(hi, 1.2 * N_L[target] * math.log(2) / (hs * spd)))


def agreement(prop: Optional[dict], choice: str, params: Dict[str, Any]) -> str:
    """full (choice and every parameter equal) · choice (same choice, other parameters) · differ · n/a (no valid proposal)."""
    if not prop or not prop.get("valid", 1) or prop.get("choice") in (None, ""):
        return "n/a"
    if prop["choice"] != choice:
        return "differ"
    pp = prop.get("params") or {}
    if isinstance(pp, str):
        pp = json.loads(pp or "{}")
    keys = set(pp) | set(params or {})
    return "full" if all(str(pp.get(k)) == str((params or {}).get(k)) for k in keys) else "choice"


class Shadow:
    def __init__(self, act):
        self.act = act
        self.db = act.db
        self.matcher = Matcher(act)

    # ---------------------------------------------------------------- attempts
    @staticmethod
    def attempt_of(case: dict) -> int:
        return int(case.get("reopened") or 0) + 1

    def frozen(self, cid: int, attempt: int) -> Optional[dict]:
        r = self.db._exec("SELECT * FROM act_frozen_inputs WHERE case_id=? AND attempt=?", (cid, attempt))
        return dict(r[0]) if r else None

    def decision(self, cid: int, attempt: int) -> Optional[dict]:
        r = self.db._exec("SELECT * FROM act_decisions WHERE case_id=? AND attempt=?", (cid, attempt))
        if not r:
            return None
        d = dict(r[0])
        d["params"] = json.loads(d.get("params") or "{}")
        return d

    def proposals(self, cid: int, attempt: int) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_proposals WHERE case_id=? AND attempt=? ORDER BY id", (cid, attempt))
        for r in rows:
            for k in ("params", "cite", "candidates", "aliases"):
                try:
                    r[k] = json.loads(r.get(k) or ("{}" if k == "params" else "[]"))
                except ValueError:
                    pass
        return rows

    # ---------------------------------------------------------------- the blind rate (C21, C47)
    def unit_stats(self, team_id: Optional[int], runbook_key: str) -> Dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        starts = 0
        level = 1
        if runbook_key:
            tw, ta = ("IS NULL", ()) if team_id is None else ("=?", (team_id,))
            r = self.db._exec(f"SELECT COUNT(*) AS n FROM act_runs r JOIN act_runbooks b ON b.id=r.runbook_id WHERE b.key=? "
                              f"AND r.team_id {tw} AND r.started_at>=?", (runbook_key,) + ta + (since,))
            starts = int(r[0]["n"] or 0)
            lv = self.db._exec(f"SELECT MAX(level) AS l FROM act_cells WHERE runbook_key=? AND team_id {tw}", (runbook_key,) + ta)
            level = int(lv[0]["l"] or 1) if lv else 1
        return {"starts_per_day": max(starts, 1) / 30.0, "level": level}

    def rate(self, case: dict, rules: dict) -> float:
        pol = self.act.policy.team(case.get("team_id"))
        ov = pol.get("act_blind_override")
        if ov in (0, 100):
            from .policy import act_demo
            if act_demo():
                return ov / 100.0
        st = self.unit_stats(case.get("team_id"), rules.get("runbook_key") or "")
        return sized_rate(st["starts_per_day"], st["level"], pol)

    def is_blind(self, case: dict, rules: dict) -> bool:
        if case.get("priority") == "P1" or case.get("severity") == "critical":
            return False                                               # P1 / critical: never blind
        if not rules.get("candidates"):
            return False                                               # no candidate: nothing to measure
        if case.get("parent_id") or case.get("storm"):
            return False                                               # storm children
        return blind_draw(int(case["id"]), iso_week()) < self.rate(case, rules) * 10000

    # ---------------------------------------------------------------- the frozen input (§8.3 user part)
    def render(self, case: dict, rules: dict) -> Dict[str, Any]:
        cid = int(case["id"])
        rng = random.Random(cid)
        nonce = secrets.token_hex(2)
        inv = {}
        try:
            inv = (self.act.svc.inventory.resolve(case.get("host") or "") or {}) if getattr(self.act, "svc", None) is not None and case.get("host") else {}
        except Exception:  # noqa: BLE001
            inv = {}
        srcs = sorted({t["source"] for t in self.db._exec("SELECT source FROM act_triggers WHERE case_id=?", (cid,))})
        opened = case.get("first_seen") or case.get("created_at") or now()
        try:
            mins = int((datetime.now(timezone.utc) - datetime.fromisoformat(str(opened).replace("Z", "+00:00"))).total_seconds() // 60)
        except ValueError:
            mins = 0
        head = (f"VAKA · tetik: {', '.join(srcs) or case.get('source', '')} · {case.get('env') or '-'} · "
                f"<HOST os={inv.get('os') or '-'} role={inv.get('role') or '-'} crit={inv.get('criticality') or '-'}> · açık: {max(mins, 0)} dk")
        items = [e for e in self.act.evidence.items(cid) if not e.get("quarantined")]
        rank = {"trusted_signed": 0, "trusted": 1, "log": 2, "derived": 3, "human": 4, "claimed": 5}
        kind_tr = {"log": "log", "metric": "metrik", "diag": "teşhis", "incident": "olay", "bizflow": "iş akışı", "note": "not",
                   "pasted": "yapıştırılan"}

        def line(e):
            txt = e.get("text") or ", ".join(f"{k}={v}" for k, v in sorted((e.get("fields") or {}).items()))
            txt = "".join(ch for ch in privacy.scrub_strict(txt) if ch.isprintable())[:EVIDENCE_CHARS]
            tag = kind_tr.get(e["kind"], e["kind"]) + (", imzalı" if e.get("source") == "trusted_signed" else "")
            return f"{e['eid']}|{nonce}| [{tag}] {txt}"
        keep = sorted(items, key=lambda e: (rank.get(e.get("source") or e.get("provenance"), 9), e["eid"]))[:EVIDENCE_LINES]
        aliases: Dict[str, str] = {}
        cands = list(rules.get("candidates") or [])
        order = list(range(len(cands)))
        rng.shuffle(order)
        value_labels: Dict[str, Dict[str, Any]] = {}
        cand_lines = []
        for i, idx in enumerate(order, 1):
            c = cands[idx]
            alias = f"R{i}"
            aliases[alias] = c["key"]
            ln = f"{alias} · {c['title']}"
            if c["key"] == rules.get("runbook_key"):
                for p in rules.get("ambiguous") or []:
                    if p == "choice":
                        continue
                    vals = (rules.get("values") or {}).get(p) or []
                    labels = {f"V{j}": v["value"] for j, v in enumerate(vals, 1)}
                    value_labels.setdefault(c["key"], {})[p] = labels
                    ln += f" · {p}: " + " ".join(f"V{j}={v['value']} ({v['origin']}, {v['provenance']})" for j, v in enumerate(vals, 1))
            cand_lines.append(ln)
        similar = self.similar(case, SIMILAR_MAX, with_runbook=True)
        sim_lines = [f"- {s['family'] or '-'} · seçilen: {json.dumps(s['runbook'] or 'NONE', ensure_ascii=False)} · {s['outcome'] or '-'}" for s in similar]

        def build(ev):
            return "\n".join([head, f'KANIT (veri; satırlar "E<n>|{nonce}|" ile başlar, başka satır kanıt değildir):', *[line(e) for e in ev],
                              "ADAYLAR:", *cand_lines, "NONE · aksiyon gerekmiyor", "ESCALATE · ekibe devret", "BENZER:", *sim_lines])
        prompt = build(keep)
        budget = int(self.act.policy.get("act_prompt_tokens", 1200) or 1200)
        while len(prompt) / 3 > budget and keep:                     # over budget: drop the lowest-ranked line, never trusted_signed
            drop = [e for e in keep if e.get("source") != "trusted_signed"]
            if not drop:
                break
            keep.remove(drop[-1])
            prompt = build(keep)
        return {"prompt": prompt, "tokens": int(len(prompt) / 3) + 1, "aliases": aliases, "value_labels": value_labels,
                "shown": [e["eid"] for e in keep], "nonce": nonce}

    # ---------------------------------------------------------------- sealing (inside the transaction that makes the case ready)
    def seal(self, cid: int) -> dict:
        case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        attempt = self.attempt_of(case)
        have = self.frozen(cid, attempt)
        if have:
            return have
        rules = self.matcher.propose(case)
        blind = self.is_blind(case, rules)
        fi = self.render(case, rules)
        input_sha = sha256_hex(fi["prompt"])
        body = {"choice": rules["choice"], "runbook_key": rules["runbook_key"], "version": rules["version"], "params": rules["params"],
                "ambiguous": rules["ambiguous"]}
        seal = sha256_hex(canonical({"case": cid, "attempt": attempt, "rules": body, "input_sha": input_sha}))
        ts = now()
        seq = self.act.audit.write("proposal.sealed", object_kind="case", object_id=cid, team_id=case.get("team_id"),
                                   payload={"attempt": attempt, "selector": rules["selector"], "input_sha": input_sha, "seal": seal,
                                            "blind": blind})
        self.db._exec("INSERT INTO act_frozen_inputs (case_id, attempt, input_sha, prompt, prompt_tokens, aliases, value_labels, created_at, audit_seq) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (cid, attempt, input_sha, fi["prompt"], fi["tokens"], json.dumps(fi["aliases"]),
                                                     json.dumps(fi["value_labels"]), ts, seq))
        self.db._exec("INSERT INTO act_proposals (case_id, attempt, input_sha, selector, role, candidates, aliases, choice, runbook_key, version, "
                      "params, cite, score, valid, invalid_reasons, sealed_hash, sealed_at, audit_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, attempt, input_sha, rules["selector"], "champion",
                       json.dumps([{k: c[k] for k in ("key", "version", "title", "score")} for c in rules["candidates"]]),
                       json.dumps(fi["aliases"]), rules["choice"], rules["runbook_key"], rules["version"], json.dumps(rules["params"], default=str),
                       "[]", rules["score"], 1, ",".join(rules["ambiguous"]), seal, ts, seq))
        self.db._exec("UPDATE actions SET blind=?, peeked=0 WHERE id=?", (int(blind), cid))
        return self.frozen(cid, attempt)

    def record_proposal(self, cid: int, attempt: int, selector: str, choice: str, params: Dict[str, Any], cite: List[str],
                        role: str = "champion", model_digest: str = "", valid: bool = True, latency_ms: int = 0,
                        invalid_reasons: str = "") -> dict:
        """A model answer on the frozen input (WP13 calls this): sealed with its own hash; `late` when the decision came first."""
        fi = self.frozen(cid, attempt)
        if not fi:
            raise KeyError(f"no frozen input for case {cid} attempt {attempt}")
        sealed = sha256_hex(canonical({"input_sha": fi["input_sha"], "selector": selector, "model_tag": selector, "model_digest": model_digest,
                                       "choice": choice, "params": params or {}, "cite": cite or []}))
        dec = self.decision(cid, attempt)
        key = json.loads(fi["aliases"] or "{}").get(choice, choice if choice in (NONE, ESCALATE) else "")
        with self.db.tx():
            seq = self.act.audit.write("proposal.sealed", object_kind="case", object_id=cid, actor=selector, actor_kind="model",
                                       payload={"attempt": attempt, "selector": selector, "input_sha": fi["input_sha"], "seal": sealed,
                                                "late": bool(dec)})
            self.db._exec("INSERT INTO act_proposals (case_id, attempt, input_sha, selector, role, model_digest, choice, runbook_key, params, cite, "
                          "valid, invalid_reasons, late, latency_ms, sealed_hash, sealed_at, audit_seq) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, attempt, fi["input_sha"], selector, role, model_digest, key or choice, key, json.dumps(params or {}),
                           json.dumps(cite or []), int(bool(valid)), invalid_reasons, int(bool(dec)), int(latency_ms), sealed, now(), seq))
            if dec and role == "champion":
                ag = agreement({"choice": key or choice, "params": params, "valid": valid}, dec["choice"], dec["params"])
                self.db._exec("UPDATE act_decisions SET agree_model=? WHERE case_id=? AND attempt=?", (ag, cid, attempt))
        return {"sealed_hash": sealed, "late": bool(dec)}

    # ---------------------------------------------------------------- reveal, peek, decide
    def hidden(self, case: dict) -> bool:
        """True while proposals must stay hidden from everyone: blind, not peeked, no decision for the current attempt."""
        return bool(case.get("blind")) and not case.get("peeked") and self.decision(int(case["id"]), self.attempt_of(case)) is None

    def _public(self, p: dict) -> dict:
        return {"selector": p["selector"], "choice": p["choice"], "runbook_key": p.get("runbook_key") or "", "version": p.get("version"),
                "params": p.get("params") or {}, "cite": p.get("cite") or [], "score": p.get("score"), "valid": bool(p.get("valid", 1)),
                "ambiguous": [x for x in (p.get("invalid_reasons") or "").split(",") if x] if str(p["selector"]).startswith("rules") else [],
                "late": bool(p.get("late")), "sealed_hash": p.get("sealed_hash"), "sealed_at": p.get("sealed_at")}

    def view(self, case: dict) -> dict:
        cid, attempt = int(case["id"]), self.attempt_of(case)
        props = self.proposals(cid, attempt)
        rules = next((p for p in props if str(p["selector"]).startswith("rules")), None)
        model = next((p for p in props if not str(p["selector"]).startswith("rules") and p.get("role") == "champion"), None)
        state = "none" if rules is None else ("blind" if self.hidden(case) else ("revealed" if case.get("blind") else "visible"))
        out: Dict[str, Any] = {"state": state, "attempt": attempt, "sealed_at": rules["sealed_at"] if rules else None,
                               "sealed_hash": rules["sealed_hash"] if rules else None,
                               "model": "skipped" if case.get("injection_suspect") else ("sealed" if model else "pending")}
        if state in ("visible", "revealed"):
            out["rules"] = self._public(rules)
            out["model_proposal"] = self._public(model) if model else None
        return out

    def candidates_view(self, case: dict) -> List[dict]:
        cid, attempt = int(case["id"]), self.attempt_of(case)
        rules = next((p for p in self.proposals(cid, attempt) if str(p["selector"]).startswith("rules")), None)
        if not rules:
            return []
        hidden = self.hidden(case)
        out = []
        for c in sorted(rules.get("candidates") or [], key=lambda c: c["key"] if hidden else -float(c.get("score") or 0)):
            item = {"key": c["key"], "version": c.get("version"), "title": c.get("title")}
            if not hidden:
                item["score"] = c.get("score")
                item["suggested"] = c["key"] == rules.get("choice")
            out.append(item)
        return out

    def similar(self, case: dict, n: int = 3, with_runbook: Optional[bool] = None) -> List[dict]:
        """Closed cases with the same signature (or family); without their runbook while the case is blind (C51)."""
        show = (not self.hidden(case)) if with_runbook is None else with_runbook
        rows = self.db._exec("SELECT id, title, family, runbook_key, outcome, first_seen, closed_at FROM actions WHERE kind='case' AND id<>? "
                             "AND (signature=? OR family=?) AND closed_at<>'' ORDER BY id DESC LIMIT ?",
                             (case["id"], case.get("signature") or "-", case.get("family") or "-", int(n)))
        out = []
        for r in rows:
            try:
                mins = int((datetime.fromisoformat(r["closed_at"]) - datetime.fromisoformat(r["first_seen"])).total_seconds() // 60)
            except (TypeError, ValueError):
                mins = None
            out.append({"id": r["id"], "title": r["title"], "family": r["family"], "runbook": (r["runbook_key"] or None) if show else None,
                        "outcome": r["outcome"] or "", "minutes": mins})
        return out

    def peek(self, cid: int, by: str) -> dict:
        case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        if not case.get("blind"):
            return self.view(case)
        if self.decision(cid, self.attempt_of(case)) is not None:
            return self.view(case)
        with self.db.tx():
            self.db._exec("UPDATE actions SET peeked=1 WHERE id=?", (cid,))
            self.act.audit.write("proposal.peeked", object_kind="case", object_id=cid, actor=by, actor_kind="user", team_id=case.get("team_id"),
                                 payload={"attempt": self.attempt_of(case)})
        case["peeked"] = 1
        return self.view(case)

    def decide(self, cid: int, by: str, choice: str, version: Optional[int] = None, params: Optional[Dict[str, Any]] = None,
               reason_code: str = "", note: str = "", level: Optional[int] = None, implicit: bool = False) -> dict:
        case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        attempt = self.attempt_of(case)
        if self.frozen(cid, attempt) is None:
            with self.db.tx():
                self.seal(cid)                                       # cases from before WP7: seal before the first decision
            case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        if self.decision(cid, attempt) is not None:
            raise ShadowError("act_already_decided", "Bu deneme için karar verildi", "This attempt is already decided")
        params = dict(params or {})
        rb_key = ""
        if choice not in (NONE, ESCALATE, MANUAL):
            rows = self.db._exec("SELECT key FROM act_runbooks WHERE key=?", (choice,))
            if not rows:
                raise ShadowError("act_param_invalid", f"Bilinmeyen runbook: {choice}", f"Unknown runbook: {choice}", 422)
            rb_key = choice
        if reason_code and reason_code not in REASON_CODES:
            raise ShadowError("act_param_invalid", "Geçersiz neden kodu", "Invalid reason code", 422)
        if reason_code == "other" and not (note or "").strip():
            raise ShadowError("act_reason_required", "\"Diğer\" için not zorunlu", "A note is required for \"other\"", 422)
        blind, peeked = bool(case.get("blind")), bool(case.get("peeked"))
        props = self.proposals(cid, attempt)
        rules = next((p for p in props if str(p["selector"]).startswith("rules")), None)
        model = next((p for p in props if not str(p["selector"]).startswith("rules") and p.get("role") == "champion"), None)
        ag_rules = agreement(rules, choice, params)
        ag_model = agreement(model, choice, params) if model else "n/a"
        visible = not blind or peeked
        if visible and not implicit and not reason_code and any(a in ("differ", "choice") for a in (ag_rules, ag_model)):
            raise ShadowError("act_reason_required", "Öneriden farklı karar için bir neden seçin", "Pick a reason when deciding against the proposal",
                              422)
        ts = now()
        with self.db.tx():
            seq = self.act.audit.write("decision", object_kind="case", object_id=cid, actor=by, actor_kind="user" if "@" in by else "system",
                                       team_id=case.get("team_id"),
                                       payload={"attempt": attempt, "choice": choice, "version": version, "params": params, "blind": blind,
                                                "peeked": peeked, "agree_rules": ag_rules, "agree_model": ag_model, "reason_code": reason_code})
            self.db._exec("INSERT INTO act_decisions (case_id, attempt, actor, choice, runbook_key, version, params, reason_code, note, blind, peeked, "
                          "agree_rules, agree_model, decided_at, level_at_decision) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, attempt, self.act.audit.actor(by, "user"), choice, rb_key, version, json.dumps(params, default=str), reason_code,
                           privacy.scrub_strict(note or "")[:1000], int(blind), int(peeked), ag_rules, ag_model, ts, level))
            if blind and not peeked:
                self.db._exec("UPDATE act_proposals SET revealed_at=? WHERE case_id=? AND attempt=?", (ts, cid, attempt))
                self.act.audit.write("proposal.revealed", object_kind="case", object_id=cid, team_id=case.get("team_id"),
                                     payload={"attempt": attempt, "after_decision_seq": seq})
            if rb_key:
                self.db._exec("UPDATE actions SET runbook_key=?, updated_at=? WHERE id=?", (rb_key, ts, cid))
        case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        v = self.view(case)
        res = {"decision": {"choice": choice, "attempt": attempt, "blind": blind, "peeked": peeked, "agree_rules": ag_rules,
                            "agree_model": ag_model, "reason_code": reason_code},
               "proposals": {"rules": v.get("rules"), "model": v.get("model_proposal")}, "plan": None}
        if blind and ag_rules in ("full", "choice"):
            res["reveal"] = {"tr": "Sistem de aynısını seçmişti ✓", "en": "The system had chosen the same ✓", "sealed_at": v.get("sealed_at")}
        elif blind:
            res["reveal"] = {"tr": "Sistem farklı seçmişti", "en": "The system had chosen differently", "sealed_at": v.get("sealed_at")}
        return res

    def implicit(self, cid: int, by: str, choice: str, note: str = "") -> Optional[dict]:
        """dismiss → NONE, escalate → ESCALATE, close by hand → MANUAL, when the attempt has no decision yet."""
        case = dict(self.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
        if self.decision(cid, self.attempt_of(case)) is not None:
            return None
        return self.decide(cid, by, choice, reason_code="other" if note else "", note=note or "", implicit=True)
