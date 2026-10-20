"""MCP sources ("data pulling"): external MCP servers under Ayarlar › Veri kaynakları › MCP kaynakları.

A source is an external MCP server reached over streamable HTTP or the older HTTP+SSE transport (stdio is not offered: the
container must not start arbitrary programs). Its auth header or bearer token is stored like every other secret (vault.py,
enc:v1:) and never shown back. Discovery lists its tools and resources (kept with the source).

A pull is one tool call with fixed arguments on an interval; its result is ingested as
  * logs       -> lines in the live store under a source name (JSON objects keep their fields; text becomes one line each),
  * inventory  -> inventory (CMDB) rows merged by hostname,
  * itsm       -> tickets shown and correlated on the ITSM page.
A source belongs to a team or to the installation, and so do its pulls: a team's log lines pass the same ingest guard as a
team's agent (only the team's hosts), a team's inventory rows only touch the team's hosts, a team's tickets are seen by that
team. Every pull is bounded (response bytes, total time, rows) and keeps its state: last run, last success, rows, error, and a
back-off after failures. Every outgoing call is audited (mcp_audit, direction "out").

"Watchover'a sor" may call the tools an administrator allowed on a source (assist_tools), read-only (a tool that declares
readOnlyHint=false or destructiveHint=true is never called), with the question masked by privacy.scrub; answers come back as
[M1], [M2] citations.

The client is a small synchronous JSON-RPC client on urllib: it runs inside worker threads with hard time and size limits
and works without the optional `mcp` package (Python 3.9)."""
from __future__ import annotations

import hashlib
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from .connectors import dig, open_url, parse_headers

