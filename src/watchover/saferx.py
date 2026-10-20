"""Regular expressions a user typed (grok patterns, business-flow keys, dataset search, log search), run with a time limit.

Python's `re` holds the interpreter lock for the whole match, so one catastrophic pattern ((.*.*)*!, (.*){12}!) stops every request
of the single API process. A shape check cannot catch every such pattern; a clock can. With the `regex` module (a dependency) each
call gets `CALL_S` and the pattern as a whole a cumulative `budget_s`; past either the call raises PatternTimeout (a ValueError: 400
on a request, a business flow is switched off). Without the module the stricter shape check below is the only guard.

    rx = saferx.compile(user_text, re.I, budget_s=2.0)
    m = rx.search(line)            # a regex Match, the same API as re.Match
"""
from __future__ import annotations

import re
import time

try:
    import regex as _rx                       # noqa: F401  (timeout= support)
except ImportError:                           # pragma: no cover - the image always has it
    _rx = None

CALL_S = 0.05                                 # one match on one line
BUDGET_S = 2.0                                # all matches of one compiled pattern (one request, one flow window)
MAX_LEN = 4000
# nested quantifiers: (a+)+  (a*)*  (.*){12}  (x+){2,}  and a quantified group holding an alternation of overlapping parts
_RISKY = re.compile(r"\([^()]*[+*}][^()]*\)\s*(?:[*+]|\{\d*,?\d*\})|\)\s*\+\s*[*+]|\([^()]*\|[^()]*\)\s*(?:[*+]|\{\d+,\d*\})")


class PatternTimeout(ValueError):
    """The pattern took too long (catastrophic backtracking): refused instead of stopping the server."""


def shape_ok(pattern: str) -> bool:
    return len(pattern) <= MAX_LEN and not _RISKY.search(pattern)


class SafePattern:
    __slots__ = ("rx", "pattern", "groups", "groupindex", "budget", "spent", "_timed")

    def __init__(self, pattern: str, flags: int = 0, budget_s: float = BUDGET_S):
        if len(pattern) > MAX_LEN:
            raise ValueError(f"pattern too long ({len(pattern)} > {MAX_LEN})")
        self._timed = _rx is not None
        if self._timed:
            try:
                self.rx = _rx.compile(pattern, int(flags) | _rx.VERSION0)
            except _rx.error as e:                      # callers catch re.error, as for re.compile
                raise re.error(str(e)) from None
        else:
            if not shape_ok(pattern):
                raise ValueError("pattern has nested unbounded quantifiers (catastrophic backtracking risk)")
            self.rx = re.compile(pattern, flags)
        self.pattern, self.groups, self.groupindex = pattern, self.rx.groups, dict(self.rx.groupindex)
        self.budget, self.spent = float(budget_s), 0.0

    def _run(self, fn, *a, **k):
        if not self._timed:
            return fn(*a, **k)
        left = self.budget - self.spent
        if left <= 0:
            raise PatternTimeout("pattern too slow: its time budget is used up")
        t0 = time.perf_counter()
        try:
            return fn(*a, timeout=min(CALL_S, left), **k)
        except TimeoutError:
            self.spent = self.budget
            raise PatternTimeout("pattern too slow (catastrophic backtracking): refused") from None
        finally:
            self.spent += time.perf_counter() - t0

    def search(self, s, *a):
        return self._run(self.rx.search, s, *a)

    def match(self, s, *a):
        return self._run(self.rx.match, s, *a)

    def fullmatch(self, s, *a):
        return self._run(self.rx.fullmatch, s, *a)

    def findall(self, s, *a):
        return self._run(self.rx.findall, s, *a)

    def sub(self, repl, s, count=0):
        return self._run(self.rx.sub, repl, s, count)

    def reset(self) -> None:
        """A long-lived pattern (a business flow) starts a new budget window."""
        self.spent = 0.0


def compile(pattern: str, flags: int = 0, budget_s: float = BUDGET_S) -> SafePattern:   # noqa: A001 (mirrors re.compile)
    return SafePattern(pattern, flags, budget_s)
