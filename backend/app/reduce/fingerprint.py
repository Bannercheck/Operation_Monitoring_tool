"""Fingerprint = sha1(template + severity + service). Groups observations into Signals (TODO)."""

from __future__ import annotations

from app.model import Observation, Signal


def build_signals(observations: list[Observation]) -> list[Signal]:
    raise NotImplementedError
