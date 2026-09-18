"""The engine must give the same result on every machine: no hash-order, path-separator, line-ending or clock dependence."""
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from watchover import analysis as an, stamp
from watchover.pipeline import ingest, ingest_path

ROOT = Path(__file__).resolve().parents[1]
STORM = ROOT / "samples" / "alarm_storm.zip"


def _files(conv=lambda b: b, names=lambda n: n):
    zf = zipfile.ZipFile(STORM)
    return [(names(n), conv(zf.read(n)).decode("utf-8-sig")) for n in zf.namelist() if not n.endswith("/")]


def test_result_id_is_stable_across_hash_seeds_and_processes():
    code = ("import sys; sys.path.insert(0, 'src')\n"
            "from watchover.pipeline import ingest_path; from watchover import analysis as an, stamp\n"
            f"obs, rep = ingest_path(r'{STORM}'); a = an.Analysis(obs, rep); print(stamp.result_id(a), stamp.input_id(rep))")
    outs = set()
    for seed in ("1", "2", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        outs.add(subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout.strip())
    assert len(outs) == 1, outs


def test_same_result_with_windows_paths_crlf_bom_and_upload_order():
    base_obs, base_rep = ingest(iter(_files()))
    base = stamp.result_id(an.Analysis(base_obs, base_rep)); base_in = stamp.input_id(base_rep)
    variants = {
        "backslash": _files(names=lambda n: "C:\\Users\\ops\\S-A1\\" + n.rsplit("/", 1)[-1]),
        "crlf": _files(conv=lambda b: b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")),
        "bom": _files(conv=lambda b: b"\xef\xbb\xbf" + b),
        "reversed": list(reversed(_files())),
    }
    for name, files in variants.items():
        obs, rep = ingest(iter(files))
        a = an.Analysis(obs, rep)
        assert stamp.input_id(rep) == base_in, name          # same input set -> same input id
        assert stamp.result_id(a) == base, name               # -> same result id
        assert a.mode == "density" and len(a.dependencies) == 32, name


def test_stamp_fields():
    obs, rep = ingest_path(str(STORM)); a = an.Analysis(obs, rep)
    st = stamp.stamp(a)
    assert set(st) >= {"version", "engine", "scenario", "git", "input", "result", "package"} and len(st["engine"]) == 10
    assert all(len(r.get("sha", "")) == 10 for r in rep)
    assert stamp.stale_package(str(ROOT / "app.py")) is None
