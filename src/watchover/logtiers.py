"""Log tiers: raw log lines live outside the main database (PostgreSQL stays small).

  hot   `live_batches` rows (logstore v2) in SQLite day files under WATCHOVER_LOG_HOT_DIR (default <WATCHOVER_HOME>/logs/hot):
        logs-YYYYMMDD.db per UTC day of the row's ts_max, WAL + synchronous=NORMAL. HotStore speaks the Database subset the readers
        use (_exec, executemany, pk, pg); every read connection sees one TEMP VIEW `live_batches` over the attached day files, so
        the explorer, live tail, detail, context and the analysis window keep their SQL. Expiry deletes a file: no VACUUM, no bloat.
  cold  one archive per hot day under WATCHOVER_LOG_COLD_DIR (default <WATCHOVER_HOME>/logs/cold), logs-YYYYMMDD.wcl: chunks of
        ~32k lines, each stored JSON line split into a template (its text with the digit runs cut out, repeats as back references,
        the stamp and the line id as deltas) and its digit runs in per-(template, slot) columns, lzma. Lines come back byte for
        byte (the privacy transform included), so search, detail and context read archived lines like stored ones.
  job   hourly, archiving in a child process at the lowest CPU priority: hot days older than live_retention_h go to cold,
        hot_max_gb archives the oldest hot day early, cold_retention_days (default 365) expires archives. Crash-safe: a temp
        file + rename, and a re-run merges with an existing archive by row id (no loss, no duplicate).
  audit every archive is sealed (0440) and entered in a hash-chained manifest (logaudit.py); a size cap never deletes an archive
        younger than the retention: cold_max_gb is a budget that raises alerts (80 % / 90 %). When the archive disk cannot take
        the next day, archiving pauses (the day stays on the hot disk, alert cold_full) and resumes by itself once there is room.
  paths given by hand (watchover.sh storage set, System › Storage): nothing picks or proposes a disk.

WATCHOVER_LOG_STORE=db keeps the old layout: live_batches in the main database, pruned after live_retention_h, no archive.
"""
from __future__ import annotations

import heapq
import json
import lzma
import os
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from operator import itemgetter
from pathlib import Path
from typing import Iterable, Iterator, Optional

from . import logaudit

UTC = timezone.utc
GB = 1024 ** 3
COLS = ("id", "ts_min", "ts_max", "received_at", "agent", "env", "n", "payload", "v", "pz", "seq_min", "seq_max", "sevmask", "host", "src")
SEL = ", ".join(COLS)
DDL = ("CREATE TABLE IF NOT EXISTS {t} (id INTEGER PRIMARY KEY, ts_min TEXT, ts_max TEXT, received_at TEXT, agent TEXT DEFAULT '', "
       "env TEXT DEFAULT '', n INTEGER DEFAULT 0, payload TEXT DEFAULT '', v INTEGER DEFAULT 2, pz BLOB, seq_min INTEGER DEFAULT 0, "
       "seq_max INTEGER DEFAULT 0, sevmask INTEGER DEFAULT 31, host TEXT DEFAULT '', src TEXT DEFAULT '')")
INDEXES = ("CREATE INDEX IF NOT EXISTS live_batches_seq ON live_batches(seq_min)",
           "CREATE INDEX IF NOT EXISTS live_batches_host ON live_batches(host, ts_max)",
           "CREATE INDEX IF NOT EXISTS live_batches_agent ON live_batches(agent, ts_max)",
           "CREATE INDEX IF NOT EXISTS live_batches_tsmm ON live_batches(ts_max, ts_min)")
HOT_RE = re.compile(r"^logs-(\d{8})\.db$")
COLD_RE = re.compile(r"^logs-(\d{8})\.wcl$")
INSERT_RE = re.compile(r"^\s*INSERT\s+(?:OR\s+IGNORE\s+)?INTO\s+live_batches\s*\(([^)]*)\)", re.I)
ARCHIVE_MARGIN = timedelta(minutes=10)          # a day is archived this long after it left the retention (late writes have settled)
CHUNK_LINES = 32768
PRESET = 6
MAGIC, IMAGIC = b"WOCOLD1\n", b"WOCIDX1\n"
TRAILER = struct.Struct("<QI8s")


def store_mode() -> str:
    """'files' (hot day files + cold archive, the default) or 'db' (WATCHOVER_LOG_STORE=db: live_batches in the main database)."""
    return "db" if (os.environ.get("WATCHOVER_LOG_STORE") or "files").strip().lower() == "db" else "files"


def _home() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data")


def hot_dir() -> Path:
    return Path(os.environ.get("WATCHOVER_LOG_HOT_DIR") or (_home() / "logs" / "hot"))


def cold_dir() -> Path:
    return Path(os.environ.get("WATCHOVER_LOG_COLD_DIR") or (_home() / "logs" / "cold"))


def day_of(iso: str) -> str:
    return f"{iso[0:4]}{iso[5:7]}{iso[8:10]}"


