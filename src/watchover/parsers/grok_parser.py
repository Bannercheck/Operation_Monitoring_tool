"""Grok line packs (built-in and user-defined): the pattern that matches most of the file is applied to every line;
its role map says which capture is the timestamp / level / host / service / message, the rest become attributes."""

from __future__ import annotations

from typing import Iterator

from .. import grok
from .base import Parser
from .text_parser import CONT_RE

STATUS_SEV = lambda st: "ERROR" if st.startswith("5") else "WARN" if st.startswith("4") else "INFO"  # noqa: E731


class GrokParser(Parser):
    name = "grok"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        lines = text.splitlines()
        lp, _share = grok.detect(lines)
        if lp is None:
            return
        rx = grok.compile_pattern(lp.pattern, trusted=True)
        roles = lp.roles or {}
        prev: tuple[int, dict] | None = None
        for i, ln in enumerate(lines, 1):
            if not ln.strip():
                continue
            m = rx.search(ln)
            if not m:
                if prev is not None and (CONT_RE.match(ln) or ln[:1].isspace()):        # continuation of the event above
                    prev[1]["message"] = (prev[1].get("message", "") + "\n" + ln.strip()).strip()
                    prev[1]["_lines"] = prev[1].get("_lines", 1) + 1
                    continue
                rec = {"message": ln.strip(), "_unmatched": True}
            else:
                caught = {k: v for k, v in m.groupdict().items() if v is not None}
                rec = {}
                for role, fld in roles.items():
                    if fld in caught:
                        rec[role] = caught[fld]
                if "status" in rec and "severity" not in rec:
                    rec["severity"] = STATUS_SEV(str(rec.pop("status")))
                elif "status" in rec:
                    rec.pop("status")
                rec.setdefault("message", caught.get("message") or ln.strip())
                for k, v in caught.items():
                    if k not in rec and k not in roles.values():
                        rec[k] = v
                if "pid" in caught and "pid" not in rec:
                    rec["pid"] = caught["pid"]
            if lp.family and not rec.get("service"):
                rec["service"] = lp.family
            rec["_grok"] = lp.name
            if prev is not None:
                yield prev
            prev = (i, rec)
        if prev is not None:
            yield prev
