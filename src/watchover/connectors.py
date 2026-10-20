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


# cloud metadata services that are not link-local (Alibaba 100.100.100.200, AWS over IPv6) and the shared-address space a
# carrier-grade NAT uses (100.64.0.0/10: neither "private" nor "link-local" to Python)
_METADATA = (ipaddress.ip_network("100.100.100.200/32"), ipaddress.ip_network("fd00:ec2::254/128"))
_SHARED = ipaddress.ip_network("100.64.0.0/10")
MAX_RESPONSE = 256 * 1024 * 1024


def guard_url(url: str, allow_private: bool = False) -> list:
    """Reject SSRF-prone URLs and return the addresses checked. Always: only http/https (no file:/gopher:/ftp:), and never
    link-local (169.254.0.0/16 cloud metadata), other metadata addresses, multicast, reserved or unspecified addresses. When
    `allow_private` is False (the default, for ad-hoc/user-supplied URLs) loopback, RFC1918 private and 100.64.0.0/10 are blocked
    too; admin-configured data sources that legitimately live on the company network (internal Elasticsearch, Grafana, ITSM) pass
    `allow_private=True`. Every resolved address of the host is checked, so a name pointing at an internal IP is caught; open_url
    then connects to that checked address (no second lookup: DNS rebinding) and follows no redirect."""
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
    out = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified or any(ip in n for n in _METADATA):
            raise BlockedURL(f"host resolves to a blocked address: {ip}")
        if not allow_private and (ip.is_loopback or ip.is_private or (ip.version == 4 and ip in _SHARED)):
            raise BlockedURL(f"host resolves to a private/loopback address: {ip} (not allowed for ad-hoc fetch)")
        out.append(str(ip))
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would leave the checked address (and carry the caller's credentials along): refused."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BlockedURL(f"the server answered {code} redirect to {str(newurl)[:200]}: not followed, use the final address")


def _pinned(ip: str, ctx):
    import http.client

    class PinHTTP(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection((ip, self.port), self.timeout, self.source_address)

    class PinHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((ip, self.port), self.timeout, self.source_address)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)   # SNI and the certificate check use the name

    class H(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(PinHTTP, req)

    class HS(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(PinHTTPS, req, context=ctx or _CTX)
    return [H(), HS()]


def open_url(req: urllib.request.Request, timeout: float, context=None, allow_private: bool = False):
    """urlopen for a URL someone typed or configured: guard_url, no redirects, and the connection goes to the address that was
    checked. Through a configured outbound proxy (HTTPS_PROXY) the proxy resolves the name; the check and the redirect rule stay."""
    ips = guard_url(req.full_url, allow_private=allow_private)
    host = urlparse(req.full_url).hostname or ""
    proxies = urllib.request.getproxies()
    via_proxy = bool(proxies.get(urlparse(req.full_url).scheme)) and not urllib.request.proxy_bypass(host)
    handlers: list = [_NoRedirect()]
    if via_proxy:
        handlers += [urllib.request.ProxyHandler(), urllib.request.HTTPSHandler(context=context or _CTX)]
    else:
        handlers += [urllib.request.ProxyHandler({})] + _pinned(ips[0], context)
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def read_capped(r, limit: int = MAX_RESPONSE) -> bytes:
    data = r.read(limit + 1)
    if len(data) > limit:
        raise BlockedURL(f"response larger than {limit >> 20} MB")
    return data


def _request(url: str, method: str = "GET", headers: dict | None = None, body: bytes | None = None,
             timeout: int = 60, allow_private: bool = False) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=headers or {})
    with open_url(req, timeout, _CTX, allow_private) as r:
        return read_capped(r), {k.lower(): v for k, v in r.headers.items()}


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
