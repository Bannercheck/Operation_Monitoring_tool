"""Signal Sprint dashboard (Streamlit).   streamlit run app.py

Sidebar: upload / demo. Tabs: Overview, Signals, Incidents, Actions. Deterministic engine, no API needed.
"""

from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from signal_sprint.actions import PRIORITIES, STATUSES, ActionStore
from signal_sprint.analysis import Analysis, interesting, llm_prompt, postmortem_md, signal_dict
from signal_sprint.connectors import fetch_http, fetch_mcp, mcp_tools, parse_headers
from signal_sprint.i18n import factor_value, narrative_text, reason_text, recommendation_text, link_text, t
from signal_sprint.models import SEV_RANK
from signal_sprint.pipeline import ingest_bytes, ingest_path
from signal_sprint.profiler import profile

st.set_page_config(page_title="Signal Sprint", page_icon="📡", layout="wide", initial_sidebar_state="expanded")

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

SEV_COLORS = {"CRITICAL": "#ff5c5c", "ERROR": "#ffb347", "WARN": "#f2d55c", "INFO": "#6b7a90", "DEBUG": "#4a5361",
              "critical": "#ff5c5c", "high": "#ffb347", "medium": "#f2d55c", "low": "#6b7a90"}
PRIO_COLORS = {"P1": "#ff5c5c", "P2": "#ffb347", "P3": "#f2d55c", "P4": "#6b7a90"}
CSS = """
<style>
.block-container{padding-top:1.2rem;padding-bottom:2rem}
.pill{display:inline-block;padding:1px 9px;border-radius:10px;font-size:11px;font-weight:700;color:#000;letter-spacing:.3px}
.card{background:var(--secondary-background-color);border:1px solid #262b33;border-radius:12px;padding:14px 16px;margin-bottom:10px}
.card.hot{border-color:#ff5c5c}
.kpi{background:var(--secondary-background-color);border:1px solid #262b33;border-radius:12px;padding:12px 14px;text-align:center}
.kpi b{display:block;font-size:30px;line-height:1.1}.kpi span{color:#8b93a1;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.arrow{text-align:center;color:#3ddc84;font-size:26px;padding-top:18px}
.tile div[data-testid="stButton"] button{width:100%;margin-top:-6px;padding:2px;font-size:11px;background:transparent;color:#8b93a1;border:0}
.tile div[data-testid="stButton"] button:hover{color:#3ddc84}
.muted{color:#8b93a1}.mono{font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
.tl{border-left:2px solid #262b33;margin-left:6px;padding-left:14px}.tl .step{position:relative;margin-bottom:8px}
.tl .step:before{content:"";position:absolute;left:-19px;top:7px;width:8px;height:8px;border-radius:4px;background:#8b93a1}
.tl .step.root:before{background:#ff5c5c;box-shadow:0 0 0 3px rgba(255,92,92,.25)}
.act{background:#0f1216;border:1px solid #262b33;border-left:4px solid #8b93a1;border-radius:8px;padding:8px 10px;margin-bottom:8px}
</style>"""
st.markdown(CSS, unsafe_allow_html=True)


esc = html.escape


def pill(s: str) -> str:
    return f'<span class="pill" style="background:{SEV_COLORS.get(s, "#6b7a90")}">{s}</span>'


def kpi(value, label) -> str:
    return f'<div class="kpi"><b>{value}</b><span>{label}</span></div>'


@st.cache_resource
def store() -> ActionStore:
    return ActionStore(os.environ.get("ACTIONS_DB", "actions.db"))


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
    st.session_state.update({"analysis": analysis, "profile": prof, "dataset": name,
                             "source": {"name": name, "data": data, "path": path}, "mapping": mapping or {},
                             "elapsed": time.perf_counter() - t0})
    st.session_state.pop("cursor", None)


