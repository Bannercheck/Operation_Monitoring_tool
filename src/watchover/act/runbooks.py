"""Runbooks (docs/design/action-agent.md §6): versioned, typed procedures. A version body is immutable once submitted; a
change is a new version. Lint blocks submission; a reviewer who is not the author publishes (two, one of them an installation
act.policy holder, for R3); diagnose-only runbooks may be self-published. Each step has a semantic hash: a new version keeps
the trust cells of unchanged steps and resets only the changed ones (their events are archived, not deleted).

Every executable step binds to a catalogue template (host actuator, executor "agent", the default) or to a connector job
(executor "http": AWX / Rundeck / Jenkins / generic, `exec` binding); `manual`, `itsm` and `notify` are the other executors.
The executors themselves arrive in later work packages; the step's executor is part of the version body (and its hash) now."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import params as P
from .conditions import STEP_FLAGS, check as check_condition
from .types import EXECUTORS, HTTP_KINDS, RISKS, STEP_TYPES, canonical, cell_key, now, sha256_hex

SLOTS = ("host", "env", "os", "host_class", "criticality", "metric", "value", "limit", "mount", "use_pct", "log_dir", "path", "service",
         "unit", "win_service", "app_pool", "iis_site", "pid", "sap_sid", "sap_nr", "wp_no", "wp_runtime_s", "wp_program", "flow_id",
         "step", "stuck", "url")
CASE_FIELDS = ("host", "service", "env")
PRECONDITIONS = ("actuator_ready", "agent_online", "template_available", "no_other_run_on_host", "metric_at_least", "metric_below",
                 "service_state", "fact", "case_still_active", "not_frozen", "in_window", "pool_healthy_at_least", "inventory_status",
                 "change_approved", "clock_skew_below")
ELSE = ("stop", "ask", "guided", "wait")
CHECKS = ("metric_below", "metric_above", "log_absent", "log_present", "anomaly_cleared", "http_ok", "bizflow_stuck_below", "slo_ok")
SOURCES = ("threshold", "anomaly", "query", "incident", "bizflow", "manual", "external")
KEY_RX = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
STEP_RX = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
PH = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
STATUSES = ("draft", "in_review", "approved", "rejected", "superseded", "retired")
HASH_KEYS = ("type", "template", "template_sha256", "args", "when", "expect", "dry_run", "dry_expect", "rollback", "verify", "checks",
             "executor", "exec")


class RunbookError(Exception):
    def __init__(self, code: str, tr: str, en: str, status: int = 409, **extra):
        super().__init__(en)
        self.code, self.tr, self.en, self.status, self.extra = code, tr, en, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en, **self.extra}


def _f(level: str, code: str, where: str = "", detail: str = "") -> dict:
    return {"level": level, "code": code, "step": where, "detail": detail}


# ---------------------------------------------------------------- step hashes and risk
def step_hash(step: Dict[str, Any], body: Dict[str, Any], templates: Optional[Dict[str, dict]] = None) -> str:
    """sha256 over the step's semantic fields plus the schema of every parameter it references (titles, why, texts excluded)."""
    sem = {k: step[k] for k in HASH_KEYS if k in step}
    if step.get("template") and templates and step["template"] in templates:
        sem["template_sha256"] = templates[step["template"]]["sha256"]
    used = set()
    for v in [step.get("args"), step.get("checks"), step.get("exec"), step.get("when"), step.get("expect")]:
        used |= {m.split(".")[0] for m in PH.findall(json.dumps(v, ensure_ascii=False))} if v is not None else set()
    prm = body.get("params") or {}
    sem["params"] = {k: {kk: vv for kk, vv in prm[k].items() if kk not in ("title", "hint")} for k in sorted(used) if k in prm}
    return sha256_hex(canonical(sem))


def step_hashes(body: Dict[str, Any], templates: Optional[Dict[str, dict]] = None) -> Dict[str, str]:
    return {s.get("id"): step_hash(s, body, templates) for s in body.get("steps") or [] if s.get("id")}


