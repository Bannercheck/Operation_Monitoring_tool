"""Audit-safe raw-log archive (the cold tier of logtiers.py): sealed day archives in a hash-chained manifest, verification,
auditor exports, moving a tier to a hand-set directory without loss, the storage alerts and the yearly estimate.

  manifest  <cold dir>/MANIFEST.jsonl, one JSON object per line, only ever appended:
              {"seq": 12, "at": "2026-10-16T01:10:00+00:00", "op": "seal", "day": "20261014", "file": "logs-20261014.wcl",
               "sha256": "<archive file>", "bytes": 81234567, "lines": 10450210, "rows": 5120, "first": "<oldest stamp>",
               "last": "<newest stamp>", "note": "", "prev": "<hash of entry 11>", "hash": "<sha256 of this entry without 'hash'>"}
            op: seal (a day archived), reseal (its archive rewritten: a late merge, the demo clean-up), register (an archive that
            existed before the manifest, added on the first run with a note saying so), expire (deleted by cold_retention_days),
            remove (deleted otherwise). The hash is sha256 over the canonical JSON (sorted keys, no spaces, UTF-8) of every other
            field; the first entry's prev is 64 zeros. MANIFEST.head holds the last seq and hash; the hot tier's meta.db keeps the
            last seq too (another disk), so a manifest cut short is noticed.
  sealing   an archive is chmod 0440 once written; the tier job never writes one in place (a rewrite is a new file + rename and
            a reseal entry).
  verify    the chain (each hash, prev = the previous entry's hash, seq + 1, the head), then every archive in force against its
            entry (size; sha256 when deep) and archive files no entry knows. `python -m watchover.logtiers verify [--from --to]`.
  export    a date range for auditors: archive files + MANIFEST.jsonl + SLICE.json + SHA256SUMS + README.txt in one .tar, or the
            stored lines as gzip NDJSON; every export file gets <name>.sha256 (`sha256sum -c`).
  relocate  a tier moved to a new directory (watchover.sh storage set): copy under a temp name, sha256 of what was read equals the
            sha256 of what landed, then rename; a final pass with the writers stopped copies what changed; the old files go only
            after the new path runs.
Python 3.9, standard library."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

UTC = timezone.utc
MANIFEST, HEAD, LOCK, VERIFY_LOG = "MANIFEST.jsonl", "MANIFEST.head", ".manifest.lock", "VERIFY.jsonl"
ZERO = "0" * 64
SEALED = 0o440
OPS_ACTIVE = ("seal", "reseal", "register")
OPS_GONE = ("expire", "remove")
REGISTERED_LATER = "registered later: this archive existed before the manifest (first run of the audit manifest)"
LINE_BYTES = 7.5              # cold bytes per stored line measured on production-like logs (~7-8): used until the archive can say
WARN_PCT, CRIT_PCT = 80.0, 90.0
WARN_ONLY = frozenset({"not_sealed"})
STATE = ".relocate-state.json"
ARCHIVE_RE = re.compile(r"^logs-(\d{8})\.wcl$")
DAY_RE = re.compile(r"^\d{8}$")
# a directory the operator gives (host side): absolute, plain characters, not a system directory itself or inside one
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,240}$")
SYSTEM_DIRS = frozenset({"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64", "/libx32", "/media", "/mnt", "/opt",
                         "/proc", "/root", "/run", "/sbin", "/srv", "/sys", "/tmp", "/usr", "/var", "/var/lib", "/var/log", "/var/tmp",
                         "/Users", "/Volumes", "/private", "/System", "/Library", "/Applications"})
SYSTEM_TREES = ("/proc/", "/sys/", "/dev/", "/etc/", "/boot/", "/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/run/",
                "/var/lib/docker/", "/var/lib/containers/", "/System/", "/Library/", "/private/etc/")
# what a tier directory may hold before it becomes one (a fresh file system has lost+found)
OURS = re.compile(r"^(logs-\d{8}\.(db|db-wal|db-shm|db-journal|wcl|wcl\.tmp)|meta\.db(-wal|-shm|-journal)?|MANIFEST\.(jsonl|head)|"
                  r"VERIFY\.jsonl|\.manifest\.lock|\.relocate-state\.json|\.probe|.+\.part|lost\+found)$")


class Cancelled(Exception):
    pass


class AuditError(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def day_arg(v) -> Optional[str]:
    """'2026-10-14' / '20261014' / '' -> 'YYYYMMDD' or None; anything else raises ValueError."""
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().replace("-", "")[:8]
    if not DAY_RE.match(s):
        raise ValueError(f"not a date: {v!r} (YYYY-MM-DD)")
    datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return s


def check_path(p) -> Optional[str]:
    """None when `p` may be given as a raw-log directory, else why not: not_absolute, bad_chars, system_dir."""
    if not isinstance(p, str) or not p:
        return "empty"
    if not p.startswith("/"):
        return "not_absolute"
    if not p.strip("/"):
        return "system_dir"
    if not PATH_RE.match(p) or "//" in p or any(seg in (".", "..") for seg in p.split("/")):
        return "bad_chars"
    q = p.rstrip("/") or "/"
    if q in SYSTEM_DIRS or any((q + "/").startswith(t) for t in SYSTEM_TREES):
        return "system_dir"
    return None


def check_pair(hot: str, cold: str) -> Optional[str]:
    """The hot and the archive directory are two different directories, neither inside the other."""
    if hot and cold:
        a, b = hot.rstrip("/") + "/", cold.rstrip("/") + "/"
        if a == b:
            return "same_dir"
        if a.startswith(b) or b.startswith(a):
            return "nested"
    return None


def sha256_file(path, progress: Optional[Callable[[int], None]] = None, cancel=None, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
            if progress is not None:
                progress(len(b))
            if cancel is not None and cancel.is_set():
                raise Cancelled()
    return h.hexdigest()


def seal(path) -> None:
    try:
        os.chmod(str(path), SEALED)
    except OSError:
        pass


def unseal(path) -> None:
    """Writable for the owner again (Windows refuses to replace or delete a read-only file; POSIX does not need it)."""
    try:
        os.chmod(str(path), 0o640)
    except OSError:
        pass


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.part")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _flock:
    """An exclusive lock on <dir>/.manifest.lock across processes (fcntl; a no-op where there is none)."""

    def __init__(self, path: Path):
        self.path, self.f = path, None

    def __enter__(self):
        try:
            import fcntl
            self.f = open(self.path, "a")
            fcntl.flock(self.f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            self.f = None
        return self

    def __exit__(self, *a):
        if self.f is not None:
            try:
                import fcntl
                fcntl.flock(self.f, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            self.f.close()


# ================================================================ the manifest
def _canon(e: dict) -> bytes:
    return json.dumps({k: v for k, v in e.items() if k != "hash"}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def entry_hash(e: dict) -> str:
    return hashlib.sha256(_canon(e)).hexdigest()


class Manifest:
    def __init__(self, cdir):
        self.dir = Path(cdir)
        self.path, self.head_path = self.dir / MANIFEST, self.dir / HEAD
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> list:
        """[(line number, entry or None when the line is not a JSON object)] in file order."""
        try:
            data = self.path.read_bytes()
        except OSError:
            return []
        out = []
        for i, ln in enumerate(data.split(b"\n"), 1):
            if not ln.strip():
                continue
            try:
                e = json.loads(ln)
            except ValueError:
                e = None
            out.append((i, e if isinstance(e, dict) else None))
        return out

    def entries(self) -> list:
        return [e for _i, e in self.read() if e is not None]

    def active(self) -> dict:
        """day -> the entry that describes its archive now (seal / reseal / register); expired and removed days are gone."""
        st: dict = {}
        for e in self.entries():
            if e.get("op") in OPS_ACTIVE:
                st[e.get("day")] = e
            elif e.get("op") in OPS_GONE:
                st.pop(e.get("day"), None)
        return st

    def head(self) -> dict:
        try:
            h = json.loads(self.head_path.read_text(encoding="utf-8"))
            return h if isinstance(h, dict) else {}
        except (OSError, ValueError):
            return {}

    def append(self, op: str, day: str, info: Optional[dict] = None, note: str = "") -> dict:
        info = info or {}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock, _flock(self.dir / LOCK):
            ents = self.entries()
            last = ents[-1] if ents else None
            seq = int(last["seq"]) + 1 if last and isinstance(last.get("seq"), int) else len(ents) + 1
            e = {"seq": seq, "at": now_iso(), "op": op, "day": day, "file": f"logs-{day}.wcl", "sha256": str(info.get("sha256") or ""),
                 "bytes": int(info.get("bytes") or 0), "lines": int(info.get("lines") or 0), "rows": int(info.get("rows") or 0),
                 "first": str(info.get("first") or ""), "last": str(info.get("last") or ""), "note": str(note or "")[:300],
                 "prev": str(last.get("hash") or ZERO) if last else ZERO}
            e["hash"] = entry_hash(e)
            line = json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
            fresh = not self.path.exists()
            with open(self.path, "ab") as f:
                if f.tell() > 0:                              # a line cut by a crash stays one (reported) bad line, never glued to this one
                    with open(self.path, "rb") as r:
                        r.seek(-1, os.SEEK_END)
                        if r.read(1) != b"\n":
                            f.write(b"\n")
                f.write(line.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())
            if fresh:
                try:
                    os.chmod(str(self.path), 0o640)
                except OSError:
                    pass
            _write_atomic(self.head_path, json.dumps({"seq": e["seq"], "hash": e["hash"], "at": e["at"]}) + "\n")
        return e


def archive_info(cold, day: str) -> dict:
    p = cold.path(day)
    ix = cold.index(day) or {}
    return {"sha256": sha256_file(p), "bytes": p.stat().st_size, "lines": int(ix.get("lines") or 0), "rows": int(ix.get("rows") or 0),
            "first": ix.get("t0") or "", "last": ix.get("t1") or ""}


def register_existing(cold, manifest: Manifest, note: str = REGISTERED_LATER) -> list:
    """Archives no entry describes get one (op register, the note says it came later) and are sealed: the manifest's first run."""
    act = manifest.active()
    out = []
    for d in cold.days():
        if d not in act:
            seal(cold.path(d))
            out.append(manifest.append("register", d, archive_info(cold, d), note))
    return out


