"""Case-specific overrides. This is the ONLY place to touch on hackathon day.

Everything here is optional; empty values mean "use the generic defaults".
"""

from __future__ import annotations

# Column -> role mapping when the auto-mapper guesses wrong, e.g. {"timestamp": "event_ts", "message": "detail"}
MAPPING: dict[str, str] = {}

# Extra (regex, replacement) masks applied before fingerprinting, e.g. [(r"order-\d+", "<order>")]
EXTRA_MASKS: list[tuple[str, str]] = []

# Words that mark a signal as an infrastructure dependency (root-cause hint)
EXTRA_DEPENDENCY_WORDS: list[str] = []

# Correlation window in minutes and scoring weights; None keeps the defaults in analysis.py
WINDOW_MIN: int | None = None
WEIGHTS: dict[str, float] | None = None

# Recommendation templates: substring of root-cause template -> list of suggested actions
RECOMMENDATIONS: dict[str, list[str]] = {
    "timeout": ["Check connection pool exhaustion on the dependency", "Verify network path / DNS to the target"],
    "latency": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "duration": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "slow": ["Inspect slow queries and locks", "Check CPU / IO saturation on the host"],
    "no space": ["Free disk on the host, rotate logs", "Add disk usage alert threshold at 85%"],
    "oom": ["Raise memory limit or fix the leak", "Add memory alert before OOM"],
    "5xx": ["Roll back last deploy if correlated", "Check upstream dependency health"],
    "certificate": ["Renew the certificate", "Automate certificate rotation"],
}
