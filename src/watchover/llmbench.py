"""The bundled Ollama's speed on this machine, and the chat model it can carry (v1.59).

CPU inference speed is bound by memory bandwidth and the CPU share the LLM container gets, so it cannot be read off the RAM size:
it is measured. The ladder is Qwen 2.5 instruct (Turkish answers, JSON, log reasoning) at Q4_K_M, the GGUF form Ollama serves:

    qwen2.5:14b-instruct  ~9.0 GB    chosen when it generates at least --min-tps tokens/s here (default 5) and RAM is 60 GB+
    qwen2.5:7b-instruct   ~4.7 GB    the default
    qwen2.5:3b-instruct   ~1.9 GB    when the 7B stays under --floor-tps (default 2.5): answers would take minutes

The 7B is measured first; the 14B's speed is predicted from it (about weights 4.7 / 9.0 of it, the same bandwidth moves twice
the bytes per token) and it is downloaded and measured only when that prediction reaches the threshold, so a machine that
cannot carry it never spends 9 GB of disk on it. Downloaded models are never deleted here.

    python -m watchover.llmbench bench --model qwen2.5:7b-instruct [--url http://ollama:11434] [--threads N]
    python -m watchover.llmbench auto --mem-gb 64 [--threads N] [--min-tps 5] [--floor-tps 2.5] [--no-pull] [--shell]
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

LARGE, DEFAULT, SMALL = "qwen2.5:14b-instruct", "qwen2.5:7b-instruct", "qwen2.5:3b-instruct"
SIZE_GB = {LARGE: 9.0, DEFAULT: 4.7, SMALL: 1.9}
LARGE_MIN_RAM_GB = 60
PROMPT = ("Aşağıdaki log satırlarına bakarak olası kök nedeni iki cümleyle açıkla ve önerilen ilk adımı yaz.\n"
          "2026-10-19T09:14:02Z ERROR payment-api POST /v1/charge 502 upstream timeout after 30000 ms host=pay-02\n"
          "2026-10-19T09:14:03Z WARN  payment-api connection pool exhausted (50/50) db=orders-primary\n"
          "2026-10-19T09:14:05Z ERROR orders-db too many connections; max_connections=200 active=200\n"
          "2026-10-19T09:14:09Z ERROR payment-api POST /v1/charge 502 upstream timeout after 30000 ms host=pay-01\n")


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:            # noqa: S310 (the bundled Ollama on the compose network)
        return json.loads(r.read().decode() or "{}")


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:            # noqa: S310
        return json.loads(r.read().decode() or "{}")


def installed(url: str) -> list:
    return [m.get("name", "") for m in _get(url.rstrip("/") + "/api/tags").get("models", [])]


def pull(url: str, model: str, timeout: float = 3600) -> bool:
    doc = _post(url.rstrip("/") + "/api/pull", {"model": model, "stream": False}, timeout)
    return doc.get("status") == "success"


def bench(url: str, model: str, threads: int | None = None, tokens: int = 64, timeout: float = 900) -> dict:
    """Generation and prompt speed (tokens/s) of MODEL: one warm-up call loads it, the second is measured."""
    opts = {"num_predict": tokens, "temperature": 0, "seed": 1}
    if threads:
        opts["num_thread"] = int(threads)
    body = {"model": model, "prompt": PROMPT, "stream": False, "options": opts, "keep_alive": "5m"}
    root = url.rstrip("/") + "/api/generate"
    warm = _post(root, dict(body, options=dict(opts, num_predict=8)), timeout)
    doc = _post(root, body, timeout)
    rate = lambda n, d: round(n / (d / 1e9), 2) if n and d else 0.0  # noqa: E731
    return {"model": model, "tps": rate(doc.get("eval_count"), doc.get("eval_duration")),
            "prompt_tps": rate(doc.get("prompt_eval_count"), doc.get("prompt_eval_duration")),
            "load_s": round((warm.get("load_duration") or 0) / 1e9, 1), "threads": threads or 0}


def choose(measure, mem_gb: float, have: list, get_model=None, min_tps: float = 5.0, floor_tps: float = 2.5) -> dict:
    """The ladder above. measure(model) -> bench dict; get_model(model) -> bool downloads it (None: never download)."""
    res, notes = {}, []
    if DEFAULT not in have:
        return {"chat": "", "results": res, "notes": [f"{DEFAULT} is not downloaded: nothing to measure"]}
    res[DEFAULT] = r7 = measure(DEFAULT)
    chat = DEFAULT
    if r7["tps"] >= min_tps:
        predicted = round(r7["tps"] * SIZE_GB[DEFAULT] / SIZE_GB[LARGE], 2)
        if mem_gb < LARGE_MIN_RAM_GB:
            notes.append(f"14B not tried: {mem_gb:g} GB RAM (needs {LARGE_MIN_RAM_GB}+)")
        elif predicted < min_tps * 0.9:
            notes.append(f"14B not downloaded: predicted {predicted} tokens/s here (needs {min_tps:g})")
        elif LARGE not in have and (get_model is None or not get_model(LARGE)):
            notes.append(f"14B predicted {predicted} tokens/s but could not be downloaded")
        else:
            res[LARGE] = r14 = measure(LARGE)
            if r14["tps"] >= min_tps:
                chat = LARGE
            else:
                notes.append(f"14B measured {r14['tps']} tokens/s (needs {min_tps:g}): kept {DEFAULT}; the 14B stays downloaded")
    elif r7["tps"] < floor_tps and SMALL in have:
        res[SMALL] = measure(SMALL)
        chat = SMALL
        notes.append(f"7B only {r7['tps']} tokens/s here (floor {floor_tps:g}): the 3B answers in reasonable time")
    return {"chat": chat, "results": res, "notes": notes}


def summary(out: dict) -> str:
    return ", ".join(f"{m.split(':')[1].split('-')[0]} {r['tps']} tok/s" for m, r in out["results"].items()) or "-"


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.llmbench")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("bench", "auto"):
        p = sub.add_parser(name)
        p.add_argument("--url", default="http://ollama:11434")
        p.add_argument("--threads", type=int, default=0)
    sub.choices["bench"].add_argument("--model", default=DEFAULT)
    a = sub.choices["auto"]
    a.add_argument("--mem-gb", type=float, required=True)
    a.add_argument("--min-tps", type=float, default=5.0)
    a.add_argument("--floor-tps", type=float, default=2.5)
    a.add_argument("--no-pull", action="store_true")
    a.add_argument("--shell", action="store_true", help="KEY=value lines for watchover.sh")
    args = ap.parse_args(argv)
    threads = args.threads or None
    if args.cmd == "bench":
        print(json.dumps(bench(args.url, args.model, threads), ensure_ascii=False))
        return 0
    out = choose(lambda m: bench(args.url, m, threads), args.mem_gb, installed(args.url),
                 None if args.no_pull else (lambda m: pull(args.url, m)), args.min_tps, args.floor_tps)
    if args.shell:
        print(f"CHAT={out['chat']}")
        print(f"SUMMARY={summary(out)}")
        for n in out["notes"]:
            print(f"NOTE={n}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["chat"] else 1


if __name__ == "__main__":
    sys.exit(main())
