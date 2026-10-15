"""The dashboard's half of the Go receiver: publish who may send, then process what was received, in the background.

    auth.json   every few seconds: sha256 of each active agent token -> {id, name, env, site}, the shared key's sha256,
                whether open mode applies (no key, no active agent), and the RUM / mobile app keys with their allowed origins. Written atomically; the receiver reloads it on change.
    seg-*.wo    closed spool segments, oldest first: each record goes through the same path a direct POST would
                (LiveStore.ingest -> history, traces, anomalies) and the agent's last-seen / counters are updated.
    pseg-*.wo   the same, written by the receiver's Go parse stage (WATCHOVER_GO_PARSE, on by default with the Go receiver):
                a batch the Go parsers handled arrives as a v=2 "parsed" record (canonical observations) and goes to
                LiveStore.ingest_parsed, everything else is the raw v=1 record, unchanged. Both kinds are read in time order.
    profiles.json  (Go parse on) a copy of <home>/profiles.json plus scenario.MAPPING, so the Go parsers apply the same
                learned and user column mappings. The receiver parses only while this file exists (WATCHOVER_GO_PARSE=0 here
                removes it, so a receiver left "on" forwards everything raw instead of parsing without these mappings).

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


def go_parse_mode() -> str:
    """WATCHOVER_GO_PARSE as the receiver reads it: unset or "1" = "on", "shadow", "0"/"off"/"false"/"no" = "off".
    Here it only decides whether profiles.json is exported for the receiver (on and shadow): v=2 records are handled
    whatever the flag says, and raw segments are read in every mode."""
    v = (os.environ.get("WATCHOVER_GO_PARSE") or "1").strip().lower()
    return "off" if v in ("0", "off", "false", "no") else "shadow" if v == "shadow" else "on"


class SpoolConsumer:
    def __init__(self, spool_dir: str, store, registry=None, key_fn=None, handlers: dict | None = None, interval: float = 0.2, apps_fn=None):
        self.dir = Path(spool_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.store, self.registry, self.key_fn = store, registry, key_fn or (lambda: "")
        self.handlers = handlers or {}                      # path -> fn(header, body) for non-/ingest paths (sensors, OTLP)
        self.apps_fn = apps_fn                              # RUM / mobile apps: key -> name, origins (checked by the Go side too)
        self.interval = interval
        self.stop_evt = threading.Event()
        self.thread: threading.Thread | None = None
        self.records = self.batches_failed = self.segments = self.torn = 0
        self.parsed_records = self.raw_records = 0
        self._parsed_ms = 0.0
        self.last_lag_s = 0.0
        self.last_auth = ""
        self._auth_at = 0.0
        self.go_parse = go_parse_mode()
        self._profiles_sig = None

    # ---------------------------------------------------------------- auth export
    def auth_state(self) -> dict:
        agents = {}
        n_active = 0
        if self.registry is not None:
            for r in self.registry.kb._exec("SELECT id, name, env, site, token_hash FROM agents WHERE status='active'"):
                agents[r["token_hash"]] = {"id": int(r["id"]), "name": r["name"], "env": r["env"] or "", "site": r["site"] or ""}
            n_active = len(agents)
        key = (self.key_fn() or "").strip()
        apps = {}
        if self.apps_fn is not None:
            for a in self.apps_fn():
                if a.get("status") == "active":
                    apps[a["key"]] = {"name": a["name"], "origins": [o.strip().rstrip("/") for o in (a.get("origins") or "").replace("\n", ",").split(",") if o.strip()]}
        return {"agents": agents, "key_sha256": _sha(key) if key else "", "open": not key and n_active == 0, "apps": apps}

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

    def export_profiles(self) -> bool:
        """Go parse on: publish <home>/profiles.json into the spool for the receiver, when it changed, with what else the
        parse step reads from this process: scenario.MAPPING, the parse-path scenario constants (the receiver compares
        them with its compiled copies) and the names of enabled user grok patterns (the receiver does not port them)."""
        from . import grok as wo_grok, profiles as wo_profiles, scenario
        src = wo_profiles._path()
        extra = {"_scenario_mapping": scenario.MAPPING or {},
                 "_scenario_parse": {"SEVERITY_MAP": scenario.SEVERITY_MAP, "SIDE_TABLES": list(scenario.SIDE_TABLES),
                                     "DEDUP_KEY": scenario.DEDUP_KEY, "DEDUP_PREFER": scenario.DEDUP_PREFER},
                 "_grok_user": [x.name for x in wo_grok.user_patterns() if x.enabled]}
        try:
            st = src.stat()
            sig = (st.st_mtime_ns, st.st_size, json.dumps(extra, sort_keys=True))
        except OSError:
            sig = (0, 0, json.dumps(extra, sort_keys=True))
        if sig == self._profiles_sig and (self.dir / "profiles.json").exists():   # the receiver parses only while the file exists
            return False
        d = wo_profiles.load()
        if not isinstance(d, dict):
            d = {}
        d.update(extra)
        tmp = self.dir / "profiles.json.tmp"
        tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / "profiles.json")
        self._profiles_sig = sig
        return True

    # ---------------------------------------------------------------- processing
    @staticmethod
    def _ns(p: Path) -> str:
        return p.name.split("-", 1)[1].split(".", 1)[0]

    def pending(self) -> list[Path]:
        """Raw segments (seg-*.wo) and Go-parsed ones (pseg-*.wo), oldest first: <ns> is the receive time of the segment either way."""
        return sorted([*self.dir.glob("seg-*.wo"), *self.dir.glob("pseg-*.wo")], key=self._ns)

    def stats(self) -> dict:
        segs = self.pending()
        oldest = None
        if segs:
            try:
                oldest = max(0.0, time.time() - int(self._ns(segs[0])) / 1e9)
            except ValueError:
                oldest = None
        return {"engine": "go", "backlog_segments": len(segs), "backlog_bytes": sum(p.stat().st_size for p in segs if p.exists()),
                "oldest_s": round(oldest, 1) if oldest is not None else 0.0, "records": self.records, "segments_done": self.segments,
                "failed": self.batches_failed, "torn": self.torn, "last_lag_s": round(self.last_lag_s, 2), "go_parse": self.go_parse,
                "parsed_records": self.parsed_records, "raw_records": self.raw_records,
                "ingest_parsed_ms_avg": round(self._parsed_ms / self.parsed_records, 3) if self.parsed_records else 0.0}

    def _handle(self, h: dict, body: bytes) -> None:
        path = h.get("path") or "/ingest"
        if path == "/ingest":
            if h.get("v") == 2 and h.get("kind") == "parsed":       # parsed by the receiver (WATCHOVER_GO_PARSE)
                t0 = time.perf_counter()
                n = self.store.ingest_parsed(body, h)
                self.parsed_records += 1
                self._parsed_ms += (time.perf_counter() - t0) * 1000
            else:
                self.raw_records += 1
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
            if self.go_parse != "off":
                try:
                    self.export_profiles()
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
        if self.go_parse != "off":
            try:
                self.export_profiles()
            except Exception:  # noqa: BLE001
                pass
        else:                       # a stale export from an earlier "on" run would let the receiver parse without this
            try:                    # process's profiles / scenario.MAPPING; without the file it forwards every batch raw
                (self.dir / "profiles.json").unlink(missing_ok=True)
            except OSError:
                pass
        self.thread = threading.Thread(target=self._loop, daemon=True, name="spool-consumer")
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_evt.set()