def risk_effective(body: Dict[str, Any], templates: Dict[str, dict]) -> str:
    """max(declared risk, the highest template risk of its act / rollback steps); the +1 for a critical / high target host is
    applied per run (§6.8)."""
    r = body.get("risk") if body.get("risk") in RISKS else "R0"
    for s in body.get("steps") or []:
        if s.get("type") in ("act", "rollback"):
            t = templates.get(s.get("template") or "")
            if t and t.get("risk") in RISKS and RISKS.index(t["risk"]) > RISKS.index(r):
                r = t["risk"]
            if s.get("risk") in RISKS and RISKS.index(s["risk"]) > RISKS.index(r):
                r = s["risk"]
    return r


def diagnose_only(body: Dict[str, Any]) -> bool:
    return all(s.get("type") in ("diagnose", "verify", "wait", "notify", "manual") for s in body.get("steps") or [])


# ---------------------------------------------------------------- lint (§6.9)
def lint(body: Dict[str, Any], templates: Dict[str, dict], policy: Optional[Dict[str, Any]] = None) -> List[dict]:
    pol = policy or {}
    out: List[dict] = []
    err = lambda code, where="", detail="": out.append(_f("error", code, where, detail))    # noqa: E731
    warn = lambda code, where="", detail="": out.append(_f("warning", code, where, detail))  # noqa: E731
    if not KEY_RX.match(str(body.get("key", ""))):
        err("bad_key", "", "lowercase letters, digits and dashes, 3-64")
    if not str(body.get("title", "")).strip():
        err("title")
    if body.get("risk") not in RISKS:
        err("risk", "", "declared risk R0-R4")
    if not isinstance(body.get("max_level", 4), int) or not 0 <= body.get("max_level", 4) <= 4:
        err("max_level")
    m = body.get("match") or {}
    if set(m.get("sources") or []) - set(SOURCES):
        err("match_sources", "", ", ".join(sorted(set(m["sources"]) - set(SOURCES))))
    oses = (body.get("applies_to") or {}).get("os") or ["linux"]
    if set(oses) - {"linux", "windows"}:
        err("applies_os")
    steps = body.get("steps") or []
    if not steps:
        err("no_steps")
    ids = [s.get("id") for s in steps]
    for d in {i for i in ids if ids.count(i) > 1}:
        err("duplicate_step", d)
    sidx = {s.get("id"): i for i, s in enumerate(steps)}
    by_id = {s.get("id"): s for s in steps}

    # parameters: typed (closed set, I1) and sourced
    prm = body.get("params") or {}
    for name, spec in prm.items():
        ty = (spec or {}).get("type")
        if ty not in P.TYPES:
            err("param_type", name, f"{ty!r} (no free-string type)")
        src = spec.get("from") or []
        if not src:
            err("param_source", name, "every parameter names where its value comes from")
        for s in src:
            if s in ("default", "ask", "select"):
                if s == "default" and "default" not in spec:
                    err("param_default", name)
                continue
            parts = str(s).split(".")
            if parts[0] == "case" and len(parts) == 2 and parts[1] in CASE_FIELDS:
                continue
            if parts[0] == "evidence" and len(parts) == 2 and parts[1] in SLOTS:
                continue
            if parts[0] == "inventory" and len(parts) == 2:
                continue
            if parts[0] == "steps" and len(parts) >= 3 and parts[1] in sidx:
                continue
            err("param_source", name, str(s))
        if ty == "pid" and not any(str(s).startswith("steps.") for s in src):
            err("pid_source", name, "a pid comes only from a diagnose capture of the same run")
        if ty == "path" and not spec.get("roots"):
            err("missing_roots", name)
        if "default" in spec and ty in P.TYPES and ty not in ("host", "pid", "url"):
            try:
                P.validate(spec, spec["default"])
            except P.ParamError as e:
                err("param_default", name, e.code)

    # captures available after each step: declared captures + the template parser's fields + flags
    from .templates import PARSERS
    caps: Dict[str, Set[str]] = {}
    for s in steps:
        t = templates.get(s.get("template") or "") or {}
        parser = ((t.get("body") or {}).get("output") or {}).get("parser", "")
        caps[s.get("id")] = set((s.get("capture") or {}).keys()) | set(PARSERS.get(parser, {}).keys()) | {"out"}
    known_ids = set(sidx)

    def refs_ok(v, where, i):
        for ph in PH.findall(json.dumps(v, ensure_ascii=False)) if v is not None else []:
            head = ph.split(".")
            if head[0] in prm and len(head) == 1:
                continue
            if head[0] == "steps":
                if len(head) < 3 or head[1] not in sidx:
                    err("binding", where, f"{{{ph}}}: unknown step")
                elif sidx[head[1]] >= i and by_id[where].get("type") != "rollback":
                    err("binding", where, f"{{{ph}}}: a later step")
                continue
            if head[0] in ("inventory", "case") and len(head) == 2:
                continue
            if head[0] == "host":
                continue
            err("binding", where, f"{{{ph}}}: not a parameter")

    def cond_ok(expr, where, bare=()):
        for e in check_condition(expr, steps=known_ids, captures=caps, bare=bare):
            err("condition", where, e)

    act_steps, verify_independent = [], False
    for i, s in enumerate(steps):
        sid, typ = s.get("id"), s.get("type")
        if not STEP_RX.match(str(sid or "")):
            err("step_id", str(sid))
        if typ not in STEP_TYPES:
            err("step_type", sid, str(typ))
            continue
        exe = s.get("executor") or ""
        if exe and exe not in EXECUTORS:
            err("executor", sid, exe)
        if isinstance(s.get("when"), str) and s["when"]:
            cond_ok(s["when"], sid)
        refs_ok(s.get("args"), sid, i)
        t = templates.get(s.get("template") or "")
        if typ in ("diagnose", "act", "rollback"):
            binding = s.get("exec")
            if binding:
                _lint_exec(binding, sid, prm, err)
                if exe not in ("", "http"):
                    err("executor", sid, "a connector binding runs through the http executor")
                if typ == "act" and s.get("reversibility") not in ("transient", "data_preserving", "reversible", "irreversible"):
                    err("reversibility", sid, "a connector job declares its reversibility")
            elif not s.get("template"):
                err("template_missing", sid, "an executable step binds a catalogue template or a connector job")
            elif t is None:
                err("template_unknown", sid, s["template"])
            else:
                want = "diag" if typ == "diagnose" else "act"
                if t.get("kind") != want:
                    err("template_kind", sid, f"{s['template']} is {t.get('kind')}")
                if t.get("os") not in oses and len(oses) == 1:
                    err("template_os", sid, f"{s['template']} is for {t.get('os')}")
                tparams = (t.get("body") or {}).get("params") or {}
                args = s.get("args") or {}
                for a in args:
                    if a not in tparams:
                        err("arg_unknown", sid, a)
                for pn, ps in tparams.items():
                    if pn not in args and "default" not in ps:
                        err("arg_missing", sid, pn)
                if exe == "http":
                    err("executor", sid, "an http step needs an exec binding")
        if typ == "act":
            act_steps.append(i)
            v = s.get("verify")
            if not v or v not in by_id or by_id[v].get("type") != "verify":
                err("verify_missing", sid, "every act step names a verify step")
            else:
                for j in range(i + 1, sidx[v]):
                    if steps[j].get("type") == "manual":
                        err("manual_before_verify", sid, steps[j].get("id"))
            rev = (t or {}).get("reversibility") or s.get("reversibility") or ""
            rb = s.get("rollback")
            if rev == "reversible" and not rb:
                err("rollback_missing", sid, "a reversible step names its rollback step")
            if rb and (rb not in by_id or by_id[rb].get("type") != "rollback"):
                err("rollback_unknown", sid, str(rb))
            dr = s.get("dry_run", "none")
            if dr not in ("required", "optional", "none"):
                err("dry_run", sid)
            if dr == "required" and t is not None and not ((t.get("body") or {}).get("dry_run")):
                err("dry_run", sid, "the template has no dry-run")
            if s.get("dry_expect"):
                cond_ok(s["dry_expect"], sid, bare=caps.get(sid, set()) | {"files", "bytes"})
        if typ == "verify":
            checks = s.get("checks") or []
            if not checks:
                err("verify_empty", sid, "a human claim alone is not a check")
            for c in checks:
                kind = [k for k in c if k in CHECKS]
                if c.get("template"):
                    tt = templates.get(c["template"])
                    if tt is None:
                        err("template_unknown", sid, c["template"])
                    elif tt.get("kind") != "diag":
                        err("template_kind", sid, f"verify templates are read-only: {c['template']}")
                    if c.get("expect"):
                        tp = ((tt or {}).get("body") or {}).get("output", {}).get("parser", "")
                        cond_ok(c["expect"], sid, bare=set(PARSERS.get(tp, {})) | set(c.get("capture") or {}))
                    refs_ok(c.get("args"), sid, i)
                    if c.get("independent"):
                        err("independent", sid, "a check on the acting host's own actuator is not independent")
                elif len(kind) != 1:
                    err("check_kind", sid, json.dumps(c, ensure_ascii=False)[:80])
                else:
                    refs_ok(c[kind[0]], sid, i)
                    if c.get("independent"):
                        if _independent_ok(kind[0], c[kind[0]]):
                            verify_independent = True
                        else:
                            err("independent", sid, f"{kind[0]} is not independent of the acting host")
                if c.get("when"):
                    cond_ok(c["when"], sid)
            if s.get("require", "all") not in ("all", "any"):
                err("require", sid)
            if int(s.get("retries", 0) or 0) > 10:
                err("retries", sid, "<= 10")
            if s.get("on_fail", "rollback") not in ("rollback", "escalate", "guided"):
                err("on_fail", sid)
        if typ == "manual" and not str(s.get("text", "")).strip():
            err("manual_text", sid)
        if typ == "choose":
            opts = s.get("options") or []
            if not opts:
                err("choose_options", sid)
            for o in opts:
                if o.get("to") not in by_id:
                    err("choose_target", sid, str(o.get("to")))
            for w in s.get("when") if isinstance(s.get("when"), list) else []:
                cond_ok(w.get("if", ""), sid)
                if w.get("to") not in by_id:
                    err("choose_target", sid, str(w.get("to")))
        if typ == "wait" and not (0 < int(s.get("seconds", 0) or 0) <= 3600 or s.get("until")):
            err("wait", sid, "1..3600 s or until a verify passes")
        if typ in ("notify",) and not str(s.get("text", "")).strip():
            err("notify_text", sid)

    # preconditions, watch / expected noise (R7), freeze_exempt (R10), blast
    for pc in body.get("preconditions") or []:
        if pc.get("check") not in PRECONDITIONS:
            err("precondition", "", str(pc.get("check")))
        if pc.get("else", "stop") not in ELSE:
            err("precondition_else", "", str(pc.get("else")))
        if pc.get("if"):
            cond_ok(pc["if"], "")
    w = body.get("watch") or {}
    if not 5 <= int(w.get("minutes", 15) or 15) <= 240:
        err("watch_minutes", "", "5..240")
    noise = w.get("expected_noise") or []
    if len(noise) > int(pol.get("act_expected_noise_max", 5)):
        err("expected_noise", "", f"at most {pol.get('act_expected_noise_max', 5)} entries")
    for n in noise:
        tid = n.get("template") if isinstance(n, dict) else n
        if not isinstance(n, dict) or not str(n.get("justification", "")).strip():
            err("expected_noise", "", f"{tid}: each entry needs a justification")
        if not tid or any(ch in str(tid) for ch in "*?[]%"):
            err("expected_noise", "", f"{tid}: an exact template id, no wildcards")
    if int(w.get("expected_noise_s", 0) or 0) > int(pol.get("act_expected_noise_s_max", 300)):
        err("expected_noise_s", "", f"<= {pol.get('act_expected_noise_s_max', 300)}")
    for s in steps:
        if int(s.get("expect_silence_s", 0) or 0) > int(pol.get("act_expect_silence_s_max", 120)):
            err("expect_silence_s", s.get("id"), f"<= {pol.get('act_expect_silence_s_max', 120)}")
    if (body.get("windows") or {}).get("freeze_exempt") and not diagnose_only(body):
        err("freeze_exempt", "", "only runbooks without act or rollback steps may be freeze-exempt")
    for k, v in (body.get("blast") or {}).items():
        if not isinstance(v, (int, float)) or v < 0:
            err("blast", "", k)
    if act_steps and not verify_independent:
        warn("no_independent_verify", "", "bağımsız doğrulama yok: en fazla Onaylı")
    return out


