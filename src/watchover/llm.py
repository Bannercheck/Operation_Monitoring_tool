"""Provider-agnostic LLM client. One config, four wire formats, no SDKs (stdlib urllib only):

  provider = "openai"     OpenAI-compatible: Ollama (/v1), vLLM, LM Studio, LocalAI, OpenLLM, OpenAI, Groq, Mistral, Together, Azure (with api-version in URL)
  provider = "ollama"     Ollama native API (/api/chat, /api/embeddings, /api/tags) -- model management lives in ollama.py
  provider = "anthropic"  Anthropic Messages API (/v1/messages)
  provider = "gemini"     Google Generative Language API (models/<m>:generateContent)
  provider = "auto"       guessed from the URL (default)

Every call is timed and reported to a sink (knowledge base) so the LLM page can show success rate, latency and
grounding. The engine never depends on this module: if no endpoint is configured or a call fails, the deterministic
narrative stays.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

_CTX = ssl.create_default_context()
PROVIDERS = ("auto", "openai", "ollama", "anthropic", "gemini")
DEFAULT_SYSTEM = "You are a senior SRE. Be concise and cite evidence refs in [brackets]."

_sink: Callable[[dict], None] | None = None


def set_sink(fn: Callable[[dict], None] | None) -> None:
    """Where call records go ({provider, model, kind, ok, latency_ms, prompt_chars, answer_chars, error})."""
    global _sink
    _sink = fn


def _report(rec: dict) -> None:
    if _sink:
        try:
            _sink(rec)
        except Exception:  # noqa: BLE001 - metrics must never break a call
            pass


@dataclass
class LLMConfig:
    base_url: str = ""          # http://localhost:11434 (Ollama native or /v1), http://localhost:8000/v1 (vLLM), https://api.openai.com/v1 ...
    model: str = ""             # e.g. qwen2.5:7b-instruct, llama3.1, gpt-4o-mini, claude-sonnet-4-5, gemini-2.0-flash
    api_key: str = ""           # optional; local servers ignore it
    timeout: int = 120
    embed_model: str = ""       # e.g. bge-m3, nomic-embed-text, text-embedding-3-small
    provider: str = "auto"
    extra: dict = field(default_factory=dict)   # provider extras (e.g. {"api_version": "2024-02-01"} for Azure)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def kind(self) -> str:
        """Resolved provider."""
        if self.provider and self.provider != "auto":
            return self.provider
        u = (self.base_url or "").lower()
        if "anthropic.com" in u:
            return "anthropic"
        if "generativelanguage.googleapis.com" in u:
            return "gemini"
        if ":11434" in u and not u.rstrip("/").endswith("/v1"):
            return "ollama"
        return "openai"

    @property
    def root(self) -> str:
        return (self.base_url or "").rstrip("/")


# ---------------------------------------------------------------- http
def _http(url: str, body: dict | None, headers: dict, timeout: int, method: str | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers}, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def _headers(cfg: LLMConfig) -> dict:
    k = cfg.kind
    if k == "anthropic":
        return {"x-api-key": cfg.api_key, "anthropic-version": cfg.extra.get("anthropic_version", "2023-06-01")}
    if k == "gemini":
        return {"x-goog-api-key": cfg.api_key} if cfg.api_key else {}
    h = {}
    if cfg.api_key:
        h["Authorization"] = f"Bearer {cfg.api_key}"
        h["api-key"] = cfg.api_key            # Azure OpenAI reads this one
    return h


def _openai_root(cfg: LLMConfig) -> str:
    r = cfg.root
    return r if r.endswith("/v1") or "/openai/deployments" in r else r + "/v1"


# ---------------------------------------------------------------- models
def list_models(cfg: LLMConfig) -> list[str]:
    k = cfg.kind
    if k == "ollama":
        doc = _http(cfg.root + "/api/tags", None, _headers(cfg), cfg.timeout)
        return [m.get("name") or m.get("model") for m in doc.get("models", [])]
    if k == "anthropic":
        doc = _http(cfg.root + "/v1/models", None, _headers(cfg), cfg.timeout)
        return [m.get("id") for m in doc.get("data", [])]
    if k == "gemini":
        doc = _http(cfg.root + "/v1beta/models", None, _headers(cfg), cfg.timeout)
        return [m.get("name", "").replace("models/", "") for m in doc.get("models", [])]
    doc = _http(_openai_root(cfg) + "/models", None, _headers(cfg), cfg.timeout)
    data = doc.get("data", doc if isinstance(doc, list) else [])
    return [m.get("id") or m.get("name") for m in data if isinstance(m, dict)]


# ---------------------------------------------------------------- chat
def _chat_raw(cfg: LLMConfig, messages: list[dict], temperature: float, max_tokens: int) -> str:
    k = cfg.kind
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if k == "ollama":
        doc = _http(cfg.root + "/api/chat", {"model": cfg.model, "messages": messages, "stream": False,
                                               "options": {"temperature": temperature, "num_predict": max_tokens}}, _headers(cfg), cfg.timeout)
        return doc["message"]["content"].strip()
    if k == "anthropic":
        doc = _http(cfg.root + "/v1/messages", {"model": cfg.model, "system": system or None, "max_tokens": max_tokens, "temperature": temperature,
                                                  "messages": [{"role": m["role"], "content": m["content"]} for m in turns]}, _headers(cfg), cfg.timeout)
        return "".join(b.get("text", "") for b in doc.get("content", []) if b.get("type") == "text").strip()
    if k == "gemini":
        body = {"contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in turns],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        doc = _http(f"{cfg.root}/v1beta/models/{cfg.model}:generateContent", body, _headers(cfg), cfg.timeout)
        return "".join(p.get("text", "") for p in doc["candidates"][0]["content"]["parts"]).strip()
    url = _openai_root(cfg) + "/chat/completions"
    if cfg.extra.get("api_version"):
        url += f"?api-version={cfg.extra['api_version']}"
    doc = _http(url, {"model": cfg.model, "messages": messages, "temperature": temperature, "stream": False, "max_tokens": max_tokens}, _headers(cfg), cfg.timeout)
    return doc["choices"][0]["message"]["content"].strip()


def chat_messages(cfg: LLMConfig, messages: list[dict], temperature: float = 0.2, max_tokens: int = 900, kind: str = "chat") -> str:
    """Full conversation through the configured provider; timed and reported to the sink."""
    t0 = time.perf_counter()
    rec = {"provider": cfg.kind, "model": cfg.model, "kind": kind, "prompt_chars": sum(len(m.get("content", "")) for m in messages), "answer_chars": 0, "ok": 1, "error": ""}
    try:
        out = _chat_raw(cfg, messages, temperature, max_tokens)
        rec["answer_chars"] = len(out)
        return out
    except Exception as e:  # noqa: BLE001
        rec.update(ok=0, error=str(e)[:300])
        raise
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        _report(rec)


def chat(cfg: LLMConfig, prompt: str, system: str = DEFAULT_SYSTEM, kind: str = "explain") -> str:
    return chat_messages(cfg, [{"role": "system", "content": system}, {"role": "user", "content": prompt}], kind=kind)


# ---------------------------------------------------------------- embeddings
def embed(cfg: LLMConfig, texts: list[str]) -> list[list[float]]:
    if not (cfg.base_url and cfg.embed_model):
        raise RuntimeError("embedding model not configured")
    k = cfg.kind
    t0 = time.perf_counter()
    rec = {"provider": k, "model": cfg.embed_model, "kind": "embed", "prompt_chars": sum(len(x) for x in texts), "answer_chars": 0, "ok": 1, "error": ""}
    try:
        if k == "ollama":
            out = []
            for x in texts:
                doc = _http(cfg.root + "/api/embeddings", {"model": cfg.embed_model, "prompt": x}, _headers(cfg), cfg.timeout)
                out.append(doc["embedding"])
            return out
        if k == "gemini":
            out = []
            for x in texts:
                doc = _http(f"{cfg.root}/v1beta/models/{cfg.embed_model}:embedContent", {"content": {"parts": [{"text": x}]}}, _headers(cfg), cfg.timeout)
                out.append(doc["embedding"]["values"])
            return out
        if k == "anthropic":
            raise RuntimeError("Anthropic has no embeddings endpoint; use an OpenAI-compatible or Ollama embedding model")
        doc = _http(_openai_root(cfg) + "/embeddings", {"model": cfg.embed_model, "input": texts}, _headers(cfg), cfg.timeout)
        return [d["embedding"] for d in sorted(doc["data"], key=lambda d: d.get("index", 0))]
    except Exception as e:  # noqa: BLE001
        rec.update(ok=0, error=str(e)[:300])
        raise
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        _report(rec)


def test_connection(cfg: LLMConfig) -> tuple[bool, str]:
    try:
        models = list_models(cfg)
        if cfg.model and models and cfg.model not in models and not any(m.startswith(cfg.model) for m in models):
            return True, f"{cfg.kind}: reachable; model '{cfg.model}' not in list ({', '.join(models[:6])}…)"
        return True, f"{cfg.kind}: reachable; {len(models)} model(s)" + (f": {', '.join(models[:6])}" if models else "")
    except Exception as e:  # noqa: BLE001
        return False, f"{cfg.kind}: {e}"
