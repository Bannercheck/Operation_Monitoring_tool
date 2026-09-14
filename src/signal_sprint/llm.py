"""Optional LLM enrichment through any OpenAI-compatible endpoint (OpenLLM, Ollama, vLLM, LM Studio, LocalAI, cloud APIs).

The engine never depends on this: if no endpoint is configured or the call fails, the deterministic narrative stays.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from dataclasses import dataclass

_CTX = ssl.create_default_context()


@dataclass
class LLMConfig:
    base_url: str = ""          # e.g. http://localhost:3000/v1 (OpenLLM), http://localhost:11434/v1 (Ollama), http://localhost:8000/v1 (vLLM)
    model: str = ""             # e.g. llama3.1, mistral, qwen2.5
    api_key: str = ""           # optional; local servers usually ignore it
    timeout: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)


def _headers(cfg: LLMConfig) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.api_key:
        h["Authorization"] = f"Bearer {cfg.api_key}"
    return h


def list_models(cfg: LLMConfig) -> list[str]:
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/models", headers=_headers(cfg))
    with urllib.request.urlopen(req, timeout=cfg.timeout, context=_CTX) as r:
        doc = json.loads(r.read())
    data = doc.get("data", doc if isinstance(doc, list) else [])
    return [m.get("id") or m.get("name") for m in data if isinstance(m, dict)]


def chat(cfg: LLMConfig, prompt: str, system: str = "You are a senior SRE. Be concise and cite evidence refs in [brackets].") -> str:
    body = {"model": cfg.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "temperature": 0.2, "stream": False}
    req = urllib.request.Request(cfg.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                 headers=_headers(cfg), method="POST")
    with urllib.request.urlopen(req, timeout=cfg.timeout, context=_CTX) as r:
        doc = json.loads(r.read())
    return doc["choices"][0]["message"]["content"].strip()


def test_connection(cfg: LLMConfig) -> tuple[bool, str]:
    try:
        models = list_models(cfg)
        if cfg.model and models and cfg.model not in models:
            return True, f"reachable; model '{cfg.model}' not in list ({', '.join(models[:6])}…)"
        return True, f"reachable; {len(models)} model(s)" + (f": {', '.join(models[:6])}" if models else "")
    except Exception as e:  # noqa: BLE001
        return False, str(e)
