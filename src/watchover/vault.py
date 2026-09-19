"""Secrets at rest. Values such as SMTP / SMS / LLM / OIDC secrets and pull-source credentials are stored encrypted
(Fernet: AES-128-CBC + HMAC-SHA256, from the `cryptography` package) with a key that lives only in the data folder
(`data/.vault.key`, mode 0600). Passwords of user accounts are never stored at all: auth.py keeps a scrypt hash.
Encrypted values carry the prefix enc:v1: so plain legacy values still load and are re-encrypted on the next save."""
from __future__ import annotations

import os
from pathlib import Path

PREFIX = "enc:v1:"
_FERNET = None


def key_path() -> Path:
    from . import settings
    return settings.home() / ".vault.key"


def available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _fernet():
    global _FERNET
    if _FERNET is None:
        from cryptography.fernet import Fernet
        p = key_path()
        if not p.exists():
            p.write_bytes(Fernet.generate_key())
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
        _FERNET = Fernet(p.read_bytes().strip())
    return _FERNET


def encrypt(value: str) -> str:
    """Plain -> enc:v1:…; empty or already-encrypted values pass through; without `cryptography` the value stays plain."""
    if not value or not isinstance(value, str) or value.startswith(PREFIX) or not available():
        return value
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """enc:v1:… -> plain; anything else passes through; an undecryptable value (key replaced) becomes '' rather than a crash."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    if not available():
        return ""
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def status() -> dict:
    p = key_path()
    return {"available": available(), "key_file": str(p), "key_exists": p.exists(), "mode": oct(p.stat().st_mode & 0o777) if p.exists() else "-"}
