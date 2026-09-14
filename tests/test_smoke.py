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
    monkeypatch.setenv("LIVE_SPOOL", str(tmp_path / "live.jsonl"))
    monkeypatch.setenv("LIVE_PORT", "8699")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    assert not at.exception
    at.sidebar.radio(key="page").set_value("data").run()   # Datasets page
    at.button(key="demo_main").click().run()                 # "Demo veri setini yükle"
    assert not at.exception
    assert any(">916<" in m.value and "ham olay" in m.value.lower() for m in at.markdown)
    at.sidebar.radio[0].set_value("en").run()     # language switch
    assert not at.exception
    assert any(">916<" in m.value and "raw events" in m.value.lower() for m in at.markdown)


def test_multi_dataset_registry_and_compare(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("LIVE_SPOOL", str(tmp_path / "live.jsonl"))
    monkeypatch.setenv("LIVE_PORT", "8698")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    at.sidebar.radio(key="page").set_value("data").run()
    at.button(key="demo_main").click().run()
    at.button(key="demo_main").click().run()            # same file twice -> same key (not duplicated)
    assert not at.exception and len(at.session_state["datasets"]) == 1
    at.button(key="live_an_data").click().run()         # live buffer -> second dataset
    assert not at.exception and len(at.session_state["datasets"]) == 2
    assert at.selectbox(key="ds_select").value == "live_buffer.jsonl"
    at.selectbox(key="ds_select").set_value("demo_mixed.zip").run()
    assert at.session_state["active"] == "demo_mixed.zip" and not at.exception
    at.selectbox(key="cmp_b").set_value("live_buffer.jsonl").run()
    assert not at.exception
