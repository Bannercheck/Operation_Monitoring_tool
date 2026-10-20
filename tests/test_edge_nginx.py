"""The nginx edge (docker/nginx): every agent path goes to the receiver, the client address cannot be forged past it, the load
balancer's health check answers on both ports, and (with nginx installed) the filled-in template passes `nginx -t`; the edge
certificate script issues a chain the CA verifies."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "docker" / "nginx" / "watchover.conf.template"
ENTRY = ROOT / "docker" / "nginx" / "40-watchover-edge.sh"


def locations(txt: str) -> dict:
    out, cur, depth = {}, None, 0
    for line in txt.splitlines():
        s = line.split("#", 1)[0].strip()
        m = re.match(r"location\s+(.+?)\s*\{", s)
        if m and depth == 1:                                  # the file is included inside http {}: server blocks are depth 1
            cur = m.group(1); out[cur] = out.get(cur, "") + " " + s[m.end():]
        elif cur is not None:
            out[cur] += " " + s
        depth += s.count("{") - s.count("}")
        if cur is not None and depth <= 1:
            cur = None
    assert out, "no location parsed"
    return out


def test_agent_paths_go_to_the_receiver_and_the_rest_to_the_ui():
    loc = locations(TPL.read_text(encoding="utf-8"))
    recv = {k for k, v in loc.items() if "$wo_receiver" in v}
    for path in ("~ ^/(ingest|enroll)", "= /agent.py", "^~ /agent/", "= /rum", "= /rum.js", "= /mobile", "^~ /v1/"):
        assert path in recv, path
    assert "$wo_mcp" in loc["~ ^/mcp(/|$)"] and "$wo_ui" in loc["/"] and "/api/health" in loc["= /healthz"]


def test_no_location_drops_the_forwarded_headers():
    """nginx inherits proxy_set_header only into a level that sets none: a location with its own would pass the client's
    X-Forwarded-For through unchanged (found in review: the UI location did)."""
    txt = TPL.read_text(encoding="utf-8")
    for k, v in locations(txt).items():
        assert "proxy_set_header" not in v, k
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in txt and "$proxy_add_x_forwarded_for" not in txt


def test_trusted_proxies_are_validated(tmp_path):
    script = ENTRY.read_text(encoding="utf-8").replace("/etc/nginx/conf.d", str(tmp_path))
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 10.89.0.1\nnameserver fe80::1%eth0\n")
    script = script.replace("/etc/resolv.conf", str(resolv))
    run = lambda trusted: subprocess.run(["sh", "-c", script], env={"PATH": "/usr/bin:/bin", "WATCHOVER_EDGE_TRUSTED": trusted},   # noqa: E731
                                         capture_output=True, text=True, check=True)
    run("10.0.0.5/32 bad;include_x 2001:db8::/32 / (./watchover.sh edge lb) 10.0.0 ::")
    inc = (tmp_path / "realip.inc").read_text()
    assert "set_real_ip_from 10.0.0.5/32;" in inc and "set_real_ip_from 2001:db8::/32;" in inc and "bad" not in inc and "real_ip_recursive on;" in inc
    assert inc.count("set_real_ip_from") == 3 and "set_real_ip_from /;" not in inc and "edge" not in inc   # "/" stopped nginx (RHEL VM)
    assert (tmp_path / "resolver.inc").read_text().startswith("resolver 10.89.0.1  valid=10s")     # Podman's gateway DNS; link-local skipped
    run("")
    assert (tmp_path / "realip.inc").read_text() == ""                                            # no load balancer: nothing believed


def _incs(tmp_path, tls_min=""):
    """The .inc files the container's start script writes, written into tmp_path."""
    script = ENTRY.read_text(encoding="utf-8").replace("/etc/nginx/conf.d", str(tmp_path))
    resolv = tmp_path / "resolv.conf"; resolv.write_text("nameserver 127.0.0.11\n")
    env = {"PATH": "/usr/bin:/bin", "WATCHOVER_EDGE_TLS_MIN": tls_min}
    return subprocess.run(["sh", "-c", script.replace("/etc/resolv.conf", str(resolv))], env=env, capture_output=True, text=True, check=True)


def test_tls_versions_from_the_environment(tmp_path):
    for v, want in (("", "ssl_protocols TLSv1.2 TLSv1.3;"), ("1.2", "ssl_protocols TLSv1.2 TLSv1.3;"), ("1.3", "ssl_protocols TLSv1.3;"),
                    ("1.0;include /etc/passwd", "ssl_protocols TLSv1.2 TLSv1.3;")):
        r = _incs(tmp_path, v)
        assert (tmp_path / "tls.inc").read_text().strip() == want, v
    assert "using 1.2" in r.stderr
    t = TPL.read_text(encoding="utf-8")
    assert "include /etc/nginx/conf.d/tls.inc;" in t and not re.search(r"^\s*ssl_protocols ", t, re.M)   # one place decides the versions
    ciphers = re.search(r"ssl_ciphers (\S+);", t).group(1).split(":")
    assert ciphers and all(c.startswith("ECDHE-") and ("GCM" in c or "CHACHA20" in c) for c in ciphers)   # forward-secret AEAD only


