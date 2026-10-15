"""How runbooks are born, part 1 (docs/design/action-agent.md §6.10, §6.11): playbook notes -> drafts, anomaly checklists ->
drafts, and the starter pack (shipped disabled; a team adopts and publishes).

Each `- …` bullet of a playbook entry becomes a step: a bullet matching a capture rule becomes a typed step bound to a catalogue
template (when the catalogue has it), anything else a `manual` step. Every act step gets its own verify step right after it
(a server-side check that the entry's log pattern is gone), so no manual step sits between an act step and its verify."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import privacy
from . import params as P

# capture rules (regex on scrubbed text) -> (template, {param: (group, from-slot, transform)}); the design's table plus a few
# read-only diagnose shapes (df, du, systemctl status, tail)
RULES: List[Tuple[re.Pattern, str, Dict[str, Tuple[int, str]]]] = [
    (re.compile(r"systemctl\s+restart\s+(\S+)"), "linux.service.restart", {"unit": (1, "unit")}),
    (re.compile(r"\bservice\s+(\S+)\s+restart"), "linux.service.restart", {"unit": (1, "unit")}),
    (re.compile(r"Restart-Service\s+(?:-Name\s+)?(\S+)", re.I), "win.service.restart", {"name": (1, "win_service")}),
    (re.compile(r"net\s+stop\s+(\S+).*net\s+start", re.I), "win.service.restart", {"name": (1, "win_service")}),
    (re.compile(r"appcmd(?:\.exe)?\s+recycle\s+apppool\s+/apppool\.name:\"?([^\"\s]+)", re.I), "iis.apppool.recycle", {"pool": (1, "app_pool")}),
    (re.compile(r"Restart-WebAppPool\s+(?:-Name\s+)?\"?([^\"\s]+)", re.I), "iis.apppool.recycle", {"pool": (1, "app_pool")}),
    (re.compile(r"find\s+(\S+).*-mtime\s+\+(\d+).*gzip"), "linux.logs.compress_old", {"path": (1, "log_dir"), "days": (2, "")}),
    (re.compile(r"gzip\b.*-mtime\s+\+(\d+)"), "linux.logs.compress_old", {"days": (1, "")}),
    (re.compile(r"logrotate\s+-f\s+(\S+)"), "linux.logrotate.force", {"conf": (1, "")}),
    (re.compile(r"journalctl\s+--vacuum-size[= ](\S+)"), "linux.journal.vacuum", {"size": (1, "")}),
    (re.compile(r"\bkill\s+(?:-9\s+)?(\d+)"), "linux.proc.kill", {}),
    (re.compile(r"sapcontrol.*-function\s+ABAPGetWPTable", re.I), "sap.wp.table", {}),
    (re.compile(r"sapcontrol.*-function\s+GetProcessList", re.I), "sap.process.list", {}),
    (re.compile(r"\bdf\s+(?:-\w+\s+)*(/\S*)"), "linux.disk.usage", {"path": (1, "")}),
    (re.compile(r"\bdu\s+(?:-\S+\s+)*(/\S*)"), "linux.dir.top", {"path": (1, "")}),
    (re.compile(r"systemctl\s+status\s+(\S+)"), "linux.service.status", {"unit": (1, "unit")}),
    (re.compile(r"\btail\s+(?:-n\s*\d+\s+|-\d+\s+)?(/\S+)"), "linux.log.tail", {"path": (1, "log_dir")}),
]
SAP_MANUAL = re.compile(r"\bSM(50|66)\b")
BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.*\S)\s*$")
CASE_SLOT = {"unit": "unit", "name": "win_service", "pool": "app_pool", "path": "log_dir"}


def bullets(text: str) -> List[str]:
    out = []
    for line in (text or "").splitlines():
        m = BULLET.match(line)
        if m:
            out.append(m.group(1))
    return out


def _norm(name: str, raw: str, spec: Dict[str, Any]) -> Optional[Any]:
    ty = spec.get("type")
    v: Any = raw.strip().strip("'\"")
    if name == "conf":
        v = v.rsplit("/", 1)[-1]
    if ty == "unit" and not v.endswith(".service"):
        v += ".service"
    try:
        return P.validate(spec, int(v) if ty == "int" and str(v).isdigit() else v)
    except P.ParamError:
        return None


def capture(text: str, templates: Dict[str, dict]) -> Optional[Dict[str, Any]]:
    """One bullet / "Ne yaptın?" line -> {"template", "values"} when a capture rule matches a catalogue template."""
    clean = privacy.scrub_strict(text)
    for rx, tid, groups in RULES:
        m = rx.search(clean)
        if not m:
            continue
        t = templates.get(tid)
        if t is None:
            return {"template": tid, "missing": True, "values": {}}
        tparams = (t.get("body") or {}).get("params") or {}
        vals = {}
        for pname, (grp, _slot) in groups.items():
            if pname in tparams:
                vals[pname] = _norm(pname, m.group(grp), tparams[pname])
        return {"template": tid, "values": vals, "missing": False}
    return None


def draft_from_bullets(key: str, title: str, items: List[str], templates: Dict[str, dict], team_id: Optional[int] = None,
                       match_template: str = "", os_name: str = "linux") -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    steps: List[Dict[str, Any]] = []
    n = {"d": 0, "a": 0, "v": 0, "m": 0, "r": 0}

    def sid(prefix):
        n[prefix] += 1
        return f"{prefix}{n[prefix]}"

    verify_check = ({"log_absent": {"query": json.dumps(match_template[:120], ensure_ascii=False) + " host:{host}", "window_s": 300}}
                    if match_template else {"anomaly_cleared": {"within_s": 600}})
    if any((capture(b, templates) or {}).get("template") for b in items):
        params["host"] = {"type": "host", "from": ["case.host"]}
    for b in items:
        text = privacy.scrub_strict(b)[:500]
        cap = capture(b, templates)
        if SAP_MANUAL.search(text) and not cap:
            steps.append({"id": sid("m"), "type": "manual", "title": "SAP SM50 iptal", "text": text})
            continue
        if not cap or cap.get("missing"):
            st = {"id": sid("m"), "type": "manual", "title": text[:80], "text": text}
            if cap and cap.get("missing"):
                st["hint"] = f"normalised: {cap['template']} (not in this installation's catalogue)"
            steps.append(st)
            continue
        t = templates[cap["template"]]
        body = t.get("body") or {}
        tparams = body.get("params") or {}
        args = {}
        for pname, spec in tparams.items():
            if pname in params and params[pname].get("type") != spec.get("type"):
                pname_rb = f"{pname}_{cap['template'].split('.')[-1]}"
            else:
                pname_rb = pname
            val = cap["values"].get(pname)
            if val is None and "default" in spec:
                continue                                    # the template's own default applies
            p = {k: v for k, v in spec.items() if k in ("type", "roots", "min", "max", "values", "values_from", "pattern", "pid_exe")}
            slot = CASE_SLOT.get(pname)
            src = ([f"evidence.{slot}"] if slot else []) + (["default"] if val is not None else ["ask"])
            p["from"] = src
            if val is not None:
                p["default"] = val
            params.setdefault(pname_rb, p)
            args[pname] = "{" + pname_rb + "}"
        if body.get("kind") == "diag":
            steps.append({"id": sid("d"), "type": "diagnose", "title": t.get("title_tr") or text[:80], "template": cap["template"], "args": args})
            continue
        aid = sid("a")
        act = {"id": aid, "type": "act", "title": t.get("title_tr") or text[:80], "template": cap["template"], "args": args,
               "dry_run": "optional" if body.get("dry_run") else "none"}
        vid = sid("v")
        act["verify"] = vid
        steps.append(act)
        if body.get("reversibility") == "reversible" and body.get("rollback") in templates:
            rid = sid("r")
            act["rollback"] = rid
            steps.append({"id": vid, "type": "verify", "title": "Sorun geçti mi?", "checks": [verify_check], "require": "all", "retries": 3,
                          "interval_s": 20, "on_fail": "rollback"})
            steps.append({"id": rid, "type": "rollback", "title": "Geri al", "template": body["rollback"],
                          "args": {"list_id": "{steps." + aid + ".out.list_id}"}, "auto_safe": True})
        else:
            steps.append({"id": vid, "type": "verify", "title": "Sorun geçti mi?", "checks": [verify_check], "require": "all", "retries": 3,
                          "interval_s": 20, "on_fail": "escalate"})
    risk = "R0"
    for s in steps:
        if s["type"] == "act":
            r = templates[s["template"]].get("risk", "R1")
            risk = max(risk, r)
    return {"key": key, "version": 1, "title": title[:120], "team_id": team_id, "risk": risk,
            "summary": "Playbook notlarından içe aktarıldı; yayından önce gözden geçirin.",
            "match": {"sources": ["query", "anomaly", "incident", "threshold"], "templates": [match_template] if match_template else []},
            "applies_to": {"os": [os_name]}, "params": params,
            "preconditions": [{"check": "no_other_run_on_host", "else": "stop"}] if any(s["type"] == "act" for s in steps) else [],
            "watch": {"minutes": 15, "expected_noise_s": 0, "expected_noise": []}, "max_level": 4, "steps": steps}


def slug(text: str, prefix: str = "pb") -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")
    return f"{prefix}-{t}" if t else prefix


def draft_from_playbook(entry: Dict[str, Any], templates: Dict[str, dict], key: Optional[str] = None, team_id: Optional[int] = None) -> Dict[str, Any]:
    items = bullets(entry.get("runbook") or "")
    os_name = "windows" if any((capture(b, templates) or {}).get("template", "").startswith(("win.", "iis.")) for b in items) else "linux"
    return draft_from_bullets(key or slug(entry.get("title") or entry.get("key") or ""), f"Playbook: {entry.get('title') or entry.get('key')}",
                              items, templates, team_id, match_template=str(entry.get("template") or entry.get("key") or ""), os_name=os_name)


# ---------------------------------------------------------------- starter pack
STARTER_DIR = Path(__file__).resolve().parent / "starter"


def starter_bodies(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    d = Path(path) if path else STARTER_DIR
    out = {}
    for f in sorted(d.glob("*.json")):
        body = json.loads(f.read_text(encoding="utf-8"))
        out[body["key"]] = body
    return out


def load_starter(runbooks, by: str = "system") -> int:
    """Starter runbooks as shared (team NULL), disabled drafts: nothing here is proposed or run until a team adopts one
    (copies it into its own runbook) and publishes it with a second person."""
    n = 0
    for key, body in starter_bodies().items():
        if runbooks.db._exec("SELECT 1 AS x FROM act_runbooks WHERE key=?", (key,)):
            continue
        rb = runbooks.create(key, body.get("title", key), None, body, by, origin="starter")
        runbooks.db._exec("UPDATE act_runbooks SET status='starter' WHERE key=?", (key,))      # disabled until a team adopts it
        n += 1 if rb else 0
    return n
