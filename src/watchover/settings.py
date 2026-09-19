"""Persistent product settings (survive restarts): one JSON file under WATCHOVER_HOME (default ./data).

Secrets (API keys, the legacy receiver key) live here too, so the file is written 0600. The first-run wizard writes
`setup_done`; until then the app shows the wizard instead of the pages.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULTS = {"setup_done": False, "lang": "tr", "workspace": "", "llm_provider": "auto", "llm_base": "", "llm_model": "", "llm_key": "", "llm_embed": "",
            "live_port": int(os.environ.get("LIVE_PORT", "8600") or 8600),      # the env var is what the receiver actually binds
            "live_key": "", "public_host": "", "sim_on": False, "learn_min": 15, "auth_mode": "off", "auth_self_register": True, "auth_domains": "", "oidc_issuer": "", "oidc_client_id": "", "oidc_client_secret": "", "oidc_redirect": "http://localhost:8501/oauth2callback", "demo_on_start": False, "version": 1}
SESSION_KEYS = ("lang", "llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed", "live_port", "live_key", "public_host", "sim_on", "learn_min", "auth_mode", "auth_self_register", "auth_domains", "oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_redirect")


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


def local_addresses() -> list[str]:
    """Addresses other machines may use to reach this one: primary route IP, hostname, then anything else bound."""
    import socket
    out: list[str] = []
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sk.connect(("10.255.255.255", 1)); out.append(sk.getsockname()[0]); sk.close()
    except OSError:
        pass
    try:
        hn = socket.gethostname()
        for cand in (hn, socket.getfqdn()):
            if cand and cand not in out and cand != "localhost":
                out.append(cand)
        for info in socket.getaddrinfo(hn, None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out or ["localhost"]
