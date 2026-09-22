"""Unattended learning: the trainer service turns the company's memory into `watchover-ops` without anyone lifting a finger.

Loop (trainer container, `python -m watchover.autotrain --loop`):
  * every minute: read the switches from the shared settings (LLM › Quality › "Your own model"), count the examples the
    memory yields today and how many arrived since the last model that passed the gate;
  * when `train_auto` is on, examples >= `train_min_examples`, new >= `train_min_new` and `train_every_h` hours passed
    since the last run (or the operator pressed "train now"), run once:
      export -> LoRA on CPU (watchover.lora) -> candidate model in the bundled Ollama (blob API, Modelfile fallback)
      -> quality gate on the deterministic root-cause benchmark (live feed when it has incidents, else the demo set)
      -> PASS: the candidate becomes `watchover-ops` (+ a dated copy for rollback); FAIL: candidate deleted, previous kept.
  * every run is a row in `train_runs`; the dashboard shows them and only uses `watchover-ops` after a PASS.
The operator's own chat LLM connection is never touched: `watchover-ops` is an extra model used for incident reviews and
"Ask Watchover" when "use it" is on.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import autolearn, lora, ollama as wo_ollama, settings as wo_settings
from .training import SYSTEM

UTC = timezone.utc
CANDIDATE = "watchover-ops-candidate"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def flag_path() -> Path:
    return Path(wo_settings.home()) / "train.now"


def request_run() -> str:
    p = flag_path(); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(_now(), encoding="utf-8")
    return p.as_posix()


def heartbeat_path() -> Path:
    return Path(wo_settings.home()) / "trainer.alive"


def trainer_alive(max_age_s: int = 180) -> bool:
    try:
        return time.time() - heartbeat_path().stat().st_mtime < max_age_s
    except OSError:
        return False


class AutoTrainer:
    def __init__(self, kb, ollama_url: str | None = None, models_dir: str | None = None, lang: str = "tr", log=print):
        self.kb, self.lang, self.log = kb, lang, log
        self.url = ollama_url or os.environ.get("OLLAMA_URL") or "http://ollama:11434"
        self.models_dir = Path(models_dir or os.environ.get("WATCHOVER_MODELS_DIR") or "models")
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS train_runs (id {pk}, ts TEXT, status TEXT, examples INTEGER DEFAULT 0, new_examples INTEGER DEFAULT 0,
            base TEXT DEFAULT '', loss REAL DEFAULT 0, seconds INTEGER DEFAULT 0, gate TEXT DEFAULT '{{}}', model TEXT DEFAULT '', note TEXT DEFAULT '')""")

    # ---------------------------------------------------------------- state
    def settings(self) -> dict:
        c = wo_settings.load()
        return {k: c.get(k) for k in ("train_auto", "train_every_h", "train_min_examples", "train_min_new", "train_base", "train_gate_min", "train_use_ops")}

    def runs(self, n: int = 20) -> list[dict]:
        out = []
        for r in self.kb._exec("SELECT * FROM train_runs ORDER BY id DESC LIMIT ?", (n,)):
            d = dict(r)
            try:
                d["gate"] = json.loads(d.get("gate") or "{}")
            except ValueError:
                d["gate"] = {}
            out.append(d)
        return out

    def last(self, status: str | None = None) -> dict | None:
        rows = self.kb._exec("SELECT * FROM train_runs" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT 1", (status,) if status else ())
        return dict(rows[0]) if rows else None

    def state(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        s = self.settings()
        _, counts = autolearn.training_examples(self.kb, self.lang)
        total = sum(counts.values())
        last_pass = self.last("pass"); last_any = self.last()
        new = total - int(last_pass["examples"]) if last_pass else total
        elapsed_ok = True
        if last_any and last_any.get("ts"):
            try:
                last_dt = datetime.fromisoformat(str(last_any["ts"]))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                elapsed_ok = now - last_dt >= timedelta(hours=float(s["train_every_h"] or 24))
            except ValueError:
                elapsed_ok = True
        reasons = []
        if not s["train_auto"]:
            reasons.append("auto off")
        if total < int(s["train_min_examples"] or 0):
            reasons.append(f"examples {total} < {s['train_min_examples']}")
        if new < int(s["train_min_new"] or 0):
            reasons.append(f"new {new} < {s['train_min_new']}")
        if not elapsed_ok:
            reasons.append(f"waiting {s['train_every_h']} h since the last run")
        forced = flag_path().exists()
        return {"examples": total, "by_source": counts, "new_since_pass": new, "eligible": (not reasons) or forced, "forced": forced, "reasons": reasons,
                "last_run": last_any, "last_pass": last_pass, "runs": self.runs(10)}

    def should_run(self, now: datetime | None = None) -> bool:
        st = self.state(now)
        return bool(st["eligible"] and (st["examples"] > 0))

    # ---------------------------------------------------------------- one run
    def _record(self, **f) -> int:
        f.setdefault("ts", _now()); f["gate"] = json.dumps(f.get("gate") or {}, ensure_ascii=False)
        cols = ("ts", "status", "examples", "new_examples", "base", "loss", "seconds", "gate", "model", "note")
        return self.kb._insert(f"INSERT INTO train_runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(f.get(c, 0 if c in ('examples', 'new_examples', 'loss', 'seconds') else '') for c in cols))

    def _gate_analysis(self):
        from .analysis import Analysis
        from .pipeline import ingest_path
        spool = os.environ.get("LIVE_SPOOL") or str(Path(wo_settings.home()) / "live" / "events.jsonl")
        for src in (spool, str(Path(__file__).resolve().parents[2] / "samples" / "demo_mixed.zip")):
            try:
                if not Path(src).exists() or Path(src).stat().st_size == 0:
                    continue
                obs, rep = ingest_path(src)
                a = Analysis(obs, rep)
                if len(a.incidents) >= 3:
                    return a, src
            except Exception as e:  # noqa: BLE001
                self.log(f"gate dataset {src} skipped: {e}")
        return None, ""

    def run_once(self, force: bool = False, dry: bool = False) -> dict:
        st = self.state()
        if not (force or st["eligible"]):
            return {"status": "skipped", "reasons": st["reasons"]}
        s = self.settings(); base = s["train_base"] or "qwen2.5:1.5b-instruct"
        try:
            flag_path().unlink()
        except OSError:
            pass
        b = autolearn.training_bundle(self.kb, self.lang)
        if b["stats"]["total"] == 0:
            return {"status": "skipped", "reasons": ["no examples"]}
        run_dir = self.models_dir / datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "train.jsonl").write_text(b["train"] or autolearn.training_export(self.kb, self.lang).decode(), encoding="utf-8")
        (run_dir / "eval.jsonl").write_text(b["eval"], encoding="utf-8")
        rows = lora.load_examples(run_dir / "train.jsonl")
        t0 = time.time()
        try:
            if dry:
                stats = lora.dry_run(rows, run_dir, base, self.lang)
                self._record(status="dry", examples=b["stats"]["total"], new_examples=st["new_since_pass"], base=base, seconds=int(time.time() - t0), note=f"dry run · {run_dir}")
                return {"status": "dry", "dir": run_dir.as_posix(), **stats}
            self.log(f"training on {len(rows)} examples (base {base}) -> {run_dir}")
            stats = lora.train(rows, run_dir, base, self.lang, threads=int(os.environ.get("WATCHOVER_TRAIN_THREADS", "0") or 0) or None, log=self.log)
        except Exception as e:  # noqa: BLE001
            self._record(status="error", examples=b["stats"]["total"], new_examples=st["new_since_pass"], base=base, seconds=int(time.time() - t0), note=f"train: {type(e).__name__}: {str(e)[:300]}")
            return {"status": "error", "note": str(e)[:300]}
        # candidate model in Ollama (make sure the base tag is present first)
        try:
            if not wo_ollama.exists(self.url, base):
                self.log(f"pulling {base} into Ollama")
                for _ in wo_ollama.pull(self.url, base):
                    pass
            try:
                wo_ollama.delete(self.url, CANDIDATE)
            except Exception:  # noqa: BLE001
                pass
            wo_ollama.create_from_adapter(self.url, CANDIDATE, str(run_dir / "adapter"), base, SYSTEM.get(self.lang, SYSTEM["en"]),
                                          server_adapter_path=str(run_dir / "adapter"))
        except Exception as e:  # noqa: BLE001
            self._record(status="error", examples=b["stats"]["total"], new_examples=st["new_since_pass"], base=base, loss=stats.get("final_loss", 0), seconds=int(time.time() - t0), note=f"ollama create: {str(e)[:300]}")
            return {"status": "error", "note": f"ollama create: {str(e)[:300]}"}
        # quality gate
        a, src = self._gate_analysis()
        if a is None:
            g = {"passed": False, "note": "no dataset with >= 3 incidents for the gate"}
        else:
            try:
                g = lora.gate(CANDIDATE, a, self.url, self.lang, base_model=base, min_correct=float(s["train_gate_min"] or 0.6))
                g["dataset"] = src
            except Exception as e:  # noqa: BLE001
                g = {"passed": False, "note": f"gate error: {str(e)[:200]}"}
        if g.get("passed"):
            tag = "watchover-ops:" + datetime.now(UTC).strftime("%Y%m%d")
            wo_ollama.copy(self.url, CANDIDATE, "watchover-ops"); wo_ollama.copy(self.url, CANDIDATE, tag)
            status, model = "pass", "watchover-ops"
        else:
            status, model = "fail", ""
        try:
            wo_ollama.delete(self.url, CANDIDATE)
        except Exception:  # noqa: BLE001
            pass
        self._record(status=status, examples=b["stats"]["total"], new_examples=st["new_since_pass"], base=base, loss=stats.get("final_loss", 0),
                     seconds=int(time.time() - t0), gate=g, model=model, note=run_dir.as_posix())
        self.log(f"run {status}: {g}")
        return {"status": status, "gate": g, "dir": run_dir.as_posix(), **stats}

    def loop(self, poll_s: int = 60, dry: bool = False) -> None:
        self.log(f"trainer loop: ollama {self.url}, models {self.models_dir}, poll {poll_s}s")
        while True:
            try:
                heartbeat_path().parent.mkdir(parents=True, exist_ok=True); heartbeat_path().write_text(_now(), encoding="utf-8")
                if self.should_run():
                    self.run_once(dry=dry)
            except Exception as e:  # noqa: BLE001
                self.log(f"loop error: {type(e).__name__}: {e}")
            time.sleep(poll_s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true"); ap.add_argument("--once", action="store_true"); ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--poll", type=int, default=60); ap.add_argument("--lang", default=None)
    a = ap.parse_args(argv)
    from .knowledge import Knowledge
    kb = Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db"))
    lang = a.lang or wo_settings.load().get("lang", "tr") or "tr"
    t = AutoTrainer(kb, lang=lang)
    if a.loop:
        t.loop(a.poll, dry=a.dry_run); return 0
    res = t.run_once(force=a.force or a.once, dry=a.dry_run) if (a.once or a.force) else t.state()
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
