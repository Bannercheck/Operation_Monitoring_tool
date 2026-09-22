"""W3C extended log format (IIS, Exchange, some proxies): '#Fields:' directive names the space-separated columns."""

from __future__ import annotations

from typing import Iterator

from .access_parser import status_severity
from .base import Parser


class W3cParser(Parser):
    name = "w3c"
    kind = "log"

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        fields: list[str] = []
        software = ""
        for i, ln in enumerate(text.splitlines(), 1):
            if not ln.strip():
                continue
            if ln.startswith("#"):
                if ln.startswith("#Fields:"):
                    fields = ln[8:].split()
                elif ln.startswith("#Software:"):
                    software = ln[10:].strip()
                continue
            if not fields:
                continue
            vals = ln.split(" ")
            rec = {k: v for k, v in zip(fields, vals) if v != "-"}
            if "date" in rec and "time" in rec:
                rec["timestamp"] = f"{rec.pop('date')} {rec.pop('time')}"
            elif "date-time" in rec:
                rec["timestamp"] = rec.pop("date-time")
            st = rec.get("sc-status") or rec.get("status") or ""
            method, uri = rec.get("cs-method", ""), rec.get("cs-uri-stem", rec.get("cs-uri", ""))
            rec["message"] = " ".join(x for x in (method, uri, st) if x) or ln
            if st:
                rec["severity"] = status_severity(st)
            if "s-ip" in rec or "s-computername" in rec:
                rec["host"] = rec.get("s-computername") or rec["s-ip"]
            rec["service"] = rec.get("s-sitename") or ("iis" if "Internet Information Services" in software else "w3c")
            yield i, rec