def _archive_days(cdir: Path) -> list:
    out = []
    try:
        for p in cdir.iterdir():
            m = ARCHIVE_RE.match(p.name)
            if m:
                out.append(m.group(1))
    except OSError:
        pass
    return sorted(out)


def verify(cdir, frm: Optional[str] = None, to: Optional[str] = None, deep: bool = True, anchor_seq: int = 0,
           progress: Optional[Callable[[float], None]] = None, cancel=None, manifest: Optional[Path] = None) -> dict:
    """The chain, then the archives of days in [frm, to] (YYYYMMDD, inclusive) against it. deep=False compares sizes only (the
    hourly check); deep=True re-reads every archive for its sha256. ok is False on any problem but a missing seal (warning).
    manifest: a MANIFEST.jsonl kept elsewhere (an auditor's extracted export: archives in archive/, the manifest beside it)."""
    cdir = Path(cdir)
    m = Manifest(cdir)
    if manifest is not None:
        m.path, m.head_path = Path(manifest), Path(manifest).with_name(HEAD)
    rows = m.read()
    problems: list = []
    prev, seq = ZERO, 0
    state: dict = {}
    for ln, e in rows:
        if e is None:
            problems.append({"code": "entry_unreadable", "line": ln})
            continue
        if e.get("hash") != entry_hash(e):
            problems.append({"code": "entry_altered", "line": ln, "seq": e.get("seq"), "day": e.get("day")})
        if e.get("prev") != prev:
            problems.append({"code": "chain_broken", "line": ln, "seq": e.get("seq"), "day": e.get("day")})
        if e.get("seq") != seq + 1:
            problems.append({"code": "seq_order", "line": ln, "seq": e.get("seq"), "expected": seq + 1})
        prev = str(e.get("hash") or "")
        seq = e["seq"] if isinstance(e.get("seq"), int) else seq + 1
        if e.get("op") in OPS_ACTIVE:
            state[e.get("day")] = e
        elif e.get("op") in OPS_GONE:
            state.pop(e.get("day"), None)
    files = _archive_days(cdir)
    if not rows and (files or anchor_seq):
        problems.append({"code": "manifest_missing"})
    head = m.head()
    if head:
        if int(head.get("seq") or 0) > seq:
            problems.append({"code": "truncated", "head_seq": head.get("seq"), "seq": seq})
        elif int(head.get("seq") or 0) == seq and head.get("hash") != prev:
            problems.append({"code": "head_mismatch", "seq": seq})
    if anchor_seq and int(anchor_seq) > seq:
        problems.append({"code": "truncated", "anchor_seq": int(anchor_seq), "seq": seq})

    def inr(d: str) -> bool:
        return (not frm or d >= frm) and (not to or d <= to)
    todo = [d for d in sorted(set(state) | set(files)) if inr(d)]
    total = 0
    if deep:
        for d in todo:
            try:
                total += (cdir / f"logs-{d}.wcl").stat().st_size
            except OSError:
                pass
    done = [0]

    def step(n: int) -> None:
        done[0] += n
        if progress is not None and total:
            progress(min(1.0, done[0] / total))
    days = []
    for d in todo:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        e = state.get(d)
        p = cdir / f"logs-{d}.wcl"
        row = {"day": d, "status": "ok"}
        if e is None:
            row["status"] = "unregistered"
            problems.append({"code": "unregistered", "day": d})
        elif not p.exists():
            row["status"] = "missing"
            problems.append({"code": "missing", "day": d, "seq": e.get("seq")})
        else:
            st = p.stat()
            row.update(bytes=st.st_size, sha256=e.get("sha256"), seq=e.get("seq"), op=e.get("op"))
            if st.st_size != int(e.get("bytes") or -1):
                row["status"] = "altered"
                problems.append({"code": "altered", "day": d, "seq": e.get("seq"), "why": "size"})
            elif deep:
                got = sha256_file(p, step, cancel)
                if got != e.get("sha256"):
                    row.update(status="altered", sha256_now=got)
                    problems.append({"code": "altered", "day": d, "seq": e.get("seq"), "why": "sha256"})
            if row["status"] == "ok" and st.st_mode & 0o222:
                row["status"] = "not_sealed"
                problems.append({"code": "not_sealed", "day": d})
        days.append(row)
    if progress is not None:
        progress(1.0)
    bad = [p for p in problems if p["code"] not in WARN_ONLY]
    return {"ok": not bad, "deep": deep, "at": now_iso(), "from": frm, "to": to, "entries": len(rows), "checked": len(days),
            "bytes": total if deep else None, "days": days, "problems": problems[:500], "problem_count": len(problems),
            "head": {"seq": seq, "hash": prev}, "dir": str(cdir)}