def _edge_conf(tmp_path, tls_min=""):
    crt = tmp_path / "certs"
    subprocess.run(["sh", str(ROOT / "scripts" / "edge-cert.sh"), str(crt), "watchover.local,10.0.0.7"], check=True, capture_output=True)
    t = TPL.read_text(encoding="utf-8")
    for k, v in {"WATCHOVER_EDGE_HTTP_PORT": "18080", "WATCHOVER_EDGE_HTTPS_PORT": "18443", "WATCHOVER_EDGE_SERVER_NAMES": "watchover.local 10.0.0.7",
                 "WATCHOVER_EDGE_CERT": str(crt / "watchover.crt"), "WATCHOVER_EDGE_KEY": str(crt / "watchover.key")}.items():
        t = t.replace("${" + k + "}", v)
    t = t.replace("/etc/nginx/conf.d/", f"{tmp_path}/")
    out = subprocess.run(["nginx", "-v"], capture_output=True, text=True).stderr
    if tuple(int(x) for x in re.findall(r"nginx/(\d+)\.(\d+)", out)[0]) < (1, 25):
        t = t.replace("    http2 on;\n", "")                    # `http2 on` is 1.25.1+ (the image is 1.28)
    _incs(tmp_path, tls_min)
    (tmp_path / "w.conf").write_text(t)
    (tmp_path / "nginx.conf").write_text(f"pid {tmp_path}/n.pid; error_log {tmp_path}/e.log; events {{}}\n"
                                         f"http {{ access_log off; client_body_temp_path {tmp_path}; proxy_temp_path {tmp_path}; include {tmp_path}/w.conf; }}\n")
    return tmp_path / "nginx.conf"


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx not installed")
def test_filled_template_passes_nginx_t(tmp_path):
    r = subprocess.run(["nginx", "-t", "-c", str(_edge_conf(tmp_path))], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _accepts(port: int, *flags: str) -> bool:
    r = subprocess.run(["openssl", "s_client", "-connect", f"127.0.0.1:{port}", "-servername", "watchover.local", *flags],
                       input="", capture_output=True, text=True, timeout=15)
    return bool(re.search(r"^New, TLSv1\.\d", r.stdout, re.M))


@pytest.mark.skipif(not (shutil.which("nginx") and shutil.which("openssl")), reason="nginx / openssl not installed")
@pytest.mark.parametrize("tls_min", ["1.2", "1.3"])
def test_edge_speaks_tls13_and_never_old_protocols(tmp_path, tls_min):
    """A running edge: TLS 1.3 always; TLS 1.2 unless 1.3 only; TLS 1.0 / 1.1 and a CBC suite never."""
    import time
    conf = _edge_conf(tmp_path, tls_min)
    p = subprocess.Popen(["nginx", "-c", str(conf), "-g", "daemon off;"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if (tmp_path / "n.pid").exists():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        assert _accepts(18443, "-tls1_3")
        assert _accepts(18443, "-tls1_2") is (tls_min == "1.2")
        weak = ("-cipher", "DEFAULT@SECLEVEL=0")                  # the client would try them: the server is what refuses
        assert not _accepts(18443, "-tls1_1", *weak) and not _accepts(18443, "-tls1", *weak)
        assert not _accepts(18443, "-tls1_2", "-cipher", "ECDHE-RSA-AES128-SHA256:AES128-GCM-SHA256")   # CBC and RSA key exchange refused
    finally:
        p.terminate(); p.wait(timeout=10)


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_edge_certificate_script(tmp_path):
    d = tmp_path / "certs"
    run = lambda *a: subprocess.run(["sh", str(ROOT / "scripts" / "edge-cert.sh"), str(d), *a], capture_output=True, text=True)   # noqa: E731
    r = run("watchover.local, 192.168.1.107")
    assert r.returncode == 0 and "issued" in r.stdout, r.stderr
    v = subprocess.run(["openssl", "verify", "-CAfile", str(d / "watchover-root.crt"), str(d / "watchover.crt")], capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr
    san = subprocess.run(["openssl", "x509", "-in", str(d / "watchover.crt"), "-noout", "-text"], capture_output=True, text=True).stdout
    assert "DNS:watchover.local" in san and "IP Address:192.168.1.107" in san and "TLS Web Server Authentication" in san
    assert oct((d / "watchover.key").stat().st_mode)[-3:] == "600" and oct((d / ".ca" / "watchover-ca.key").stat().st_mode)[-3:] == "600"
    root = (d / "watchover-root.crt").read_bytes()
    assert "valid" in run("watchover.local,192.168.1.107").stdout                  # same names, far from expiry: kept
    assert "issued" in run("watchover.local,10.0.0.9").stdout and (d / "watchover-root.crt").read_bytes() == root   # new names, same CA
    assert run("a;b").returncode == 2
    (d / ".ca" / "issued").unlink()                                                # a company certificate (no marker): never touched
    assert "company certificate" in run("other.example").stdout


def test_forwarded_headers_believed_only_behind_the_edge(monkeypatch):
    """Without an edge the dashboard port is reached directly: a client's own X-Forwarded-For must not be believed. The edge
    override (only the edge reaches the port then) and the OpenShift chart (behind the router) turn it on."""
    import uvicorn
    from watchover.api import __main__ as m
    got = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: got.update(k))
    monkeypatch.delenv("WATCHOVER_FORWARDED_ALLOW", raising=False)
    m.main(["--port", "1"])
    assert got["forwarded_allow_ips"] == "127.0.0.1" and got["proxy_headers"] is True
    monkeypatch.setenv("WATCHOVER_FORWARDED_ALLOW", "*")
    m.main(["--port", "1"])
    assert got["forwarded_allow_ips"] == "*"
    edge = (ROOT / "docker-compose.edge.yml").read_text()
    assert 'WATCHOVER_FORWARDED_ALLOW: "*"' in edge and "WATCHOVER_FORWARDED_ALLOW" not in (ROOT / "docker-compose.yml").read_text()
