"""Service-level report: one self-contained HTML page a director can read or print to PDF. Deterministic: built only
from the live store's SLO figures, the error breakdown and (when present) the incident cards of the current analysis.
No external assets, inline SVG charts, print stylesheet."""
from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

T = {
    "tr": {"title": "Servis seviyesi raporu", "period": "Dönem", "scope": "Kapsam", "all": "tüm ortamlar ve sunucular", "generated": "Oluşturulma",
           "summary": "Yönetici özeti", "availability": "Erişilebilirlik", "p95": "p95 gecikme", "budget": "Kalan hata bütçesi", "events": "Olay",
           "errors": "hata", "breach": "SLO altı dakika", "target": "hedef", "sla": "SLA", "ok": "hedefin üstünde", "miss": "hedefin altında",
           "chart": "Dakikalık erişilebilirlik ve hata bütçesi", "by_service": "Hataların servise dağılımı", "by_host": "Hataların sunucuya dağılımı",
           "latency": "Servis gecikmeleri (ms)", "templates": "En sık hata şablonları", "incidents": "Öne çıkan incident'lar", "root": "Kök neden",
           "recommend": "Önerilen aksiyon", "service": "Servis", "host": "Sunucu", "count": "Adet", "share": "Pay", "n": "n", "min": "dk",
           "no_data": "Bu kapsamda ve dönemde olay yok.", "footer": "Watchover · deterministik motor · her rakam canlı akıştaki olaylardan hesaplanır",
           "s_ok": "Erişilebilirlik {a} ile {slo} SLO hedefinin üstünde; {sla} SLA hedefi korunuyor.",
           "s_slo": "Erişilebilirlik {a}, {slo} SLO hedefinin altında; SLA ({sla}) henüz ihlal edilmedi.",
           "s_sla": "Erişilebilirlik {a}: hem SLO ({slo}) hem SLA ({sla}) hedefi ihlal edildi.",
           "s_err": "{errors} hata / {total} olay; hata bütçesinin %{used} kadarı tüketildi, {breach} {unit} SLO altında kaldı.",
           "s_p95": "p95 gecikme {p95} ms (hedef {tgt} ms).", "s_top": "Hataların en büyük kaynağı {svc} ({share}).",
           "s_inc": "{n} incident kartı açık; en yüksek öncelikli: {title}.", "print": "Yazdır / PDF olarak kaydet",
           "by_sender": "Hataların kaynağa dağılımı (ajan / platform)", "sender": "Kaynak", "bucket_minute": "dakika", "bucket_hour": "saat", "bucket_day": "gün"},
    "en": {"title": "Service level report", "period": "Period", "scope": "Scope", "all": "all environments and hosts", "generated": "Generated",
           "summary": "Executive summary", "availability": "Availability", "p95": "p95 latency", "budget": "Error budget left", "events": "Events",
           "errors": "errors", "breach": "minutes below SLO", "target": "target", "sla": "SLA", "ok": "above target", "miss": "below target",
           "chart": "Per-minute availability and error budget", "by_service": "Errors by service", "by_host": "Errors by host",
           "latency": "Service latency (ms)", "templates": "Most frequent error templates", "incidents": "Key incidents", "root": "Root cause",
           "recommend": "Recommended action", "service": "Service", "host": "Host", "count": "Count", "share": "Share", "n": "n", "min": "min",
           "no_data": "No events in this scope and period.", "footer": "Watchover · deterministic engine · every figure is computed from live events",
           "s_ok": "Availability {a} is above the {slo} SLO; the {sla} SLA holds.",
           "s_slo": "Availability {a} is below the {slo} SLO; the SLA ({sla}) is not breached yet.",
           "s_sla": "Availability {a}: both the SLO ({slo}) and the SLA ({sla}) are breached.",
           "s_err": "{errors} errors / {total} events; {used}% of the error budget is used, {breach} {unit} were below the SLO.",
           "s_p95": "p95 latency {p95} ms (target {tgt} ms).", "s_top": "The largest error source is {svc} ({share}).",
           "s_inc": "{n} incident cards are open; highest priority: {title}.", "print": "Print / save as PDF",
           "by_sender": "Errors by sender (agent / platform)", "sender": "Sender", "bucket_minute": "minutes", "bucket_hour": "hours", "bucket_day": "days"},
}
SEV_COLOR = {"CRITICAL": "#f87171", "ERROR": "#fb923c", "WARN": "#fbbf24", "INFO": "#60a5fa"}