def log_verify(cdir, res: dict, user: str = "") -> None:
    """Every deep verification leaves a line in <cold dir>/VERIFY.jsonl (the archive's own verification history)."""
    try:
        with open(Path(cdir) / VERIFY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": res.get("at"), "ok": res.get("ok"), "from": res.get("from"), "to": res.get("to"), "checked": res.get("checked"),
                                "problems": res.get("problem_count"), "head": res.get("head"), "user": user}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def last_verify(cdir) -> Optional[dict]:
    try:
        lines = (Path(cdir) / VERIFY_LOG).read_text(encoding="utf-8").strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError, IndexError):
        return None


# ================================================================ exports for auditors
class _HashWriter:
    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def write(self, b) -> int:
        self.h.update(b)
        self.n += len(b)
        return self.f.write(b)

    def flush(self) -> None:
        self.f.flush()

    def tell(self) -> int:
        return self.n


class _HashReader:
    def __init__(self, f, progress=None, cancel=None):
        self.f, self.h, self.progress, self.cancel = f, hashlib.sha256(), progress, cancel

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        self.h.update(b)
        if self.progress is not None:
            self.progress(len(b))
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        return b


def _space(out_dir: Path, need: int) -> None:
    free = shutil.disk_usage(str(out_dir)).free
    if need * 1.05 + (64 << 20) > free:
        raise AuditError("no_space", f"the export needs about {need >> 20} MB, {free >> 20} MB free in {out_dir}")