def day_key(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y%m%d")


def day_start(day: str) -> datetime:
    return datetime(int(day[:4]), int(day[4:6]), int(day[6:8]), tzinfo=UTC)


def day_end(day: str) -> datetime:
    return day_start(day) + timedelta(days=1)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds")


def _fsize(p: Path) -> int:
    n = 0
    for suf in ("", "-wal", "-shm"):
        try:
            n += os.path.getsize(str(p) + suf)
        except OSError:
            pass
    return n


def _connect(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    return c


# ================================================================ hot: SQLite day files
class _Reader:
    __slots__ = ("conn", "gen", "names")

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(DDL.format(t="lb0"))           # the view's first arm: answers (empty) when there is no day file yet
        self.gen = -1
        self.names: list = []


class HotStore:
    """live_batches in SQLite day files (see the module doc). Writers route each row by the UTC day of its ts_max (a future stamp
    lands in tomorrow's file at most; a day being archived is never written again: its late rows go to the arrival day) and give
    ids from one counter, so (id) stays unique over every file and the archive. Readers are a small pool of connections, each with
    the day files ATTACHed and a TEMP VIEW live_batches = UNION ALL of them, rebuilt when the file set changes."""
    hot = True
    pg = False
    pk = "INTEGER PRIMARY KEY"
    blob = "BLOB"
    backend = "sqlite-files"

    def __init__(self, path=None, readers: int = 6):
        self.dir = Path(path) if path else hot_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.files: dict = {}
        self.closed: set = set()
        self.gen = 0
        self.archive_first = False                       # LogTiers sets it: expire() leaves days past retention to the archiver
        self.pending_delete: list = []
        self._w: dict = {}
        self._pool: "queue.LifoQueue[_Reader]" = queue.LifoQueue()
        self._readers, self._made = max(1, readers), 0
        self._meta = _connect(str(self.dir / "meta.db"))
        self._meta.execute("CREATE TABLE IF NOT EXISTS tier_state (k TEXT PRIMARY KEY, v INTEGER)")
        self._meta.commit()
        for p in sorted(self.dir.glob("logs-*.db")):
            m = HOT_RE.match(p.name)
            if m:
                self.files[m.group(1)] = p
        hw = 0
        for day in list(self.files):
            try:
                r = self._writer(day).execute("SELECT MAX(id) AS m FROM live_batches").fetchone()
                hw = max(hw, int(r["m"] or 0))
            except sqlite3.Error:
                pass
        self._next_id = max(hw, self._state("id_hw")) + 1

    # ---------------------------------------------------------------- state (high-water marks survive the files they came from)
    def _state(self, k: str) -> int:
        with self.lock:
            r = self._meta.execute("SELECT v FROM tier_state WHERE k = ?", (k,)).fetchone()
        return int(r["v"] or 0) if r else 0

    def _set_state(self, k: str, v: int) -> None:
        with self.lock:
            self._meta.execute("INSERT OR REPLACE INTO tier_state (k, v) VALUES (?, ?)", (k, int(v)))
            self._meta.commit()

    def reserve(self, max_id: int = 0, max_seq: int = 0) -> None:
        """Ids and line ids above rows kept elsewhere (the old main-database table being migrated)."""
        with self.lock:
            self._next_id = max(self._next_id, int(max_id or 0) + 1)
            if max_id:
                self._set_state("id_hw", max(self._state("id_hw"), int(max_id)))
            if max_seq:
                self._set_state("seq_hw", max(self._state("seq_hw"), int(max_seq)))

    # ---------------------------------------------------------------- files
    def _path(self, day: str) -> Path:
        return self.dir / f"logs-{day}.db"

    def _writer(self, day: str) -> sqlite3.Connection:
        c = self._w.get(day)
        if c is None:
            p = self._path(day)
            c = _connect(str(p))
            c.execute(DDL.format(t="live_batches"))
            for s in INDEXES:
                c.execute(s)
            c.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER)")
            c.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('rows', 0)")
            c.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('events', 0)")
            c.commit()
            self._w[day] = c
            if day not in self.files:
                self.files[day] = p
                self.gen += 1
        return c

    def days(self) -> list:
        with self.lock:
            return sorted(self.files)

    def oldest_start(self) -> Optional[datetime]:
        d = self.days()
        return day_start(d[0]) if d else None

    def total_bytes(self) -> int:
        return sum(_fsize(p) for p in list(self.files.values()))

    def _route(self, ts_max: str, received: Optional[str], today: str, tomorrow: str) -> str:
        day = day_of(ts_max or "") if ts_max and len(ts_max) >= 10 else today
        if not day.isdigit():
            day = today
        if day > tomorrow:
            day = tomorrow
        if day in self.closed:
            rd = day_of(received) if received and len(received) >= 10 else today
            day = rd if rd.isdigit() and rd not in self.closed and rd <= tomorrow else today
        return day

    # ---------------------------------------------------------------- the Database subset
    def executemany(self, sql: str, rows: list) -> None:
        if not rows:
            return
        m = INSERT_RE.match(sql)
        if not m:
            raise NotImplementedError("HotStore: only INSERT INTO live_batches")
        cols = [c.strip() for c in m.group(1).split(",")]
        keep = "id" in cols
        if not keep:
            cols = ["id"] + cols
        i_ts, i_rc, i_n = cols.index("ts_max"), (cols.index("received_at") if "received_at" in cols else None), (cols.index("n") if "n" in cols else None)
        stmt = f"INSERT OR IGNORE INTO live_batches ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        now = datetime.now(UTC)
        today, tomorrow = day_key(now), day_key(now + timedelta(days=1))
        with self.lock:
            by_day: dict = {}
            for r in rows:
                r = tuple(r)
                if keep:
                    self._next_id = max(self._next_id, int(r[0]) + 1)
                else:
                    r = (self._next_id,) + r
                    self._next_id += 1
                by_day.setdefault(self._route(r[i_ts], r[i_rc] if i_rc is not None else None, today, tomorrow), []).append(r)
            for day, rs in by_day.items():
                c = self._writer(day)
                if keep:                                    # migration: a row copied before an interruption is skipped, not counted
                    added = ev = 0
                    for x in rs:
                        b = c.total_changes
                        c.execute(stmt, x)
                        if c.total_changes > b:
                            added += 1
                            ev += int(x[i_n] or 0) if i_n is not None else 0
                else:
                    c.executemany(stmt, rs)
                    added, ev = len(rs), (sum(int(x[i_n] or 0) for x in rs) if i_n is not None else 0)
                c.execute("UPDATE meta SET v = v + ? WHERE k = 'rows'", (added,))
                c.execute("UPDATE meta SET v = v + ? WHERE k = 'events'", (ev,))
                c.commit()

    def insert_rows(self, rows: list) -> None:
        """Rows as dicts (COLS), ids kept (migration); a row already present is skipped."""
        if rows:
            self.executemany(f"INSERT INTO live_batches ({SEL}) VALUES ({', '.join('?' * len(COLS))})",
                             [tuple(r.get(c) for c in COLS) for r in rows])

    def _insert(self, sql: str, params: tuple) -> int:
        with self.lock:
            rid = self._next_id
            self.executemany(sql, [params])
            return rid

    def _reader(self) -> _Reader:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self.lock:
            if self._made < self._readers:
                self._made += 1
                return _Reader()
        return self._pool.get(timeout=60)

    def _rebuild(self, rd: _Reader) -> None:
        with self.lock:
            gen = self.gen
            files = sorted(self.files.items(), reverse=True)     # newest first: past the attach limit the oldest days drop out
        c = rd.conn
        c.execute("DROP VIEW IF EXISTS temp.live_batches")
        for name in rd.names:
            try:
                c.execute(f"DETACH DATABASE {name}")
            except sqlite3.Error:
                pass
        rd.names = []
        for day, p in files:
            name = f"d{day}"
            try:
                c.execute(f"ATTACH DATABASE ? AS {name}", (str(p),))
                rd.names.append(name)
            except sqlite3.Error:
                break
        arms = [f"SELECT {SEL} FROM main.lb0"] + [f"SELECT {SEL} FROM {n}.live_batches" for n in rd.names]
        c.execute("CREATE TEMP VIEW live_batches AS " + " UNION ALL ".join(arms))
        rd.gen = gen

    def _exec(self, sql: str, params: tuple = ()) -> list:
        head = sql.lstrip()[:8].upper()
        if "live_batches" not in sql:                      # anything else (a test's saved searches) lives in meta.db
            with self.lock:
                cur = self._meta.execute(sql, params)
                rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                self._meta.commit()
            return rows
        if head.startswith("SELECT") or head.startswith("WITH"):
            rd = self._reader()
            try:
                if rd.gen != self.gen:
                    self._rebuild(rd)
                cur = rd.conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
            finally:
                self._pool.put(rd)
        if head.startswith("INSERT"):
            self.executemany(sql, [params])
            return []
        if head.startswith(("CREATE", "DROP", "ALTER")):
            return []                                       # the schema of a day file is fixed (DDL / INDEXES)
        raise NotImplementedError("HotStore: " + head)

    def ts_edge(self, newest: bool, before: Optional[str] = None) -> Optional[str]:
        """MIN(ts_max), or MAX(ts_max) at or before `before`, over the day files: one index probe per file (over the view the
        aggregate would read every row)."""
        rd = self._reader()
        vals = []
        try:
            if rd.gen != self.gen:
                self._rebuild(rd)
            for n in rd.names:
                if newest:
                    r = rd.conn.execute(f"SELECT MAX(ts_max) FROM {n}.live_batches" + (" WHERE ts_max <= ?" if before else ""),
                                        (before,) if before else ()).fetchone()
                else:
                    r = rd.conn.execute(f"SELECT MIN(ts_max) FROM {n}.live_batches").fetchone()
                if r and r[0]:
                    vals.append(r[0])
        finally:
            self._pool.put(rd)
        return (max(vals) if newest else min(vals)) if vals else None

    def seq_floor(self) -> Optional[int]:
        """The lowest line id held by any hot day file (an index probe per file); a lower id can only be archived."""
        rd = self._reader()
        vals = []
        try:
            if rd.gen != self.gen:
                self._rebuild(rd)
            for n in rd.names:
                r = rd.conn.execute(f"SELECT MIN(seq_min) FROM {n}.live_batches WHERE seq_min > 0").fetchone()
                if r and r[0]:
                    vals.append(int(r[0]))
        finally:
            self._pool.put(rd)
        return min(vals) if vals else None

    def columns(self, table: str) -> list:
        return list(COLS) if table == "live_batches" else [r["name"] for r in self._exec(f"PRAGMA table_info({table})")]

    def tables(self) -> list:
        return ["live_batches"]

    def size_bytes(self) -> int:
        return self.total_bytes()

    def last_seq(self) -> int:
        hw = self._state("seq_hw")
        for day in self.days():
            try:
                with self.lock:
                    rows = self._writer(day).execute("SELECT seq_max FROM live_batches ORDER BY seq_min DESC LIMIT 64").fetchall()
                hw = max([hw] + [int(r["seq_max"] or 0) for r in rows])
            except sqlite3.Error:
                pass
        return hw

    # ---------------------------------------------------------------- expiry
    def close_day(self, day: str) -> Optional[Path]:
        """No more writes to `day` (its late rows go to the arrival day); the writer is checkpointed and closed."""
        with self.lock:
            p = self.files.get(day)
            if p is None:
                return None
            self.closed.add(day)
            c = self._w.pop(day, None)
            if c is not None:
                try:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                c.close()
            return p

    def drop_day(self, day: str) -> int:
        """Forget and delete a day file (after it was archived, or past retention without an archive); its id and line-id
        high-water marks stay in meta.db. Returns its row count."""
        p = self.close_day(day)
        if p is None:
            return 0
        rows = 0
        try:
            c = sqlite3.connect(str(p))
            r = c.execute("SELECT MAX(id), (SELECT v FROM meta WHERE k = 'rows') FROM live_batches").fetchone()
            q = c.execute("SELECT seq_max FROM live_batches ORDER BY seq_min DESC LIMIT 64").fetchall()
            c.close()
            rows = int(r[1] or 0)
            with self.lock:
                if r[0]:
                    self._set_state("id_hw", max(self._state("id_hw"), int(r[0])))
                if q:
                    self._set_state("seq_hw", max([self._state("seq_hw")] + [int(x[0] or 0) for x in q]))
        except sqlite3.Error:
            pass
        with self.lock:
            self.files.pop(day, None)
            self.closed.discard(day)
            self.gen += 1
            self.pending_delete.append(p)
        self.retry_deletes()
        return rows

    def retry_deletes(self) -> None:
        with self.lock:
            left = []
            for p in self.pending_delete:
                try:
                    for suf in ("-wal", "-shm", ""):
                        if os.path.exists(str(p) + suf):
                            os.remove(str(p) + suf)
                except OSError:                              # Windows: a reader still has it open; the next tick retries
                    left.append(p)
            self.pending_delete = left

    def expire(self, cut: datetime) -> int:
        """Delete day files wholly older than `cut` (LiveStore.prune) unless the archiver owns them."""
        if self.archive_first:
            return 0
        n = 0
        for day in self.days():
            if day_end(day) <= cut:
                n += self.drop_day(day)
        return n

    def purge_agent(self, agent: str, dry_run: bool = False) -> int:
        """Lines a sender stored (demo clean-up: the simulator), counted and, unless dry_run, deleted from every open day file
        (by the agent index; the freed pages are reused by later rows). A day being archived is left to the archive's purge."""
        n = 0
        for day in self.days():
            with self.lock:
                if day in self.closed or day not in self.files:
                    continue
                c = self._writer(day)
                rows, ev = c.execute("SELECT COUNT(*), COALESCE(SUM(n), 0) FROM live_batches WHERE agent = ?", (agent,)).fetchone()
                n += int(ev or 0)
                if rows and not dry_run:
                    c.execute("DELETE FROM live_batches WHERE agent = ?", (agent,))
                    c.execute("UPDATE meta SET v = MAX(0, v - ?) WHERE k = 'rows'", (int(rows),))
                    c.execute("UPDATE meta SET v = MAX(0, v - ?) WHERE k = 'events'", (int(ev or 0),))
                    c.commit()
        return n

    def stats(self) -> dict:
        days, events, rows, oldest, newest = [], 0, 0, "", ""
        for day in self.days():
            p = self.files.get(day)
            if p is None:
                continue
            d = {"day": day, "bytes": _fsize(p), "rows": 0, "events": 0}
            try:
                with self.lock:
                    c = self._writer(day) if day not in self.closed else sqlite3.connect(str(p))
                    for r in c.execute("SELECT k, v FROM meta").fetchall():
                        d[r[0]] = int(r[1] or 0)
                    lo = c.execute("SELECT MIN(ts_max) FROM live_batches").fetchone()[0] or ""
                    hi = c.execute("SELECT MAX(ts_max) FROM live_batches").fetchone()[0] or ""
                    if day in self.closed:
                        c.close()
                oldest = min(oldest, lo) if oldest and lo else (oldest or lo)
                newest = max(newest, hi)
            except sqlite3.Error:
                pass
            days.append(d)
            events += d["events"]
            rows += d["rows"]
        b = sum(d["bytes"] for d in days)
        return {"enabled": True, "batches": rows, "events": events, "oldest": oldest, "newest": newest, "bytes": b, "days": days,
                "bytes_per_line": round(b / events, 1) if events else None, "dir": str(self.dir)}

    def close(self) -> None:
        with self.lock:
            for c in list(self._w.values()):
                try:
                    c.commit()
                    c.close()
                except sqlite3.Error:
                    pass
            self._w.clear()
            while True:
                try:
                    self._pool.get_nowait().conn.close()
                except queue.Empty:
                    break
            try:
                self._meta.close()
            except sqlite3.Error:
                pass


# ================================================================ cold: chunk codec
_DIG = re.compile(rb"\d+")
_TSH = re.compile(rb'^\{"ts":"([0-9T:.+\-]{10,40})"')
_QF = re.compile(rb',"q":([1-9]\d{0,17})(?=[,}])')
_CTRL = re.compile(rb"[\x00-\x04]")
EPOCH = datetime(1970, 1, 1)
US = timedelta(microseconds=1)


def _uv(out: bytearray, v: int) -> None:
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def _zz(v: int) -> int:
    return v * 2 if v >= 0 else -v * 2 - 1


def _unzz(u: int) -> int:
    return u >> 1 if not u & 1 else -((u + 1) >> 1)


def _uvs(b: bytes) -> list:
    out, v, sh = [], 0, 0
    for x in b:
        v |= (x & 0x7F) << sh
        if x & 0x80:
            sh += 7
        else:
            out.append(v)
            v, sh = 0, 0
    return out


def _sections(data: bytes) -> list:
    out, i, n = [], 0, len(data)
    while i < n:
        v, sh = 0, 0
        while True:
            x = data[i]
            i += 1
            v |= (x & 0x7F) << sh
            if not x & 0x80:
                break
            sh += 7
        out.append(data[i:i + v])
        i += v
    return out


def _pack_sections(parts: list) -> bytes:
    out = bytearray()
    for p in parts:
        _uv(out, len(p))
        out += p
    return bytes(out)


def _split_line(ln: bytes):
    """(skeleton, digit runs, wall-clock µs or None, line id or None) of one stored JSON line; None when it holds a byte the
    skeleton uses as a marker (then it is kept literally)."""
    if _CTRL.search(ln):
        return None
    head, rest, ts, q = b"", ln, None, None
    m = _TSH.match(ln)
    if m:
        s = m.group(1).decode("ascii")
        try:
            d = datetime.fromisoformat(s)
            wall = d.replace(tzinfo=None)
            w = wall.isoformat()
            if d.isoformat() == s and s.startswith(w):
                ts = (wall - EPOCH) // US
                head = b'{"ts":"\x03' + s[len(w):].encode("ascii") + b'"'
                rest = ln[m.end():]
        except ValueError:
            pass
    m = _QF.search(rest)
    if m:
        q = int(m.group(1))
        rest = rest[:m.start()] + b',"q":\x04' + rest[m.end():]
    parts, vs, seen, last = [head], [], {}, 0
    for mm in _DIG.finditer(rest):
        t = mm.group(0)
        parts.append(rest[last:mm.start()])
        last = mm.end()
        if len(t) >= 3:
            j = seen.get(t)
            if j is not None:
                parts.append(b"\x02" + bytes((j,)))
                continue
            if len(vs) < 250:
                seen[t] = len(vs)
        parts.append(b"\x01")
        vs.append(t)
    parts.append(rest[last:])
    return b"".join(parts), vs, ts, q


def encode_chunk(rows: list, preset: int = PRESET) -> tuple:
    """(lzma blob, lines, text bytes) for rows (dicts with COLS and `_text`, the row's JSONL as stored)."""
    meta, tpl, tids, tsb, qb, lits = [], {}, bytearray(), bytearray(), bytearray(), bytearray()
    cols: dict = {}
    pts = pq = 0
    nlines = tbytes = 0
    for r in rows:
        text = r["_text"]
        tbytes += len(text)
        lines = text.split(b"\n")
        meta.append([r.get("id"), r.get("ts_min"), r.get("ts_max"), r.get("received_at"), r.get("agent") or "", r.get("env") or "",
                     int(r.get("n") or 0), int(r.get("v") or 2), int(r.get("seq_min") or 0), int(r.get("seq_max") or 0),
                     int(r.get("sevmask") if r.get("sevmask") is not None else 31), r.get("host") or "", r.get("src") or "", len(lines)])
        for ln in lines:
            nlines += 1
            sp = _split_line(ln)
            if sp is None:
                _uv(tids, 0)
                _uv(lits, len(ln))
                lits += ln
                continue
            sk, vs, ts, q = sp
            tid = tpl.get(sk)
            if tid is None:
                tid = tpl[sk] = len(tpl) + 1
            _uv(tids, tid)
            if ts is not None:
                _uv(tsb, _zz(ts - pts))
                pts = ts
            if q is not None:
                _uv(qb, _zz(q - pq))
                pq = q
            for k, v in enumerate(vs):
                c = cols.get((tid, k))
                if c is None:
                    c = cols[(tid, k)] = []
                c.append(v)
    tb = bytearray()
    for sk in tpl:
        _uv(tb, len(sk))
        tb += sk
    parts = [json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), bytes(tb), bytes(tids), bytes(tsb), bytes(qb), bytes(lits)]
    for key in sorted(cols):
        parts.append(b"\x00".join(cols[key]))
    return lzma.compress(_pack_sections(parts), preset=preset), nlines, tbytes


