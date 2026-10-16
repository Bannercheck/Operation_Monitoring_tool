"""The `act-llm` queue (docs/design/action-agent.md §8.3, C25, C27): every action-agent model call goes through one bounded queue
and one worker — never from request threads, the CaseRouter or the engine tick, so a slow CPU model can never block a case.

Priority: live select (0) > act_next (1) > lazy explain (2) > drafts (3); the same key is queued once; when the queue is full
the oldest item of the lowest priority is dropped and counted. The nightly challenger batch is not queued here (night window)."""
from __future__ import annotations

import heapq
import itertools
import threading
from typing import Callable, Dict, List, Optional, Tuple

PRIORITY = {"select": 0, "next": 1, "explain": 2, "draft": 3}


class LLMQueue:
    def __init__(self, maxlen: int = 200):
        self.maxlen = max(1, int(maxlen))
        self._heap: List[Tuple[int, int, str, str, Callable[[], object]]] = []
        self._keys: Dict[str, int] = {}
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats = {"queued": 0, "done": 0, "dropped": 0, "errors": 0, "deduped": 0}

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)

    def submit(self, kind: str, key: str, fn: Callable[[], object]) -> bool:
        """Queue one call; False when the same key is already waiting."""
        pri = PRIORITY.get(kind, 3)
        with self._lock:
            if key in self._keys:
                self.stats["deduped"] += 1
                return False
            if len(self._heap) >= self.maxlen:                       # drop the oldest of the lowest priority
                worst = max(self._heap, key=lambda x: (x[0], -x[1]))
                self._heap.remove(worst)
                heapq.heapify(self._heap)
                self._keys.pop(worst[3], None)
                self.stats["dropped"] += 1
            item = (pri, next(self._seq), kind, key, fn)
            heapq.heappush(self._heap, item)
            self._keys[key] = item[1]
            self.stats["queued"] += 1
        self._wake.set()
        return True

    def run_once(self) -> bool:
        """Run the most urgent item (tests call this; the worker thread loops on it)."""
        with self._lock:
            if not self._heap:
                return False
            _pri, _n, _kind, key, fn = heapq.heappop(self._heap)
            self._keys.pop(key, None)
        try:
            fn()
            self.stats["done"] += 1
        except Exception:  # noqa: BLE001 - one failed call never stops the worker
            self.stats["errors"] += 1
        return True

    def start(self) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.is_set():
                if not self.run_once():
                    self._wake.wait(1.0)
                    self._wake.clear()

        self._stop.clear()
        self._thread = threading.Thread(target=loop, name="act-llm", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread = None
