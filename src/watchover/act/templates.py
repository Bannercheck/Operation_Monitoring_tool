"""Command templates (docs/design/action-agent.md §5): pack schema, lint, semantic sha256, argv and human rendering, and the
server catalogue (`act_templates`).

Two lists, one truth: what can run on a host is the host's own root-owned allowlist (`/etc/watchover/actions.d`), which the
server never pushes; the catalogue is what runbook authors may bind to. Vendor packs ship in `agent_actions/` (inside the
pinned release archive) and are loaded into the catalogue as approved; packs marked disabled (sap-act) load as proposed."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import warnings
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import params as P
from .conditions import ConditionError, parse as parse_condition
from .types import RISKS, canonical, now, sha256_hex

SEMANTIC_KEYS = ("id", "rev", "os", "kind", "risk", "reversibility", "rollback", "run_as", "argv", "dry_argv", "script", "builtin",
                 "dry_run", "params", "pre", "timeout_s", "max_output_kb", "env", "limits", "output", "writes")
KNOWN_KEYS = set(SEMANTIC_KEYS) | {"cost", "human_cmd", "title_tr", "title_en", "hint_tr", "hint_en", "privacy", "manifest", "executor",
                                   "pid_exe", "justification", "expect_tr", "expect_en"}
OSES = ("linux", "windows")
SHELLS = re.compile(r"^(sh|bash|dash|zsh|ksh|csh|tcsh|fish|ash|cmd|cmd\.exe|env|xargs|python\d*(\.\d+)?|python\d*\.exe|perl|busybox|"
                    r"powershell|powershell\.exe|pwsh|pwsh\.exe|wscript\.exe|cscript\.exe|mshta\.exe|rundll32\.exe|node|ruby|php|lua)$", re.I)
PS_COMMAND = re.compile(r"^-(command|c|encodedcommand|enc|ec|e)$", re.I)
PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")
DERIVED = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)_(lower|owner)$")       # run_as only: {sid_lower}adm, {pid_owner}
PREDICATES = {"path_under": 2, "fs_free_pct_below": 2, "fs_free_pct_above": 2, "proc_comm_is": 2, "proc_user_is": 2, "proc_age_min": 2,
              "unit_exists": 1, "unit_state_is": 2, "win_service_exists": 1, "iis_pool_exists": 1, "file_age_min": 2, "host_is": 1}
# built-in functions inside agent.py (§5.7): kind, dry support, needs a manifest, parameters
BUILTINS = {
    "disk_usage": {"kind": "diag", "dry": False, "params": ("path",)},
    "dir_top": {"kind": "diag", "dry": False, "params": ("path", "depth", "n")},
    "files_largest": {"kind": "diag", "dry": False, "params": ("path", "n")},
    "compress_older_than": {"kind": "act", "dry": True, "manifest": True, "params": ("path", "days")},
    "decompress_list": {"kind": "act", "dry": False, "params": ("list_id",)},
    "log_tail": {"kind": "diag", "dry": False, "raw": True, "params": ("path", "n")},
    "proc_top": {"kind": "diag", "dry": False, "params": ("n",)},
    "proc_find": {"kind": "diag", "dry": False, "params": ("pattern",)},
    "port_check": {"kind": "diag", "dry": False, "params": ("port",)},
    "http_check": {"kind": "diag", "dry": False, "params": ("url", "expect_status")},
    "service_status": {"kind": "diag", "dry": False, "params": ("unit",)},
    "win_service_status": {"kind": "diag", "dry": False, "params": ("name",)},
    "iis_pool_state": {"kind": "diag", "dry": False, "params": ("pool",)},
    "sap_wp_table": {"kind": "diag", "dry": False, "params": ("sid", "nr")},
}
# server-side output parsers and the type of every field they emit (§5.4 privacy: structured fields are scrubbed by type)
PARSERS: Dict[str, Dict[str, str]] = {
    "kv": {"use_pct": "number", "avail_gb": "number", "size_gb": "number", "mount": "path", "fs": "ident"},
    "du_top": {"rows": "rows", "path": "path", "bytes": "number"},
    "files_largest": {"rows": "rows", "path": "path", "bytes": "number", "mtime": "number"},
    "lsof_deleted": {"deleted_open_bytes": "number", "files": "number", "rows": "rows", "pid": "number", "comm": "ident", "path": "path"},
    "systemctl_show": {"ActiveState": "enum", "SubState": "enum", "MainPID": "number", "NRestarts": "number"},
    "appcmd_apppool": {"name": "ident", "state": "enum"},
    "sc_query": {"name": "ident", "state": "enum", "pid": "number"},
    "jcmd_heap": {"used_kb": "number", "capacity_kb": "number", "metaspace_kb": "number"},
    "sap_wp_table": {"rows": "rows", "No": "number", "Typ": "enum", "Pid": "number", "Status": "enum", "Time": "number",
                     "Program": "ident", "User": "ident"},
    "sap_process_list": {"rows": "rows", "name": "ident", "dispstatus": "enum", "pid": "number"},
    "compress_result": {"list_id": "ident", "files": "number", "bytes_before": "number", "bytes_after": "number"},
    "proc_top": {"rows": "rows", "pid": "number", "start": "ident", "comm": "ident", "user": "ident", "cpu": "number", "mem_mb": "number"},
    "proc_find": {"rows": "rows", "pid": "number", "start": "ident", "exe": "path", "unit": "ident", "user": "ident"},
    "journal_vacuum": {"freed_bytes": "number"},
    "logrotate_result": {"rotated": "number", "errors": "number"},
    "http_status": {"status": "number", "ms": "number"},
    "port_state": {"port": "number", "open": "enum"},
    "win_disk": {"use_pct": "number", "avail_gb": "number", "drive": "ident"},
    "recycle_result": {"ok": "enum"},
}
RAW_REQUIRED = re.compile(r"(log_tail|journalctl|wevtutil|Thread\.print)")
ROOT = Path(__file__).resolve().parents[3]


def actions_dir() -> Path:
    return Path(os.environ.get("WATCHOVER_ACTIONS_DIR") or (ROOT / "agent_actions"))


# ---------------------------------------------------------------- semantic sha256
def semantic(t: Dict[str, Any]) -> Dict[str, Any]:
    return {k: t[k] for k in SEMANTIC_KEYS if k in t}


def semantic_sha(t: Dict[str, Any]) -> str:
    """What jobs, cells and the catalogue bind to: titles and hints are excluded, so a translation never resets trust."""
    return sha256_hex(canonical(semantic(t)))


# ---------------------------------------------------------------- lint
def _finding(level, code, template="", detail=""):
    return {"level": level, "code": code, "template": template, "detail": detail}


def privacy_of(t: Dict[str, Any]) -> str:
    if t.get("privacy") in ("raw", "structured"):
        return t["privacy"]
    parser = (t.get("output") or {}).get("parser", "")
    return "structured" if parser in PARSERS else "raw"


def _sre():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            import re._parser as sre_parse  # type: ignore  # Python 3.11+
        except ImportError:  # pragma: no cover - Python 3.9 / 3.10
            import sre_parse  # type: ignore
        try:
            import re._constants as c  # type: ignore
        except ImportError:  # pragma: no cover
            import sre_constants as c  # type: ignore
    return sre_parse, c


def pattern_broad(pat: str) -> str:
    """'' when a unit / win_service / iis_pool pattern is narrow enough (R2): anchored ^…$, every alternative starts with a literal
    of >= 3 characters and the variable tail is <= 40 characters; else the reason."""
    if not pat:
        return "no pattern"
    sre_parse, c = _sre()
    try:
        items = list(sre_parse.parse(pat))
    except Exception:  # noqa: BLE001
        return "invalid regex"
    if len(items) < 2 or items[0] != (c.AT, c.AT_BEGINNING) or items[-1] != (c.AT, c.AT_END):
        return "not anchored ^…$"
    body = items[1:-1]

    def lead_literals(seq) -> int:
        n = 0
        for op, av in seq:
            if op == c.LITERAL:
                n += 1
            else:
                break
        return n

    def var_width(seq) -> int:
        """Max width of a sequence without its leading and trailing literal runs."""
        seq = list(seq)[lead_literals(seq):]
        while seq and seq[-1][0] == c.LITERAL:
            seq.pop()
        if not seq:
            return 0
        state = sre_parse.State() if hasattr(sre_parse, "State") else sre_parse.Pattern()
        return int(sre_parse.SubPattern(state, seq).getwidth()[1])

    try:
        if body and body[0][0] == c.SUBPATTERN and len(list(body[0][1][-1])) == 1 and list(body[0][1][-1])[0][0] == c.BRANCH:
            alts = [list(a) for a in list(body[0][1][-1])[0][1][1]]
            if min(lead_literals(a) for a in alts) < 3:
                return "an alternative starts with fewer than 3 literal characters"
            width = max(var_width(a) for a in alts) + var_width(body[1:]) + (lead_literals(body[1:]) if any(var_width(a) for a in alts) else 0)
        elif body and body[0][0] == c.SUBPATTERN:
            sub = list(body[0][1][-1])
            if lead_literals(sub) < 3:
                return "starts with fewer than 3 literal characters"
            width = var_width(sub) + var_width(body[1:])
        else:
            if lead_literals(body) < 3:
                return "starts with fewer than 3 literal characters"
            width = var_width(body)
    except Exception:  # noqa: BLE001
        return "cannot measure the variable tail"
    return "" if width <= 40 else f"variable tail up to {width} characters (max 40)"


def _placeholders(s: str) -> List[str]:
    return PLACEHOLDER.findall(s) if isinstance(s, str) else []


def lint_template(t: Dict[str, Any], pack: Optional[Dict[str, Any]] = None, scripts_dir: Optional[Path] = None) -> List[dict]:
    tid = str(t.get("id", ""))
    out: List[dict] = []

    def err(code, detail=""):
        out.append(_finding("error", code, tid, detail))

    def warn(code, detail=""):
        out.append(_finding("warning", code, tid, detail))

    if not re.fullmatch(r"[a-z0-9_]+(\.[a-z0-9_]+)+", tid):
        err("bad_id", "lowercase dotted id, e.g. linux.disk.usage")
    for k in t:
        if k not in KNOWN_KEYS:
            err("unknown_key", k)
    if not isinstance(t.get("rev"), int) or t.get("rev", 0) < 1:
        err("bad_rev")
    osn = t.get("os")
    if osn not in OSES:
        err("bad_os", str(osn))
    kind = t.get("kind")
    if kind not in ("diag", "act"):
        err("bad_kind", str(kind))
    risk = t.get("risk")
    if risk not in RISKS:
        err("bad_risk", str(risk))
    if t.get("cost", "light") not in ("light", "heavy"):
        err("bad_cost")
    if t.get("executor", "agent") not in ("agent", "manual"):
        err("bad_executor", "templates run on the host actuator (agent) or by hand; HTTP executors are step bindings")
    bodies = [b for b in ("argv", "script", "builtin") if b in t]
    if len(bodies) != 1:
        err("one_body", "exactly one of argv / script / builtin")
    prm = t.get("params") or {}
    if not isinstance(prm, dict):
        err("bad_params"); prm = {}
    for name, spec in prm.items():
        ty = (spec or {}).get("type")
        if ty in P.FREE_STRING_NAMES or ty not in P.TYPES:
            err("free_string" if ty in P.FREE_STRING_NAMES else "unknown_type", f"{name}: {ty} (no free-string type, I1)")
            continue
        if ty == "path" and not spec.get("roots"):
            err("missing_roots", name)
        if ty == "enum" and not spec.get("values") and not spec.get("values_from"):
            err("enum_values", name)
        if ty == "int" and (spec.get("min") is None or spec.get("max") is None):
            err("int_bounds", name)
        if ty == "pid" and not (spec.get("pid_exe") or t.get("pid_exe")):
            err("pid_exe", f"{name}: a pid parameter names the executable it must belong to")
        if ty in ("unit", "win_service", "iis_pool"):
            why = pattern_broad(spec.get("pattern", "")) if spec.get("pattern") else ""
            if spec.get("pattern") and why and not (t.get("justification") or spec.get("justification")):
                err("pattern_broad", f"{name}: {why}")
        if ty == "url" and t.get("builtin") != "http_check":
            err("url_param", f"{name}: url parameters only for http_check / HTTP executors")
        if ty == "host":
            err("host_param", f"{name}: the host is the actuator itself; never a template parameter")
    used: List[str] = []

    def elem_check(el, where, first=False):
        if not isinstance(el, str):
            err("bad_element", f"{where}: {el!r}")
            return
        phs = _placeholders(el)
        used.extend(phs)
        if first and phs:
            err("placeholder_argv0", where)
        if not phs:
            return
        if len(phs) > 1:
            err("placeholder_count", f"{where}: one placeholder per element")
        name = phs[0]
        literal = el.replace("{" + name + "}", "")
        if literal:
            ty = (prm.get(name) or {}).get("type")
            if not el.endswith("{" + name + "}") or ty not in ("int", "enum") + P.IDENT_FAMILY:
                err("placeholder_prefix", f"{where}: a literal prefix plus one placeholder only for int, enum and ident-family types")

    if "argv" in t:
        argv = t["argv"]
        if not isinstance(argv, list) or not argv:
            err("bad_argv")
        else:
            exe = argv[0]
            if not isinstance(exe, str) or not (exe.startswith("/") or re.match(r"^[A-Za-z]:\\", exe)):
                err("argv0_absolute", str(exe))
            elif SHELLS.match(re.split(r"[\\/]", exe)[-1]):
                err("shell", exe)
            for i, el in enumerate(argv):
                elem_check(el, f"argv[{i}]", first=(i == 0))
                if isinstance(el, str) and PS_COMMAND.match(el):
                    err("ps_command", el)
    if "dry_argv" in t:
        for i, el in enumerate(t["dry_argv"] or []):
            elem_check(el, f"dry_argv[{i}]", first=(i == 0))
        if t["dry_argv"] and isinstance(t["dry_argv"][0], str) and SHELLS.match(re.split(r"[\\/]", t["dry_argv"][0])[-1]):
            err("shell", t["dry_argv"][0])
    if "script" in t:
        sc = t["script"] or {}
        path = sc.get("path", "")
        if not (path.startswith("/") or re.match(r"^[A-Za-z]:\\", path)):
            err("script_absolute", path)
        if not re.fullmatch(r"[0-9a-f]{64}", str(sc.get("sha256", ""))):
            err("script_sha256", "64 hex characters")
        if path.lower().endswith(".ps1") and osn != "windows":
            err("script_os", ".ps1 only on Windows")
        if SHELLS.match(re.split(r"[\\/]", path)[-1]):
            err("shell", path)
        for i, el in enumerate(sc.get("args") or []):
            elem_check(el, f"script.args[{i}]")
            if isinstance(el, str) and PS_COMMAND.match(el):
                err("ps_command", el)
        if scripts_dir is not None and re.fullmatch(r"[0-9a-f]{64}", str(sc.get("sha256", ""))):
            local = Path(scripts_dir) / re.split(r"[\\/]", path)[-1]
            if local.exists() and hashlib.sha256(local.read_bytes()).hexdigest() != sc["sha256"]:
                err("script_sha_mismatch", local.name)
    if "builtin" in t:
        b = BUILTINS.get(t["builtin"])
        if not b:
            err("unknown_builtin", str(t["builtin"]))
        else:
            if b["kind"] != kind and not (kind == "act" and b["kind"] == "diag"):
                err("builtin_kind", f"{t['builtin']} is a {b['kind']} built-in")
            for name in prm:
                if name not in b["params"]:
                    err("builtin_param", f"{t['builtin']} has no parameter {name}")
            if b.get("manifest") and t.get("manifest") != "required":
                err("manifest_required", "rollback-capable built-ins write their manifest first (manifest: required)")
            if b.get("raw") and privacy_of(t) != "raw":
                err("privacy_raw", "free-form output is display-only")
        if t.get("human_cmd"):
            used.extend(_placeholders(t["human_cmd"]))
        else:
            err("human_cmd", "builtin templates show a human_cmd in guided mode")
    elif "human_cmd" in t:
        err("human_cmd_body", "argv / script templates render the guided command from the body")
    if "run_as" in t:
        if osn == "windows":
            err("run_as_windows", "Windows templates run as the service account")
        for ph in _placeholders(t["run_as"]):
            m = DERIVED.match(ph)
            if not m or m.group(1) not in prm:
                err("run_as_placeholder", ph)
            else:
                used.append(m.group(1))
    for name in set(used):
        if name not in prm:
            err("undeclared", name)
    for name in prm:
        if name not in used and "builtin" not in t:
            warn("unused_param", name)
    if kind == "diag":
        if risk != "R0":
            err("diag_risk", "diagnose templates are R0")
        if t.get("writes"):
            err("diag_writes", "R0 / diagnose templates write nothing")
    if risk == "R0" and t.get("writes"):
        err("r0_writes")
    if kind == "act":
        rev = t.get("reversibility")
        if rev not in ("transient", "data_preserving", "reversible", "irreversible"):
            err("reversibility")
        if rev == "reversible":
            rb = t.get("rollback")
            names = {x.get("id"): x for x in (pack or {}).get("templates", [])} if pack else {}
            if not rb:
                err("rollback_missing")
            elif pack is not None and (rb not in names or names[rb].get("os") != osn):
                err("rollback_pack", f"{rb} must be a template of the same pack and OS")
    if t.get("dry_run"):
        b = BUILTINS.get(t.get("builtin", ""), {})
        if not t.get("dry_argv") and not b.get("dry"):
            err("dry_run_body", "dry_run needs dry_argv or a built-in that supports dry")
    if int(t.get("timeout_s", 0) or 0) <= 0 or int(t.get("timeout_s", 0) or 0) > 1800:
        err("timeout", "1..1800 s")
    if int(t.get("max_output_kb", 16) or 0) > 256:
        err("max_output", "<= 256 kB")
    for pr in t.get("pre") or []:
        name = pr.get("pred")
        if name not in PREDICATES:
            err("unknown_predicate", str(name))
            continue
        args = pr.get("args") or []
        if len(args) != PREDICATES[name]:
            err("predicate_args", name)
        for a in args:
            for ph in _placeholders(str(a)):
                if ph not in prm:
                    err("undeclared", f"{name}: {ph}")
    parser = (t.get("output") or {}).get("parser")
    if parser and parser not in PARSERS:
        err("unknown_parser", parser)
    if t.get("privacy") not in (None, "raw", "structured"):
        err("privacy", str(t.get("privacy")))
    if t.get("privacy") == "structured" and parser not in PARSERS:
        err("privacy_structured", "structured output needs a parser with a field schema")
    blob = json.dumps(t.get("argv") or []) + str(t.get("builtin", "")) + json.dumps(t.get("script") or {})
    if kind == "diag" and RAW_REQUIRED.search(blob) and privacy_of(t) != "raw":
        err("privacy_raw", "log tails, journalctl, wevtutil and thread dumps are display-only (privacy: raw)")
    for k in ("when", "expect"):
        if k in t:
            try:
                parse_condition(t[k])
            except ConditionError as e:
                err("condition", f"{k}: {e}")
    return out


def lint_pack(pack: Dict[str, Any], scripts_dir: Optional[Path] = None) -> List[dict]:
    out = []
    if pack.get("format") != 1:
        out.append(_finding("error", "format", "", "format must be 1"))
    if not re.fullmatch(r"[a-z0-9-]+", str(pack.get("pack", ""))):
        out.append(_finding("error", "pack_name"))
    if not isinstance(pack.get("pack_version"), int):
        out.append(_finding("error", "pack_version"))
    ids = [t.get("id") for t in pack.get("templates", [])]
    for d in {i for i in ids if ids.count(i) > 1}:
        out.append(_finding("error", "duplicate_id", d))
    for t in pack.get("templates", []):
        out += lint_template(t, pack, scripts_dir)
    return out


def load_packs(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    d = Path(path) if path else actions_dir()
    out = {}
    for f in sorted(d.glob("*.tmpl.json")) if d.is_dir() else ([d] if d.exists() else []):
        out[f.name] = json.loads(f.read_text(encoding="utf-8"))
    return out


def lint_dir(path: str) -> Dict[str, List[dict]]:
    d = Path(path)
    scripts = (d / "scripts") if d.is_dir() else None
    res = {}
    for name, pack in load_packs(path).items():
        res[name] = lint_pack(pack, scripts)
    return res


def errors(findings: Iterable[dict]) -> List[dict]:
    return [f for f in findings if f["level"] == "error"]


# ---------------------------------------------------------------- rendering (§5.8: what a person copies is what would run)
def _fill(el: str, values: Dict[str, Any]) -> str:
    def rep(m):
        name = m.group(1)
        if name not in values:
            raise P.ParamError("missing_param", name)
        v = values[name]
        return str(v["pid"]) if isinstance(v, dict) and "pid" in v else str(v)
    s = PLACEHOLDER.sub(rep, el).replace("{{", "{").replace("}}", "}")
    return P.check_rendered(s)


def validate_params(t: Dict[str, Any], values: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None, local: bool = False) -> Dict[str, Any]:
    prm = t.get("params") or {}
    extra = set(values or {}) - set(prm)
    if extra:
        raise P.ParamError("extra_param", ", ".join(sorted(extra)))
    out = {}
    for name, spec in prm.items():
        if name not in (values or {}):
            if "default" in spec:
                out[name] = P.validate(spec, spec["default"], ctx, local)
                continue
            raise P.ParamError("missing_param", name)
        out[name] = P.validate(spec, values[name], ctx, local)
    return out


def render_argv(t: Dict[str, Any], values: Dict[str, Any], mode: str = "execute", ctx=None, validated: bool = False) -> Optional[List[str]]:
    """The argv the actuator would build (None for built-ins). Parameters are validated first."""
    vals = values if validated else validate_params(t, values, ctx)
    if "argv" in t:
        src = t["dry_argv"] if mode == "dry_run" and t.get("dry_argv") else t["argv"]
        return [_fill(el, vals) for el in src]
    if "script" in t:
        sc = t["script"]
        args = [_fill(el, vals) for el in sc.get("args") or []]
        if sc["path"].lower().endswith(".ps1"):
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", sc["path"]] + args
        return [sc["path"]] + args
    return None


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def render_for_human(t: Dict[str, Any], values: Dict[str, Any], ctx=None) -> Dict[str, Any]:
    """{"cmd", "shell", "argv"}: Linux shlex.join (with sudo -n when run_as is set), Windows PowerShell quoting; built-ins
    show their human_cmd with the validated values (shell-quoted)."""
    vals = validate_params(t, values, ctx)
    argv = render_argv(t, vals, validated=True)
    win = t.get("os") == "windows"
    if argv is None:
        def rep(m):
            v = vals.get(m.group(1), "")
            v = str(v["pid"]) if isinstance(v, dict) and "pid" in v else str(v)
            return ps_quote(v) if win else shlex.quote(v)
        cmd = PLACEHOLDER.sub(rep, t.get("human_cmd", "")).replace("{{", "{").replace("}}", "}")
        return {"cmd": cmd, "shell": "powershell" if win else "sh", "argv": None}
    if win:
        return {"cmd": "& " + " ".join(ps_quote(a) for a in argv), "shell": "powershell", "argv": argv,
                "cmdline": subprocess.list2cmdline(argv)}
    prefix = ""
    run_as = t.get("run_as")
    if run_as:
        user = PLACEHOLDER.sub(lambda m: _derived(m.group(1), vals), run_as)
        prefix = "sudo -n " if user == "root" else f"sudo -n -u {shlex.quote(user)} "
    return {"cmd": prefix + shlex.join(argv), "shell": "sh", "argv": argv}


def _derived(name: str, vals: Dict[str, Any]) -> str:
    m = DERIVED.match(name)
    if not m:
        return str(vals.get(name, ""))
    base, how = m.group(1), m.group(2)
    if how == "lower":
        return str(vals.get(base, "")).lower()
    return "<" + base + " sahibi>"


# ---------------------------------------------------------------- catalogue (act_templates)
class Catalogue:
    def __init__(self, db, audit=None):
        self.db, self.audit = db, audit

    def sync_vendor(self, path: Optional[str] = None, force: bool = False) -> Dict[str, int]:
        """Load the vendor packs (idempotent; skipped while the pack set's hash is unchanged)."""
        packs = load_packs(path)
        if not packs:
            return {"packs": 0, "templates": 0}
        digest = sha256_hex(canonical({k: v for k, v in packs.items()}))
        try:
            mark = self.db._exec("SELECT value FROM rbac_meta WHERE key='act_catalogue_sha'")
        except Exception:  # noqa: BLE001
            self.db._exec("CREATE TABLE IF NOT EXISTS rbac_meta (key TEXT PRIMARY KEY, value TEXT)")
            mark = []
        if mark and mark[0]["value"] == digest and not force:
            return {"packs": len(packs), "templates": 0}
        n = 0
        ts = now()
        with self.db.tx():
            for fname, pack in packs.items():
                status = "proposed" if pack.get("disabled") else "approved"
                for t in pack.get("templates", []):
                    if errors(lint_template(t, pack)):
                        continue
                    sha = semantic_sha(t)
                    if self.db._exec("SELECT 1 AS x FROM act_templates WHERE id=? AND sha256=?", (t["id"], sha)):
                        continue
                    self.db._exec(
                        "INSERT INTO act_templates (id, sha256, rev, os, kind, risk, reversibility, cost, body, title_tr, title_en, human_hint, "
                        "parser, source, status, approved_by, approved_at, pack, executor, lint) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (t["id"], sha, t["rev"], t["os"], t["kind"], t["risk"], t.get("reversibility", ""), t.get("cost", "light"),
                         canonical(semantic(t)), t.get("title_tr", ""), t.get("title_en", ""), t.get("human_cmd", ""),
                         (t.get("output") or {}).get("parser", ""), "vendor", status, "vendor" if status == "approved" else "",
                         ts if status == "approved" else "", pack.get("pack", ""), t.get("executor", "agent"),
                         json.dumps(lint_template(t, pack), ensure_ascii=False)))
                    n += 1
                    # an older vendor revision of the same id is retired (cells bind to (id, sha), history stays)
                    self.db._exec("UPDATE act_templates SET status='retired' WHERE id=? AND sha256<>? AND source='vendor' AND status='approved'",
                                  (t["id"], sha))
            self.db._exec("DELETE FROM rbac_meta WHERE key='act_catalogue_sha'")
            self.db._exec("INSERT INTO rbac_meta (key, value) VALUES ('act_catalogue_sha', ?)", (digest,))
            if self.audit is not None and n:
                self.audit.write("template.approved", object_kind="catalogue", object_id="vendor", payload={"templates": n, "packs": sorted(packs)})
        return {"packs": len(packs), "templates": n}

    def list(self, status: Optional[str] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_templates" + (" WHERE status=?" if status else "") + " ORDER BY id, rev",
                             (status,) if status else ())
        for r in rows:
            r["body"] = json.loads(r["body"] or "{}")
            r["lint"] = json.loads(r.get("lint") or "[]")
        return rows

    def get(self, tid: str, sha: Optional[str] = None) -> Optional[dict]:
        if sha:
            rows = self.db._exec("SELECT * FROM act_templates WHERE id=? AND sha256=?", (tid, sha))
        else:
            rows = self.db._exec("SELECT * FROM act_templates WHERE id=? AND status='approved' ORDER BY rev DESC", (tid,))
        if not rows:
            return None
        r = rows[0]
        r["body"] = json.loads(r["body"] or "{}")
        r["body"].setdefault("human_cmd", r.get("human_hint") or "")
        if not r["body"]["human_cmd"]:
            r["body"].pop("human_cmd")
        r["body"]["title_tr"], r["body"]["title_en"] = r.get("title_tr", ""), r.get("title_en", "")
        return r

    def approved(self) -> Dict[str, dict]:
        """id -> the approved entry (newest rev), for runbook lint."""
        out = {}
        for r in self.db._exec("SELECT id, sha256, rev, os, kind, risk, reversibility, cost, body FROM act_templates WHERE status='approved' ORDER BY rev"):
            r["body"] = json.loads(r["body"] or "{}")
            out[r["id"]] = r
        return out
