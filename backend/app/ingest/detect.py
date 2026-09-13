"""Format detector: looks at the first lines and picks a parser name."""

from __future__ import annotations

from .parsers import PARSERS

SAMPLE_LINES = 50


def detect_format(text: str) -> tuple[str, float]:
    """Return (parser_name, confidence) for the given text."""
    lines = [ln for ln in text.splitlines()[:SAMPLE_LINES] if ln.strip()]
    best, best_score = "plain", 0.0
    for name, parser in PARSERS.items():
        score = parser.sniff(lines)
        if score > best_score:
            best, best_score = name, score
    return best, best_score