_PLAN_CACHE: dict = {}


def _plan(sk: bytes) -> tuple:
    """(format, number of digit runs, getter or None, has stamp, has line id): the skeleton as a bytes %-format whose arguments
    are the line's runs, then its stamp, then its line id; back references pick a run twice."""
    hit = _PLAN_CACHE.get(sk)
    if hit is not None:
        return hit
    fmt, order, nv, has_ts, has_q, i = bytearray(), [], 0, False, False, 0
    marks = []
    while i < len(sk):
        c = sk[i]
        if c == 1:
            fmt += b"%s"
            marks.append(("v", nv))
            nv += 1
        elif c == 2:
            i += 1
            fmt += b"%s"
            marks.append(("v", sk[i]))
        elif c == 3:
            fmt += b"%s"
            marks.append(("t", 0))
            has_ts = True
        elif c == 4:
            fmt += b"%s"
            marks.append(("q", 0))
            has_q = True
        elif c == 37:
            fmt += b"%%"
        else:
            fmt.append(c)
        i += 1
    for kind, j in marks:
        order.append(j if kind == "v" else nv if kind == "t" else nv + (1 if has_ts else 0))
    width = nv + has_ts + has_q
    if order == list(range(width)):
        getter = None
    elif len(order) == 1:
        o = order[0]
        getter = lambda x, o=o: (x[o],)   # noqa: E731
    else:
        getter = itemgetter(*order)
    if len(_PLAN_CACHE) > 50_000:
        _PLAN_CACHE.clear()
    out = _PLAN_CACHE[sk] = (bytes(fmt), nv, getter, has_ts, has_q, width)
    return out


def _ts_text(us: int) -> bytes:
    return (EPOCH + timedelta(microseconds=us)).isoformat().encode("ascii")


_NONDIG = re.compile(rb"\d+")
_LONGDIG = re.compile(rb"\d{7,}")


def chunk_may_hold(sec: list, lit: bytes, fold: tuple = ()) -> bool:
    """False when no line of the chunk can contain `lit` (a prefilter literal, ASCII-lowercased and Turkish-folded): a run of
    non-digits of a line lies inside one piece of its template, a run of 7+ digits inside one digit column value or line id (a
    stamp's runs are 6 digits at most). Necessary, never sufficient: a True chunk is decoded and filtered line by line."""
    if sec[5]:
        t = sec[5].lower()
        for a, k in fold:
            t = t.replace(a, k)
        if lit in t:
            return True
    nd = [x for x in _NONDIG.split(lit) if len(x) >= 2]
    dg = _LONGDIG.findall(lit)
    if nd:
        tb = sec[1].lower()
        if fold and not tb.isascii():
            for a, k in fold:
                tb = tb.replace(a, k)
        if any(x not in tb for x in nd):
            return False
    if dg:
        vb = b"\x00".join(sec[6:])
        if any(x not in vb for x in dg):
            q, qs = 0, []
            for u in _uvs(sec[4]):
                q += _unzz(u)
                qs.append(b"%d" % q)
            qb = b"\x00".join(qs)
            if any(x not in vb and x not in qb for x in dg):
                return False
    return True


