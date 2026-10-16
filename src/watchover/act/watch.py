"""Side-effect watch (docs/design/action-agent.md §4.7; C48, R7).

After the last act step verified, the run watches the host for `watch.minutes` (default 15, 5–240; time-scaled in demos) and
compares the window after the step with the 15 minutes before it, skipping the first `expected_noise_s` seconds:

    (a) r_post ≥ max(2·r_pre, r_pre + 5) ERROR+/min and errors_post ≥ 20
    (b) ≥ 2 ERROR+ templates on the host not seen in the 24 h before the step (excluding the case's evidence templates and the
        runbook's expected_noise list)
    (c) a new anomaly / case on the host with a key other than the case key
    (d) the host's agent silent > 5 min beyond the step's expect_silence_s
    (e) availability of a service on the host drops > 2 points against the previous hour

`expected_noise` (≤ 5 exact template ids) and `expected_noise_s` (≤ 300) only ever suppress (a) and (b); (c), (d) and (e) are
never suppressed. Suppressed hits are still recorded (`act_steps.side_effect.suppressed`) for the reviewer and promotion screens."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional


def evaluate(pre: Optional[Dict[str, Any]], post: Optional[Dict[str, Any]], *, new_templates: Iterable[str] = (),
             expected_noise: Iterable[str] = (), known_templates: Iterable[str] = (), new_cases: Iterable[str] = (),
             silent_s: float = 0.0, expect_silence_s: float = 0.0, slo_pre: Optional[float] = None,
             slo_post: Optional[float] = None, noise_window_hits: Iterable[str] = ()) -> Dict[str, Any]:
    """Pure decision over collected numbers: {"side_effect", "hits": [{rule, detail}], "suppressed": [{rule, detail}]}."""
    hits: List[dict] = []
    suppressed: List[dict] = []
    noise = set(expected_noise or ())
    if pre is not None and post is not None:
        r_pre, r_post, n_post = float(pre.get("per_min") or 0), float(post.get("per_min") or 0), int(post.get("count") or 0)
        if r_post >= max(2 * r_pre, r_pre + 5) and n_post >= 20:
            hits.append({"rule": "a", "detail": f"ERROR+/dk {r_pre:.1f} → {r_post:.1f} ({n_post})"})
    known = set(known_templates or ())
    fresh = [t for t in new_templates if t not in known]
    real = [t for t in fresh if t not in noise]
    quiet = [t for t in fresh if t in noise]
    if len(real) >= 2:
        hits.append({"rule": "b", "detail": f"yeni hata şablonları: {len(real)}", "templates": real[:10]})
    if quiet:
        suppressed.append({"rule": "b", "detail": f"{len(quiet)} hata şablonu beklenen gürültü sayıldı", "templates": quiet[:10]})
    for t in noise_window_hits or ():
        suppressed.append({"rule": "noise_s", "detail": str(t)[:120]})
    cases = list(new_cases or ())
    if cases:
        hits.append({"rule": "c", "detail": f"yeni anomali: {', '.join(cases[:3])}"})        # never suppressed
    if silent_s > 300 + float(expect_silence_s or 0):
        hits.append({"rule": "d", "detail": f"ajan {int(silent_s)} sn sessiz"})            # never suppressed
    if slo_pre is not None and slo_post is not None and slo_pre - slo_post > 2:
        hits.append({"rule": "e", "detail": f"erişilebilirlik %{slo_pre:.1f} → %{slo_post:.1f}"})   # never suppressed
    return {"side_effect": bool(hits), "hits": hits, "suppressed": suppressed}


def collect(sources, host: str, case_key: str, step_at: datetime, end: datetime, *, expected_noise_s: int = 0,
            expected_noise: Iterable[str] = (), known_templates: Iterable[str] = (), expect_silence_s: int = 0) -> Dict[str, Any]:
    """Gather the numbers for `evaluate` from the dashboard's own data (Sources) and decide."""
    skip = step_at + timedelta(seconds=int(expected_noise_s or 0))
    pre = sources.errors(host, step_at - timedelta(minutes=15), step_at)
    post = sources.errors(host, skip, end) if end > skip else {"count": 0, "per_min": 0.0, "templates": []}
    noisy = sources.errors(host, step_at, skip) if skip > step_at else None
    before = sources.templates_seen(host, step_at - timedelta(hours=24), step_at) or set()
    new_t = [t for t in ((post or {}).get("templates") or []) if t not in before]
    seen = sources.agent_last_seen(host)
    silent = (end - seen).total_seconds() if seen is not None else 0.0
    res = evaluate(pre, post, new_templates=new_t, expected_noise=expected_noise, known_templates=known_templates,
                   new_cases=sources.new_cases(host, step_at, case_key), silent_s=silent, expect_silence_s=expect_silence_s,
                   slo_pre=sources.slo(host, 60, step_at), slo_post=sources.slo(host, 15, end),
                   noise_window_hits=(noisy or {}).get("templates") or [])
    res["window"] = {"from": step_at.isoformat(timespec="seconds"), "to": end.isoformat(timespec="seconds"), "skip_s": int(expected_noise_s or 0)}
    return res
