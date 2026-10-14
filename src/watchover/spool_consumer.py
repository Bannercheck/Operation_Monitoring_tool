"""The dashboard's half of the Go receiver: publish who may send, then process what was received, in the background.

    auth.json   every few seconds: sha256 of each active agent token -> {id, name, env, site}, the shared key's sha256, and
                whether open mode applies (no key, no active agent). Written atomically; the receiver reloads it on change.
    seg-*.wo    closed spool segments, oldest first: each record goes through the same path a direct POST would
                (LiveStore.ingest -> history, traces, anomalies) and the agent's last-seen / counters are updated.

At-least-once: a segment is deleted only after every record in it was handled; the byte offset reached is kept in
<segment>.pos, so a restart resumes inside the segment instead of replaying it. A torn last record (power loss while the
receiver wrote) is skipped. Lag and backlog are reported in the System › Capacity panel.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class SpoolConsumer:
    def __init__(self, spool_dir: str, store, registry=None, key_fn=None, handlers: dict | None = None, interval: float = 0.2):
        self.dir = Path(spool_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store, self.registry, self.key_fn = store, registry, key_fn or (lambda: "")
        self.handlers = handlers or {}                      # path -> fn(header, body) for non-/ingest paths (sensors, OTLP)
        self.interval = interval
        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None
        self.records = self.batches_failed = self.segments = self.torn = 0
        self.last_lag_s = 0.0
        self.last_auth = ""
        self._auth_at = 0.0

    # ---------------------------------------------------------------- auth export
    def auth_state(self) -> dict:
        agents = {}
        n_active = 0
        if self.registry is not None:
            for r in self.registry.kb._exec("SELECT id, name, env, site, token_hash FROM agents WHERE status='active'"):
                agents[r["token_hash"]] = {"id": int(r["id"]), "name": r["name"], "env": r["env"] or "", "site": r["site"] or ""}
            n_active = len(agents)
        key = (self.key_fn() or "").strip()
        return {"agents": agents, "key_sha256": _sha(key) if key else "", "open": not key and n_active == 0}

    def export_auth(self, force: bool = False) -> bool:
        st = self.auth_state()
        blob = json.dumps(st, sort_keys=True)
        if blob == self.last_auth and not force:
            return False
        st["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = self.dir / "auth.json.tmp"
        tmp.write_text(json.dumps(st))
        try:
            os.chmod(tmp, 0o640)
        except OSError:
            pass
        os.replace(tmp, self.dir / "auth.json")
        self.last_auth = blob
        return True

    # ---------------------------------------------------------------- processing
    def pending(self) -> list[Path]:
        return sorted(self.dir.glob("seg-*.wo"))

    def stats(self) -> dict:
        segs = self.pending()
        oldest = None
        if segs:
            try:
                oldest = max(0.0, time.time() - int(segs[0].name[4:23]) / 1e9)
            except ValueError:
                oldest = None
        return {"engine": "go", "backlog_segments": len(segs), "backlog_bytes": sum(p.stat().st_size for p in segs if p.exists()),
                "oldest_s": round(oldest, 1) if oldest is not None else 0.0, "records": self.records, "segments_done": self.segments,
                "failed": self.batches_failed, "torn": self.torn, "last_lag_s": round(self.last_lag_s, 2)}

    def _handle(self, h: dict, body: bytes) -> None:
        path = h.get("path") or "/ingest"
        if path == "/ingest":
            n = self.store.ingest(h.get("file") or "agent.log", body, h.get("agent") or "unknown", env=h.get("env") or "", site=h.get("site") or "",
                                  agent_id=h.get("agent_id"))
            if h.get("agent_id") is not None and self.registry is not None:
                try:
                    self.registry.touch(int(h["agent_id"]), h.get("ip", ""), n)
                except Exception:  # noqa: BLE001
                    pass
        elif path in self.handlers:
            self.handlers[path](h, body)

    def process_segment(self, seg: Path) -> int:
        pos_file = seg.with_suffix(".pos")
        try:
            start = int(pos_file.read_text()) if pos_file.exists() else 0
        except ValueError:
            start = 0
        data = seg.read_bytes()
        i, n, last_pos_write = start, 0, time.time()
        while i < len(data):
            nl = data.find(b"\n", i)
            if nl < 0:
                self.torn += 1; break
            try:
                h = json.loads(data[i:nl])
                ln = int(h["len"])
            except (ValueError, KeyError, TypeError):
                self.torn += 1; break
            body = data[nl + 1: nl + 1 + ln]
            if len(body) < ln:
                self.torn += 1; break
            try:
                self._handle(h, body)
            except Exception:  # noqa: BLE001 - a batch the parser refuses is counted, the rest of the segment goes on
                self.batches_failed += 1
            ts = h.get("ts")
            if ts:
                try:
                    from datetime import datetime
                    self.last_lag_s = max(0.0, time.time() - datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
            i = nl + 1 + ln + 1
            n += 1; self.records += 1
            if time.time() - last_pos_write > 1:
                pos_file.write_text(str(i)); last_pos_write = time.time()
        seg.unlink(missing_ok=True)
        pos_file.unlink(missing_ok=True)
        self.segments += 1
        return n

    def run_once(self) -> int:
        if time.time() - self._auth_at > 3:
            try:
                self.export_auth()
            except Exception:  # noqa: BLE001
                pass
            self._auth_at = time.time()
        done = 0
        for seg in self.pending():
            if self.stop_evt.is_set():
                break
            done += self.process_segment(seg)
        return done

    def _loop(self) -> None:
        while not self.stop_evt.is_set():
            try:
                busy = self.run_once()
            except Exception:  # noqa: BLE001 - never let the consumer die
                busy = 0
            if not busy:
                self.stop_evt.wait(self.interval)

    def start(self) -> "SpoolConsumer":
        try:
            self.export_auth(force=True)
        except Exception:  # noqa: BLE001
            pass
        self.thread = threading.Thread(target=self._loop, daemon=True, name="spool-consumer")
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_evt.set()
