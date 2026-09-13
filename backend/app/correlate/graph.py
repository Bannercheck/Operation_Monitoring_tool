"""Co-occurrence graph: signals are nodes; edges from shared time window and shared entities (TODO)."""

from __future__ import annotations

from app.model import Signal


def build_components(signals: list[Signal], window_minutes: int = 5) -> list[list[Signal]]:
    """Return connected components of correlated signals."""
    raise NotImplementedError