def _independent_ok(kind: str, spec: Dict[str, Any]) -> bool:
    spec = spec or {}
    if kind == "http_ok":
        pf = str(spec.get("probe_from", ""))
        return pf == "server" or pf.startswith("peer:")
    if kind in ("slo_ok", "bizflow_stuck_below"):
        return True
    if kind in ("log_absent", "log_present"):
        return spec.get("hosts") == "others" or (spec.get("host") not in (None, "", "{host}") and "{host}" not in str(spec.get("query", "")))
    return False


def _lint_exec(binding: Dict[str, Any], sid: str, prm: Dict[str, Any], err) -> None:
    """HTTP executor binding (§9.5): one connector kind, the remote job fixed in the version, {host} only as the whole value of
    limit / filter (R2)."""
    if not isinstance(binding, dict) or len(binding) != 1:
        err("exec_binding", sid, "exactly one of awx / rundeck / jenkins / generic")
        return
    kind, spec = next(iter(binding.items()))
    if kind not in HTTP_KINDS:
        err("exec_binding", sid, kind)
        return
    spec = spec or {}
    if not spec.get("connector"):
        err("exec_connector", sid)
    job = spec.get("job_template", spec.get("job", spec.get("url", "")))
    if job in ("", None) or PH.search(str(job)):
        err("exec_job", sid, "the remote job is fixed in the runbook version, never a parameter")
    for k, v in spec.items():
        if k in ("connector", "job_template", "job", "url", "vars", "options", "params"):
            continue
        if "{host}" in str(v) and not (k in ("limit", "filter") and str(v) in ("{host}", "name: {host}")):
            err("exec_host", sid, f"{{host}} only as the whole value of limit / filter ({k})")
    for k, v in (spec.get("vars") or spec.get("options") or spec.get("params") or {}).items():
        if "{host}" in str(v):
            err("exec_host", sid, f"{{host}} only in limit / filter, not in {k}")
        for ph in PH.findall(str(v)):
            if ph.split(".")[0] not in prm and not ph.startswith("steps."):
                err("binding", sid, f"{{{ph}}}: not a parameter")


