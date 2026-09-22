"""Windows Event Log exported as XML (one <Event> per line, or pretty-printed over several lines)."""

from __future__ import annotations

import re
from typing import Iterator
from xml.etree import ElementTree as ET

from .base import Parser

LEVEL = {"1": "CRITICAL", "2": "ERROR", "3": "WARN", "4": "INFO", "5": "DEBUG", "0": "INFO"}
EVENT_RE = re.compile(r"<Event\b.*?</Event>", re.S)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


class WinEvtParser(Parser):
    name = "winevt"
    kind = "event"
    line_oriented = False

    def records(self, text: str) -> Iterator[tuple[int, dict]]:
        for i, chunk in enumerate(EVENT_RE.findall(text), 1):
            try:
                ev = ET.fromstring(chunk)
            except ET.ParseError:
                continue
            rec: dict = {}
            fields: dict = {}
            for node in ev.iter():
                tag = _local(node.tag)
                txt = (node.text or "").strip()
                if tag == "Provider":
                    rec["provider"] = node.get("Name", "")
                elif tag == "TimeCreated":
                    rec["timestamp"] = node.get("SystemTime", "")
                elif tag in ("EventID", "Level", "Task", "Channel", "Computer", "Keywords", "Opcode") and txt:
                    rec[{"EventID": "event_id", "Level": "level", "Computer": "host"}.get(tag, tag.lower())] = txt
                elif tag == "Data":
                    fields[node.get("Name") or f"data{len(fields) + 1}"] = txt
            rec["severity"] = LEVEL.get(rec.get("level", ""), "INFO")
            rec["service"] = rec.get("provider") or rec.get("channel") or "windows"
            rec["message"] = " ".join(x for x in (f"EventID {rec.get('event_id', '')}", rec.get("provider", ""), *(f"{k}={v}" for k, v in fields.items())) if x)
            rec.update({k: v for k, v in fields.items() if k not in rec})
            yield i, rec
