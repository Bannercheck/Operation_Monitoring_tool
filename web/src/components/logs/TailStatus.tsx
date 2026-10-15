import { Pause, Play } from "lucide-react";
import { useT } from "../../i18n";
import { errText, fullN, type LgErr } from "./lib";

/** Live tail status under the histogram: arrival and match rate, the per-second display cap when it bit, a gap when the stream
 *  outran a paused tab, and Pause / Resume. */
export function TailStatus({ info, paused, buffered, onPause, onResume, err }: { info: any; paused: boolean; buffered: number; onPause: () => void; onResume: () => void; err: LgErr | null }) {
  const { t, lang } = useT();
  const rate = (v: number | null | undefined) => (v == null ? "–" : v >= 100 ? fullN(Math.round(v), lang) : v.toLocaleString(lang === "tr" ? "tr-TR" : "en-US", { maximumFractionDigits: 1 }));
  return <div className="lg-tail">
    <span className={`lg-tail-dot ${paused ? "paused" : ""}`} />
    <span>{paused ? t("lg_tail_paused", { n: fullN(buffered, lang) }) : t("lg_tail_rate", { in: rate(info?.rate?.in_per_s), m: rate(info?.rate?.match_per_s) })}</span>
    <button className="btn sm" onClick={paused ? onResume : onPause}>{paused ? <Play size={12} /> : <Pause size={12} />} {paused ? t("lg_resume") : t("lg_pause")}</button>
    {info?.dropped > 0 && <span className="lg-tail-cap">{t("lg_tail_cap", { n: info.cap_per_s, d: fullN(info.dropped, lang) })}</span>}
    {info?.skipped > 0 && <span className="lg-tail-cap">{t("lg_tail_gap", { n: fullN(info.skipped, lang) })}</span>}
    {err && <span className="lg-tail-cap">{errText(err, lang)}</span>}
  </div>;
}
