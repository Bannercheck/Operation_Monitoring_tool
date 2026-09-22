"""CPU LoRA training for `watchover-ops` (library part; scripts/train_lora.py and autotrain.py call these).

`train()` needs the optional `train` extra (torch, transformers, peft); `dry_run()` and `load_examples()` need nothing, so
the pipeline can be rehearsed and tested on any machine. Loss is computed on the assistant turn only."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .training import HF_BASE, modelfile


def load_examples(path: Path) -> list[dict]:
    rows = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except ValueError as e:
            raise ValueError(f"{path}:{i}: not JSON ({e})")
        msgs = d.get("messages")
        if not (isinstance(msgs, list) and len(msgs) >= 2 and msgs[-1].get("role") == "assistant" and msgs[-1].get("content")):
            raise ValueError(f"{path}:{i}: expected messages ending with a non-empty assistant turn")
        rows.append(d)
    if not rows:
        raise ValueError(f"{path}: no examples")
    return rows


def dry_run(rows: list[dict], out: Path, base: str, lang: str) -> dict:
    """Validate, estimate tokens, write the Modelfile and stats; no torch, no training."""
    chars = [sum(len(m["content"]) for m in r["messages"]) for r in rows]
    est = [c // 3 for c in chars]
    st = {"examples": len(rows), "avg_tokens": sum(est) // len(rows), "max_tokens": max(est), "over_1024": sum(1 for t in est if t > 1024),
          "base": base, "hf_base": HF_BASE.get(base, base), "dry_run": True}
    out.mkdir(parents=True, exist_ok=True)
    (out / "Modelfile").write_text(modelfile("./adapter", base, lang), encoding="utf-8")
    (out / "train_stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


def train(rows: list[dict], out: Path, base: str, lang: str, epochs: float = 2.0, lr: float = 2e-4, rank: int = 8, alpha: int = 16,
          max_len: int = 1024, accum: int = 8, threads: int | None = None, log=print) -> dict:
    try:
        import torch
        from peft import LoraConfig, get_peft_model
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        raise RuntimeError(f"missing training dependency ({e}); run: pip install -e \".[train]\"  — or use dry_run") from e
    threads = threads or max(1, (os.cpu_count() or 4) - 2)
    torch.set_num_threads(threads)
    hf = HF_BASE.get(base, base)
    log(f"base {hf} · {len(rows)} examples · r={rank} α={alpha} · seq {max_len} · epochs {epochs} · threads {threads}")
    tok = AutoTokenizer.from_pretrained(hf)
    tok.pad_token = tok.pad_token or tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(hf, torch_dtype=torch.float32)
    model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=alpha, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
                                             target_modules=["q_proj", "k_proj", "v_proj", "o_proj"]))

    def encode(r: dict) -> dict:
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
                    win = losses[-accum * 10:]
                    log(f"epoch {ep + 1} step {step}/{steps_total} loss {sum(win) / max(1, len(win)):.3f} · {time.time() - t0:.0f}s")
    out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(out / "adapter"), safe_serialization=True)
    tok.save_pretrained(str(out / "adapter"))
    (out / "Modelfile").write_text(modelfile("./adapter", base, lang), encoding="utf-8")
    st = {"examples": len(rows), "steps": step, "final_loss": round(sum(losses[-20:]) / max(1, len(losses[-20:])), 4), "seconds": int(time.time() - t0),
          "base": base, "hf_base": hf, "dry_run": False}
    (out / "train_stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
    return st


def gate(model: str, analysis, url: str, lang: str, base_model: str | None = None, min_correct: float = 0.6, tolerance: float = 0.1) -> dict:
    """Quality gate on the deterministic root-cause benchmark: the candidate must reach `min_correct` and, when a base
    model is given, must not fall more than `tolerance` below it. Returns the figures and `passed`."""
    from .llm import LLMConfig
    from .llm_eval import run_benchmark
    res = run_benchmark(LLMConfig(url, model, provider="ollama"), analysis, lang)
    score = res["correct"] / max(1, res["n"])
    out = {"model": model, "n": res["n"], "correct": res["correct"], "score": round(score, 3), "grounded": res["grounded"], "latency_ms": res["latency_ms"], "passed": score >= min_correct}
    if base_model:
        b = run_benchmark(LLMConfig(url, base_model, provider="ollama"), analysis, lang)
        bscore = b["correct"] / max(1, b["n"])
        out.update({"base_model": base_model, "base_score": round(bscore, 3), "passed": out["passed"] and score >= bscore - tolerance})
    return out
