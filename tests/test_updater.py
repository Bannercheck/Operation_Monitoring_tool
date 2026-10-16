"""In-app updates: package checks (zip-slip, symlinks, sizes, versions, bundles), signatures, the updater's state machine with a fake
engine CLI on PATH (apply, build failure, failed health check with auto-rollback, rollback with and without the database, retention),
the host-side guard of watchover.sh, and that no container but the updater ever gets the engine socket."""
import base64
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from watchover import release, updates as U
from watchover.updater import Updater, is_local

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env bash
# fake engine CLI: images in $FAKE_DIR/images, the running dashboard version in $FAKE_DIR/running, versions in $FAKE_DIR/broken never answer
echo "docker $* [tag=${WATCHOVER_TAG:-}]" >> "$FAKE_DIR/calls.log"
imgs="$FAKE_DIR/images"; touch "$imgs"
envtag() { sed -n 's/^WATCHOVER_TAG=//p' .env 2>/dev/null | head -1; }
case "$1" in
  image) case "$2" in
      inspect) grep -qx "$3" "$imgs"; exit $? ;;
      rm) shift 2; for i in "$@"; do grep -vx "$i" "$imgs" > "$imgs.t" || true; mv "$imgs.t" "$imgs"; done; exit 0 ;;
    esac ;;
  load) cat > /dev/null; v=$(cat "$FAKE_DIR/bundle_version"); printf 'watchover:%s\nwatchover-receiver:%s\n' "$v" "$v" >> "$imgs"; exit 0 ;;
  info) case "${3:-}" in *NCPU*) echo 8 ;; *MemTotal*) echo 17179869184 ;; esac; exit 0 ;;
  compose)
    sub=""; for a in "$@"; do case "$a" in version|build|up|exec|ps|config|stop|rm|restart|logs) sub="$a"; break ;; esac; done
    case "$sub" in
      version) echo "Docker Compose version v2.29" ;;
      build) [ -f "$FAKE_DIR/build_fail" ] && { echo "build error" ; exit 1; }
             t="${WATCHOVER_TAG:-$(envtag)}"
             for s in "$@"; do case "$s" in dashboard) echo "watchover:$t" >> "$imgs" ;; receiver) echo "watchover-receiver:$t" >> "$imgs" ;; esac; done ;;
      up) case "$*" in *"--no-deps updater"*) echo "UPDATER RECREATED" >> "$FAKE_DIR/calls.log" ;; *) envtag > "$FAKE_DIR/running" ;; esac ;;
      exec) case "$*" in
              *api/health*) r=$(cat "$FAKE_DIR/running" 2>/dev/null); grep -qx "$r" "$FAKE_DIR/broken" 2>/dev/null && exit 7; echo "{\"ok\":true,\"version\":\"$r\"}" ;;
              *pg_dump*) echo "-- dump" ;;
              *) cat > /dev/null ;;
            esac ;;
      ps) [[ "$*" == *--format* ]] && echo "dashboard healthy" ;;
      config) echo '{"volumes":{"watchover-data":{"name":"watchover_watchover-data"}}}' ;;
    esac ;;
