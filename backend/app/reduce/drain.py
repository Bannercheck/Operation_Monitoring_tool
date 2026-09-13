"""Drain-style log template miner (TODO).

Tokenises messages, masks volatile tokens (numbers, IPs, UUIDs, hex), then
clusters by token count and prefix into templates with <*> wildcards.
"""

from __future__ import annotations

from app.model import Observation


class Drain:
    def __init__(self, depth: int = 4, similarity: float = 0.5) -> None:
        self.depth = depth
        self.similarity = similarity
        self.templates: dict[str, str] = {}

    def assign(self, observations: list[Observation]) -> None:
        """Fill observation.template_id in place."""
        raise NotImplementedError
