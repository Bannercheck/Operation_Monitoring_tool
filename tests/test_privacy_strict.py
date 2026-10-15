"""WP4b: strict scrubbing for the action agent (R12, C34): canaries, field-aware scrubbing, folding, parity fixture, CLI."""
import json
import time
from pathlib import Path

import pytest

from watchover import privacy as pv

FIX = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(pv, "_KEY", b"parity-fixture-key")      # the parity fixture was written with this pseudonym key


def test_every_canary_masked():
    text = (FIX / "privacy_canaries.txt").read_text(encoding="utf-8")
    assert pv.canary_hits(text)                                  # the corpus really carries them
    assert set(pv.canary_hits(text)) == set(pv.CANARIES)
    out = pv.scrub_strict(text)
    assert pv.canary_hits(out) == []
    for line in text.splitlines():                               # also line by line (evidence is stored per line)
        assert pv.canary_hits(pv.scrub_strict(line)) == [], line
    assert "WARN config dump continues" in out                   # a terminated PEM block does not swallow the rest


def test_pem_multiline_and_unterminated():
    t = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nxyz\n-----END RSA PRIVATE KEY-----\nb"
    assert pv.scrub_strict(t) == "a\n[PEM]\nb"
    u = "a\n-----BEGIN PRIVATE KEY-----\nMIIEow\nstill secret\nmore"
    assert pv.scrub_strict(u) == "a\n[PEM]"


def test_scrub_fields_keeps_numbers():
    out = pv.scrub_fields({"bytes_before": 5368709120, "big": 4111111111111111, "n": 1234567890123, "ok": True, "x": None, "f": 1.5})
    assert out == {"bytes_before": 5368709120, "big": 4111111111111111, "n": 1234567890123, "ok": True, "x": None, "f": 1.5}
    assert pv.scrub("\"bytes_before\": 5368709120") != "\"bytes_before\": 5368709120"   # the reason scrub_fields exists
    typed = pv.scrub_fields({"size": "4111111111111111", "pct": "93.4"}, {"size": "number", "pct": "number"})
    assert typed == {"size": "4111111111111111", "pct": "93.4"}
    assert pv.scrub_fields({"size": "abc PGPASSWORD=x"}, {"size": "number"})["size"] == "abc PGPASSWORD=[GIZLI]"


def test_scrub_fields_types_and_secret_keys():
    out = pv.scrub_fields({"unit": "orders.service", "state": "active", "note": "mail a@b.com", "password": "x", "api_key": "y",
                           "nested": {"cmd": "--password Canary-Flag-8", "list": ["PGPASSWORD=Canary-Env-22", 7]},
                           "home": "/home/ayse/x.log", "mount": "/var/log"},
                          {"unit": "ident", "state": "enum", "note": "text", "home": "path", "mount": "path"})
    assert out["unit"] == "orders.service" and out["state"] == "active" and out["mount"] == "/var/log"
    assert out["note"] == "mail [E-POSTA]" and out["password"] == "[GIZLI]" and out["api_key"] == "[GIZLI]"
    assert out["nested"]["list"][1] == 7 and pv.canary_hits(out) == []
    assert out["home"].startswith("/home/u_") and "ayse" not in out["home"]


def test_hex_hashes_and_own_ids_not_masked():
    for s in ("9f3c2a7b1e0d4c5b6a7f8e9d0c1b2a3f4e5d6c7b8a9f0e1d2c3b4a5f6e7d8c9b", "9F3C2A7B1E0D4C5B6A7F8E9D0C1B2A3F",
              "J-20261015-000913", "op_4b81c3d2e1f0", "u_ac870ed68cfab117", "linux.logs.compress_old", "/var/log/orders/orders.log"):
        assert pv.scrub_strict(s) == s


def test_entropy_rule():
    assert pv.scrub_strict("x aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW y") == "x [GIZLI] y"
    assert pv.scrub_strict("x Aaaaaaaaaaaaaaaaaaaaaaaaa1 y") == "x Aaaaaaaaaaaaaaaaaaaaaaaaa1 y"   # low entropy
    assert pv.scrub_strict("x abcdefghijklmnopqrstuvwxyz y") == "x abcdefghijklmnopqrstuvwxyz y"   # one case, no digit


def test_fold_nfkc_zero_width_confusables():
    f = pv.fold("pass​word ｉｇｎｏｒｅ")
    assert f.text == "password ignore" and "zero_width_bidi" in f.hits
    g = pv.fold("іgnоre previous")                               # Cyrillic і and о
    assert g.text == "ignore previous" and "confusable" in g.hits
    assert pv.fold("Disk kullanımı").text == "Disk kullanimi"   # skeleton for matching only
    assert pv.fold("Disk kullanımı", confusables=False).text == "Disk kullanımı"
    assert pv.scrub_strict("ｐａｓｓｗｏｒｄ=FullWidth1") == "password=***"


def test_scrub_unchanged():
    # scrub() keeps its exact behaviour for ingest (the existing privacy tests rely on it)
    assert pv.scrub("password=hunter2 x") == "password=*** x"
    assert pv.scrub("postgres://app:pw@db:5432/x") == "postgres://app:pw@db:5432/x"
    assert pv.scrub("PGPASSWORD=Canary-Env-22") == "PGPASSWORD=Canary-Env-22"


def test_parity_fixture():
    rows = [json.loads(line) for line in (FIX / "privacy_parity.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 25
    for r in rows:
        got = pv.scrub_strict(r["in"]) if r["kind"] == "text" else pv.scrub_fields(r["in"], r.get("schema"))
        assert got == r["out"], r["in"]


def test_throughput():
    line = ("2026-10-15T02:11:07Z INFO orders-api GET /api/orders/1234 200 12ms user=u_4b81c3d2 "
            "trace=9f3c2a7b1e0d4c5b path=/var/log/orders/orders.log bytes=5368709120\n")
    text = line * (1_000_000 // len(line))
    t0 = time.perf_counter()
    pv.scrub_strict(text)
    mb_per_min = len(text) / 1e6 / ((time.perf_counter() - t0) / 60)
    assert mb_per_min >= 20, mb_per_min


def test_check_cli(tmp_path, capsys):
    rc = pv.main(["check", str(FIX / "privacy_canaries.txt")])
    out = capsys.readouterr().out
    assert rc == 0 and "pem" in out and "url_userinfo" in out and "env_secret" in out and "canaries left after scrub_strict: 0" in out
    assert "Canary" not in out                                   # values are never printed
    clean = tmp_path / "clean.txt"
    clean.write_text("disk ok\n", encoding="utf-8")
    assert pv.main(["check", str(clean)]) == 0
