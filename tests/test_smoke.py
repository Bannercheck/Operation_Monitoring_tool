"""Smoke: package imports, demo dataset parses, pipeline yields incidents, Streamlit app renders."""
from pathlib import Path

from watchover.analysis import Analysis
from watchover.pipeline import ingest_path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo_mixed.zip"


def test_pipeline_smoke():
    obs, report = ingest_path(str(DEMO))
    a = Analysis(obs, report)
    assert a.funnel()["raw_events"] > 800 and a.incidents


def test_streamlit_app_renders(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIONS_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1")
    monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
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
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db"))
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1")
    monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
    monkeypatch.setenv("LIVE_SPOOL", str(tmp_path / "live.jsonl"))
    monkeypatch.setenv("LIVE_PORT", "8698")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    at.sidebar.radio(key="page").set_value("data").run()
    at.button(key="demo_main").click().run()
    at.button(key="demo_main").click().run()            # same file twice -> same key (not duplicated)
    assert not at.exception and len(at.session_state["datasets"]) == 1
    at.session_state["sim_on"] = True                   # simulation is off by default: switch it on so the live buffer fills
    at.run(); import time; time.sleep(4); at.run()
    at.button(key="live_an_data").click().run()         # live buffer -> second dataset
    assert not at.exception and len(at.session_state["datasets"]) == 2
    assert at.selectbox(key="ds_select").value == "live_buffer.jsonl"
    at.selectbox(key="ds_select").set_value("demo_mixed.zip").run()
    assert at.session_state["active"] == "demo_mixed.zip" and not at.exception
    at.selectbox(key="cmp_b").set_value("live_buffer.jsonl").run()
    assert not at.exception


def test_every_page_renders(monkeypatch, tmp_path):
    """Regression: the chat page once vanished from app.py (NameError on 'Watchover'a sor') without any test noticing."""
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home")); monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1"); monkeypatch.setenv("LIVE_PORT", "18631")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    for page in ("assist", "src", "llm", "map", "pb", "itsm", "conn", "users", "sys", "readme", "ops"):
        at.sidebar.radio(key="page").set_value(page).run()
        assert not at.exception, (page, at.exception)



def test_model_switch_does_not_touch_widget_keys(tmp_path, monkeypatch):
    """Regression: switching the Ollama model on the LLM page wrote llm_model into session state after the text_input with
    that key was instantiated (StreamlitWidgetAlreadyInstantiatedError). The change is queued and applied before any widget."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    i = src.index("def model_switcher(")
    body = src[i:src.index("\ndef ", i + 10)]
    assert "ss.update(changed)" not in body and '_pending_settings' in body
    assert src.index('st.session_state.pop("_pending_settings"') < src.index('if "cfg_loaded" not in st.session_state')


def test_upload_processed_once_and_multi_file(tmp_path, monkeypatch):
    """Regression: a re-uploaded same-named file (stdout0 from several SAP hosts) or a switch of the active dataset re-triggered
    the load on every run (the page kept 'loading'). Uploads are tracked by upload id; several files load as one dataset."""
    import io, zipfile
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'accept_multiple_files=True' in src and 'u.file_id not in done' in src and 'st.session_state.get("dataset") != up.name' not in src
    monkeypatch.setenv("KNOWLEDGE_DB", str(tmp_path / "k.db")); monkeypatch.setenv("PLAYBOOK_DB", str(tmp_path / "pb.db"))
    monkeypatch.setenv("WATCHOVER_HOME", str(tmp_path / "home")); monkeypatch.setenv("WATCHOVER_SKIP_SETUP", "1"); monkeypatch.setenv("LIVE_PORT", "18633")
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
    at.sidebar.radio(key="page").set_value("data").run()
    assert not at.exception
    # the combined-upload path: two SAP-style files packed the way the page packs them
    from watchover.pipeline import ingest_bytes
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("dev_w0", "A  Mon Sep 21 10:00:01 2026\nA  ** ERROR => rollback failed [dbsl.c 123]\n")
        z.writestr("stdout0", "")
    obs, _ = ingest_bytes("dev_w0 +1.zip", buf.getvalue())
    assert len(obs) >= 1
