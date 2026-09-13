"""Root-cause ranking inside a component: earliest onset, dependency hints, fan-out (TODO)."""

from __future__ import annotations

from app.model import Signal


def pick_root_cause(component: list[Signal]) -> Signal:
    raise NotImplementedError
