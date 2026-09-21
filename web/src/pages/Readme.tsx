import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useT } from "../i18n";
import { Wordmark } from "../components/Logo";

/** Small Markdown renderer for the README: headings, lists, tables, code, bold, links. Enough for docs, not a full parser. */
function render(md: string): string {
  const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s: string) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  const out: string[] = []; const lines = md.split("\n"); let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (l.startsWith("```")) { const buf: string[] = []; i++; while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]); i++; out.push(`<pre class="code">${esc(buf.join("\n"))}</pre>`); continue; }
    const h = /^(#{1,4})\s+(.*)/.exec(l); if (h) { out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); i++; continue; }
    if (l.startsWith("|")) { const rows: string[][] = []; while (i < lines.length && lines[i].startsWith("|")) { const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim()); if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells); i++; }
      out.push(`<div class="table-wrap"><table class="table"><thead><tr>${rows[0].map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`); continue; }
    if (/^\s*[-*]\s+/.test(l)) { const items: string[] = []; while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*]\s+/, "")); out.push(`<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`); continue; }
    if (/^\s*\d+\.\s+/.test(l)) { const items: string[] = []; while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+\.\s+/, "")); out.push(`<ol>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ol>`); continue; }
    if (l.trim() === "" || l.trim().startsWith("<")) { i++; continue; } // raw HTML (logo <p><img>) is skipped
    const buf: string[] = []; while (i < lines.length && lines[i].trim() !== "" && !/^(#|\||```|<|\s*[-*]\s|\s*\d+\.\s)/.test(lines[i])) buf.push(lines[i++]); out.push(`<p>${inline(buf.join(" "))}</p>`);
  }
  return out.join("\n");
}

export default function Readme() {
  const { t } = useT();
  const q = useQuery({ queryKey: ["readme"], queryFn: () => api<string>("/system/readme", { text: true }) });
  return <div className="stack"><div className="row between"><h2>📖 {t("nav_readme")}</h2><Wordmark size={44} /></div><div className="card readme" dangerouslySetInnerHTML={{ __html: render(q.data ?? "") }} /></div>;
}
