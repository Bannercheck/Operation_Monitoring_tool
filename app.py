"""Watchover dashboard (Streamlit).   streamlit run app.py

Sidebar: upload / demo. Tabs: Overview, Signals, Incidents, Actions. Deterministic engine, no API needed.
"""

from __future__ import annotations

from collections import Counter
import html
import re
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().with_name("src")))   # the repo's engine always wins over a stale pip install

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
from watchover.knowledge import Knowledge, RULE_KINDS
from watchover.agents import AgentRegistry
from watchover import settings as wo_settings
from watchover import assistant as wo_assistant
from watchover import sources as wo_sources
from watchover import report as wo_report
from watchover.llm import LLMConfig, PROVIDERS, chat as llm_chat, embed as llm_embed, list_models as llm_models, set_sink as llm_set_sink, test_connection as llm_test
from watchover import ollama as wo_ollama
from watchover import llm_eval as wo_eval
from watchover.i18n import current_lang, factor_value, narrative_text, reason_text, recommendation_text, link_text, t
from watchover.models import SEV_RANK
from watchover.pipeline import ingest_bytes, ingest_path
from watchover.profiler import profile
from watchover import stamp as wo_stamp

if "cfg_loaded" not in st.session_state:                      # persisted settings become the session's defaults, once
    _cfg = wo_settings.load()
    for _k in wo_settings.SESSION_KEYS:
        if _k not in st.session_state and _cfg.get(_k) not in (None, ""):
            st.session_state[_k] = _cfg[_k]
    st.session_state["cfg_loaded"] = True
ASSETS = Path(__file__).with_name("assets")
LOGO_PNG = str(ASSETS / "logo-64.png")
LOGO_SVG = (ASSETS / "logo.svg").read_text(encoding="utf-8") if (ASSETS / "logo.svg").exists() else ""


def logo(size: int) -> str:
    """The SVG mark at a given pixel size (inline, so it renders inside markdown)."""
    return LOGO_SVG.replace('width="64" height="64"', f'width="{size}" height="{size}"')