UTC = timezone.utc
PROTOCOL = "2025-06-18"
TRANSPORTS = ("http", "sse")
AUTHS = ("none", "bearer", "header")
TARGETS = ("logs", "inventory", "itsm")
QUERY_KEYS = ("query", "q", "question", "search", "text", "prompt", "keywords")
_HDR = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_NAME = re.compile(r"[^A-Za-z0-9_.\-]+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class McpClientError(Exception):
    code = "error"


class McpTimeout(McpClientError):
    code = "timeout"


class McpTooLarge(McpClientError):
    code = "too_large"


class McpRpcError(McpClientError):
    code = "rpc_error"


class McpToolError(McpClientError):
    code = "tool_error"


def _json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- client
class Client:
    """One MCP session with a total time budget (`timeout` seconds from construction) and a total byte budget (`max_bytes`)."""

    def __init__(self, url: str, transport: str = "http", headers: Optional[dict] = None, timeout: float = 15.0, max_bytes: int = 1_048_576,
                 verify_tls: bool = True, allow_private: bool = True):
        if transport not in TRANSPORTS:
            raise McpClientError(f"transport not supported: {transport} (http or sse; stdio is not available in the container)")
        self.url, self.transport, self.headers = url, transport, dict(headers or {})
        self.timeout, self.max_bytes, self.allow_private = float(timeout), int(max_bytes), allow_private
        self.deadline = time.monotonic() + self.timeout
        self.used = 0
        self.session: Optional[str] = None
        self.version: Optional[str] = None
        self.post_url: Optional[str] = None
        self._stream = None
        self._events_it = None
        self._id = 0
        if verify_tls:
            self.ctx = ssl.create_default_context()
        else:
            self.ctx = ssl.create_default_context(); self.ctx.check_hostname = False; self.ctx.verify_mode = ssl.CERT_NONE

    # ---- plumbing
    def _left(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise McpTimeout(f"no answer within {self.timeout:g} s")
        return left

    def _open(self, url: str, method: str, body: Optional[bytes], headers: dict):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            return open_url(req, max(0.5, self._left()), self.ctx, allow_private=self.allow_private)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read(400).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                detail = ""
            raise McpClientError(f"HTTP {e.code}: {detail[:200]}".strip())
        except (socket.timeout, TimeoutError):
            raise McpTimeout(f"no answer within {self.timeout:g} s")
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise McpTimeout(f"no answer within {self.timeout:g} s")
            raise McpClientError(f"connection failed: {e.reason}"[:200])

    def _count(self, n: int) -> None:
        self.used += n
        if self.used > self.max_bytes:
            raise McpTooLarge(f"answer larger than {self.max_bytes // 1024} KB")

    def _read_all(self, r) -> bytes:
        out = []
        try:
            while True:
                self._left()
                chunk = r.read(65536)
                if not chunk:
                    return b"".join(out)
                self._count(len(chunk)); out.append(chunk)
        except (socket.timeout, TimeoutError):
            raise McpTimeout(f"no answer within {self.timeout:g} s")

    def _events(self, r):
        """(event, data) pairs of a text/event-stream."""
        ev, data = "message", []
        try:
            while True:
                self._left()
                line = r.readline(65537)
                if not line:
                    if data:
                        yield ev, "\n".join(data)
                    return
                self._count(len(line))
                s = line.decode("utf-8", "replace").rstrip("\r\n")
                if not s:
                    if data:
                        yield ev, "\n".join(data)
                    ev, data = "message", []
                    continue
                if s.startswith(":"):
                    continue
                k, _, v = s.partition(":")
                v = v[1:] if v.startswith(" ") else v
                if k == "event":
                    ev = v
                elif k == "data":
                    data.append(v)
        except (socket.timeout, TimeoutError):
            raise McpTimeout(f"no answer within {self.timeout:g} s")

    @staticmethod
    def _match(obj, mid) -> bool:
        return isinstance(obj, dict) and obj.get("id") == mid and ("result" in obj or "error" in obj)

    def _rpc_http(self, msg: dict):
        h = {**self.headers, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        if self.version:
            h["MCP-Protocol-Version"] = self.version
        r = self._open(self.url, "POST", json.dumps(msg).encode("utf-8"), h)
        with r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self.session = sid
            if "id" not in msg:
                return None
            if "text/event-stream" in (r.headers.get("Content-Type") or ""):
                for _, data in self._events(r):
                    obj = _json(data)
                    if self._match(obj, msg["id"]):
                        return obj
                raise McpClientError("event stream ended without a response")
            obj = _json(self._read_all(r).decode("utf-8", "replace"))
            if isinstance(obj, list):
                obj = next((o for o in obj if self._match(o, msg["id"])), None)
            return obj

    def _connect_sse(self) -> None:
        self._stream = self._open(self.url, "GET", None, {**self.headers, "Accept": "text/event-stream"})
        self._events_it = self._events(self._stream)
        for ev, data in self._events_it:
            if ev == "endpoint":
                post = urljoin(self.url, data.strip())
                if urlparse(post).netloc != urlparse(self.url).netloc:
                    raise McpClientError("SSE endpoint on another host refused")
                self.post_url = post
                return
        raise McpClientError("SSE stream ended before the endpoint event")

    def _rpc_sse(self, msg: dict):
        if self.post_url is None:
            self._connect_sse()
        r = self._open(self.post_url, "POST", json.dumps(msg).encode("utf-8"), {**self.headers, "Content-Type": "application/json"})
        with r:
            r.read(4096)
        if "id" not in msg:
            return None
        for ev, data in self._events_it:
            obj = _json(data) if ev == "message" else None
            if self._match(obj, msg["id"]):
                return obj
        raise McpClientError("SSE stream ended without a response")

    def rpc(self, method: str, params: Optional[dict] = None, notify: bool = False):
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        obj = (self._rpc_sse if self.transport == "sse" else self._rpc_http)(msg)
        if notify:
            return None
        if not isinstance(obj, dict):
            raise McpClientError("no JSON-RPC response")
        if "error" in obj:
            e = obj["error"] or {}
            err = McpRpcError(f"{e.get('code')}: {str(e.get('message'))[:200]}")
            err.rpc_code = e.get("code")
            raise err
        return obj.get("result") or {}

    # ---- MCP
    def initialize(self) -> dict:
        res = self.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "watchover", "version": "1"}})
        self.version = str(res.get("protocolVersion") or PROTOCOL)
        self.rpc("notifications/initialized", notify=True)
        return res

    def _paged(self, method: str, key: str) -> list:
        out, cursor = [], None
        for _ in range(5):
            res = self.rpc(method, {"cursor": cursor} if cursor else {})
            out += res.get(key) or []
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return out

    def list_tools(self) -> list:
        return self._paged("tools/list", "tools")

    def list_resources(self) -> list:
        try:
            return self._paged("resources/list", "resources")
        except McpRpcError as e:
            if getattr(e, "rpc_code", None) in (-32601, -32602):
                return []
            raise

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict:
        res = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if res.get("isError"):
            raise McpToolError("tool error: " + _text(res)[:300])
        return res

    def close(self) -> None:
        try:
            if self._stream is not None:
                self._stream.close()
            elif self.session and self.transport == "http":
                self.deadline = time.monotonic() + 2
                h = {**self.headers, "Mcp-Session-Id": self.session}
                self._open(self.url, "DELETE", None, h).close()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def __enter__(self) -> "Client":
        self.initialize()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _text(res: dict) -> str:
    parts = []
    for c in res.get("content") or []:
        if c.get("type") == "text":
            parts.append(str(c.get("text") or ""))
        elif c.get("type") == "resource" and isinstance(c.get("resource"), dict) and "text" in c["resource"]:
            parts.append(str(c["resource"]["text"]))
    return "\n".join(parts)


