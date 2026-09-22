"""Fine-tuning helpers: export the company's memory as chat-format JSONL and render the Ollama Modelfile that turns a
trained LoRA adapter into the `watchover-ops` model.

    python -m watchover.training export  --out training.jsonl [--lang tr] [--part all|train|eval]
    python -m watchover.training modelfile --adapter models/watchover-ops/adapter --base qwen2.5:3b-instruct > Modelfile

The trainer itself is scripts/train_lora.py (CPU, needs the optional `train` extra: pip install -e ".[train]").
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import autolearn

SYSTEM = {"tr": "Sen Watchover operasyon asistanısın. Kanıta dayalı, kısa ve gerekçeli yanıt verirsin.",
          "en": "You are the Watchover operations assistant. Answer briefly, with evidence and reasons."}
HF_BASE = {"qwen2.5:1.5b-instruct": "Qwen/Qwen2.5-1.5B-Instruct", "qwen2.5:3b-instruct": "Qwen/Qwen2.5-3B-Instruct",
           "qwen2.5:7b-instruct": "Qwen/Qwen2.5-7B-Instruct"}                      # Ollama tag -> Hugging Face repo of the same weights


def open_kb():
    from .knowledge import Knowledge
    return Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db"))


def export(out: Path, lang: str = "tr", part: str = "all", kb=None) -> dict:
    kb = kb or open_kb()
    b = autolearn.training_bundle(kb, lang)
    body = b[part] if part in ("train", "eval") else autolearn.training_export(kb, lang).decode()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    return b["stats"] | {"written": out.as_posix(), "part": part}


def modelfile(adapter: str | None, base: str = "qwen2.5:3b-instruct", lang: str = "tr", merged: str | None = None,
              temperature: float = 0.2, ctx: int = 4096) -> str:
    """Ollama Modelfile: FROM the base tag + ADAPTER (safetensors LoRA), or FROM a merged HF directory when the
    architecture's adapter import is not supported by the local Ollama build."""
    src = merged if merged else base
    lines = [f"FROM {src}"]
    if adapter and not merged:
        lines.append(f"ADAPTER {adapter}")
    lines += [f"SYSTEM \"\"\"{SYSTEM.get(lang, SYSTEM['en'])}\"\"\"", f"PARAMETER temperature {temperature}", f"PARAMETER num_ctx {ctx}",
              "PARAMETER stop \"<|im_end|>\"", "PARAMETER stop \"<|endoftext|>\""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m watchover.training", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write chat-format JSONL from the knowledge database")
    e.add_argument("--out", default="training.jsonl"); e.add_argument("--lang", default="tr"); e.add_argument("--part", default="all", choices=["all", "train", "eval"])
    m = sub.add_parser("modelfile", help="print the Ollama Modelfile for a trained adapter")
    m.add_argument("--adapter", default=None); m.add_argument("--merged", default=None); m.add_argument("--base", default="qwen2.5:3b-instruct"); m.add_argument("--lang", default="tr")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        st = export(Path(a.out), a.lang, a.part)
        print(f"{st['written']}: {st['total']} examples (train {st['train']} / eval {st['eval']}) · " + ", ".join(f"{k} {v}" for k, v in st["by_source"].items()), file=sys.stderr)
        return 0
    sys.stdout.write(modelfile(a.adapter, a.base, a.lang, a.merged))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