def minute_chart(df: pd.DataFrame, incidents=None, height=200):
    base = alt.Chart(df).mark_bar(color="#3ddc84", opacity=0.85).encode(
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


# ------------------------------------------------------------------ sidebar (minimal: language, upload, demo)
demo = Path(__file__).with_name("samples") / "demo_mixed.zip"
with st.sidebar:
    st.markdown("## 📡 Signal Sprint")
    st.radio("Language", ["tr", "en"], horizontal=True, label_visibility="collapsed",
             format_func=lambda x: {"tr": "🇹🇷 Türkçe", "en": "🇬🇧 English"}[x], key="lang")
    up = st.file_uploader(t("upload"), type=None, key="up_side")
    if up is not None and st.session_state.get("dataset") != up.name:
        load(up.name, data=up.getvalue())
        st.rerun()
    if demo.exists() and st.button(t("load_demo"), key="demo_side", **wide("button")):
        load(demo.name, path=str(demo))
        st.rerun()
    with st.expander(t("conn")):
        kind = st.radio(t("conn_type"), ["http", "mcp"], horizontal=True, format_func=lambda x: t("conn_" + x), key="conn_kind")
        c_url = st.text_input(t("conn_url"), key="conn_url", placeholder="https://api.example.com/alerts" if kind == "http" else "http://localhost:8765/mcp")
        c_headers = st.text_area(t("conn_headers"), key="conn_headers", height=68, placeholder="Authorization: Bearer …")
        if kind == "http":
            c_method = st.selectbox(t("conn_method"), ["GET", "POST"], key="conn_method")
            c_body = st.text_area(t("conn_body"), key="conn_body", height=68) if c_method == "POST" else ""
            c_path = st.text_input(t("conn_path"), key="conn_path")
        else:
            st.caption(t("conn_mcp_hint"))
            if st.button(t("conn_list_tools"), key="conn_tools", **wide("button")) and c_url:
                try:
                    st.session_state["conn_toolnames"] = [x["name"] for x in mcp_tools(c_url, parse_headers(c_headers))]
                except Exception as e:  # noqa: BLE001
                    st.error(t("conn_err", e=e))
            names = st.session_state.get("conn_toolnames", [])
            c_tool = st.selectbox(t("conn_tool"), names, key="conn_tool") if names else st.text_input(t("conn_tool"), key="conn_tool_txt")
            c_args = st.text_input(t("conn_args"), key="conn_args", value="{}")
        if st.button(t("conn_fetch"), key="conn_go", **wide("button")) and c_url:
            try:
                if kind == "http":
                    name, data = fetch_http(c_url, c_method, parse_headers(c_headers), c_body or None, c_path)
                else:
                    name, data = fetch_mcp(c_url, c_tool, json.loads(c_args or "{}"), parse_headers(c_headers))
                st.success(t("conn_ok", n=f"{len(data):,}", name=name))
                load(name, data=data)
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.error(t("conn_err", e=e))
    if "analysis" in st.session_state:
        st.caption(f"{st.session_state['dataset']} · {st.session_state.get('elapsed', 0):.1f}s")
    st.caption(t("footer"))

if "analysis" not in st.session_state:
    st.markdown(f"## {t('landing_title')}")
    up_main = st.file_uploader(t("upload"), type=None, key="up_main", label_visibility="collapsed")
    if up_main is not None:
        load(up_main.name, data=up_main.getvalue())
        st.rerun()
    c1, c2 = st.columns([1, 5])
    if demo.exists() and c1.button(t("load_demo"), key="demo_main", **wide("button")):
        load(demo.name, path=str(demo))
        st.rerun()
    c2.markdown(
        '<div style="display:flex;gap:10px;align-items:center;padding:6px 0">' + "".join(
            f'<span class="card" style="margin:0;padding:8px 14px;white-space:nowrap">{icon} {t(k)}</span>' + ('<span class="arrow" style="padding:0">→</span>' if i < 3 else "")
            for i, (icon, k) in enumerate((("📥", "step1"), ("🧹", "step2"), ("🔗", "step3"), ("✅", "step4")))) + "</div>",
        unsafe_allow_html=True)
    st.stop()

a: Analysis = st.session_state["analysis"]
prof = st.session_state["profile"]
f = a.funnel()
tab_over, tab_sig, tab_inc, tab_act = st.tabs([t("tab_overview"), f"{t('tab_signals')} · {f['fingerprints']}", f"{t('tab_incidents')} · {f['incidents']}", f"{t('tab_actions')} · {len(store().list())}"])

# ------------------------------------------------------------------ overview
SEV_SCALE = alt.Scale(domain=list(SEV_RANK), range=[SEV_COLORS[k] for k in SEV_RANK])


@st.cache_data(show_spinner=False)
def frames(dataset: str, n: int):
    """DataFrames for the overview charts, cached per loaded dataset."""
    a_ = st.session_state["analysis"]
    df = pd.DataFrame([{"timestamp": o.timestamp, "severity": o.severity, "service": o.service or "-", "host": o.host or "-",
                        "kind": o.kind, "source": o.source, "message": o.message} for o in a_.observations])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("min")
    return df


with tab_over:
    def tile(col, key: str, value, label: str) -> None:
        with col:
            st.markdown('<div class="tile">' + kpi(value, label) + "</div>", unsafe_allow_html=True)
            if st.button(t("detail"), key=f"tile-{key}", **wide("button")):
                st.session_state["detail"] = None if st.session_state.get("detail") == key else key
                st.rerun()

    cols = st.columns([3, 1, 3, 1, 3, 1, 3, 1, 3])
    steps = [("raw_events", f"{f['raw_events']:,}"), ("fingerprints", f["fingerprints"]), ("meaningful", f["meaningful_signals"]),
             ("incidents", f["incidents"]), ("actions", len(store().list()))]
    for i, (key, v) in enumerate(steps):
        tile(cols[i * 2], key, v, t(key))
        if i < 4:
            cols[i * 2 + 1].markdown('<div class="arrow">→</div>', unsafe_allow_html=True)
    df = frames(st.session_state["dataset"], len(a.observations))
    tiles = st.columns(6)
    for c, (key, v) in zip(tiles, [("files_n", len(prof["files"])), ("records_n", f"{prof['records']:,}"), ("services_n", len(prof["services"])),
                                   ("hosts_n", len(prof["hosts"])), ("error_classes", prof["error_classes"]), ("span_min", prof["time_range"]["minutes"])]):
        tile(c, key, v, t(key))

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
                st.dataframe(pd.DataFrame([{"id": i.id, "severity": i.severity, "score": i.score, "title": i.title, "signals": len(i.signal_ids),
                                            "services": ", ".join(i.affected_services), "window": f"{i.started_at:%H:%M}–{i.ended_at:%H:%M}"} for i in a.incidents]),
                             hide_index=True, **wide("dataframe"), column_config={"score": st.column_config.ProgressColumn(min_value=0, max_value=1)})
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
            f'<div><span class="muted">{o.timestamp:%H:%M:%S}</span> <span style="color:{SEV_COLORS.get(o.severity, "#8b93a1")};font-weight:700">{o.severity:<8}</span> '
            f'<span style="color:#9fb3c8">{esc(o.service or o.source)[:18]:<18}</span> {esc(o.message)[:160]}</div>' for o in shown)
        st.markdown(f'<div class="card mono" style="max-height:420px;overflow:auto;white-space:pre;line-height:1.55">{rows}</div>', unsafe_allow_html=True)

    live_panel()