def _finish(part: Path, final: Path, sha: str) -> None:
    os.replace(part, final)
    (final.parent / (final.name + ".sha256")).write_text(f"{sha}  {final.name}\n", encoding="utf-8")


README = """Watchover denetim dışa aktarımı / audit export
=============================================

Türkçe
  archive/logs-YYYYMMDD.wcl  günlük ham log arşivleri (değiştirilmeden kopyalandı)
  MANIFEST.jsonl             arşiv manifestosunun tamamı (karma zinciri: her kayıt bir öncekinin karmasını içerir)
  MANIFEST.head              zincirin son kaydı (seq, hash)
  SLICE.json                 bu aralığın günleri, manifestodaki kayıtları ve dışa aktarım anındaki doğrulama sonucu
  SHA256SUMS                 bu paketteki her dosyanın sha256 özeti

  Doğrulama:  sha256sum -c SHA256SUMS
              python -m watchover.logtiers verify --dir <bu klasör>/archive --manifest <bu klasör>/MANIFEST.jsonl --from {frm} --to {to}
  Paketin kendisinin özeti yanındaki .sha256 dosyasındadır (sha256sum -c <paket>.sha256).

English
  archive/ holds the day archives as stored, MANIFEST.jsonl the full hash chain (every entry carries the previous entry's hash),
  SLICE.json the days of this range with their entries and the verification at export time, SHA256SUMS a sha256 of every file.
  Check: sha256sum -c SHA256SUMS; python -m watchover.logtiers verify --dir <folder>/archive --manifest <folder>/MANIFEST.jsonl --from {frm} --to {to}
  The package's own sha256 is in the .sha256 file next to it.
"""


