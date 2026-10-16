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
            "live_key": "", "public_host": "", "sim_on": False, "learn_min": 15, "auth_local": False, "auth_google": False, "auth_oidc": False, "google_client_id": "", "google_client_secret": "", "auth_self_register": True, "auth_domains": "", "oidc_issuer": "", "oidc_client_id": "", "oidc_client_secret": "", "oidc_redirect": "http://localhost:8501/oauth2callback",
            "mfa_email": True, "mfa_admins": False, "auth_apple": False, "apple_client_id": "", "apple_team_id": "", "apple_key_id": "", "apple_private_key": "", "auth_microsoft": False, "ms_tenant": "common", "ms_client_id": "", "ms_client_secret": "",
            "self_monitor": True, "self_agent_token": "", "thr_cpu": 85, "thr_memory": 90, "thr_disk": 90, "thr_gpu": 95, "alerts_on": True, "smtp_host": "", "smtp_port": 587, "smtp_security": "starttls", "smtp_user": "", "smtp_password": "", "smtp_from": "", "smtp_from_name": "Watchover",
            "sms_preset": "http", "sms_url": "", "sms_method": "POST", "sms_auth": "bearer", "sms_user": "", "sms_password": "", "sms_token": "", "sms_from": "", "sms_account": "", "sms_body": "", "sms_content_type": "application/json", "demo_on_start": False,
            "live_retention_h": 48, "live_window_max_events": 200000, "live_ring": 50000, "live_spool_mb": 64, "history_retention_days": 400,
            "cold_retention_days": 365, "cold_max_gb": 0, "hot_max_gb": 0,    # log tiers (logtiers.py): archive days (a year, audits), cold_max_gb = an alert budget, hot cap (0 = none)
            "logs_raw": True, "logs_attr_max_bytes": 2048, "logs_msg_max_bytes": 16384,      # stored log lines v2 (logstore.py)
            "trace_sample_pct": 10, "trace_slow_ms": 1000, "trace_apdex_ms": 500, "trace_retention_h": 72, "trace_rollup_days": 30, "rum_retention_days": 30, "anomaly_learning_days": 7, "anomaly_learning_mode": "dinamik", "anomaly_learning_min_days": 1, "teams_strict": False, "teams_self_claim": False,
            "train_auto": True, "train_every_h": 24, "train_schedule_on": False, "train_days": "1,2,3,4,5,6,7", "train_start": "01:00", "train_end": "06:00", "train_tz": "Europe/Istanbul", "train_min_examples": 200, "train_min_new": 50, "train_base": "qwen2.5:1.5b-instruct", "train_gate_min": 0.6, "train_use_ops": True, "version": 2}
SESSION_KEYS = ("lang", "llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed", "live_port", "live_key", "public_host", "sim_on", "learn_min", "auth_local", "auth_google", "auth_oidc", "google_client_id", "google_client_secret", "auth_self_register", "auth_domains", "oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_redirect", "mfa_email", "mfa_admins", "auth_apple", "apple_client_id", "apple_team_id", "apple_key_id", "apple_private_key", "auth_microsoft", "ms_tenant", "ms_client_id", "ms_client_secret",
                "self_monitor", "self_agent_token", "alerts_on", "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_password", "smtp_from", "smtp_from_name",
                "sms_preset", "sms_url", "sms_method", "sms_auth", "sms_user", "sms_password", "sms_token", "sms_from", "sms_account", "sms_body", "sms_content_type")


def home() -> Path:
    p = Path(os.environ.get("WATCHOVER_HOME") or "data")
    p.mkdir(parents=True, exist_ok=True)
    return p


def path() -> Path:
    return home() / "config.json"


SECRET_KEYS = ("llm_key", "live_key", "google_client_secret", "oidc_client_secret", "smtp_password", "sms_password", "sms_token", "apple_private_key", "ms_client_secret", "self_agent_token", "x_itsm_pass", "x_itsm_token")   # encrypted at rest (vault.py)


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    if int(cfg.get("version") or 1) < 2:                    # v2 (Watchover 1.15): self-registration is on by default, as the sign-in page promises
        cfg["auth_self_register"] = True
        cfg["version"] = 2
    if int(cfg.get("version") or 1) < 3:                    # v3 (1.57): the raw-log archive is kept a year (audits); the old default 7 follows
        if cfg.get("cold_retention_days") in (7, "7"):
            cfg["cold_retention_days"] = 365
        cfg["version"] = 3
    legacy = cfg.pop("auth_mode", None)                     # earlier versions had one mode instead of per-provider flags
    if legacy == "local":
        cfg["auth_local"] = True
    elif legacy == "oidc":
        cfg["auth_oidc"] = True
    from . import vault
    for k in SECRET_KEYS:
        if isinstance(cfg.get(k), str):
            cfg[k] = vault.decrypt(cfg[k])
    cfg.update(pinned())
    return cfg


def pinned() -> dict:
    """Settings the platform fixes (OpenShift chart: retention, LLM lock, public address): WATCHOVER_SETTINGS='{"key": value}'.
    They win over config.json, so a change on the System page does not outlive the next read; change them in the deployment."""
    raw = os.environ.get("WATCHOVER_SETTINGS", "").strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): v for k, v in val.items()} if isinstance(val, dict) else {}


def save(values: dict) -> dict:
    cfg = load()
    cfg.update({k: v for k, v in values.items() if k in DEFAULTS or k.startswith("x_")})
    from . import vault
    for k in SECRET_KEYS:                                  # secrets never reach the disk in clear text
        if isinstance(cfg.get(k), str):
            cfg[k] = vault.encrypt(cfg[k])
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
