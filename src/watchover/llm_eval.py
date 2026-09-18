"""How good is the model? Two objective measures that need no labelled data:

* **grounding** -- every citation in an answer ([INC-3], [L12], [file:line]) must exist in the context that was sent.
  cited / grounded / invalid counts are stored per call; invalid citations are the closest thing to a hallucination meter.
* **root-cause benchmark** -- for every incident of the loaded dataset the model gets the evidence and the candidate signals
  (engine root cause + counter-hypotheses, shuffled deterministically) and must pick the root cause. Accuracy against the
  deterministic engine is reported per run; the engine is the reference, so this measures agreement, not truth.
"""
from __future__ import annotations

import hashlib
import re
import time

from .llm import LLMConfig, chat_messages

CITE_RE = re.compile(r"\[(INC-\d+|L\d+|R\d+|S\d+|[\w./\-]+:\d+)(?: alt)?\]", re.I)


def grounding(answer: str, context: str) -> dict:
    """Which citations in the answer exist in the context."""
    cited = [m[1] for m in CITE_RE.finditer(answer or "")]
    ctx = (context or "").upper()
    ok = [c for c in cited if f"[{c.upper()}" in ctx]
    return {"citations": len(cited), "grounded": len(ok), "invalid": len(cited) - len(ok), "invalid_list": sorted({c for c in cited if c not in ok})}


PROMPT = {
    "tr": "Aşağıda bir incident'ın kanıt satırları ve aday sinyaller var. Kök neden hangi sinyaldir? Yalnızca sinyal kimliğini yaz (ör. S7), başka bir şey yazma.",
    "en": "Below are an incident's evidence lines and candidate signals. Which signal is the root cause? Reply with the signal id only (e.g. S7), nothing else.",
}


def _cases(analysis) -> list[dict]:
    cases = []
    for inc in analysis.incidents:
        root = analysis.signal_by_id[inc.root_cause_signal]
        cands = [root] + [analysis.signal_by_id[x["signal"]] for x in inc.root_cause_alternatives[:3] if x.get("signal") in analysis.signal_by_id]
        if len(cands) < 2:
            cands += [analysis.signal_by_id[s] for s in inc.signal_ids if s != root.id][:2]
        seen, uniq = set(), []
        for c in cands:
            if c.id not in seen:
                seen.add(c.id); uniq.append(c)
        if len(uniq) < 2:
            continue
        key = hashlib.sha1(f"{inc.id}{root.template}".encode()).hexdigest()
        uniq.sort(key=lambda c: hashlib.sha1((key + c.id).encode()).hexdigest())      # deterministic shuffle, no positional bias
        cases.append({"incident": inc, "root": root, "candidates": uniq})
    return cases


def run_benchmark(cfg: LLMConfig, analysis, lang: str = "tr", max_cases: int = 15) -> dict:
    cases = _cases(analysis)[:max_cases]
    detail, correct, cited, grounded, t_total = [], 0, 0, 0, 0
    for c in cases:
        inc, root = c["incident"], c["root"]
        lines = [f"[{inc.id}] {inc.title[:100]} · {inc.started_at:%H:%M}-{inc.ended_at:%H:%M} · services {', '.join(inc.affected_services[:6])}", "EVIDENCE:"]
        for s in c["candidates"]:
            for o in s.observations[:3]:
                lines.append(f"[{o.ref}] {o.timestamp:%H:%M:%S} {o.severity} {o.service} {o.host} {o.message[:140]}")
        lines.append("CANDIDATES:")
        for s in c["candidates"]:
            lines.append(f"[{s.id}] {s.template[:100]} · services {', '.join(s.services)} · first {s.first_seen:%H:%M:%S} · {s.count} events · {s.severity}")
        ctx = "\n".join(lines)
        t0 = time.perf_counter()
        try:
            raw = chat_messages(cfg, [{"role": "system", "content": "You are a senior SRE. Answer with the id only."},
                                      {"role": "user", "content": PROMPT.get(lang, PROMPT["en"]) + "\n\n" + ctx}], temperature=0.0, max_tokens=20, kind="eval")
            err = ""
        except Exception as e:  # noqa: BLE001
            raw, err = "", str(e)[:200]
        ms = round((time.perf_counter() - t0) * 1000); t_total += ms
        m = re.search(r"\bS\d+\b", raw or "", re.I)
        pick = m[0].upper() if m else ""
        g = grounding(raw, ctx)
        ok = pick == root.id
        correct += int(ok); cited += int(g["citations"] > 0); grounded += int(g["citations"] > 0 and g["invalid"] == 0)
        detail.append({"incident": inc.id, "expected": root.id, "picked": pick, "ok": ok, "ms": ms, "error": err, "candidates": [s.id for s in c["candidates"]]})
    return {"n": len(cases), "correct": correct, "cited": cited, "grounded": grounded, "latency_ms": t_total // max(1, len(cases)), "detail": detail}
