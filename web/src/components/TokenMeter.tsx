import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Coins } from "lucide-react";
import { get } from "../api";
import { useT } from "../i18n";
import { Btn } from "./ui";

/** Token meter in the top bar: today's LLM token total as a small pill, refreshed every 15 s and right after a chat or
 *  review answer (the `wo:llm` event). The panel breaks it down: the last call, today / 7 days / 30 days / all time,
 *  today's split by purpose and by model. Counts come from the provider (Ollama, OpenAI-compatible, Anthropic, Gemini);
 *  when a provider reports none they are estimated from the text and marked. */
export function fmtTok(n: number | null | undefined): string {
  const v = Number(n || 0);
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(v >= 10_000_000 ? 0 : 1)}M`;
  if (v >= 1_000) return `${(v / 1_000).toFixed(v >= 10_000 ? 0 : 1)}k`;
  return String(v);
}

export function TokenMeter() {
  const { t } = useT(); const [open, setOpen] = useState(false); const ref = useRef<HTMLDivElement>(null);
  const q = useQuery({ queryKey: ["tokens"], queryFn: () => get("/llm/tokens"), refetchInterval: 15000 });
  useEffect(() => { const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); }; document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h); }, []);
  useEffect(() => { const h = () => q.refetch(); window.addEventListener("wo:llm", h); return () => window.removeEventListener("wo:llm", h); }, [q]);
  const d = q.data; const today = d?.today; const last = d?.last;
  const Row = ({ k, v }: { k: string; v: any }) => <div className="row between item"><span className="muted">{k}</span><span><b>{fmtTok(v?.total)}</b> <span className="dim small">({fmtTok(v?.in)} {t("tok_in")} · {fmtTok(v?.out)} {t("tok_out")} · {v?.calls ?? 0} {t("tok_calls")})</span></span></div>;
  return <div className="bell tokmeter" ref={ref}>
    <Btn kind="ghost" sm onClick={() => setOpen((o) => !o)} title={t("tok_title")}><Coins size={16} /><span className="tokpill">{fmtTok(today?.total)}{last ? <span className="dim"> · {t("tok_last_short")} {fmtTok(last.total)}</span> : null}</span></Btn>
    {open && <div className="bell-panel">
      <div className="row between" style={{ marginBottom: 6 }}><b>🪙 {t("tok_title")}</b><span className="dim small">{t("tok_utc")}</span></div>
      {!d ? <div className="muted small">…</div> : <>
        {last ? <div className="item"><div className="row between"><span className="muted">{t("tok_last")}</span><span><b>{fmtTok(last.total)}</b> <span className="dim small">({fmtTok(last.tokens_in)} {t("tok_in")} · {fmtTok(last.tokens_out)} {t("tok_out")} · {last.latency_ms} ms{last.tokens_est ? ` · ${t("tok_est_mark")}` : ""})</span></span></div>
          <div className="dim small">{last.kind} · {last.model} · {(last.ts || "").replace("T", " ").slice(0, 16)}</div></div> : <div className="muted small item">{t("tok_none")}</div>}
        <Row k={t("tok_today")} v={d.today} /><Row k={t("tok_week")} v={d.week} /><Row k={t("tok_month")} v={d.month} /><Row k={t("tok_all")} v={d.all} />
        {today?.estimated > 0 && <div className="dim small" style={{ padding: "6px 6px 0" }}>≈ {fmtTok(today.estimated)} {t("tok_est")}</div>}
        {d.by_kind?.length > 0 && <div className="item"><div className="muted small" style={{ marginBottom: 4 }}>{t("tok_by_kind")}</div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>{d.by_kind.map((k: any) => <span key={k.kind} className="badge" style={{ textTransform: "none" }}>{k.kind} · {fmtTok(k.total)} ({k.calls})</span>)}</div></div>}
        {d.by_model?.length > 0 && <div className="item"><div className="muted small" style={{ marginBottom: 4 }}>{t("tok_by_model")}</div>
          <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>{d.by_model.map((m: any) => <span key={m.model} className="badge" style={{ textTransform: "none" }}>{m.model} · {fmtTok(m.total)} ({m.calls})</span>)}</div></div>}
        <div className="dim small" style={{ padding: "6px 6px 0" }}>{t("tok_hint")}</div>
      </>}
    </div>}
  </div>;
}