esac
exit 0
'''
STAGE = ("watchover.sh", "docker-compose.yml", "docker-compose.edge.yml", "docker-compose.loghot.yml", "docker-compose.logcold.yml",
         "docker-compose.otlp.yml", ".env.docker.example", "Dockerfile", "Dockerfile.updater", "src/watchover/updates.py", "src/watchover/updater.py")


def make_tree(d: Path, version: str, extra: dict = None, drop: tuple = ()) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for f in STAGE:
        (d / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, d / f)
    (d / "src" / "watchover" / "__init__.py").write_text(f'"""Watchover."""\n\n__version__ = "{version}"\n')
    (d / "pyproject.toml").write_text(f'[project]\nname = "watchover"\nversion = "{version}"\n')
    disp = U.display(version)
    (d / "CHANGELOG.md").write_text(f"# Watchover sürüm geçmişi\n\n## {disp} · 2026-10-16 · güncelleme\n\n- {disp} notu\n")
    (d / "README.md").write_text("readme\n")
    (d / "docs").mkdir(exist_ok=True)
    (d / "docs" / "old.md").write_text("old\n")
    for k, v in (extra or {}).items():
        (d / k).parent.mkdir(parents=True, exist_ok=True)
        (d / k).write_text(v)
    for k in drop:
        (d / k).unlink()
    return d


def zip_tree(tree: Path, out: Path, prefix: str = "hackathon/", comment: bytes = b"") -> Path:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(tree.rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo(prefix + p.relative_to(tree).as_posix())
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | (0o755 if os.access(p, os.X_OK) else 0o644)) << 16
                zf.writestr(info, p.read_bytes())
        zf.comment = comment
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An install folder at v1.53.0 (captured), a spool, a fake engine on PATH."""
    install = make_tree(tmp_path / "install", "1.53.0", extra={"scripts/old.sh": "echo old\n"})
    (install / ".env").write_text("WATCHOVER_ENGINE=docker\nWATCHOVER_TAG=1.53.0\nWATCHOVER_IMAGE=watchover\nPOSTGRES_PASSWORD=s3cr3t-pw\nWATCHOVER_UPDATER=1\nWATCHOVER_BACKUP_DIR=\n")
    (install / "backups").mkdir()
    (install / "backups" / "nightly.tgz").write_text("keep me")
    (install / "certs").mkdir()
    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    (fake / "bin" / "docker").write_text(FAKE)
    (fake / "bin" / "docker").chmod(0o755)
    (fake / "images").write_text("watchover:1.53.0\nwatchover-receiver:1.53.0\nwatchover-updater:1\n")
    (fake / "running").write_text("1.53.0\n")
    monkeypatch.setenv("PATH", f"{fake / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    monkeypatch.setenv("WATCHOVER_HEALTH_POLL", "0.2")
    monkeypatch.delenv("WATCHOVER_UPDATE_PUBKEY", raising=False)
    up = Updater(install, tmp_path / "spool")
    up.prepare()
    U.write_json(up.spool / "dash" / "config.json", {"keep": 5, "health_s": 3})
    assert up.capture() == "1.53.0"
    return up, fake, tmp_path


def request(up: Updater, pkg: Path, rid: str, **kw) -> dict:
    d = up.spool / "inbox" / rid
    d.mkdir(parents=True)
    shutil.copy(pkg, d / ("package.tar" if pkg.suffix == ".tar" else "package.zip"))
    U.write_json(up.spool / "requests" / f"{rid}.json", {"id": rid, "kind": "apply", "user": "ops@firma.com", **kw})
    out = up.once()
    assert len(out) == 1
    return out[0]


def steps(st: dict) -> dict:
    return {s["name"]: s["status"] for s in st["steps"]}


# ---------------------------------------------------------------- package checks
def test_zip_inspection_any_prefix_version_and_notes(tmp_path):
    tree = make_tree(tmp_path / "t", "1.54.0")
    for prefix in ("hackathon/", "watchover-1.54.0/", ""):
        info = U.inspect(zip_tree(tree, tmp_path / "p.zip", prefix, comment=b"2fca251d3921ded0bd521f4dbda1cd5ce88fc666"), current="1.53.0")
        assert info["version"] == "1.54.0" and info["kind"] == "zip" and info["prefix"] == prefix and info["direction"] == "upgrade"
        assert info["git"] == "2fca251d3921" and info["notes"]["lines"] == ["v1.54 notu"] and len(info["sha256"]) == 64
        assert info["signature"] == {"required": False, "status": "none", "fingerprint": ""}
    assert U.inspect(tmp_path / "p.zip", current="1.60.0")["direction"] == "downgrade"
    assert U.inspect(tmp_path / "p.zip", current="1.54.0")["direction"] == "same"
    git = shutil.which("git")
    if git and (ROOT / ".git").exists():                                   # the real thing: git archive of this repository
        out = tmp_path / "real.zip"
        subprocess.run([git, "-C", str(ROOT), "archive", "--format=zip", "--prefix=hackathon/", "-o", str(out), "HEAD"], check=True)
        info = U.inspect(out, require_updater=False)
        assert info["prefix"] == "hackathon/" and U.VERSION_RE.match(info["version"]) and re.fullmatch(r"[0-9a-f]{12}", info["git"])


def _zip_with(tmp_path, name, data=b"x", mode=0o644, base=None):
    out = tmp_path / "evil.zip"
    shutil.copy(base, out)
    with zipfile.ZipFile(out, "a") as zf:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = mode << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, data)
    return out


@pytest.mark.filterwarnings("ignore:Duplicate name")
@pytest.mark.parametrize("name,mode,code", [
    ("hackathon/../../etc/cron.d/x", stat.S_IFREG | 0o644, "unsafe_path"),
    ("/etc/passwd", stat.S_IFREG | 0o644, "unsafe_path"),
    ("hackathon\\..\\evil", stat.S_IFREG | 0o644, "unsafe_path"),
    ("C:/evil", stat.S_IFREG | 0o644, "unsafe_path"),
    ("hackathon/link", stat.S_IFLNK | 0o777, "symlink"),
    ("hackathon/fifo", stat.S_IFIFO | 0o644, "special_file"),
    ("hackathon/README.md", stat.S_IFREG | 0o644, "duplicate"),
])
def test_zip_refusals(tmp_path, name, mode, code):
    good = zip_tree(make_tree(tmp_path / "t", "1.54.0"), tmp_path / "good.zip")
    with pytest.raises(U.PackageError) as e:
        U.inspect(_zip_with(tmp_path, name, b"/etc/passwd", mode, base=good))
    assert e.value.code == code


def test_size_ratio_required_files_and_versions(tmp_path, monkeypatch):
    tree = make_tree(tmp_path / "t", "1.54.0")
    good = zip_tree(tree, tmp_path / "good.zip")
    with pytest.raises(U.PackageError) as e:                                   # upload cap
        U.inspect(good, max_bytes=100)
    assert e.value.code == "too_large"
    monkeypatch.setenv("WATCHOVER_UPDATE_MAX_MB", "1")
    assert U.max_upload_bytes() == 16 * 1048576                               # floor of 16 MB
    bomb = _zip_with(tmp_path, "hackathon/bomb.bin", b"\0" * (8 << 20), base=good)
    with pytest.raises(U.PackageError) as e:                                   # compression ratio
        U.inspect(bomb)
    assert e.value.code == "zip_bomb"
    monkeypatch.setattr(U, "MAX_TREE_BYTES", 1000)
    with pytest.raises(U.PackageError) as e:
        U.inspect(good)
    assert e.value.code == "too_large"
    monkeypatch.undo()
    for drop, code in (("watchover.sh", "not_watchover"), ("src/watchover/updater.py", "no_updater"), ("Dockerfile.updater", "no_updater")):
        t = make_tree(tmp_path / f"t-{drop.replace('/', '_')}", "1.54.0", drop=(drop,))
        with pytest.raises(U.PackageError) as e:
            U.inspect(zip_tree(t, tmp_path / "x.zip"))
        assert e.value.code == code
    assert U.inspect(zip_tree(make_tree(tmp_path / "t2", "1.54.0", drop=("Dockerfile.updater",)), tmp_path / "y.zip"), require_updater=False)["version"] == "1.54.0"
    t = make_tree(tmp_path / "t3", "1.54.0")
    (t / "pyproject.toml").write_text('[project]\nversion = "1.55.0"\n')
    with pytest.raises(U.PackageError) as e:                                   # images are tagged by pyproject: it must agree
        U.inspect(zip_tree(t, tmp_path / "z.zip"))
    assert e.value.code == "version_mismatch"
    (t / "pyproject.toml").write_text('[project]\nversion = "dev"\n')
    (t / "src" / "watchover" / "__init__.py").write_text('__version__ = "dev"\n')
    with pytest.raises(U.PackageError) as e:
        U.inspect(zip_tree(t, tmp_path / "z.zip"))
    assert e.value.code == "no_version"
    (tmp_path / "junk.bin").write_bytes(b"\0" * 2048)
    with pytest.raises(U.PackageError) as e:
        U.inspect(tmp_path / "junk.bin")
    assert e.value.code == "not_a_package"
    assert U.compare("1.10.0", "1.9.2") == 1 and U.compare("1.53.0", "1.53.0") == 0 and U.display("1.53.0") == "v1.53"


def make_bundle(tree: Path, out: Path, version: str, evil: str = "") -> Path:
    with tarfile.open(out, "w") as tf:
        def add(name, data):
            ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
        add("MANIFEST", f"version={version}\nimages=watchover:{version}\n".encode())
        add("images.tar.gz", __import__("gzip").compress(b"fake images"))
        add("volumes/watchover-ollama.tar", b"")
        for p in sorted(tree.rglob("*")):
            if p.is_file():
                ti = tf.gettarinfo(str(p), "source/" + p.relative_to(tree).as_posix())
                with open(p, "rb") as f:
                    tf.addfile(ti, f)
        if evil == "symlink":
            ti = tarfile.TarInfo("source/evil"); ti.type = tarfile.SYMTYPE; ti.linkname = "/etc/passwd"; tf.addfile(ti)
        elif evil == "slip":
            add("source/../../x", b"x")
        elif evil == "extra":
            add("run.sh", b"rm -rf /")
    return out


def test_bundle_inspection(tmp_path):
    tree = make_tree(tmp_path / "t", "1.54.0")
    info = U.inspect(make_bundle(tree, tmp_path / "b.tar", "1.54.0"), current="1.53.0")
    assert info["kind"] == "bundle" and info["version"] == "1.54.0" and info["prefix"] == "source/"
    for evil, code in (("symlink", "symlink"), ("slip", "unsafe_path"), ("extra", "unexpected_member")):
        with pytest.raises(U.PackageError) as e:
            U.inspect(make_bundle(tree, tmp_path / f"{evil}.tar", "1.54.0", evil))
        assert e.value.code == code
    with pytest.raises(U.PackageError) as e:
        U.inspect(make_bundle(tree, tmp_path / "m.tar", "1.55.0"))
    assert e.value.code == "version_mismatch"
    dest = tmp_path / "x"
    files = U.extract(tmp_path / "b.tar", info, dest)
    assert "watchover.sh" in files and os.access(dest / "watchover.sh", os.X_OK) and (dest / "src/watchover/__init__.py").is_file()


# ---------------------------------------------------------------- signatures
def test_signatures_and_release_helpers(tmp_path, monkeypatch):
    r = release.keygen(tmp_path / "keys")
    assert oct(Path(r["key"]).stat().st_mode)[-3:] == "600" and len(base64.b64decode(r["pubkey"])) == 32
    with pytest.raises(FileExistsError):
        release.keygen(tmp_path / "keys")
    pkg = zip_tree(make_tree(tmp_path / "t", "1.54.0"), tmp_path / "w.zip")
    sig = release.sign_package(pkg, Path(r["key"]))
    pub = U.load_pubkey(r["pubkey"])
    assert pub == U.load_pubkey(Path(r["pub"]).read_text()) == U.load_pubkey(r["pub"]) == U.load_pubkey(pub.hex())
    info = U.inspect(pkg, sig=sig.read_text(), pubkey=pub)
    assert info["signature"] == {"required": True, "status": "ok", "fingerprint": U.fingerprint(pub)}
    assert release.verify_package(pkg, r["pubkey"]) and release.main(["verify", str(pkg), "--pub", r["pub"]]) == 0
    for bad, code in (("", "signature_missing"), (base64.b64encode(b"\0" * 64).decode(), "signature_bad"), ("not base64!", "signature_bad")):
        with pytest.raises(U.PackageError) as e:
            U.inspect(pkg, sig=bad, pubkey=pub)
        assert e.value.code == code
    other = U.load_pubkey(release.keygen(tmp_path / "k2")["pubkey"])
    with pytest.raises(U.PackageError) as e:                                   # signed, but with another key
        U.inspect(pkg, sig=sig.read_text(), pubkey=other)
    assert e.value.code == "signature_bad"
    pkg.write_bytes(pkg.read_bytes() + b"\0")                                 # tampered after signing
    with pytest.raises(U.PackageError):
        U.inspect(pkg, sig=sig.read_text(), pubkey=pub)
    assert U.inspect(pkg, sig=sig.read_text())["signature"]["status"] == "unverified"   # no key configured: shown, not enforced
    monkeypatch.setenv("WATCHOVER_UPDATE_PUBKEY", r["pubkey"])
    assert U.configured_pubkey() == pub
    monkeypatch.delenv("WATCHOVER_UPDATE_PUBKEY")
    inst = tmp_path / "inst"; inst.mkdir()
    (inst / ".env").write_text(f"WATCHOVER_UPDATE_PUBKEY={r['pubkey']}  # release key\n")
    assert U.configured_pubkey(inst, env={}) == pub
    (inst / ".env").write_text("")
    shutil.copy(r["pub"], inst / "update-pubkey.pem")
    assert U.configured_pubkey(inst, env={}) == pub
    with pytest.raises(ValueError):
        U.load_pubkey("AAAA")


def test_redaction():
    assert U.redact("\x1b[1;36m▸ POSTGRES_PASSWORD=hunter2 MCP_API_KEY: abc\x1b[0m") == "▸ POSTGRES_PASSWORD=••• MCP_API_KEY: •••"
    assert U.redact("building image watchover:1.54.0") == "building image watchover:1.54.0"


# ---------------------------------------------------------------- the state machine (fake engine)
def test_apply_switch_rollback_db_and_retention(env):
    up, fake, tmp = env
    idx = up.index()
    assert idx["current"] == "1.53.0" and (up.rels / "1.53.0" / "tree" / "watchover.sh").is_file()
    assert not (up.rels / "1.53.0" / "tree" / ".env").exists() and not (up.rels / "1.53.0" / "tree" / "backups").exists()
    # 1.53 -> 1.54: a file removed, a file added, local files untouched
    t154 = make_tree(tmp / "r154", "1.54.0", extra={"scripts/new.sh": "echo new\n", "data/live/events.jsonl": "{}\n"}, drop=("docs/old.md",))
    st = request(up, zip_tree(t154, tmp / "p154.zip", comment=b"a" * 40), "20261016-100000-aaaaaa", name="hackathon.zip")
    assert st["status"] == "done", st
    assert steps(st) == {n: "ok" for n in ("received", "validated", "backup", "build", "switch", "start", "health", "done")}
    inst = up.install
    assert U.read_version(inst) == "1.54.0" and (inst / "scripts" / "new.sh").exists() and not (inst / "docs" / "old.md").exists()
    assert not (inst / "docs").exists() and (inst / "scripts" / "old.sh").exists() is False
    assert (inst / ".release-rev").read_text().strip() == "aaaaaaa" and not (inst / "data").exists()       # data/ is the owner's, never synced
    assert (inst / "backups" / "nightly.tgz").read_text() == "keep me" and "POSTGRES_PASSWORD=s3cr3t-pw" in (inst / ".env").read_text()
    assert "WATCHOVER_TAG=1.54.0" in (inst / ".env").read_text() and (fake / "running").read_text().strip() == "1.54.0"
    assert (up.rels / "1.53.0" / "pre-update.tgz").is_file() and (up.rels / "1.53.0" / "tree" / "docs" / "old.md").is_file()
    calls = (fake / "calls.log").read_text()
    assert "compose build" in calls and "[tag=1.54.0]" in calls and "pg_dump" in calls and "UPDATER RECREATED" not in calls
    assert calls.index("pg_dump") < calls.index("compose build") < calls.index("exec -T dashboard curl")
    idx = up.index()
    assert idx["current"] == "1.54.0" and idx["previous"] == "1.53.0" and idx["releases"]["1.54.0"]["source"] == "zip"
    assert idx["releases"]["1.53.0"]["backup"]["before"] == "1.54.0" and not (up.spool / "inbox" / "20261016-100000-aaaaaa").exists()
    log = (up.spool / "jobs" / "20261016-100000-aaaaaa.log").read_text()
    assert "s3cr3t-pw" not in log and "$ ./watchover.sh backup --no-logs --out" in log
    view = {r["version"]: r for r in json.loads((up.spool / "releases.json").read_text())["releases"]}
    assert view["1.54.0"]["current"] and view["1.53.0"]["previous"] and view["1.53.0"]["backup"] and view["1.54.0"]["notes"]["lines"] == ["v1.54 notu"]
    assert view["1.53.0"]["images"] is True
    # build failure: nothing switched, the half-made release is gone
    (fake / "build_fail").write_text("")
    st = request(up, zip_tree(make_tree(tmp / "r156", "1.56.0"), tmp / "p156.zip"), "20261016-100500-bbbbbb")
    assert st["status"] == "failed" and st["error"]["code"] == "build_failed" and steps(st)["switch"] == "pending"
    assert U.read_version(inst) == "1.54.0" and not (up.rels / "1.56.0").exists() and "1.56.0" not in up.index()["releases"]
    (fake / "build_fail").unlink()
    # 1.55 never answers /api/health: auto-rollback to 1.54 with its kept images
    (fake / "broken").write_text("1.55.0\n")
    st = request(up, zip_tree(make_tree(tmp / "r155", "1.55.0"), tmp / "p155.zip"), "20261016-101000-cccccc")
    assert st["status"] == "rolled_back" and st["error"]["code"] == "health_failed", st
    assert steps(st)["health"] == "failed" and steps(st)["rollback"] == "ok"
    assert U.read_version(inst) == "1.54.0" and (fake / "running").read_text().strip() == "1.54.0" and "WATCHOVER_TAG=1.54.0" in (inst / ".env").read_text()
    idx = up.index()
    assert idx["current"] == "1.54.0" and idx["previous"] == "1.55.0" and idx["releases"]["1.55.0"]["failed_at"]
    # the same failure without auto-rollback stays failed on the new version (the owner chose so)
    st = request(up, zip_tree(make_tree(tmp / "r157", "1.57.0"), tmp / "p157.zip"), "20261016-101500-dddddd", auto_rollback=False)
    (fake / "broken").write_text("1.57.0\n")
    assert st["status"] == "done"                                             # 1.57 was healthy when it ran
    # rollback to 1.53 keeping the data: images present, nothing rebuilt, no restore
    (fake / "calls.log").write_text("")
    U.write_json(up.spool / "requests" / "20261016-102000-eeeeee.json", {"id": "20261016-102000-eeeeee", "kind": "rollback", "version": "1.53.0"})
    st = up.once()[0]
    assert st["status"] == "done" and steps(st)["build"] == "skipped" and "restore_db" not in steps(st), st
    calls = (fake / "calls.log").read_text()
    assert "compose build" not in calls and "DROP SCHEMA" not in calls and U.read_version(inst) == "1.53.0" and (inst / "docs" / "old.md").exists()
    assert (up.rels / "1.57.0" / "pre-update.tgz").is_file()                  # leaving 1.57 took its backup
    # rollback to 1.57 WITH its database backup (taken when 1.57 was left)
    (fake / "broken").write_text("")
    (fake / "calls.log").write_text("")
    U.write_json(up.spool / "requests" / "20261016-102500-ffffff.json", {"id": "20261016-102500-ffffff", "kind": "rollback", "version": "1.57.0", "restore_db": True})
    st = up.once()[0]
    assert st["status"] == "done" and steps(st)["restore_db"] == "ok", st
    calls = (fake / "calls.log").read_text()
    assert "DROP SCHEMA public CASCADE" in calls and calls.index("DROP SCHEMA") < calls.rindex("exec -T dashboard curl")
    assert U.read_version(inst) == "1.57.0"
    # retention: keep 2 -> only current (1.57) and previous (1.53) stay, the others go with their images
    U.write_json(up.spool / "dash" / "config.json", {"keep": 2, "health_s": 3})
    assert sorted(up.prune(up.cfg()["keep"])) == ["1.54.0", "1.55.0"]
    assert sorted(up.index()["releases"]) == ["1.53.0", "1.57.0"] and not (up.rels / "1.54.0").exists()
    assert "watchover:1.54.0" not in (fake / "images").read_text() and "watchover:1.57.0" in (fake / "images").read_text()
    hist = [json.loads(l) for l in (up.spool / "history.jsonl").read_text().splitlines()]
    assert [h["status"] for h in hist] == ["done", "failed", "rolled_back", "done", "done", "done"]


def test_rollback_with_db_failure_restores_both_ways(env):
    """A rollback that restored the database and then fails its health check brings the code AND the data of before back."""
    up, fake, tmp = env
    st = request(up, zip_tree(make_tree(tmp / "r154", "1.54.0"), tmp / "p.zip"), "20261016-110000-aaaaaa")
    assert st["status"] == "done"
    (fake / "broken").write_text("1.53.0\n")
    (fake / "calls.log").write_text("")
    U.write_json(up.spool / "requests" / "20261016-110500-bbbbbb.json", {"id": "20261016-110500-bbbbbb", "kind": "rollback", "version": "1.53.0", "restore_db": True})
    st = up.once()[0]
    assert st["status"] == "rolled_back" and steps(st)["restore_db"] == "ok" and steps(st)["rollback"] == "ok", st
    calls = (fake / "calls.log").read_text()
    assert calls.count("DROP SCHEMA") == 2 and U.read_version(up.install) == "1.54.0"


def test_bundle_apply_loads_images(env):
    up, fake, tmp = env
    (fake / "bundle_version").write_text("1.54.0")
    pkg = make_bundle(make_tree(tmp / "r154", "1.54.0"), tmp / "b.tar", "1.54.0")
    st = request(up, pkg, "20261016-120000-aaaaaa")
    assert st["status"] == "done" and "bundle" in [s for s in st["steps"] if s["name"] == "build"][0]["msg"], st
    calls = (fake / "calls.log").read_text()
    assert "docker load" in calls and "compose build" not in calls and "WATCHOVER_OFFLINE=1" in (up.install / ".env").read_text()


def test_requests_are_checked_and_signatures_enforced(env):
    up, fake, tmp = env
    for raw, name in (({"id": "../../etc", "kind": "apply"}, "x"), ({"id": "20261016-130000-aaaaaa", "kind": "shell"}, "20261016-130000-aaaaaa"),
                      ({"id": "20261016-130000-bbbbbb", "kind": "rollback", "version": "1.0; rm -rf /"}, "20261016-130000-bbbbbb"),
                      ({"id": "20261016-130000-cccccc", "kind": "apply"}, "20261016-130000-dddddd")):   # id must equal the file name
        U.write_json(up.spool / "requests" / f"{name}.json", raw)
    assert up.once() == [] and not list((up.spool / "requests").iterdir())
    r = release.keygen(tmp / "keys")
    with open(up.install / ".env", "a") as f:
        f.write(f"WATCHOVER_UPDATE_PUBKEY={r['pubkey']}\n")
    pkg = zip_tree(make_tree(tmp / "r154", "1.54.0"), tmp / "p.zip")
    st = request(up, pkg, "20261016-130500-eeeeee")
    assert st["status"] == "failed" and st["error"]["code"] == "signature_missing" and U.read_version(up.install) == "1.53.0"
    st = request(up, pkg, "20261016-131000-ffffff", sig=release.sign_package(pkg, Path(r["key"])).read_text())
    assert st["status"] == "done" and "signature ok" in [s for s in st["steps"] if s["name"] == "validated"][0]["msg"]
    hb = up.heartbeat() or json.loads((up.spool / "updater.json").read_text())
    assert hb["signing"]["required"] and hb["signing"]["fingerprint"] == r["fingerprint"]
    st = request(up, pkg, "20261016-131500-gggggg", sig=release.sign_package(pkg, Path(r["key"])).read_text())
    assert st["status"] == "failed" and st["error"]["code"] == "same_version"


def test_interrupted_job_and_local_files(env):
    up, fake, tmp = env
    U.write_json(up.spool / "state.json", {"id": "x", "status": "running", "step": "build"})
    (up.work / "busy").write_text("{}")
    up.recover()
    assert json.loads((up.spool / "state.json").read_text())["error"]["code"] == "interrupted" and not (up.work / "busy").exists()
    for rel in (".env", ".env.bak", "backups/x.tgz", "certs/watchover.key", "data/live/events.jsonl", ".releases/index.json", "web/node_modules/a.js",
                "src/watchover/__pycache__/x.pyc", "watchover-bundle-1.0.tar", ".streamlit/secrets.toml", "update-pubkey.pem"):
        assert is_local(rel), rel
    for rel in (".env.docker.example", "watchover.sh", "src/watchover/updater.py", ".streamlit/config.toml", "docs/x.md"):
        assert not is_local(rel), rel


def test_host_script_guard_and_new_commands(env):
    """watchover.sh: update refuses while the updater works; build / images / health / backup --out / updater off as the updater uses them."""
    up, fake, tmp = env
    inst = up.install
    run = lambda *a, **e: subprocess.run(["bash", str(inst / "watchover.sh"), *a], cwd=inst, capture_output=True, text=True, timeout=60, env=dict(os.environ, **e))
    (up.work / "busy").write_text('{"id": "x"}')
    r = run("update")
    assert r.returncode == 1 and "in-app update is running" in r.stderr
    assert run("update", WATCHOVER_IN_UPDATER="1").returncode == 0
    (up.work / "busy").unlink()
    r = run("start")
    assert r.returncode == 0 and "UPDATER RECREATED" in (fake / "calls.log").read_text()                 # outside: the updater is (re)started
    env_text = (inst / ".env").read_text()
    assert f"WATCHOVER_INSTALL_DIR={inst}" in env_text and "WATCHOVER_ENGINE_SOCK=/var/run/docker.sock" in env_text
    assert run("images", "1.53.0").returncode == 0 and run("images", "9.9.9").returncode == 1
    assert run("health", "1.53.0", "2").returncode == 0 and run("health", "9.9.9", "1").returncode == 1
    out = tmp / "b" / "pre.tgz"
    r = run("backup", "--no-logs", "--out", str(out))
    assert r.returncode == 0 and out.is_file() and (inst / "backups" / "nightly.tgz").exists()
    assert "env" in subprocess.run(["tar", "tzf", str(out)], capture_output=True, text=True).stdout
    r = run("updater", "off")
    assert r.returncode == 0 and "WATCHOVER_UPDATER=0" in (inst / ".env").read_text() and "rm -f updater" in (fake / "calls.log").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "watchover.sh")]).returncode == 0


# ---------------------------------------------------------------- the socket stays with the updater
def test_only_the_updater_mounts_the_engine_socket():
    import yaml  # noqa: F401  (PyYAML ships with streamlit's dependencies; skip cleanly otherwise)
    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    sock = re.compile(r"docker\.sock|podman\.sock|ENGINE_SOCK")
    for name, svc in doc["services"].items():
        vols = json.dumps(svc.get("volumes", []))
        if name == "updater":
            assert sock.search(vols) and svc.get("network_mode") == "none" and not svc.get("ports") and svc.get("profiles") == ["updater"]
            assert "label=disable" in svc.get("security_opt", []) and svc["environment"]["WATCHOVER_IN_UPDATER"] == "1"
        else:
            assert not sock.search(vols), name
            assert not sock.search(json.dumps(svc.get("environment", {}))), name
    assert "watchover-updates:/updates" in doc["services"]["dashboard"]["volumes"]
    for f in (ROOT / "deploy" / "openshift").rglob("*.yaml"):                  # OpenShift: no engine socket, no host path at all
        text = f.read_text()
        assert not sock.search(text) and "hostPath" not in text, f
    for f in ("docker-compose.edge.yml", "docker-compose.loghot.yml", "docker-compose.logcold.yml", "docker-compose.otlp.yml"):
        assert not sock.search((ROOT / f).read_text()), f


def test_real_compose_accepts_the_updater(tmp_path):
    real = shutil.which("docker")
    if not real or subprocess.run([real, "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose not installed")
    for f in ("docker-compose.yml", ".env.docker.example"):
        shutil.copy(ROOT / f, tmp_path / f)
    (tmp_path / ".env").write_text((ROOT / ".env.docker.example").read_text() + f"WATCHOVER_INSTALL_DIR={tmp_path}\nWATCHOVER_ENGINE_SOCK=/run/podman/podman.sock\n")
    out = subprocess.run([real, "compose", "--profile", "updater", "config", "--format", "json"], cwd=tmp_path, capture_output=True, text=True,
                         env={k: v for k, v in os.environ.items() if k != "COMPOSE_FILE"})
    assert out.returncode == 0, out.stderr
    svc = json.loads(out.stdout)["services"]
    mounts = {v["target"]: v for v in svc["updater"]["volumes"]}
    assert mounts[str(tmp_path)]["source"] == str(tmp_path) and mounts["/run/podman/podman.sock"]["source"] == "/run/podman/podman.sock"
    assert svc["updater"]["working_dir"] == str(tmp_path) and svc["updater"]["environment"]["CONTAINER_HOST"] == "unix:///run/podman/podman.sock"
    assert all("sock" not in v["target"] for n, s in svc.items() if n != "updater" for v in s.get("volumes", []))


def test_schema_changes_are_additive():
    """Rollback keeps the data by default because migrations only add (CREATE TABLE IF NOT EXISTS, ADD COLUMN). A destructive statement
    needs an entry here and a note in docs/KURULUM_DOCKER.md (3b)."""
    allowed = {("logtiers.py", "DROP TABLE IF EXISTS live_batches")}           # v1.48: rows moved to the hot tier first; older than any rollback target
    bad = re.compile(r"(DROP\s+TABLE|DROP\s+COLUMN|RENAME\s+COLUMN|RENAME\s+TO|ALTER\s+COLUMN|TRUNCATE\s+TABLE)[^\"']*", re.I)
    found = set()
    for p in (ROOT / "src" / "watchover").rglob("*.py"):
        if p.name in ("i18n.py",):
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            code = line.split("#", 1)[0]
            for m in bad.finditer(code):
                if "_exec" in code or "execute" in code:
                    found.add((p.name, m.group(0).strip()))
    assert found <= allowed, found - allowed


def test_storage_request_moves_the_log_paths_through_watchover_sh(env):
    """System › Storage on Docker: a request of kind "storage" makes the updater run ./watchover.sh storage set with what the admin
    gave; its ::step lines move the job's steps; .env gets the paths; the copy, the final pass, the retention and the clean-up go
    through the image as throwaway containers. Bad requests (a system directory, nested paths, numbers out of range) never run."""
    up, fake, tmp = env
    hot, cold = tmp / "nvme" / "wo-hot", tmp / "sata" / "wo-arsiv"
    for rid, bad in (("stor-bad-01", {"hot": "/etc/wo"}), ("stor-bad-02", {"hot": str(hot), "cold": str(hot) + "/a"}),
                     ("stor-bad-03", {"cold": str(cold), "cold_days": 99999}), ("stor-bad-04", {"cold": "relative/x"}), ("stor-bad-05", {"hot_hours": 48})):
        U.write_json(up.spool / "requests" / f"{rid}.json", {"id": rid, "kind": "storage", "user": "x", **bad})
    assert up.once() == []
    U.write_json(up.spool / "requests" / "stor-ok-001.json", {"id": "stor-ok-001", "kind": "storage", "hot": str(hot), "cold": str(cold),
                                                               "hot_hours": 72, "cold_days": 365, "user": "ops@firma.com"})
    out = up.once()
    assert len(out) == 1 and out[0]["status"] == "done", json.dumps(out, indent=1)
    steps = {s["name"]: s["status"] for s in out[0]["steps"]}
    assert [s["name"] for s in out[0]["steps"]] == list(("received", "validated", "prepare", "copy", "switch", "start", "health", "cleanup", "done"))
    assert set(steps.values()) == {"ok"}
    envf = (up.install / ".env").read_text()
    assert f"WATCHOVER_LOG_HOT_PATH={hot}\n" in envf and f"WATCHOVER_LOG_COLD_PATH={cold}\n" in envf
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.loghot.yml:docker-compose.logcold.yml" in envf
    calls = (fake / "calls.log").read_text()
    assert f"-v watchover_watchover-data:/from -v {hot}:/to:z watchover:1.53.0 python -m watchover.logtiers relocate /from/logs/hot /to --check --uid 10001" in calls
    assert f"-v {cold}:/to:z watchover:1.53.0 python -m watchover.logtiers relocate /from/logs/cold /to --final" in calls
    assert "retention --home /data --hot-hours 72 --cold-days 365" in calls and "relocate-clean /from/logs/cold /to" in calls
    assert calls.index("relocate /from/logs/hot /to --uid") < calls.index("compose stop") < calls.index("--final") < calls.index("relocate-clean")
    hist = [json.loads(x) for x in (up.spool / "history.jsonl").read_text().splitlines()]
    assert hist[-1]["kind"] == "storage" and hist[-1]["status"] == "done" and hist[-1]["user"] == "ops@firma.com"
