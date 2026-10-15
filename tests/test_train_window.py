"""The operator's training window: weekdays + start / end in a time zone; a window may run past midnight; outside it automatic
training waits (with the next opening shown) while "Train now" still runs."""
from datetime import datetime

from watchover.autotrain import window_state

WEEKNIGHTS = {"train_schedule_on": True, "train_days": "1,2,3,4,5", "train_start": "22:00", "train_end": "05:00", "train_tz": "Europe/Istanbul"}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def test_window_past_midnight_belongs_to_the_day_it_starts():
    assert window_state(WEEKNIGHTS, at("2026-10-16T20:30:00+00:00"))["open"]          # Fri 23:30 Istanbul
    assert window_state(WEEKNIGHTS, at("2026-10-17T01:00:00+00:00"))["open"]          # Sat 04:00: Friday's window
    st = window_state(WEEKNIGHTS, at("2026-10-17T03:00:00+00:00"))                    # Sat 06:00: closed, next Monday 22:00
    assert not st["open"] and st["next"] == "2026-10-19 22:00"
    assert not window_state(WEEKNIGHTS, at("2026-10-18T20:00:00+00:00"))["open"]      # Sun 23:00: Sunday is not a training day


def test_same_day_window_and_bad_values():
    s = {"train_schedule_on": True, "train_days": "", "train_start": "01:00", "train_end": "06:00", "train_tz": "Nowhere/City"}
    st = window_state(s, at("2026-10-15T03:00:00+00:00"))                             # empty days = every day; unknown zone = UTC
    assert st["open"] and st["tz"] == "UTC"
    assert not window_state(s, at("2026-10-15T07:00:00+00:00"))["open"]
    assert window_state({**s, "train_schedule_on": False}, at("2026-10-15T07:00:00+00:00"))["on"] is False


def test_api_validates_and_reports_the_window(tmp_path, monkeypatch):
    for k, v in {"KNOWLEDGE_DB": str(tmp_path / "k.db"), "ACTIONS_DB": str(tmp_path / "a.db"), "PLAYBOOK_DB": str(tmp_path / "pb.db"),
                 "WATCHOVER_HOME": str(tmp_path / "home"), "LIVE_SPOOL": str(tmp_path / "live.jsonl"), "WATCHOVER_API_BACKGROUND": "0", "WATCHOVER_SELFMON": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from fastapi.testclient import TestClient
    from watchover.api import create_app
    from watchover.auth import INITIAL_EMAIL, INITIAL_PASSWORD
    app = create_app(); app.state.services.kb._exec("UPDATE users SET must_change=0")
    with TestClient(app) as c:
        h = {"Authorization": "Bearer " + c.post("/api/auth/login", json={"email": INITIAL_EMAIL, "password": INITIAL_PASSWORD}).json()["token"]}
        assert c.put("/api/llm/autotrain", json={"train_start": "25:00"}, headers=h).status_code == 400
        assert c.put("/api/llm/autotrain", json={"train_days": "9,0"}, headers=h).status_code == 400
        assert c.put("/api/llm/autotrain", json={"train_tz": "Mars/Base"}, headers=h).status_code == 400
        r = c.put("/api/llm/autotrain", json={"train_schedule_on": True, "train_days": "5, 1,3", "train_start": "23:00", "train_end": "04:30", "train_tz": "Europe/Istanbul"}, headers=h)
        assert r.status_code == 200 and r.json()["train_days"] == "1,3,5" and r.json()["train_schedule_on"] is True
        st = c.get("/api/llm/autotrain", headers=h).json()
        assert st["window"]["on"] and st["window"]["label"].startswith("Pzt,Çar,Cum 23:00-04:30")
        if not st["window"]["open"]:
            assert any("outside the training window" in x for x in st["reasons"])
