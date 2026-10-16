"""One address for the company network (./watchover.sh edge public NAME, v1.58): links and sign-in callbacks use the public URL,
and a load balancer that passes connections through (every colleague arrives from its address) is never throttled as one client."""
from watchover.api.routers import auth as rauth


def test_public_url_from_the_edge_unless_set_in_system(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path))
    from watchover import settings
    monkeypatch.delenv("WATCHOVER_PUBLIC_URL", raising=False)
    assert settings.public_url() == ""
    monkeypatch.setenv("WATCHOVER_PUBLIC_URL", "https://watchover.sirket.test/")
    assert settings.public_url() == "https://watchover.sirket.test"
    settings.save({"public_host": "watchover2.sirket.test"})                       # System › Dış adres wins, https assumed
    assert settings.public_url() == "https://watchover2.sirket.test"


def test_load_balancer_address_is_not_throttled(monkeypatch):
    rauth._FAILS.clear()
    monkeypatch.setenv("WATCHOVER_EDGE_TRUSTED", "10.20.0.0/28 2001:db8::/32 bad")
    for _ in range(rauth.IP_FAILS + 5):
        assert not rauth._ip_blocked("10.20.0.10", failed=True)
    assert "10.20.0.10" not in rauth._FAILS                                         # the per-account lockout still applies
    for _ in range(rauth.IP_FAILS):
        rauth._ip_blocked("10.20.1.10", failed=True)
    assert rauth._ip_blocked("10.20.1.10")                                          # a real client next to it: throttled
    assert rauth._lb_without_client("2001:db8::5") and not rauth._lb_without_client("2001:db9::1")
    assert not rauth._lb_without_client("testclient") and not rauth._lb_without_client("-")
    monkeypatch.delenv("WATCHOVER_EDGE_TRUSTED")
    assert not rauth._lb_without_client("10.20.0.10")
    rauth._FAILS.clear()
