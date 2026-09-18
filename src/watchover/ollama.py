"""Ollama model management: discovery on the usual ports, installed models, pulls with progress, removal.

Pure stdlib. Watchover treats Ollama as its default local runtime: `scripts/install_linux.sh --with-ollama` installs it
next to the app and pulls the recommended models; this module lets the LLM page do the same from the browser.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Iterator

CANDIDATES = ("http://localhost:11434", "http://127.0.0.1:11434", "http://ollama:11434", "http://host.docker.internal:11434")
RECOMMENDED = [
    {"name": "qwen2.5:7b-instruct", "role": "chat", "note": "best Turkish among 7B models · ~4.7 GB · 6 GB RAM"},
    {"name": "llama3.1:8b", "role": "chat", "note": "strong general model · ~4.9 GB"},
    {"name": "gemma2:9b", "role": "chat", "note": "good reasoning · ~5.4 GB"},
    {"name": "qwen2.5:3b-instruct", "role": "chat", "note": "small & fast for CPU-only boxes · ~1.9 GB"},
    {"name": "bge-m3", "role": "embed", "note": "multilingual embeddings (Turkish ok) · ~1.2 GB"},
    {"name": "nomic-embed-text", "role": "embed", "note": "light English-first embeddings · ~0.3 GB"},
]


def _get(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def discover(candidates: tuple = CANDIDATES) -> str | None:
    """First reachable Ollama base URL (native API root, without /v1)."""
    for base in candidates:
        try:
            _get(base + "/api/tags", 1.5)
            return base
        except Exception:  # noqa: BLE001
            continue
    return None


def installed(base: str) -> list[dict]:
    doc = _get(base.rstrip("/") + "/api/tags", 5)
    out = []
    for m in doc.get("models", []):
        det = m.get("details", {}) or {}
        out.append({"name": m.get("name") or m.get("model"), "size_gb": round((m.get("size") or 0) / 1e9, 2), "family": det.get("family", ""),
                    "params": det.get("parameter_size", ""), "quant": det.get("quantization_level", ""), "modified": (m.get("modified_at") or "")[:16]})
    return out


def pull(base: str, model: str, timeout: float = 3600) -> Iterator[dict]:
    """Streams {"status", "completed", "total", "pct"} while Ollama downloads the model."""
    req = urllib.request.Request(base.rstrip("/") + "/api/pull", data=json.dumps({"name": model, "stream": True}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            tot, done = doc.get("total") or 0, doc.get("completed") or 0
            yield {"status": doc.get("status", ""), "completed": done, "total": tot, "pct": (done / tot) if tot else None, "error": doc.get("error", "")}


def remove(base: str, model: str) -> bool:
    req = urllib.request.Request(base.rstrip("/") + "/api/delete", data=json.dumps({"name": model}).encode(), headers={"Content-Type": "application/json"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False


def running(base: str) -> list[dict]:
    """Models currently loaded in memory (/api/ps)."""
    try:
        doc = _get(base.rstrip("/") + "/api/ps", 5)
    except Exception:  # noqa: BLE001
        return []
    return [{"name": m.get("name"), "size_vram_gb": round((m.get("size_vram") or 0) / 1e9, 2), "until": (m.get("expires_at") or "")[:16]} for m in doc.get("models", [])]