# ------------------------------------------------------------------ signals
with tab_sig:
    fc1, fc2, fc3, fc4 = st.columns([1, 1, 1, 2])
    min_sev = fc1.selectbox(t("min_sev"), list(SEV_RANK), index=0)
    only_burst = fc2.checkbox(t("only_burst"))
    only_meaningful = fc3.checkbox(t("only_meaningful"), value=False)
    svc_filter = fc4.multiselect(t("services"), sorted({s for x in a.signals for s in x.services}))
    rows = [s for s in a.signals if SEV_RANK[s.severity] >= SEV_RANK[min_sev]
            and (not only_burst or s.burst_score >= 0.5) and (not only_meaningful or interesting(s))
            and (not svc_filter or set(s.services) & set(svc_filter))]
    st.dataframe(pd.DataFrame([{"id": s.id, "severity": s.severity, "template": s.template, "count": s.count, "burst": s.burst_score,
                                "peak/min": s.peak_rate, "base/min": s.baseline_rate, "services": ", ".join(s.services),
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
            st.markdown(f"**{t('evidence_first', n=min(12, s.count), total=s.count)}**")
            st.dataframe(pd.DataFrame([{"ref": o.ref, "time": o.timestamp.strftime("%H:%M:%S"), "sev": o.severity, "service": o.service, "message": o.message}
                                       for o in s.observations[:12]]), hide_index=True, **wide("dataframe"), height=460)

# ------------------------------------------------------------------ incidents
with tab_inc:
    if not a.incidents:
        st.info(t("no_incidents"))
    else:
        iid = st.radio(t("incident"), [i.id for i in a.incidents], horizontal=True,
                       format_func=lambda i: f"{i} · {a.incident_by_id[i].severity} · {a.incident_by_id[i].score}")
        inc = a.incident_by_id[iid]
        st.markdown(f'### {inc.id} &nbsp;{pill(inc.severity)} &nbsp;<span class="muted">{t("score")} {inc.score} · {inc.started_at:%H:%M:%S} → {inc.ended_at:%H:%M:%S}</span>', unsafe_allow_html=True)
        st.markdown(f'<div class="card hot"><b>{t("probable_origin")}</b> · <span class="mono">{inc.root_cause_signal}</span> "{esc(a.signal_by_id[inc.root_cause_signal].template)}"'
                    f'<br><span class="muted">{t("because")}: {reason_text(inc.root_cause_codes)}</span><br><br>'
                    f'<b>{t("affected")}</b> · {t("services").lower()}: {", ".join(inc.affected_services) or "-"} · {t("hosts")}: {", ".join(inc.affected_hosts) or "-"}</div>', unsafe_allow_html=True)
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
            st.altair_chart(alt.Chart(fac).mark_bar(color="#3ddc84").encode(
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
            ref = st.selectbox(t("open_raw"), inc.evidence, key=f"ev-{iid}", label_visibility="collapsed")
            o = a.obs_by_ref[ref]
            st.code(f"{o.ref}   {o.timestamp.isoformat()}   {o.severity}   {o.service} {o.host}\n{o.message}\n{o.attributes or ''}", language=None)
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
        mine = store().list(inc.id)
        if mine:
            st.markdown(f"**{t('actions_for', id=inc.id)}** ({len(mine)})")
            for act in mine:
                st.markdown(f'<div class="act" style="border-left-color:{PRIO_COLORS[act["priority"]]}"><b>{act["priority"]}</b> · {esc(act["title"])} '
                            f'<span class="muted">· {t("status_" + act["status"])} · {act["owner"] or t("unassigned")}</span></div>', unsafe_allow_html=True)

# ------------------------------------------------------------------ actions
with tab_act:
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
