import { useQuery } from "@tanstack/react-query";
import { get } from "../api";
import { useT } from "../i18n";

/** The receiver address the panel uses for agents: OpenShift's ingest Route (WATCHOVER_INGEST_URL), else <this host>:8600. */
export function useIngestBase(): string {
  const hq = useQuery({ queryKey: ["api-health"], queryFn: () => get("/health"), staleTime: 300000 });
  const host = typeof window !== "undefined" ? window.location.hostname : "watchover";
  return ((hq.data as any)?.ingest as string) || `http://${host}:8600`;
}

/** The current one-line install commands from /agents/install-info (verified bootstrap, release key, edge CA), so a Watchover
 *  upgrade or a new edge certificate never leaves a stale command on screen. `auth` is "--enroll-key wk_… --env prod" or
 *  "--token wo_… --env prod". */
export function InstallCmds({ auth, compact }: { auth: string; compact?: boolean }) {
  const { t } = useT();
  const base = useIngestBase();
  const q = useQuery({ queryKey: ["agent-install-info"], queryFn: () => get("/agents/install-info"), staleTime: 60000, refetchInterval: 300000 });
  const i: any = q.data;
  if (!i) return <pre className="code">{`curl -fsSL ${base}/agent/install.sh | sudo bash -s -- --url ${base}/ingest ${auth}`}</pre>;
  const fill = (c: string) => c.split("{BASE}").join(base).split("{AUTH}").join(auth);
  const https: string = i.https || "";
  const sameAsHttps = !!https && base.startsWith("https://") && i.https_base === base;
  const httpLabel = i.signed ? (base.startsWith("https://") ? t("agu_cmd_https") : t("agu_cmd_http")) : t("agu_cmd_legacy");
  return <div className="stack" style={{ gap: 6 }}>
    {https && <div><div className="small muted">{t("agu_cmd_https")}</div><pre className="code">{fill(https)}</pre></div>}
    {!sameAsHttps && <div><div className="small muted">{httpLabel}</div><pre className="code">{fill(i.http)}</pre></div>}
    {!compact && <div className="dim small">{i.signed ? t("agu_cmd_note") : t("agu_unsigned")}{https && i.ca_sha256 ? " " + t("agu_cmd_ca") : ""}
      {i.release_fp && <> · {t("agu_key")}: <span className="mono">{i.release_fp}</span></>}</div>}
  </div>;
}
