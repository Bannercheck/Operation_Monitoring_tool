"""WP8: the side-effect watch (docs/design/action-agent.md §4.7; C48, R7). (c)–(e) are never suppressed by expected_noise or
expected_noise_s; suppressed hits are still recorded for the reviewer."""
from datetime import datetime, timedelta, timezone

from watchover.act import watch as W


def test_error_rate_rule_a():
    assert W.evaluate({"per_min": 1.0, "count": 15}, {"per_min": 7.0, "count": 105})["hits"][0]["rule"] == "a"
    assert not W.evaluate({"per_min": 1.0, "count": 15}, {"per_min": 5.0, "count": 75})["side_effect"]      # < r_pre + 5
    assert not W.evaluate({"per_min": 0.0, "count": 0}, {"per_min": 6.0, "count": 12})["side_effect"]        # < 20 errors
    assert not W.evaluate({"per_min": 10.0, "count": 150}, {"per_min": 19.0, "count": 285})["side_effect"]   # < 2·r_pre


def test_new_templates_rule_b_and_expected_noise_suppression_recorded():
    r = W.evaluate(None, None, new_templates=["T1", "T2"])
    assert r["side_effect"] and r["hits"][0]["rule"] == "b"
    r = W.evaluate(None, None, new_templates=["T1", "T2"], expected_noise=["T2"])
    assert not r["side_effect"] and r["suppressed"][0]["templates"] == ["T2"]                     # recorded, not hidden
    r = W.evaluate(None, None, new_templates=["T1", "T2"], known_templates=["T1"])                # already in the case evidence
    assert not r["side_effect"]


def test_rules_c_d_e_are_never_suppressed():
    noise = ["T1", "T2", "case-x"]
    r = W.evaluate(None, None, new_cases=["anomaly:web-prod-07:cpu"], expected_noise=noise)
    assert r["side_effect"] and r["hits"][0]["rule"] == "c"
    r = W.evaluate(None, None, silent_s=400, expect_silence_s=0, expected_noise=noise)
    assert r["side_effect"] and r["hits"][0]["rule"] == "d"
    assert not W.evaluate(None, None, silent_s=400, expect_silence_s=120)["side_effect"]          # within the declared silence
    r = W.evaluate(None, None, slo_pre=99.9, slo_post=97.0, expected_noise=noise)
    assert r["side_effect"] and r["hits"][0]["rule"] == "e"


class Src:
    def __init__(self):
        self.calls = []

    def errors(self, host, start, end):
        self.calls.append((start, end))
        if (end - start) <= timedelta(seconds=120):                                                 # the expected_noise_s window
            return {"count": 50, "per_min": 25.0, "templates": ["restart-burst"]}
        return {"count": 3, "per_min": 0.2, "templates": []}

    def templates_seen(self, host, start, end):
        return set()

    def agent_last_seen(self, host):
        return None

    def new_cases(self, host, since, exclude_key):
        return []

    def slo(self, host, window_min=15, at=None):
        return 99.9


def test_collect_skips_the_expected_noise_window_and_records_it():
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    src = Src()
    r = W.collect(src, "web-prod-07", "k", t0, t0 + timedelta(minutes=15), expected_noise_s=120)
    assert not r["side_effect"] and r["suppressed"] and r["suppressed"][0]["rule"] == "noise_s"
    assert r["window"]["skip_s"] == 120
    assert any(s == t0 + timedelta(seconds=120) for s, _e in src.calls)                       # the post window starts after the skip
