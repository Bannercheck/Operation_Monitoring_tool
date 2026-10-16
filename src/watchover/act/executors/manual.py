"""The manual executor: guided steps (docs/design/action-agent.md §9.2, §5.8).

The step card shows the command rendered from the same template and validated parameters the actuator would use, with the
expected result in words. "Yaptım" records the person's claim — Başarılı / Başarısız / Kısmen, the optional pasted output
(scrubbed server-side, ≤ 64 kB, parsed with the template's parser) and a note; the claim is stored next to the verified result
and never replaces it. "Farklı yaptım" records a deviation, "Atla" needs a reason. Manual is always available at L ≤ 1."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from ... import privacy
from ..types import Plan, job_hash

OUTPUT_MAX = 64 * 1024
CLAIMS = ("ok", "failed", "partial")


def _num(s: str):
    s = s.strip().rstrip("%").replace(",", ".")
    try:
        return int(s) if re.fullmatch(r"-?\d+", s) else round(float(s), 1)
    except ValueError:
        return None


def parse_text(parser: str, text: str) -> Dict[str, Any]:
    """Pasted output → fields with the template's parser (the actuator's built-ins return JSON; people paste what the
    human_cmd printed). Unknown shapes give {} (the check then has no data)."""
    t = (text or "").strip()
    if not t:
        return {}
    if t.startswith("{"):
        try:
            v = json.loads(t)
            return v if isinstance(v, dict) else {}
        except ValueError:
            pass
    if parser == "kv":
        lines = [ln for ln in t.splitlines() if ln.strip()]
        if lines and lines[0].lower().startswith("filesystem"):                  # df -P
            for ln in lines[1:]:
                parts = ln.split()
                if len(parts) >= 6 and parts[4].endswith("%"):
                    blocks, avail = _num(parts[1]), _num(parts[3])
                    out = {"use_pct": _num(parts[4]), "mount": parts[5]}
                    if isinstance(avail, (int, float)):
                        out["avail_gb"] = round(avail / 1024 / 1024, 1)
                    if isinstance(blocks, (int, float)):
                        out["size_gb"] = round(blocks / 1024 / 1024, 1)
                    return out
    if parser in ("du_top", "files_largest"):
        rows = []
        for ln in t.splitlines()[:50]:
            m = re.match(r"^\s*(\d+)\s+(\S.*)$", ln)
            if m:
                rows.append({"bytes": int(m.group(1)), "path": m.group(2).strip()})
        return {"rows": rows}
    out: Dict[str, Any] = {}
    for ln in t.splitlines():
        for part in ln.split():
            if "=" in part:
                k, v = part.split("=", 1)
                if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,40}", k):
                    n = _num(v)
                    out[k] = n if n is not None else v
    return out


class ManualExecutor:
    kind = "manual"

    def __init__(self, act):
        self.act = act

    def can_run(self, step: Dict[str, Any], host: str, mode: str = "") -> Tuple[bool, str]:
        return True, ""

    def plan(self, step: Dict[str, Any], params: Dict[str, Any], host: str, mode: str, version_sha: str = "") -> Plan:
        tid = step.get("template_id") or step.get("template") or ""
        t = self.act.catalogue.get(tid) if tid else None
        human, preview = "", ()
        if t:
            from ..templates import render_for_human
            try:
                r = render_for_human(t["body"], params)
                human, preview = r.get("cmd") or "", tuple(r.get("argv") or ())
            except Exception as e:  # noqa: BLE001 - shown as the reason; the person types nothing the validator refused
                human = f"<{getattr(e, 'code', 'param_invalid')}>"
        sha = step.get("template_sha256") or (t or {}).get("sha256", "")
        return Plan(executor="manual", host=host, template_id=tid, template_sha256=sha, params=dict(params), mode=mode, preview=preview,
                    human_cmd=human, job_hash=job_hash(host, tid, sha, params, mode, version_sha, step.get("key", "")))

    def dispatch(self, plan: Plan, **ctx) -> str:
        return ""                                     # a person does it; the step waits for "Yaptım"

    def poll(self, handle: str):
        return None

    def cancel(self, handle: str) -> bool:
        return True

    @staticmethod
    def claim(template: Optional[dict], result: str, output: str = "", note: str = "") -> Dict[str, Any]:
        """A person's claim, scrubbed and parsed; the fields go through the same field-aware scrub as actuator output."""
        if result not in CLAIMS:
            raise ValueError("result must be ok, failed or partial")
        text = privacy.scrub_strict(output or "")[:OUTPUT_MAX]
        parser = ((template or {}).get("body") or {}).get("output", {}).get("parser", "") if template else ""
        fields = privacy.scrub_fields(parse_text(parser, text)) if text else {}
        return {"result": result, "fields": fields, "text": text, "note": privacy.scrub_strict(note or "")[:1000]}
