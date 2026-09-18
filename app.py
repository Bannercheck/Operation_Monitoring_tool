"""Watchover dashboard (Streamlit).   streamlit run app.py

Sidebar: upload / demo. Tabs: Overview, Signals, Incidents, Actions. Deterministic engine, no API needed.
"""

from __future__ import annotations

from collections import Counter
import html
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from watchover.actions import PRIORITIES, STATUSES, ActionStore
from watchover.analysis import Analysis, interesting, llm_prompt, postmortem_md, signal_dict, suggested_owner
from watchover.connectors import fetch_http, fetch_mcp, mcp_tools, parse_headers
from watchover.live import LiveStore, start_receiver, start_simulator
from watchover.itsm import SYSTEMS, correlate as correlate_tickets, demo_tickets, fetch_generic
from watchover.analysis import template_of
from watchover.compare import compare as compare_datasets
from watchover.graph import build_map, to_html
from watchover.playbook import Playbook
from watchover.llm import LLMConfig, chat as llm_chat, test_connection as llm_test
from watchover.i18n import current_lang, factor_value, narrative_text, reason_text, recommendation_text, link_text, t
from watchover.models import SEV_RANK
from watchover.pipeline import ingest_bytes, ingest_path
from watchover.profiler import profile

st.set_page_config(page_title="Watchover", page_icon="📡", layout="wide", initial_sidebar_state="expanded")

# ---- Streamlit version compatibility: `width="stretch"` (>=1.5x) vs `use_container_width=True` (older)
_WIDE_CACHE: dict = {}


def wide(fn_name: str) -> dict:
    """Keyword args that make a widget/chart fill its container on any supported Streamlit version."""
    if fn_name not in _WIDE_CACHE:
        import inspect
        try:
            params = inspect.signature(getattr(st, fn_name)).parameters
        except (TypeError, ValueError):
            params = {}
        _WIDE_CACHE[fn_name] = {"width": "stretch"} if "width" in params else {"use_container_width": True}
    return _WIDE_CACHE[fn_name]

SEV_COLORS = {"CRITICAL": "#f87171", "ERROR": "#fb923c", "WARN": "#fbbf24", "INFO": "#64748b", "DEBUG": "#475569",
              "critical": "#f87171", "high": "#fb923c", "medium": "#fbbf24", "low": "#64748b"}
PRIO_COLORS = {"P1": "#f87171", "P2": "#fb923c", "P3": "#fbbf24", "P4": "#64748b"}
UTC = timezone.utc
SEV_SCALE = alt.Scale(domain=list(SEV_RANK), range=[SEV_COLORS[k] for k in SEV_RANK])


def _wo_altair() -> dict:
    """Chart theme matching the app: transparent background, muted axes, Inter, brand category colours."""
    return {"config": {"background": "transparent", "view": {"stroke": None}, "font": "Inter, sans-serif",
                       "axis": {"gridColor": "#1f2a3d", "gridOpacity": 0.8, "domainColor": "#243044", "tickColor": "#243044", "labelColor": "#8b98ad", "titleColor": "#8b98ad", "labelFontSize": 11},
                       "legend": {"labelColor": "#cbd5e1", "titleColor": "#8b98ad", "labelFontSize": 11},
                       "title": {"color": "#e6ebf2", "fontWeight": 600},
                       "range": {"category": ["#2dd4bf", "#60a5fa", "#a78bfa", "#fb923c", "#fbbf24", "#f87171", "#34d399", "#f472b6"]}}}


try:                                              # altair >= 5.5
    alt.theme.register("watchover", enable=True)(_wo_altair)
except AttributeError:                            # older altair
    alt.themes.register("watchover", _wo_altair); alt.themes.enable("watchover")
CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap');
:root{--wo-bg:#0e1420;--wo-surface:#151d2b;--wo-surface2:#111827;--wo-border:#243044;--wo-text:#e6ebf2;--wo-muted:#8b98ad;--wo-accent:#2dd4bf;--wo-accent2:#60a5fa}
.stApp,.stApp p,.stApp li,.stApp label,.stApp h1,.stApp h2,.stApp h3,.stApp h4,.stApp input,.stApp textarea,.stApp .stMarkdown{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
code,pre,.mono{font-family:"JetBrains Mono",ui-monospace,Menlo,monospace}
.block-container{padding-top:1.1rem;padding-bottom:2.5rem;max-width:1480px}
h1,h2,h3{letter-spacing:-.02em}h1{font-weight:800}h2{font-weight:700}
[data-testid="stHeader"]{background:transparent}
hr{border-color:var(--wo-border)!important;opacity:1}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:700;color:#0b1220;letter-spacing:.3px}
.card{background:linear-gradient(180deg,var(--wo-surface) 0%,#131b28 100%);border:1px solid var(--wo-border);border-radius:14px;padding:16px 18px;margin-bottom:12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.03),0 10px 28px -20px rgba(0,0,0,.9)}
.card.hot{border-color:rgba(248,113,113,.55);box-shadow:0 0 0 1px rgba(248,113,113,.12),0 14px 32px -18px rgba(248,113,113,.35)}
.kpi{background:linear-gradient(180deg,var(--wo-surface) 0%,#131b28 100%);border:1px solid var(--wo-border);border-radius:14px;padding:12px 14px;text-align:center}
.kpi b{display:block;font-size:30px;line-height:1.1;font-variant-numeric:tabular-nums}.kpi span{color:var(--wo-muted);font-size:12px;letter-spacing:.5px}
.k2{position:relative;overflow:hidden;background:linear-gradient(160deg,#182234 0%,#111827 100%);border:1px solid var(--wo-border);border-top:2px solid var(--acc);border-radius:14px;padding:14px 16px 8px;min-height:104px;box-shadow:0 12px 30px -22px rgba(0,0,0,.95);transition:border-color .15s,transform .15s}
.k2:hover{border-color:var(--acc)}
.k2:before{content:"";position:absolute;right:-34px;top:-34px;width:120px;height:120px;border-radius:60px;background:var(--acc);opacity:.10;filter:blur(6px)}
.k2 .ic{position:absolute;right:12px;top:10px;font-size:20px;opacity:.9}
.k2 .lb{color:var(--wo-muted);font-size:11px;font-weight:600;letter-spacing:.8px;padding-right:30px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.k2 .v{display:block;font-size:32px;font-weight:800;line-height:1.15;margin:4px 0 2px;color:#f4f7fb;white-space:nowrap;font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.k2 .v.s{font-size:26px}
.k2 .sub{font-size:12px;color:var(--acc);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.k2 .sub.m{color:var(--wo-muted)}
.flow{display:flex;align-items:center;justify-content:center;height:100%;color:var(--wo-accent);font-size:22px;padding-top:34px}
.arrow{text-align:center;color:var(--wo-accent);font-size:26px;padding-top:18px}
[class*="st-key-tile-"]{margin-top:-14px}
[class*="st-key-tile-"] button{width:100%;min-height:22px;height:22px;padding:0;font-size:11px;letter-spacing:.4px;background:#111827;color:#7c8aa3;border:1px solid var(--wo-border);border-top:0;border-radius:0 0 14px 14px}
[class*="st-key-tile-"] button:hover{color:var(--wo-accent);border-color:var(--wo-accent);background:#111827}
[class*="st-key-tile-"] button p{font-size:11px}
.k2{border-radius:14px 14px 0 0}
.muted{color:var(--wo-muted)}.mono{font-size:12.5px}
.tl{border-left:2px solid var(--wo-border);margin-left:6px;padding-left:14px}.tl .step{position:relative;margin-bottom:8px}
.tl .step:before{content:"";position:absolute;left:-19px;top:7px;width:8px;height:8px;border-radius:4px;background:var(--wo-muted)}
.tl .step.root:before{background:#f87171;box-shadow:0 0 0 3px rgba(248,113,113,.25)}
.fc-row{margin:8px 0;line-height:1.55}
.act{background:#111827;border:1px solid var(--wo-border);border-left:4px solid var(--wo-muted);border-radius:10px;padding:8px 10px;margin-bottom:8px}
section[data-testid="stSidebar"]{background:linear-gradient(180deg,#0b111b 0%,#0e1420 100%)}
section[data-testid="stSidebar"] [data-testid="stRadio"] label{padding:2px 0}
.wo-brand{display:flex;align-items:center;gap:11px;padding:4px 0 12px}
.wo-brand .name{font-size:23px;font-weight:800;letter-spacing:-.03em;line-height:1.05;background:linear-gradient(90deg,#2dd4bf 0%,#60a5fa 100%);-webkit-background-clip:text;background-clip:text;color:transparent}
.wo-brand .tag{font-size:10.5px;color:var(--wo-muted);letter-spacing:.7px;margin-top:2px}
.stTabs [data-baseweb="tab"]{font-weight:600}
div[data-testid="stExpander"] details{border:1px solid var(--wo-border);border-radius:12px;background:var(--wo-surface)}
</style>"""
st.markdown(CSS, unsafe_allow_html=True)


esc = html.escape


def pill(s: str) -> str:
    return f'<span class="pill" style="background:{SEV_COLORS.get(s, "#64748b")}">{s}</span>'


def upper(s: str) -> str:
    """Locale-aware upper case for labels: Turkish i -> İ (CSS text-transform gives ILIŞKI instead of İLİŞKİ)."""
    return str(s).replace("i", "İ").upper() if current_lang() == "tr" else str(s).upper()


def kpi(value, label) -> str:
    return f'<div class="kpi"><b>{value}</b><span>{upper(label)}</span></div>'


def kpi2(value, label, icon: str, accent: str, sub: str = "", muted: bool = False) -> str:
    return (f'<div class="k2" style="--acc:{accent}"><span class="ic">{icon}</span><div class="lb">{upper(label)}</div>'
            f'<span class="v{" s" if len(str(value)) > 6 else ""}">{value}</span><div class="sub{" m" if muted else ""}">{esc(str(sub))}</div></div>')


@st.cache_resource
def store() -> ActionStore:
    return ActionStore(os.environ.get("ACTIONS_DB", "actions.db"))


@st.cache_resource
def playbook() -> Playbook:
    return Playbook(os.environ.get("PLAYBOOK_DB", "playbook.db"))


@st.cache_resource
def live_store() -> LiveStore:
    return LiveStore(spool=os.environ.get("LIVE_SPOOL", "data/live/events.jsonl"))


@st.cache_resource
def receiver(port: int, api_key: str):
    """One receiver per (port, key) for the life of the process; a port clash returns the OSError to display."""
    try:
        return start_receiver(live_store(), port, api_key or None)
    except OSError as e:
        return e


@st.cache_resource
def simulator():
    return start_simulator(live_store(), interval=1.0)


def llm_cfg() -> LLMConfig:
    return LLMConfig(st.session_state.get("llm_base", ""), st.session_state.get("llm_model", ""), st.session_state.get("llm_key", ""))


def load(name: str, data: bytes | None = None, path: str | None = None, mapping: dict | None = None) -> None:
    t0 = time.perf_counter()
    with st.status(t("working"), expanded=True) as status:
        st.write(t("step_parse"))
        obs, report = ingest_bytes(name, data, mapping) if data is not None else ingest_path(path, mapping)
        st.write(t("step_analyze", n=f"{len(obs):,}"))
        analysis = Analysis(obs, report)
        st.write(t("step_profile", s=len(analysis.signals), i=len(analysis.incidents)))
        prof = profile(obs, report)
        status.update(label=t("done", secs=f"{time.perf_counter() - t0:.1f}"), state="complete", expanded=False)
    reg = st.session_state.setdefault("datasets", {})
    key = name
    if key in reg and reg[key]["source"] != {"name": name, "data": data, "path": path}:
        n = 2
        while f"{name} #{n}" in reg:
            n += 1
        key = f"{name} #{n}"
    reg[key] = {"analysis": analysis, "profile": prof, "source": {"name": name, "data": data, "path": path},
                "mapping": mapping or {}, "elapsed": time.perf_counter() - t0, "loaded_at": datetime.now(UTC)}
    activate_dataset(key)
    record_auto_recoveries(analysis, key)
    record_first_actions(analysis, key)
    playbook().record(analysis, key)
    rec = st.session_state.setdefault("recent", [])
    if key not in rec:
        rec.append(key)


def activate_dataset(key: str) -> None:
    """Mirror the selected dataset into the legacy single-dataset keys used by the analysis views."""
    d = st.session_state["datasets"][key]
    st.session_state.update({"analysis": d["analysis"], "profile": d["profile"], "dataset": key, "source": d["source"],
                             "mapping": d["mapping"], "elapsed": d["elapsed"], "active": key})
    st.session_state.pop("cursor", None)
    st.session_state.pop("detail", None)


def show_row(o, key: str = "") -> None:
    """Full record of one observation: all fields plus the raw line from the dataset."""
    fields = {"ref": o.ref, "timestamp": o.timestamp.isoformat(), "severity": o.severity, "service": o.service, "host": o.host,
              "environment": o.environment, "origin": o.origin, "kind": o.kind, "parser": o.parser, "template": o.template, "fingerprint": o.fingerprint, **{f"attr.{k}": v for k, v in o.attributes.items()}}
    st.markdown(f"**{t('row_detail')}** · `{o.ref}`")
    st.dataframe(pd.DataFrame({"field": list(fields), "value": [str(v) for v in fields.values()]}), hide_index=True, **wide("dataframe"),
                 height=min(420, 38 + 35 * len(fields)))
    st.markdown(f"**{t('raw_line')}**")
    st.code(o.raw or o.message, language=None)


def evidence_table(observations: list, key: str, height: int = 320):
    """Clickable evidence table; returns the selected observation (or None)."""
    df_ = pd.DataFrame([{"ref": o.ref, "time": o.timestamp.strftime("%H:%M:%S"), "sev": o.severity, "service": o.service, "host": o.host,
                         "message": o.message} for o in observations])
    st.caption(t("click_row"))
    ev = st.dataframe(df_, hide_index=True, **wide("dataframe"), height=height, on_select="rerun", selection_mode="single-row", key=key)
    rows = getattr(getattr(ev, "selection", None), "rows", None) or []
    refs = [o.ref for o in observations]
    pick = st.selectbox(t("pick_row"), [""] + refs, key=key + "-pick", label_visibility="collapsed", format_func=lambda x: x or t("pick_row"))
    if pick:
        return observations[refs.index(pick)]
    return observations[rows[0]] if rows else None


def incident_flashcard(inc, a: Analysis, key: str) -> None:
    """One card that tells the whole story of an incident."""
    root = a.signal_by_id[inc.root_cause_signal]
    tm, rc, og = inc.timing, inc.recovery, inc.origin
    symptoms = [a.signal_by_id[x] for x in inc.signal_ids if x != root.id]
    rk = rc.get("kind", "unknown")
    rcol = {"restart": "#2dd4bf", "self_healed": "#2dd4bf", "stopped": "#fbbf24", "ongoing": "#f87171"}.get(rk, "#8b98ad")
    chain = " → ".join([f"{root.id} {esc(root.template[:40])}"] + [f"{x.id} {esc(x.template[:40])}" for x in symptoms[:4]])
    alts_html = ""
    if inc.root_cause_alternatives:
        alts_html = f'<div class="fc-row"><b>{t("fc_alts")}</b><ul style="margin:4px 0 0 18px">' + "".join(
            f'<li><span class="mono">{esc(x["signal"])}</span> "{esc(x["template"][:70])}" ({esc(", ".join(x["services"][:2]))}) · {t("fc_alt_score")} {x["score"]:.1f} — {esc(reason_text(x["codes"]))}</li>'
            for x in inc.root_cause_alternatives[:3]) + "</ul></div>"
    group_html = ""
    if getattr(a, "mode", "") == "density" and a.storm.get("clusters"):
        cl = next((c for c in a.storm["clusters"] if any(o.attributes.get("alarm_id") == root.observations[0].attributes.get("alarm_id") for o in c["obs"])), None)
        if cl:
            cells = sorted(cl["hot"].items(), key=lambda kv: kv[0][1])
            t0 = a.storm["t0"]; bm = a.storm["bucket_min"]
            cell_txt = ", ".join(f"{svc} {(t0 + timedelta(minutes=b * bm)):%H:%M} ({h['count']}" + (f", {t('fc_median')} {h['median']:.0f})" if h.get("median") is not None else f", {h.get('by', '')})")
                                 for (svc, b), h in cells[:8])
            more = f" … +{len(cells) - 8}" if len(cells) > 8 else ""
            group_html = f'<div class="fc-row"><b>{t("fc_group")}</b> · {t("fc_group_how", n=len(cells), m=bm)} {esc(cell_txt)}{more}' + (f' · {t("fc_pruned", n=cl.get("pruned", 0))}' if cl.get("pruned") else "") + "</div>"
    total = sum(a.signal_by_id[x].count for x in inc.signal_ids)
    recs = "".join(f"<li>{esc(recommendation_text(r))}</li>" for r in inc.recommendations)
    resolved = recovery_label(rk) + (f' · {esc(rc.get("recovered_at") or "")[11:19]} · <span class="mono">{esc(rc.get("evidence") or "")}</span> · {esc(rc.get("what") or "")[:120]}' if rc.get("evidence") else "")
    pbe = playbook().lookup(root.template)
    seen = ""
    if pbe and (len(pbe["datasets"]) > 1 or pbe["occurrences"] > 1):
        seen = (f'<div class="fc-row"><b>📚 {t("pb_seen_before")}</b> {t("pb_times", n=pbe["occurrences"], d=len(pbe["datasets"]))} · '
                f'{esc(", ".join(pbe["datasets"][:3]))}' + (f' · {esc(pbe["resolution"][:140])}' if pbe["resolution"] else "") + "</div>")
    st.markdown(f"""<div class="card" style="border-top:3px solid {rcol};padding:18px 20px">
<div style="display:flex;justify-content:space-between;align-items:center"><span style="font-size:20px;font-weight:700">{inc.id} {pill(inc.severity)} <span class="muted" style="font-size:13px">{t('fc_score')} {inc.score}</span></span>
<span class="muted">{tm.get('first_signal', '')[:10]}</span></div>
<div class="fc-row"><b>{t('fc_what')}</b> · {esc(inc.title)}. {total:,} {t('fc_events')} {len(inc.signal_ids)} {t('fc_signals')}. <b>{t('fc_chain')}:</b> <span class="mono">{chain}</span></div>
<div class="fc-row"><b>{t('fc_why')}</b> · <span class="mono">{root.id}</span> "{esc(root.template)}" — {esc(reason_text(inc.root_cause_codes))}</div>
<div class="fc-row"><b>{t('fc_where')}</b> · {t('environment')}: {esc(', '.join(f'{e} ({n})' for e, n in og.get('environments', {}).items()))} · {t('services').lower()}: {esc(', '.join(og.get('services', [])) or '-')} · {t('hosts')}: {esc(', '.join(og.get('hosts', [])) or '-')} · {t('sources')}: {esc(', '.join(f'{k} ({v})' for k, v in list(og.get('files', {}).items())[:4]))}{(' · ' + t('origin_col') + ': ' + esc(', '.join(og.get('origins', {})))) if og.get('origins') else ''}</div>
<div class="fc-row"><b>{t('fc_when')}</b> · {t('fc_started')} <span class="mono">{tm.get('first_signal', '')[11:19]}</span> · {t('fc_last')} <span class="mono">{tm.get('last_error', '')[11:19]}</span> · {t('fc_lasted')} <b>{tm.get('duration_s', 0) / 60:.1f} min</b> · {t('quiet')} {rc.get('quiet_min', 0)} min</div>
<div class="fc-row"><b>{t('fc_resolved')}</b> · <span style="color:{rcol};font-weight:700">{resolved}</span></div>
<div class="fc-row"><b>{t('fc_todo')}</b> <span class="muted">· {t('fc_owner')}: {esc(suggested_owner(inc, root))}</span><ul style="margin:4px 0 0 18px">{recs}</ul></div>{alts_html}{group_html}{seen}</div>""", unsafe_allow_html=True)
    b1, b2, _ = st.columns([1, 1, 3])
    if b1.button(t("fc_open"), key=f"fc-open-{key}-{inc.id}", **wide("button")):
        st.session_state["inc_pick"] = inc.id
        st.session_state["detail"] = None
        st.rerun()
    def _to_map(i=inc.id):
        st.session_state["page"] = "map"
        st.session_state["map_inc"] = i
    b2.button(f"🕸 {t('fc_map')}", key=f"fc-map-{key}-{inc.id}", **wide("button"), on_click=_to_map)


def incidents_table(incidents: list, key: str, a: Analysis):
    """Selectable incident table + flashcard of the selected row."""
    df_ = pd.DataFrame([{"id": i.id, "severity": i.severity, "score": i.score, "title": i.title, "signals": len(i.signal_ids),
                         "services": ", ".join(i.affected_services), "window": f"{i.started_at:%H:%M}–{i.ended_at:%H:%M}",
                         "resolved": recovery_label(i.recovery.get("kind", "unknown"))} for i in incidents])
    st.caption(t("fc_pick"))
    ev = st.dataframe(df_, hide_index=True, **wide("dataframe"), on_select="rerun", selection_mode="single-row", key=key,
                      column_config={"score": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    rows = getattr(getattr(ev, "selection", None), "rows", None) or []
    ids = [i.id for i in incidents]
    pick = st.selectbox(t("pick_row"), [""] + ids, key=key + "-pick", label_visibility="collapsed", format_func=lambda x: x or t("pick_row"))
    chosen = pick or (ids[rows[0]] if rows else None)
    if chosen:
        incident_flashcard(a.incident_by_id[chosen], a, key)


def recovery_label(kind: str) -> str:
    return t("rec_" + kind) if kind in ("restart", "self_healed", "stopped", "ongoing") else t("rec_unknown")


def record_first_actions(a: Analysis, dataset: str) -> int:
    """Every card gets its recommended first action on record: owner + open status (deduplicated per dataset + incident)."""
    st_ = store()
    existing = {x["evidence"] for x in st_.list()}
    n = 0
    for inc in a.incidents:
        tag = f"first:{dataset}:{inc.id}"
        if tag in existing or not inc.recommendations:
            continue
        root = a.signal_by_id[inc.root_cause_signal]
        prio = "P1" if inc.severity == "critical" else "P2" if inc.severity == "high" else "P3"
        st_.create(inc.id, t("first_action_title", rec=inc.recommendations[0][:110]), prio, suggested_owner(inc, root), inc.recommendations[0], tag)
        n += 1
    return n


def record_auto_recoveries(a: Analysis, dataset: str) -> int:
    """Self-recovered incidents get a done action describing what happened (deduplicated per dataset + incident)."""
    st_ = store()
    existing = {x["evidence"] for x in st_.list()}
    n = 0
    for inc in a.incidents:
        kind = inc.recovery.get("kind")
        if kind not in ("restart", "self_healed"):
            continue
        tag = f"auto:{dataset}:{inc.id}"
        if tag in existing:
            continue
        what = (t("rec_restart") if kind == "restart" else t("rec_self_healed")) + f" · {inc.recovery.get('recovered_at', '')[11:19]} · {inc.recovery.get('evidence') or ''} · {inc.recovery.get('what', '')[:80]}"
        act = st_.create(inc.id, t("auto_title", what=what), "P3", "", t("auto_rec"), tag)
        st_.update(act["id"], status="done")
        n += 1
    return n


def minute_chart(df: pd.DataFrame, incidents=None, height=200):
    base = alt.Chart(df).mark_bar(color="#2dd4bf", opacity=0.85).encode(
        x=alt.X("timestamp:T", title=None, axis=alt.Axis(format="%H:%M")),
        y=alt.Y("events:Q", title="events / min"),
        tooltip=[alt.Tooltip("timestamp:T", format="%H:%M"), "events:Q"])
    layers = [base]
    if incidents:
        win = pd.DataFrame([{"start": i.started_at, "end": i.ended_at, "id": i.id, "severity": i.severity} for i in incidents])
        layers.insert(0, alt.Chart(win).mark_rect(opacity=0.22).encode(
            x="start:T", x2="end:T", color=alt.Color("severity:N", scale=alt.Scale(domain=list(SEV_COLORS), range=list(SEV_COLORS.values())), legend=None),
            tooltip=["id:N", "severity:N"]))
    return alt.layer(*layers).properties(height=height).configure_view(strokeWidth=0)


# ------------------------------------------------------------------ sidebar navigation
demo = Path(__file__).with_name("samples") / "demo_mixed.zip"
LIVE_PORT = int(os.environ.get("LIVE_PORT", "8600"))
PAGES = ["ops", "data", "map", "pb", "itsm", "conn", "readme"]
PAGE_KEYS = {"ops": "sb_ops", "data": "sb_data", "map": "sb_map", "pb": "sb_pb", "itsm": "sb_itsm", "conn": "sb_conn", "readme": "sb_readme"}
LOGO_SVG = ('<svg width="36" height="36" viewBox="0 0 34 34" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="wo" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="#2dd4bf"/><stop offset="1" stop-color="#60a5fa"/></linearGradient></defs>'
            '<circle cx="17" cy="17" r="15" fill="none" stroke="url(#wo)" stroke-width="2" opacity=".3"/>'
            '<path d="M17 5a12 12 0 0 1 12 12" fill="none" stroke="url(#wo)" stroke-width="2.6" stroke-linecap="round"/>'
            '<path d="M17 10.5a6.5 6.5 0 0 1 6.5 6.5" fill="none" stroke="url(#wo)" stroke-width="2.6" stroke-linecap="round" opacity=".75"/>'
            '<circle cx="17" cy="17" r="3.2" fill="url(#wo)"/></svg>')

with st.sidebar:
    st.markdown(f'<div class="wo-brand">{LOGO_SVG}<div><div class="name">Watchover</div><div class="tag">{upper(t("brand_tag"))}</div></div></div>', unsafe_allow_html=True)
    st.radio("Language", ["tr", "en"], horizontal=True, label_visibility="collapsed",
             format_func=lambda x: {"tr": "🇹🇷 Türkçe", "en": "🇬🇧 English"}[x], key="lang")
    st.markdown("")
    page = st.radio("nav", PAGES, format_func=lambda x: t(PAGE_KEYS[x]), label_visibility="collapsed", key="page")
    st.markdown("---")

    @st.fragment(run_every="5s")
    def _status():
        ls_ = live_store()
        st.caption(f"⚡ :{st.session_state.get('live_port', LIVE_PORT)} · {ls_.received:,} {t('events_n')} · {len(ls_.agents)} {t('live_agents')}")
        if "analysis" in st.session_state:
            st.caption(f"🗄️ {st.session_state['dataset']} · {st.session_state['analysis'].funnel()['incidents']} {t('incidents')}")
        if st.session_state.get("tickets"):
            st.caption(f"🎫 {len(st.session_state['tickets'])} {t('tickets_n')} · {st.session_state.get('tickets_src', '-')}")

    _status()
    st.caption(t("footer"))

# always-on receiver + optional simulator (started once per process)
_srv = receiver(int(st.session_state.get("live_port", LIVE_PORT)), st.session_state.get("live_key", ""))
if st.session_state.get("sim_on", True):
    simulator()


def fetch_tickets(system: str) -> list:
    ss = st.session_state
    if system == "demo":
        return demo_tickets()
    if system == "generic":
        mapping = json.loads(ss.get("itsm_mapping") or "{}")
        headers = {"Authorization": f"Bearer {ss['itsm_token']}"} if ss.get("itsm_token") else {}
        return fetch_generic(ss.get("itsm_base", ""), headers, mapping, ss.get("itsm_path", ""))
    kw = {"user": ss.get("itsm_user", ""), "password": ss.get("itsm_pass", ""), "token": ss.get("itsm_token", "")}
    if ss.get("itsm_query"):
        kw["query"] = ss["itsm_query"]
    return SYSTEMS[system](ss.get("itsm_base", ""), **kw)


def live_signals(ls: LiveStore, window_min: int = 15) -> list[dict]:
    """Quick error/burst signals from the live buffer for ticket correlation (no full pipeline)."""
    start = datetime.now(UTC) - timedelta(minutes=window_min)
    groups: dict[str, dict] = {}
    for o in ls.snapshot():
        if o.timestamp < start or SEV_RANK[o.severity] < 3:
            continue
        tpl = template_of(o.message)
        g = groups.setdefault(tpl, {"id": f"L{len(groups) + 1}", "template": tpl, "services": set(), "hosts": set(), "severity": o.severity, "onset": o.timestamp, "count": 0})
        g["count"] += 1
        if o.service: g["services"].add(o.service)
        if o.host: g["hosts"].add(o.host)
        g["onset"] = min(g["onset"], o.timestamp)
    out = [dict(g, services=sorted(g["services"]), hosts=sorted(g["hosts"])) for g in groups.values() if g["count"] >= 3]
    return sorted(out, key=lambda g: -g["count"])[:12]


def correlated_tickets(ls: LiveStore, breaches: list[dict]) -> list:
    tickets = st.session_state.get("tickets")
    if tickets is None and st.session_state.get("itsm_system", "demo") == "demo":
        tickets = st.session_state["tickets"] = demo_tickets()
        st.session_state["tickets_src"] = "demo"
    if not tickets:
        return []
    incs = [{"id": i.id, "title": i.title, "services": i.affected_services, "hosts": i.affected_hosts, "started_at": i.started_at}
            for i in st.session_state["analysis"].incidents] if "analysis" in st.session_state else []
    return correlate_tickets(tickets, live_signals(ls), breaches, incs)


def tickets_df(tickets: list) -> pd.DataFrame:
    return pd.DataFrame([{t("t_id"): x.id, t("t_prio"): x.priority, t("t_status"): x.status, t("t_title"): x.title, t("t_service"): x.service,
                          t("t_opened"): x.created.strftime("%m-%d %H:%M") if x.created else "", t("t_rel"): x.relevance,
                          t("t_related"): ", ".join(x.related), t("t_why"): "; ".join(x.reasons)} for x in tickets])


def pct(v, digits=2):
    return f"{v * 100:.{digits}f}%" if v is not None else t("no_data")


def picker(label: str, options: list[str], key: str, fmt=None, help_: str = "", hide: bool = False) -> str | None:
    """Dropdown with an 'All' entry first. Returns the picked option or None for 'All'. Keeps its value across reruns."""
    all_ = t("ops_all")
    opts = [all_] + options
    if st.session_state.get(key) not in opts:
        st.session_state[key] = all_
    pick = st.selectbox(label, opts, key=key, help=help_ or None, format_func=(lambda v: v if v == all_ or fmt is None else fmt(v)),
                        label_visibility="collapsed" if hide else "visible")
    return None if pick == all_ else pick


def _set_scope(env_key: str, host_key: str, env=None, host=None) -> None:
    """Button callback: runs before widgets are built, so the popover pickers can be updated safely."""
    all_ = t("ops_all")
    st.session_state[env_key] = env or all_
    st.session_state[host_key] = host or all_


def scope_panel(ls: LiveStore) -> tuple[str | None, str | None]:
    """Environment box next to the cards: environment list, then the hosts of that environment. Returns (env, host)."""
    host_env = ls.hosts()
    summary = {r["environment"]: r for r in ls.env_summary(15)}
    envs = [e for e in summary] + sorted(set(host_env.values()) - set(summary))
    with st.container(border=True):
        st.markdown(f"**🌐 {t('ops_env')}**")
        env = picker("env", envs, "ops_env_pick", help_=t("ops_env_hint"), hide=True)
        if not env and summary:
            st.markdown("<div class='muted' style='font-size:12px;line-height:1.7'>" + "<br>".join(
                f"{esc(e)} · {pct(r['availability'], 1)} · {r['errors']} {t('env_errors')}" for e, r in summary.items()) + "</div>", unsafe_allow_html=True)
        hosts = [h for h, e in host_env.items() if not env or e == env]
        st.markdown(f"**🖥 {t('ops_hosts')}** <span class='muted'>· {len(hosts)}</span>", unsafe_allow_html=True)
        all_ = t("ops_all")
        opts = [all_] + hosts
        if st.session_state.get("ops_host_pick") not in opts:
            st.session_state["ops_host_pick"] = all_
        pick = st.radio("host", opts, key="ops_host_pick", label_visibility="collapsed", help=t("ops_host_hint"),
                        format_func=lambda h: h if (h == all_ or env) else f"{h} · {host_env.get(h, '')}")
        host = None if pick == all_ else pick
        st.button(f"✕ {t('ops_reset')}", key="ops_reset", on_click=_set_scope, args=("ops_env_pick", "ops_host_pick"), disabled=not (env or host), **wide("button"))
    return env, host


def ops_tile(key: str, html_: str, clickable: bool) -> None:
    """A kpi2 card; with clickable=True a 'Detail' strip below toggles the live detail panel (session key ops_detail)."""
    if not clickable:
        st.markdown(html_, unsafe_allow_html=True); return
    sel = st.session_state.get("ops_detail") == key
    st.markdown('<div class="tile">' + html_ + "</div>", unsafe_allow_html=True)
    st.button(("✓ " if sel else "") + t("detail"), key=f"ops-tile-{key}", **wide("button"), type="primary" if sel else "secondary",
              on_click=lambda k=key: st.session_state.__setitem__("ops_detail", None if st.session_state.get("ops_detail") == k else k))


def metric_chart(d: pd.DataFrame, height: int, thr: float | None = None):
    base = alt.Chart(d).mark_line(interpolate="monotone", strokeWidth=1.6).encode(
        x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("value:Q", title=None, scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("host:N", legend=alt.Legend(orient="bottom", title=None, columns=4)), tooltip=["host", "env", "metric", "value"])
    if thr is not None:
        base = base + alt.Chart(pd.DataFrame({"y": [thr]})).mark_rule(color="#f87171", strokeDash=[4, 4]).encode(y="y:Q")
    return base.properties(height=height).configure_view(strokeWidth=0)


def metrics_block(ms: dict, clickable: bool = False) -> None:
    """CPU / GPU / memory / disk tiles with per-host line charts."""
    thr = __import__("watchover.scenario", fromlist=["x"]).METRIC_THRESHOLDS
    pm = pd.DataFrame(ms["per_minute"]) if ms["per_minute"] else pd.DataFrame(columns=["minute", "host", "metric", "env", "value"])
    ic = st.columns(4)
    for col, metric, icon in zip(ic, ("cpu", "gpu", "memory", "disk"), ("🧠", "🎮", "💾", "🗄")):
        sm = ms["summary"][metric]
        val = f"{sm['avg']:.0f}%" if sm["avg"] is not None else t("no_data")
        color = "#8b98ad" if sm["max"] is None else "#f87171" if sm["max"] >= thr[metric] else "#fb923c" if sm["max"] >= thr[metric] - 15 else "#2dd4bf"
        sub = f"{t('worst')} {sm['worst']} {sm['max']:.0f}% · {sm['hosts']} {t('hosts_n_short')}" if sm["max"] is not None else ""
        with col:
            ops_tile(metric, kpi2(val, t(metric), icon, color, sub), clickable)
            d = pm[pm.metric == metric]
            if len(d):
                st.altair_chart(metric_chart(d, 170), **wide("altair_chart"))


def slo_block(slo: dict, stt: dict, clickable: bool = False) -> None:
    k = st.columns(6)
    av, p95, bud = slo["availability"], slo["p95_ms"], slo["error_budget"]
    av_ok = av is not None and av >= slo["slo"]["availability"]
    p_ok = p95 is None or p95 <= slo["slo"]["p95_ms"]
    tiles = [
        ("availability", kpi2(pct(av, 1), t("availability"), "🎯", "#2dd4bf" if av_ok else "#f87171", t("slo_target", v=pct(slo["slo"]["availability"], 1)))),
        ("p95", kpi2(f"{p95:.0f} ms" if p95 is not None else t("no_data"), t("p95"), "⏱", "#2dd4bf" if p_ok else "#fb923c", f"SLO ≤ {slo['slo']['p95_ms']} ms")),
        ("budget", kpi2(pct(bud, 0) if bud is not None else t("no_data"), t("budget"), "🧮", "#2dd4bf" if (bud or 0) > 0.25 else "#fb923c" if (bud or 0) > 0 else "#f87171", f"{slo['errors']} / {slo['total']} ERROR+")),
        ("sla", kpi2(t("ok") if slo["sla_ok"] else t("breach"), t("sla"), "📜", "#2dd4bf" if slo["sla_ok"] else "#f87171", t("sla_target", v=pct(slo["sla"]["availability"], 1)))),
        ("rate", kpi2(stt["per_minute_now"], t("live_rate"), "⚡", "#60a5fa", f"{stt['received']:,} {t('n_total')}")),
        ("errors", kpi2(stt["errors"], t("live_errors"), "🔥", "#f87171" if stt["errors"] else "#8b98ad", f"{stt['total']:,} {t('live_buffered')}", True)),
    ]
    for col, (key, html_) in zip(k, tiles):
        with col:
            ops_tile(key, html_, clickable)


def _bar(df: pd.DataFrame, x: str, y: str, color: str = "#f87171", height: int = 200, fmt: str = "d"):
    return alt.Chart(df).mark_bar(color=color).encode(x=alt.X(f"{x}:Q", title=None, axis=alt.Axis(format=fmt)), y=alt.Y(f"{y}:N", sort="-x", title=None),
                                                     tooltip=[y, x]).properties(height=height).configure_view(strokeWidth=0)


def ops_detail_panel(kind: str, ls: LiveStore, env, host, ms: dict, stt: dict, slo: dict) -> None:
    """Live detail for the clicked card: one metric in depth, or what drives a service-level figure."""
    thr = __import__("watchover.scenario", fromlist=["x"]).METRIC_THRESHOLDS
    with st.container(border=True):
        h, x = st.columns([8, 1])
        h.markdown(f"#### {t('od_' + kind)} <span class='muted' style='font-size:13px'>· {t('od_window')}</span>", unsafe_allow_html=True)
        x.button(t("close"), key="ops-detail-close", **wide("button"), on_click=lambda: st.session_state.__setitem__("ops_detail", None))
        if kind in ("cpu", "gpu", "memory", "disk"):
            pm = pd.DataFrame(ms["per_minute"]) if ms["per_minute"] else pd.DataFrame(columns=["minute", "host", "metric", "env", "value"])
            d = pm[pm.metric == kind]
            if not len(d):
                st.info(t("no_data")); return
            st.altair_chart(metric_chart(d, 300, thr[kind]), **wide("altair_chart"))
            rows = []
            for hn, g in d.groupby("host"):
                last, mx, avg = float(g.value.iloc[-1]), float(g.value.max()), float(g.value.mean())
                rows.append({t("host"): hn, t("environment"): g.env.iloc[0], t("od_latest"): last, t("od_avg"): round(avg, 1), t("od_max"): mx,
                             t("od_threshold"): thr[kind], t("od_status"): t("breach") if mx >= thr[kind] else t("od_warn") if mx >= thr[kind] - 15 else t("ok")})
            st.dataframe(pd.DataFrame(rows).sort_values(t("od_max"), ascending=False), hide_index=True, **wide("dataframe"),
                         column_config={t("od_latest"): st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f%%"),
                                        t("od_max"): st.column_config.NumberColumn(format="%.0f%%")})
            br = [b for b in ms["breaches"] if b.get("metric") == kind]
            if br:
                st.markdown(f"**{t('od_breaches')}** · " + ", ".join(f"{b['host']} {b['value']:.0f}%" for b in br[:8]))
            return
        det = ls.slo_detail(15, env, host)
        if kind == "rate":
            obs = [o for o in ls.snapshot(env, host) if o.timestamp >= datetime.now(UTC) - timedelta(minutes=15)]
            top = [s_ for s_, _ in Counter((o.service or "-") for o in obs).most_common(6)]
            rows = Counter(((o.timestamp.replace(second=0, microsecond=0)), (o.service if o.service in top else t("od_other"))) for o in obs)
            df_ = pd.DataFrame([{"minute": k[0], "service": k[1], "events": v} for k, v in rows.items()])
            if len(df_):
                st.altair_chart(alt.Chart(df_).mark_area(interpolate="monotone").encode(
                    x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("events:Q", stack=True, title=None, axis=alt.Axis(format="d")),
                    color=alt.Color("service:N", legend=alt.Legend(orient="top", title=None)), tooltip=[alt.Tooltip("minute:T", format="%H:%M"), "service", "events"])
                    .properties(height=240).configure_view(strokeWidth=0), **wide("altair_chart"))
            c1, c2 = st.columns(2)
            c1.dataframe(pd.DataFrame(stt["services"], columns=[t("service"), t("env_events")]), hide_index=True, **wide("dataframe"))
            c2.dataframe(pd.DataFrame(Counter((o.host or "-") for o in obs).most_common(), columns=[t("host"), t("env_events")]), hide_index=True, **wide("dataframe"))
            return
        if kind == "errors":
            if not det["recent"]:
                st.success(t("od_no_errors")); return
            df_ = pd.DataFrame(det["recent"]); df_["ts"] = df_["ts"].dt.strftime("%H:%M:%S")
            st.dataframe(df_.rename(columns={"ts": t("time"), "severity": t("severity"), "service": t("service"), "host": t("host"), "message": t("message")}),
                         hide_index=True, **wide("dataframe"), height=380)
            return
        # availability / sla / budget / p95 -> what lowers the figure
        target = slo["slo"]["availability"] if kind != "sla" else slo["sla"]["availability"]
        k = st.columns(4)
        k[0].markdown(kpi2(pct(slo["availability"], 2), t("availability"), "🎯", "#2dd4bf" if (slo["availability"] or 0) >= target else "#f87171", t("od_target", v=pct(target, 1))), unsafe_allow_html=True)
        k[1].markdown(kpi2(f"{det['errors']} / {det['total']}", t("od_err_total"), "🔥", "#f87171" if det["errors"] else "#8b98ad", t("od_allowed", n=det["allowed_errors"])), unsafe_allow_html=True)
        k[2].markdown(kpi2(det["breach_minutes"], t("od_breach_min"), "⏱", "#f87171" if det["breach_minutes"] else "#2dd4bf", t("od_of_15")), unsafe_allow_html=True)
        if kind == "p95":
            k[3].markdown(kpi2(f"{slo['p95_ms']:.0f} ms" if slo["p95_ms"] is not None else t("no_data"), t("p95"), "⏱", "#fb923c", f"SLO ≤ {slo['slo']['p95_ms']} ms"), unsafe_allow_html=True)
        else:
            k[3].markdown(kpi2(pct(slo["error_budget"], 0) if slo["error_budget"] is not None else t("no_data"), t("budget"), "🧮",
                               "#2dd4bf" if (slo["error_budget"] or 0) > 0.25 else "#f87171", t("od_budget_sub", v=pct(1 - target, 2))), unsafe_allow_html=True)
        pm = pd.DataFrame(det["per_minute"])
        if kind == "p95":
            if not det["latency"]:
                st.info(t("od_no_latency")); return
            c1, c2 = st.columns([2, 3])
            with c1:
                lat = pd.DataFrame(det["latency"])
                st.altair_chart((alt.Chart(lat).mark_bar(color="#fb923c").encode(x=alt.X("p95:Q", title="p95 ms"), y=alt.Y("service:N", sort="-x", title=None), tooltip=["service", "n", "p50", "p95", "max"])
                                 + alt.Chart(pd.DataFrame({"x": [slo["slo"]["p95_ms"]]})).mark_rule(color="#f87171", strokeDash=[4, 4]).encode(x="x:Q")).properties(height=220).configure_view(strokeWidth=0), **wide("altair_chart"))
                st.dataframe(lat.rename(columns={"service": t("service"), "n": t("od_samples")}), hide_index=True, **wide("dataframe"))
            with c2:
                st.markdown(f"**{t('od_slowest')}**")
                sl = pd.DataFrame(det["slowest"]); sl["ts"] = sl["ts"].dt.strftime("%H:%M:%S")
                st.dataframe(sl.rename(columns={"ts": t("time"), "service": t("service"), "host": t("host"), "message": t("message")}), hide_index=True, **wide("dataframe"), height=330)
            return
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(f"**{t('od_minute_avail') if kind != 'budget' else t('od_burn')}**")
            if kind == "budget":
                d = pm.dropna(subset=["budget_left"])
                ch = alt.Chart(d).mark_area(interpolate="monotone", color="#2dd4bf", opacity=.5).encode(
                    x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("budget_left:Q", title=None, axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
                    tooltip=[alt.Tooltip("minute:T", format="%H:%M"), alt.Tooltip("budget_left:Q", format=".0%"), "errors", "total"])
            else:
                d = pm.dropna(subset=["availability"])
                ch = (alt.Chart(d).mark_line(interpolate="monotone", color="#60a5fa", point=True).encode(
                    x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("availability:Q", title=None, axis=alt.Axis(format="%"), scale=alt.Scale(domainMax=1)),
                    tooltip=[alt.Tooltip("minute:T", format="%H:%M"), alt.Tooltip("availability:Q", format=".2%"), "errors", "total"])
                      + alt.Chart(pd.DataFrame({"y": [target]})).mark_rule(color="#f87171", strokeDash=[4, 4]).encode(y="y:Q"))
            if len(d):
                st.altair_chart(ch.properties(height=220).configure_view(strokeWidth=0), **wide("altair_chart"))
            else:
                st.info(t("no_data"))
        with c2:
            st.markdown(f"**{t('od_err_by_service')}**")
            if det["by_service"]:
                st.altair_chart(_bar(pd.DataFrame(det["by_service"], columns=["service", "errors"]), "errors", "service"), **wide("altair_chart"))
            else:
                st.success(t("od_no_errors"))
        if det["templates"]:
            st.markdown(f"**{t('od_what_lowers')}**")
            tp = pd.DataFrame([{t("severity"): g["severity"], t("od_count"): g["count"], t("od_template"): g["template"], t("service"): ", ".join(g["services"]),
                                t("host"): ", ".join(g["hosts"]), t("od_first"): g["first"].strftime("%H:%M:%S"), t("od_last"): g["last"].strftime("%H:%M:%S"),
                                t("od_sample"): g["sample"]} for g in det["templates"]])
            st.dataframe(tp, hide_index=True, **wide("dataframe"), height=38 + 35 * min(8, len(tp)))
            if det["by_host"]:
                st.markdown(f"**{t('od_err_by_host')}** · " + ", ".join(f"{h_} ({n})" for h_, n in det["by_host"][:8]))


def events_block(stt: dict) -> None:
    c1, c2 = st.columns([3, 2])
    rows = pd.DataFrame(stt["rows"])
    with c1:
        st.markdown(f"**{t('live_events_min')}**")
        st.altair_chart(alt.Chart(rows).mark_area(interpolate="monotone").encode(
            x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("events:Q", stack=True, title=None, axis=alt.Axis(format="d")),
            color=alt.Color("severity:N", scale=SEV_SCALE, legend=alt.Legend(orient="top", title=None)), order=alt.Order("severity:N"),
            tooltip=[alt.Tooltip("minute:T", format="%H:%M"), "severity", "events"]).properties(height=170).configure_view(strokeWidth=0), **wide("altair_chart"))
    with c2:
        st.markdown(f"**{t('live_services')}**")
        svc = pd.DataFrame(stt["services"], columns=["service", "events"]) if stt["services"] else pd.DataFrame({"service": ["-"], "events": [0]})
        st.altair_chart(alt.Chart(svc).mark_bar(color="#2dd4bf").encode(x=alt.X("events:Q", title=None, axis=alt.Axis(format="d")), y=alt.Y("service:N", sort="-x", title=None),
                        tooltip=["service", "events"]).properties(height=170).configure_view(strokeWidth=0), **wide("altair_chart"))


def tail_block(ls: LiveStore, n: int = 12, env: str | None = None, host: str | None = None) -> None:
    st.markdown(f"**{t('live_tail')}**")
    lines = "".join(
        f'<div><span class="muted">{o.timestamp:%H:%M:%S}</span> <span style="color:{SEV_COLORS.get(o.severity, "#8b98ad")};font-weight:700">{o.severity:<8}</span> '
        f'<span style="color:#a5b4c9">{esc(o.service or o.source)[:18]:<18}</span> {esc(o.message)[:150]}</div>' for o in reversed(ls.tail(n, env, host)))
    st.markdown(f'<div class="card mono" style="max-height:260px;overflow:auto;white-space:pre;line-height:1.55">{lines or "…"}</div>', unsafe_allow_html=True)


# ------------------------------------------------------------------ page: Operations (home)
def page_ops() -> None:
    ls = live_store()

    @st.fragment(run_every="2s")
    def _panel():
        all_stt = ls.stats(15)
        real_agents = [a_ for a_ in all_stt["agents"] if a_ != "simulator"]
        badge_txt, badge_col = (t("live_real_badge"), "#2dd4bf") if real_agents else (t("live_sim_badge"), "#fbbf24")
        st.markdown(f'## {t("live_title")} <span class="pill" style="background:{badge_col}">{badge_txt}</span>'
                    f' <span class="muted" style="font-size:13px">· {t("live_sub")} · {len(all_stt["agents"])} {t("live_agents")}: {", ".join(list(all_stt["agents"])[:4]) or t("live_no_agent")}</span>', unsafe_allow_html=True)
        # cards on the left, environment / host box on the right; the box narrows all ten cards
        main, side = st.columns([4.6, 1.25], gap="medium")
        with side:
            st.markdown(f"#### {t('ops_scope')}")
            env, host = scope_panel(ls)
        stt, slo, ms = ls.stats(15, env, host), ls.slo(15, env, host), ls.metric_stats(15, env, host)
        scope = " · ".join(x for x in (env, host) if x)
        scope_html = f" <span class='pill' style='background:#60a5fa'>{t('ops_filter_on')}: {esc(scope)}</span>" if scope else ""
        with main:
            st.markdown(f"#### {t('ops_infra')}{scope_html} <span class='muted'>· {ms['samples']} {t('od_samples')}</span>", unsafe_allow_html=True)
            metrics_block(ms, clickable=True)
            if st.session_state.get("ops_detail") in ("cpu", "gpu", "memory", "disk"):
                ops_detail_panel(st.session_state["ops_detail"], ls, env, host, ms, stt, slo)
            st.markdown(f"#### {t('ops_slo')}{scope_html}", unsafe_allow_html=True)
            st.caption(t("ops_slo_basis"))
            slo_block(slo, stt, clickable=True)
            if st.session_state.get("ops_detail") in ("availability", "p95", "budget", "sla", "rate", "errors"):
                ops_detail_panel(st.session_state["ops_detail"], ls, env, host, ms, stt, slo)
        st.markdown("")
        events_block(stt)
        tk = correlated_tickets(ls, ms["breaches"])
        st.markdown(f"#### {t('ops_tickets_top')} <span class='muted'>· {st.session_state.get('tickets_src', '-')} · {t('see_itsm')}</span>", unsafe_allow_html=True)
        if tk:
            st.dataframe(tickets_df(tk[:5]), hide_index=True, **wide("dataframe"), height=38 + 35 * min(5, len(tk)),
                         column_config={t("t_rel"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        else:
            st.info(t("no_tickets"))
        tail_block(ls, env=env, host=host)

    _panel()
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button(t("live_analyze"), key="ops_an", **wide("button")) and ls.received:
        name, data = ls.to_dataset()
        load(name, data=data)
        st.session_state["page"] = "data"
        st.rerun()
    if b2.button(t("live_clear"), key="ops_clr", **wide("button")):
        ls.clear()
        st.rerun()



# ------------------------------------------------------------------ page: Error map
MAP_LEGEND = [("root", "map_l_root"), ("affected", "map_l_affected"), ("erroring", "map_l_erroring"), ("clean", "map_l_clean")]


@st.cache_resource(show_spinner=False)
def live_analysis(n_received: int) -> Analysis | None:
    """Analysis over the live buffer, recomputed only when new events arrived (keyed by received count)."""
    ls = live_store()
    obs = ls.snapshot()
    return Analysis(list(obs), []) if obs else None


def page_map() -> None:
    from watchover.graph import PALETTE
    st.markdown(f"## 🕸 {t('map_title')} <span class='muted' style='font-size:13px'>· {t('map_sub')}</span>", unsafe_allow_html=True)
    reg = st.session_state.get("datasets", {})
    sources = list(reg) + ["__live__"]
    fmt = lambda k: t("map_live") if k == "__live__" else k
    default = st.session_state.get("dataset") if st.session_state.get("dataset") in reg else sources[0]
    if st.session_state.get("map_src") not in sources:
        st.session_state["map_src"] = default
    src = st.selectbox(t("map_source"), sources, format_func=fmt, key="map_src")
    live = src == "__live__"

    @st.fragment(run_every="5s" if live else None)
    def _map():
        a = live_analysis(live_store().received) if live else reg[src]["analysis"]
        if a is None or not a.observations:
            st.info(t("map_empty")); return
        inc_ids = [i.id for i in a.incidents]
        opts = ["__all__"] + inc_ids
        want = st.session_state.pop("map_inc", None)
        if want in inc_ids:
            st.session_state["map_pick"] = want
        if st.session_state.get("map_pick") not in opts:
            st.session_state["map_pick"] = inc_ids[0] if inc_ids else "__all__"
        envs = sorted({o.environment or "unknown" for o in a.observations})
        hosts_all = sorted({o.host for o in a.observations if o.host})
        c2, c3, c4, c5 = st.columns([2.2, 1, 1.2, 0.8], vertical_alignment="bottom")
        inc_pick = c2.selectbox(t("map_incident"), opts, key="map_pick",
                                format_func=lambda x: t("map_all_inc") if x == "__all__" else f"{x} · {a.incident_by_id[x].title[:48]}")
        with c3:
            env = picker(t("ops_env"), envs, "map_env")
        with c4:
            host = picker(t("ops_hosts"), [h for h in hosts_all if not env or any((o.host == h and (o.environment or "unknown") == env) for o in a.observations[:5000])] or hosts_all, "map_host")
        with_hosts = c5.toggle(t("map_draw_hosts"), value=len(hosts_all) <= 12, key="map_hosts_toggle")   # dozens of hosts squash the map: off by default
        m = build_map(a, None if inc_pick == "__all__" else inc_pick, env=env, host=host, with_hosts=with_hosts)
        if not m["nodes"]:
            st.info(t("map_empty")); return
        k = st.columns(5)
        k[0].markdown(kpi2(len(m["nodes"]), t("map_k_services"), "🧩", "#60a5fa", f"{len({h['host'] for h in m['hosts']})} {t('env_hosts')}"), unsafe_allow_html=True)
        k[1].markdown(kpi2(len(m["deps"]), t("map_k_deps"), "🔗", "#f87171" if m["deps"] else "#8b98ad", f"{sum(e['n'] for e in m['deps'])} {t('map_k_dep_events')}"), unsafe_allow_html=True)
        k[2].markdown(kpi2(len(m["corr"]), t("map_k_corr"), "🧭", "#60a5fa", t("map_k_corr_sub")), unsafe_allow_html=True)
        k[3].markdown(kpi2(", ".join(m["root"]) or "—", t("map_k_root"), "⚑", "#f87171" if m["root"] else "#8b98ad", ", ".join(m["incidents"][:3])), unsafe_allow_html=True)
        k[4].markdown(kpi2(m["errors_total"], t("map_k_errors"), "🔥", "#fb923c", f"{len(m['affected'])} {t('map_k_affected')}"), unsafe_allow_html=True)
        sig_by_svc: dict[str, list[dict]] = {}
        for s_ in a.signals:
            for svc in s_.services:
                if any(n["id"] == svc for n in m["nodes"]) and len(sig_by_svc.setdefault(svc, [])) < 6:
                    sig_by_svc[svc].append({"severity": s_.severity, "count": s_.count, "template": s_.template[:110],
                                            "hosts": ", ".join(sorted(s_.hosts)[:3]), "color": SEV_COLORS.get(s_.severity, "#8b98ad")})
        labels = {"root": t("map_l_root_tag"), "dep": t("map_dep_lbl"), "hosts": t("env_hosts"), "errors": "ERROR+", "signals": t("signals_n"),
                  "depends_on": t("map_depends_on").rstrip(":"), "depended_by": t("map_depended_by").rstrip(":"), "on_hosts": t("ops_hosts"),
                  "reset": t("map_reset"), "hint": t("map_hint"), "kind_root": t("map_l_root"), "kind_affected": t("map_l_affected"),
                  "kind_erroring": t("map_l_erroring"), "kind_clean": t("map_l_clean"), "host": t("host")}
        html_ = to_html(m, labels, sig_by_svc, height=640)
        if hasattr(st, "iframe"):
            st.iframe(html_, height=660)
        else:
            components.html(html_, height=660, scrolling=False)
        c1, c2_ = st.columns([3, 2])
        with c1:
            if len(m["chain"]) > 1:
                st.markdown(f"**{t('map_chain')}** · " + " → ".join(f"<span class='pill' style='background:{PALETTE['root'] if i == 0 else PALETTE['affected']}'>{esc(x)}</span>" for i, x in enumerate(m["chain"])),
                            unsafe_allow_html=True)
        with c2_:
            st.markdown("".join(f"<span style='display:inline-block;margin-right:12px'><span style='display:inline-block;width:10px;height:10px;border-radius:3px;background:{PALETTE[k_]};margin-right:5px'></span>{t(key)}</span>" for k_, key in MAP_LEGEND)
                        + f"<div class='muted' style='font-size:12px;margin-top:4px'>{t('map_l_edges')}</div>", unsafe_allow_html=True)
        if live:
            st.caption(f"⟳ {t('map_live_note')} · {live_store().received:,} events")

    _map()


# ------------------------------------------------------------------ page: Playbook
def page_playbook() -> None:
    pb = playbook()
    st.markdown(f"## {t('pb_title')}")
    st.caption(t("pb_sub"))
    q = st.text_input(t("pb_search"), key="pb_q")
    rows = pb.all(q)
    if not rows:
        st.info(t("pb_empty")); return
    df_ = pd.DataFrame([{"template": r["template"], "sev": r["severity"], t("pb_occ"): r["occurrences"], t("pb_datasets"): len(r["datasets"]),
                         t("pb_events"): r["events"], t("pb_root"): r["root_cause_count"],
                         t("pb_recov"): ", ".join(f"{k}×{v}" for k, v in r["recoveries"].items()) or "-",
                         t("pb_last"): (r["last_seen"] or "")[:16], "runbook": "✓" if r["runbook"] else "", "notes": "✓" if r["resolution"] else ""} for r in rows])
    st.dataframe(df_, hide_index=True, **wide("dataframe"), height=min(400, 38 + 35 * len(df_)))
    keys = [r["template"] for r in rows]
    sel = st.selectbox(t("pb_entry"), keys, key="pb_sel", format_func=lambda k: k[:90])
    r = pb.get(sel)
    if not r:
        return
    st.markdown(f'<div class="card">{pill(r["severity"])} <span class="mono">{esc(r["template"])}</span><br>'
                f'<span class="muted">{t("pb_first")} {esc(r["first_seen"] or "")[:16]} · {t("pb_last")} {esc(r["last_seen"] or "")[:16]} · '
                f'{t("pb_times", n=r["occurrences"], d=len(r["datasets"]))} · {r["events"]} {t("pb_events")} · {t("pb_root")} {r["root_cause_count"]}×</span><br>'
                f'{t("pb_datasets")}: {esc(", ".join(r["datasets"]))}<br>{t("services").lower()}: {esc(", ".join(r["services"]) or "-")} · {t("hosts")}: {esc(", ".join(r["hosts"]) or "-")}<br>'
                f'{t("pb_recov")}: ' + (", ".join(f"{recovery_label(k)} ×{v}" for k, v in r["recoveries"].items()) or "-") + "</div>", unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    res = c1.text_area(t("pb_resolution"), value=r["resolution"], key=f"pb_res_{sel}", height=140)
    rb = c2.text_area(t("pb_runbook"), value=r["runbook"], key=f"pb_rb_{sel}", height=140)
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button(t("pb_save"), key="pb_save", **wide("button")):
        pb.set_notes(sel, res, rb); st.success(t("pb_saved"))
    if b2.button(t("pb_delete"), key="pb_del", **wide("button")):
        pb.delete(sel); st.rerun()


def playbook_card(template: str, current_dataset: str) -> None:
    """'Seen before' card for an incident's root-cause template."""
    e = playbook().lookup(template)
    others = [d for d in (e["datasets"] if e else []) if d != current_dataset]
    if not e or not others and e["occurrences"] <= 1:
        st.markdown(f'<div class="card"><b>📚 {t("pb_new")}</b></div>', unsafe_allow_html=True); return
    sim = f' · {t("pb_similar")} {e.get("similarity")}' if e.get("similarity") else ""
    recov = ", ".join(f"{recovery_label(k)} ×{v}" for k, v in e["recoveries"].items()) or "-"
    st.markdown(f'<div class="card" style="border-left:4px solid #a78bfa"><b>📚 {t("pb_seen_before")}</b>{sim} · {t("pb_times", n=e["occurrences"], d=len(e["datasets"]))} · '
                f'{t("pb_last")} {esc(e["last_seen"] or "")[:16]}<br><span class="muted">{t("pb_datasets")}: {esc(", ".join(others or e["datasets"]))} · {t("pb_recov")}: {recov}</span>'
                f'<br><b>{t("pb_resolution")}:</b> {esc(e["resolution"]) or t("pb_no_notes")}<br><b>{t("pb_runbook")}:</b><br>{esc(e["runbook"]).replace(chr(10), "<br>") or t("pb_no_notes")}</div>',
                unsafe_allow_html=True)
    if st.button(t("pb_open"), key=f"pb_open_{template[:20]}"):
        st.session_state["page"] = "pb"; st.session_state["pb_sel"] = e["template"]; st.rerun()


# ------------------------------------------------------------------ page: ITSM
def page_itsm() -> None:
    st.markdown(f"## {t('sb_itsm')}")
    left, right = st.columns([1, 2])
    with left:
        st.markdown(f"#### {t('itsm_config')}")
        system = st.selectbox(t("itsm_system"), ["demo", "servicenow", "jira", "onedesk", "generic"], key="itsm_system",
                              format_func=lambda x: {"demo": t("itsm_demo"), "servicenow": "ServiceNow", "jira": "Jira Service Management", "onedesk": "OneDesk", "generic": "Generic REST"}[x])
        if system != "demo":
            st.text_input(t("itsm_base"), key="itsm_base", placeholder={"servicenow": "https://<instance>.service-now.com", "jira": "https://<site>.atlassian.net",
                                                                        "onedesk": "https://app.onedesk.com/rest", "generic": "https://api.example.com/tickets"}[system])
            st.text_input(t("itsm_user"), key="itsm_user"); st.text_input(t("itsm_pass"), key="itsm_pass", type="password")
            st.text_input(t("itsm_token"), key="itsm_token", type="password")
            st.text_input(t("itsm_query"), key="itsm_query")
            if system == "generic":
                st.text_input(t("itsm_path"), key="itsm_path"); st.text_area(t("itsm_mapping"), key="itsm_mapping", height=68, placeholder='{"title": "subject", "created": "opened_at"}')
        st.toggle(t("itsm_auto"), key="itsm_auto", value=False)
        if st.button(t("itsm_fetch"), key="itsm_fetch", **wide("button")):
            try:
                st.session_state["tickets"] = fetch_tickets(system)
                st.session_state["tickets_src"] = system
                st.session_state["tickets_at"] = time.time()
                st.success(t("itsm_ok", n=len(st.session_state["tickets"]), src=system))
            except Exception as e:  # noqa: BLE001
                st.error(t("itsm_err", e=e))
    with right:
        ls = live_store()

        @st.fragment(run_every="5s")
        def _table():
            if st.session_state.get("itsm_auto") and time.time() - st.session_state.get("tickets_at", 0) > 60:
                try:
                    st.session_state["tickets"] = fetch_tickets(st.session_state.get("itsm_system", "demo"))
                    st.session_state["tickets_at"] = time.time()
                except Exception:  # noqa: BLE001
                    pass
            ms = ls.metric_stats(15)
            tk = correlated_tickets(ls, ms["breaches"])
            st.markdown(f"#### {t('itsm_table')} <span class='muted'>· {len(tk)} · {t('ops_tickets_sub')}</span>", unsafe_allow_html=True)
            if not tk:
                st.info(t("no_tickets")); return
            st.dataframe(tickets_df(tk), hide_index=True, **wide("dataframe"), height=min(420, 38 + 35 * len(tk)),
                         column_config={t("t_rel"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
            sel = st.selectbox(t("itsm_detail"), [x.id for x in tk], format_func=lambda i: f"{i} · {next(x.title for x in tk if x.id == i)[:60]}", key="itsm_sel")
            x = next(x for x in tk if x.id == sel)
            st.markdown(f'<div class="card"><b>{esc(x.id)}</b> {pill(x.priority) if x.priority in ("P1", "P2", "P3", "P4") else esc(x.priority)} · {esc(x.status)} · {esc(x.service)} · {esc(x.assignee) or t("unassigned")}'
                        f'<br><b>{esc(x.title)}</b><br><span class="muted">{esc(x.description)}</span><br><br><b>{t("t_rel")}</b> {x.relevance} · <b>{t("t_related")}</b> {", ".join(x.related) or "-"}'
                        f'<br><b>{t("t_why")}</b><br>' + "<br>".join("• " + esc(r) for r in x.reasons) + (f'<br><a href="{esc(x.url)}">{esc(x.url)}</a>' if x.url else "") + "</div>", unsafe_allow_html=True)

        _table()


# ------------------------------------------------------------------ page: Connection Settings
def page_conn() -> None:
    st.markdown(f"## {t('sb_conn')}")
    tab_src, tab_live_, tab_llm = st.tabs([t("conn_sources"), t("conn_live"), t("conn_llm")])
    with tab_src:
        kind = st.radio(t("conn_type"), ["http", "mcp"], horizontal=True, format_func=lambda x: t("conn_" + x), key="conn_kind")
        c_url = st.text_input(t("conn_url"), key="conn_url", placeholder="https://api.example.com/alerts" if kind == "http" else "http://localhost:8765/mcp")
        c_headers = st.text_area(t("conn_headers"), key="conn_headers", height=68, placeholder="Authorization: Bearer …\nX-API-Key: …")
        if kind == "http":
            c_method = st.selectbox(t("conn_method"), ["GET", "POST"], key="conn_method")
            c_body = st.text_area(t("conn_body"), key="conn_body", height=68) if c_method == "POST" else ""
            c_path = st.text_input(t("conn_path"), key="conn_path")
        else:
            st.caption(t("conn_mcp_hint"))
            if st.button(t("conn_list_tools"), key="conn_tools") and c_url:
                try:
                    st.session_state["conn_toolnames"] = [x["name"] for x in mcp_tools(c_url, parse_headers(c_headers))]
                except Exception as e:  # noqa: BLE001
                    st.error(t("conn_err", e=e))
            names = st.session_state.get("conn_toolnames", [])
            c_tool = st.selectbox(t("conn_tool"), names, key="conn_tool") if names else st.text_input(t("conn_tool"), key="conn_tool_txt")
            c_args = st.text_input(t("conn_args"), key="conn_args", value="{}")
        if st.button(t("conn_fetch"), key="conn_go") and c_url:
            try:
                if kind == "http":
                    name, data = fetch_http(c_url, c_method, parse_headers(c_headers), c_body or None, c_path)
                else:
                    name, data = fetch_mcp(c_url, c_tool, json.loads(c_args or "{}"), parse_headers(c_headers))
                st.success(t("conn_ok", n=f"{len(data):,}", name=name))
                load(name, data=data)
                st.session_state["page"] = "data"
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(t("conn_err", e=e))
    with tab_live_:
        c1, c2 = st.columns(2)
        port = c1.number_input(t("live_port"), 1024, 65535, LIVE_PORT, key="live_port")
        key = c2.text_input(t("live_key"), key="live_key", type="password", placeholder="secret")
        st.toggle(t("live_sim"), value=st.session_state.get("sim_on", True), key="sim_on")
        srv = receiver(int(port), key)
        (st.error(str(srv)) if isinstance(srv, OSError) else st.success(t("live_running", port=int(port))))
        st.caption(t("live_agent_cmd"))
        st.code(f"python agent.py --url http://<dashboard-host>:{int(port)}/ingest{' --key ' + key if key else ''} --tail /var/log/app.log\n"
                f"python agent.py --url http://<dashboard-host>:{int(port)}/ingest{' --key ' + key if key else ''} --metrics --interval 10", language="bash")
    with tab_llm:
        st.caption(t("llm_hint"))
        st.text_input(t("llm_base"), key="llm_base", placeholder="http://localhost:3000/v1")
        st.text_input(t("llm_model"), key="llm_model", placeholder="llama3.1")
        st.text_input(t("llm_key"), key="llm_key", type="password")
        if st.button(t("llm_test"), key="llm_test_btn"):
            ok, info = llm_test(llm_cfg())
            (st.success if ok else st.error)(t("llm_ok", info=info) if ok else t("llm_fail", e=info))


# ------------------------------------------------------------------ page: Datasets (upload + log analysis)
def datasets_controls() -> None:
    st.markdown(f"## {t('sb_data')}")
    c1, c2 = st.columns([3, 1])
    up = c1.file_uploader(t("upload"), type=None, key="up_main")
    if up is not None and st.session_state.get("dataset") != up.name:
        load(up.name, data=up.getvalue())
        st.rerun()
    with c2:
        if demo.exists() and st.button(t("load_demo"), key="demo_main", **wide("button")):
            load(demo.name, path=str(demo))
            st.rerun()
        if live_store().received and st.button(f"{t('live_analyze')} ({len(live_store().buf):,})", key="live_an_data", **wide("button")):
            name, data = live_store().to_dataset()
            load(name, data=data)
            st.rerun()
    reg = st.session_state.get("datasets", {})
    if reg:
        keys = list(reg)
        s1, s2 = st.columns([4, 1])
        chosen = s1.selectbox(t("ds_active"), keys, index=keys.index(st.session_state.get("active", keys[-1])) if st.session_state.get("active") in keys else len(keys) - 1,
                              key="ds_select", format_func=lambda k: f"{k} · {reg[k]['analysis'].funnel()['raw_events']:,} {t('raw_events')} · {reg[k]['analysis'].funnel()['incidents']} {t('incidents')} · {reg[k]['loaded_at']:%H:%M}")
        if chosen != st.session_state.get("active"):
            activate_dataset(chosen)
            st.rerun()
        if s2.button(t("ds_remove"), key="ds_remove", **wide("button")):
            reg.pop(chosen, None)
            for k in ("analysis", "profile", "dataset", "active", "source", "mapping"):
                st.session_state.pop(k, None)
            if reg:
                activate_dataset(list(reg)[-1])
            st.rerun()
        if len(reg) >= 2:
            with st.expander(t("ds_compare")):
                ca, cb = st.columns(2)
                ka = ca.selectbox(t("ds_a"), keys, index=0, key="cmp_a")
                kb = cb.selectbox(t("ds_b"), keys, index=min(1, len(keys) - 1), key="cmp_b")
                if ka == kb:
                    st.warning(t("cmp_same"))
                else:
                    compare_view(reg[ka], reg[kb], ka, kb)


def compare_view(da: dict, db: dict, ka: str, kb: str) -> None:
    c = compare_datasets(da["analysis"], da["profile"], db["analysis"], db["profile"])
    st.caption(f"A = {ka} · B = {kb}")
    st.markdown(f"**{t('cmp_kpis')}**")
    kdf = pd.DataFrame(c["kpis"]).rename(columns={"metric": t("metric"), "a": "A", "b": "B", "delta": t("delta"), "delta_pct": "Δ %"})
    kdf[t("metric")] = kdf[t("metric")].map(lambda k: t(k) if k in ("raw_events", "fingerprints", "incidents") else k)
    st.dataframe(kdf, hide_index=True, **wide("dataframe"), height=38 + 35 * len(kdf))
    l, r = st.columns(2)
    with l:
        st.markdown(f"**{t('cmp_sev')}**")
        st.altair_chart(alt.Chart(pd.DataFrame(c["severity"])).mark_bar().encode(
            x=alt.X("dataset:N", title=None), y=alt.Y("events:Q", stack="normalize", title=None),
            color=alt.Color("severity:N", scale=SEV_SCALE, legend=alt.Legend(orient="top", title=None)), order=alt.Order("severity:N"),
            tooltip=["dataset", "severity", "events"]).properties(height=200).configure_view(strokeWidth=0), **wide("altair_chart"))
    with r:
        st.markdown(f"**{t('cmp_timeline')}**")
        st.altair_chart(alt.Chart(pd.DataFrame(c["timeline"])).mark_line(interpolate="monotone").encode(
            x=alt.X("minute:Q", title="min"), y=alt.Y("events:Q", title=None),
            color=alt.Color("dataset:N", scale=alt.Scale(domain=["A", "B"], range=["#2dd4bf", "#60a5fa"]), legend=alt.Legend(orient="top", title=None)),
            tooltip=["dataset", "minute", "events"]).properties(height=200).configure_view(strokeWidth=0), **wide("altair_chart"))
    sm = c["summary"]
    t1, t2, t3 = st.tabs([f"{t('cmp_shared')} · {sm['shared']}", f"{t('cmp_only_a')} · {sm['only_a']}", f"{t('cmp_only_b')} · {sm['only_b']}"])
    with t1:
        st.dataframe(pd.DataFrame(c["shared"]) if c["shared"] else pd.DataFrame(columns=["template"]), hide_index=True, **wide("dataframe"), height=min(360, 38 + 35 * max(len(c["shared"]), 1)))
    with t2:
        st.dataframe(pd.DataFrame(c["only_a"]), hide_index=True, **wide("dataframe"), height=min(360, 38 + 35 * max(len(c["only_a"]), 1)))
    with t3:
        st.dataframe(pd.DataFrame(c["only_b"]), hide_index=True, **wide("dataframe"), height=min(360, 38 + 35 * max(len(c["only_b"]), 1)))
    st.markdown(f"**{t('cmp_incidents')}**")
    ia, ib = st.columns(2)
    ia.dataframe(pd.DataFrame(c["incidents_a"]) if c["incidents_a"] else pd.DataFrame(columns=["id"]), hide_index=True, **wide("dataframe"))
    ib.dataframe(pd.DataFrame(c["incidents_b"]) if c["incidents_b"] else pd.DataFrame(columns=["id"]), hide_index=True, **wide("dataframe"))


if page == "ops":
    page_ops()
    st.stop()
if page == "map":
    page_map()
    st.stop()
if page == "pb":
    page_playbook()
    st.stop()
if page == "itsm":
    page_itsm()
    st.stop()
if page == "conn":
    page_conn()
    st.stop()
if page == "readme":
    readme = Path(__file__).with_name("README.md")
    st.markdown(readme.read_text(encoding="utf-8") if readme.exists() else "README.md not found")
    st.stop()

datasets_controls()
if "analysis" not in st.session_state:
    st.info(t("ds_hint"))
    st.stop()

a: Analysis = st.session_state["analysis"]
prof = st.session_state["profile"]
f = a.funnel()
st.caption(f"{t('ds_loaded')}: {st.session_state['dataset']} · {st.session_state.get('elapsed', 0):.1f}s")
na = a.noise_audit()
tab_over, tab_sig, tab_inc, tab_act, tab_noise = st.tabs([t("tab_overview"), f"{t('tab_signals')} · {f['fingerprints']}", f"{t('tab_incidents')} · {f['incidents']}", f"{t('tab_actions')} · {len(store().list())}", f"{t('tab_noise')} · {na['eliminated']}"])

# ------------------------------------------------------------------ overview
@st.cache_data(show_spinner=False)
def frames(dataset: str, n: int):
    """DataFrames for the overview charts, cached per loaded dataset."""
    a_ = st.session_state["analysis"]
    df = pd.DataFrame([{"timestamp": o.timestamp, "severity": o.severity, "service": o.service or "-", "host": o.host or "-",
                        "environment": o.environment or "unknown", "origin": o.origin or "-",
                        "kind": o.kind, "source": o.source, "message": o.message} for o in a_.observations])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("min")
    return df


with tab_over:
    def tile(col, key: str, value, label: str, icon: str, accent: str, sub: str = "", muted: bool = False) -> None:
        with col:
            st.markdown('<div class="tile">' + kpi2(value, label, icon, accent, sub, muted) + "</div>", unsafe_allow_html=True)
            if st.button(t("detail"), key=f"tile-{key}", **wide("button")):
                st.session_state["detail"] = None if st.session_state.get("detail") == key else key
                st.rerun()

    df = frames(st.session_state["dataset"], len(a.observations))
    n_err = int((df.severity.isin(["ERROR", "CRITICAL"])).sum())
    n_crit = sum(1 for i in a.incidents if i.severity == "critical")
    acts_all = store().list()
    n_open = sum(1 for x_ in acts_all if x_["status"] in ("open", "in_progress"))
    top_sig = a.signals[0] if a.signals else None
    svc_counts = df[df.service != "-"].service.value_counts()
    host_counts = df[df.host != "-"].host.value_counts()
    err_sigs = [x_ for x_ in a.signals if SEV_RANK[x_.severity] >= 3]
    tr = prof["time_range"]
    cols = st.columns([3, 1, 3, 1, 3, 1, 3, 1, 3])
    steps = [
        ("raw_events", f"{f['raw_events']:,}", "🧾", "#60a5fa", f"{n_err / max(f['raw_events'], 1):.0%} ERROR+" if f["raw_events"] else ""),
        ("fingerprints", f["fingerprints"], "🧬", "#2dd4bf", f"{f['reduction']}× {t('reduction')}"),
        ("meaningful", f["meaningful_signals"], "📡", "#fbbf24", f"{top_sig.id} · {t('burst')} {top_sig.burst_score}" if top_sig else ""),
        ("incidents", f["incidents"], "🚨", "#f87171" if n_crit else "#fb923c", f"{n_crit} critical" if n_crit else t("no_critical")),
        ("actions", len(acts_all), "✅", "#a78bfa", f"{n_open} {t('open_n')}" if acts_all else t("none_yet")),
    ]
    for i, (key, v, ic, acc, sub) in enumerate(steps):
        tile(cols[i * 2], key, v, t(key), ic, acc, sub)
        if i < 4:
            cols[i * 2 + 1].markdown('<div class="flow">→</div>', unsafe_allow_html=True)
    tiles = st.columns(7)
    fmts = ", ".join(sorted({r["format"] for r in a.report}))
    envs = prof.get("environments", {})
    env_err_total = sum(v["errors"] for v in envs.values()) or 1
    env_sub = " · ".join(f"{e} {v['errors'] / env_err_total:.0%}" for e, v in list(envs.items())[:3] if v["errors"]) or ", ".join(list(envs)[:3])
    second = [
        ("files_n", len(prof["files"]), "📁", "#8b98ad", fmts, True),
        ("records_n", f"{prof['records']:,}", "🗂", "#8b98ad", f"{max(a.report, key=lambda r: r['rows'])['file'].split('/')[-1]} · {max(r['rows'] for r in a.report):,}" if a.report else "", True),
        ("services_n", len(prof["services"]), "🧩", "#60a5fa", f"{svc_counts.index[0]} · {svc_counts.iloc[0]:,}" if len(svc_counts) else "-", False),
        ("hosts_n", len(prof["hosts"]), "🖥", "#60a5fa", f"{host_counts.index[0]} · {host_counts.iloc[0]:,}" if len(host_counts) else "-", False),
        ("error_classes", prof["error_classes"], "💥", "#fb923c", f"{err_sigs[0].template[:32]} · {err_sigs[0].count}" if err_sigs else "-", False),
        ("span_min", tr["minutes"], "⏱", "#2dd4bf", f"{tr['start'][11:16]} → {tr['end'][11:16]} · {prof.get('bucket', '1min')}", False),
        ("environments", len([e for e in envs if e != "unknown"]) or len(envs), "🌍", "#a78bfa", env_sub, False),
    ]
    for c, (key, v, ic, acc, sub, muted) in zip(tiles, second):
        tile(c, key, v, t(key), ic, acc, sub, muted)

    detail = st.session_state.get("detail")
    if detail:
        with st.container(border=True):
            h, x = st.columns([6, 1])
            h.markdown(f"#### {t('d_' + detail)}")
            if x.button(t("close"), key="detail-close", **wide("button")):
                st.session_state["detail"] = None; st.rerun()
            obs_df = df.assign(time=df["timestamp"].dt.strftime("%H:%M:%S"))[["time", "severity", "service", "host", "source", "message"]]
            if detail == "raw_events":
                st.dataframe(obs_df, hide_index=True, **wide("dataframe"), height=420)
            elif detail in ("fingerprints", "meaningful"):
                rows = [x_ for x_ in a.signals if detail == "fingerprints" or interesting(x_)]
                st.dataframe(pd.DataFrame([signal_dict(x_) for x_ in rows]), hide_index=True, **wide("dataframe"), height=min(420, 38 + 35 * max(len(rows), 1)),
                             column_config={"burst": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
            elif detail == "incidents":
                incidents_table(a.incidents, "ov-inc", a)
            elif detail == "actions":
                acts_ = store().list()
                st.dataframe(pd.DataFrame(acts_) if acts_ else pd.DataFrame(columns=["id", "incident_id", "title", "priority", "status", "owner"]), hide_index=True, **wide("dataframe"))
            elif detail in ("files_n", "records_n"):
                st.dataframe(pd.DataFrame([{"file": r["file"], "format": r["format"], t("conf"): r["confidence"], t("rows"): r["rows"], "kind": r["kind"],
                                            **{k: (v or "") for k, v in r["roles"].items()}} for r in a.report]), hide_index=True, **wide("dataframe"),
                             column_config={t("conf"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
                per_file = df.groupby(["source", "severity"]).size().rename("events").reset_index()
                st.altair_chart(alt.Chart(per_file).mark_bar().encode(x=alt.X("events:Q", title=None), y=alt.Y("source:N", title=None),
                                color=alt.Color("severity:N", scale=SEV_SCALE, legend=None), tooltip=["source", "severity", "events"]).properties(height=30 * len(a.report) + 20), **wide("altair_chart"))
            elif detail in ("services_n", "hosts_n"):
                col = "service" if detail == "services_n" else "host"
                g = df[df[col] != "-"].groupby(col).agg(events=("severity", "size"), errors=("severity", lambda x_: (x_.isin(["ERROR", "CRITICAL"])).sum()),
                                                      first=("timestamp", "min"), last=("timestamp", "max")).reset_index().sort_values("events", ascending=False)
                g["error_rate"] = (g["errors"] / g["events"]).round(3)
                g["first"] = g["first"].dt.strftime("%H:%M:%S"); g["last"] = g["last"].dt.strftime("%H:%M:%S")
                l2, r2 = st.columns([2, 3])
                l2.dataframe(g, hide_index=True, **wide("dataframe"), column_config={"error_rate": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
                bycol = df[df[col] != "-"].groupby([col, "severity"]).size().rename("events").reset_index()
                r2.altair_chart(alt.Chart(bycol).mark_bar().encode(x=alt.X("events:Q", title=None), y=alt.Y(f"{col}:N", sort="-x", title=None),
                                color=alt.Color("severity:N", scale=SEV_SCALE, legend=None), tooltip=[col, "severity", "events"]).properties(height=max(120, 26 * bycol[col].nunique())), **wide("altair_chart"))
            elif detail == "error_classes":
                err = [x_ for x_ in a.signals if SEV_RANK[x_.severity] >= 3]
                st.dataframe(pd.DataFrame([{"id": x_.id, "severity": x_.severity, "template": x_.template, "count": x_.count, "services": ", ".join(x_.services),
                                            "first": x_.first_seen.strftime("%H:%M:%S"), "last": x_.last_seen.strftime("%H:%M:%S")} for x_ in err]),
                             hide_index=True, **wide("dataframe"))
            elif detail == "environments":
                env_rows = [{t("environment"): e, t("env_events"): v["events"], t("env_errors"): v["errors"], t("env_rate"): v["error_rate"],
                             t("env_share"): round(v["errors"] / env_err_total, 3)} for e, v in envs.items()]
                l3, r3 = st.columns([2, 3])
                l3.dataframe(pd.DataFrame(env_rows), hide_index=True, **wide("dataframe"),
                             column_config={t("env_rate"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
                                            t("env_share"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0%")})
                byenv = df.groupby(["environment", "severity"]).size().rename("events").reset_index()
                r3.altair_chart(alt.Chart(byenv).mark_bar(size=26).encode(x=alt.X("events:Q", title=None), y=alt.Y("environment:N", sort="-x", title=None),
                                color=alt.Color("severity:N", scale=SEV_SCALE, legend=alt.Legend(orient="top", title=None)), tooltip=["environment", "severity", "events"])
                                .properties(height=70 + 44 * byenv.environment.nunique()), **wide("altair_chart"))
                st.markdown(f"**{t('d_origins')}**")
                origins = prof.get("origins", {})
                if origins:
                    st.dataframe(pd.DataFrame([{t("origin_col"): o_, t("env_events"): v["events"], t("env_errors"): v["errors"]} for o_, v in origins.items()]),
                                 hide_index=True, **wide("dataframe"))
                else:
                    st.caption(t("no_origin"))
                err_env_svc = df[df.severity.isin(["ERROR", "CRITICAL"])].groupby(["environment", "service"]).size().rename("errors").reset_index().sort_values("errors", ascending=False).head(15)
                if len(err_env_svc):
                    st.altair_chart(alt.Chart(err_env_svc).mark_bar(size=22).encode(x=alt.X("errors:Q", title=None), y=alt.Y("service:N", sort="-x", title=None),
                                    color=alt.Color("environment:N", legend=alt.Legend(orient="top", title=None)), tooltip=["environment", "service", "errors"])
                                    .properties(height=70 + 36 * err_env_svc.service.nunique()), **wide("altair_chart"))
            elif detail == "span_min":
                tr = prof["time_range"]
                c1_, c2_, c3_ = st.columns(3)
                c1_.markdown(kpi(tr["start"][11:19], tr["start"][:10]), unsafe_allow_html=True)
                c2_.markdown(kpi(tr["end"][11:19], tr["end"][:10]), unsafe_allow_html=True)
                c3_.markdown(kpi(prof.get("bucket", "1min"), "bucket"), unsafe_allow_html=True)
                per_file_t = df.groupby("source").agg(first=("timestamp", "min"), last=("timestamp", "max"), events=("severity", "size")).reset_index()
                st.altair_chart(alt.Chart(per_file_t).mark_bar(cornerRadius=3, height=14).encode(x=alt.X("first:T", title=None, axis=alt.Axis(format="%H:%M")), x2="last:T",
                                y=alt.Y("source:N", title=None), tooltip=["source", "events"]).properties(height=30 * len(per_file_t) + 20), **wide("altair_chart"))
    st.markdown("")
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"#### {t('activity')}")
        st.caption(t("activity_cap"))
        st.altair_chart(minute_chart(prof["per_minute"], a.incidents), **wide("altair_chart"))
        st.markdown(f"#### {t('sev_over_time')}")
        sev_min = df.groupby(["minute", "severity"]).size().rename("events").reset_index()
        st.altair_chart(alt.Chart(sev_min).mark_area(interpolate="monotone").encode(
            x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("events:Q", stack=True, title="events / min"),
            color=alt.Color("severity:N", scale=SEV_SCALE, legend=alt.Legend(orient="top", title=None)),
            order=alt.Order("severity:N"), tooltip=[alt.Tooltip("minute:T", format="%H:%M"), "severity", "events"])
            .properties(height=180).configure_view(strokeWidth=0), **wide("altair_chart"))
    with right:
        st.markdown(f"#### {t('top_services')}")
        svc = df[df.service != "-"].groupby(["service", "severity"]).size().rename("events").reset_index()
        top = svc.groupby("service").events.sum().nlargest(8).index.tolist()
        st.altair_chart(alt.Chart(svc[svc.service.isin(top)]).mark_bar().encode(
            x=alt.X("events:Q", title=None), y=alt.Y("service:N", sort=top, title=None),
            color=alt.Color("severity:N", scale=SEV_SCALE, legend=None), tooltip=["service", "severity", "events"])
            .properties(height=200).configure_view(strokeWidth=0), **wide("altair_chart"))
        st.markdown(f"#### {t('sources')}")
        kinds = df.groupby(["source", "kind"]).size().rename("events").reset_index()
        st.altair_chart(alt.Chart(kinds).mark_arc(innerRadius=45).encode(
            theta="events:Q", color=alt.Color("source:N", legend=alt.Legend(orient="right", title=None)), tooltip=["source", "kind", "events"])
            .properties(height=170).configure_view(strokeWidth=0), **wide("altair_chart"))
    if a.incidents:
        st.markdown(f"#### {t('top_incidents')}")
        for inc in a.incidents[:3]:
            hot = " hot" if inc.severity == "critical" else ""
            st.markdown(f'<div class="card{hot}">{pill(inc.severity)} &nbsp;<b>{inc.id}</b> · score {inc.score} · '
                        f'{inc.started_at:%H:%M}–{inc.ended_at:%H:%M} · {len(inc.signal_ids)} {t("signals_n")} · {", ".join(inc.affected_services) or "-"}'
                        f'<br><span class="mono">{esc(inc.title)}</span><br><span class="muted">{t("origin")} {inc.root_cause_signal}: {reason_text(inc.root_cause_codes)}</span></div>',
                        unsafe_allow_html=True)
    else:
        st.info(t("no_incidents"))

    with st.expander(f"{t('parser')} · {len(a.report)} {t('files_n')}"):
        st.dataframe(pd.DataFrame([{"file": r["file"], "format": r["format"], t("conf"): r["confidence"], t("rows"): r["rows"], "kind": r["kind"],
                                    **{k: (v or "") for k, v in r["roles"].items()}} for r in a.report]),
                     hide_index=True, **wide("dataframe"),
                     column_config={t("conf"): st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
        if prof["relations"]:
            st.dataframe(pd.DataFrame(prof["relations"]), hide_index=True, **wide("dataframe"))
        st.caption(t("mapping_hint"))
        all_keys = sorted({k for r in a.report for k in r["keys"]})
        cur = st.session_state.get("mapping", {})
        with st.form("mapping_form"):
            mc = st.columns(5)
            choice = {}
            for col, role in zip(mc, ("timestamp", "severity", "service", "host", "message")):
                opts = [""] + all_keys
                choice[role] = col.selectbox(role, opts, index=opts.index(cur.get(role, "")) if cur.get(role, "") in opts else 0,
                                             format_func=lambda x: x or t("auto"))
            if st.form_submit_button(t("apply"), **wide("form_submit_button")):
                src = st.session_state["source"]
                load(src["name"], data=src["data"], path=src["path"], mapping={k: v for k, v in choice.items() if v})
                st.rerun()

    # ---- live log stream (replay of the dataset in time order)
    st.markdown(f"#### {t('live')}")
    st.caption(t("live_cap"))
    total = len(a.observations)
    ss = st.session_state
    ss.setdefault("cursor", min(200, total)); ss.setdefault("playing", False)
    b1, b2, b3, b4, b5, b6 = st.columns([1, 1, 1, 1, 2, 2])
    if b1.button(t("pause") if ss.playing else t("play"), **wide("button")):
        ss.playing = not ss.playing; st.rerun()
    if b2.button(t("reset"), **wide("button")):
        ss.cursor, ss.playing = min(200, total), False; st.rerun()
    speed = b3.selectbox(t("speed"), [50, 200, 1000, 5000], index=1, format_func=lambda x: f"{x}/s")
    n_lines = b4.selectbox(t("lines"), [25, 50, 100], index=1)
    live_sev = b5.selectbox(t("min_sev"), list(SEV_RANK), index=0, key="live_sev")
    live_q = b6.text_input(t("filter_text"), key="live_q")

    @st.fragment(run_every="1s" if ss.playing else None)
    def live_panel():
        if ss.playing:
            ss.cursor = min(total, ss.cursor + speed)
            if ss.cursor >= total:
                ss.playing = False
        ss.cursor = st.slider(t("position"), 1, max(total, 1), ss.cursor, label_visibility="collapsed")
        window = a.observations[:ss.cursor]
        q = live_q.lower()
        shown = [o for o in reversed(window) if SEV_RANK[o.severity] >= SEV_RANK[live_sev] and (not q or q in o.message.lower() or q in o.service.lower())][:n_lines]
        upto = window[-1].timestamp.strftime("%H:%M:%S") if window else "-"
        st.caption(t("showing", n=len(shown), total=len(window), time=upto))
        rows = "".join(
            f'<div><span class="muted">{o.timestamp:%H:%M:%S}</span> <span style="color:{SEV_COLORS.get(o.severity, "#8b98ad")};font-weight:700">{o.severity:<8}</span> '
            f'<span style="color:#a5b4c9">{esc(o.service or o.source)[:18]:<18}</span> {esc(o.message)[:160]}</div>' for o in shown)
        st.markdown(f'<div class="card mono" style="max-height:420px;overflow:auto;white-space:pre;line-height:1.55">{rows}</div>', unsafe_allow_html=True)

    live_panel()

# ------------------------------------------------------------------ signals
with tab_sig:
    fc1, fc2, fc3, fc4, fc5 = st.columns([1, 1, 1, 2, 1])
    min_sev = fc1.selectbox(t("min_sev"), list(SEV_RANK), index=0)
    only_burst = fc2.checkbox(t("only_burst"))
    only_meaningful = fc3.checkbox(t("only_meaningful"), value=False)
    svc_filter = fc4.multiselect(t("services"), sorted({s for x in a.signals for s in x.services}))
    sig_env = lambda x: sorted({o.environment or "unknown" for o in x.observations})  # noqa: E731
    env_filter = fc5.multiselect(t("environments"), sorted({e for x in a.signals for e in sig_env(x)}))
    rows = [s for s in a.signals if SEV_RANK[s.severity] >= SEV_RANK[min_sev]
            and (not only_burst or s.burst_score >= 0.5) and (not only_meaningful or interesting(s))
            and (not svc_filter or set(s.services) & set(svc_filter)) and (not env_filter or set(sig_env(s)) & set(env_filter))]
    st.dataframe(pd.DataFrame([{"id": s.id, "severity": s.severity, "template": s.template, "count": s.count, "burst": s.burst_score,
                                "peak/min": s.peak_rate, "base/min": s.baseline_rate, "services": ", ".join(s.services),
                                t("environment"): ", ".join(sig_env(s)), t("origin_col"): ", ".join(sorted({o.origin for o in s.observations if o.origin})[:3]),
                                "onset": s.onset.strftime("%H:%M:%S")} for s in rows]),
                 hide_index=True, **wide("dataframe"), height=min(420, 38 + 35 * max(len(rows), 1)),
                 column_config={"burst": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f"),
                                "count": st.column_config.NumberColumn(format="%d")})
    st.markdown(f"#### {t('why_signal')}")
    if rows:
        sid = st.selectbox(t("signal"), [s.id for s in rows], format_func=lambda i: f"{i} · {a.signal_by_id[i].template[:80]}", label_visibility="collapsed")
        s = a.signal_by_id[sid]
        w = s.why()
        l, r = st.columns([2, 3])
        with l:
            st.markdown(f'{pill(s.severity)} &nbsp;<b>{s.count}</b> {t("events")} · {t("burst")} <b>{s.burst_score}</b> · {t("confidence")} <b>{w["confidence"]}</b>', unsafe_allow_html=True)
            st.markdown(f'<div class="card"><b>{t("reason")}</b><br>{t("reason_fp")}<br><br><b>{t("template")}</b><br><span class="mono">{esc(s.template)}</span><br><br>'
                        f'<b>{t("grouping")}</b><br>{t("severity")} {s.severity} · {t("services").lower()} {", ".join(s.services) or "-"} · {t("hosts")} {", ".join(s.hosts) or "-"}<br>{t("window")} {w["grouping"]["window"]}<br><br>'
                        f'<b>{t("burst").capitalize()}</b><br>{t("burst_detail", peak=f"{s.peak_rate:.0f}", base=f"{s.baseline_rate:.0f}", score=s.burst_score)}</div>', unsafe_allow_html=True)
            per_min = pd.DataFrame({"timestamp": [o.timestamp for o in s.observations]})
            per_min = per_min.set_index(pd.to_datetime(per_min["timestamp"], utc=True)).resample("1min").size().rename("events").reset_index()
            st.altair_chart(minute_chart(per_min, height=120), **wide("altair_chart"))
        with r:
            srcs = Counter(o.source for o in s.observations)
            agents_ = sorted({str(o.attributes.get("agent")) for o in s.observations if o.attributes.get("agent")})
            recs = [recommendation_text(x) for key_, lst in __import__("watchover.scenario", fromlist=["x"]).RECOMMENDATIONS.items() if key_ in s.template for x in lst][:3]
            dur = (s.last_seen - s.first_seen).total_seconds()
            st.markdown(
                f'<div class="card"><b>{t("where")}</b> · {t("sources")}: ' + ", ".join(f"{esc(k)} ({v})" for k, v in srcs.most_common(4)) +
                (f' · {t("agents_n")}: {esc(", ".join(agents_))}' if agents_ else "") + f' · {t("services").lower()}: {esc(", ".join(s.services) or "-")} · {t("hosts")}: {esc(", ".join(s.hosts) or "-")}'
                f' · {t("environment")}: {esc(", ".join(f"{e} ({n})" for e, n in Counter(o.environment or "unknown" for o in s.observations).most_common(3)))}'
                + (f' · {t("origin_col")}: {esc(", ".join(sorted({o.origin for o in s.observations if o.origin})[:3]))}' if any(o.origin for o in s.observations) else "") +
                f'<br><b>{t("why_raised")}</b> · {t("why_fp")}; {t("why_sev", sev=s.severity, n=s.count)}; {t("why_burst", score=s.burst_score, peak=f"{s.peak_rate:.0f}", base=f"{s.baseline_rate:.0f}")}'
                f'<br><b>{t("timing")}</b> · {t("first_signal")} {s.first_seen:%H:%M:%S} · {t("last_error")} {s.last_seen:%H:%M:%S} · {t("duration")} {dur / 60:.1f} min'
                f'<br><b>{t("what_to_do")}</b> · ' + (" · ".join(esc(x) for x in recs) if recs else esc(recommendation_text("Investigate the root-cause signal's evidence lines"))) + "</div>",
                unsafe_allow_html=True)
            picked = evidence_table(s.observations[:200], key=f"sig-ev-{sid}", height=260)
            if picked is not None:
                show_row(picked)

# ------------------------------------------------------------------ incidents
with tab_inc:
    if not a.incidents:
        st.info(t("no_incidents"))
    else:
        ids_ = [i.id for i in a.incidents]
        default = st.session_state.pop("inc_pick", None)
        if default in ids_:
            st.session_state["inc_radio"] = default
        iid = st.radio(t("incident"), ids_, horizontal=True, key="inc_radio",
                       format_func=lambda i: f"{i} · {a.incident_by_id[i].severity} · {a.incident_by_id[i].score}")
        inc = a.incident_by_id[iid]
        incident_flashcard(inc, a, "tab")
        st.markdown(f'### {inc.id} &nbsp;{pill(inc.severity)} &nbsp;<span class="muted">{t("score")} {inc.score} · {inc.started_at:%H:%M:%S} → {inc.ended_at:%H:%M:%S}</span>', unsafe_allow_html=True)
        st.markdown(f'<div class="card hot"><b>{t("probable_origin")}</b> · <span class="mono">{inc.root_cause_signal}</span> "{esc(a.signal_by_id[inc.root_cause_signal].template)}"'
                    f'<br><span class="muted">{t("because")}: {reason_text(inc.root_cause_codes)}</span><br><br>'
                    f'<b>{t("affected")}</b> · {t("services").lower()}: {", ".join(inc.affected_services) or "-"} · {t("hosts")}: {", ".join(inc.affected_hosts) or "-"}</div>', unsafe_allow_html=True)
        og, tm, rc = inc.origin, inc.timing, inc.recovery
        oc, tc = st.columns(2)
        oc.markdown(f'<div class="card"><b>{t("where")}</b><br>{t("sources")}: ' + ", ".join(f"{esc(k)} ({v})" for k, v in list(og.get("files", {}).items())[:5]) +
                    f'<br>{t("services").lower()}: {esc(", ".join(og.get("services", [])) or "-")}<br>{t("hosts")}: {esc(", ".join(og.get("hosts", [])) or "-")}' +
                    (f'<br>{t("agents_n")}: {esc(", ".join(og.get("agents", [])))}' if og.get("agents") else "") +
                    f'<br>{t("environment")}: {esc(", ".join(f"{e} ({n})" for e, n in og.get("environments", {}).items()))}' +
                    (f'<br>{t("origin_col")}: {esc(", ".join(f"{k} ({v})" for k, v in og.get("origins", {}).items()))}' if og.get("origins") else "") +
                    f'<br>parser: {esc(", ".join(og.get("parsers", [])))} · kind: {esc(", ".join(og.get("kinds", [])))}</div>', unsafe_allow_html=True)
        rk = rc.get("kind", "unknown")
        rcol = {"restart": "#2dd4bf", "self_healed": "#2dd4bf", "stopped": "#fbbf24", "ongoing": "#f87171"}.get(rk, "#8b98ad")
        tc.markdown(f'<div class="card" style="border-left:4px solid {rcol}"><b>{t("timing")}</b><br>{t("first_signal")}: <span class="mono">{tm.get("first_signal", "")[11:19]}</span> · '
                    f'{t("last_error")}: <span class="mono">{tm.get("last_error", "")[11:19]}</span> · {t("duration")}: <b>{tm.get("duration_s", 0) / 60:.1f} min</b> ({tm.get("error_count", 0)} ERROR+)'
                    f'<br>{t("quiet")}: <b>{rc.get("quiet_min", 0)} min</b><br><span style="color:{rcol};font-weight:700">{recovery_label(rk)}</span>' +
                    (f'<br>{t("rec_evidence")}: <span class="mono">{esc(rc.get("evidence") or "")}</span> {esc(rc.get("recovered_at") or "")[11:19]} · {esc(rc.get("what") or "")}' if rc.get("evidence") else "") + "</div>",
                    unsafe_allow_html=True)
        playbook_card(a.signal_by_id[inc.root_cause_signal].template, st.session_state.get("dataset", ""))
        st.markdown(f"#### {t('propagation')}")
        rowsdf = pd.DataFrame([{"signal": f"{x['signal']} · {x['template'][:55]}", "start": a.signal_by_id[x["signal"]].first_seen,
                                "end": a.signal_by_id[x["signal"]].last_seen, "severity": x["severity"], "count": x["count"], "role": t(x["role"].replace(" ", "_"))}
                               for x in inc.timeline])
        gantt = alt.Chart(rowsdf).mark_bar(cornerRadius=3, height=16).encode(
            x=alt.X("start:T", title=None, axis=alt.Axis(format="%H:%M")), x2="end:T",
            y=alt.Y("signal:N", sort=list(rowsdf.signal), title=None),
            color=alt.Color("severity:N", scale=alt.Scale(domain=list(SEV_COLORS), range=list(SEV_COLORS.values())), legend=None),
            tooltip=["signal", "severity", "count", "role"]).properties(height=30 * len(rowsdf) + 20)
        st.altair_chart(gantt.configure_view(strokeWidth=0), **wide("altair_chart"))
        l, r = st.columns([2, 3])
        with l:
            st.markdown(f"#### {t('why_score')}")
            fac = pd.DataFrame([{"factor": t("f_" + x.name), "contribution": x.contribution, "detail": factor_value(x), "weight": x.weight} for x in inc.factors])
            st.altair_chart(alt.Chart(fac).mark_bar(color="#2dd4bf").encode(
                x=alt.X("contribution:Q", scale=alt.Scale(domain=[0, max(0.4, fac.contribution.max())]), title=None),
                y=alt.Y("factor:N", sort=None, title=None), tooltip=["factor", "detail", "weight", "contribution"]).properties(height=140), **wide("altair_chart"))
            for x in inc.factors:
                st.markdown(f'<span class="muted">{t("f_" + x.name)}</span> · {factor_value(x)} × w{x.weight} = <b>{x.contribution}</b>', unsafe_allow_html=True)
            st.markdown(f"#### {t('narrative')}")
            st.write(narrative_text(inc, a.signal_by_id))
        with r:
            st.markdown(f"#### {t('timeline')}")
            html = '<div class="tl">' + "".join(
                f'<div class="step {"root" if x["role"] == "root cause" else ""}"><span class="mono">{x["time"][11:19]}</span> {pill(x["severity"])} '
                f'<b>{x["signal"]}</b> ×{x["count"]} <span class="mono">{esc(x["template"][:80])}</span> <span class="muted">· {t(x["role"].replace(" ", "_"))}</span></div>'
                for x in inc.timeline) + "</div>"
            st.markdown(html, unsafe_allow_html=True)
            if inc.links:
                st.markdown(f"**{t('links')}**")
                for e in inc.links[:6]:
                    st.markdown(f'<span class="mono">{e["a"]} ↔ {e["b"]}</span> <span class="muted">{link_text(e)}</span>', unsafe_allow_html=True)
            st.markdown(f"#### {t('evidence')}")
            ev_obs = [a.obs_by_ref[r_] for r_ in inc.evidence if r_ in a.obs_by_ref]
            picked = evidence_table(ev_obs, key=f"inc-ev-{iid}", height=min(300, 38 + 35 * max(len(ev_obs), 1)))
            if picked is not None:
                show_row(picked)
        st.markdown(f"#### {t('recommended')}")
        rc = st.columns(len(inc.recommendations))
        for c, rec in zip(rc, inc.recommendations):
            with c:
                st.markdown(f'<div class="card">{recommendation_text(rec)}</div>', unsafe_allow_html=True)
                if st.button(t("create_action"), key=f"rec-{iid}-{rec[:20]}", **wide("button")):
                    store().create(inc.id, recommendation_text(rec), "P1" if inc.severity == "critical" else "P2", "", rec, inc.root_cause_signal)
                    st.rerun()
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1.expander(t("custom_action")):
            with st.form(f"act-{iid}", clear_on_submit=True):
                title = st.text_input(t("title"))
                pr = st.selectbox(t("priority"), PRIORITIES, index=0 if inc.severity == "critical" else 1)
                owner = st.text_input(t("owner"))
                if st.form_submit_button(t("create")) and title:
                    store().create(inc.id, title, pr, owner, recommendation=title, evidence=inc.root_cause_signal)
                    st.rerun()
        c2.download_button(t("postmortem"), postmortem_md(inc, a.signal_by_id), file_name=f"{inc.id}-postmortem.md", **wide("download_button"))
        with c3.popover(t("llm_prompt"), **wide("popover")):
            st.caption(t("llm_cap"))
            st.code(llm_prompt(inc, a.signal_by_id), language=None)
        if st.button(t("llm_explain"), key=f"llm-{iid}"):
            cfg = llm_cfg()
            if not cfg.enabled:
                st.warning(t("llm_none"))
            else:
                try:
                    with st.spinner("LLM…"):
                        st.session_state[f"llm_out_{iid}"] = llm_chat(cfg, llm_prompt(inc, a.signal_by_id))
                except Exception as e:  # noqa: BLE001
                    st.error(t("llm_fail", e=e))
        if st.session_state.get(f"llm_out_{iid}"):
            st.markdown(f"**{t('llm_answer')}** · {llm_cfg().model}")
            st.markdown(f'<div class="card">{esc(st.session_state[f"llm_out_{iid}"]).replace(chr(10), "<br>")}</div>', unsafe_allow_html=True)
        mine = store().list(inc.id)
        if mine:
            st.markdown(f"**{t('actions_for', id=inc.id)}** ({len(mine)})")
            for act in mine:
                st.markdown(f'<div class="act" style="border-left-color:{PRIO_COLORS[act["priority"]]}"><b>{act["priority"]}</b> · {esc(act["title"])} '
                            f'<span class="muted">· {t("status_" + act["status"])} · {act["owner"] or t("unassigned")}</span></div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ actions
with tab_act:
    st.markdown(f"#### {t('auto_actions')}")
    healed = [i for i in a.incidents if i.recovery.get("kind") in ("restart", "self_healed")]
    if healed:
        for inc in healed:
            rc = inc.recovery
            st.markdown(f'<div class="act" style="border-left-color:#2dd4bf"><b>{inc.id}</b> · {esc(inc.title[:70])}<br>'
                        f'<span class="muted">{recovery_label(rc["kind"])} · {esc(rc.get("recovered_at") or "")[11:19]} · {esc(rc.get("evidence") or "")} · {esc(rc.get("what") or "")}</span></div>', unsafe_allow_html=True)
    else:
        st.caption(t("auto_none"))
    acts = store().list()
    if not acts:
        st.info(t("no_actions"))
    cols = st.columns(len(STATUSES))
    for col, status in zip(cols, STATUSES):
        items = [x for x in acts if x["status"] == status]
        col.markdown(f"#### {t('status_' + status)} <span class='muted'>({len(items)})</span>", unsafe_allow_html=True)
        for act in items:
            with col.container(border=True):
                st.markdown(f'<span class="pill" style="background:{PRIO_COLORS[act["priority"]]}">{act["priority"]}</span> <b>{esc(act["title"])}</b>'
                            f'<br><span class="muted">{act["incident_id"]} · {act["owner"] or t("unassigned")} · {t("evidence").lower()} {act["evidence"] or "-"}<br>{act["updated_at"][:16]}</span>',
                            unsafe_allow_html=True)
                b1, b2 = st.columns([3, 1])
                new = b1.selectbox("status", STATUSES, index=STATUSES.index(status), key=f"st-{act['id']}", label_visibility="collapsed",
                                   format_func=lambda x: t("status_" + x))
                if new != status:
                    store().update(act["id"], status=new); st.rerun()
                if b2.button("✕", key=f"del-{act['id']}"):
                    store().delete(act["id"]); st.rerun()


with tab_noise:
    st.markdown(f"#### {t('tab_noise')} <span class='muted'>· {t('noise_sub')}</span>", unsafe_allow_html=True)
    k = st.columns(5)
    k[0].markdown(kpi2(f"{na['total']:,}", t("raw_events"), "📥", "#60a5fa", ""), unsafe_allow_html=True)
    k[1].markdown(kpi2(len(a.incidents), t("noise_cards"), "🗂", "#2dd4bf", f"{t('inc_cap')} {__import__('watchover.scenario', fromlist=['x']).MAX_INCIDENTS}"), unsafe_allow_html=True)
    k[2].markdown(kpi2(f"{na['on_cards']:,}", t("noise_on_cards"), "📌", "#fb923c", f"{na['on_cards'] / max(1, na['total']):.0%}"), unsafe_allow_html=True)
    k[3].markdown(kpi2(f"{na['eliminated']:,}", t("noise_eliminated"), "🧹", "#8b98ad", f"{na['eliminated'] / max(1, na['total']):.0%}"), unsafe_allow_html=True)
    k[4].markdown(kpi2(f"1 : {na['total'] / max(1, len(a.incidents)):.0f}", t("noise_ratio"), "📉", "#2dd4bf", f"{na['total']} → {len(a.incidents)}"), unsafe_allow_html=True)
    if na["totals"]:
        st.markdown("**" + t("noise_reason") + "** · " + " · ".join(f"{t(k_)}: **{v:,}**" for k_, v in na["totals"].items()), unsafe_allow_html=True)
    if getattr(a, "mode", "") == "density" and a.storm.get("cells") is not None:
        st.markdown(f"**{t('noise_heat')}** <span class='muted'>· {t('noise_heat_sub')}</span>", unsafe_allow_html=True)
        t0, bm = a.storm["t0"], a.storm["bucket_min"]
        counts = Counter((o.service or "-", int((o.timestamp - t0).total_seconds() // (bm * 60))) for o in a.observations)
        hot = a.storm["cells"]
        heat = pd.DataFrame([{"service": svc, "bucket": (t0 + timedelta(minutes=b * bm)).strftime("%H:%M"), "alarms": n, "hot": (svc, b) in hot,
                              "threshold": hot.get((svc, b), {}).get("threshold", None)} for (svc, b), n in counts.items()])
        svc_tot = Counter()
        for (svc, b), n in counts.items():
            svc_tot[svc] += n
        order = [s_ for s_, _ in svc_tot.most_common()]
        base = alt.Chart(heat).encode(x=alt.X("bucket:O", title=None, axis=alt.Axis(labelAngle=0)), y=alt.Y("service:N", sort=order, title=None))
        st.altair_chart((base.mark_rect().encode(color=alt.Color("alarms:Q", scale=alt.Scale(scheme="inferno"), legend=alt.Legend(title=t("env_events"))),
                                                 tooltip=["service", "bucket", "alarms", "hot", "threshold"])
                         + base.transform_filter(alt.datum.hot == True).mark_rect(fill=None, stroke="#2dd4bf", strokeWidth=2.5))
                        .properties(height=22 * max(8, len(order))).configure_view(strokeWidth=0), **wide("altair_chart"))
    rows = na["rows"]
    if rows:
        df_n = pd.DataFrame([{t("signal_id"): r["signal"], t("noise_reason"): t(r["reason"]), t("severity"): r["severity"], t("od_count"): r["count"],
                              t("service"): r["services"], t("hosts"): r["hosts"], t("od_first"): r["first"].strftime("%H:%M"), t("od_last"): r["last"].strftime("%H:%M"),
                              t("od_template"): r["template"]} for r in sorted(rows, key=lambda r: -r["count"])])
        st.dataframe(df_n, hide_index=True, **wide("dataframe"), height=420)
    if a.demoted:
        st.markdown(f"**{t('noise_low_groups')}**")
        st.dataframe(pd.DataFrame([{"id": i.id, t("severity"): i.severity, t("od_count"): sum(a.signal_by_id[x].count for x in i.signal_ids), t("service"): ", ".join(i.affected_services[:4]),
                                    t("od_first"): i.started_at.strftime("%H:%M"), t("od_last"): i.ended_at.strftime("%H:%M"), t("od_template"): i.title[:80]} for i in a.demoted]),
                     hide_index=True, **wide("dataframe"))
