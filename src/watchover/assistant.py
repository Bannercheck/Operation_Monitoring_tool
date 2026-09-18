"""Ask Watchover: a chat assistant grounded in the current analysis and the knowledge base.

The engine decides; the assistant explains, recalls and proposes. Every answer is built from a context the user can
inspect (incidents of the loaded dataset, retrieved lessons, approved rules) and must cite it: [INC-3], [L12],
[file:line]. Without an LLM endpoint the assistant still answers from the same context with a deterministic composer,
so the chat never goes dark.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .llm import LLMConfig, chat_messages

INC_RE = re.compile(r"\bINC-\d+\b", re.I)
SYSTEM = {
    "tr": ("Sen Watchover'sın: bir operasyon merkezinin SRE asistanı. Yalnızca verilen BAĞLAM'a dayanarak Türkçe cevap ver. "
           "Her iddiada kaynağını köşeli parantezle göster: incident için [INC-3], bilgi tabanı dersi için [L12], kanıt satırı için [dosya:satır]. "
           "Bağlamda olmayan bir şeyi bilmiyorsan 'bağlamda yok' de; uydurma. Kısa, net, madde madde; ilk aksiyonu ve sahibini söyle."),
    "en": ("You are Watchover, the SRE assistant of an operations centre. Answer in English using ONLY the CONTEXT. "
           "Cite every claim in brackets: [INC-3] for incidents, [L12] for knowledge-base lessons, [file:line] for evidence. "
           "If the context does not contain it, say so; never invent. Be short and concrete; name the first action and its owner."),
}


def _inc_line(a, inc, lang: str) -> str:
    from .analysis import suggested_owner
    from .i18n import reason_text
    root = a.signal_by_id[inc.root_cause_signal]
    return (f"[{inc.id}] {inc.title[:110]} · {inc.severity} · score {inc.score} · {inc.started_at:%Y-%m-%d %H:%M}–{inc.ended_at:%H:%M} · "
            f"services: {', '.join(inc.affected_services[:8])} · root cause: {root.template[:90]} ({', '.join(root.services)}) — {reason_text(inc.root_cause_codes, lang)} · "
            f"recovery: {inc.recovery.get('kind', '?')} · first action: {inc.recommendations[0] if inc.recommendations else '-'} · owner: {suggested_owner(inc, root)}")


def build_context(question: str, analysis, kb, lang: str = "tr", k: int = 6) -> tuple[str, list[dict]]:
    """Text block for the model + the source cards shown to the user."""
    parts, sources = [], []
    if analysis is not None:
        parts.append(f"## DATASET · {len(analysis.observations)} events · {len(analysis.incidents)} incidents · mode {analysis.mode}")
        for inc in analysis.incidents[:15]:
            parts.append(_inc_line(analysis, inc, lang))
        wanted = {m.upper() for m in INC_RE.findall(question)}
        for inc in analysis.incidents:
            if inc.id.upper() in wanted:
                root = analysis.signal_by_id[inc.root_cause_signal]
                parts.append(f"### {inc.id} evidence")
                for o in root.observations[:5]:
                    parts.append(f"[{o.ref}] {o.timestamp:%H:%M:%S} {o.severity} {o.service} {o.host} {o.message[:160]}")
                for x in inc.root_cause_alternatives[:3]:
                    parts.append(f"[{inc.id} alt] {x['template'][:90]} ({', '.join(x['services'])}) score {x['score']}")
                sources.append({"kind": "incident", "id": inc.id, "title": inc.title, "text": _inc_line(analysis, inc, lang)})
    if kb is not None:
        lessons = kb.search(question, k=k)
        if lessons:
            parts.append("## KNOWLEDGE BASE")
            for d in lessons:
                refs = ", ".join(f"{r.get('dataset')}/{r.get('incident')}" for r in d.get("refs", [])[:4])
                parts.append(f"[L{d['id']}] ({d['kind']}, ×{d.get('occurrences', 1)}) {d['title']}\n{d['text'][:700]}" + (f"\nseen in: {refs}" if refs else ""))
                sources.append({"kind": d["kind"], "id": f"L{d['id']}", "title": d["title"], "text": d["text"][:500], "score": d.get("score")})
        rules = kb.rules("approved")
        if rules:
            parts.append("## APPROVED RULES")
            parts += [f"[R{r['id']}] {r['kind']} {r['key']} = {r['value']} ({r['reason'][:80]})" for r in rules[:20]]
    return "\n".join(parts), sources


def fallback_answer(question: str, analysis, kb, lang: str = "tr") -> str:
    """No LLM: compose an answer from the same context (incidents that mention the terms, top lessons, rules)."""
    q = question.lower()
    terms = [w for w in re.findall(r"[a-zçğıöşü0-9][\w\-]{2,}", q) if w not in ("için", "nedir", "neden", "hangi", "what", "which", "the", "and", "olan")]
    lines = []
    if analysis is not None and analysis.incidents:
        wanted = {m.upper() for m in INC_RE.findall(question)}
        hits = [i for i in analysis.incidents if i.id.upper() in wanted] or \
               [i for i in analysis.incidents if any(w in (i.title + " " + " ".join(i.affected_services)).lower() for w in terms)]
        if not hits:                                   # a general question ("what happened tonight?"): the cards themselves are the answer
            hits = analysis.incidents[:5]
        for inc in hits[:5]:
            lines.append("- " + _inc_line(analysis, inc, lang))
    if kb is not None:
        for d in kb.search(question, k=4):
            lines.append(f"- [L{d['id']}] {d['title']} — {d['text'][:240].replace(chr(10), ' ')}")
    if not lines:
        return ("Bağlamda buna dair bir şey bulamadım. Bir veri seti yükleyin, bilgi tabanına not ekleyin ya da soruda bir incident numarası "
                "(INC-1) / servis adı geçirin." if lang == "tr" else
                "Nothing in the context matches. Load a dataset, add a note to the knowledge base, or mention an incident id (INC-1) or a service name.")
    head = "LLM bağlı değil; bağlamdan doğrudan derlendi:" if lang == "tr" else "No LLM connected; composed directly from the context:"
    return head + "\n\n" + "\n".join(lines)


def answer(cfg: LLMConfig | None, question: str, history: list[dict], analysis, kb, lang: str = "tr") -> dict:
    """-> {"text", "sources", "context", "used_llm", "error"}"""
    ctx, sources = build_context(question, analysis, kb, lang)
    if cfg is not None and cfg.enabled:
        msgs = [{"role": "system", "content": SYSTEM.get(lang, SYSTEM["en"]) + f"\n\nCURRENT TIME: {datetime.now():%Y-%m-%d %H:%M}\n\nCONTEXT:\n{ctx[:14000]}"}]
        for h in history[-6:]:
            msgs.append({"role": h["role"], "content": h["content"][:2000]})
        msgs.append({"role": "user", "content": question})
        try:
            return {"text": chat_messages(cfg, msgs), "sources": sources, "context": ctx, "used_llm": True, "error": ""}
        except Exception as e:  # noqa: BLE001 - fall back, never break the chat
            return {"text": fallback_answer(question, analysis, kb, lang), "sources": sources, "context": ctx, "used_llm": False, "error": str(e)}
    return {"text": fallback_answer(question, analysis, kb, lang), "sources": sources, "context": ctx, "used_llm": False, "error": ""}


# ---------------------------------------------------------------- rule proposals by the model (a human still approves)
RULE_PROMPT = {
    "tr": ("Aşağıdaki BAĞLAM'a bakarak Watchover'ın deterministik motoru için kural önerileri üret. Yalnız JSON dizisi döndür, başka hiçbir şey yazma. "
           "Her öğe: {\"kind\": ..., \"key\": ..., \"value\": ..., \"reason\": ...}. İzin verilen kind değerleri ve anlamları:\n"
           "- owner: key = servis adı ya da kök kelime (billing, payment), value = sorumlu ekip\n"
           "- cause_rank: key = alarm tipi (BAĞLAM'daki alarm tiplerinden), value = 0-6 arası sayı; yüksek = daha çok kök neden, 0 = belirti\n"
           "- noise_type: key = alarm tipi, value = \"1\"; bu tip hiçbir zaman incident olmamalı\n"
           "- dependency: key = \"kaynak->hedef\" (BAĞLAM'daki servisler), value = senkron|asenkron\n"
           "- recommendation: key = kök neden kelimesi (disk, tablespace, timeout), value = tek cümlelik ilk aksiyon\n"
           "Sadece bağlamdaki kanıtla desteklenen, ekip kararlarıyla (👎 / doğru kök neden) tutarlı öneriler ver; en fazla 8 öneri; reason alanında kanıtı kısaca yaz."),
    "en": ("From the CONTEXT below, propose rules for Watchover's deterministic engine. Return ONLY a JSON array, nothing else. "
           "Each item: {\"kind\": ..., \"key\": ..., \"value\": ..., \"reason\": ...}. Allowed kinds:\n"
           "- owner: key = service name or stem (billing, payment), value = owning team\n"
           "- cause_rank: key = alarm type (from the CONTEXT), value = number 0-6; high = more of a cause, 0 = symptom\n"
           "- noise_type: key = alarm type, value = \"1\"; this type must never become an incident\n"
           "- dependency: key = \"source->target\" (services from the CONTEXT), value = sync|async\n"
           "- recommendation: key = root-cause word (disk, tablespace, timeout), value = a one-sentence first action\n"
           "Only rules backed by evidence in the context and consistent with the team's verdicts (👎 / correct root cause); at most 8; put the evidence in reason."),
}


def rule_context(analysis, kb, lang: str = "tr") -> str:
    parts = []
    if analysis is not None:
        types = sorted({str(o.attributes.get("alarm_type", "")).lower() for o in analysis.observations if o.attributes.get("alarm_type")})
        services = sorted({o.service for o in analysis.observations if o.service})
        parts.append("## ALARM TYPES\n" + ", ".join(types[:80]) if types else "## ALARM TYPES\n(none: log data)")
        parts.append("## SERVICES\n" + ", ".join(services[:80]))
        parts.append("## INCIDENTS")
        for inc in analysis.incidents[:15]:
            parts.append(_inc_line(analysis, inc, lang))
            for x in inc.root_cause_alternatives[:3]:
                parts.append(f"   alt: {x['template'][:80]} ({', '.join(x['services'])}) score {x['score']}")
        if analysis.demoted:
            parts.append("## SMALL GROUPS NOT ON CARDS\n" + "; ".join(f"{d.title[:60]} ({d.severity})" for d in analysis.demoted[:10]))
    if kb is not None:
        fb = kb.all("feedback", limit=30)
        if fb:
            parts.append("## TEAM VERDICTS\n" + "\n".join(f"[L{d['id']}] {d['text'][:300]}" for d in fb))
        notes = kb.all("note", limit=15) + kb.all("doc", limit=10)
        if notes:
            parts.append("## NOTES\n" + "\n".join(f"[L{d['id']}] {d['title'][:60]}: {d['text'][:240]}" for d in notes))
        cur = kb.rules("approved") + kb.rules("proposed")
        if cur:
            parts.append("## EXISTING RULES (do not repeat)\n" + "\n".join(f"{r['kind']} {r['key']} = {r['value']} [{r['status']}]" for r in cur[:40]))
    return "\n".join(parts)


def _extract_json(text: str) -> list:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m[0])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def validate_rules(items: list, analysis) -> tuple[list[dict], list[str]]:
    """Keep only well-formed proposals whose keys exist in the data; report why the rest were dropped."""
    from .knowledge import RULE_KINDS
    types = {str(o.attributes.get("alarm_type", "")).lower() for o in analysis.observations if o.attributes.get("alarm_type")} if analysis is not None else set()
    services = {o.service.lower() for o in analysis.observations if o.service} if analysis is not None else set()
    ok, dropped = [], []
    for it in items:
        if not isinstance(it, dict):
            dropped.append("not an object"); continue
        kind, key, val, reason = str(it.get("kind", "")).strip().lower(), str(it.get("key", "")).strip(), str(it.get("value", "")).strip(), str(it.get("reason", "")).strip()
        if kind not in RULE_KINDS or kind == "noise_template" or not key:
            dropped.append(f"{kind}:{key} (kind)"); continue
        if kind in ("cause_rank", "noise_type"):
            key = key.lower()
            if types and key not in types:
                dropped.append(f"{kind}:{key} (unknown alarm type)"); continue
            if kind == "cause_rank":
                try:
                    v = float(val); assert 0 <= v <= 6
                except (ValueError, AssertionError):
                    dropped.append(f"{kind}:{key} (value {val})"); continue
                val = str(v)
            else:
                val = "1"
        elif kind == "dependency":
            src, _, tgt = key.replace(" ", "").partition("->")
            if not src or not tgt or (services and (src.lower() not in services or tgt.lower() not in services)):
                dropped.append(f"{kind}:{key} (unknown service)"); continue
            key = f"{src}->{tgt}"; val = "asenkron" if val.lower().startswith("as") else "senkron"
        elif not val:
            dropped.append(f"{kind}:{key} (empty value)"); continue
        ok.append({"kind": kind, "key": key[:200], "value": val[:300], "reason": reason[:300]})
    return ok, dropped


def propose_rules(cfg: LLMConfig, analysis, kb, lang: str = "tr") -> dict:
    """Ask the model for rule proposals, validate them against the data, store them as *proposed* (never applied by itself)."""
    ctx = rule_context(analysis, kb, lang)
    msgs = [{"role": "system", "content": SYSTEM.get(lang, SYSTEM["en"])}, {"role": "user", "content": RULE_PROMPT.get(lang, RULE_PROMPT["en"]) + "\n\nCONTEXT:\n" + ctx[:14000]}]
    raw = chat_messages(cfg, msgs, temperature=0.1, max_tokens=1200)
    items, dropped = validate_rules(_extract_json(raw), analysis)
    ids = [kb.propose(it["kind"], it["key"], it["value"], it["reason"] or "llm", f"llm:{cfg.model}") for it in items]
    return {"proposed": ids, "items": items, "dropped": dropped, "raw": raw}
