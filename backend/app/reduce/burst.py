"""Burst detection: per-signal rate per minute vs EWMA baseline (TODO)."""

from __future__ import annotations

from app.model import Signal


def score_bursts(signals: list[Signal], window_minutes: int = 1) -> None:
    """Fill peak_rate, baseline_rate, burst_score and onset on each signal."""
    raise NotImplementedError
