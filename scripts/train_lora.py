#!/usr/bin/env python3
"""Train `watchover-ops` by hand: a LoRA adapter on Qwen 2.5 (1.5B / 3B) from the company's memory, on CPU.
The Docker stack does this automatically (trainer service, LLM › Quality › "Your own model"); this script is the manual /
offline path and uses the same library (watchover.lora).

  export   python -m watchover.training export --out training.jsonl              (docker: ./watchover.sh llm export)
  train    python scripts/train_lora.py --data training.jsonl --base qwen2.5:1.5b-instruct --out models/watchover-ops
  import   ./watchover.sh llm import models/watchover-ops     ·   ollama create watchover-ops -f models/watchover-ops/Modelfile
  gate     python scripts/train_lora.py --gate --model watchover-ops --base-model qwen2.5:1.5b-instruct --dataset samples/demo_mixed.zip

Needs `pip install -e ".[train]"` for training; `--dry-run` needs nothing (validates data, writes the Modelfile).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from watchover import lora  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="training.jsonl"); ap.add_argument("--out", default="models/watchover-ops")
    ap.add_argument("--base", default="qwen2.5:1.5b-instruct", help="Ollama tag of the base (1.5b for CPU speed, 3b for quality)")
    ap.add_argument("--lang", default="tr"); ap.add_argument("--epochs", type=float, default=2.0); ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=8); ap.add_argument("--alpha", type=int, default=16); ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--accum", type=int, default=8); ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--gate", action="store_true")
    ap.add_argument("--model", default="watchover-ops"); ap.add_argument("--base-model", default=None)
    ap.add_argument("--dataset", default=str(ROOT / "samples" / "demo_mixed.zip")); ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--min-correct", type=float, default=0.6); ap.add_argument("--tolerance", type=float, default=0.1)
    a = ap.parse_args(argv)
    if a.gate:
        from watchover.analysis import Analysis
        from watchover.pipeline import ingest_path
        obs, rep = ingest_path(a.dataset)
        g = lora.gate(a.model, Analysis(obs, rep), a.url, a.lang, a.base_model, a.min_correct, a.tolerance)
        print(json.dumps(g, indent=1)); print("GATE:", "PASS" if g["passed"] else "FAIL")
        return 0 if g["passed"] else 1
    try:
        rows = lora.load_examples(Path(a.data))
        out = Path(a.out)
        st = lora.dry_run(rows, out, a.base, a.lang) if a.dry_run else lora.train(rows, out, a.base, a.lang, a.epochs, a.lr, a.rank, a.alpha, a.max_len, a.accum, a.threads)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr); return 1
    print(json.dumps(st, indent=1))
    print(f"\nnext: ollama create {a.model} -f {out / 'Modelfile'}   (docker: ./watchover.sh llm import {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
