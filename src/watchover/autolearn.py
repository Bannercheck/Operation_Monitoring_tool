"""Live learning: every N minutes the deterministic engine analyses the recent live window and records the resulting
incident patterns into the knowledge base (one lesson per root cause, repeats bump occurrences). This is what grounds
"Ask Watchover" and the rule proposals; the language model itself is not retrained. Also exports a fine-tuning set
(chat-format JSONL) built from lessons, approved rules and thumbs-up answers for teams that do train their own model."""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class LiveLearner:
    def __init__(self, ls, kb, interval_min: int = 15, window_min: int = 60, min_events: int = 50, lang: str = "tr", inventory_fn=None):
        self.ls, self.kb, self.interval_min, self.window_min, self.min_events, self.lang = ls, kb, interval_min, window_min, min_events, lang
        self.inventory_fn = inventory_fn
        self.stop = threading.Event()
        self.last: dict = {}
        self.runs = 0
        kb._exec(f"CREATE TABLE IF NOT EXISTS learn_runs (id {kb.pk}, ts TEXT, events INTEGER, incidents INTEGER, lessons INTEGER, note TEXT DEFAULT '')")

    def learn_once(self, env: str | None = None) -> dict:
        """Analyse the last window of live events and record patterns; returns what happened."""
        from .analysis import Analysis
        start = datetime.now(UTC) - timedelta(minutes=self.window_min)
        obs = [o for o in self.ls.snapshot(env) if o.timestamp >= start]
        out = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "events": len(obs), "incidents": 0, "lessons": 0, "note": ""}
        if len(obs) < self.min_events:
            out["note"] = "too few events"
        else:
            try:
                a = Analysis(obs, [], self.inventory_fn() if self.inventory_fn else None)
                out["incidents"] = len(a.incidents)
                out["lessons"] = self.kb.record(a, f"live:{out['ts'][:16]}", self.lang)
            except Exception as e:  # noqa: BLE001
                out["note"] = f"{type(e).__name__}: {str(e)[:160]}"
        self.kb._exec("INSERT INTO learn_runs (ts, events, incidents, lessons, note) VALUES (?,?,?,?,?)", (out["ts"], out["events"], out["incidents"], out["lessons"], out["note"]))
        self.last = out; self.runs += 1
        return out

    def history(self, n: int = 10) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM learn_runs ORDER BY id DESC LIMIT ?", (n,))]

    def start(self) -> "LiveLearner":
        def loop():
            while not self.stop.is_set():
                if self.stop.wait(max(self.interval_min, 1) * 60):
                    break
                if self.interval_min <= 0:
                    continue
                try:
                    self.learn_once()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True, name="live-learner").start()
        return self


def training_examples(kb, lang: str = "tr") -> tuple[list[dict], dict]:
    """Chat-format examples (system / user / assistant) from everything the company taught Watchover: root-cause patterns,
    anomalies, resolutions (closed actions), notes, runbook chunks, saved chats, approved rules and thumbs-up answers.
    Deduplicated on (question, answer); returns (examples, counts-by-source)."""
    sys_tr = "Sen Watchover operasyon asistanısın. Kanıta dayalı, kısa ve gerekçeli yanıt verirsin."
    sys_en = "You are the Watchover operations assistant. Answer briefly, with evidence and reasons."
    system = sys_tr if lang == "tr" else sys_en
    q = {
        "pattern": "Bu hata kalıbı ne anlama geliyor, kök nedeni ve ilk aksiyonu nedir? {t}" if lang == "tr" else "What does this error pattern mean, what is the root cause and the first action? {t}",
        "anomaly": "Bu anomali ne anlama geliyor ve nasıl ele alınmalı? {t}" if lang == "tr" else "What does this anomaly mean and how should it be handled? {t}",
        "resolution": "{t} için hangi aksiyon alındı ve sonucu ne oldu?" if lang == "tr" else "What action was taken for {t} and what was the outcome?",
        "note": "{t} hakkında ne biliyoruz?" if lang == "tr" else "What do we know about {t}?",
        "doc": "{t} hakkında ne biliyoruz?" if lang == "tr" else "What do we know about {t}?",
        "rule": "{k} için geçerli kural nedir?" if lang == "tr" else "What is the rule for {k}?",
    }
    out, seen, counts = [], set(), {}

    def add(src: str, question: str, answer: str) -> None:
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            return
        h = hashlib.sha1(f"{question}\n{answer}".encode()).hexdigest()
        if h in seen:
            return
        seen.add(h); counts[src] = counts.get(src, 0) + 1
        out.append({"messages": [{"role": "system", "content": system}, {"role": "user", "content": question}, {"role": "assistant", "content": answer}]})

    for r in kb._exec("SELECT kind, title, text FROM lessons WHERE kind IN ('pattern', 'anomaly', 'resolution', 'note', 'doc', 'chat') ORDER BY id"):
        if r["kind"] == "chat":
            if r["text"].startswith("Q: ") and "\nA: " in r["text"]:
                qq, aa = r["text"][3:].split("\nA: ", 1)
                add("chat", qq, aa)
            continue
        add(r["kind"], q[r["kind"]].format(t=r["title"]), r["text"])
    for r in kb._exec("SELECT kind, key, value, reason FROM rules WHERE status='approved' ORDER BY id"):
        add("rule", q["rule"].format(k=r["key"]), f"{r['kind']}: {r['key']} → {r['value']}" + (f" ({r['reason']})" if r["reason"] else ""))
    if "answer" in kb.columns("answer_feedback"):
        for r in kb._exec("SELECT question, answer FROM answer_feedback WHERE verdict='up' AND answer <> '' ORDER BY id"):
            add("feedback", r["question"], r["answer"])
    return out, counts


def training_bundle(kb, lang: str = "tr", holdout: float = 0.1, seed: int = 7) -> dict:
    """Train / eval split of the examples (deterministic shuffle) plus counts; the eval part is the held-out quality set."""
    ex, counts = training_examples(kb, lang)
    order = list(range(len(ex)))
    random.Random(seed).shuffle(order)
    n_eval = int(len(ex) * holdout) if len(ex) >= 10 else 0
    ev = [ex[i] for i in order[:n_eval]]
    tr = [ex[i] for i in order[n_eval:]]
    dump = lambda rows: ("\n".join(json.dumps(x, ensure_ascii=False) for x in rows) + ("\n" if rows else ""))
    return {"train": dump(tr), "eval": dump(ev), "stats": {"total": len(ex), "train": len(tr), "eval": len(ev), "by_source": counts, "lang": lang}}


def training_export(kb, lang: str = "tr") -> bytes:
    """Every example as chat-format JSONL (no split): what the Training page / CLI download by default."""
    ex, _ = training_examples(kb, lang)
    return ("\n".join(json.dumps(x, ensure_ascii=False) for x in ex) + ("\n" if ex else "")).encode()
