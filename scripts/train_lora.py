#!/usr/bin/env python3
"""Train `watchover-ops`: a LoRA adapter on Qwen 2.5 (1.5B / 3B) from the company's own memory, on CPU, no GPU needed.

  1. export the data      python -m watchover.training export --out training.jsonl            (or ./watchover.sh llm export)
  2. train (CPU)          python scripts/train_lora.py --data training.jsonl --base qwen2.5:1.5b-instruct --out models/watchover-ops
  3. import into Ollama   ./watchover.sh llm import models/watchover-ops      (docker)   ·   ollama create watchover-ops -f models/watchover-ops/Modelfile
  4. quality gate         python scripts/train_lora.py --gate --model watchover-ops --dataset samples/demo_mixed.zip

Needs the optional extra:  pip install -e ".[train]"   (torch, transformers, peft). `--dry-run` needs nothing: it validates the data,
counts tokens with a cheap estimate and writes the Modelfile so the pipeline can be rehearsed on any machine.

CPU sizing (32 cores, no GPU): 1.5B with r=8, seq 1024, batch 1 × accum 8 trains a few hundred examples per epoch in tens of
minutes; 3B roughly 2-3× slower. Memory: ~6 GB (1.5B) / ~12 GB (3B) in float32.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from watchover.training import HF_BASE, SYSTEM, modelfile  # noqa: E402


def load_examples(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError as e:
            raise SystemExit(f"{path}:{i}: not JSON ({e})")
        msgs = d.get("messages")
        if not (isinstance(msgs, list) and len(msgs) >= 2 and msgs[-1].get("role") == "assistant" and msgs[-1].get("content")):
            raise SystemExit(f"{path}:{i}: expected messages ending with a non-empty assistant turn")
        rows.append(d)
    if not rows:
        raise SystemExit(f"{path}: no examples")
    return rows


def dry_run(rows: list[dict], out: Path, base: str, lang: str) -> dict:
    chars = [sum(len(m["content"]) for m in r["messages"]) for r in rows]
    est_tokens = [c // 3 for c in chars]                     # rough: ~3 chars per token for Turkish/English mixed
    st = {"examples": len(rows), "avg_tokens": sum(est_tokens) // len(rows), "max_tokens": max(est_tokens), "over_1024": sum(1 for t in est_tokens if t > 1024),
          "base": base, "hf_base": HF_BASE.get(base, base)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "Modelfile").write_text(modelfile("./adapter", base, lang), encoding="utf-8")
    (out / "train_stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


def train(rows: list[dict], out: Path, base: str, lang: str, epochs: float, lr: float, rank: int, alpha: int, max_len: int,
          accum: int, threads: int) -> dict:
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        raise SystemExit(f"missing training dependency ({e}); run: pip install -e \".[train]\"  — or use --dry-run")
    torch.set_num_threads(threads)
    hf = HF_BASE.get(base, base)
    print(f"base {hf} · {len(rows)} examples · r={rank} α={alpha} · seq {max_len} · epochs {epochs} · threads {threads}", flush=True)
    tok = AutoTokenizer.from_pretrained(hf)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(hf, torch_dtype=torch.float32)
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))
    model.print_trainable_parameters()

    def encode(r: dict) -> dict:
        # loss only on the assistant turn: prompt tokens are masked with -100
        prompt = tok.apply_chat_template(r["messages"][:-1], tokenize=False, add_generation_prompt=True)
        full = prompt + r["messages"][-1]["content"] + tok.eos_token
        p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
        ids = tok(full, add_special_tokens=False, truncation=True, max_length=max_len)["input_ids"]
        labels = [-100] * min(len(p_ids), len(ids)) + ids[len(p_ids):]
        return {"input_ids": ids, "labels": labels[:len(ids)]}

    data = [encode(r) for r in rows]
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=lr, weight_decay=0.0)
    steps_total = max(1, int(len(data) * epochs / accum))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: max(0.05, 1 - s / steps_total))
    model.train(); step = 0; t0 = time.time(); losses = []
    n_epochs = int(epochs) + (1 if epochs % 1 else 0)
    for ep in range(n_epochs):
        limit = len(data) if ep < int(epochs) else int(len(data) * (epochs % 1))
        for i, ex in enumerate(data[:limit]):
            ids = torch.tensor([ex["input_ids"]]); labels = torch.tensor([ex["labels"]])
            loss = model(input_ids=ids, labels=labels).loss / accum
            loss.backward(); losses.append(loss.item() * accum)
            if (i + 1) % accum == 0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step(); sched.step(); opt.zero_grad(); step += 1
                if step % 10 == 0:
                    print(f"epoch {ep + 1} step {step}/{steps_total} loss {sum(losses[-accum * 10:]) / max(1, len(losses[-accum * 10:])):.3f} · {time.time() - t0:.0f}s", flush=True)
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out / "adapter"), safe_serialization=True)
    tok.save_pretrained(str(out / "adapter"))
    (out / "Modelfile").write_text(modelfile("./adapter", base, lang), encoding="utf-8")
    st = {"examples": len(rows), "steps": step, "final_loss": round(sum(losses[-20:]) / max(1, len(losses[-20:])), 4), "seconds": int(time.time() - t0), "base": base, "hf_base": hf}
    (out / "train_stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


def gate(model: str, dataset: str, base_model: str | None, url: str, lang: str, min_correct: float, tolerance: float) -> int:
    """Quality gate: the new model must reach `min_correct` on the deterministic root-cause benchmark and must not fall
    more than `tolerance` below the base model (when one is given). Exit code 0 = PASS."""
    from watchover.analysis import Analysis
    from watchover.llm import LLMConfig
    from watchover.llm_eval import run_benchmark
    from watchover.pipeline import ingest_path
    obs, rep = ingest_path(dataset)
    a = Analysis(obs, rep)
    res = run_benchmark(LLMConfig(url, model, provider="ollama"), a, lang)
    score = res["correct"] / max(1, res["n"]); grounded = res["grounded"] / max(1, res["n"])
    print(f"{model}: correct {res['correct']}/{res['n']} ({score:.0%}) · grounded {grounded:.0%} · {res['latency_ms']} ms/case")
    ok = score >= min_correct
    if base_model:
        b = run_benchmark(LLMConfig(url, base_model, provider="ollama"), a, lang)
        bscore = b["correct"] / max(1, b["n"])
        print(f"{base_model}: correct {b['correct']}/{b['n']} ({bscore:.0%}) · {b['latency_ms']} ms/case")
        ok = ok and score >= bscore - tolerance
    print("GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="training.jsonl"); ap.add_argument("--out", default="models/watchover-ops")
    ap.add_argument("--base", default="qwen2.5:1.5b-instruct", help="Ollama tag of the base (1.5b for CPU speed, 3b for quality)")
    ap.add_argument("--lang", default="tr"); ap.add_argument("--epochs", type=float, default=2.0); ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=8); ap.add_argument("--alpha", type=int, default=16); ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--accum", type=int, default=8); ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    ap.add_argument("--dry-run", action="store_true", help="validate data, write Modelfile + stats, no training (no torch needed)")
    ap.add_argument("--gate", action="store_true", help="run the quality gate against a model already created in Ollama")
    ap.add_argument("--model", default="watchover-ops"); ap.add_argument("--base-model", default=None, help="compare against this Ollama model in --gate")
    ap.add_argument("--dataset", default=str(ROOT / "samples" / "demo_mixed.zip")); ap.add_argument("--url", default="http://localhost:11434")
    ap.add_argument("--min-correct", type=float, default=0.6); ap.add_argument("--tolerance", type=float, default=0.1)
    a = ap.parse_args(argv)
    if a.gate:
        return gate(a.model, a.dataset, a.base_model, a.url, a.lang, a.min_correct, a.tolerance)
    rows = load_examples(Path(a.data))
    out = Path(a.out)
    st = dry_run(rows, out, a.base, a.lang) if a.dry_run else train(rows, out, a.base, a.lang, a.epochs, a.lr, a.rank, a.alpha, a.max_len, a.accum, a.threads)
    print(json.dumps(st, indent=1))
    print(f"\nnext: ollama create {a.model} -f {out / 'Modelfile'}   (docker: ./watchover.sh llm import {out})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
