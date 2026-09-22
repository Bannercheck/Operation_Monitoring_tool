"""Learned column mappings ("format profiles"): a file is recognised by the shape of its records (detected format + its keys);
the roles decided for it (by the user's mapping or the auto-mapper) are remembered in <home>/profiles.json and applied the
next time a file of the same shape arrives, so a correction made once holds for every later upload from that source."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
ROLES = ("timestamp", "severity", "service", "host", "message", "environment", "origin")


def _path() -> Path:
    from . import settings
    return settings.home() / "profiles.json"


def signature(fmt: str, keys: list[str]) -> str:
    """Stable id of a record shape: format + sorted key names (text formats have no keys, so they share one signature per format)."""
    return hashlib.sha1((fmt + "|" + ",".join(sorted(str(k) for k in keys))).encode("utf-8")).hexdigest()[:16]


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(d: dict) -> None:
    _path().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def lookup(fmt: str, keys: list[str]) -> dict | None:
    """The remembered roles for this shape (user-made ones only are authoritative; auto ones are hints the mapper already agrees with)."""
    p = load().get(signature(fmt, keys))
    return p if p and p.get("mapping") else None


def remember(fmt: str, keys: list[str], mapping: dict, name: str = "", source: str = "auto") -> dict:
    """Store roles for a shape. A user mapping always wins over an auto one; an auto one never overwrites a user one."""
    if not keys:
        return {}
    sig = signature(fmt, keys)
    clean = {k: v for k, v in (mapping or {}).items() if k in ROLES and v}
    if not clean:
        return {}
    with _lock:
        d = load()
        cur = d.get(sig)
        if cur and cur.get("source") == "user" and source != "user":
            cur["hits"] = int(cur.get("hits", 0)) + 1
            cur["last"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            d[sig] = cur; save(d)
            return cur
        rec = {"id": sig, "format": fmt, "keys": sorted(str(k) for k in keys)[:60], "mapping": clean, "name": (name or (cur or {}).get("name", ""))[:120],
               "source": source, "hits": int((cur or {}).get("hits", 0)) + 1, "first": (cur or {}).get("first") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "last": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        d[sig] = rec; save(d)
        return rec


def update(sig: str, mapping: dict | None = None, name: str | None = None) -> dict | None:
    with _lock:
        d = load()
        rec = d.get(sig)
        if not rec:
            return None
        if mapping is not None:
            rec["mapping"] = {k: v for k, v in mapping.items() if k in ROLES and v}
            rec["source"] = "user"
        if name is not None:
            rec["name"] = name[:120]
        d[sig] = rec; save(d)
        return rec


def delete(sig: str) -> bool:
    with _lock:
        d = load()
        if sig not in d:
            return False
        del d[sig]; save(d)
        return True


def all_profiles() -> list[dict]:
    return sorted(load().values(), key=lambda r: (-int(r.get("hits", 0)), r.get("name", "")))