def payload(res: dict):
    """A CallToolResult -> its structured content, else its text parsed as JSON when it is JSON, else the text."""
    sc = res.get("structuredContent")
    if sc is not None:
        return sc["result"] if isinstance(sc, dict) and list(sc) == ["result"] else sc
    text = _text(res)
    j = _json(text)
    return j if j is not None else text


def records(obj, items: str = "") -> list:
    """The list of records in a tool result: the `items` path, else the list itself, a well-known list key, the only list of
    objects, or the object; text becomes one record per line (a JSON line keeps its fields)."""
    if items:
        try:
            obj = dig(obj, items)
        except (KeyError, IndexError, TypeError, ValueError):
            raise McpClientError(f"items path not found: {items}")
    if isinstance(obj, str):
        out = []
        for ln in obj.splitlines():
            ln = ln.strip()
            if ln:
                j = _json(ln) if ln.startswith("{") else None
                out.append(j if isinstance(j, dict) else {"msg": ln})
        return out
    if isinstance(obj, list):
        return [x if isinstance(x, dict) else {"msg": json.dumps(x, ensure_ascii=False) if not isinstance(x, str) else x} for x in obj]
    if isinstance(obj, dict):
        for k in ("items", "results", "records", "rows", "data", "logs", "lines", "events", "hits", "tickets", "hosts", "value"):
            v = obj.get(k)
            if isinstance(v, list):
                return records(v)
            if isinstance(v, dict) and k in ("data", "hits"):
                inner = records(v)
                if inner and inner != [v]:
                    return inner
        lists = [v for v in obj.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        return lists[0] if len(lists) == 1 else [obj]
    return [] if obj is None else [{"msg": str(obj)}]


# ---------------------------------------------------------------- store
class McpSources:
    SRC = ("name", "url", "transport", "auth", "header_name", "secret", "headers", "verify_tls", "enabled", "timeout_s", "max_kb", "assist_tools", "team_id",
           "tools_cache", "discovered_at", "last_ok", "last_err", "created_by", "created_at")
    PULL = ("source_id", "name", "tool", "arguments", "interval_s", "target", "source_name", "env", "mapping", "max_rows", "enabled", "team_id", "status",
            "last_run", "last_ok", "last_err", "last_rows", "last_skipped", "total_rows", "runs", "fails", "last_ms", "created_by", "created_at")

    def __init__(self, kb):
        self.kb = kb
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS mcp_sources (id {pk}, name TEXT NOT NULL UNIQUE, url TEXT NOT NULL, transport TEXT DEFAULT 'http',
            auth TEXT DEFAULT 'none', header_name TEXT DEFAULT '', secret TEXT DEFAULT '', headers TEXT DEFAULT '', verify_tls INTEGER DEFAULT 1,
            enabled INTEGER DEFAULT 1, timeout_s INTEGER DEFAULT 15, max_kb INTEGER DEFAULT 1024, assist_tools TEXT DEFAULT '', team_id INTEGER,
            tools_cache TEXT DEFAULT '', discovered_at TEXT DEFAULT '', last_ok TEXT DEFAULT '', last_err TEXT DEFAULT '', created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT '')""")
        kb._exec(f"""CREATE TABLE IF NOT EXISTS mcp_pulls (id {pk}, source_id INTEGER NOT NULL, name TEXT NOT NULL, tool TEXT NOT NULL,
            arguments TEXT DEFAULT '{{}}', interval_s INTEGER DEFAULT 300, target TEXT DEFAULT 'logs', source_name TEXT DEFAULT '', env TEXT DEFAULT '',
            mapping TEXT DEFAULT '{{}}', max_rows INTEGER DEFAULT 1000, enabled INTEGER DEFAULT 1, team_id INTEGER, status TEXT DEFAULT 'never',
            last_run TEXT DEFAULT '', last_ok TEXT DEFAULT '', last_err TEXT DEFAULT '', last_rows INTEGER DEFAULT 0, last_skipped INTEGER DEFAULT 0,
            total_rows INTEGER DEFAULT 0, runs INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, last_ms INTEGER DEFAULT 0, created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT '')""")
        kb._exec(f"CREATE TABLE IF NOT EXISTS mcp_tickets (id {pk}, pull_id INTEGER NOT NULL, team_id INTEGER, data TEXT NOT NULL, fetched_at TEXT)")

    # ---- validation
    @staticmethod
    def clean_source(d: dict, partial: bool = False) -> dict:
        out = {}
        if "name" in d or not partial:
            name = str(d.get("name") or "").strip()[:80]
            if not name:
                raise ValueError("name is required")
            out["name"] = name
        if "url" in d or not partial:
            url = str(d.get("url") or "").strip()
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                raise ValueError("url must be an http(s) address of a streamable HTTP or SSE MCP endpoint (stdio is not supported)")
            out["url"] = url[:500]
        if "transport" in d or not partial:
            t = str(d.get("transport") or "http")
            if t not in TRANSPORTS:
                raise ValueError("transport must be http (streamable HTTP) or sse; stdio is not supported in the container")
            out["transport"] = t
        if "auth" in d or not partial:
            a = str(d.get("auth") or "none")
            if a not in AUTHS:
                raise ValueError("auth must be none, bearer or header")
            out["auth"] = a
        if "header_name" in d or not partial:
            hn = str(d.get("header_name") or "").strip()
            if hn and not _HDR.match(hn):
                raise ValueError("header_name: letters, digits and '-' only")
            out["header_name"] = hn
        if out.get("auth") == "header" and not out.get("header_name", d.get("header_name")):
            raise ValueError("header_name is required for auth=header")
        if "headers" in d or not partial:
            hdrs = str(d.get("headers") or "")[:2000]
            for k in parse_headers(hdrs):
                if not _HDR.match(k) or k.lower() in ("authorization", "cookie", "x-api-key"):
                    raise ValueError(f"extra header not allowed here: {k[:40]} (put credentials in the secret)")
            out["headers"] = hdrs
        for k, lo, hi, dflt in (("timeout_s", 1, 120, 15), ("max_kb", 16, 16384, 1024)):
            if k in d or not partial:
                try:
                    out[k] = max(lo, min(hi, int(d.get(k) if d.get(k) is not None else dflt)))
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number")
        for k in ("verify_tls", "enabled"):
            if k in d or not partial:
                out[k] = int(bool(d.get(k, True)))
        if "assist_tools" in d or not partial:
            out["assist_tools"] = ",".join(x.strip()[:80] for x in str(d.get("assist_tools") or "").split(",") if x.strip())[:1000]
        if "secret" in d and d["secret"] is not None:
            out["secret"] = str(d["secret"])[:4000]
        return out

    @staticmethod
    def clean_pull(d: dict, partial: bool = False) -> dict:
        out = {}
        for k, req in (("name", True), ("tool", True)):
            if k in d or not partial:
                v = str(d.get(k) or "").strip()[:120]
                if req and not v:
                    raise ValueError(f"{k} is required")
                out[k] = v
        for k, cap in (("arguments", 8000), ("mapping", 4000)):
            if k in d or not partial:
                v = d.get(k) if d.get(k) is not None else {}
                if isinstance(v, str):
                    v = _json(v) if v.strip() else {}
                if not isinstance(v, dict):
                    raise ValueError(f"{k} must be a JSON object")
                s = json.dumps(v, ensure_ascii=False)
                if len(s) > cap:
                    raise ValueError(f"{k} too large")
                out[k] = s
        if "target" in d or not partial:
            t = str(d.get("target") or "logs")
            if t not in TARGETS:
                raise ValueError("target must be logs, inventory or itsm")
            out["target"] = t
        for k, lo, hi, dflt in (("interval_s", 30, 86400, 300), ("max_rows", 1, 10000, 1000)):
            if k in d or not partial:
                try:
                    out[k] = max(lo, min(hi, int(d.get(k) if d.get(k) is not None else dflt)))
                except (TypeError, ValueError):
                    raise ValueError(f"{k} must be a number")
        if "source_name" in d or not partial:
            out["source_name"] = _NAME.sub("-", str(d.get("source_name") or "").strip())[:64].strip("-")
        if "env" in d or not partial:
            out["env"] = str(d.get("env") or "").strip()[:32]
        if "enabled" in d or not partial:
            out["enabled"] = int(bool(d.get("enabled", True)))
        return out

    # ---- sources
    def _src(self, r: dict, secret: bool = False) -> dict:
        from . import vault
        d = dict(r)
        d["verify_tls"], d["enabled"] = bool(d.get("verify_tls")), bool(d.get("enabled"))
        sec = d.pop("secret", "") or ""
        if secret:
            d["secret"] = vault.decrypt(sec)
        d["has_secret"] = bool(sec)
        d["tools"] = _json(d.pop("tools_cache", "") or "") or {}
        return d

    def sources(self, secret: bool = False) -> list:
        return [self._src(r, secret) for r in self.kb._exec("SELECT * FROM mcp_sources ORDER BY name")]

    def source(self, sid: int, secret: bool = False) -> dict:
        rows = self.kb._exec("SELECT * FROM mcp_sources WHERE id=?", (sid,))
        if not rows:
            raise KeyError(f"mcp source {sid}")
        return self._src(rows[0], secret)

    def add_source(self, d: dict, team_id: Optional[int], by: str = "") -> dict:
        from . import vault
        v = self.clean_source(d)
        if any(s["name"].lower() == v["name"].lower() for s in self.sources()):
            raise ValueError(f"an MCP source named '{v['name']}' already exists")
        v["secret"] = vault.encrypt(v.get("secret") or "")
        v.update(team_id=team_id, created_by=(by or "").lower(), created_at=_now())
        cols = list(v)
        sid = self.kb._insert(f"INSERT INTO mcp_sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(v[c] for c in cols))
        return self.source(sid)

    def update_source(self, sid: int, d: dict) -> dict:
        from . import vault
        v = self.clean_source(d, partial=True)
        cur = self.source(sid, secret=True)
        if "url" in v and v["url"] != cur.get("url"):           # re-pointed: the stored bearer token / headers do not follow (security review)
            v.setdefault("secret", "")
            if "headers" in cur:
                v.setdefault("headers", "")
        if "secret" in v:
            v["secret"] = vault.encrypt(v["secret"])
        if "name" in v and any(s["name"].lower() == v["name"].lower() and s["id"] != sid for s in self.sources()):
            raise ValueError(f"an MCP source named '{v['name']}' already exists")
        if v:
            self.kb._exec(f"UPDATE mcp_sources SET {', '.join(f'{k}=?' for k in v)} WHERE id=?", tuple(v.values()) + (sid,))
        return self.source(sid)

    def set_source(self, sid: int, **fields) -> None:
        self.kb._exec(f"UPDATE mcp_sources SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", tuple(fields.values()) + (sid,))

    def delete_source(self, sid: int) -> None:
        for p in self.pulls(sid):
            self.delete_pull(p["id"])
        self.kb._exec("DELETE FROM mcp_sources WHERE id=?", (sid,))

    # ---- pulls
    def _pull(self, r: dict) -> dict:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["arguments"] = _json(d.get("arguments") or "{}") or {}
        d["mapping"] = _json(d.get("mapping") or "{}") or {}
        return d

    def pulls(self, source_id: Optional[int] = None) -> list:
        if source_id is None:
            return [self._pull(r) for r in self.kb._exec("SELECT * FROM mcp_pulls ORDER BY id")]
        return [self._pull(r) for r in self.kb._exec("SELECT * FROM mcp_pulls WHERE source_id=? ORDER BY id", (source_id,))]

    def pull(self, pid: int) -> dict:
        rows = self.kb._exec("SELECT * FROM mcp_pulls WHERE id=?", (pid,))
        if not rows:
            raise KeyError(f"mcp pull {pid}")
        return self._pull(rows[0])

    def add_pull(self, source_id: int, d: dict, by: str = "") -> dict:
        src = self.source(source_id)
        v = self.clean_pull(d)
        if not v["source_name"]:
            v["source_name"] = _NAME.sub("-", f"mcp-{src['name']}")[:64].strip("-")
        v.update(source_id=source_id, team_id=src.get("team_id"), created_by=(by or "").lower(), created_at=_now())
        cols = list(v)
        pid = self.kb._insert(f"INSERT INTO mcp_pulls ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(v[c] for c in cols))
        return self.pull(pid)

    def update_pull(self, pid: int, d: dict) -> dict:
        v = self.clean_pull(d, partial=True)
        if v:
            if v.get("enabled"):
                v["fails"] = 0
            self.kb._exec(f"UPDATE mcp_pulls SET {', '.join(f'{k}=?' for k in v)} WHERE id=?", tuple(v.values()) + (pid,))
        return self.pull(pid)

    def set_pull(self, pid: int, **fields) -> None:
        self.kb._exec(f"UPDATE mcp_pulls SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", tuple(fields.values()) + (pid,))

    def delete_pull(self, pid: int) -> None:
        self.kb._exec("DELETE FROM mcp_tickets WHERE pull_id=?", (pid,))
        self.kb._exec("DELETE FROM mcp_pulls WHERE id=?", (pid,))


_LOCK = threading.Lock()


def store(svc) -> McpSources:
    s = getattr(svc, "mcp_sources", None)
    if s is None:
        with _LOCK:
            s = getattr(svc, "mcp_sources", None)
            if s is None:
                s = McpSources(svc.kb)
                svc.mcp_sources = s
    return s


def client_for(src: dict, timeout: Optional[float] = None) -> Client:
    """A Client for a source row read with secret=True."""
    h = parse_headers(src.get("headers") or "")
    sec = src.get("secret") or ""
    if src.get("auth") == "bearer" and sec:
        h["Authorization"] = f"Bearer {sec}"
    elif src.get("auth") == "header" and sec and src.get("header_name"):
        h[src["header_name"]] = sec
    return Client(src["url"], src.get("transport") or "http", h, timeout if timeout is not None else float(src.get("timeout_s") or 15),
                  int(src.get("max_kb") or 1024) * 1024, bool(src.get("verify_tls", True)))


def _err(e: Exception) -> tuple:
    code = getattr(e, "code", None) if isinstance(e, McpClientError) else None
    if code is None:
        from .connectors import BlockedURL
        code = "blocked_url" if isinstance(e, BlockedURL) else ("bad_request" if isinstance(e, ValueError) else "error")
    return code, f"{code}: {str(e)[:250]}"


def discover(svc, sid: int) -> dict:
    """List the tools and resources of a source; kept with the source for the pull form and the assistant."""
    st = store(svc)
    src = st.source(sid, secret=True)
    try:
        with client_for(src) as c:
            tools = c.list_tools()
            resources = c.list_resources()
        keep = {"tools": [{"name": t.get("name"), "description": str(t.get("description") or "")[:300], "inputSchema": t.get("inputSchema") or {},
                           "annotations": t.get("annotations") or {}} for t in tools[:200]],
                "resources": [{"uri": r.get("uri"), "name": r.get("name"), "mimeType": r.get("mimeType")} for r in resources[:200]]}
        st.set_source(sid, tools_cache=json.dumps(keep, ensure_ascii=False)[:200000], discovered_at=_now(), last_ok=_now(), last_err="")
        return {"ok": True, **keep}
    except Exception as e:  # noqa: BLE001
        code, msg = _err(e)
        st.set_source(sid, last_err=msg)
        return {"ok": False, "error": code, "message": msg}


# ---------------------------------------------------------------- ingestion
def _flat(rec: dict, limit: int = 40) -> dict:
    out = {}
    for k, v in rec.items():
        if len(out) >= limit:
            break
        if isinstance(v, (str, int, float, bool)):
            out[str(k)[:64]] = v[:1000] if isinstance(v, str) else v
    return out


def _ingest_logs(svc, p: dict, recs: list, seen: deque) -> tuple:
    from .sources import row_from_record, to_jsonl
    m = p.get("mapping") or {}
    rows, dup = [], 0
    for rec in recs:
        h = hashlib.sha1(json.dumps(rec, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if h in seen:
            dup += 1
            continue
        seen.append(h)
        base = row_from_record(rec, str(m.get("ts") or ""))
        for f in ("level", "host", "service", "msg"):
            if m.get(f):
                try:
                    v = dig(rec, str(m[f]))
                    if v is not None:
                        base[f] = str(v)
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
        rows.append({**_flat(rec), **base})
    if not rows:
        return 0, dup
    name = p.get("source_name") or f"mcp-{p['id']}"
    n = svc.live.ingest(f"{name}.jsonl", to_jsonl(rows, p.get("env") or ""), agent=name, env=p.get("env") or "", team_id=p.get("team_id"))
    return int(n or 0), dup + max(0, len(rows) - int(n or 0))


HOST_KEYS = ("hostname", "host", "fqdn", "name", "server", "node", "ci_name")


def _ingest_inventory(svc, p: dict, recs: list) -> tuple:
    from .inventory import FIELDS
    m = p.get("mapping") or {}
    team_hosts = None
    if p.get("team_id") is not None:
        try:
            team_hosts = set(svc.teams.get(int(p["team_id"]))["matched"])
        except (KeyError, TypeError, ValueError):
            team_hosts = set()
    n = skipped = 0
    for rec in recs:
        def val(f):
            if m.get(f):
                try:
                    return dig(rec, str(m[f]))
                except (KeyError, IndexError, TypeError, ValueError):
                    return None
            return rec.get(f)
        h = val("hostname") if m.get("hostname") else next((rec.get(k) for k in HOST_KEYS if rec.get(k)), None)
        h = str(h or "").strip().lower()
        if not h or (team_hosts is not None and h not in team_hosts):
            skipped += 1
            continue
        cur = svc.inventory.get(h) or {}
        new = {f: cur.get(f, "") for f in FIELDS}
        for f in FIELDS:
            v = val(f)
            if v not in (None, "") and f != "hostname":
                new[f] = ",".join(map(str, v)) if isinstance(v, list) else str(v)[:300]
        new["hostname"] = h
        try:
            svc.inventory.upsert(new)
            n += 1
        except ValueError:
            skipped += 1
    return n, skipped


def _ingest_tickets(svc, p: dict, recs: list) -> tuple:
    from .itsm import fetch_generic_from_doc
    m = {k: str(v) for k, v in (p.get("mapping") or {}).items() if k in ("id", "title", "description", "priority", "status", "created", "service", "assignee")}
    tickets = fetch_generic_from_doc(recs, m)
    st = store(svc)
    st.kb._exec("DELETE FROM mcp_tickets WHERE pull_id=?", (p["id"],))
    now = _now()
    n = 0
    for t, rec in zip(tickets, recs):
        if not (t.id or t.title):
            continue
        url = str(rec.get(str((p.get("mapping") or {}).get("url") or "url")) or "")
        d = {"id": t.id, "title": t.title, "description": t.description[:2000], "priority": t.priority, "status": t.status,
             "created": t.created.isoformat() if t.created else "", "service": t.service, "assignee": t.assignee,
             "url": url if url.startswith(("http://", "https://")) else ""}
        st.kb._exec("INSERT INTO mcp_tickets (pull_id, team_id, data, fetched_at) VALUES (?,?,?,?)", (p["id"], p.get("team_id"), json.dumps(d, ensure_ascii=False), now))
        n += 1
    return n, len(recs) - n


def tickets_for(svc, acc) -> list:
    """Tickets pulled from MCP sources that this account may see (installation-wide pulls: everyone with page.itsm; a team's:
    that team). Each carries `own` (a team's ticket stays visible to the team even without correlated evidence)."""
    from .itsm import Ticket
    from .itsm import parse_timestamp as parse_ts
    try:
        rows = store(svc).kb._exec("SELECT t.data, t.team_id FROM mcp_tickets t JOIN mcp_pulls p ON p.id = t.pull_id WHERE p.enabled=1")
    except Exception:  # noqa: BLE001
        return []
    mine = None if acc.wide else set(acc.team_ids("page.itsm"))
    out = []
    for r in rows:
        tid = r.get("team_id")
        if mine is not None and tid is not None and tid not in mine:
            continue
        d = _json(r["data"]) or {}
        t = Ticket(str(d.get("id") or ""), str(d.get("title") or ""), str(d.get("description") or ""), str(d.get("priority") or ""), str(d.get("status") or ""),
                   parse_ts(d["created"]) if d.get("created") else None, str(d.get("service") or ""), str(d.get("assignee") or ""), str(d.get("url") or ""))
        t.own = tid is not None and mine is not None
        out.append(t)
    return out


# ---------------------------------------------------------------- runner
class PullRunner:
    """Runs due pulls one after another (each bounded by its source's time and size limits); failures back off up to 8x."""

    def __init__(self, svc):
        self.svc = svc
        self.stop = threading.Event()
        self.due: dict = {}
        self.seen: dict = {}
        self.running: set = set()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="mcp-pulls")

    def start(self) -> "PullRunner":
        self.thread.start()
        return self

    def _loop(self) -> None:
        while not self.stop.is_set():
            try:
                st = store(self.svc)
                srcs = {s["id"]: s for s in st.sources()}
                now = time.time()
                for p in st.pulls():
                    s = srcs.get(p["source_id"])
                    if not p["enabled"] or not s or not s["enabled"] or now < self.due.get(p["id"], 0):
                        continue
                    self.due[p["id"]] = now + int(p["interval_s"] or 300) * min(2 ** int(p.get("fails") or 0), 8)
                    self.run(p["id"])
            except Exception:  # noqa: BLE001 - the loop never dies
                pass
            self.stop.wait(2.0)

    def run(self, pid: int) -> dict:
        with self.lock:
            if pid in self.running:
                return {"ok": False, "status": "running"}
            self.running.add(pid)
        try:
            return run_pull(self.svc, pid, self.seen.setdefault(pid, deque(maxlen=20000)))
        finally:
            with self.lock:
                self.running.discard(pid)


def run_pull(svc, pid: int, seen: Optional[deque] = None) -> dict:
    """One run of a pull: call the tool, ingest, record the state and an audit row."""
    from .mcpkeys import store as keys
    st = store(svc)
    p = st.pull(pid)
    src = st.source(p["source_id"], secret=True)
    t0 = time.perf_counter()
    st.set_pull(pid, status="running", last_run=_now())
    label = f"{src['name']}/{p['tool']}"
    try:
        with client_for(src) as c:
            res = c.call_tool(p["tool"], p.get("arguments") or {})
        recs = records(payload(res), str((p.get("mapping") or {}).get("items") or ""))
        over = max(0, len(recs) - int(p["max_rows"] or 1000))
        recs = recs[: int(p["max_rows"] or 1000)]
        if p["target"] == "inventory":
            n, skipped = _ingest_inventory(svc, p, recs)
        elif p["target"] == "itsm":
            n, skipped = _ingest_tickets(svc, p, recs)
        else:
            n, skipped = _ingest_logs(svc, p, recs, seen if seen is not None else deque(maxlen=20000))
        ms = int((time.perf_counter() - t0) * 1000)
        st.set_pull(pid, status="ok", last_ok=_now(), last_err="", last_rows=n, last_skipped=skipped + over, total_rows=int(p.get("total_rows") or 0) + n,
                    runs=int(p.get("runs") or 0) + 1, fails=0, last_ms=ms)
        st.set_source(src["id"], last_ok=_now(), last_err="")
        keys(svc).audit(None, label, (p.get("arguments") or {}).keys(), True, "", ms, n, direction="out", principal=f"pull:{p['name']}",
                        scope=f"team:{p['team_id']}" if p.get("team_id") is not None else "all", team_id=p.get("team_id"))
        return {"ok": True, "status": "ok", "rows": n, "skipped": skipped + over, "ms": ms}
    except Exception as e:  # noqa: BLE001
        code, msg = _err(e)
        ms = int((time.perf_counter() - t0) * 1000)
        st.set_pull(pid, status="error", last_err=msg, runs=int(p.get("runs") or 0) + 1, fails=int(p.get("fails") or 0) + 1, last_ms=ms)
        keys(svc).audit(None, label, (p.get("arguments") or {}).keys(), False, code, ms, 0, direction="out", principal=f"pull:{p['name']}",
                        scope=f"team:{p['team_id']}" if p.get("team_id") is not None else "all", team_id=p.get("team_id"))
        return {"ok": False, "status": "error", "error": code, "message": msg, "ms": ms}


def runner(svc) -> PullRunner:
    r = getattr(svc, "mcp_runner", None)
    if r is None:
        with _LOCK:
            r = getattr(svc, "mcp_runner", None)
            if r is None:
                r = PullRunner(svc)
                svc.mcp_runner = r
    return r


# ---------------------------------------------------------------- the assistant
def visible_source(acc, team_id, perm: str) -> bool:
    return acc.wide or team_id is None or team_id in acc.team_ids(perm)


def assist_context(svc, acc, question: str, budget_s: float = 8.0, max_items: int = 3) -> list:
    """Call the external tools allowed for the assistant (read-only) with the masked question; -> [{id: M1, title, text}]."""
    from .mcpkeys import store as keys
    from .privacy import scrub
    q = scrub((question or "").strip())[:500]
    if not q:
        return []
    out: list = []
    end = time.monotonic() + budget_s
    for src in store(svc).sources(secret=True):
        if not src["enabled"] or not src.get("assist_tools") or not visible_source(acc, src.get("team_id"), "page.assist"):
            continue
        meta = {t.get("name"): t for t in (src.get("tools") or {}).get("tools", [])}
        names = [n for n in (x.strip() for x in src["assist_tools"].split(",")) if n in meta]
        names = [n for n in names if (meta[n].get("annotations") or {}).get("readOnlyHint") is not False
                 and (meta[n].get("annotations") or {}).get("destructiveHint") is not True]
        left = end - time.monotonic()
        if not names or left < 1:
            continue
        try:
            with client_for(src, timeout=min(float(src.get("timeout_s") or 15), left)) as c:
                for n in names:
                    props = ((meta[n].get("inputSchema") or {}).get("properties") or {})
                    k = next((x for x in QUERY_KEYS if x in props), None)
                    t0 = time.perf_counter()
                    try:
                        res = c.call_tool(n, {k: q} if k else {})
                        body = payload(res)
                        text = scrub(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str))[:1500]
                        out.append({"id": f"M{len(out) + 1}", "title": f"{src['name']}/{n}", "text": text})
                        keys(svc).audit(None, f"{src['name']}/{n}", [k] if k else [], True, "", int((time.perf_counter() - t0) * 1000), 1,
                                        direction="out", principal=acc.email, scope="assist", team_id=src.get("team_id"))
                    except McpTimeout:
                        raise
                    except Exception as e:  # noqa: BLE001
                        keys(svc).audit(None, f"{src['name']}/{n}", [k] if k else [], False, _err(e)[0], int((time.perf_counter() - t0) * 1000), 0,
                                        direction="out", principal=acc.email, scope="assist", team_id=src.get("team_id"))
                    if len(out) >= max_items:
                        return out
        except Exception:  # noqa: BLE001 - the chat never breaks on an external server
            continue
    return out
