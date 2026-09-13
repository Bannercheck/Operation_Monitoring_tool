"""Signal Sprint dashboard (Streamlit).   streamlit run app.py

Sidebar: upload. Tabs: Profile, Signals, Incidents, Actions. Deterministic engine; no API needed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from signal_sprint.actions import PRIORITIES, STATUSES, ActionStore
from signal_sprint.analysis import Analysis, llm_prompt, postmortem_md, signal_dict
from signal_sprint.pipeline import ingest_bytes, ingest_path
from signal_sprint.profiler import profile, profile_text

st.set_page_config(page_title="Signal Sprint", page_icon="📡", layout="wide")
SEV_COLOR = {"CRITICAL": "red", "critical": "red", "ERROR": "orange", "high": "orange", "WARN": "blue", "medium": "blue",
             "INFO": "gray", "low": "gray", "DEBUG": "gray"}


def badge(s: str) -> str:
    return f":{SEV_COLOR.get(s, 'gray')}[**{s}**]"


@st.cache_resource
def store() -> ActionStore:
    return ActionStore(os.environ.get("ACTIONS_DB", "actions.db"))


def load(name: str, data: bytes | None = None, path: str | None = None) -> None:
    obs, report = ingest_bytes(name, data) if data is not None else ingest_path(path)
    st.session_state["analysis"] = Analysis(obs, report)
    st.session_state["profile"] = profile(obs, report)
    st.session_state["dataset"] = name


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.title("📡 Signal Sprint")
    st.caption("noise → signals → explained incidents → tracked actions")
    up = st.file_uploader("Upload dataset (file / ZIP / TAR.GZ, up to 1 GB)", type=None)
    if up is not None and st.session_state.get("dataset") != up.name:
        with st.spinner("Analyzing…"):
            load(up.name, data=up.getvalue())
    demo = Path(__file__).with_name("samples") / "demo_mixed.zip"
    if demo.exists() and st.button("Load demo dataset"):
        with st.spinner("Analyzing…"):
            load(demo.name, path=str(demo))
    if "analysis" in st.session_state:
        f = st.session_state["analysis"].funnel()
        st.divider()
        st.markdown(f"**{st.session_state['dataset']}**")
        st.markdown(f"{f['raw_events']:,} raw events  \n↓ {f['fingerprints']} fingerprints  \n↓ {f['meaningful_signals']} meaningful signals  "
                    f"\n↓ {f['incidents']} incidents  \n↓ {len(store().list())} actions")

if "analysis" not in st.session_state:
    st.info("Upload a dataset or load the demo from the sidebar.")
    st.stop()

a: Analysis = st.session_state["analysis"]
prof = st.session_state["profile"]
tab_profile, tab_signals, tab_incidents, tab_actions = st.tabs(["Profile", "Signals", "Incidents", "Actions"])

# ------------------------------------------------------------------ profile
with tab_profile:
    f = a.funnel()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Raw events", f["raw_events"]); c2.metric("Signals", f["fingerprints"])
    c3.metric("Incidents", f["incidents"]); c4.metric("Noise reduction", f"{f['reduction']}×")
    st.code(profile_text(prof), language=None)
    st.subheader("Events per minute")
    st.bar_chart(prof["per_minute"].set_index("timestamp")["events"], height=180)
    l, r = st.columns(2)
    l.subheader("Files"); l.dataframe(pd.DataFrame(prof["files"]), hide_index=True, width="stretch")
    r.subheader("Severity mix"); r.bar_chart(pd.Series(prof["severity"]), height=180)
    if prof["relations"]:
        st.subheader("Suggested relations"); st.dataframe(pd.DataFrame(prof["relations"]), hide_index=True)

# ------------------------------------------------------------------ signals
with tab_signals:
    st.subheader(f"Signals ({len(a.signals)})")
    st.dataframe(pd.DataFrame([signal_dict(s) for s in a.signals]), hide_index=True, width="stretch",
                 column_config={"burst": st.column_config.ProgressColumn(min_value=0, max_value=1)})
    sid = st.selectbox("WHY THIS SIGNAL?", [s.id for s in a.signals], format_func=lambda i: f"{i}  {a.signal_by_id[i].template[:70]}")
    s = a.signal_by_id[sid]
    w = s.why()
    l, r = st.columns([1, 2])
    with l:
        st.markdown(f"{badge(s.severity)} **{s.count}** events · burst **{s.burst_score}** · confidence **{w['confidence']}**")
        st.json({k: v for k, v in w.items() if k != "template"}, expanded=True)
    with r:
        st.markdown("**Evidence (first 10)**")
        for o in s.observations[:10]:
            st.markdown(f"`{o.ref}` {o.timestamp:%H:%M:%S} {badge(o.severity)} {o.message[:160]}")

# ------------------------------------------------------------------ incidents
with tab_incidents:
    st.subheader(f"Incident candidates ({len(a.incidents)})")
    if not a.incidents:
        st.info("No incident candidates: nothing bursts or correlates above the thresholds.")
    else:
        st.dataframe(pd.DataFrame([{"id": i.id, "severity": i.severity, "score": i.score, "title": i.title,
                                    "signals": len(i.signal_ids), "services": ", ".join(i.affected_services),
                                    "window": f"{i.started_at:%H:%M}–{i.ended_at:%H:%M}"} for i in a.incidents]),
                     hide_index=True, width="stretch",
                     column_config={"score": st.column_config.ProgressColumn(min_value=0, max_value=1)})
        iid = st.selectbox("Incident", [i.id for i in a.incidents], format_func=lambda i: f"{i}  {a.incident_by_id[i].title[:70]}")
        inc = a.incident_by_id[iid]
        st.markdown(f"### {inc.id} {badge(inc.severity)} score **{inc.score}**")
        st.write(inc.narrative)
        st.markdown(f"**Probable origin:** `{inc.root_cause_signal}` — {inc.root_cause_reason}")
        l, r = st.columns(2)
        with l:
            st.markdown("**Why this score**")
            st.bar_chart(pd.DataFrame({"contribution": [f.contribution for f in inc.factors]}, index=[f.name for f in inc.factors]), height=180)
            for f_ in inc.factors:
                st.caption(f"{f_.name}: {f_.value} × w{f_.weight} = {f_.contribution}")
        with r:
            st.markdown("**Timeline**")
            for t in inc.timeline:
                mark = "🔴" if t["role"] == "root cause" else "↓"
                st.markdown(f"{mark} `{t['time'][11:19]}` {badge(t['severity'])} **{t['signal']}** ×{t['count']} `{t['template'][:70]}` _{t['role']}_")
        st.markdown("**Evidence**")
        ref = st.selectbox("Open evidence line", inc.evidence, key=f"ev-{iid}")
        o = next(o for o in a.observations if o.ref == ref)
        st.code(f"{o.ref}  {o.timestamp.isoformat()}  {o.severity}  {o.service} {o.host}\n{o.message}\n{o.attributes}", language=None)
        st.markdown("**Recommendations**")
        for rec in inc.recommendations:
            st.markdown(f"- {rec}")
        c1, c2, c3 = st.columns(3)
        with c1.form(f"act-{iid}", clear_on_submit=True):
            st.markdown("**Create action**")
            title = st.text_input("Title", value=inc.recommendations[0])
            pr = st.selectbox("Priority", PRIORITIES, index=0 if inc.severity == "critical" else 1)
            owner = st.text_input("Owner")
            if st.form_submit_button("Create") and title:
                store().create(inc.id, title, pr, owner, recommendation=title, evidence=inc.root_cause_signal)
                st.success("Action created"); st.rerun()
        c2.download_button("Export postmortem (.md)", postmortem_md(inc, a.signal_by_id), file_name=f"{inc.id}-postmortem.md")
        with c3.expander("LLM enrichment prompt (paste into Claude SAKA)"):
            st.code(llm_prompt(inc, a.signal_by_id), language=None)
        st.markdown("**Actions for this incident**")
        for act in store().list(inc.id):
            st.markdown(f"- [{act['priority']}] {act['title']} — _{act['status']}_ · {act['owner'] or 'unassigned'}")

# ------------------------------------------------------------------ actions
with tab_actions:
    acts = store().list()
    st.subheader(f"Actions ({len(acts)})")
    cols = st.columns(len(STATUSES))
    for col, status in zip(cols, STATUSES):
        col.markdown(f"**{status.replace('_', ' ').title()}** ({sum(1 for x in acts if x['status'] == status)})")
        for act in [x for x in acts if x["status"] == status]:
            with col.container(border=True):
                st.markdown(f"**[{act['priority']}] {act['title']}**  \n{act['incident_id']} · {act['owner'] or 'unassigned'}  \n_{act['updated_at'][:16]}_")
                new = st.selectbox("status", STATUSES, index=STATUSES.index(status), key=f"st-{act['id']}", label_visibility="collapsed")
                if new != status:
                    store().update(act["id"], status=new); st.rerun()
                if st.button("delete", key=f"del-{act['id']}"):
                    store().delete(act["id"]); st.rerun()
