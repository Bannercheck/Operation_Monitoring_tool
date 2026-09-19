"""Live learning: every N minutes the deterministic engine analyses the recent live window and records the resulting
incident patterns into the knowledge base (one lesson per root cause, repeats bump occurrences). This is what grounds
"Ask Watchover" and the rule proposals; the language model itself is not retrained. Also exports a fine-tuning set
(chat-format JSONL) built from lessons, approved rules and thumbs-up answers for teams that do train their own model."""
from __future__ import annotations

import json
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
        kb._exec("CREATE TABLE IF NOT EXISTS learn_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, events INTEGER, incidents INTEGER, lessons INTEGER, note TEXT DEFAULT '')"
                 if not kb.pg else "CREATE TABLE IF NOT EXISTS learn_runs (id SERIAL PRIMARY KEY, ts TEXT, events INTEGER, incidents INTEGER, lessons INTEGER, note TEXT DEFAULT '')")

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


def training_export(kb, lang: str = "tr") -> bytes:
    """Chat-format JSONL (system / user / assistant) from pattern lessons, notes, approved rules and thumbs-up answers."""
    sys_tr = "Sen Watchover operasyon asistanısın. Kanıta dayalı, kısa ve gerekçeli yanıt verirsin."
    sys_en = "You are the Watchover operations assistant. Answer briefly, with evidence and reasons."
    system = sys_tr if lang == "tr" else sys_en
    q_pat = "Bu hata kalıbı ne anlama geliyor, kök nedeni ve ilk aksiyonu nedir? {t}" if lang == "tr" else "What does this error pattern mean, what is the root cause and the first action? {t}"
    q_note = "{t} hakkında ne biliyoruz?" if lang == "tr" else "What do we know about {t}?"
    q_rule = "{k} için geçerli kural nedir?" if lang == "tr" else "What is the rule for {k}?"
    lines = []
    for r in kb._exec("SELECT kind, title, text FROM lessons WHERE kind IN ('pattern', 'note', 'doc', 'chat') ORDER BY id"):
        if r["kind"] == "chat" and r["text"].startswith("Q: ") and "\nA: " in r["text"]:
            q, a = r["text"][3:].split("\nA: ", 1)
        else:
            q, a = (q_pat if r["kind"] == "pattern" else q_note).format(t=r["title"]), r["text"]
        lines.append({"messages": [{"role": "system", "content": system}, {"role": "user", "content": q.strip()}, {"role": "assistant", "content": a.strip()}]})
    for r in kb._exec("SELECT kind, key, value, reason FROM rules WHERE status='approved' ORDER BY id"):
        a = f"{r['kind']}: {r['key']} → {r['value']}" + (f" ({r['reason']})" if r["reason"] else "")
        lines.append({"messages": [{"role": "system", "content": system}, {"role": "user", "content": q_rule.format(k=r["key"])}, {"role": "assistant", "content": a}]})
    cols = [c["name"] for c in kb._exec("PRAGMA table_info(answer_feedback)")] if not kb.pg else ["answer"]
    if "answer" in cols:
        for r in kb._exec("SELECT question, answer FROM answer_feedback WHERE verdict='up' AND answer <> '' ORDER BY id"):
            lines.append({"messages": [{"role": "system", "content": system}, {"role": "user", "content": r["question"]}, {"role": "assistant", "content": r["answer"]}]})
    return ("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + ("\n" if lines else "")).encode()
