"""Untrusted-input findings of the October 2026 security review (v1.60.2): typed patterns run with a time limit (ReDoS), log-search
wildcards are collapsed and capped, outbound fetches follow no redirect and connect to the address that was checked (SSRF), and
compressed uploads are inflated within a limit."""
import http.server
import threading
import time
import urllib.request

import pytest

from watchover import connectors, saferx


@pytest.mark.parametrize("pat,text", [(r"(a|aa)+$", "a" * 5000 + "!")])
def test_a_catastrophic_pattern_is_cut_off(pat, text):
    rx = saferx.compile(pat)
    t0 = time.perf_counter()
    with pytest.raises(saferx.PatternTimeout):
        rx.search(text)
    assert time.perf_counter() - t0 < 1.0


def test_a_pattern_has_a_total_budget():
    rx = saferx.compile(r"\w+\d", budget_s=0.0)
    with pytest.raises(saferx.PatternTimeout):
        rx.search("abc1")
    ok = saferx.compile(r"order (\d+)", 2)
    assert ok.search("ORDER 42").group(1) == "42" and ok.groups == 1


def test_flow_suggest_refuses_a_slow_key_pattern():
    from watchover.bizflow import BizFlows
    from watchover.models import Observation
    from datetime import datetime, timezone
    obs = [Observation(timestamp=datetime.now(timezone.utc), message="a" * 4000 + "!", kind="log")]
    with pytest.raises(ValueError):
        BizFlows.suggest(r"((a|aa)+)$", obs)


def test_log_search_wildcards_are_collapsed_and_capped():
    from watchover import logquery as lq
    v = lq.Val("*?*?*?*?*?*?*?*?*?*?*?*?!", frozenset(range(24)), False)
    rx = lq._glob_rx(v, False, True)
    assert rx.pattern == r"\S{12,}!"
    t0 = time.perf_counter(); rx.search("550e8400-e29b-41d4-a716-446655440000 " * 50); assert time.perf_counter() - t0 < 0.5
    many = lq.Val("a*b*c*d*e*f*g*h", frozenset({1, 3, 5, 7, 9, 11, 13}), False)
    with pytest.raises(ValueError):
        lq._glob_rx(many, False, True)


class _Redirect(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302); self.send_header("Location", "http://169.254.169.254/latest/meta-data/"); self.end_headers()

    def log_message(self, *a):
        pass


def test_a_redirect_to_the_metadata_service_is_not_followed(monkeypatch):
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/r"
        with pytest.raises(connectors.BlockedURL, match="redirect"):
            connectors.open_url(urllib.request.Request(url, headers={"Metadata-Flavor": "Google"}), 5, allow_private=True)
        with pytest.raises(connectors.BlockedURL, match="private"):
            connectors.fetch_http(url)                                       # ad-hoc fetch: loopback refused outright
    finally:
        srv.shutdown()


def test_metadata_and_shared_ranges_are_blocked(monkeypatch):
    def fake(addr):
        return lambda host, port, **k: [(2, 1, 6, "", (addr, port))]
    monkeypatch.setattr(connectors.socket, "getaddrinfo", fake("100.100.100.200"))
    with pytest.raises(connectors.BlockedURL):
        connectors.guard_url("http://meta.example/", allow_private=True)    # Alibaba metadata: never
    monkeypatch.setattr(connectors.socket, "getaddrinfo", fake("100.64.1.2"))
    with pytest.raises(connectors.BlockedURL):
        connectors.guard_url("http://cgnat.example/")                        # shared space: not for ad-hoc fetches
    assert connectors.guard_url("http://cgnat.example/", allow_private=True) == ["100.64.1.2"]


def test_the_connection_goes_to_the_checked_address(monkeypatch):
    """DNS rebinding: the name is looked up once by guard_url; the socket connects to that address, not to a second answer."""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    seen = []
    monkeypatch.setattr(connectors.socket, "getaddrinfo", lambda host, port, **k: [(2, 1, 6, "", ("93.184.216.34", port))])

    def conn(addr, *a, **k):
        seen.append(addr); raise OSError("stop here")
    monkeypatch.setattr(connectors.socket, "create_connection", conn)
    with pytest.raises(Exception):
        connectors.open_url(urllib.request.Request("http://rebind.example/x"), 2)
    assert seen and seen[0][0] == "93.184.216.34"


def test_a_re_pointed_source_does_not_carry_its_secret(tmp_path, monkeypatch):
    """An operator who may edit a source (but never sees its masked secret) points it at their own server: the stored password /
    token / extra headers are dropped instead of being sent there."""
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    from watchover import sources
    from watchover.knowledge import Knowledge
    store = sources.SourceStore(Knowledge(str(tmp_path / "k.db")))
    src = store.add(sources.Source(None, "es", "elasticsearch", "http://es:9200", auth="basic", user="u", secret="s3cret", headers="X-Key: abc"))
    store.update(src.id, interval=60)
    assert store.get(src.id).secret == "s3cret"                               # other edits keep it
    store.update(src.id, url="https://attacker.example")
    got = store.get(src.id)
    assert got.secret == "" and got.headers == "" and got.url == "https://attacker.example"
    store.update(src.id, url="http://es2:9200", secret="new")                 # a new secret sent with the new address is kept
    assert store.get(src.id).secret == "new"


def test_a_gzip_bomb_is_refused_before_it_fills_memory(monkeypatch):
    import gzip as gz
    from watchover import loader
    monkeypatch.setattr(loader, "MAX_MEMBER_BYTES", 1 << 20)
    bomb = gz.compress(b"\0" * (8 << 20))                                     # 8 MB of zeros in a few KB
    with pytest.raises(loader.ArchiveTooBig):
        list(loader.iter_bytes("x.log.gz", bomb))
    assert list(loader.iter_bytes("ok.log.gz", gz.compress(b"line 1\nline 2\n"))) == [("ok.log", "line 1\nline 2\n")]
