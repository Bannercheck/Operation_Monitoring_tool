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


# ---------------------------------------------------------------- model creation from a trained adapter (autotrain)
def _post(base: str, path: str, body: dict, timeout: float = 600) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    out: dict = {}
    for line in raw.splitlines():                      # streaming or single JSON: keep the last status
        try:
            out = json.loads(line) or out
        except ValueError:
            continue
    return out


def exists(base: str, name: str) -> bool:
    try:
        return any((m.get("name") or m.get("model")) in (name, f"{name}:latest") for m in _get(base.rstrip("/") + "/api/tags", 5).get("models", []))
    except Exception:  # noqa: BLE001
        return False


def copy(base: str, src: str, dst: str) -> None:
    _post(base, "/api/copy", {"source": src, "destination": dst}, 60)


def delete(base: str, name: str) -> None:
    req = urllib.request.Request(base.rstrip("/") + "/api/delete", data=json.dumps({"name": name}).encode(), headers={"Content-Type": "application/json"}, method="DELETE")
    with urllib.request.urlopen(req, timeout=60):
        pass


def upload_blob(base: str, path: str, timeout: float = 1800) -> str:
    """POST /api/blobs/sha256:<digest> with the file body; returns the digest reference Ollama expects in `adapters`."""
    import hashlib
    data = open(path, "rb").read()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    req = urllib.request.Request(base.rstrip("/") + "/api/blobs/" + digest, data=data, headers={"Content-Type": "application/octet-stream"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            pass
    except urllib.error.HTTPError as e:
        if e.code not in (200, 201):                 # an already-present blob answers 200/201 as well
            raise
    return digest


def create_from_adapter(base: str, name: str, adapter_dir: str, from_tag: str, system: str = "", parameters: dict | None = None,
                        server_adapter_path: str | None = None) -> dict:
    """Create `name` = `from_tag` + the safetensors LoRA in `adapter_dir`. First the blob API (Ollama >= 0.5: files are
    uploaded, no shared filesystem needed); if the server rejects it, the legacy Modelfile form with a path that is visible
    to the Ollama server (`server_adapter_path`, a shared volume)."""
    import os
    params = parameters or {"temperature": 0.2, "num_ctx": 4096, "stop": ["<|im_end|>", "<|endoftext|>"]}
    files = [f for f in sorted(os.listdir(adapter_dir)) if f.endswith((".safetensors", ".json"))]
    try:
        adapters = {f: upload_blob(base, os.path.join(adapter_dir, f)) for f in files}
        return _post(base, "/api/create", {"model": name, "from": from_tag, "adapters": adapters, "system": system or None, "parameters": params, "stream": False}) | {"via": "blobs"}
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as first:
        if not server_adapter_path:
            raise
        mf = f"FROM {from_tag}\nADAPTER {server_adapter_path}\n" + (f'SYSTEM """{system}"""\n' if system else "") + \
             "".join(f"PARAMETER {k} {v}\n" for k, v in params.items() if k != "stop") + "".join(f'PARAMETER stop "{s}"\n' for s in params.get("stop", []))
        try:
            return _post(base, "/api/create", {"name": name, "modelfile": mf, "stream": False}) | {"via": "modelfile"}
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(f"ollama create failed (blobs: {first}; modelfile: {second})") from second
