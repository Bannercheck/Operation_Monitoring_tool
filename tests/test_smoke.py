"""Smoke: package imports, demo dataset parses, pipeline yields incidents, Streamlit app renders."""
from pathlib import Path

from signal_sprint.analysis import Analysis
from signal_sprint.pipeline import ingest_path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo_mixed.zip"


def test_pipeline_smoke():
    obs, report = ingest_path(str(DEMO))
    a = Analysis(obs, report)
    assert a.funnel()["raw_events"] > 800 and a.incidents


def test_streamlit_app_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db"))
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.sidebar.button[0].click().run()            # "Demo veri setini yükle"
    assert not at.exception
    assert any(">914<" in m.value and "ham olay" in m.value.lower() for m in at.markdown)
    at.sidebar.radio[0].set_value("en").run()     # language switch
    assert not at.exception
    assert any(">914<" in m.value and "raw events" in m.value.lower() for m in at.markdown)