def decode_chunk(blob: bytes) -> list:
    """The rows of a chunk: dicts with COLS (pz None, payload '') and `_text`, the row's JSONL exactly as it was stored."""
    return decode_sections(_sections(lzma.decompress(blob)))


def decode_sections(sec: list) -> list:
    meta = json.loads(sec[0])
    tb, tpls, i = sec[1], [None], 0
    while i < len(tb):
        v, sh = 0, 0
        while True:
            x = tb[i]
            i += 1
            v |= (x & 0x7F) << sh
            if not x & 0x80:
                break
            sh += 7
        tpls.append(tb[i:i + v])
        i += v
    tids = _uvs(sec[2])
    tsd, qd = iter(_uvs(sec[3])), iter(_uvs(sec[4]))
    plans = [None] + [_plan(sk) for sk in tpls[1:]]
    per: dict = {}
    pts = pq = 0
    for t in tids:                                       # stamps and line ids are deltas in line order: split them per template
        if t:
            p = plans[t]
            lst = per.get(t)
            if lst is None:
                lst = per[t] = ([], [])
            if p[3]:
                pts += _unzz(next(tsd))
                lst[0].append(pts)
            if p[4]:
                pq += _unzz(next(qd))
                lst[1].append(b"%d" % pq)
    ci = 6
    out_t: dict = {}
    for t in sorted(per):
        fmt, nv, getter, has_ts, has_q, _w = plans[t]
        ts_l, q_l = per[t]
        cols = [sec[ci + k].split(b"\x00") for k in range(nv)]
        ci += nv
        if has_ts:
            cols.append([_ts_text(u) for u in ts_l])
        if has_q:
            cols.append(q_l)
        if not cols:
            n = sum(1 for x in tids if x == t)
            out_t[t] = iter([fmt % ()] * n)
        elif getter is None:
            out_t[t] = iter([fmt % x for x in zip(*cols)])
        else:
            out_t[t] = iter([fmt % getter(x) for x in zip(*cols)])
    lb, lits, i = sec[5], [], 0
    while i < len(lb):
        v, sh = 0, 0
        while True:
            x = lb[i]
            i += 1
            v |= (x & 0x7F) << sh
            if not x & 0x80:
                break
            sh += 7
        lits.append(lb[i:i + v])
        i += v
    out_t[0] = iter(lits)
    lines = [next(out_t[t]) for t in tids]
    rows, pos = [], 0
    for m in meta:
        k = m[13]
        rows.append({"id": m[0], "ts_min": m[1], "ts_max": m[2], "received_at": m[3], "agent": m[4], "env": m[5], "n": m[6], "payload": "",
                     "v": m[7], "pz": None, "seq_min": m[8], "seq_max": m[9], "sevmask": m[10], "host": m[11], "src": m[12],
                     "_text": b"\n".join(lines[pos:pos + k])})
        pos += k
    return rows


# ================================================================ cold: day archives
def _sort_key(r: dict) -> tuple:
    return (r.get("ts_max") or "", r.get("ts_min") or "", int(r.get("id") or 0))


def _merge(*streams: Iterable) -> Iterator[dict]:
    """Rows of several (ts_max, ts_min, id)-ordered streams in that order, each row id once."""
    last = None
    for r in heapq.merge(*streams, key=_sort_key):
        k = int(r.get("id") or 0)
        if last is not None and k == last[0] and _sort_key(r) == last[1]:
            continue
        last = (k, _sort_key(r))
        yield r


