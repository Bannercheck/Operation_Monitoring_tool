"""Persistent product settings (survive restarts): one JSON file under WATCHOVER_HOME (default ./data).

Secrets (API keys, the legacy receiver key) live here too, so the file is written 0600. The first-run wizard writes
`setup_done`; until then the app shows the wizard instead of the pages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {"setup_done": False, "lang": "tr", "workspace": "", "llm_provider": "auto", "llm_base": "", "llm_model": "", "llm_key": "", "llm_embed": "",
            "live_port": 8600, "live_key": "", "sim_on": True, "demo_on_start": False, "version": 1}
SESSION_KEYS = ("lang", "llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed", "live_port", "live_key", "sim_on")


def home() -> Path:
    p = Path(os.environ.get("WATCHOVER_HOME") or "data")
    p.mkdir(parents=True, exist_ok=True)
    return p


def path() -> Path:
    return home() / "config.json"


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save(values: dict) -> dict:
    cfg = load()
    cfg.update({k: v for k, v in values.items() if k in DEFAULTS or k.startswith("x_")})
    p = path()
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return cfg


def setup_done() -> bool:
    return bool(os.environ.get("WATCHOVER_SKIP_SETUP")) or bool(load().get("setup_done"))
