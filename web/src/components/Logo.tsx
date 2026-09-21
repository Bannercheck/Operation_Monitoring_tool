/** The Watchover mark (assets/logo.svg) as a React component: eye + signal arcs on a dark tile, teal→blue gradient.
 *  `Wordmark` adds the gradient name and the tagline next to it (sidebar, login, README page). */
import { useId } from "react";
import { useT } from "../i18n";

export function Logo({ size = 40, className = "" }: { size?: number; className?: string }) {
  const id = useId().replace(/:/g, "");
  return (
    <svg className={`wo-logo ${className}`} viewBox="0 0 64 64" width={size} height={size} role="img" aria-label="Watchover">
      <defs>
        <linearGradient id={`g${id}`} x1="0" y1="0" x2="1" y2="1"><stop offset="0" stopColor="#2dd4bf" /><stop offset="1" stopColor="#60a5fa" /></linearGradient>
        <radialGradient id={`r${id}`} cx=".5" cy=".55" r=".5"><stop offset="0" stopColor="#2dd4bf" stopOpacity=".38" /><stop offset="1" stopColor="#2dd4bf" stopOpacity="0" /></radialGradient>
      </defs>
      <rect x="2" y="2" width="60" height="60" rx="16" fill="#0e1420" stroke={`url(#g${id})`} strokeWidth="2" />
      <circle cx="32" cy="35" r="19" fill={`url(#r${id})`} />
      <path d="M9 35 Q32 13 55 35 Q32 57 9 35 Z" fill="none" stroke={`url(#g${id})`} strokeWidth="3" strokeLinejoin="round" />
      <circle cx="32" cy="35" r="9.5" fill="none" stroke={`url(#g${id})`} strokeWidth="2.4" opacity=".55" />
      <path d="M32 25.5 a9.5 9.5 0 0 1 9.5 9.5" fill="none" stroke="#e6fffa" strokeWidth="2.6" strokeLinecap="round" />
      <path d="M32 35 L38.8 28.2" stroke="#e6fffa" strokeWidth="2.2" strokeLinecap="round" opacity=".9" />
      <circle cx="32" cy="35" r="3.4" fill={`url(#g${id})`} />
      <path d="M45 13.5 a10 10 0 0 1 6.5 6.5" fill="none" stroke={`url(#g${id})`} strokeWidth="2.2" strokeLinecap="round" opacity=".85" />
      <path d="M48.5 8.5 a15.5 15.5 0 0 1 8 8" fill="none" stroke={`url(#g${id})`} strokeWidth="2.2" strokeLinecap="round" opacity=".45" />
    </svg>
  );
}

export function Wordmark({ size = 40, tagline = true }: { size?: number; tagline?: boolean }) {
  const { t } = useT();
  return (
    <div className="wordmark" style={{ ["--wm" as any]: `${size}px` }}>
      <Logo size={size} />
      <div><div className="wm-name">Watchover</div>{tagline && <div className="wm-tag">{t("brand_tag")}</div>}</div>
    </div>
  );
}