def write_day(cdir: Path, day: str, rows: Iterable, chunk_lines: int = CHUNK_LINES, preset: int = PRESET, _fail_after: Optional[int] = None,
              merge: bool = True) -> dict:
    """Write the archive of `day` from (ts_max, ts_min, id)-ordered rows, merged with the archive already there (merge=False:
    replaced, the rows are its new content): a temp file, fsync, rename, fsync of the directory. `_fail_after` (tests) raises
    after that many chunks: a crash mid-archive."""
    cdir.mkdir(parents=True, exist_ok=True)
    final = cdir / f"logs-{day}.wcl"
    tmp = cdir / f"logs-{day}.wcl.tmp"
    t0 = time.perf_counter()
    old = ColdStore(cdir).iter_day(day) if merge and final.exists() else iter(())
    chunks, buf, nbuf, agents = [], [], 0, set()
    tot = {"rows": 0, "lines": 0, "text_bytes": 0}
    with open(tmp, "wb") as f:
        f.write(MAGIC)

        def flush():
            if not buf:
                return
            if _fail_after is not None and len(chunks) >= _fail_after:
                raise RuntimeError("simulated crash")
            blob, nl, tb = encode_chunk(buf, preset)
            off = f.tell()
            f.write(blob)
            qs = [(int(r.get("seq_min") or 0), int(r.get("seq_max") or 0)) for r in buf if r.get("seq_min")]
            mask = 0
            for r in buf:
                mask |= int(r.get("sevmask") if r.get("sevmask") is not None else 31)
            chunks.append({"o": off, "l": len(blob), "n": nl, "r": len(buf), "t0": min(r.get("ts_min") or "" for r in buf),
                           "t1": max(r.get("ts_max") or "" for r in buf), "m0": buf[0].get("ts_max") or "",
                           "i0": min(int(r["id"]) for r in buf), "i1": max(int(r["id"]) for r in buf),
                           "q0": min((a for a, _b in qs), default=0), "q1": max((b for _a, b in qs), default=0), "sev": mask})
            tot["rows"] += len(buf)
            tot["lines"] += nl
            tot["text_bytes"] += tb
            buf.clear()

        for r in _merge(old, rows):
            if r.get("_text") is None:
                from .logstore import row_bytes
                r = dict(r)
                r["_text"] = row_bytes(r)
            buf.append(r)
            agents.add(r.get("agent") or "")
            nbuf += r["_text"].count(b"\n") + 1
            if nbuf >= chunk_lines:
                flush()
                nbuf = 0
        flush()
        index = {"v": 1, "day": day, "chunks": chunks, **tot, "created": _iso(datetime.now(UTC)),
                 "t0": min((c["t0"] for c in chunks), default=""), "t1": max((c["t1"] for c in chunks), default=""),
                 "id_max": max((c["i1"] for c in chunks), default=0), "seq_max": max((c["q1"] for c in chunks), default=0),
                 "agents": sorted(agents)}
        ib = lzma.compress(json.dumps(index, separators=(",", ":")).encode("utf-8"), preset=6)
        off = f.tell()
        f.write(ib)
        f.write(TRAILER.pack(off, len(ib), IMAGIC))
        f.flush()
        os.fsync(f.fileno())
    if final.exists():
        logaudit.unseal(final)                             # Windows cannot replace a read-only file (POSIX renames over it anyway)
    os.replace(tmp, final)
    logaudit.seal(final)                                   # sealed: 0440, never written in place again
    try:
        fd = os.open(str(cdir), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:                                        # Windows: a directory cannot be opened; the rename is already durable
        pass
    secs = time.perf_counter() - t0
    size = final.stat().st_size
    return {"day": day, **tot, "bytes": size, "bytes_per_line": round(size / tot["lines"], 2) if tot["lines"] else None,
            "seconds": round(secs, 2), "lines_per_s": int(tot["lines"] / secs) if secs > 0 else None}


def hot_rows(path: Path) -> Iterator[dict]:
    """Every row of a hot day file in (ts_max, ts_min, id) order, its JSONL text decoded once."""
    from .logstore import row_bytes
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    try:
        cur = c.execute(f"SELECT {SEL} FROM live_batches ORDER BY ts_max, ts_min, id")
        while True:
            got = cur.fetchmany(500)
            if not got:
                break
            for r in got:
                d = dict(r)
                d["_text"] = row_bytes(d)
                d["pz"] = None
                d["payload"] = ""
                yield d
    finally:
        c.close()


def archive_file(hot_path, cdir, day: str, chunk_lines: int = CHUNK_LINES, preset: int = PRESET, _fail_after: Optional[int] = None) -> dict:
    return write_day(Path(cdir), day, hot_rows(Path(hot_path)), chunk_lines, preset, _fail_after)


class ColdStore:
    """Reads the day archives: indexes and decoded chunks are cached (a chunk by file identity, so a re-written day is never
    served stale). Rows come out like live_batches rows plus `_text` and `_cold` (the day)."""

    def __init__(self, path=None, cache_mb: Optional[int] = None):
        self.dir = Path(path) if path else cold_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._idx: dict = {}
        self._chunks: "OrderedDict" = OrderedDict()
        self._cached = 0
        mb = cache_mb if cache_mb is not None else int(os.environ.get("WATCHOVER_COLD_CACHE_MB", "96") or 96)
        self.cache_max = max(0, mb) * 1024 * 1024
        self.decoded = 0                                   # chunks decoded (not served from the cache)
        self.skipped = 0                                   # chunks a search literal ruled out without decoding their lines
        self.manifest = logaudit.Manifest(self.dir)        # the hash-chained record of every archive (logaudit.py)
        self.on_manifest = None                            # LogTiers: keep the last seq on the hot disk too (truncation check)

    def path(self, day: str) -> Path:
        return self.dir / f"logs-{day}.wcl"

    def days(self) -> list:
        out = []
        try:
            for p in self.dir.iterdir():
                m = COLD_RE.match(p.name)
                if m:
                    out.append(m.group(1))
        except OSError:
            pass
        return sorted(out)

    def oldest_start(self) -> Optional[datetime]:
        d = self.days()
        return day_start(d[0]) if d else None

    def total_bytes(self) -> int:
        n = 0
        for d in self.days():
            try:
                n += self.path(d).stat().st_size
            except OSError:
                pass
        return n

    def index(self, day: str) -> Optional[dict]:
        p = self.path(day)
        try:
            st = p.stat()
        except OSError:
            return None
        ident = (st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self._idx.get(day)
            if hit and hit[0] == ident:
                return hit[1]
        try:
            with open(p, "rb") as f:
                if f.read(len(MAGIC)) != MAGIC:
                    return None
                f.seek(-TRAILER.size, os.SEEK_END)
                off, ln, mg = TRAILER.unpack(f.read(TRAILER.size))
                if mg != IMAGIC:
                    return None
                f.seek(off)
                ix = json.loads(lzma.decompress(f.read(ln)))
        except (OSError, ValueError, lzma.LZMAError, struct.error):
            return None
        ix["_ident"] = ident
        with self.lock:
            self._idx[day] = (ident, ix)
        return ix

    def chunk(self, day: str, ix: dict, ch: dict, pre=None) -> Optional[list]:
        """The decoded rows of a chunk (cached); None when `pre` (a logquery prefilter) cannot match any of its lines."""
        key = (day, ix.get("_ident"), ch["o"])
        with self.lock:
            hit = self._chunks.get(key)
            if hit is not None:
                self._chunks.move_to_end(key)
                return hit[0]
        with open(self.path(day), "rb") as f:
            f.seek(ch["o"])
            sec = _sections(lzma.decompress(f.read(ch["l"])))
        if pre is not None and not chunk_may_hold(sec, pre.lit, getattr(pre, "fold", ())):
            with self.lock:
                self.skipped += 1
            return None
        rows = decode_sections(sec)
        for r in rows:
            r["_cold"] = day
        size = sum(len(r["_text"]) for r in rows) + 200 * len(rows)
        with self.lock:
            self.decoded += 1
            if self.cache_max:
                self._chunks[key] = (rows, size)
                self._cached += size
                while self._cached > self.cache_max and len(self._chunks) > 1:
                    _k, (_r, sz) = self._chunks.popitem(last=False)
                    self._cached -= sz
        return rows

    def iter_day(self, day: str) -> Iterator[dict]:
        """Every row of a day archive in its stored (ts_max, ts_min, id) order, uncached (re-archiving merges with it)."""
        ix = self.index(day)
        if not ix:
            return
        with open(self.path(day), "rb") as f:
            for ch in ix["chunks"]:
                f.seek(ch["o"])
                for r in decode_chunk(f.read(ch["l"])):
                    yield r

    def _days_desc(self, skip=None, days=None) -> list:
        """Archived days newest first, without `skip` (the days a hot file still holds: hot is authoritative for its days)."""
        return [d for d in reversed(self.days()) if (not skip or d not in skip) and (days is None or d in days)]

    def rows_desc(self, s: datetime, T: datetime, lt: str = "<", skip=None, prune=None, days=None, pre=None) -> Iterator[dict]:
        """Rows with ts_max >= s and ts_min < T (<= when lt is '<='), newest ts_max first, from the archived days (not in `skip`,
        only `days` when given). prune(chunk index entry) -> True skips a chunk without reading it (severity mask); a chunk the
        search literal `pre` rules out yields one placeholder row marked _skip (the reader moves its frontier and budget on)."""
        s_iso, T_iso = _iso(s), _iso(T)
        for day in self._days_desc(skip, days):
            ix = self.index(day)
            if not ix or not ix["chunks"] or ix["t1"] < s_iso:
                continue
            for ch in reversed(ix["chunks"]):
                if ch["t1"] < s_iso or (ch["t0"] >= T_iso if lt == "<" else ch["t0"] > T_iso):
                    continue
                if prune is not None and prune(ch):
                    continue
                rows = self.chunk(day, ix, ch, pre)
                if rows is None:
                    yield {"id": ch["i0"], "ts_min": ch["t0"], "ts_max": max(ch["m0"], s_iso), "received_at": "", "agent": "", "env": "", "n": 0,
                           "payload": "", "v": 2, "pz": None, "seq_min": 0, "seq_max": 0, "sevmask": ch["sev"], "host": "", "src": "",
                           "_text": b"", "_skip": True, "_cold": day}
                    continue
                sel = [r for r in rows
                       if r["ts_max"] >= s_iso and (r["ts_min"] < T_iso if lt == "<" else r["ts_min"] <= T_iso)]
                sel.sort(key=_sort_key, reverse=True)
                for r in sel:
                    yield dict(r)

    def lines_between(self, s: datetime, e: datetime, skip=None) -> float:
        """Archived lines stamped in [s, e) by the chunk index (a chunk partly inside counts in proportion of its time span)."""
        s_iso, e_iso, n = _iso(s), _iso(e), 0.0
        for day in self._days_desc(skip):
            ix = self.index(day)
            if not ix or ix["t1"] < s_iso or ix["t0"] >= e_iso:
                continue
            for ch in ix["chunks"]:
                if ch["t1"] < s_iso or ch["t0"] >= e_iso:
                    continue
                if ch["t0"] >= s_iso and ch["t1"] < e_iso:
                    n += ch["n"]
                    continue
                a, b = datetime.fromisoformat(ch["t0"]), datetime.fromisoformat(ch["t1"]) + timedelta(seconds=1)
                span = (b - a).total_seconds()
                part = (min(b, e) - max(a, s)).total_seconds()
                n += ch["n"] * max(0.0, min(1.0, part / span)) if span > 0 else ch["n"]
        return n

    def find_seq(self, lid: int, skip=None) -> list:
        """Rows whose line-id range holds `lid`."""
        out = []
        for day in self._days_desc(skip):
            ix = self.index(day)
            if not ix or not (0 < lid <= ix.get("seq_max", 0)):
                continue
            for ch in ix["chunks"]:
                if ch["q0"] <= lid <= ch["q1"]:
                    out += [dict(r) for r in self.chunk(day, ix, ch) if r["seq_min"] and r["seq_min"] <= lid <= r["seq_max"]]
            if out:
                break
        return out

    def find_id(self, rid: int, skip=None) -> list:
        for day in self._days_desc(skip):
            ix = self.index(day)
            if not ix:
                continue
            for ch in ix["chunks"]:
                if ch["i0"] <= rid <= ch["i1"]:
                    got = [dict(r) for r in self.chunk(day, ix, ch) if int(r["id"]) == rid]
                    if got:
                        return got
        return []

    def rows_agent(self, agent: str, lo: datetime, hi: datetime, skip=None) -> list:
        """Rows of one agent with ts_max >= lo and ts_min <= hi (the context window), oldest first."""
        out = [r for r in self.rows_desc(lo, hi, "<=", skip) if (r.get("agent") or "") == agent]
        out.reverse()
        return out

    def purge_agent(self, agent: str, dry_run: bool = False) -> int:
        """Lines a sender stored in the archive (demo clean-up), counted and, unless dry_run, removed by rewriting each day that
        holds some (temp file + rename, like an archive run; a day left empty is deleted). Days whose index lists its senders
        and not this one are not read."""
        n = 0
        for day in self.days():
            ix = self.index(day)
            if not ix or ("agents" in ix and agent not in ix["agents"]):
                continue
            k = sum(int(r.get("n") or 0) for r in self.iter_day(day) if (r.get("agent") or "") == agent)
            if not k:
                continue
            n += k
            if dry_run:
                continue
            if all((r.get("agent") or "") == agent for r in self.iter_day(day)):
                self.delete_day(day, "remove", f"demo clean-up: every line was the simulator's ({agent})")
            else:
                write_day(self.dir, day, (r for r in self.iter_day(day) if (r.get("agent") or "") != agent), merge=False)
                self.seal_day(day, f"demo clean-up: the simulator's lines ({agent}) removed")
        return n

    def seal_day(self, day: str, note: str = "", op: Optional[str] = None) -> Optional[dict]:
        """Enter the archive of `day` in the manifest as it is now (seal, or reseal when an entry already describes it) and keep it
        read-only. Returns the entry."""
        p = self.path(day)
        if not p.exists():
            return None
        logaudit.seal(p)
        op = op or ("reseal" if day in self.manifest.active() else "seal")
        e = self.manifest.append(op, day, logaudit.archive_info(self, day), note)
        if self.on_manifest is not None:
            self.on_manifest(e)
        return e

    def delete_day(self, day: str, op: str = "remove", note: str = "") -> None:
        p = self.path(day)
        known = self.manifest.active().get(day)
        try:
            logaudit.unseal(p)
            p.unlink()
        except OSError:
            pass
        else:
            if known is not None:                          # the manifest says why the archive is gone (verify does not call it missing)
                e = self.manifest.append(op, day, {k: known.get(k) for k in ("sha256", "bytes", "lines", "rows", "first", "last")}, note)
                if self.on_manifest is not None:
                    self.on_manifest(e)
        with self.lock:
            self._idx.pop(day, None)
            for k in [k for k in self._chunks if k[0] == day]:
                self._cached -= self._chunks.pop(k)[1]

    def cleanup_tmp(self, older_s: float = 0) -> None:
        now = time.time()
        for p in self.dir.glob("*.wcl.tmp"):
            try:
                if now - p.stat().st_mtime >= older_s:
                    p.unlink()
            except OSError:
                pass

    def stats(self) -> dict:
        days, lines, b = [], 0, 0
        for d in self.days():
            ix = self.index(d) or {}
            try:
                size = self.path(d).stat().st_size
            except OSError:
                continue
            n = int(ix.get("lines") or 0)
            days.append({"day": d, "bytes": size, "lines": n, "rows": int(ix.get("rows") or 0), "text_bytes": int(ix.get("text_bytes") or 0),
                         "oldest": ix.get("t0") or "", "newest": ix.get("t1") or "", "ok": bool(ix)})
            lines += n
            b += size
        return {"bytes": b, "lines": lines, "days": days, "oldest": days[0]["oldest"] if days else "", "newest": days[-1]["newest"] if days else "",
                "bytes_per_line": round(b / lines, 2) if lines else None, "dir": str(self.dir)}


# ================================================================ the job and the report
class LogTiers:
    """The hourly tier job (archive, caps, retention), the one-off migration from the main database, and the storage report."""

    def __init__(self, hot: Optional[HotStore], cold: Optional[ColdStore], settings_fn=None, subprocess_ok: bool = True, every_s: float = 3600.0):
        self.hot, self.cold = hot, cold
        self.settings_fn = settings_fn or (lambda: {})
        self.subprocess_ok = subprocess_ok
        self.every_s = every_s
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.last_run: dict = {}
        self.archived: list = []
        self.migration: dict = {"state": "none"}
        self._thread: Optional[threading.Thread] = None
        self.paused: Optional[dict] = None                 # archiving held back: the archive disk cannot take the next day
        self.check: Optional[dict] = None                  # the last hourly manifest check (sizes, chain; no re-hash)
        self.audit_ok = True                               # False when the archive cannot be audited (manifest not writable)
        self.on_tick = None                                # the app re-reads its retention (a CLI may have changed config.json)
        if hot is not None:                                # before the first tick too: prune must not delete what is not archived
            hot.archive_first = cold is not None and self.cfg()["cold_days"] > 0
        if cold is not None and hot is not None:
            cold.on_manifest = lambda e: hot._set_state("manifest_seq", int(e.get("seq") or 0))

    def cfg(self) -> dict:
        try:
            c = self.settings_fn() or {}
        except Exception:  # noqa: BLE001
            c = {}

        def num(k: str, d: float) -> float:
            try:
                v = c.get(k, d)
                return float(d if v is None or v == "" else v)
            except (TypeError, ValueError):
                return float(d)
        return {"retention_h": num("live_retention_h", 48) or 48.0, "cold_days": max(0.0, num("cold_retention_days", 365)),
                "cold_max": max(0.0, num("cold_max_gb", 0)) * GB, "hot_max": max(0.0, num("hot_max_gb", 0)) * GB}

    # ---------------------------------------------------------------- archive one day
    def archive(self, path: Path, day: str) -> dict:
        if self.subprocess_ok:
            env = dict(os.environ)
            root = str(Path(__file__).resolve().parents[1])
            env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            try:
                r = subprocess.run([sys.executable, "-m", "watchover.logtiers", "archive", str(path), str(self.cold.dir), day],
                                   capture_output=True, timeout=12 * 3600, env=env)
            except (OSError, subprocess.SubprocessError):
                r = None
            if r is not None and r.returncode == 0:
                try:
                    return json.loads(r.stdout.decode().strip().splitlines()[-1])
                except (ValueError, IndexError):
                    pass                                   # no report: archive here (the merge by id makes a re-run harmless)
        return archive_file(path, self.cold.dir, day)      # no child process (or it failed): same code in this thread

    def anchor(self) -> int:
        """The manifest's last seq as the hot disk remembers it (0 when unknown)."""
        try:
            return self.hot._state("manifest_seq") if self.hot is not None else 0
        except Exception:  # noqa: BLE001
            return 0

    def _room(self, day: str) -> Optional[dict]:
        """None when the archive disk can take `day`, else why not: the estimated archive (its lines x the measured bytes per
        line x 1.5, + 64 MB) does not fit in the free space minus a reserve (1 GB or 1 % of the disk)."""
        try:
            du = shutil.disk_usage(str(self.cold.dir))
        except OSError:
            return None
        events = 0
        p = self.hot.files.get(day)
        if p is not None:
            try:
                c = sqlite3.connect(str(p))
                r = c.execute("SELECT v FROM meta WHERE k = 'events'").fetchone()
                c.close()
                events = int(r[0] or 0) if r else 0
            except sqlite3.Error:
                events = 0
            if not events:
                events = _fsize(p) // 60                   # ~60+ bytes per line in a hot day file
        cs = self.cold.stats() if events else {}
        bpl = cs.get("bytes_per_line") if (cs.get("lines") or 0) >= 100_000 else None
        need = int(events * float(bpl or logaudit.LINE_BYTES) * 1.5) + (64 << 20)
        reserve = max(1 << 30, du.total // 100)
        if du.free - need < reserve:
            return {"day": day, "need": need, "free": du.free, "reserve": reserve, "since": (self.paused or {}).get("since") or _iso(datetime.now(UTC))}
        return None

    def _retire(self, day: str, cold_on: bool, out: dict, reason: str) -> bool:
        if cold_on:
            full = self._room(day)
            if full is not None:                           # no silent loss: the day stays hot, archiving waits for room (alert)
                self.paused = full
                out["paused"] = full
                return False
        p = self.hot.close_day(day)
        if p is None:
            return False
        if cold_on:
            try:
                st = self.archive(p, day)
            except Exception as e:  # noqa: BLE001 - kept closed and hot: the next tick retries
                if isinstance(e, OSError) and getattr(e, "errno", None) == 28:     # ENOSPC: the estimate was short
                    self.paused = {"day": day, "need": 0, "free": 0, "since": _iso(datetime.now(UTC)), "error": "no space left"}
                    out["paused"] = self.paused
                    self.cold.cleanup_tmp()                # the half-written temp file gives its space back at once
                out["errors"].append({"day": day, "error": str(e)[:300]})
                return False
            try:
                e = self.cold.seal_day(day, "")
                st["seq"], st["sha256"] = (e or {}).get("seq"), (e or {}).get("sha256")
            except OSError as e:                           # archived, but the manifest is not writable: keep the day hot, say so
                self.audit_ok = False
                out["errors"].append({"day": day, "error": f"manifest: {str(e)[:250]}"})
                return False
            self.audit_ok = True
            self.paused = None
            st["reason"] = reason
            out["archived"].append(st)
            self.archived = (self.archived + [st])[-14:]
        self.hot.drop_day(day)
        out["dropped_hot"].append(day)
        return True

    def tick(self, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(UTC)
        out: dict = {"archived": [], "dropped_hot": [], "deleted_cold": [], "errors": []}
        if self.on_tick is not None:
            try:
                self.on_tick()
            except Exception:  # noqa: BLE001
                pass
        with self.lock:
            c = self.cfg()
            cold_on = self.cold is not None and c["cold_days"] > 0
            if self.cold is not None and not self.cold.manifest.exists() and not self.anchor():
                try:                                       # the manifest's first run: archives made before it are entered (noted)
                    out["registered"] = [e["day"] for e in logaudit.register_existing(self.cold, self.cold.manifest)]
                    if out["registered"] and self.hot is not None:
                        self.hot._set_state("manifest_seq", int(self.cold.manifest.head().get("seq") or 0))
                except OSError as e:
                    out["errors"].append({"error": f"manifest: {str(e)[:250]}"})
            if self.hot is not None:
                self.hot.archive_first = cold_on
                self.hot.retry_deletes()
                cut = now - timedelta(hours=c["retention_h"]) - ARCHIVE_MARGIN
                today = day_key(now)
                for day in self.hot.days():
                    if day < today and day_end(day) <= cut and not self.stop.is_set():
                        if not self._retire(day, cold_on, out, "retention") and out.get("paused"):
                            break
                while c["hot_max"] > 0 and self.hot.total_bytes() > c["hot_max"] and not self.stop.is_set() and not out.get("paused"):
                    old = [d for d in self.hot.days() if d < today]
                    if not old or not self._retire(old[0], cold_on, out, "hot_cap"):
                        break
            if self.cold is not None:
                self.cold.cleanup_tmp(older_s=60)
                if c["cold_days"] > 0:                     # retention is the only thing that deletes an archive (cold_max_gb alerts)
                    keep_from = now - timedelta(days=c["cold_days"])
                    for day in self.cold.days():
                        if day_end(day) <= keep_from:
                            self.cold.delete_day(day, "expire", f"retention {c['cold_days']:g} days")
                            out["deleted_cold"].append(day)
                try:
                    self.check = logaudit.verify(self.cold.dir, deep=False, anchor_seq=self.anchor())
                except Exception as e:  # noqa: BLE001
                    self.check = {"ok": False, "at": _iso(now), "problems": [{"code": "check_failed", "detail": str(e)[:200]}]}
            if out.get("paused"):
                out["paused"] = dict(out["paused"])
            self.last_run = {"at": _iso(now), **out}
        return out

    def verify_deep(self, frm: Optional[str] = None, to: Optional[str] = None, progress=None, cancel=None, user: str = "") -> dict:
        """Every archive of [frm, to] re-read against the manifest (sha256), the chain checked; logged in VERIFY.jsonl."""
        if self.cold is None:
            raise logaudit.AuditError("no_archive", "this installation keeps no archive (WATCHOVER_LOG_STORE=db)")
        res = logaudit.verify(self.cold.dir, frm, to, deep=True, anchor_seq=self.anchor(), progress=progress, cancel=cancel)
        logaudit.log_verify(self.cold.dir, res, user)
        if not frm and not to:
            self.check = res
        elif not res["ok"]:
            self.check = res
        return res

    def alerts(self) -> list:
        """The storage alerts (logaudit.alerts) with their notification text, for the alert engine (condition log_storage)."""
        rep = self.report()
        out = []
        for a in rep.get("alerts", []):
            t, b = logaudit.alert_text(a, rep)
            out.append({**a, "title": t, "body": b})
        return out

    # ---------------------------------------------------------------- migration from the main database
    @staticmethod
    def prepare(kb, hot: HotStore) -> bool:
        """Reserve ids / line ids above the old main-database table so copied rows keep theirs. True when there is one to copy."""
        try:
            if "live_batches" not in kb.tables():
                return False
            from . import logstore
            r = kb._exec("SELECT MAX(id) AS m FROM live_batches")
            hot.reserve(int((r[0]["m"] if r else 0) or 0), logstore.last_seq(kb) if "seq_max" in kb.columns("live_batches") else 0)
            return True
        except Exception:  # noqa: BLE001
            return False

    def migrate(self, kb, batch: int = 500, pause: float = 0.02) -> dict:
        """Copy the main database's live_batches into the hot day files (ids kept, INSERT OR IGNORE), delete what was copied,
        drop the table once empty. Bounded (batch rows per step, a pause between steps), safe to interrupt and re-run."""
        try:
            if "live_batches" not in kb.tables():
                self.migration = {"state": "none"}
                return self.migration
            have = set(kb.columns("live_batches"))
        except Exception as e:  # noqa: BLE001
            self.migration = {"state": "error", "error": str(e)[:200]}
            return self.migration
        dflt = {"v": "1", "pz": "NULL", "seq_min": "0", "seq_max": "0", "sevmask": "31", "host": "''", "src": "''", "payload": "''", "env": "''", "agent": "''"}
        sel = ", ".join(c if c in have else f"{dflt.get(c, 'NULL')} AS {c}" for c in COLS)
        total = kb.count("live_batches")
        self.migration = {"state": "running", "total": total, "copied": 0, "started": _iso(datetime.now(UTC))}
        last = 0
        try:
            while not self.stop.is_set():
                rows = kb._exec(f"SELECT {sel} FROM live_batches WHERE id > ? ORDER BY id LIMIT {int(batch)}", (last,))
                if not rows:
                    break
                for r in rows:
                    if r.get("v") is None:
                        r["v"] = 1
                self.hot.insert_rows(rows)
                last = int(rows[-1]["id"])
                kb._exec("DELETE FROM live_batches WHERE id <= ?", (last,))
                self.migration["copied"] += len(rows)
                if pause:
                    time.sleep(pause)
            if not self.stop.is_set() and kb.count("live_batches") == 0:
                kb._exec("DROP TABLE IF EXISTS live_batches")
                self.migration["state"] = "done"
        except Exception as e:  # noqa: BLE001 - what was copied is deleted; the rest stays for the next start
            self.migration.update(state="error", error=str(e)[:200])
        return self.migration

    # ---------------------------------------------------------------- thread
    def start(self, kb=None, first_s: float = 60.0) -> "LogTiers":
        def run():
            if kb is not None and self.hot is not None:
                self.migrate(kb)
            if self.stop.wait(first_s):
                return
            while not self.stop.is_set():
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - never stops ingest; reported on the storage card
                    self.last_run = {"at": _iso(datetime.now(UTC)), "errors": [{"error": str(e)[:300]}]}
                if self.stop.wait(self.every_s):
                    return
        self._thread = threading.Thread(target=run, name="log-tiers", daemon=True)
        self._thread.start()
        return self

    # ---------------------------------------------------------------- demo clean-up (demo.PURGERS)
    def purge(self, agent: str, hosts=(), dry_run: bool = False) -> dict:
        """Remove one sender's lines from both tiers (demo.clean: the simulator's). Every line of that sender goes, whatever its
        host name, as in the main-database path (live_batches WHERE agent = ?); `hosts` is accepted for the PURGERS signature."""
        with self.lock:                                    # not while the job archives a day
            return {"live_lines_hot": self.hot.purge_agent(agent, dry_run) if self.hot is not None else 0,
                    "live_lines_cold": self.cold.purge_agent(agent, dry_run) if self.cold is not None else 0}

    # ---------------------------------------------------------------- report
    def report(self, kb=None, live=None) -> dict:
        c = self.cfg()
        out: dict = {"mode": "files" if self.hot is not None else "db", "retention_h": c["retention_h"], "cold_retention_days": c["cold_days"]}
        warnings = []
        if self.hot is not None:
            h = self.hot.stats()
            h.update(cap_bytes=int(c["hot_max"]), pct=round(100.0 * h["bytes"] / c["hot_max"], 1) if c["hot_max"] else None,
                     disk=_disk(self.hot.dir))
            out["hot"] = h
        elif live is not None:
            out["hot"] = {**live.db_stats(), "cap_bytes": 0, "pct": None, "in_main_db": True}
        if self.cold is not None:
            cs = self.cold.stats()
            cs.update(cap_bytes=int(c["cold_max"]), pct=round(100.0 * cs["bytes"] / c["cold_max"], 1) if c["cold_max"] else None,
                      enabled=c["cold_days"] > 0, disk=_disk(self.cold.dir), last_run=self.last_run or None, archived=self.archived[-7:],
                      paused=self.paused)
            out["cold"] = cs
            head = self.cold.manifest.head()
            ck = self.check or {}
            out["manifest"] = {"file": str(self.cold.manifest.path), "exists": self.cold.manifest.exists(), "head": head, "anchor_seq": self.anchor(),
                               "writable": self.audit_ok,
                               "check": {k: ck.get(k) for k in ("ok", "at", "deep", "checked", "problem_count")} if ck else None,
                               "problems": (ck.get("problems") or [])[:20], "last_verify": logaudit.last_verify(self.cold.dir)}
        out["estimate"] = logaudit.estimate(out.get("cold"), out.get("hot"), c["cold_days"])
        tier = out.get("hot") or {}
        if tier.get("pct") is not None and tier["pct"] >= 85:
            warnings.append({"code": "hot_cap", "tier": "hot", "pct": tier["pct"], "bytes": tier.get("bytes"), "cap_bytes": tier.get("cap_bytes")})
        out["alerts"] = logaudit.alerts(out, self.paused, self.check)
        warnings += [{"code": a["code"], "tier": a.get("tier"), "pct": a.get("pct"), "level": a["level"]} for a in out["alerts"]]
        if kb is not None:
            try:
                out["main_db"] = {"backend": kb.backend, "bytes": kb.size_bytes(), "label": kb.label}
            except Exception:  # noqa: BLE001
                out["main_db"] = {"backend": getattr(kb, "backend", "?"), "bytes": 0}
        out["migration"] = self.migration
        out["warnings"] = warnings
        return out


_ACTIVE = None


def demo_purge(agent: str, hosts=(), dry_run: bool = False) -> dict:
    """demo.PURGERS entry: the running process's tiers (the last LogTiers registered)."""
    t = _ACTIVE() if _ACTIVE is not None else None
    return t.purge(agent, hosts, dry_run) if t is not None else {}


def register_purger(tiers: "LogTiers") -> None:
    """Make demo clean-up reach the hot day files and the archive of `tiers` (registered once; a later Services replaces it)."""
    global _ACTIVE
    import weakref
    _ACTIVE = weakref.ref(tiers)
    try:
        from . import demo
    except ImportError:
        return
    if demo_purge not in demo.PURGERS:
        demo.PURGERS.append(demo_purge)


def _disk(p: Path) -> Optional[dict]:
    try:
        u = shutil.disk_usage(str(p))
        return {"total": u.total, "free": u.free, "used": u.used}
    except OSError:
        return None


def _hot_summary(d: Path) -> dict:
    """Hot day files read-only (no meta.db is created): bytes, days, lines, oldest / newest stamp."""
    days, events, b, lo, hi = [], 0, 0, "", ""
    for p in sorted(d.glob("logs-*.db")) if d.is_dir() else []:
        m = HOT_RE.match(p.name)
        if not m:
            continue
        sz = _fsize(p)
        b += sz
        ev = 0
        try:
            c = sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True)   # no -wal / -shm created (a read-only mount)
            r = c.execute("SELECT v FROM meta WHERE k = 'events'").fetchone()
            ev = int(r[0] or 0) if r else 0
            a, z = c.execute("SELECT MIN(ts_max), MAX(ts_max) FROM live_batches").fetchone()
            c.close()
            lo = min(lo, a) if lo and a else (lo or a or "")
            hi = max(hi, z or "")
        except sqlite3.Error:
            pass
        events += ev
        days.append({"day": m.group(1), "bytes": sz, "events": ev})
    return {"bytes": b, "events": events, "days": days, "oldest": lo, "newest": hi, "dir": str(d), "disk": _disk(d)}


def _cold_summary(d: Path) -> dict:
    if not d.is_dir():
        return {"bytes": 0, "lines": 0, "days": [], "dir": str(d), "disk": None}
    cs = ColdStore.__new__(ColdStore)                      # read-only: no directory is created, nothing cached across calls
    cs.dir, cs.lock, cs._idx, cs._chunks, cs._cached, cs.cache_max = d, threading.Lock(), {}, OrderedDict(), 0, 0
    cs.decoded = cs.skipped = 0
    cs.manifest, cs.on_manifest = logaudit.Manifest(d), None
    out = cs.stats()
    out["disk"] = _disk(d)
    return out


def _config_path(home: str) -> Path:
    return Path(home) / "config.json"


def set_retention(home: str, hot_hours=None, cold_days=None) -> dict:
    """live_retention_h / cold_retention_days written into <home>/config.json, nothing else touched (secrets stay encrypted as
    they are): `./watchover.sh storage set --hot-hours N --cold-days N` runs this against the data volume."""
    p = _config_path(home)
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg = cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
    if hot_hours is not None:
        cfg["live_retention_h"] = int(hot_hours)
    if cold_days is not None:
        cfg["cold_retention_days"] = int(cold_days)
    tmp = p.with_name("config.json.storage.part")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(cfg, indent=1, ensure_ascii=False))
    os.chmod(str(tmp), 0o600)
    os.replace(tmp, p)
    return {"live_retention_h": cfg.get("live_retention_h", 48), "cold_retention_days": cfg.get("cold_retention_days", 365)}


def _show(a) -> int:
    """`./watchover.sh storage show`: where the tiers are (or that nothing was set), how long they keep lines, what they hold, the
    free space and the yearly need at the current rate. Read-only."""
    try:
        cfg = json.loads(_config_path(a.home).read_text(encoding="utf-8")) if a.home else {}
    except (OSError, ValueError):
        cfg = {}
    hh = float(cfg.get("live_retention_h") or 48)
    cd = float(cfg.get("cold_retention_days", 365) if cfg.get("cold_retention_days") not in (None, "") else 365)
    hot, cold = _hot_summary(Path(a.hot)), _cold_summary(Path(a.cold))
    est = logaudit.estimate(cold, hot, cd)
    fb = logaudit.fmt_bytes

    def disk(x):
        dk = x.get("disk")
        return f"disk {fb(dk['total'])}, {fb(dk['free'])} free" if dk else "disk not readable"
    for k, path, t in (("hot", a.hot_path, hot), ("cold", a.cold_path, cold)):
        where = f"{path}  (set by hand)" if path else f"NOT SET - inside the data volume (/data/logs/{k}); set it: ./watchover.sh storage set --{k} DIR"
        print(f"  {k:<5}{where}")
        if k == "hot":
            print(f"       keeps {hh:g} h · {len(t['days'])} day files · {fb(t['bytes'])} · {t['events']:,} lines · {disk(t)}")
        else:
            print(f"       keeps {cd:g} days · {len(t['days'])} archives · {fb(t['bytes'])} · {t.get('lines', 0):,} lines"
                  + (f" · {t['bytes_per_line']} B/line" if t.get("bytes_per_line") else "") + f" · {disk(t)}")
            m = logaudit.Manifest(Path(a.cold))
            h, lv = m.head(), logaudit.last_verify(Path(a.cold))
            print(f"       manifest: {len(m.entries())} entries, head seq {h.get('seq', 0)} {str(h.get('hash', ''))[:16]}"
                  + (f" · last verify {lv['at']}: {'OK' if lv.get('ok') else 'PROBLEMS'}" if lv else " · never verified (python -m watchover.logtiers verify)"))
    if est.get("basis"):
        print(f"  rate   ~{est['lines_per_min']:,} lines/min ({est['basis']}) x {est['bytes_per_line']} B/line"
              f" -> ~{fb(est['daily_bytes'])}/day, ~{fb(est['yearly_bytes'])}/year; {cd:g} days of archive need ~{fb(est['retention_bytes'])}")
    else:
        print(f"  rate   not measured yet; sizing: ~7-8 B/line archived, e.g. 100,000 lines/min ~ 1.1 GB/day ~ 400 GB/year")
    return 0


def main(argv: list) -> int:
    """python -m watchover.logtiers COMMAND
      archive HOTFILE COLDDIR YYYYMMDD     the tier job's child (lowest CPU priority)
      verify [--dir COLD] [--manifest FILE] [--from D] [--to D] [--quick] [--json]   check the archives against the manifest
      export --from D --to D [--format archive|ndjson] [--out DIR] [--dir COLD] [--hot HOT]   an export for auditors
      relocate SRC DST [--check|--final] [--uid N --gid N]   copy a tier to a new directory (watchover.sh storage set)
      relocate-clean SRC DST [--keep]      after the switch: the old copies go (--keep: they stay)
      show --home H --hot DIR --cold DIR [--hot-path P --cold-path P]   the storage summary
      retention --home H [--hot-hours N] [--cold-days N]   write the retention into config.json"""
    if len(argv) == 4 and argv[0] == "archive":
        try:
            os.nice(19)
        except (AttributeError, OSError):
            pass
        st = archive_file(argv[1], argv[2], argv[3])
        print(json.dumps(st))
        return 0
    import argparse
    ap = argparse.ArgumentParser(prog="python -m watchover.logtiers")
    sub = ap.add_subparsers(dest="cmd")
    v = sub.add_parser("verify")
    v.add_argument("--dir", default="")
    v.add_argument("--manifest", default="")
    v.add_argument("--from", dest="frm", default="")
    v.add_argument("--to", default="")
    v.add_argument("--quick", action="store_true")
    v.add_argument("--json", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("--from", dest="frm", required=True)
    e.add_argument("--to", required=True)
    e.add_argument("--format", choices=("archive", "ndjson"), default="archive")
    e.add_argument("--out", default=".")
    e.add_argument("--dir", default="")
    e.add_argument("--hot", default="")
    r = sub.add_parser("relocate")
    r.add_argument("src")
    r.add_argument("dst")
    r.add_argument("--final", action="store_true")
    r.add_argument("--check", action="store_true", help="prepare the target and check its room, copy nothing")
    r.add_argument("--uid", type=int)
    r.add_argument("--gid", type=int, default=0)
    rc = sub.add_parser("relocate-clean")
    rc.add_argument("src")
    rc.add_argument("dst")
    rc.add_argument("--keep", action="store_true")
    sh = sub.add_parser("show")
    sh.add_argument("--home", default="")
    sh.add_argument("--hot", required=True)
    sh.add_argument("--cold", required=True)
    sh.add_argument("--hot-path", default="")
    sh.add_argument("--cold-path", default="")
    rt = sub.add_parser("retention")
    rt.add_argument("--home", required=True)
    rt.add_argument("--hot-hours", type=int)
    rt.add_argument("--cold-days", type=int)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "verify":
            cdir = Path(a.dir) if a.dir else cold_dir()
            res = logaudit.verify(cdir, logaudit.day_arg(a.frm), logaudit.day_arg(a.to), deep=not a.quick,
                                  manifest=Path(a.manifest) if a.manifest else None, anchor_seq=_anchor_of(hot_dir()) if not a.dir else 0)
            if not a.quick and not a.manifest:
                logaudit.log_verify(cdir, res, "cli")
            if a.json:
                print(json.dumps(res, ensure_ascii=False, indent=1))
            else:
                print(f"{'OK' if res['ok'] else 'PROBLEMS'}: {res['checked']} archives checked ({'sha256' if res['deep'] else 'sizes'}), "
                      f"{res['entries']} manifest entries, head seq {res['head']['seq']} {res['head']['hash'][:16]}")
                for p in res["problems"]:
                    print("  " + " ".join(f"{k}={v}" for k, v in p.items()))
            return 0 if res["ok"] else 1
        if a.cmd == "export":
            cdir = Path(a.dir) if a.dir else cold_dir()
            frm, to = logaudit.day_arg(a.frm), logaudit.day_arg(a.to)
            if a.format == "archive":
                res = logaudit.export_archive(cdir, frm, to, Path(a.out))
            else:
                hd = Path(a.hot) if a.hot else hot_dir()
                res = logaudit.export_ndjson(_cold_summary_store(cdir), frm, to, Path(a.out), hot=_ro_hot(hd))
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0 if not res.get("not_ok") else 1
        if a.cmd == "relocate":
            res = logaudit.relocate(a.src, a.dst, final=a.final, uid=a.uid, gid=a.gid, dry=a.check,
                                    progress=lambda n: print(f"  copy {n}", flush=True))
            print(json.dumps(res))
            return 0
        if a.cmd == "relocate-clean":
            print(json.dumps(logaudit.cleanup_source(a.src, a.dst, delete=not a.keep)))
            return 0
        if a.cmd == "show":
            return _show(a)
        if a.cmd == "retention":
            for k, val, lo, hi in (("--hot-hours", a.hot_hours, 1, 8760), ("--cold-days", a.cold_days, 0, 3650)):
                if val is not None and not lo <= val <= hi:
                    print(f"{k} must be {lo}..{hi}", file=sys.stderr)
                    return 2
            print(json.dumps(set_retention(a.home, a.hot_hours, a.cold_days)))
            return 0
    except logaudit.AuditError as e:
        print(f"error {e.code}: {e.detail}", file=sys.stderr)
        return {"no_space": 3, "target_not_empty": 4}.get(e.code, 5)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(main.__doc__, file=sys.stderr)
    return 2


def _anchor_of(hdir: Path) -> int:
    """The manifest's last seq as the hot disk's meta.db remembers it (0 when there is none)."""
    if not (hdir / "meta.db").exists():
        return 0
    for q in ("mode=ro", "mode=ro&immutable=1"):            # a read-only mount cannot take the -shm file: read the main file then
        try:
            c = sqlite3.connect(f"file:{hdir / 'meta.db'}?{q}", uri=True)
            r = c.execute("SELECT v FROM tier_state WHERE k = 'manifest_seq'").fetchone()
            c.close()
            return int(r[0] or 0) if r else 0
        except sqlite3.Error:
            continue
    return 0


def _cold_summary_store(d: Path) -> "ColdStore":
    return ColdStore(d)


class _ro_hot:
    """The hot day files of a directory for an export (read-only: no HotStore, no meta.db)."""

    def __init__(self, d: Path):
        self.files = {HOT_RE.match(p.name).group(1): p for p in sorted(Path(d).glob("logs-*.db")) if HOT_RE.match(p.name)} if Path(d).is_dir() else {}

    def days(self) -> list:
        return sorted(self.files)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