st.set_page_config(page_title="Watchover", page_icon=LOGO_PNG if (ASSETS / "logo-64.png").exists() else "📡", layout="wide", initial_sidebar_state="expanded")

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
.funnel{background:linear-gradient(180deg,#182234 0%,#111827 100%);border:1px solid var(--wo-border);border-radius:16px;padding:14px 18px 14px;margin:4px 0 12px;box-shadow:inset 0 1px 0 rgba(255,255,255,.03),0 14px 34px -24px rgba(0,0,0,.95)}
.funnel .head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:10px;flex-wrap:wrap}
.funnel .ttl{font-size:13px;font-weight:700;letter-spacing:.2px;color:#eef2f7}
.funnel .stages{display:flex;align-items:stretch}
.funnel .stage{flex:1 1 0;min-width:0;padding:6px 16px 4px;position:relative}
.funnel .stage:first-child{padding-left:2px}.funnel .stage+.stage{border-left:1px solid var(--wo-border)}
.funnel .stage+.stage:after{content:"›";position:absolute;left:-6px;top:38%;color:#3b4b66;font-size:20px;line-height:1;background:transparent}
.funnel .lb{display:flex;align-items:center;gap:7px;font-size:10.5px;font-weight:700;letter-spacing:.8px;color:var(--wo-muted);white-space:nowrap;overflow:hidden}
.funnel .lb .ic{margin-left:auto;font-size:15px;opacity:.9}
.funnel .dot{flex:none;width:8px;height:8px;border-radius:4px;background:var(--acc);box-shadow:0 0 0 3px rgba(255,255,255,.06)}
.funnel .v{font-size:34px;font-weight:800;letter-spacing:-.03em;color:#f4f7fb;line-height:1.1;margin:8px 0 2px;font-variant-numeric:tabular-nums}
.funnel .sub{font-size:12px;color:var(--acc);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.funnel .bar{height:5px;border-radius:3px;background:linear-gradient(90deg,var(--acc),transparent);margin-top:12px;opacity:.9}
.facts{display:flex;flex-wrap:wrap;background:var(--wo-surface2);border:1px solid var(--wo-border);border-radius:14px;padding:4px 6px;margin-bottom:10px}
.facts .fact{flex:1 1 0;min-width:140px;padding:10px 14px;position:relative}
.facts .fact+.fact:before{content:"";position:absolute;left:0;top:14px;bottom:14px;width:1px;background:var(--wo-border)}
.facts .lb{font-size:10.5px;letter-spacing:.7px;color:var(--wo-muted);font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.facts .v{font-size:21px;font-weight:700;color:#eef2f7;margin:3px 0 1px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.facts .sub{font-size:11.5px;color:var(--wo-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
div[data-testid="stPills"] button{border-radius:999px}
[role="radiogroup"][aria-orientation="horizontal"]{flex-wrap:wrap;row-gap:6px}
.st-key-page div[role="radiogroup"]{gap:2px}
.st-key-page label[data-testid="stRadioOption"]{display:flex;align-items:center;padding:8px 12px;border-radius:10px;margin:0;width:100%;cursor:pointer;border:1px solid transparent;transition:background .12s,border-color .12s}
.st-key-page label[data-testid="stRadioOption"]:hover{background:rgba(255,255,255,.045)}
.st-key-page label[data-testid="stRadioOption"][data-selected="true"]{background:rgba(45,212,191,.10);border-color:rgba(45,212,191,.28)}
.st-key-page label[data-testid="stRadioOption"][data-selected="true"] p{color:#e6fffa;font-weight:600}
.st-key-page label[data-testid="stRadioOption"] > div > div > div:first-child{display:none}
.st-key-page label[data-testid="stRadioOption"] p{font-size:14px;color:#c7d0dd;margin:0}
.st-key-lang [data-testid="stRadioGroup"]{gap:6px}
.st-key-lang label[data-testid="stRadioOption"]{padding:2px 12px;border-radius:999px;border:1px solid var(--wo-border);margin:0;cursor:pointer}
.st-key-lang label[data-testid="stRadioOption"] > div > div > div:first-child{display:none}
.st-key-lang label[data-testid="stRadioOption"][data-selected="true"]{background:rgba(96,165,250,.14);border-color:rgba(96,165,250,.5)}
.st-key-lang label[data-testid="stRadioOption"] p{font-size:12px;font-weight:700;letter-spacing:.6px;margin:0}
.k2.live{min-height:150px;padding-bottom:6px}
.k2 .spark{display:block;width:100%;height:36px;margin-top:8px;overflow:visible}
.k2 .spark.empty{height:36px;margin-top:8px;border-top:1px dashed var(--wo-border)}
.k2 .pulse{animation:wo-pulse 1.6s ease-out infinite;transform-origin:center;transform-box:fill-box}
@keyframes wo-pulse{0%{opacity:1;r:2.2}70%{opacity:.25;r:4.5}100%{opacity:1;r:2.2}}
.k2 .livedot{position:absolute;left:14px;top:12px;width:7px;height:7px;border-radius:4px;background:var(--acc);box-shadow:0 0 0 0 var(--acc);animation:wo-ring 2s ease-out infinite}
.k2.live .lb{padding-left:14px}
@keyframes wo-ring{0%{box-shadow:0 0 0 0 rgba(255,255,255,.35)}100%{box-shadow:0 0 0 7px rgba(255,255,255,0)}}
.chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:2px 0 10px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:3px 11px;border-radius:999px;border:1px solid var(--wo-border);background:rgba(255,255,255,.03);font-size:12px;color:#c7d0dd;white-space:nowrap}
.chip .d{width:7px;height:7px;border-radius:4px;flex:none}
mark{background:rgba(251,191,36,.35);color:#fff;border-radius:3px;padding:0 2px}
.facet-h{font-size:10.5px;letter-spacing:.8px;color:#5f6d84;font-weight:700;margin:4px 0 0}
.rowcard{background:#111827;border:1px solid var(--wo-border);border-left:4px solid var(--wo-accent);border-radius:12px;padding:10px 14px;margin:6px 0 8px;font-size:13px;line-height:1.6}
.sb-cap{font-size:10.5px;letter-spacing:.9px;color:#5f6d84;font-weight:700;margin:16px 0 4px 4px}
.sb-status{background:rgba(255,255,255,.03);border:1px solid var(--wo-border);border-radius:12px;padding:10px 12px;font-size:12px;color:#aab4c5;line-height:1.8}
.sb-status .dot{display:inline-block;width:7px;height:7px;border-radius:4px;margin-right:8px;vertical-align:middle}
section[data-testid="stSidebar"] hr{margin:10px 0}
div[data-testid="stDialog"] [data-testid="stMarkdownContainer"] h4{margin-top:0}
div[data-testid="stExpander"] details{border:1px solid var(--wo-border);border-radius:12px;background:var(--wo-surface)}
</style>"""
st.markdown(CSS, unsafe_allow_html=True)


esc = html.escape


def pill(s: str) -> str:
    return f'<span class="pill" style="background:{SEV_COLORS.get(s, "#64748b")}">{s}</span>'


def upper(s: str) -> str:
    """Locale-aware upper case for labels: Turkish i -> İ (CSS text-transform gives ILIŞKI instead of İLİŞKİ)."""
    return str(s).replace("i", "İ").upper() if current_lang() == "tr" else str(s).upper()


def cap(s: str) -> str:
    """First letter upper-cased the Turkish way (i -> İ)."""
    return (upper(s[0]) + s[1:]) if s else s


def kpi(value, label) -> str:
    return f'<div class="kpi"><b>{value}</b><span>{upper(label)}</span></div>'


def sparkline(series: list, accent: str, ymax: float | None = None, ymin: float = 0.0, target: float | None = None) -> str:
    """Inline SVG: area + line of the last N points, a pulsing dot on the newest one, an optional dashed target line."""
    pts = [v for v in series if v is not None]
    if len(pts) < 2:
        return '<div class="spark empty"></div>'
    lo = ymin if ymin is not None else min(pts)
    hi = ymax if ymax is not None else max(pts)
    if hi <= lo:
        hi = lo + 1
    n = len(series)
    coords = []
    for i, v in enumerate(series):
        if v is None:
            continue
        x = 2 + 96 * i / max(1, n - 1)
        y = 30 - 26 * (min(max(v, lo), hi) - lo) / (hi - lo)
        coords.append((x, y))
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"M{coords[0][0]:.1f},31 L" + line.replace(" ", " L") + f" L{coords[-1][0]:.1f},31 Z"
    tgt = ""
    if target is not None and lo <= target <= hi:
        ty = 30 - 26 * (target - lo) / (hi - lo)
        tgt = f'<line x1="2" y1="{ty:.1f}" x2="98" y2="{ty:.1f}" stroke="#f87171" stroke-width=".8" stroke-dasharray="2 2" opacity=".8"/>'
    lx, ly = coords[-1]
    return (f'<svg class="spark" viewBox="0 0 100 34" preserveAspectRatio="none"><path d="{area}" fill="{accent}" opacity=".16"/>'
            f'<polyline points="{line}" fill="none" stroke="{accent}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>{tgt}'
            f'<circle class="pulse" cx="{lx:.1f}" cy="{ly:.1f}" r="2.2" fill="{accent}"/></svg>')


def live_tile(value, label, icon: str, accent: str, sub: str, series: list, ymax=None, ymin=0.0, target=None, muted: bool = False) -> str:
    """kpi2 with a live sparkline and a 'live' pulse in the corner (the surrounding fragment refreshes every 2 s)."""
    return (f'<div class="k2 live" style="--acc:{accent}"><span class="ic">{icon}</span><span class="livedot" title="live"></span><div class="lb">{upper(label)}</div>'
            f'<span class="v{" s" if len(str(value)) > 6 else ""}">{value}</span><div class="sub{" m" if muted else ""}">{esc(str(sub))}</div>'
            f'{sparkline(series, accent, ymax, ymin, target)}</div>')


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
def _singletons() -> dict:
    return {}


def _singleton(key: str, cls, make):
    """One instance per process. Rebuilt when the class object changed, i.e. the module was hot-reloaded while developing:
    a cached instance of the old class would lack methods added since (AttributeError on the settings page)."""
    h = _singletons()
    obj = h.get(key)
    if type(obj) is not cls:
        obj = h[key] = make()
    return obj


def knowledge() -> Knowledge:
    def make():
        kb = Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db"))
        kb.apply_rules()                               # approved rules shape the engine from the first analysis on
        return kb
    return _singleton("kb", Knowledge, make)


def kb_with_embedder() -> Knowledge:
    """The knowledge base, with the session's embedding model attached when one is configured."""
    kb = knowledge()
    llm_set_sink(kb.log_call)                        # every LLM call lands in the quality log
    cfg = llm_cfg()
    kb.embedder = (lambda texts, c=cfg: llm_embed(c, texts)) if cfg.base_url and cfg.embed_model else None
    return kb


def reanalyze() -> None:
    """Rules changed: rebuild the analysis of every loaded dataset from its observations (fast, no re-parse)."""
    knowledge().apply_rules()
    for key, d in st.session_state.get("datasets", {}).items():
        old = d["analysis"]
        d["analysis"] = Analysis(old.observations, old.report)
    if st.session_state.get("dataset") in st.session_state.get("datasets", {}):
        activate_dataset(st.session_state["dataset"])
    for fn in (globals().get("frames"), globals().get("search_frame")):   # defined further down; absent on pages that stop early
        if fn is not None:
            fn.clear()


@st.cache_resource
def live_store() -> LiveStore:
    return LiveStore(spool=os.environ.get("LIVE_SPOOL", "data/live/events.jsonl"))


def agents() -> AgentRegistry:
    return _singleton("agents", AgentRegistry, lambda: AgentRegistry(knowledge()))


def sources() -> wo_sources.SourceStore:
    return _singleton("sources", wo_sources.SourceStore, lambda: wo_sources.SourceStore(knowledge()))


def source_poller() -> wo_sources.Poller:
    return _singleton("poller", wo_sources.Poller, lambda: wo_sources.Poller(sources(), live_store()).start())


@st.cache_resource
def _receivers() -> dict:
    return {}


def receiver(port: int, api_key: str):
    """One receiver per process. A changed port/key stops the old server and binds a new one; a port clash returns the OSError to display."""
    holder = _receivers()
    cur = holder.get("srv")
    if cur is not None and holder.get("cfg") == (port, api_key or None):
        return cur
    if cur is not None:
        try:
            cur.shutdown(); cur.server_close()
        except Exception:  # noqa: BLE001
            pass
        holder.pop("srv", None)
    try:
        srv = start_receiver(live_store(), port, api_key or None, agents())
    except OSError as e:
        return e
    holder["srv"], holder["cfg"] = srv, (port, api_key or None)
    return srv


@st.cache_resource
def simulator():
    return start_simulator(live_store(), interval=1.0)


def llm_cfg() -> LLMConfig:
    ss = st.session_state
    if "llm_base" not in ss and not os.environ.get("LLM_BASE_URL"):       # first run: adopt a local Ollama if there is one
        found = wo_ollama.discover()
        if found:
            ss["llm_base"], ss["llm_provider"] = found, "ollama"
            inst = [m["name"] for m in wo_ollama.installed(found)] if found else []
            ss.setdefault("llm_model", next((m for m in inst if not any(e in m for e in ("embed", "bge"))), ""))
            ss.setdefault("llm_embed", next((m for m in inst if any(e in m for e in ("embed", "bge"))), ""))
    return LLMConfig(ss.get("llm_base", "") or os.environ.get("LLM_BASE_URL", ""), ss.get("llm_model", "") or os.environ.get("LLM_MODEL", ""),
                     ss.get("llm_key", "") or os.environ.get("LLM_API_KEY", ""), embed_model=ss.get("llm_embed", "") or os.environ.get("LLM_EMBED_MODEL", ""),
                     provider=ss.get("llm_provider", "") or os.environ.get("LLM_PROVIDER", "auto"))


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
    kb_with_embedder().record(analysis, key, current_lang())
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


def learning_panel(inc, a: Analysis) -> None:
    """Under the flashcard: what the knowledge base remembers about this kind of incident, and the team's verdict on the card."""
    kb = kb_with_embedder()
    root = a.signal_by_id[inc.root_cause_signal]
    rel = kb.related(inc, root)
    l, r = st.columns([3, 2], gap="medium")
    with l:
        st.markdown(f"**🧠 {t('kb_related')}**")
        if not rel:
            st.caption(t("kb_related_none"))
        for d in rel:
            refs = ", ".join(f"{x.get('dataset')}/{x.get('incident')}" for x in d.get("refs", [])[:3])
            st.markdown(f'<div class="card" style="padding:10px 14px"><span class="pill" style="background:#60a5fa">L{d["id"]}</span> '
                        f'<span class="muted">{d["kind"]} · ×{d.get("occurrences", 1)} · {t("fc_score")} {d.get("score", 0)}</span><br><b>{esc(d["title"][:110])}</b>'
                        f'<br><span class="muted">{esc(d["text"][:260])}</span>' + (f'<br><span class="mono muted">{esc(refs)}</span>' if refs else "") + "</div>", unsafe_allow_html=True)
    with r:
        st.markdown(f"**✍️ {t('fb_title')}**")
        with st.form(key=f"fb-{inc.id}", border=True):
            verdict = st.radio(t("fb_verdict"), ["up", "down"], horizontal=True, format_func=lambda v: t("fb_up") if v == "up" else t("fb_down"), label_visibility="collapsed")
            alts = [(x["template"][:80], x["services"]) for x in inc.root_cause_alternatives[:4]]
            opts = ["__keep__"] + [f"alt{i}" for i in range(len(alts))] + ["__noise__", "__other__"]
            labels = {"__keep__": t("fb_keep"), "__noise__": t("fb_noise"), "__other__": t("fb_other"), **{f"alt{i}": f"{tpl} ({', '.join(sv)})" for i, (tpl, sv) in enumerate(alts)}}
            correct = st.selectbox(t("fb_correct"), opts, format_func=lambda k: labels[k])
            comment = st.text_area(t("fb_comment"), height=70, placeholder=t("fb_comment_ph"))
            if st.form_submit_button(t("fb_send"), **wide("button")):
                root_type = str(root.observations[0].attributes.get("alarm_type", "")).lower() if root.observations else ""
                alt_type = ""
                if correct.startswith("alt"):
                    x = inc.root_cause_alternatives[int(correct[3:])]
                    sig = a.signal_by_id.get(x.get("signal", "")) if isinstance(x, dict) else None
                    alt_type = str(sig.observations[0].attributes.get("alarm_type", "")).lower() if sig and sig.observations else ""
                    correct_txt = labels[correct]
                else:
                    correct_txt = {"__keep__": "", "__noise__": "noise", "__other__": comment}[correct]
                out = kb.add_feedback(st.session_state.get("dataset", ""), inc, root, verdict, correct=correct_txt, comment=comment,
                                      alt_type=alt_type, root_type=root_type, mark_noise=(correct == "__noise__"))
                st.toast(t("fb_saved", n=len(out["proposals"])), icon="✅")


def receiver_url(port: int) -> str:
    """The address servers will post to: the saved public host, else the best local guess."""
    host = st.session_state.get("public_host") or (wo_settings.local_addresses()[0])
    return f"http://{host}:{port}/ingest"


def agent_install_cmd(port: int, token: str, logs: str = "/var/log/syslog,/var/log/messages", env: str = "") -> str:
    url = receiver_url(port); base = url[: -len("/ingest")]
    return (f"curl -fsSL {base}/agent/install.sh | sudo bash -s -- --url {url} --token {token} --logs \"{logs}\"" + (f" --env {env}" if env else ""))


def agents_panel(port: int, wizard: bool = False) -> None:
    """Where the receiver lives, what a server needs, enrol collectors, hand out one-time tokens, watch them come online, revoke."""
    reg = agents()
    ss = st.session_state
    if not wizard:
        st.markdown(f"#### {t('ag_title')}")
    with st.container(border=True):
        st.markdown(f"**📡 {t('ag_addr_title')}**")
        st.caption(t("ag_addr_hint"))
        cands = wo_settings.local_addresses()
        c1, c2, c3 = st.columns([2.5, 1, 1.2])
        cur = ss.get("public_host") or cands[0]
        opts = list(dict.fromkeys([cur] + cands))
        pick = c1.selectbox(t("ag_addr"), opts + ["__custom__"], index=opts.index(cur), key="ag_addr_pick", format_func=lambda x: t("ag_addr_custom") if x == "__custom__" else x)
        if pick == "__custom__":
            pick = c1.text_input(t("ag_addr_custom"), value=cur, key="ag_addr_txt")
        if pick and pick != ss.get("public_host"):
            ss["public_host"] = pick; wo_settings.save({"public_host": pick})
        c2.markdown(kpi2(port, t("live_port"), "🔌", "#60a5fa", ""), unsafe_allow_html=True)
        c3.markdown(kpi2(t("ag_open"), t("ag_firewall"), "🛡", "#fbbf24", t("ag_firewall_sub", p=port)), unsafe_allow_html=True)
        st.code(receiver_url(port), language=None)
        st.markdown(f'<div class="card" style="padding:10px 14px"><b>{t("ag_what_title")}</b><br><span class="muted">{t("ag_what_body")}</span></div>', unsafe_allow_html=True)
    with st.form("ag-enroll", border=True):
        st.markdown(f"**➕ {t('ag_enroll')}**")
        c = st.columns([2, 1.2, 1.2, 2.4])
        name = c[0].text_input(t("ag_name"), placeholder="db-01")
        env = c[1].selectbox(t("environment"), ["prod", "staging", "test", "dev", "qa", "dr"])
        site = c[2].text_input(t("ag_site"), placeholder="IST-DC1")
        logs = c[3].text_input(t("ag_logs"), value="/var/log/syslog,/var/log/messages", help=t("ag_logs_help"))
        tags = st.text_input(t("ag_tags"), placeholder="oracle, core")
        if st.form_submit_button(f"➕ {t('ag_enroll')}", **wide("button")) and name.strip():
            try:
                rec, tok = reg.enroll(name, env, site, tags)
                ss["ag_new"] = (rec, tok, logs.strip() or "/var/log/syslog,/var/log/messages")
            except ValueError:
                st.warning(t("ag_dup", n=name.strip()))
    if ss.get("ag_new"):
        rec, tok, logs = ss["ag_new"]
        st.success(t("ag_token_once", n=rec["name"]))
        st.markdown(f"**1 · {t('ag_step_run')}**")
        st.code(agent_install_cmd(port, tok, logs, rec.get("env", "")), language="bash")
        st.markdown(f"**2 · {t('ag_step_watch')}**")
        st.caption(t("ag_step_watch_sub"))
        with st.expander(t("ag_manual")):
            st.code(f"export WATCHOVER_URL={receiver_url(port)}\nexport WATCHOVER_TOKEN={tok}\ncurl -fsSL {receiver_url(port)[:-7]}/agent.py -o agent.py\n"
                    f"python3 agent.py --test\npython3 agent.py --metrics --tail \"{logs}\" --spool ./spool", language="bash")
            st.caption(t("ag_manual_sub"))
        if st.button(t("ag_done"), key="ag-done"):
            ss.pop("ag_new", None); st.rerun()

    with st.container(border=True):
        st.markdown(f"**🚀 {t('ag_fleet_title')}**")
        st.caption(t("ag_fleet_body"))
        ekey = reg.enroll_key(create=True) if ss.get("ag_fleet_first", True) else reg.enroll_key(create=False)
        ss["ag_fleet_first"] = False
        fc = st.columns([1, 1, 4])
        if ekey:
            if fc[0].button(f"↻ {t('ag_fleet_rotate')}", key="ag-fleet-rot"):
                reg.rotate_enroll_key(); st.rerun()
            if fc[1].button(f"⏏ {t('ag_fleet_off')}", key="ag-fleet-off"):
                reg.disable_enroll_key(); st.rerun()
            url = receiver_url(port); base = url[: -len("/ingest")]
            st.markdown(f"**{t('ag_fleet_cmd')}**")
            st.code(f"curl -fsSL {base}/agent/install.sh | sudo bash -s -- --url {url} --enroll-key {ekey} --env prod", language="bash")
            st.caption(t("ag_fleet_opts"))
        else:
            st.warning(t("ag_fleet_disabled"))
            if fc[0].button(t("ag_fleet_on"), key="ag-fleet-on"):
                reg.rotate_enroll_key(); st.rerun()

    @st.fragment(run_every="5s")
    def _agent_rows():
        rows = reg.list()
        if not rows:
            st.caption(t("ag_none")); return
        from datetime import datetime as _dt
        for r in rows:
            c = st.columns([2, 1, 1.2, 1.8, 1, 0.8, 0.8])
            seen = r["last_seen"] or ""
            fresh = bool(seen) and (datetime.now(UTC) - _dt.fromisoformat(seen)).total_seconds() < 120
            dot = "#f87171" if r["status"] != "active" else ("#2dd4bf" if fresh else "#fbbf24" if seen else "#64748b")
            state = t("ag_revoked") if r["status"] != "active" else (t("ag_online") if fresh else t("ag_stale") if seen else t("ag_waiting"))
            c[0].markdown(f'<span style="display:inline-block;width:8px;height:8px;border-radius:4px;background:{dot};margin-right:6px;box-shadow:0 0 0 3px {dot}33"></span><b>{esc(r["name"])}</b> <span class="muted">· {state}</span>', unsafe_allow_html=True)
            c[1].caption(r["env"] or "-"); c[2].caption(r["site"] or "-")
            c[3].caption(f"{seen[:16] or '-'} · {r['last_ip'] or ''}")
            c[4].caption(f"{r['events']:,} {t('events_n')}")
            if r["status"] == "active":
                if c[5].button("⏏", key=f"ag-rev-{r['id']}", help=t("ag_revoke")):
                    reg.revoke(r["id"]); st.rerun()
            elif c[5].button("↻", key=f"ag-rot-{r['id']}", help=t("ag_rotate")):
                ss["ag_new"] = (r, reg.rotate(r["id"]), "/var/log/syslog,/var/log/messages"); st.rerun()
            if c[6].button("🗑", key=f"ag-del-{r['id']}", help=t("ag_delete")):
                reg.delete(r["id"]); st.rerun()

    _agent_rows()


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
PAGES = ["ops", "data", "src", "map", "assist", "llm", "pb", "itsm", "conn", "readme"]
PAGE_KEYS = {"ops": "sb_ops", "data": "sb_data", "src": "sb_src", "map": "sb_map", "assist": "sb_assist", "llm": "sb_llm", "pb": "sb_pb", "itsm": "sb_itsm", "conn": "sb_conn", "readme": "sb_readme"}


# ------------------------------------------------------------------ first-run setup wizard
def page_setup() -> None:
    ss = st.session_state
    step = ss.setdefault("setup_step", 0)
    steps = [t("su_s1"), t("su_s2"), t("su_s3"), t("su_s4")]
    st.markdown(f'<div class="wo-brand" style="padding:6px 0 4px"><span style="display:inline-block;width:52px">{logo(52)}</span>'
                f'<div><div class="name" style="font-size:32px">Watchover</div><div class="tag">{upper(t("su_title"))}</div></div></div>', unsafe_allow_html=True)
    st.markdown('<div class="chips">' + "".join(f'<span class="chip" style="{"border-color:#2dd4bf;color:#e6fffa" if i == step else ""}"><span class="d" style="background:{"#2dd4bf" if i <= step else "#3b4b66"}"></span>{i + 1}. {esc(x)}</span>' for i, x in enumerate(steps)) + "</div>", unsafe_allow_html=True)
    st.progress((step + 1) / len(steps))
    with st.container(border=True):
        if step == 0:
            st.markdown(f"### {t('su_s1')}")
            st.caption(t("su_s1_hint"))
            st.radio(t("su_lang"), ["tr", "en"], horizontal=True, key="lang", format_func=lambda x: {"tr": "🇹🇷 Türkçe", "en": "🇬🇧 English"}[x])
            st.text_input(t("su_workspace"), key="su_workspace", placeholder="Turkcell NOC")
            st.number_input(t("live_port"), 1024, 65535, int(ss.get("live_port", LIVE_PORT)), key="su_port")
            st.toggle(t("live_sim"), value=ss.get("sim_on", True), key="su_sim")
        elif step == 1:
            st.markdown(f"### {t('su_s2')}")
            st.caption(t("su_s2_hint"))
            found = wo_ollama.discover()
            if found:
                st.success(t("llm_found", u=found))
                inst = wo_ollama.installed(found)
                names = [m["name"] for m in inst]
                chat_opts = [n for n in names if not any(e in n for e in ("embed", "bge"))]
                emb_opts = [n for n in names if any(e in n for e in ("embed", "bge"))]
                ss.setdefault("llm_base", found); ss["llm_provider"] = "ollama"
                c1, c2 = st.columns(2)
                c1.selectbox(t("llm_model"), chat_opts + [x["name"] for x in wo_ollama.RECOMMENDED if x["role"] == "chat" and x["name"] not in chat_opts], key="su_chat")
                c2.selectbox(t("llm_embed"), [""] + emb_opts + [x["name"] for x in wo_ollama.RECOMMENDED if x["role"] == "embed" and x["name"] not in emb_opts], key="su_embed")
                need = [m for m in (ss.get("su_chat"), ss.get("su_embed")) if m and m not in names]
                if need:
                    st.warning(t("su_need_pull", m=", ".join(need)))
                    if st.button(f"⬇ {t('llm_pull')} · {', '.join(need)}", key="su-pull"):
                        for name in need:
                            bar = st.progress(0.0, text=f"{t('llm_pulling')} {name}")
                            try:
                                for ev in wo_ollama.pull(found, name):
                                    if ev.get("error"):
                                        raise RuntimeError(ev["error"])
                                    if ev["pct"] is not None:
                                        bar.progress(min(1.0, ev["pct"]), text=f"{t('llm_pulling')} {name} · {ev['pct']:.0%}")
                                bar.progress(1.0, text=t("llm_pulled", m=name))
                            except Exception as e:  # noqa: BLE001
                                st.error(f"{name}: {e}")
                        st.rerun()
                ss["llm_model"], ss["llm_embed"] = ss.get("su_chat", ""), ss.get("su_embed", "")
            else:
                st.info(t("su_no_ollama"))
                st.code("brew install --cask ollama   # macOS\ncurl -fsSL https://ollama.com/install.sh | sh   # Linux", language="bash")
                with st.expander(t("su_external")):
                    st.selectbox(t("llm_provider"), list(PROVIDERS), key="llm_provider", format_func=lambda x: t("prov_" + x))
                    st.text_input(t("llm_base"), key="llm_base"); st.text_input(t("llm_model"), key="llm_model"); st.text_input(t("llm_key"), key="llm_key", type="password")
                if st.button(f"🔄 {t('su_recheck')}", key="su-recheck"):
                    st.rerun()
        elif step == 2:
            st.markdown(f"### {t('su_s3')}")
            st.caption(t("su_s3_hint"))
            _port = int(ss.get("live_port", LIVE_PORT))
            _r = receiver(_port, ss.get("live_key", ""))                  # the receiver must already listen so the first agent can come online here
            if isinstance(_r, OSError):
                st.error(f"{t('live_port')} {_port}: {_r}")
            st.caption("⚠ " + t("ag_http_warn"))
            agents_panel(_port, wizard=True)
        else:
            st.markdown(f"### {t('su_s4')}")
            st.caption(t("su_s4_hint"))
            st.toggle(t("su_demo"), value=True, key="su_demo")
            cfg = llm_cfg()
            st.markdown(f'<div class="card"><b>{t("su_summary")}</b><br><span class="muted">{t("su_lang")}: {ss.get("lang", "tr")} · {t("live_port")}: {ss.get("live_port", LIVE_PORT)} · LLM: {cfg.kind} / {cfg.model or t("as_no_llm")} · {t("llm_embed")}: {cfg.embed_model or "-"} · {t("ag_title")}: {len(agents().list())}</span></div>', unsafe_allow_html=True)
    b1, b2, b3 = st.columns([1, 1, 4])
    if step > 0 and b1.button(f"← {t('su_back')}", key="su-back", **wide("button")):
        ss["setup_step"] = step - 1; st.rerun()
    if step < len(steps) - 1:
        if b2.button(f"{t('su_next')} →", key="su-next", type="primary", **wide("button")):
            if step == 0:                                    # widgets vanish with their step: keep what matters in plain session keys
                ss["live_port"], ss["sim_on"], ss["workspace"] = int(ss.get("su_port", LIVE_PORT)), bool(ss.get("su_sim", True)), ss.get("su_workspace", "")
            ss["setup_step"] = step + 1; st.rerun()
    elif b2.button(f"✓ {t('su_finish')}", key="su-finish", type="primary", **wide("button")):
        wo_settings.save({"setup_done": True, "lang": ss.get("lang", "tr"), "workspace": ss.get("workspace", ""), "live_port": int(ss.get("live_port", LIVE_PORT)),
                          "sim_on": bool(ss.get("sim_on", True)), "demo_on_start": bool(ss.get("su_demo", True)),
                          **{k: ss.get(k, "") for k in ("llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed")}})
        ss["setup_done"] = True
        if ss.get("su_demo", True) and "analysis" not in ss and demo.exists():
            load("demo_mixed.zip", path=str(demo))
        st.rerun()
    if b3.button(t("su_skip"), key="su-skip"):
        wo_settings.save({"setup_done": True}); ss["setup_done"] = True; st.rerun()



if not (st.session_state.get("setup_done") or wo_settings.setup_done()):
    page_setup()
    st.stop()

with st.sidebar:
    st.markdown(f'<div class="wo-brand">{logo(40)}<div><div class="name">Watchover</div><div class="tag">{upper(t("brand_tag"))}</div></div></div>', unsafe_allow_html=True)
    st.radio("Language", ["tr", "en"], horizontal=True, label_visibility="collapsed",
             format_func=lambda x: {"tr": "TR", "en": "EN"}[x], key="lang")
    st.markdown(f'<div class="sb-cap">{upper(t("sb_nav"))}</div>', unsafe_allow_html=True)
    page = st.radio("nav", PAGES, format_func=lambda x: t(PAGE_KEYS[x]), label_visibility="collapsed", key="page")
    st.markdown(f'<div class="sb-cap">{upper(t("sb_status"))}</div>', unsafe_allow_html=True)

    @st.fragment(run_every="5s")
    def _status():
        ls_ = live_store()
        rows = [("#2dd4bf" if ls_.agents else "#64748b", f"{t('live_port')} :{st.session_state.get('live_port', LIVE_PORT)} · {ls_.received:,} {t('events_n')} · {len(ls_.agents)} {t('live_agents')}")]
        if "analysis" in st.session_state:
            rows.append(("#60a5fa", f"{esc(st.session_state['dataset'])} · {st.session_state['analysis'].funnel()['incidents']} {t('incidents')}"))
        if st.session_state.get("tickets"):
            rows.append(("#a78bfa", f"{len(st.session_state['tickets'])} {t('tickets_n')} · {esc(str(st.session_state.get('tickets_src', '-')))}"))
        st.markdown('<div class="sb-status">' + "<br>".join(f'<span class="dot" style="background:{c};box-shadow:0 0 0 3px {c}33"></span>{txt}' for c, txt in rows) + "</div>", unsafe_allow_html=True)

    _status()
    st.caption(t("footer"))
    _st = wo_stamp.stamp()
    st.caption(f"Watchover v{_st['version']} · {t('stamp_engine')} {_st['engine']} · git {_st['git']} · {t('stamp_scenario')} {_st['scenario']}", help=t("stamp_hint"))
    if (_stale := wo_stamp.stale_package(__file__)):
        st.warning(t("stale_pkg", path=_stale))

# always-on receiver + optional simulator (started once per process)
_srv = receiver(int(st.session_state.get("live_port", LIVE_PORT)), st.session_state.get("live_key", ""))
_poller = source_poller()                                       # pull sources (Elasticsearch, Loki, Splunk, Graylog, HTTP) poll in the background
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
    st.session_state[host_key] = list(host) if isinstance(host, (list, tuple)) else ([host] if host else [])
    st.session_state["ops_scope_hosts"] = list(st.session_state[host_key])


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
        # The pills live inside the 2 s fragment: on the next full rerun (opening a detail dialog) Streamlit drops the widget's
        # own state, so the chosen hosts are mirrored in a plain key and re-seeded from it before the widget is built.
        if "ops_host_pick" not in st.session_state and st.session_state.get("ops_scope_hosts"):
            st.session_state["ops_host_pick"] = [h for h in st.session_state["ops_scope_hosts"] if h in hosts]
        cur = [h for h in (st.session_state.get("ops_host_pick") or []) if h in hosts]
        if hosts and cur != list(st.session_state.get("ops_host_pick") or []):   # prune only against a known host list
            st.session_state["ops_host_pick"] = cur
        pick = st.pills("host", hosts, selection_mode="multi", key="ops_host_pick", label_visibility="collapsed", help=t("ops_host_hint"),
                        format_func=lambda h: h if env else f"{h} · {host_env.get(h, '')}")
        st.session_state["ops_scope_hosts"] = list(pick or [])
        host = scope_hosts(pick)
        if isinstance(host, (list, tuple)):
            st.caption(t("ops_multi", n=len(host)))
        st.button(f"✕ {t('ops_reset')}", key="ops_reset", on_click=_set_scope, args=("ops_env_pick", "ops_host_pick"), disabled=not (env or host), **wide("button"))
    return env, host


def scope_hosts(pick) -> str | tuple | None:
    """Multi-select value → scope: None (all), one name, or a tuple of names (metrics are averaged over them)."""
    pick = [h for h in (pick or []) if h]
    return None if not pick else pick[0] if len(pick) == 1 else tuple(pick)


def scope_label(env, host) -> str:
    return " · ".join(x for x in (env, ", ".join(host) if isinstance(host, (list, tuple)) else host) if x)


def ops_tile(key: str, html_: str, clickable: bool) -> None:
    """A kpi2 card; with clickable=True a 'Detail' strip below toggles the live detail panel (session key ops_detail)."""
    if not clickable:
        st.markdown(html_, unsafe_allow_html=True); return
    st.markdown('<div class="tile">' + html_ + "</div>", unsafe_allow_html=True)
    st.button(t("detail"), key=f"ops-tile-{key}", **wide("button"), on_click=lambda k=key: st.session_state.__setitem__("open_ops_detail", k))


def metric_chart(d: pd.DataFrame, height: int, thr: float | None = None):
    base = alt.Chart(d).mark_line(interpolate="monotone", strokeWidth=1.6).encode(
        x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("value:Q", title=None, scale=alt.Scale(domain=[0, 100])),
        color=alt.Color("host:N", legend=alt.Legend(orient="bottom", title=None, columns=4)), tooltip=["host", "env", "metric", "value"])
    if thr is not None:
        base = base + alt.Chart(pd.DataFrame({"y": [thr]})).mark_rule(color="#f87171", strokeDash=[4, 4]).encode(y="y:Q")
    return base.properties(height=height).configure_view(strokeWidth=0)


def metrics_block(ms: dict, clickable: bool = False) -> None:
    """CPU / GPU / memory / disk live tiles: value, worst host, per-minute sparkline (mean over hosts) with the threshold line."""
    thr = __import__("watchover.scenario", fromlist=["x"]).METRIC_THRESHOLDS
    pm = pd.DataFrame(ms["per_minute"]) if ms["per_minute"] else pd.DataFrame(columns=["minute", "host", "metric", "env", "value"])
    ic = st.columns(4)
    for col, metric, icon in zip(ic, ("cpu", "gpu", "memory", "disk"), ("🧠", "🎮", "💾", "🗄")):
        sm = ms["summary"][metric]
        val = f"{sm['avg']:.0f}%" if sm["avg"] is not None else t("no_data")
        color = "#8b98ad" if sm["max"] is None else "#f87171" if sm["max"] >= thr[metric] else "#fb923c" if sm["max"] >= thr[metric] - 15 else "#2dd4bf"
        sub = f"{t('worst')} {sm['worst']} {sm['max']:.0f}% · {sm['hosts']} {t('hosts_n_short')}" if sm["max"] is not None else ""
        d = pm[pm.metric == metric]
        series = d.groupby("minute").value.mean().sort_index().tolist() if len(d) else []
        with col:
            ops_tile(metric, live_tile(val, t(metric), icon, color, sub, series[-15:], ymax=100, target=thr[metric]), clickable)


def slo_block(slo: dict, stt: dict, clickable: bool = False, det: dict | None = None) -> None:
    k = st.columns(6)
    av, p95, bud = slo["availability"], slo["p95_ms"], slo["error_budget"]
    av_ok = av is not None and av >= slo["slo"]["availability"]
    p_ok = p95 is None or p95 <= slo["slo"]["p95_ms"]
    pmn = (det or {}).get("per_minute", [])
    s_av = [r.get("availability") for r in pmn][-15:]
    s_bud = [r.get("budget_left") for r in pmn][-15:]
    s_err = [r.get("errors") for r in pmn][-15:]
    s_tot = [r.get("total") for r in pmn][-15:]
    lat = [x["ms"] for x in (det or {}).get("slowest", [])][::-1][-15:]
    tiles = [
        ("availability", live_tile(pct(av, 1), t("availability"), "🎯", "#2dd4bf" if av_ok else "#f87171", t("slo_target", v=pct(slo["slo"]["availability"], 1)), s_av, ymax=1.0, ymin=min([v for v in s_av if v is not None] + [slo["slo"]["availability"]]) - 0.01 if s_av else 0.9, target=slo["slo"]["availability"])),
        ("p95", live_tile(f"{p95:.0f} ms" if p95 is not None else t("no_data"), t("p95"), "⏱", "#2dd4bf" if p_ok else "#fb923c", f"SLO ≤ {slo['slo']['p95_ms']} ms", lat, ymax=max(lat + [slo["slo"]["p95_ms"]]) if lat else None, target=slo["slo"]["p95_ms"])),
        ("budget", live_tile(pct(bud, 0) if bud is not None else t("no_data"), t("budget"), "🧮", "#2dd4bf" if (bud or 0) > 0.25 else "#fb923c" if (bud or 0) > 0 else "#f87171", f"{slo['errors']} / {slo['total']} ERROR+", s_bud, ymax=1.0)),
        ("sla", live_tile(t("ok") if slo["sla_ok"] else t("breach"), t("sla"), "📜", "#2dd4bf" if slo["sla_ok"] else "#f87171", t("sla_target", v=pct(slo["sla"]["availability"], 1)), s_av, ymax=1.0, ymin=min([v for v in s_av if v is not None] + [slo["sla"]["availability"]]) - 0.01 if s_av else 0.9, target=slo["sla"]["availability"])),
        ("rate", live_tile(stt["per_minute_now"], t("live_rate"), "⚡", "#60a5fa", f"{stt['received']:,} {t('n_total')}", s_tot)),
        ("errors", live_tile(stt["errors"], t("live_errors"), "🔥", "#f87171" if stt["errors"] else "#8b98ad", f"{stt['total']:,} {t('live_buffered')}", s_err, muted=True)),
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
    with st.container():
        st.markdown(f"<span class='muted' style='font-size:13px'>{t('od_window')} · {t('ops_scope')}: {esc(scope_label(env, host) or t('ops_all'))}</span>", unsafe_allow_html=True)
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
        with st.container(border=True):
            r1, r2, r3 = st.columns([1.2, 1.2, 3])
            win = r1.selectbox(t("rep_window"), [15, 60, 240, 1440], index=1, key="rep_window", format_func=lambda m: f"{m} {t('min_short')}" if m < 60 else f"{m // 60} {t('hour_short')}")
            _html = wo_report.slo_report(ls, st.session_state.get("analysis"), env, host, int(win), current_lang(), st.session_state.get("workspace", ""), LOGO_SVG)
            r2.download_button(f"📄 {t('rep_download')}", _html.encode("utf-8"), file_name=f"watchover-slo-{datetime.now(UTC).strftime('%Y%m%d-%H%M')}.html", mime="text/html", key="rep-dl", **wide("download_button"))
            r3.caption(t("rep_hint"))
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
    with c1, st.container(border=True):
        st.markdown(f"**{t('live_events_min')}**")
        st.altair_chart(alt.Chart(rows).mark_area(interpolate="monotone").encode(
            x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M")), y=alt.Y("events:Q", stack=True, title=None, axis=alt.Axis(format="d")),
            color=alt.Color("severity:N", scale=SEV_SCALE, legend=alt.Legend(orient="top", title=None)), order=alt.Order("severity:N"),
            tooltip=[alt.Tooltip("minute:T", format="%H:%M"), "severity", "events"]).properties(height=170).configure_view(strokeWidth=0), **wide("altair_chart"))
    with c2, st.container(border=True):
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
    _k = st.session_state.pop("open_ops_detail", None)          # popped before the fragment: the fragment only asks for a full rerun

    @st.fragment(run_every="2s")
    def _panel():
        if st.session_state.get("open_ops_detail"):
            st.rerun(scope="app")
        all_stt = ls.stats(15)
        real_agents = [a_ for a_ in all_stt["agents"] if a_ != "simulator"]
        badge_txt, badge_col = (t("live_real_badge"), "#2dd4bf") if real_agents else (t("live_sim_badge"), "#fbbf24")
        st.markdown(f'## {t("live_title")}')
        agents = ", ".join(list(all_stt["agents"])[:4]) or t("live_no_agent")
        st.markdown('<div class="chips">' + "".join(f'<span class="chip"><span class="d" style="background:{c}"></span>{esc(x)}</span>' for c, x in (
            (badge_col, badge_txt), ("#60a5fa", f"{len(all_stt['agents'])} {t('live_agents')} · {agents}"), ("#8b98ad", t("live_sub_short")),
            ("#a78bfa", f"{t('live_port')} :{st.session_state.get('live_port', LIVE_PORT)}"))) + "</div>", unsafe_allow_html=True)
        # cards on the left, environment / host box on the right; the box narrows all ten cards
        main, side = st.columns([4.6, 1.25], gap="medium")
        with side:
            st.markdown(f"#### {t('ops_scope')}")
            env, host = scope_panel(ls)
        stt, slo, ms = ls.stats(15, env, host), ls.slo(15, env, host), ls.metric_stats(15, env, host)
        det = ls.slo_detail(15, env, host)
        scope = scope_label(env, host)
        scope_html = f" <span class='pill' style='background:#60a5fa'>{t('ops_filter_on')}: {esc(scope)}</span>" if scope else ""
        with main:
            st.markdown(f"#### {t('ops_infra')}{scope_html} <span class='muted'>· {ms['samples']} {t('od_samples')}</span>", unsafe_allow_html=True)
            metrics_block(ms, clickable=True)
            st.markdown(f"#### {t('ops_slo')}{scope_html}", unsafe_allow_html=True, help=t("ops_slo_basis"))
            slo_block(slo, stt, clickable=True, det=det)
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
    if _k:
        _all = t("ops_all")
        _env = st.session_state.get("ops_env_pick") or None
        _env = None if _env in ("", _all) else _env
        _host = scope_hosts(st.session_state.get("ops_scope_hosts") or st.session_state.get("ops_host_pick"))

        @st.dialog(t("od_" + _k), width="large")
        def _ops_dialog() -> None:
            @st.fragment(run_every="5s")
            def _body() -> None:
                ops_detail_panel(_k, ls, _env, _host, ls.metric_stats(15, _env, _host), ls.stats(15, _env, _host), ls.slo(15, _env, _host))
            _body()
        _ops_dialog()
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
        if st.button(t("cfg_save"), key="live-save"):
            wo_settings.save({"live_port": int(port), "live_key": key, "sim_on": st.session_state.get("sim_on", True)}); st.toast(t("cfg_saved"), icon="💾")
        st.caption(t("live_key_legacy"))
        st.caption("⚠ " + t("ag_http_warn"))
        agents_panel(int(port))
    with tab_llm:
        st.info(t("llm_moved"))
    if st.button(f"🧭 {t('su_rerun')}", key="su-rerun"):
        wo_settings.save({"setup_done": False}); st.session_state["setup_done"] = False; st.session_state["setup_step"] = 0; st.rerun()


# ------------------------------------------------------------------ page: Ask Watchover (chat + knowledge base + rules)
def page_assist() -> None:
    kb = kb_with_embedder()
    a = st.session_state.get("analysis")
    cfg = llm_cfg()
    st.markdown(f'<div class="wo-brand" style="padding:0 0 6px"><span style="display:inline-block;width:44px">{logo(44)}</span>'
                f'<div><div class="name" style="font-size:30px">{t("as_title_plain")}</div><div class="tag">{upper(t("as_title_tag"))}</div></div></div>', unsafe_allow_html=True)
    stt = kb.stats()
    chips = [("#2dd4bf" if cfg.enabled else "#64748b", f"LLM: {cfg.model or t('as_no_llm')}"), ("#60a5fa", f"{sum(stt['lessons'].values())} {t('kb_lessons')}"),
             ("#a78bfa", f"{stt['rules'].get('approved', 0)} {t('kb_rules_on')} · {stt['rules'].get('proposed', 0)} {t('kb_rules_wait')}"),
             ("#fbbf24" if a is None else "#2dd4bf", st.session_state.get("dataset") or t("as_no_dataset"))]
    st.markdown('<div class="chips">' + "".join(f'<span class="chip"><span class="d" style="background:{c}"></span>{esc(str(x))}</span>' for c, x in chips) + "</div>", unsafe_allow_html=True)
    tab_chat, tab_kb, tab_rules = st.tabs([t("as_tab_chat"), f"{t('as_tab_kb')} · {sum(stt['lessons'].values())}", f"{t('as_tab_rules')} · {stt['rules'].get('proposed', 0)}"])

    with tab_chat:
        hist = st.session_state.setdefault("chat", [])
        if not hist:
            st.markdown(f'<div class="card"><b>{t("as_hello")}</b><br><span class="muted">{t("as_hello_sub")}</span></div>', unsafe_allow_html=True)
            ex = st.columns(3)
            for i, q_ in enumerate((t("as_ex1"), t("as_ex2"), t("as_ex3"))):
                if ex[i].button(q_, key=f"as-ex-{i}", **wide("button")):
                    st.session_state["as_pending"] = q_; st.rerun()
        for i, m in enumerate(hist):
            with st.chat_message(m["role"], avatar=LOGO_PNG if m["role"] == "assistant" else "🧑‍💻"):
                st.markdown(m["content"])
                if m["role"] == "assistant":
                    meta = m.get("meta", {})
                    cap = (t("as_via_llm", m=cfg.model) if meta.get("used_llm") else t("as_via_ctx")) + (f" · ⚠ {meta['error'][:80]}" if meta.get("error") else "")
                    st.caption(cap)
                    if meta.get("sources"):
                        with st.expander(f"📎 {t('as_sources')} · {len(meta['sources'])}"):
                            for src_ in meta["sources"]:
                                st.markdown(f'<div class="card" style="padding:8px 12px"><span class="pill" style="background:{"#f87171" if src_["kind"] == "incident" else "#60a5fa"}">{esc(src_["id"])}</span> '
                                            f'<b>{esc(src_["title"][:100])}</b><br><span class="muted">{esc(src_["text"][:300])}</span></div>', unsafe_allow_html=True)
                    if meta.get("context"):
                        with st.expander(f"🔍 {t('as_context')}"):
                            st.code(meta["context"][:6000], language=None)
                    if i == len(hist) - 1:
                        c1_, c2_, c3_, _ = st.columns([1.6, 0.5, 0.5, 3.4])
                        q_prev = hist[i - 1]["content"] if i and hist[i - 1]["role"] == "user" else ""
                        if c1_.button(t("as_save"), key=f"as-save-{i}", help=t("as_save_help")):
                            kb.add("chat", q_prev[:80] or "chat", f"Q: {q_prev}\nA: {m['content']}", tags=["chat"])
                            st.toast(t("as_saved"), icon="✅")
                        if not meta.get("rated"):
                            if c2_.button("👍", key=f"as-up-{i}", help=t("as_fb_help")):
                                kb.rate_answer(cfg.model or "-", q_prev, "up"); meta["rated"] = "up"; st.toast(t("as_fb_thanks"), icon="👍"); st.rerun()
                            if c3_.button("👎", key=f"as-down-{i}", help=t("as_fb_help")):
                                kb.rate_answer(cfg.model or "-", q_prev, "down"); meta["rated"] = "down"; st.toast(t("as_fb_thanks"), icon="👎"); st.rerun()
        pending = st.session_state.pop("as_pending", None)
        q = st.chat_input(t("as_input")) or pending
        if q:
            hist.append({"role": "user", "content": q})
            with st.chat_message("user", avatar="🧑‍💻"):
                st.markdown(q)
            with st.chat_message("assistant", avatar=LOGO_PNG):
                with st.spinner(t("as_thinking")):
                    res = wo_assistant.answer(cfg, q, hist[:-1], a, kb, current_lang())
                st.markdown(res["text"])
            hist.append({"role": "assistant", "content": res["text"], "meta": {k: res[k] for k in ("sources", "context", "used_llm", "error")}})
            st.rerun()
        if hist and st.button(t("as_clear"), key="as-clear"):
            st.session_state["chat"] = []; st.rerun()

    with tab_kb:
        k = st.columns(5)
        for i, (kind, ic, col) in enumerate((("pattern", "🧩", "#2dd4bf"), ("note", "📝", "#60a5fa"), ("doc", "📄", "#a78bfa"), ("feedback", "✍️", "#fbbf24"))):
            k[i].markdown(kpi2(stt["lessons"].get(kind, 0), t("kb_" + kind), ic, col, ""), unsafe_allow_html=True)
        k[4].markdown(kpi2(f"{stt['bytes'] / 1024:.0f} KB" if stt["bytes"] else "-", t("kb_size"), "💾", "#8b98ad", t("kb_size_sub")), unsafe_allow_html=True)
        st.markdown("")
        f1, f2 = st.columns(2, gap="medium")
        with f1, st.form("kb-note", border=True):
            st.markdown(f"**📝 {t('kb_add_note')}**")
            ttl = st.text_input(t("kb_note_title"))
            body = st.text_area(t("kb_note_text"), height=120, placeholder=t("kb_note_ph"))
            tags = st.text_input(t("kb_tags"), placeholder="db, payment, runbook")
            if st.form_submit_button(t("kb_save"), **wide("button")) and body.strip():
                kb.add_note(ttl, body, [x.strip() for x in tags.split(",") if x.strip()])
                st.toast(t("kb_saved"), icon="✅"); st.rerun()
        with f2:
            with st.container(border=True):
                st.markdown(f"**📄 {t('kb_add_doc')}**")
                st.caption(t("kb_doc_hint"))
                ups = st.file_uploader(t("kb_add_doc"), type=["txt", "md", "log", "json", "csv", "yaml", "yml"], accept_multiple_files=True, key="kb_docs", label_visibility="collapsed")
                if ups and st.button(t("kb_ingest"), key="kb-ingest", **wide("button")):
                    n = 0
                    for up in ups:
                        try:
                            n += len(kb.add_doc(up.name, up.getvalue().decode("utf-8", "replace")))
                        except Exception as e:  # noqa: BLE001
                            st.error(f"{up.name}: {e}")
                    st.toast(t("kb_doc_saved", n=n), icon="✅")
            with st.form("kb-fact", border=True):
                st.markdown(f"**📌 {t('kb_add_fact')}**")
                st.caption(t("kb_fact_hint"))
                fk = st.selectbox(t("kb_fact_kind"), list(RULE_KINDS), format_func=lambda x: t("rule_" + x))
                fkey = st.text_input(t("kb_fact_key"), placeholder="billing · disk_full · payment-api->payment-db")
                fval = st.text_input(t("kb_fact_value"), placeholder="Faturalama ekibi · 5 · senkron")
                if st.form_submit_button(t("kb_propose"), **wide("button")) and fkey.strip():
                    kb.propose(fk, fkey.strip(), fval.strip() or "1", t("kb_fact_reason"), "manual")
                    st.toast(t("kb_proposed"), icon="📌"); st.rerun()
        st.markdown(f"#### {t('kb_browse')}")
        b1, b2 = st.columns([4, 1])
        qk = b1.text_input(t("kb_search"), key="kb_q", label_visibility="collapsed", placeholder=t("kb_search"))
        kind = b2.selectbox(t("kb_kind"), ["", "pattern", "note", "doc", "feedback", "chat"], format_func=lambda x: t("kb_" + x) if x else t("ops_all"), label_visibility="collapsed")
        rows = kb.search(qk, k=30, kinds=(kind,) if kind else None) if qk else kb.all(kind or None, limit=60)
        if not rows:
            st.caption(t("kb_empty"))
        for d in rows:
            with st.container(border=True):
                h, x = st.columns([8, 1])
                refs = ", ".join(f"{r_.get('dataset')}/{r_.get('incident')}" for r_ in d.get("refs", [])[:4])
                h.markdown(f'<span class="pill" style="background:#60a5fa">L{d["id"]}</span> <b>{esc(d["title"][:120])}</b> <span class="muted">· {t("kb_" + d["kind"]) if d["kind"] in ("pattern","note","doc","feedback","chat") else d["kind"]} · ×{d.get("occurrences", 1)} · {str(d.get("updated_at", ""))[:16]}'
                           + (f" · {t('fc_score')} {d['score']}" if d.get("score") is not None else "") + "</span>", unsafe_allow_html=True)
                if x.button("🗑", key=f"kb-del-{d['id']}", help=t("kb_delete")):
                    kb.delete(d["id"]); st.rerun()
                st.markdown(f'<div class="mono muted" style="white-space:pre-wrap;font-size:12px">{esc(d["text"][:900])}</div>' + (f'<div class="muted" style="font-size:11px">{esc(refs)}</div>' if refs else ""), unsafe_allow_html=True)

    with tab_rules:
        st.caption(t("rules_hint"))
        lc, lh = st.columns([1.4, 4])
        if lc.button(f"🤖 {t('rules_ask_llm')}", key="rules-llm", disabled=not cfg.enabled, help=t("rules_ask_llm_help"), **wide("button")):
            with st.spinner(t("as_thinking")):
                try:
                    out = wo_assistant.propose_rules(cfg, a, kb, current_lang())
                    st.session_state["rules_llm_out"] = out
                except Exception as e:  # noqa: BLE001
                    st.session_state["rules_llm_out"] = {"error": str(e)}
            st.rerun()
        if not cfg.enabled:
            lh.caption(t("rules_llm_off"))
        out = st.session_state.pop("rules_llm_out", None)
        if out:
            if out.get("error"):
                st.error(out["error"])
            else:
                st.success(t("rules_llm_done", n=len(out["proposed"]), d=len(out["dropped"])))
                if out["dropped"]:
                    st.caption(", ".join(out["dropped"][:8]))
        prop = kb.rules("proposed")
        st.markdown(f"#### {t('rules_proposed')} · {len(prop)}")
        if not prop:
            st.caption(t("rules_none"))
        for r_ in prop:
            c = st.columns([1.3, 2, 2, 3, 1, 1])
            c[0].markdown(f'<span class="pill" style="background:{"#a78bfa" if str(r_["source"]).startswith("llm:") else "#fbbf24"}">{("🤖 " if str(r_["source"]).startswith("llm:") else "") + t("rule_" + r_["kind"])}</span>', unsafe_allow_html=True)
            c[1].code(r_["key"][:80], language=None); c[2].markdown(f"→ **{esc(r_['value'])}**"); c[3].caption(f"{r_['reason'][:120]} · {r_['source']}")
            if c[4].button("✓", key=f"rule-ok-{r_['id']}", help=t("rules_approve"), type="primary"):
                kb.decide(r_["id"], True); reanalyze(); st.toast(t("rules_applied"), icon="✅"); st.rerun()
            if c[5].button("✕", key=f"rule-no-{r_['id']}", help=t("rules_reject")):
                kb.decide(r_["id"], False); st.rerun()
        appr = kb.rules("approved")
        st.markdown(f"#### {t('rules_active')} · {len(appr)}")
        for r_ in appr:
            c = st.columns([1.3, 2, 2, 3, 1])
            c[0].markdown(f'<span class="pill" style="background:#2dd4bf">{t("rule_" + r_["kind"])}</span>', unsafe_allow_html=True)
            c[1].code(r_["key"][:80], language=None); c[2].markdown(f"→ **{esc(r_['value'])}**"); c[3].caption(f"{r_['reason'][:120]} · {str(r_.get('decided_at', ''))[:16]}")
            if c[4].button("⏏", key=f"rule-off-{r_['id']}", help=t("rules_revoke")):
                kb.decide(r_["id"], False); reanalyze(); st.rerun()


# ------------------------------------------------------------------ page: Sources (pull from Elasticsearch / Loki / Splunk / Graylog / HTTP)
def page_sources() -> None:
    store = sources()
    ss = st.session_state
    st.markdown(f'<div class="wo-brand" style="padding:0 0 6px"><span style="display:inline-block;width:44px">{logo(44)}</span>'
                f'<div><div class="name" style="font-size:30px">{t("src_page")}</div><div class="tag">{upper(t("src_page_tag"))}</div></div></div>', unsafe_allow_html=True)
    rows = store.list()
    on = [r for r in rows if r.enabled]
    errs = [r for r in rows if r.last_err]
    chips = [("#60a5fa", f"{len(rows)} {t('src_n')}"), ("#2dd4bf", f"{len(on)} {t('src_active')}"), ("#f87171" if errs else "#64748b", f"{len(errs)} {t('src_err')}"),
             ("#a78bfa", f"{sum(r.events for r in rows):,} {t('events_n')}")]
    st.markdown('<div class="chips">' + "".join(f'<span class="chip"><span class="d" style="background:{c}"></span>{esc(str(x))}</span>' for c, x in chips) + "</div>", unsafe_allow_html=True)
    st.caption(t("src_intro"))

    with st.expander(t("src_catalog"), expanded=not rows):
        cols = st.columns(3)
        for i, (k, meta) in enumerate(wo_sources.KINDS.items()):
            cols[i % 3].markdown(f'<div class="card" style="min-height:190px"><b>{meta["icon"]} {esc(meta["label"])}</b> <span class="muted">· :{meta["port"]}</span><br>'
                                 f'<span class="muted"><b>{t("src_needs")}:</b> {esc(t("src_needs_" + k))}</span><br>'
                                 f'<span class="muted"><b>{t("src_logs")}:</b> {esc(t("src_logs_" + k))}</span></div>', unsafe_allow_html=True)
        st.caption(t("src_catalog_note"))

    with st.container(border=True):
        st.markdown(f"**➕ {t('src_add')}**")
        kind = st.selectbox(t("src_kind"), list(wo_sources.KINDS), key="src_kind", format_func=lambda k: f"{wo_sources.KINDS[k]['icon']} {wo_sources.KINDS[k]['label']}")
        meta = wo_sources.KINDS[kind]
        c = st.columns([1.4, 2.2, 2])
        name = c[0].text_input(t("src_name"), key="src_name", placeholder=f"{kind}-prod")
        url = c[1].text_input("URL", key="src_url", placeholder=meta["path_hint"], help=t("src_url_help"))
        selector = c[2].text_input(meta["selector"], key="src_sel", placeholder=meta["selector_ex"], help=t("src_sel_help_" + kind))
        c = st.columns([1.2, 1.4, 1.6, 1, 1, 1])
        auth = c[0].selectbox(t("src_auth"), list(meta["auth"]), key="src_auth", format_func=lambda a: t("src_auth_" + a))
        user = c[1].text_input(t("src_user"), key="src_user", disabled=auth != "basic")
        secret = c[2].text_input(t("src_secret"), key="src_secret", type="password", disabled=auth == "none", help=t("src_secret_help"))
        interval = c[3].number_input(t("src_interval"), 5, 3600, 30, key="src_interval")
        env = c[4].selectbox(t("environment"), ["", "prod", "staging", "test", "dev", "qa", "dr"], key="src_env")
        site = c[5].text_input(t("ag_site"), key="src_site", placeholder="IST-DC1")
        with st.expander(t("src_advanced")):
            a = st.columns([1, 1, 2])
            verify = a[0].toggle(t("src_verify_tls"), value=True, key="src_verify")
            lookback = a[1].number_input(t("src_lookback"), 1, 1440, 15, key="src_lookback")
            ts_field = a[2].text_input(t("src_ts_field"), key="src_ts_field", placeholder="@timestamp")
            headers = st.text_area(t("src_headers"), key="src_headers", placeholder="X-Scope-OrgID: tenant-1", height=68)
        draft = wo_sources.Source(None, name.strip(), kind, url.strip(), selector.strip(), auth, user.strip(), secret, int(interval), env, site.strip(), True,
                                  bool(verify), headers, ts_field.strip(), int(lookback))
        b = st.columns([1, 1, 4])
        if b[0].button(f"🧪 {t('src_test')}", key="src-test", **wide("button")) and url.strip():
            ok, msg, sample = wo_sources.test_source(draft)
            ss["src_test"] = (ok, msg, sample)
        if b[1].button(f"💾 {t('src_save')}", key="src-save", type="primary", **wide("button")) and url.strip() and name.strip():
            try:
                store.add(draft); ss.pop("src_test", None); st.toast(t("src_saved", n=name.strip()), icon="✅"); st.rerun()
            except ValueError as e:
                st.warning(str(e))
        if ss.get("src_test"):
            ok, msg, sample = ss["src_test"]
            (st.success if ok else st.error)(msg)
            if sample:
                st.dataframe(pd.DataFrame(sample)[[c_ for c_ in ("ts", "level", "host", "service", "msg") if c_ in sample[0]]], hide_index=True, **wide("dataframe"))

    st.markdown(f"#### {t('src_list')}")

    @st.fragment(run_every="5s")
    def _rows():
        rows = store.list()
        if not rows:
            st.caption(t("src_none")); return
        from datetime import datetime as _dt
        for r in rows:
            meta = wo_sources.KINDS[r.kind]
            c = st.columns([2.2, 1.2, 2.4, 1, 1.4, 1.2, 0.7, 0.7, 0.7])
            fresh = bool(r.last_ok) and (datetime.now(UTC) - _dt.fromisoformat(r.last_ok)).total_seconds() < max(r.interval * 3, 120)
            dot = "#64748b" if not r.enabled else ("#f87171" if r.last_err else "#2dd4bf" if fresh else "#fbbf24")
            state = t("src_off") if not r.enabled else (t("src_failing") if r.last_err else t("ag_online") if fresh else t("ag_waiting"))
            c[0].markdown(f'<span style="display:inline-block;width:8px;height:8px;border-radius:4px;background:{dot};margin-right:6px;box-shadow:0 0 0 3px {dot}33"></span>'
                          f'<b>{esc(r.name)}</b> <span class="muted">· {state}</span>', unsafe_allow_html=True)
            c[1].caption(f"{meta['icon']} {meta['label']}"); c[2].caption(f"{r.url[:48]}  {r.selector[:30]}")
            c[3].caption(f"{r.interval}s · {r.env or '-'}"); c[4].caption(f"{(r.last_ok or '-')[:19]}")
            c[5].caption(f"{r.events:,} {t('events_n')} · {r.polls} {t('src_polls')}")
            if c[6].button("⏸" if r.enabled else "▶", key=f"src-tog-{r.id}", help=t("src_toggle")):
                store.update(r.id, enabled=not r.enabled, last_err=""); st.rerun(scope="app")
            if c[7].button("🧪", key=f"src-tst-{r.id}", help=t("src_test")):
                ok, msg, _ = wo_sources.test_source(r); (st.success if ok else st.error)(msg)
            if c[8].button("🗑", key=f"src-del-{r.id}", help=t("src_delete")):
                store.delete(r.id); st.rerun(scope="app")
            if r.last_err:
                st.caption(f"⚠ {r.last_err[:160]}")

    _rows()


# ------------------------------------------------------------------ page: LLM (connection, models, quality)
def page_llm() -> None:
    kb = kb_with_embedder()
    cfg = llm_cfg()
    ss = st.session_state
    st.markdown(f'<div class="wo-brand" style="padding:0 0 6px"><span style="display:inline-block;width:44px">{logo(44)}</span>'
                f'<div><div class="name" style="font-size:30px">{t("llm_page")}</div><div class="tag">{upper(t("llm_page_tag"))}</div></div></div>', unsafe_allow_html=True)
    stt = kb.llm_stats()
    chips = [("#2dd4bf" if cfg.enabled else "#64748b", f"{cfg.kind} · {cfg.model or t('as_no_llm')}"), ("#60a5fa", f"{stt['calls']} {t('llm_calls')}"),
             ("#a78bfa", f"{t('llm_success')} {pct(stt['success'], 0) if stt['success'] is not None else '-'}"),
             ("#fbbf24", f"{t('llm_ground')} {pct(stt['grounding_rate'], 0) if stt['grounding_rate'] is not None else '-'}")]
    st.markdown('<div class="chips">' + "".join(f'<span class="chip"><span class="d" style="background:{c}"></span>{esc(str(x))}</span>' for c, x in chips) + "</div>", unsafe_allow_html=True)
    tab_conn, tab_models, tab_q = st.tabs([t("llm_tab_conn"), t("llm_tab_models"), t("llm_tab_quality")])

    with tab_conn:
        c1, c2 = st.columns([3, 2], gap="medium")
        with c1, st.container(border=True):
            st.markdown(f"**🔌 {t('llm_conn_title')}**")
            st.caption(t("llm_conn_hint"))
            st.selectbox(t("llm_provider"), list(PROVIDERS), key="llm_provider", format_func=lambda x: t("prov_" + x))
            st.text_input(t("llm_base"), key="llm_base", placeholder="http://localhost:11434  ·  http://localhost:8000/v1  ·  https://api.openai.com/v1")
            st.text_input(t("llm_key"), key="llm_key", type="password")
            mc1, mc2 = st.columns([3, 1])
            mc1.text_input(t("llm_model"), key="llm_model", placeholder="qwen2.5:7b-instruct")
            if mc2.button(t("llm_list"), key="llm-list", **wide("button")):
                try:
                    ss["llm_model_list"] = llm_models(llm_cfg())
                except Exception as e:  # noqa: BLE001
                    st.error(str(e))
            if ss.get("llm_model_list"):
                pick = st.selectbox(t("llm_pick"), [""] + ss["llm_model_list"], key="llm_pick_box")
                if pick and pick != ss.get("llm_model"):
                    ss["llm_model"] = pick; st.rerun()
            st.text_input(t("llm_embed"), key="llm_embed", placeholder="bge-m3", help=t("llm_embed_help"))
            b1, b2, b3 = st.columns(3)
            if b3.button(f"💾 {t('cfg_save')}", key="llm-save", **wide("button")):
                wo_settings.save({k: ss.get(k, "") for k in ("llm_provider", "llm_base", "llm_model", "llm_key", "llm_embed")}); st.toast(t("cfg_saved"), icon="💾")
            if b1.button(t("llm_test"), key="llm_test_btn", **wide("button")):
                ok, info = llm_test(llm_cfg())
                (st.success if ok else st.error)(t("llm_ok", info=info) if ok else t("llm_fail", e=info))
            if b2.button(f"🔍 {t('llm_find_ollama')}", key="llm-find", **wide("button")):
                found = wo_ollama.discover()
                if found:
                    ss["llm_base"], ss["llm_provider"] = found, "ollama"
                    st.toast(t("llm_found", u=found), icon="✅"); st.rerun()
                else:
                    st.warning(t("llm_not_found"))
        with c2:
            st.markdown(f'<div class="card"><b>{t("llm_prov_head")}</b><br><span class="muted">{t("llm_prov_body")}</span></div>', unsafe_allow_html=True)
            st.markdown(f'<div class="card"><b>{t("llm_install_head")}</b><br><span class="muted">{t("llm_install_body")}</span></div>', unsafe_allow_html=True)
            st.code("curl -fsSL https://raw.githubusercontent.com/<org>/watchover/main/scripts/install_linux.sh | sudo bash -s -- --with-ollama", language="bash")

    with tab_models:
        base = wo_ollama.discover() if cfg.kind != "ollama" else cfg.root
        if not base:
            st.info(t("llm_models_none"))
        else:
            st.caption(t("llm_models_at", u=base))
            inst = wo_ollama.installed(base)
            run = {r["name"]: r for r in wo_ollama.running(base)}
            k = st.columns(3)
            k[0].markdown(kpi2(len(inst), t("llm_installed"), "📦", "#60a5fa", f"{sum(m['size_gb'] for m in inst):.1f} GB"), unsafe_allow_html=True)
            k[1].markdown(kpi2(len(run), t("llm_loaded"), "🧠", "#2dd4bf", ", ".join(list(run)[:2]) or "-"), unsafe_allow_html=True)
            k[2].markdown(kpi2(cfg.model or "-", t("llm_active"), "⭐", "#a78bfa", cfg.embed_model or ""), unsafe_allow_html=True)
            st.markdown(f"#### {t('llm_installed')}")
            if not inst:
                st.caption(t("llm_none_yet"))
            for m in inst:
                c = st.columns([3, 1.2, 1.2, 1.2, 1, 1])
                mem_pill = f'<span class="pill" style="background:#2dd4bf">{t("llm_in_memory")}</span>' if m["name"] in run else ""
                c[0].markdown(f"**{esc(m['name'])}** {mem_pill}", unsafe_allow_html=True)
                c[1].caption(f"{m['size_gb']} GB"); c[2].caption(m["params"] or m["family"]); c[3].caption(m["quant"])
                if c[4].button(t("llm_use"), key=f"use-{m['name']}", help=t("llm_use_help"), **wide("button")):
                    if any(e in m["name"] for e in ("embed", "bge")):
                        ss["llm_embed"] = m["name"]
                    else:
                        ss["llm_model"] = m["name"]
                    ss["llm_base"], ss["llm_provider"] = base, "ollama"; st.rerun()
                if c[5].button("🗑", key=f"rm-{m['name']}", help=t("llm_remove")):
                    wo_ollama.remove(base, m["name"]); st.rerun()
            st.markdown(f"#### {t('llm_recommended')}")
            have = {m["name"] for m in inst}
            for r in wo_ollama.RECOMMENDED:
                c = st.columns([3, 4, 1.2])
                c[0].markdown(f"**{r['name']}** <span class='muted'>· {t('llm_role_' + r['role'])}</span>", unsafe_allow_html=True)
                c[1].caption(r["note"])
                if r["name"] in have:
                    c[2].markdown(f"<span class='pill' style='background:#2dd4bf'>{t('llm_have')}</span>", unsafe_allow_html=True)
                elif c[2].button(f"⬇ {t('llm_pull')}", key=f"pull-{r['name']}", **wide("button")):
                    ss["llm_pull"] = r["name"]; st.rerun()
            custom = st.text_input(t("llm_pull_custom"), placeholder="mistral:7b-instruct", key="llm_pull_custom")
            if custom and st.button(f"⬇ {t('llm_pull')} {custom}", key="pull-custom"):
                ss["llm_pull"] = custom; st.rerun()
            if ss.get("llm_pull"):
                name = ss.pop("llm_pull")
                bar = st.progress(0.0, text=f"{t('llm_pulling')} {name}")
                try:
                    last = ""
                    for ev in wo_ollama.pull(base, name):
                        if ev.get("error"):
                            raise RuntimeError(ev["error"])
                        if ev["pct"] is not None:
                            bar.progress(min(1.0, ev["pct"]), text=f"{t('llm_pulling')} {name} · {ev['status']} · {ev['pct']:.0%}")
                        elif ev["status"] != last:
                            bar.progress(0.0, text=f"{t('llm_pulling')} {name} · {ev['status']}")
                        last = ev["status"]
                    bar.progress(1.0, text=t("llm_pulled", m=name)); st.toast(t("llm_pulled", m=name), icon="✅"); time.sleep(1); st.rerun()
                except Exception as e:  # noqa: BLE001
                    st.error(f"{name}: {e}")

    with tab_q:
        st.caption(t("llm_q_hint"))
        k = st.columns(4)
        k[0].markdown(kpi2(pct(stt["success"], 0) if stt["success"] is not None else "-", t("llm_success"), "✅", "#2dd4bf" if (stt["success"] or 0) >= 0.95 else "#fb923c", f"{stt['calls']} {t('llm_calls')} · {stt['fallbacks']} {t('llm_fallbacks')}"), unsafe_allow_html=True)
        k[1].markdown(kpi2(f"{stt['p50_ms'] / 1000:.1f} s" if stt["calls"] else "-", t("llm_latency"), "⏱", "#60a5fa", f"p95 {stt['p95_ms'] / 1000:.1f} s" if stt["calls"] else ""), unsafe_allow_html=True)
        k[2].markdown(kpi2(pct(stt["grounding_rate"], 0) if stt["grounding_rate"] is not None else "-", t("llm_ground"), "📎", "#2dd4bf" if (stt["grounding_rate"] or 0) >= 0.9 else "#f87171", f"{t('llm_cite_rate')} {pct(stt['citation_rate'], 0) if stt['citation_rate'] is not None else '-'} · {stt['invalid_citations']} {t('llm_invalid')}"), unsafe_allow_html=True)
        k[3].markdown(kpi2(pct(stt["eval_accuracy"], 0) if stt["eval_accuracy"] is not None else "-", t("llm_accuracy"), "🎯", "#a78bfa", (f"{stt['last_eval']['correct']}/{stt['last_eval']['n']} · {stt['last_eval']['model']}" if stt["last_eval"] else t("llm_no_eval"))), unsafe_allow_html=True)
        k2_ = st.columns(4)
        k2_[0].markdown(kpi2(pct(stt["approval_rate"], 0) if stt["approval_rate"] is not None else "-", t("llm_approval"), "👍", "#2dd4bf", f"{stt['thumbs_up']} 👍 · {stt['thumbs_down']} 👎"), unsafe_allow_html=True)
        k2_[1].markdown(kpi2(pct(stt["rule_acceptance"], 0) if stt["rule_acceptance"] is not None else "-", t("llm_rule_acc"), "📌", "#fbbf24", f"{stt['rules_approved']} / {stt['rules_proposed']} {t('llm_rules_prop')}"), unsafe_allow_html=True)
        k2_[2].markdown(kpi2(stt["by_kind"].get("chat", 0), t("llm_kind_chat"), "💬", "#60a5fa", f"{stt['by_kind'].get('explain', 0)} {t('llm_kind_explain')} · {stt['by_kind'].get('rules', 0)} {t('llm_kind_rules')}"), unsafe_allow_html=True)
        k2_[3].markdown(kpi2(stt["by_kind"].get("embed", 0), t("llm_kind_embed"), "🧬", "#a78bfa", cfg.embed_model or "-"), unsafe_allow_html=True)
        with st.expander(f"ℹ️ {t('llm_metrics_help_title')}"):
            st.markdown(t("llm_metrics_help"))
        st.markdown(f"#### {t('llm_bench')}")
        a = ss.get("analysis")
        bc1, bc2 = st.columns([1.5, 4])
        if bc1.button(f"🎯 {t('llm_bench_run')}", key="llm-bench", disabled=not (cfg.enabled and a is not None), **wide("button")):
            with st.spinner(t("as_thinking")):
                res = wo_eval.run_benchmark(cfg, a, current_lang())
                kb.save_eval(cfg.model, ss.get("dataset", ""), res)
                ss["llm_bench_out"] = res
            st.rerun()
        bc2.caption(t("llm_bench_hint") if (cfg.enabled and a is not None) else t("llm_bench_needs"))
        out = ss.pop("llm_bench_out", None)
        if out:
            st.success(t("llm_bench_done", c=out["correct"], n=out["n"], ms=out["latency_ms"]))
            st.dataframe(pd.DataFrame(out["detail"]), hide_index=True, **wide("dataframe"))
        runs = kb.eval_runs(10)
        if runs:
            st.dataframe(pd.DataFrame([{t("time"): r["ts"][:16], "model": r["model"], "dataset": r["dataset"], "n": r["n"], t("llm_accuracy"): f"{r['correct'] / r['n']:.0%}" if r["n"] else "-",
                                        t("llm_ground"): f"{r['grounded'] / r['n']:.0%}" if r["n"] else "-", "ms": r["latency_ms"]} for r in runs]), hide_index=True, **wide("dataframe"))
        if stt["models"]:
            st.markdown(f"#### {t('llm_per_model')}")
            st.dataframe(pd.DataFrame(stt["models"]), hide_index=True, **wide("dataframe"),
                         column_config={"success": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0%"), "citation_rate": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0%")})
        calls = kb.llm_calls(300)
        if calls:
            st.markdown(f"#### {t('llm_recent')}")
            dfc = pd.DataFrame(calls)
            dfc["ts"] = pd.to_datetime(dfc["ts"])
            st.altair_chart(alt.Chart(dfc).mark_circle(size=40).encode(x=alt.X("ts:T", title=None), y=alt.Y("latency_ms:Q", title="ms"),
                            color=alt.Color("kind:N", legend=alt.Legend(orient="top", title=None)), shape=alt.Shape("ok:N", legend=None), tooltip=["ts", "model", "kind", "latency_ms", "citations", "invalid", "error"]).properties(height=200), **wide("altair_chart"))
            st.dataframe(dfc[["ts", "provider", "model", "kind", "ok", "latency_ms", "citations", "grounded", "invalid", "error"]].head(50), hide_index=True, **wide("dataframe"))


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
if page == "src":
    page_sources()
    st.stop()
if page == "map":
    page_map()
    st.stop()
if page == "assist":
    page_assist()
    st.stop()
if page == "llm":
    page_llm()
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
_ids = wo_stamp.stamp(a)
st.caption(f"{t('ds_loaded')}: {st.session_state['dataset']} · {st.session_state.get('elapsed', 0):.1f}s · "
           f"{t('stamp_input')} {_ids['input']} · {t('stamp_engine')} {_ids['engine']} · {t('stamp_result')} {_ids['result']}", help=t("stamp_hint"))
na = a.noise_audit()
tab_over, tab_search, tab_sig, tab_inc, tab_act, tab_noise = st.tabs([t("tab_overview"), t("tab_search"), f"{t('tab_signals')} · {f['fingerprints']}", f"{t('tab_incidents')} · {f['incidents']}", f"{t('tab_actions')} · {len(store().list())}", f"{t('tab_noise')} · {na['eliminated']}"])

# ------------------------------------------------------------------ overview
@st.cache_data(show_spinner=False)
def frames(dataset: str, n: int):
    """DataFrames for the overview charts, cached per loaded dataset."""
    a_ = st.session_state["analysis"]
    df = pd.DataFrame([{"timestamp": o.timestamp, "severity": o.severity, "service": o.service or "-", "host": o.host or "-",
                        "environment": o.environment or "unknown", "origin": o.origin or "-",
                        "kind": o.kind, "source": o.source, "message": o.message, "ref": o.ref, "raw": o.raw or o.message} for o in a_.observations])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["minute"] = df["timestamp"].dt.floor("min")
    df["hay"] = (df["message"] + " \x1f" + df["service"] + " " + df["host"] + " " + df["source"] + " " + df["severity"] + " " + df["origin"] + " " + df["environment"]).str.lower()
    return df


# ------------------------------------------------------------------ log search
SRCH_FIELDS = {"service": "service", "servis": "service", "host": "host", "sunucu": "host", "severity": "severity", "seviye": "severity", "level": "severity",
               "source": "source", "kaynak": "source", "dosya": "source", "env": "environment", "environment": "environment", "ortam": "environment",
               "origin": "origin", "kind": "kind", "tur": "kind"}
SRCH_TOK = re.compile(r'(-?)(?:([\w]+):)?(?:"([^"]*)"|(\S+))')
SRCH_FACETS = ("severity", "service", "host", "source")


def parse_query(q: str) -> list[tuple[bool, str | None, str]]:
    """'timeout db-01 service:payment-api -debug "exact phrase"' -> [(neg, field|None, term)]"""
    out = []
    for m in SRCH_TOK.finditer(q or ""):
        term = m[3] if m[3] is not None else m[4]
        if term:
            out.append((m[1] == "-", SRCH_FIELDS.get((m[2] or "").lower()), term.lower()))
    return out


@st.cache_data(show_spinner=False)
def search_frame(dataset: str, n: int, q: str, regex: bool, inc: tuple, exc: tuple) -> pd.DataFrame:
    """Vectorised AND search over the pre-built lowercase haystack; field terms hit one column, -term excludes, facets narrow."""
    df = frames(dataset, n)
    m = pd.Series(True, index=df.index)
    for neg, field, term in parse_query(q):
        try:
            if field:
                col = df[field].astype(str).str.lower()
                hit = col.str.contains(term, regex=True, na=False) if regex else (col == term) | col.str.contains(term, regex=False, na=False)
            else:
                hit = df["hay"].str.contains(term, regex=regex, na=False)
        except re.error:
            hit = df["hay"].str.contains(term, regex=False, na=False)
        m &= ~hit if neg else hit
    for col, vals in inc:
        if vals:
            m &= df[col].isin(vals)
    for col, vals in exc:
        if vals:
            m &= ~df[col].isin(vals)
    return df[m]


def highlight(text: str, terms: list[str]) -> str:
    out = esc(text)
    for term in sorted({x for x in terms if x}, key=len, reverse=True):
        out = re.sub(re.escape(esc(term)), lambda mm: f"<mark>{mm[0]}</mark>", out, flags=re.I)
    return out


with tab_over:
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
    # --- reduction funnel: one card, five stages, proportional (log) bars
    steps = [
        ("raw_events", f["raw_events"], "🧾", "#60a5fa", f"{n_err / max(f['raw_events'], 1):.0%} ERROR+" if f["raw_events"] else ""),
        ("fingerprints", f["fingerprints"], "🧬", "#2dd4bf", f"{f['reduction']}× {t('reduction')}"),
        ("meaningful", f["meaningful_signals"], "📡", "#fbbf24", f"{top_sig.id} · {t('burst')} {top_sig.burst_score}" if top_sig else ""),
        ("incidents", f["incidents"], "🚨", "#f87171" if n_crit else "#fb923c", f"{n_crit} critical" if n_crit else t("no_critical")),
        ("actions", len(acts_all), "✅", "#a78bfa", f"{n_open} {t('open_n')}" if acts_all else t("none_yet")),
    ]
    import math
    vmax = max(1, max(v for _, v, *_ in steps))
    stages = "".join(
        f'<div class="stage" style="--acc:{acc}"><div class="lb"><span class="dot"></span>{upper(t(key))}<span class="ic">{ic}</span></div>'
        f'<div class="v">{v:,}</div><div class="sub">{esc(str(sub))}</div>'
        f'<div class="bar" style="width:{max(6, 100 * math.log(v + 1) / math.log(vmax + 1)):.0f}%"></div></div>'
        for key, v, ic, acc, sub in steps)
    head = t("funnel_head", raw=f"{f['raw_events']:,}", inc=f["incidents"], x=f["reduction"])
    st.markdown(f'<div class="funnel"><div class="head"><span class="ttl">{t("funnel_title")}</span><span class="muted">{head}</span></div>'
                f'<div class="stages">{stages}</div></div>', unsafe_allow_html=True)
    # --- facts strip: one card, seven facts
    fmts = ", ".join(sorted({r["format"] for r in a.report}))
    envs = prof.get("environments", {})
    env_err_total = sum(v["errors"] for v in envs.values()) or 1
    env_sub = " · ".join(f"{e} {v['errors'] / env_err_total:.0%}" for e, v in list(envs.items())[:3] if v["errors"]) or ", ".join(list(envs)[:3])
    second = [
        ("files_n", len(prof["files"]), "📁", fmts),
        ("records_n", f"{prof['records']:,}", "🗂", f"{max(a.report, key=lambda r: r['rows'])['file'].split('/')[-1]} · {max(r['rows'] for r in a.report):,}" if a.report else ""),
        ("services_n", len(prof["services"]), "🧩", f"{svc_counts.index[0]} · {svc_counts.iloc[0]:,}" if len(svc_counts) else "-"),
        ("hosts_n", len(prof["hosts"]), "🖥", f"{host_counts.index[0]} · {host_counts.iloc[0]:,}" if len(host_counts) else "-"),
        ("error_classes", prof["error_classes"], "💥", f"{err_sigs[0].template[:32]} · {err_sigs[0].count}" if err_sigs else "-"),
        ("span_min", f"{tr['minutes']} {t('min_short')}", "⏱", f"{tr['start'][11:16]} → {tr['end'][11:16]} · {prof.get('bucket', '1min')}"),
        ("environments", len([e for e in envs if e != "unknown"]) or len(envs), "🌍", env_sub),
    ]
    st.markdown('<div class="facts">' + "".join(
        f'<div class="fact"><div class="lb">{ic} {upper(t(key))}</div><div class="v">{v}</div><div class="sub" title="{esc(str(sub))}">{esc(str(sub))}</div></div>'
        for key, v, ic, sub in second) + "</div>", unsafe_allow_html=True)
    # --- one selector instead of twelve "Detail" buttons
    all_keys = [k for k, *_ in steps] + [k for k, *_ in second]
    icons = {k: ic for k, _, ic, *_ in steps} | {k: ic for k, _, ic, _ in second}
    def render_detail(detail: str) -> None:
        """Body of one detail view (rendered inside a modal dialog)."""
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

    def _pick_detail() -> None:
        st.session_state["open_detail"] = st.session_state.get("detail")
        st.session_state["detail"] = None                     # pills behave like buttons: the dialog is the state

    st.pills(t("detail_pick"), all_keys, format_func=lambda k: f"{icons[k]} {cap(t(k))}", selection_mode="single", key="detail", on_change=_pick_detail)
    if (_k := st.session_state.pop("open_detail", None)):
        @st.dialog(f"{icons[_k]} {t('d_' + _k)}", width="large")
        def _detail_dialog() -> None:
            render_detail(_k)
        _detail_dialog()
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
with tab_search:
    ss = st.session_state
    ss.setdefault("srch_inc", {}); ss.setdefault("srch_exc", {})

    def _srch_add(col: str, val: str, neg: bool = False) -> None:
        ss["srch_exc" if neg else "srch_inc"].setdefault(col, set()).add(val)

    def _srch_remove(col: str, val: str, neg: bool) -> None:
        ss["srch_exc" if neg else "srch_inc"].get(col, set()).discard(val)

    def _srch_clear() -> None:
        ss["srch_inc"], ss["srch_exc"] = {}, {}
        for c_ in SRCH_FACETS:
            ss[f"facet_{c_}"] = []

    def _facet_changed(col: str) -> None:
        ss["srch_inc"][col] = set(ss.get(f"facet_{col}") or [])

    qc, rc = st.columns([6, 1])
    q = qc.text_input(t("tab_search"), key="srch_q", placeholder=t("srch_ph"), label_visibility="collapsed", help=t("srch_help"))
    regex = rc.toggle("Regex", key="srch_regex")
    inc_t = tuple((c_, tuple(sorted(v))) for c_, v in sorted(ss["srch_inc"].items()) if v)
    exc_t = tuple((c_, tuple(sorted(v))) for c_, v in sorted(ss["srch_exc"].items()) if v)
    df_all = frames(st.session_state["dataset"], len(a.observations))
    res = search_frame(st.session_state["dataset"], len(a.observations), q, regex, inc_t, exc_t)
    terms = [x_[2] for x_ in parse_query(q) if not x_[0]]
    chips = [(c_, v, False) for c_, vs in ss["srch_inc"].items() for v in sorted(vs)] + [(c_, v, True) for c_, vs in ss["srch_exc"].items() for v in sorted(vs)]
    if chips:
        cc = st.columns([1] * min(len(chips), 5) + [1.2])
        for i_, (c_, v, neg) in enumerate(chips):
            cc[i_ % 5].button(f"{'−' if neg else '+'} {t(c_)}: {v}  ✕", key=f"chip-{c_}-{v}-{neg}", on_click=_srch_remove, args=(c_, v, neg), **wide("button"))
        cc[-1].button(t("srch_clear"), key="srch_clear", on_click=_srch_clear, **wide("button"))
    st.markdown(f"<span style='font-size:20px;font-weight:800'>{len(res):,}</span> <span class='muted'>/ {len(df_all):,} {t('srch_hits')}</span>", unsafe_allow_html=True)
    fcols = st.columns(len(SRCH_FACETS))
    for c_, col_ in zip(fcols, SRCH_FACETS):
        vc = res[res[col_] != "-"][col_].value_counts().head(8)
        opts = sorted(set(vc.index) | ss["srch_inc"].get(col_, set()))
        if not opts:
            continue
        ss[f"facet_{col_}"] = [x_ for x_ in sorted(ss["srch_inc"].get(col_, set())) if x_ in opts]
        c_.markdown(f"<div class='facet-h'>{upper(t(col_))}</div>", unsafe_allow_html=True)
        c_.pills(col_, opts, format_func=lambda v, vc=vc: f"{v} · {vc.get(v, 0):,}", selection_mode="multi", key=f"facet_{col_}", label_visibility="collapsed", on_change=_facet_changed, args=(col_,))
    if len(res):
        per_min = res.groupby(["minute", "severity"]).size().rename("events").reset_index()
        span_s = (res["timestamp"].max() - res["timestamp"].min()).total_seconds()
        st.altair_chart(alt.Chart(per_min).mark_bar().encode(x=alt.X("minute:T", title=None, axis=alt.Axis(format="%H:%M:%S" if span_s < 600 else "%H:%M", tickCount=12)), y=alt.Y("events:Q", title=None, axis=alt.Axis(format="d")),
                        color=alt.Color("severity:N", scale=SEV_SCALE, legend=None), tooltip=[alt.Tooltip("minute:T", format="%H:%M"), "severity", "events"]).properties(height=90), **wide("altair_chart"))
        view = res.assign(time=res["timestamp"].dt.strftime("%H:%M:%S"))[["time", "severity", "service", "host", "source", "message"]].head(2000)
        ev = st.dataframe(view, hide_index=True, **wide("dataframe"), height=min(520, 38 + 35 * min(len(view), 14)), on_select="rerun", selection_mode="single-row", key="srch_tbl",
                          column_config={"message": st.column_config.TextColumn(width="large"), "time": st.column_config.TextColumn(width="small")})
        if len(res) > 2000:
            st.caption(t("srch_more", n=len(res) - 2000))
        sel = ev.selection.rows if ev and ev.selection else []
        if sel:
            row = res.iloc[sel[0]]
            inc_of = {o_.ref: inc_.id for inc_ in a.incidents for sid_ in inc_.signal_ids for o_ in a.signal_by_id[sid_].observations}
            inc_id = inc_of.get(row["ref"])
            st.markdown(f'<div class="rowcard"><span class="muted">{row["timestamp"]:%Y-%m-%d %H:%M:%S}</span> · {pill(row["severity"])} · <b>{esc(row["service"])}</b> · {esc(row["host"])} · '
                        f'<span class="mono">{esc(row["ref"])}</span>' + (f' · 🚨 <b>{inc_id}</b> {t("srch_in_inc")}' if inc_id else "") +
                        f'<br><span class="mono">{highlight(str(row["raw"])[:600], terms)}</span></div>', unsafe_allow_html=True)
            bc = st.columns([2, 0.5] * len(SRCH_FACETS) + [1.5])
            for i_, col_ in enumerate(SRCH_FACETS):
                v = str(row[col_])
                bc[i_ * 2].button(f"+ {t(col_)}: {v[:18]}", key=f"rf-{col_}", on_click=_srch_add, args=(col_, v), help=t("srch_add"), **wide("button"))
                bc[i_ * 2 + 1].button("−", key=f"rx-{col_}", on_click=_srch_add, args=(col_, v, True), help=f"{t('srch_exclude')}: {v}", **wide("button"))
            if inc_id:
                bc[-1].button(f"🚨 {inc_id}", key="rf-inc", on_click=lambda i=inc_id: st.session_state.__setitem__("inc_pick", i), help=t("fc_open"), **wide("button"))
        else:
            st.caption(t("srch_pick"))
    else:
        st.info(t("srch_none"))


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
        iid = st.selectbox(t("incident"), ids_, key="inc_radio",
                           format_func=lambda i: f"{i} · {a.incident_by_id[i].severity} · {a.incident_by_id[i].score} · {a.incident_by_id[i].title[:70]}")
        inc = a.incident_by_id[iid]
        incident_flashcard(inc, a, "tab")
        learning_panel(inc, a)
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