def _pct(v, d=2) -> str:
    return "-" if v is None else f"{v * 100:.{d}f}%"


def _e(x) -> str:
    return html.escape(str(x))


def _svg_chart(per_minute: list[dict], target: float, w: int = 900, h: int = 180) -> str:
    """Availability per minute (bars: green ≥ target, red below) and the error-budget line, inline SVG."""
    n = max(len(per_minute), 1)
    pad, bw = 34, (w - 60) / n
    lo = min([r["availability"] for r in per_minute if r["availability"] is not None] + [target]) if per_minute else target
    lo = max(0.0, min(lo, target) - 0.01)
    def y(v: float) -> float:
        return pad + (h - 2 * pad) * (1 - (v - lo) / (1 - lo)) if 1 - lo > 0 else pad
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" font-family="inherit" font-size="10">']
    parts.append(f'<line x1="40" x2="{w - 20}" y1="{y(target):.1f}" y2="{y(target):.1f}" stroke="#94a3b8" stroke-dasharray="4 3"/>')
    parts.append(f'<text x="{w - 18}" y="{y(target) - 3:.1f}" text-anchor="end" fill="#64748b">SLO {_pct(target, 1)}</text>')
    pts = []
    for i, r in enumerate(per_minute):
        x = 40 + i * bw
        if r["availability"] is not None:
            top = y(r["availability"]); col = "#2dd4bf" if r["availability"] >= target else "#f87171"
            parts.append(f'<rect x="{x + 1:.1f}" y="{top:.1f}" width="{max(bw - 2, 1):.1f}" height="{h - pad - top:.1f}" fill="{col}" opacity="0.85"><title>{r["label"]} · {_pct(r["availability"])} · {r["errors"]}/{r["total"]}</title></rect>')
        if r["budget_left"] is not None:
            pts.append(f"{x + bw / 2:.1f},{pad + (h - 2 * pad) * (1 - r['budget_left']):.1f}")
        if i % max(1, n // 8) == 0:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{h - 8}" text-anchor="middle" fill="#64748b">{r["label"]}</text>')
    if pts:
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#a78bfa" stroke-width="2"/>')
        parts.append(f'<text x="42" y="{pad - 6}" fill="#a78bfa">budget</text>')
    parts.append(f'<text x="4" y="{pad + 4}" fill="#64748b">100%</text><text x="4" y="{h - pad + 4}" fill="#64748b">{_pct(lo, 1)}</text></svg>')
    return "".join(parts)


def _table(headers: list[str], rows: list[list], bar_col: int | None = None) -> str:
    if not rows:
        return "<p class='muted'>–</p>"
    mx = max((float(r[bar_col]) for r in rows), default=0) if bar_col is not None else 0
    out = ["<table><thead><tr>" + "".join(f"<th>{_e(h)}</th>" for h in headers) + "</tr></thead><tbody>"]
    for r in rows:
        cells = []
        for j, c in enumerate(r):
            if j == bar_col and mx:
                cells.append(f'<td><span class="bar" style="width:{100 * float(c) / mx:.0f}%"></span>{_e(c)}</td>')
            else:
                cells.append(f"<td>{_e(c)}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(out) + "</tbody></table>"


def figures_live(ls, env: str | None = None, host=None, window_min: int = 60) -> dict:
    """Figures for the last window_min minutes from the in-memory live store (same shape as history.figures)."""
    from collections import Counter
    slo, det = ls.slo(window_min, env, host), ls.slo_detail(window_min, env, host)
    start = datetime.now(UTC) - timedelta(minutes=window_min)
    by_sender = Counter(o.attributes.get("agent", "-") for o in ls.snapshot(env, host) if o.timestamp >= start and o.severity in ("ERROR", "CRITICAL"))
    series = [{"label": r["minute"].strftime("%H:%M"), "when": r["minute"], "total": r["total"], "errors": r["errors"], "availability": r["availability"], "budget_left": r["budget_left"]}
              for r in det["per_minute"]]
    return {**slo, "allowed_errors": det["allowed_errors"], "breach_buckets": det["breach_minutes"], "series": series, "bucket": "minute",
            "by_service": det["by_service"], "by_host": det["by_host"], "by_sender": by_sender.most_common(10), "latency": det["latency"],
            "templates": det["templates"], "source": "live"}


def period_label(fig: dict, lang: str = "tr") -> str:
    L = T.get(lang, T["tr"])
    if not fig["series"]:
        return "-"
    first, last = fig["series"][0]["when"], fig["series"][-1]["when"]
    return f'{first.strftime("%Y-%m-%d %H:%M")} → {last.strftime("%Y-%m-%d %H:%M")} UTC · {len(fig["series"])} {L["bucket_" + fig["bucket"]]}'


def slo_report(fig: dict, analysis=None, scope: str = "", period: str = "", lang: str = "tr", org: str = "", logo_svg: str = "") -> str:
    """HTML report from a figures dict (report.figures_live or history.figures)."""
    L = T.get(lang, T["tr"])
    slo = det = fig
    now = datetime.now(UTC)
    tgt, sla = slo["slo"]["availability"], slo["sla"]["availability"]
    a = slo["availability"]
    scope = scope or L["all"]
    used = None if slo["error_budget"] is None else round((1 - slo["error_budget"]) * 100)
    # --- executive summary sentences (deterministic)
    sents = []
    if a is None:
        sents.append(L["no_data"])
    else:
        key = "s_ok" if a >= tgt else ("s_slo" if a >= sla else "s_sla")
        sents.append(L[key].format(a=_pct(a), slo=_pct(tgt, 1), sla=_pct(sla, 1)))
        sents.append(L["s_err"].format(errors=slo["errors"], total=slo["total"], used=used if used is not None else "-", breach=det["breach_buckets"], unit=L["bucket_" + fig["bucket"]]))
        if slo["p95_ms"] is not None:
            sents.append(L["s_p95"].format(p95=f"{slo['p95_ms']:.0f}", tgt=slo["slo"]["p95_ms"]))
        if det["by_service"] and slo["errors"]:
            svc, n = det["by_service"][0]
            sents.append(L["s_top"].format(svc=svc, share=_pct(n / slo["errors"], 0)))
    incs = list(getattr(analysis, "incidents", []) or [])[:8] if analysis is not None else []
    if incs:
        sents.append(L["s_inc"].format(n=len(getattr(analysis, "incidents", [])), title=incs[0].title[:90]))
    # --- tiles
    def tile(label, value, sub, color):
        return f'<div class="tile" style="border-top:3px solid {color}"><div class="lbl">{_e(label)}</div><div class="val">{_e(value)}</div><div class="sub">{_e(sub)}</div></div>'
    tiles = [
        tile(L["availability"], _pct(a), f'{L["target"]} {_pct(tgt, 1)} · {L["sla"]} {_pct(sla, 1)} · {L["ok"] if slo["slo_ok"] else L["miss"]}', "#2dd4bf" if slo["slo_ok"] else "#f87171"),
        tile(L["p95"], "-" if slo["p95_ms"] is None else f"{slo['p95_ms']:.0f} ms", f'{L["target"]} {slo["slo"]["p95_ms"]} ms', "#2dd4bf" if slo["p95_ms"] is None or slo["p95_ms"] <= slo["slo"]["p95_ms"] else "#fbbf24"),
        tile(L["budget"], _pct(slo["error_budget"], 0), f'{det["allowed_errors"]} / {slo["errors"]} {L["errors"]}', "#2dd4bf" if (slo["error_budget"] or 0) > 0.25 else "#f87171"),
        tile(L["events"], f'{slo["total"]:,}', f'{slo["errors"]} {L["errors"]} · {det["breach_buckets"]} {L["bucket_" + fig["bucket"]]} {L["breach"]}', "#60a5fa"),
    ]
    tot_err = max(slo["errors"], 1)
    by_service = _table([L["service"], L["count"], L["share"]], [[s, n, _pct(n / tot_err, 0)] for s, n in det["by_service"][:10]], 1)
    by_host = _table([L["host"], L["count"], L["share"]], [[h, n, _pct(n / tot_err, 0)] for h, n in det["by_host"][:10]], 1)
    by_sender = _table([L["sender"], L["count"], L["share"]], [[h, n, _pct(n / tot_err, 0)] for h, n in det.get("by_sender", [])[:10]], 1)
    latency = _table([L["service"], L["n"], "p50", "p95", "max"], [[r["service"], r["n"], f"{r['p50']:.0f}", f"{r['p95']:.0f}", f"{r['max']:.0f}"] for r in det["latency"][:10]], 3)
    templates = _table([L["count"], "Severity", L["service"], "Template"], [[g["count"], g["severity"], ", ".join(g["services"][:3]), g["template"][:110]] for g in det["templates"][:8]], 0)
    inc_html = ""
    for inc in incs:
        col = SEV_COLOR.get(inc.severity, "#60a5fa")
        rec = inc.recommendations[0] if getattr(inc, "recommendations", None) else "-"
        inc_html += (f'<div class="inc"><span class="sev" style="background:{col}">{_e(inc.severity)}</span> <b>{_e(inc.id)}</b> {_e(inc.title[:120])}'
                     f'<div class="muted">{L["root"]}: {_e(inc.root_cause_reason[:220])}</div><div class="muted">{L["recommend"]}: {_e(rec[:200])}</div>'
                     f'<div class="muted">{", ".join(_e(s) for s in inc.affected_services[:6])}</div></div>')
    css = """
    body{font-family:-apple-system,Inter,Helvetica,Arial,sans-serif;color:#0f172a;margin:0;background:#f8fafc}
    .page{max-width:1000px;margin:0 auto;padding:28px 32px;background:#fff}
    header{display:flex;align-items:center;gap:14px;border-bottom:2px solid #2dd4bf;padding-bottom:12px;margin-bottom:18px}
    header .brand{font-size:22px;font-weight:700;color:#0f766e}header .meta{margin-left:auto;text-align:right;color:#64748b;font-size:12px;line-height:1.6}
    h1{font-size:24px;margin:0 0 4px}h2{font-size:15px;margin:26px 0 8px;color:#0f766e;text-transform:uppercase;letter-spacing:.04em}
    .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.tile{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}
    .tile .lbl{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.06em}.tile .val{font-size:26px;font-weight:700;margin:4px 0}.tile .sub{font-size:11px;color:#64748b}
    .summary{background:#ecfeff;border-left:4px solid #2dd4bf;padding:12px 16px;border-radius:6px;font-size:14px;line-height:1.6}
    table{border-collapse:collapse;width:100%;font-size:12px}th,td{border-bottom:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}th{color:#64748b;font-weight:600}
    td .bar{display:inline-block;height:8px;background:#2dd4bf55;border-radius:4px;margin-right:6px;vertical-align:middle;max-width:60%}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.muted{color:#64748b;font-size:12px}
    .inc{border:1px solid #e2e8f0;border-radius:8px;padding:10px 12px;margin:8px 0;font-size:13px}.sev{color:#fff;font-size:10px;padding:2px 7px;border-radius:10px;font-weight:700}
    footer{margin-top:28px;color:#94a3b8;font-size:11px;border-top:1px solid #e2e8f0;padding-top:8px;display:flex;justify-content:space-between}
    .print{float:right;font-size:12px;color:#0f766e;cursor:pointer;background:none;border:1px solid #2dd4bf;border-radius:6px;padding:4px 10px}
    @media print{.print{display:none}.page{padding:0}body{background:#fff}.tiles{break-inside:avoid}h2{break-after:avoid}}
    """
    period = period or period_label(fig, lang)
    return f"""<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{_e(L["title"])} · Watchover</title><style>{css}</style></head><body><div class="page">
<header><span style="display:inline-block;width:40px;height:40px">{logo_svg}</span><span class="brand">Watchover</span>
<div class="meta">{_e(org)}<br>{L["generated"]}: {now.strftime("%Y-%m-%d %H:%M")} UTC</div></header>
<button class="print" onclick="window.print()">{_e(L["print"])}</button>
<h1>{_e(L["title"])}</h1><div class="muted">{L["period"]}: {period} · {L["scope"]}: {_e(scope)}</div>
<h2>{L["summary"]}</h2><div class="summary">{" ".join(_e(x) for x in sents)}</div>
<h2>KPI</h2><div class="tiles">{"".join(tiles)}</div>
<h2>{L["chart"]}</h2>{_svg_chart(det["series"], tgt)}
<div class="grid2"><div><h2>{L["by_service"]}</h2>{by_service}</div><div><h2>{L["by_host"]}</h2>{by_host}</div></div>
<div class="grid2"><div><h2>{L["by_sender"]}</h2>{by_sender}</div><div><h2>{L["latency"]}</h2>{latency}</div></div>
{('<h2>' + L["templates"] + '</h2>' + templates) if det.get("templates") else ''}
{('<h2>' + L["incidents"] + '</h2>' + inc_html) if inc_html else ''}
<footer><span>{L["footer"]}</span><span>SLO {_pct(tgt, 1)} · SLA {_pct(sla, 1)} · p95 ≤ {slo["slo"]["p95_ms"]} ms</span></footer>
</div></body></html>"""