def export_archive(cdir, frm: Optional[str], to: Optional[str], out_dir, progress: Optional[Callable[[float], None]] = None,
                   cancel=None) -> dict:
    """One .tar for auditors: the archives of [frm, to] as stored, the whole manifest, the slice, SHA256SUMS, a README."""
    cdir, out_dir = Path(cdir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    m = Manifest(cdir)
    state = m.active()
    days = [d for d in sorted(set(state) | set(_archive_days(cdir))) if (not frm or d >= frm) and (not to or d <= to)]
    present = [d for d in days if (cdir / f"logs-{d}.wcl").exists()]
    total = sum((cdir / f"logs-{d}.wcl").stat().st_size for d in present)
    _space(out_dir, total + (4 << 20))
    chain = verify(cdir, frm, to, deep=False)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"watchover-audit-{frm or 'start'}-{to or 'end'}-{stamp}.tar"
    final, part = out_dir / name, out_dir / (name + ".part")
    done = [0]

    def step(n: int) -> None:
        done[0] += n
        if progress is not None and total:
            progress(min(0.99, done[0] / total))
    sums, per_day = [], []
    try:
        with open(part, "wb") as raw:
            hw = _HashWriter(raw)
            with tarfile.open(fileobj=hw, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                def add_bytes(arc: str, data: bytes) -> None:
                    ti = tarfile.TarInfo(arc)
                    ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o444
                    tar.addfile(ti, io.BytesIO(data))
                    sums.append((hashlib.sha256(data).hexdigest(), arc))
                for d in present:
                    p = cdir / f"logs-{d}.wcl"
                    st = p.stat()
                    ti = tarfile.TarInfo(f"archive/{p.name}")
                    ti.size, ti.mtime, ti.mode = st.st_size, int(st.st_mtime), 0o440
                    with open(p, "rb") as f:
                        hr = _HashReader(f, step, cancel)
                        tar.addfile(ti, hr)
                    sha = hr.h.hexdigest()
                    sums.append((sha, ti.name))
                    e = state.get(d)
                    per_day.append({"day": d, "sha256": sha, "bytes": st.st_size, "seq": e.get("seq") if e else None,
                                    "status": "unregistered" if e is None else ("ok" if e.get("sha256") == sha else "altered")})
                for d in days:
                    if d not in present:
                        per_day.append({"day": d, "status": "missing", "seq": state[d].get("seq") if d in state else None})
                per_day.sort(key=lambda x: x["day"])
                try:
                    mbytes = m.path.read_bytes()
                except OSError:
                    mbytes = b""
                add_bytes("MANIFEST.jsonl", mbytes)
                add_bytes("MANIFEST.head", json.dumps(m.head()).encode("utf-8"))
                ents = [e for e in m.entries() if (not frm or str(e.get("day")) >= frm) and (not to or str(e.get("day")) <= to)]
                slice_ = {"from": frm, "to": to, "created": now_iso(), "days": per_day, "entries": ents, "head": chain["head"],
                          "chain_ok": not [p for p in chain["problems"] if p["code"] not in WARN_ONLY and p["code"] != "altered"],
                          "problems": chain["problems"][:200]}
                add_bytes("SLICE.json", json.dumps(slice_, ensure_ascii=False, indent=1).encode("utf-8"))
                add_bytes("README.txt", README.format(frm=frm or "", to=to or "").encode("utf-8"))
                body = "".join(f"{s}  {n}\n" for s, n in sums).encode("utf-8")
                ti = tarfile.TarInfo("SHA256SUMS")
                ti.size, ti.mtime, ti.mode = len(body), int(time.time()), 0o444
                tar.addfile(ti, io.BytesIO(body))
            raw.flush()
            os.fsync(raw.fileno())
        sha = hw.h.hexdigest()
        _finish(part, final, sha)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    if progress is not None:
        progress(1.0)
    bad = [x["day"] for x in per_day if x["status"] != "ok"]
    return {"file": name, "format": "archive", "sha256": sha, "bytes": final.stat().st_size, "days": len(per_day), "archives": len(present),
            "not_ok": bad, "head": chain["head"], "from": frm, "to": to}


def export_ndjson(cold, frm: Optional[str], to: Optional[str], out_dir, hot=None, progress: Optional[Callable[[float], None]] = None,
                  cancel=None) -> dict:
    """The stored lines of [frm, to] as gzip NDJSON, one stored JSON line per line, oldest day first: archived days (each archive
    checked against the manifest first) and, for days not archived yet, the hot day files."""
    from . import logtiers
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = Manifest(cold.dir).active()
    inr = (lambda d: (not frm or d >= frm) and (not to or d <= to))
    cdays = [d for d in cold.days() if inr(d)]
    hdays = [d for d in (hot.days() if hot is not None else []) if inr(d) and d not in cdays]
    plan = sorted([(d, "cold") for d in cdays] + [(d, "hot") for d in hdays])
    est = sum(cold.path(d).stat().st_size for d in cdays) * 3 + sum(logtiers._fsize(hot.files[d]) for d in hdays if d in hot.files) // 3
    _space(out_dir, est)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"watchover-lines-{frm or 'start'}-{to or 'end'}-{stamp}.ndjson.gz"
    final, part = out_dir / name, out_dir / (name + ".part")
    lines, per_day = 0, []
    try:
        with open(part, "wb") as raw:
            hw = _HashWriter(raw)
            with gzip.GzipFile(fileobj=hw, mode="wb", mtime=0) as gz:
                for i, (d, tier) in enumerate(plan):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    n = 0
                    row = {"day": d, "tier": tier}
                    if tier == "cold":
                        e = state.get(d)
                        sha = sha256_file(cold.path(d), cancel=cancel)
                        row["status"] = "unregistered" if e is None else ("ok" if e.get("sha256") == sha else "altered")
                        rows = cold.iter_day(d)
                    else:
                        row["status"] = "hot"
                        rows = logtiers.hot_rows(hot.files[d])
                    for r in rows:
                        for ln in r["_text"].split(b"\n"):
                            if ln:
                                gz.write(ln + b"\n")
                                n += 1
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                    row["lines"] = n
                    lines += n
                    per_day.append(row)
                    if progress is not None:
                        progress(min(0.99, (i + 1) / max(1, len(plan))))
            raw.flush()
            os.fsync(raw.fileno())
        sha = hw.h.hexdigest()
        _finish(part, final, sha)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    if progress is not None:
        progress(1.0)
    return {"file": name, "format": "ndjson", "sha256": sha, "bytes": final.stat().st_size, "lines": lines, "days": per_day,
            "not_ok": [x["day"] for x in per_day if x["status"] not in ("ok", "hot")], "from": frm, "to": to}


def ndjson_file(out_dir, stem: str, fill: Callable, cancel=None) -> dict:
    """<stem>-<stamp>.ndjson.gz written by fill(write): write(obj) adds one JSON line; fill's result dict comes back with the
    file name, its sha256 (also in <name>.sha256) and size."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _space(out_dir, 64 << 20)
    name = f"{stem}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.ndjson.gz"
    final, part = out_dir / name, out_dir / (name + ".part")
    try:
        with open(part, "wb") as raw:
            hw = _HashWriter(raw)
            with gzip.GzipFile(fileobj=hw, mode="wb", mtime=0) as gz:
                def write(obj) -> None:
                    gz.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                res = dict(fill(write) or {})
            raw.flush()
            os.fsync(raw.fileno())
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        sha = hw.h.hexdigest()
        _finish(part, final, sha)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    res.update(file=name, sha256=sha, bytes=final.stat().st_size, format="ndjson")
    return res


# ================================================================ moving a tier to a hand-set directory
def prepare_target(dst, uid: Optional[int] = None, gid: Optional[int] = None) -> dict:
    """The directory exists (created), holds nothing but tier files (an empty file system's lost+found is fine), belongs to the
    containers' user (uid / gid when given: only this directory, never recursively) and takes a write."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    foreign = sorted(p.name for p in dst.iterdir() if not OURS.match(p.name))
    if foreign:
        raise AuditError("target_not_empty", f"{dst} holds other files ({', '.join(foreign[:5])}{' …' if len(foreign) > 5 else ''}): name an empty directory")
    if uid is not None:
        try:
            os.chown(str(dst), int(uid), int(gid or 0))
            os.chmod(str(dst), 0o2770)
        except OSError as e:
            raise AuditError("target_owner", f"cannot give {dst} to {uid}:{gid or 0}: {e}")
    probe = dst / ".probe"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        raise AuditError("target_unwritable", f"{dst}: {e}")
    du = shutil.disk_usage(str(dst))
    return {"dir": str(dst), "free": du.free, "total": du.total}


def _src_files(src: Path) -> list:
    try:
        return sorted(p for p in src.iterdir() if p.is_file() and not p.is_symlink() and OURS.match(p.name)
                      and not p.name.endswith((".part", ".tmp")) and p.name not in (STATE, LOCK, ".probe"))
    except OSError:
        return []


def _state(dst: Path) -> dict:
    try:
        s = json.loads((dst / STATE).read_text(encoding="utf-8"))
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def _copy_verified(sp: Path, dp: Path, uid: Optional[int], gid: Optional[int]) -> str:
    part = dp.with_name(dp.name + ".part")
    h = hashlib.sha256()
    with open(sp, "rb") as f, open(part, "wb") as o:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            o.write(b)
        o.flush()
        os.fsync(o.fileno())
    if sha256_file(part) != h.hexdigest():
        part.unlink()
        raise AuditError("verify_failed", f"{dp.name}: the copy differs from what was read")
    shutil.copystat(str(sp), str(part))
    if uid is not None:
        try:
            os.chown(str(part), int(uid), int(gid or 0))
        except OSError:
            pass
    if dp.exists():
        unseal(dp)
    os.replace(part, dp)
    return h.hexdigest()


def relocate(src, dst, final: bool = False, uid: Optional[int] = None, gid: Optional[int] = None,
             progress: Optional[Callable[[str], None]] = None, dry: bool = False) -> dict:
    """Copy the tier files of `src` into `dst` (both directories as this process sees them). Each file: temp name, sha256 of the
    bytes read == sha256 of the copy re-read, rename; its (size, mtime) is remembered in dst/.relocate-state.json, so a later pass
    copies only what changed since. final=True (writers stopped): also drops copies of files gone from src (an expired day, a
    checkpointed WAL) and checks every source file has its copy. Refuses (no_space) before copying more than dst can take."""
    src, dst = Path(src), Path(dst)
    prepare_target(dst, uid, gid)
    state = _state(dst)
    files = _src_files(src) if src.is_dir() else []
    todo = []
    for p in files:
        st = p.stat()
        rec, d = state.get(p.name), dst / p.name
        if rec and rec[0] == st.st_size and rec[1] == st.st_mtime_ns and d.exists() and d.stat().st_size == st.st_size:
            continue
        todo.append((p, st))
    need = sum(st.st_size for _p, st in todo)
    free = shutil.disk_usage(str(dst)).free
    margin = max(256 << 20, need // 50)
    if need + margin > free:
        raise AuditError("no_space", f"{dst} has {free >> 20} MB free, the copy needs {need >> 20} MB (+{margin >> 20} MB reserve)")
    if dry:                                                # the check only: the directory is ready and has the room
        return {"src": str(src), "dst": str(dst), "files": len(files), "copied": 0, "bytes": need, "free": free, "dry": True}
    for p, st in todo:
        if progress is not None:
            progress(p.name)
        sha = _copy_verified(p, dst / p.name, uid, gid)
        state[p.name] = [st.st_size, st.st_mtime_ns, sha]
        _write_atomic(dst / STATE, json.dumps(state))
    removed = []
    if final:
        names = {p.name for p in files}
        for name in [n for n in state if n not in names]:
            q = dst / name
            if q.exists():
                unseal(q)
                q.unlink()
            state.pop(name, None)
            removed.append(name)
        bad = [p.name for p in files if not (dst / p.name).exists() or (dst / p.name).stat().st_size != p.stat().st_size or p.name not in state]
        if bad:
            raise AuditError("verify_failed", f"not copied identically: {', '.join(bad[:5])}")
    _write_atomic(dst / STATE, json.dumps(state))
    return {"src": str(src), "dst": str(dst), "files": len(files), "copied": len(todo), "bytes": need, "removed": removed, "final": final,
            "free": free}


def cleanup_source(src, dst, delete: bool = True) -> dict:
    """After the switch: the source files whose verified copy is in dst (same size as recorded) are deleted (delete=False keeps
    them); the copy record goes. A file that changed after its copy is kept and reported."""
    src, dst = Path(src), Path(dst)
    state = _state(dst)
    removed, kept = [], []
    for p in _src_files(src) if src.is_dir() else []:
        rec, d = state.get(p.name), dst / p.name
        if delete and rec and d.exists() and d.stat().st_size == p.stat().st_size == rec[0]:
            unseal(p)
            p.unlink()
            removed.append(p.name)
        else:
            kept.append(p.name)
    if delete:
        for extra in (HEAD, LOCK):
            try:
                (src / extra).unlink()
            except OSError:
                pass
    try:
        (dst / STATE).unlink()
    except OSError:
        pass
    return {"removed": len(removed), "kept": kept}


# ================================================================ sizing, alerts
def estimate(cold_stats: Optional[dict], hot_stats: Optional[dict], cold_days: float) -> dict:
    """Lines per day from the last full archived days (else the hot tier's span), cold bytes per line as measured once the archive
    holds 100k lines (else LINE_BYTES), and what that makes per day, per year and for the configured retention."""
    lpd, basis = None, None
    days = [d for d in (cold_stats or {}).get("days", []) if d.get("lines")]
    if days:
        recent = days[1:][-7:] if len(days) > 1 else days    # the last 7 full days; the first archived day may be partial
        lpd, basis = sum(d["lines"] for d in recent) / len(recent), "cold"
    hs = hot_stats or {}
    if hs.get("events") and hs.get("oldest") and hs.get("newest"):
        try:
            span = (datetime.fromisoformat(hs["newest"]) - datetime.fromisoformat(hs["oldest"])).total_seconds()
        except ValueError:
            span = 0
        if span >= 3600:
            hot_lpd = hs["events"] / span * 86400
            if lpd is None or len(days) < 2:
                lpd, basis = hot_lpd, "hot"
    cs = cold_stats or {}
    measured = bool(cs.get("bytes_per_line")) and (cs.get("lines") or 0) >= 100_000
    bpl = float(cs["bytes_per_line"]) if measured else LINE_BYTES
    if lpd is None:
        return {"basis": None, "bytes_per_line": bpl, "bpl_measured": measured}
    daily = lpd * bpl
    return {"basis": basis, "lines_per_day": int(lpd), "lines_per_min": int(lpd / 1440), "bytes_per_line": round(bpl, 2), "bpl_measured": measured,
            "daily_bytes": int(daily), "yearly_bytes": int(daily * 365), "retention_bytes": int(daily * max(0.0, cold_days))}


def _pct(used: float, total: float) -> Optional[float]:
    return round(100.0 * used / total, 1) if total else None


def disk_pct(disk: Optional[dict]) -> Optional[float]:
    """How full a disk is as df says it (used / (used + available)): blocks reserved for root do not count as room."""
    if not disk:
        return None
    used = disk.get("used")
    if used is None:
        used = disk.get("total", 0) - disk.get("free", 0)
    return _pct(used, used + disk.get("free", 0))


def alerts(rep: dict, paused: Optional[dict] = None, check: Optional[dict] = None) -> list:
    """The storage alerts of a LogTiers report: archive disk / budget at 80 % (warn) and 90 % (crit), the archive paused on a full
    disk (crit), a retention the archive cannot hold (warn), the hot disk at 80 / 90 %, an integrity problem (crit)."""
    out = []
    cold = rep.get("cold") or {}
    if cold and cold.get("enabled"):
        disk = cold.get("disk") or {}
        pd = disk_pct(disk)
        pc = cold.get("pct")
        pct = max([x for x in (pd, pc) if x is not None], default=None)
        if paused:
            out.append({"code": "cold_full", "level": "crit", "tier": "cold", "pct": pct, "need": paused.get("need"), "free": paused.get("free"),
                        "day": paused.get("day")})
        if pct is not None and pct >= CRIT_PCT:
            out.append({"code": "cold_90", "level": "crit", "tier": "cold", "pct": pct})
        elif pct is not None and pct >= WARN_PCT:
            out.append({"code": "cold_80", "level": "warn", "tier": "cold", "pct": pct})
        est = rep.get("estimate") or {}
        cap = cold.get("cap_bytes") or 0
        room = (cold.get("bytes") or 0) + (disk.get("free") or 0) if disk else 0
        hold = min(x for x in (cap, room) if x) if (cap or room) else 0
        if est.get("retention_bytes") and hold and est["retention_bytes"] > hold * 1.0:
            out.append({"code": "cold_undersized", "level": "warn", "tier": "cold", "need": est["retention_bytes"], "room": hold,
                        "days_fit": int(hold / est["daily_bytes"]) if est.get("daily_bytes") else None})
    hot = rep.get("hot") or {}
    pd = disk_pct(hot.get("disk"))
    if pd is not None and pd >= CRIT_PCT:
        out.append({"code": "hot_disk_90", "level": "crit", "tier": "hot", "pct": pd})
    elif pd is not None and pd >= WARN_PCT:
        out.append({"code": "hot_disk_80", "level": "warn", "tier": "hot", "pct": pd})
    if check and not check.get("ok"):
        codes = sorted({p["code"] for p in check.get("problems", []) if p["code"] not in WARN_ONLY})
        out.append({"code": "archive_integrity", "level": "crit", "tier": "cold", "problems": codes,
                    "days": sorted({p.get("day") for p in check.get("problems", []) if p.get("day")})[:20]})
    return out


ALERT_TEXT = {
    "cold_80": ("Log archive disk at {pct}%", "The raw-log archive ({dir}) is at {pct}% of its disk or budget. Archives younger than the "
                "retention ({days} days) are never deleted for space: add space (or lower cold_retention_days on purpose)."),
    "cold_90": ("Log archive disk at {pct}% - act now", "The raw-log archive ({dir}) is at {pct}%. When it is full, archiving pauses and the "
                "days stay on the hot disk until there is room; nothing is deleted for space."),
    "cold_full": ("Log archiving paused: archive disk full", "Archiving day {day} needs about {need_h}, {free_h} free in {dir}. The day stays "
                  "on the hot disk and archiving resumes by itself once there is room. Add space to the archive disk."),
    "cold_undersized": ("Log archive too small for its retention", "At the current rate ({lpm} lines/min) {days} days of archive need "
                        "about {need_h}; the archive holds about {room_h} ({fit} days)."),
    "hot_disk_80": ("Hot log disk at {pct}%", "The hot raw-log disk ({hdir}) is at {pct}%."),
    "hot_disk_90": ("Hot log disk at {pct}% - act now", "The hot raw-log disk ({hdir}) is at {pct}%. If archiving is paused, the hot days "
                    "stay here until the archive has room."),
    "archive_integrity": ("Log archive verification failed", "The archive manifest check found: {problems} (days {days}). "
                          "System › Storage › Verify shows the details."),
}


def fmt_bytes(n) -> str:
    v, i = float(n or 0), 0
    while v >= 1024 and i < 4:
        v /= 1024
        i += 1
    return f"{v:.1f} {['B', 'KB', 'MB', 'GB', 'TB'][i]}"


def alert_text(a: dict, rep: dict) -> tuple:
    title, body = ALERT_TEXT.get(a["code"], (a["code"], a["code"]))
    est = rep.get("estimate") or {}
    kw = {"pct": a.get("pct"), "dir": (rep.get("cold") or {}).get("dir", ""), "hdir": (rep.get("hot") or {}).get("dir", ""),
          "days": int(rep.get("cold_retention_days") or 0), "day": a.get("day", ""), "need_h": fmt_bytes(a.get("need")),
          "free_h": fmt_bytes(a.get("free")), "room_h": fmt_bytes(a.get("room")), "fit": a.get("days_fit"), "lpm": est.get("lines_per_min"),
          "problems": ", ".join(a.get("problems") or [])}
    kw["days"] = kw["days"] if a["code"] != "archive_integrity" else ", ".join(a.get("days") or []) or "-"
    try:
        return title.format(**kw), body.format(**kw)
    except (KeyError, IndexError, ValueError):
        return title, body


# ================================================================ background audit jobs (verify, export, audit search)
class Busy(Exception):
    pass


class Jobs:
    """One audit job at a time in this process, with progress and cancel; results and exported files in out_dir (exports older
    than keep_days, beyond keep_files, are removed when the next job starts). The list survives a restart (jobs.json); a job that
    was running then is marked interrupted."""

    def __init__(self, out_dir, keep_days: int = 7, keep_files: int = 10):
        self.dir = Path(out_dir)
        self.keep_days, self.keep_files = keep_days, keep_files
        self.lock = threading.Lock()
        self.jobs: "OrderedDict[str, dict]" = OrderedDict()
        self._cancel: dict = {}
        for j in self._load():
            if j.get("status") == "running":
                j.update(status="interrupted", ended=now_iso())
            self.jobs[j["id"]] = j

    def _load(self) -> list:
        try:
            v = json.loads((self.dir / "jobs.json").read_text(encoding="utf-8"))
            return [j for j in v if isinstance(j, dict) and j.get("id")] if isinstance(v, list) else []
        except (OSError, ValueError):
            return []

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(self.dir / "jobs.json", json.dumps(list(self.jobs.values())[-30:], ensure_ascii=False))
        except OSError:
            pass

    def running(self) -> Optional[dict]:
        return next((j for j in self.jobs.values() if j["status"] == "running"), None)

    def list(self) -> list:
        with self.lock:
            return [dict(j) for j in reversed(self.jobs.values())]

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def cancel(self, jid: str) -> bool:
        ev = self._cancel.get(jid)
        if ev is None:
            return False
        ev.set()
        return True

    def prune(self) -> None:
        cut = time.time() - self.keep_days * 86400
        try:
            files = sorted((p for p in self.dir.iterdir() if p.is_file() and p.name.startswith("watchover-") and not p.name.endswith(".part")),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return
        exports = [p for p in files if not p.name.endswith(".sha256")]
        for i, p in enumerate(exports):
            if i >= self.keep_files or p.stat().st_mtime < cut:
                for q in (p, p.with_name(p.name + ".sha256")):
                    try:
                        q.unlink()
                    except OSError:
                        pass
        for p in self.dir.glob("*.part"):
            try:
                if p.stat().st_mtime < time.time() - 86400:
                    p.unlink()
            except OSError:
                pass

    def path(self, name: str) -> Optional[Path]:
        if not re.match(r"^watchover-[A-Za-z0-9._-]{1,160}$", name or "") or name.endswith(".part"):
            return None
        p = self.dir / name
        return p if p.is_file() else None

    def start(self, kind: str, params: dict, user: str, fn: Callable) -> dict:
        """fn(progress(fraction), cancel_event, out_dir) -> result dict, run in a thread of its own."""
        with self.lock:
            if self.running():
                raise Busy()
            self.dir.mkdir(parents=True, exist_ok=True)
            self.prune()
            jid = uuid.uuid4().hex[:12]
            job = {"id": jid, "kind": kind, "params": params, "user": user, "status": "running", "progress": 0.0, "started": now_iso(),
                   "ended": "", "result": None, "error": None}
            self.jobs[jid] = job
            while len(self.jobs) > 30:
                self.jobs.popitem(last=False)
            ev = self._cancel[jid] = threading.Event()
            self._save()

        def prog(x: float) -> None:
            job["progress"] = round(max(0.0, min(1.0, float(x))), 4)

        def run() -> None:
            try:
                res = fn(prog, ev, self.dir)
                job.update(status="done", result=res, progress=1.0)
            except Cancelled:
                job.update(status="cancelled")
            except AuditError as e:
                job.update(status="failed", error={"code": e.code, "detail": e.detail})
            except Exception as e:  # noqa: BLE001 - the job says what happened
                job.update(status="failed", error={"code": "internal", "detail": f"{type(e).__name__}: {str(e)[:300]}"})
            finally:
                job["ended"] = now_iso()
                self._cancel.pop(jid, None)
                with self.lock:
                    self._save()
        threading.Thread(target=run, name=f"audit-{kind}", daemon=True).start()
        return dict(job)


# ================================================================ where the tiers live (System › Storage)
def storage_paths(mode: str) -> dict:
    """Per tier: whether the operator gave its place by hand, the host path (Docker: from .env, which reaches the container), the
    directory this process uses. openshift: a PVC each (the chart); updater / manual: Docker; inprocess: WATCHOVER_LOG_*_DIR."""
    from . import logtiers
    out = {}
    for k, d in (("hot", logtiers.hot_dir()), ("cold", logtiers.cold_dir())):
        K = k.upper()
        host = (os.environ.get(f"WATCHOVER_LOG_{K}_PATH") or "").strip()
        envdir = (os.environ.get(f"WATCHOVER_LOG_{K}_DIR") or "").strip()
        if mode == "openshift":
            out[k] = {"configured": True, "kind": "pvc", "host_path": "", "label": f"PVC watchover-logs-{k}", "dir": str(d)}
        elif mode in ("updater", "manual"):
            out[k] = {"configured": bool(host), "kind": "host" if host else "volume", "host_path": host,
                      "label": host or f"/data/logs/{k}", "dir": str(d)}
        else:
            out[k] = {"configured": bool(envdir), "kind": "dir", "host_path": envdir, "label": envdir or str(d), "dir": str(d)}
    return out
