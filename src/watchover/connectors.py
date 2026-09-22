"""Data-source connectors: pull a dataset from an HTTP API or from an MCP server tool. Standard library only.

    fetch_http(url, method, headers, body, json_path)  -> (name, bytes)
    fetch_mcp(url, tool, arguments, headers)          -> (name, bytes)   # MCP streamable-HTTP JSON-RPC client
"""

from __future__ import annotations

import ipaddress
import json
import socket
import ssl
import urllib.request
from typing import Any
from urllib.parse import urlparse

MCP_PROTOCOL = "2025-03-26"
_CTX = ssl.create_default_context()


class BlockedURL(ValueError):
    """A requested URL is not allowed (bad scheme, or resolves to a private / loopback / link-local address)."""


def guard_url(url: str, allow_private: bool = False) -> None:
    """Reject SSRF-prone URLs. Always: only http/https (no file:/gopher:/ftp:), and never link-local
    (169.254.0.0/16 cloud metadata), multicast, reserved or unspecified addresses. When `allow_private` is False
    (the default, for ad-hoc/user-supplied URLs) loopback and RFC1918 private ranges are blocked too; admin-configured
    data sources that legitimately live on the company network (internal Elasticsearch, Grafana, ITSM) pass
    `allow_private=True`. Every resolved address of the host is checked, so a name pointing at an internal IP is caught."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise BlockedURL(f"scheme not allowed: {p.scheme or '(none)'} (only http/https)")
    host = p.hostname
    if not host:
        raise BlockedURL("no host in URL")
    try:
        infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise BlockedURL(f"cannot resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise BlockedURL(f"host resolves to a blocked address: {ip}")
        if not allow_private and (ip.is_loopback or ip.is_private):
            raise BlockedURL(f"host resolves to a private/loopback address: {ip} (not allowed for ad-hoc fetch)")


def _request(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
             timeout: int = 60, allow_private: bool = False) -> tuple[bytes, dict]:
    guard_url(url, allow_private=allow_private)
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def parse_headers(text: str) -> dict:
    """'Authorization: Bearer x\\nX-Api-Key: y' -> dict."""
    out = {}
    for ln in (text or "").splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def dig(obj: Any, path: str) -> Any:
    """'data.alerts' or 'items[0].rows' style path into a JSON document."""
    for part in [p for p in path.replace("[", ".").replace("]", "").split(".") if p]:
        obj = obj[int(part)] if isinstance(obj, list) else obj[part]
    return obj


def fetch_http(url: str, method: str = "GET", headers: dict | None = None, body: str | None = None,
               json_path: str = "", allow_private: bool = False) -> tuple[str, bytes]:
    """Return (suggested_file_name, raw_bytes). JSON responses can be narrowed with json_path."""
    data, hdrs = _request(url, method, headers, body.encode() if body else None, allow_private=allow_private)
    ctype = hdrs.get("content-type", "")
    name = url.rstrip("/").split("/")[-1].split("?")[0] or "api"
    if json_path or "json" in ctype:
        try:
            doc = json.loads(data)
            if json_path:
                doc = dig(doc, json_path)
            return (name if name.endswith(".json") else name + ".json"), json.dumps(doc).encode()
        except (ValueError, KeyError, IndexError, TypeError):
            pass
    if "zip" in ctype and not name.endswith(".zip"):
        name += ".zip"
    elif "csv" in ctype and not name.endswith(".csv"):
        name += ".csv"
    return name, data


class McpClient:
    """Minimal MCP client over streamable HTTP (JSON-RPC 2.0). Enough to list tools and call one."""

    def __init__(self, url: str, headers: dict | None = None, allow_private: bool = False):
        self.url = url
        self.allow_private = allow_private
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
        self.session_id: str | None = None
        self._id = 0

    def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> Any:
        msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        hdrs = dict(self.headers)
        if self.session_id:
            hdrs["Mcp-Session-Id"] = self.session_id
        data, resp_hdrs = _request(self.url, "POST", hdrs, json.dumps(msg).encode(), allow_private=self.allow_private)
        if resp_hdrs.get("mcp-session-id"):
            self.session_id = resp_hdrs["mcp-session-id"]
        if notify or not data:
            return None
        text = data.decode("utf-8", errors="replace")
        if "text/event-stream" in resp_hdrs.get("content-type", "") or text.lstrip().startswith(("event:", "data:")):
            payloads = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
            for pl in reversed(payloads):
                try:
                    obj = json.loads(pl)
                    if obj.get("id") == msg.get("id"):
                        return self._result(obj)
                except json.JSONDecodeError:
                    continue
            raise RuntimeError("no JSON-RPC response in event stream")
        return self._result(json.loads(text))

    @staticmethod
    def _result(obj: dict) -> Any:
        if "error" in obj:
            raise RuntimeError(f"MCP error {obj['error'].get('code')}: {obj['error'].get('message')}")
        return obj.get("result")

    def initialize(self) -> dict:
        res = self._rpc("initialize", {"protocolVersion": MCP_PROTOCOL, "capabilities": {},
                                       "clientInfo": {"name": "watchover", "version": "0.2"}})
        self._rpc("notifications/initialized", notify=True)
        return res or {}

    def list_tools(self) -> list[dict]:
        return (self._rpc("tools/list") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        res = self._rpc("tools/call", {"name": name, "arguments": arguments or {}}) or {}
        if res.get("isError"):
            raise RuntimeError("tool returned an error: " + " ".join(c.get("text", "") for c in res.get("content", [])))
        if "structuredContent" in res:
            return json.dumps(res["structuredContent"])
        return "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")


def fetch_mcp(url: str, tool: str, arguments: dict | None = None, headers: dict | None = None, allow_private: bool = False) -> tuple[str, bytes]:
    """Connect to an MCP server, call `tool`, return its text payload as a dataset file."""
    client = McpClient(url, headers, allow_private=allow_private)
    client.initialize()
    text = client.call_tool(tool, arguments)
    stripped = text.lstrip()
    name = f"mcp_{tool}" + (".json" if stripped.startswith(("{", "[")) else ".log")
    return name, text.encode()


def mcp_tools(url: str, headers: dict | None = None, allow_private: bool = False) -> list[dict]:
    client = McpClient(url, headers, allow_private=allow_private)
    client.initialize()
    return client.list_tools()
