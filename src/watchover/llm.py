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
import re
import ssl
import threading
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


_last = threading.local()


def last_call() -> dict | None:
    """The record of the latest call made on this thread (tokens, latency); the chat endpoints return it with the answer."""
    return getattr(_last, "rec", None)


def _usage(doc: dict) -> dict:
    """Token counts from a provider response, whichever shape it uses: Ollama (prompt_eval_count / eval_count), OpenAI-compatible
    (usage.prompt_tokens / completion_tokens), Anthropic (usage.input_tokens / output_tokens), Gemini (usageMetadata.*TokenCount).
    Missing counts stay None; the caller estimates them from the text and flags the record as estimated."""
    u = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    g = doc.get("usageMetadata") if isinstance(doc.get("usageMetadata"), dict) else {}
    tin = doc.get("prompt_eval_count", u.get("prompt_tokens", u.get("input_tokens", g.get("promptTokenCount"))))
    tout = doc.get("eval_count", u.get("completion_tokens", u.get("output_tokens", g.get("candidatesTokenCount"))))
    return {"tokens_in": int(tin) if isinstance(tin, (int, float)) else None, "tokens_out": int(tout) if isinstance(tout, (int, float)) else None}


def estimate_tokens(text: str) -> int:
    """Rough count when the provider reports none: about four characters per token for log-heavy, mixed-language text."""
    return (len(text) + 3) // 4


def _finish_tokens(rec: dict, usage: dict | None, prompt_chars: int, answer_chars: int) -> None:
    u = usage or {}
    est = u.get("tokens_in") is None or u.get("tokens_out") is None
    rec["tokens_in"] = u.get("tokens_in") if u.get("tokens_in") is not None else (prompt_chars + 3) // 4
    rec["tokens_out"] = u.get("tokens_out") if u.get("tokens_out") is not None else (answer_chars + 3) // 4
    rec["tokens_est"] = int(est)


def _report(rec: dict) -> None:
    _last.rec = dict(rec)
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
def ollama_version(cfg: LLMConfig) -> tuple:
    """(major, minor, patch) from GET /api/version; () when unknown. Structured outputs (`format` with a JSON schema) need
    Ollama >= 0.5.0; older servers get `format: "json"` and the caller's validator does the rest."""
    try:
        doc = _http(cfg.root + "/api/version", None, _headers(cfg), min(cfg.timeout, 10))
        return tuple(int(x) for x in re.findall(r"\d+", str(doc.get("version") or ""))[:3])
    except Exception:  # noqa: BLE001
        return ()


def _chat_raw(cfg: LLMConfig, messages: list[dict], temperature: float, max_tokens: int, fmt=None, seed=None,
              options: dict | None = None) -> tuple[str, dict]:
    """-> (answer text, token usage as reported by the provider). `fmt`: a JSON schema (dict) or "json" — Ollama `format`,
    OpenAI-compatible `response_format`; ignored by providers without it (the caller validates). `seed`: Ollama
    `options.seed`, OpenAI-compatible `seed`."""
    k = cfg.kind
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if k == "ollama":
        opts = {"temperature": temperature, "num_predict": max_tokens, **(options or {})}
        if seed is not None:
            opts["seed"] = int(seed)
        body = {"model": cfg.model, "messages": messages, "stream": False, "options": opts}
        if fmt is not None:
            body["format"] = fmt
        doc = _http(cfg.root + "/api/chat", body, _headers(cfg), cfg.timeout)
        return doc["message"]["content"].strip(), _usage(doc)
    if k == "anthropic":
        doc = _http(cfg.root + "/v1/messages", {"model": cfg.model, "system": system or None, "max_tokens": max_tokens, "temperature": temperature,
                                                  "messages": [{"role": m["role"], "content": m["content"]} for m in turns]}, _headers(cfg), cfg.timeout)
        return "".join(b.get("text", "") for b in doc.get("content", []) if b.get("type") == "text").strip(), _usage(doc)
    if k == "gemini":
        body = {"contents": [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]} for m in turns],
                "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        doc = _http(f"{cfg.root}/v1beta/models/{cfg.model}:generateContent", body, _headers(cfg), cfg.timeout)
        return "".join(p.get("text", "") for p in doc["candidates"][0]["content"]["parts"]).strip(), _usage(doc)
    url = _openai_root(cfg) + "/chat/completions"
    if cfg.extra.get("api_version"):
        url += f"?api-version={cfg.extra['api_version']}"
    body = {"model": cfg.model, "messages": messages, "temperature": temperature, "stream": False, "max_tokens": max_tokens}
    if fmt is not None:
        body["response_format"] = ({"type": "json_schema", "json_schema": {"name": "answer", "schema": fmt, "strict": True}}
                                   if isinstance(fmt, dict) else {"type": "json_object"})
    if seed is not None:
        body["seed"] = int(seed)
    doc = _http(url, body, _headers(cfg), cfg.timeout)
    return doc["choices"][0]["message"]["content"].strip(), _usage(doc)


def chat_messages(cfg: LLMConfig, messages: list[dict], temperature: float = 0.2, max_tokens: int = 900, kind: str = "chat",
                  fmt=None, seed=None, options: dict | None = None) -> str:
    """Full conversation through the configured provider; timed and reported to the sink. `fmt` / `seed` / `options` are
    passed to providers that support them (the action agent's selector: a per-case JSON schema, the case id as seed)."""
    t0 = time.perf_counter()
    rec = {"provider": cfg.kind, "model": cfg.model, "kind": kind, "prompt_chars": sum(len(m.get("content", "")) for m in messages), "answer_chars": 0, "ok": 1, "error": ""}
    usage = None
    try:
        out, usage = _chat_raw(cfg, messages, temperature, max_tokens, fmt=fmt, seed=seed, options=options)
        rec["answer_chars"] = len(out)
        return out
    except Exception as e:  # noqa: BLE001
        rec.update(ok=0, error=str(e)[:300])
        raise
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        _finish_tokens(rec, usage, rec["prompt_chars"], rec["answer_chars"])
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
    usage: dict | None = None
    try:
        if k == "ollama":
            out = []
            for x in texts:
                doc = _http(cfg.root + "/api/embeddings", {"model": cfg.embed_model, "prompt": x}, _headers(cfg), cfg.timeout)
                out.append(doc["embedding"])
                u = _usage(doc)
                if u["tokens_in"] is not None:
                    usage = {"tokens_in": (usage or {}).get("tokens_in", 0) + u["tokens_in"], "tokens_out": 0}
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
        u = _usage(doc)
        if u["tokens_in"] is not None:
            usage = {"tokens_in": u["tokens_in"], "tokens_out": 0}
        return [d["embedding"] for d in sorted(doc["data"], key=lambda d: d.get("index", 0))]
    except Exception as e:  # noqa: BLE001
        rec.update(ok=0, error=str(e)[:300])
        raise
    finally:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        _finish_tokens(rec, usage, rec["prompt_chars"], 0)
        _report(rec)


def test_connection(cfg: LLMConfig) -> tuple[bool, str]:
    try:
        models = list_models(cfg)
        if cfg.model and models and cfg.model not in models and not any(m.startswith(cfg.model) for m in models):
            return True, f"{cfg.kind}: reachable; model '{cfg.model}' not in list ({', '.join(models[:6])}…)"
        return True, f"{cfg.kind}: reachable; {len(models)} model(s)" + (f": {', '.join(models[:6])}" if models else "")
    except Exception as e:  # noqa: BLE001
        return False, f"{cfg.kind}: {e}"