def errors(findings: Iterable[dict]) -> List[dict]:
    return [f for f in findings if f["level"] == "error"]


# ---------------------------------------------------------------- the store (act_runbooks, act_runbook_versions)
class Runbooks:
    def __init__(self, db, audit, catalogue, policy, approvals):
        self.db, self.audit, self.catalogue, self.policy, self.approvals = db, audit, catalogue, policy, approvals

    # ---- reads
    def get(self, key: str) -> dict:
        rows = self.db._exec("SELECT * FROM act_runbooks WHERE key=?", (key,))
        if not rows:
            raise KeyError(key)
        return rows[0]

    def list(self, team_ids: Optional[List[int]] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_runbooks ORDER BY key")
        if team_ids is not None:
            rows = [r for r in rows if r["team_id"] is None or r["team_id"] in team_ids]
        for r in rows:
            r["versions"] = self.db._exec("SELECT version, status, risk_effective, author, created_at FROM act_runbook_versions "
                                          "WHERE runbook_id=? ORDER BY version", (r["id"],))
        return rows

    def version(self, key: str, v: Optional[int] = None) -> dict:
        rb = self.get(key)
        if v is None:
            rows = self.db._exec("SELECT * FROM act_runbook_versions WHERE runbook_id=? ORDER BY version DESC LIMIT 1", (rb["id"],))
        else:
            rows = self.db._exec("SELECT * FROM act_runbook_versions WHERE runbook_id=? AND version=?", (rb["id"], int(v)))
        if not rows:
            raise KeyError(f"{key} v{v}")
        r = dict(rows[0])
        for k in ("body", "step_hashes", "lint"):
            try:
                r[k] = json.loads(r[k] or ("{}" if k != "lint" else "[]"))
            except ValueError:
                pass
        r["key"], r["team_id"] = key, rb["team_id"]
        return r

    # ---- writes
    def _templates(self) -> Dict[str, dict]:
        return self.catalogue.approved()

    def _lint(self, body) -> Tuple[List[dict], Dict[str, str], str]:
        tpl = self._templates()
        return lint(body, tpl, self.policy.all()), step_hashes(body, tpl), risk_effective(body, tpl)

    def create(self, key: str, title: str, team_id: Optional[int], body: Dict[str, Any], by: str, origin: str = "human",
               origin_ref: str = "", playbook_key: str = "") -> dict:
        if not KEY_RX.match(key or ""):
            raise RunbookError("act_param_invalid", "Geçersiz runbook anahtarı", "Invalid runbook key", 422)
        if self.db._exec("SELECT 1 AS x FROM act_runbooks WHERE key=?", (key,)):
            raise RunbookError("act_conflict", "Bu anahtarla bir runbook var", "A runbook with this key exists", 409)
        body = dict(body or {}, key=key, version=1, title=title or (body or {}).get("title") or key, team_id=team_id)
        findings, hashes, risk = self._lint(body)
        ts = now()
        with self.db.tx():
            rid = self.db._insert("INSERT INTO act_runbooks (key, team_id, title, summary, origin, origin_ref, playbook_key, status, current_version, "
                                  "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (key, team_id, body["title"], str(body.get("summary", ""))[:500], origin, origin_ref, playbook_key, "active", None,
                                   by, ts, ts))
            self._insert_version(rid, 1, body, by, findings, hashes, risk)
            if playbook_key:
                self.link_playbook(playbook_key, key, by)
            self.audit.write("runbook.created", object_kind="runbook", object_id=key, actor=by, actor_kind=_kind(by), team_id=team_id,
                             payload={"origin": origin, "version": 1})
        return self.version(key, 1)

    def _insert_version(self, rid, v, body, by, findings, hashes, risk) -> int:
        return self.db._insert("INSERT INTO act_runbook_versions (runbook_id, version, status, body, body_sha256, step_hashes, risk, risk_effective, "
                               "author, lint, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               (rid, v, "draft", _store(body), sha256_hex(canonical(body)), json.dumps(hashes), body.get("risk", ""), risk,
                                by, json.dumps(findings, ensure_ascii=False), now()))

    def put_draft(self, key: str, body: Dict[str, Any], by: str, change_note: str = "") -> dict:
        """Edit the open draft, or start a new version (a submitted version is immutable)."""
        rb = self.get(key)
        last = self.version(key)
        v = last["version"] if last["status"] == "draft" else last["version"] + 1
        body = dict(body or {}, key=key, version=v, team_id=rb["team_id"])
        body.setdefault("title", rb["title"])
        findings, hashes, risk = self._lint(body)
        with self.db.tx():
            if last["status"] == "draft":
                self.db._exec("UPDATE act_runbook_versions SET body=?, body_sha256=?, step_hashes=?, risk=?, risk_effective=?, lint=?, change_note=? "
                              "WHERE id=? AND status='draft'", (_store(body), sha256_hex(canonical(body)), json.dumps(hashes), body.get("risk", ""),
                                                                risk, json.dumps(findings, ensure_ascii=False), change_note[:500], last["id"]))
            else:
                self._insert_version(rb["id"], v, body, by, findings, hashes, risk)
            self.audit.write("runbook.draft", object_kind="runbook", object_id=key, actor=by, actor_kind=_kind(by), team_id=rb["team_id"],
                             payload={"version": v})
        return self.version(key, v)

    def relint(self, key: str, v: int) -> List[dict]:
        r = self.version(key, v)
        findings, hashes, risk = self._lint(r["body"])
        if r["status"] == "draft":
            self.db._exec("UPDATE act_runbook_versions SET lint=?, step_hashes=?, risk_effective=? WHERE id=?",
                          (json.dumps(findings, ensure_ascii=False), json.dumps(hashes), risk, r["id"]))
        return findings

    def submit(self, key: str, v: int, by: str, tabletop: bool = False) -> dict:
        r = self.version(key, v)
        if r["status"] != "draft":
            raise RunbookError("act_immutable", "Gönderilmiş sürüm değiştirilemez", "A submitted version is immutable")
        findings = self.relint(key, v)
        if errors(findings):
            raise RunbookError("act_lint", "Lint hataları var", "Lint errors block submission", 422, findings=errors(findings))
        body = r["body"]
        automatable = any(s.get("type") == "act" for s in body.get("steps") or [])
        if automatable and not (r.get("dry_run_ref") or tabletop):
            raise RunbookError("act_dry_run_required", "Yayın için bir kuru çalıştırma ya da 'Masa başı prova yapıldı' gerekli",
                               "Publishing needs a recorded dry-run or the tabletop check", 422)
        risk = r["risk_effective"] or body.get("risk") or "R0"
        r3 = RISKS.index(risk) >= 3
        diag = diagnose_only(body)
        with self.db.tx():
            aid = self.approvals.request("runbook_version", r["id"], requester=by, need=2 if r3 else 1, team_id=r["team_id"],
                                         four_eyes=r3, stepup=r3, requires=("policy",) if r3 else (),
                                         exclude=() if diag else (r["author"],), reason=f"{key} v{v}")
            self.db._exec("UPDATE act_runbook_versions SET status='in_review', submitted_at=?, approval_id=?, tabletop=? WHERE id=?",
                          (now(), aid, 1 if tabletop else 0, r["id"]))
            self.audit.write("runbook.submitted", object_kind="runbook", object_id=key, actor=by, actor_kind=_kind(by), team_id=r["team_id"],
                             payload={"version": v, "risk": risk, "approval_id": aid, "body_sha256": r["body_sha256"], "self_publish": diag})
        return self.version(key, v)

    def review(self, key: str, v: int, by: str, decision: str, reason: str = "", tags: Iterable[str] = (), stepup_method: str = "") -> dict:
        r = self.version(key, v)
        if r["status"] != "in_review":
            raise RunbookError("act_not_pending", "Bu sürüm incelemede değil", "This version is not in review")
        diag = diagnose_only(r["body"])
        if not diag and by.strip().lower() == (r["author"] or "").strip().lower():
            raise RunbookError("act_sod", "Yazar kendi runbook'unu yayınlayamaz", "The author cannot publish their own runbook", 403)
        st = self.approvals.vote(r["approval_id"], by, decision, reason, tags=tags, stepup_method=stepup_method) if not (
            diag and by.strip().lower() == (r["author"] or "").strip().lower()) else self._self_publish(r, by, decision, reason, stepup_method)
        if st["status"] == "approved":
            self._publish(key, v, by)
        elif st["status"] == "rejected":
            with self.db.tx():
                self.db._exec("UPDATE act_runbook_versions SET status='rejected', decided_at=? WHERE id=?", (now(), r["id"]))
                self.audit.write("runbook.rejected", object_kind="runbook", object_id=key, actor=by, actor_kind="user", team_id=r["team_id"],
                                 payload={"version": v, "reason": reason[:300]})
        out = self.version(key, v)
        out["approval"] = st
        return out

    def _self_publish(self, r, by, decision, reason, stepup_method) -> dict:
        """Diagnose-only runbooks: an act.publish holder may publish their own (§6.9). Recorded as an ordinary vote."""
        a = self.approvals.get(r["approval_id"])
        with self.db.tx():
            seq = self.audit.write("vote", object_kind="approval", object_id=a["id"], actor=by, actor_kind="user", team_id=r["team_id"],
                                   payload={"decision": decision, "self_publish": True})
            self.db._exec("INSERT INTO act_votes (approval_id, voter, decision, reason, ts, stepup_method, audit_seq, tags) VALUES (?,?,?,?,?,?,?,?)",
                          (a["id"], by.strip().lower(), decision, reason[:300], now(), stepup_method, seq, "[]"))
            return self.approvals._settle(a["id"])

    def _publish(self, key: str, v: int, by: str) -> None:
        rb = self.get(key)
        r = self.version(key, v)
        prev = None
        if rb.get("current_version"):
            try:
                prev = self.version(key, rb["current_version"])
            except KeyError:
                prev = None
        with self.db.tx():
            self.db._exec("UPDATE act_runbook_versions SET status='superseded' WHERE runbook_id=? AND status='approved'", (rb["id"],))
            self.db._exec("UPDATE act_runbook_versions SET status='approved', decided_at=? WHERE id=?", (now(), r["id"]))
            self.db._exec("UPDATE act_runbooks SET current_version=?, title=?, summary=?, updated_at=? WHERE id=?",
                          (v, r["body"].get("title", rb["title"]), str(r["body"].get("summary", ""))[:500], now(), rb["id"]))
            reset = self._apply_hash_changes(key, (prev or {}).get("step_hashes") or {}, r["step_hashes"] or {}, by) if prev else []
            self.audit.write("runbook.approved", object_kind="runbook", object_id=key, actor=by, actor_kind="user", team_id=rb["team_id"],
                             payload={"version": v, "body_sha256": r["body_sha256"], "step_hashes": r["step_hashes"], "reset_steps": reset})

    def _apply_hash_changes(self, key: str, old: Dict[str, str], new: Dict[str, str], by: str) -> List[str]:
        """Cells of steps whose semantic hash changed fall to min(level, 1) and their events are archived; unchanged steps keep
        their cells (§6.9). Removed steps' cells are archived the same way."""
        changed = sorted(s for s in set(old) | set(new) if old.get(s) != new.get(s))
        for step in changed:
            cells = self.db._exec("SELECT cell_key, level FROM act_cells WHERE runbook_key=? AND step_key=?", (key, step))
            for c in cells:
                to = min(int(c["level"] or 0), 1)
                self.db._exec("UPDATE act_cells SET level=?, step_hash=?, level_since=?, t=0, s_eff=0, f_eff=0, updated_at=? WHERE cell_key=?",
                              (to, new.get(step, ""), now(), now(), c["cell_key"]))
                self.db._exec("UPDATE act_trust_events SET archived=1, archive_reason=? WHERE cell_key=? AND archived=0",
                              ("step_changed", c["cell_key"]))
                if to != int(c["level"] or 0):
                    seq = self.audit.write("level.demoted", object_kind="cell", object_id=c["cell_key"], actor=by, actor_kind="user",
                                           payload={"from": c["level"], "to": to, "reason": "step_changed"})
                    self.db._exec("INSERT INTO act_level_events (cell_key, from_level, to_level, kind, reason, actor, ts, audit_seq) VALUES (?,?,?,?,?,?,?,?)",
                                  (c["cell_key"], c["level"], to, "demote", "step_changed", self.audit.actor(by, "user"), now(), seq))
        return changed

    def retire(self, key: str, by: str) -> dict:
        rb = self.get(key)
        with self.db.tx():
            self.db._exec("UPDATE act_runbooks SET status='retired', updated_at=? WHERE id=?", (now(), rb["id"]))
            self.db._exec("UPDATE act_runbook_versions SET status='retired' WHERE runbook_id=? AND status='approved'", (rb["id"],))
            self.audit.write("runbook.retired", object_kind="runbook", object_id=key, actor=by, actor_kind="user", team_id=rb["team_id"], payload={})
        return self.get(key)

    def diff(self, key: str, a: int, b: int) -> dict:
        va, vb = self.version(key, a), self.version(key, b)
        ha, hb = va["step_hashes"] or {}, vb["step_hashes"] or {}
        steps = []
        for s in sorted(set(ha) | set(hb)):
            state = "same" if ha.get(s) == hb.get(s) else ("added" if s not in ha else "removed" if s not in hb else "changed")
            steps.append({"step": s, "state": state,
                          "trust": "kept" if state == "same" else "reset",
                          "text_tr": "" if state == "same" else f"Bu değişiklik {s} adımının güvenini sıfırlar"})
        return {"key": key, "from": a, "to": b, "steps": steps}

    def link_playbook(self, playbook_key: str, runbook_key: str, by: str) -> None:
        self.db._exec("INSERT INTO act_playbook_links (playbook_key, runbook_key, created_by, ts) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                      (playbook_key, runbook_key, by, now()))

    def for_playbook(self, playbook_key: str) -> List[str]:
        return [r["runbook_key"] for r in self.db._exec("SELECT runbook_key FROM act_playbook_links WHERE playbook_key=?", (playbook_key,))]


def _store(body: Dict[str, Any]) -> str:
    """The stored body keeps its JSON types (fractions stay numbers); its hash is over the canonical form (floats as strings)."""
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def _kind(by: str) -> str:
    return "user" if "@" in (by or "") else "system"
