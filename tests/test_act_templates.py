"""WP4a: template packs (§5.4): lint, privacy default, semantic sha, rendering byte-equality, vendor packs, catalogue."""
import copy
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from watchover.act import store
from watchover.act.params import ParamError
from watchover.act.templates import (Catalogue, errors, lint_dir, lint_pack, lint_template, load_packs, privacy_of, render_argv,
                                     render_for_human, semantic_sha)
from watchover.db import Database

ROOT = Path(__file__).resolve().parents[1]
BASE = {"id": "x.svc.restart", "rev": 1, "os": "linux", "kind": "act", "risk": "R2", "reversibility": "transient",
        "argv": ["/usr/bin/systemctl", "restart", "{unit}"], "params": {"unit": {"type": "unit", "pattern": "^(orders)[a-z-]{0,20}\\.service$"}},
        "timeout_s": 60}


def codes(t, pack=None):
    return {f["code"] for f in errors(lint_template(t, pack))}


def mk(**kw):
    t = copy.deepcopy(BASE)
    for k, v in kw.items():
        if v is None:
            t.pop(k, None)
        else:
            t[k] = v
    return t


def test_base_is_clean():
    assert codes(BASE) == set()


@pytest.mark.parametrize("argv", [["/bin/sh", "-c", "rm -rf /"], ["/usr/bin/bash", "x"], ["/usr/bin/env", "python3"],
                                  ["C:\\Windows\\System32\\cmd.exe", "/c", "dir"], ["/usr/bin/python3", "-c", "x"],
                                  ["/usr/bin/xargs", "rm"], ["/bin/busybox", "rm"], ["/usr/bin/perl", "-e", "x"]])
def test_lint_rejects_shells(argv):
    assert "shell" in codes(mk(argv=argv, params={}))


def test_lint_rejects_powershell_command():
    t = mk(os="windows", argv=["C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", "-Command", "Restart-Service x"], params={})
    assert {"shell", "ps_command"} <= codes(t)
    s = mk(os="windows", argv=None, script={"path": "C:\\ProgramData\\Watchover\\actions\\scripts\\x.ps1", "sha256": "0" * 64,
                                            "args": ["-EncodedCommand", "{unit}"]})
    assert "ps_command" in codes(s)


def test_lint_placeholders():
    assert "placeholder_argv0" in codes(mk(argv=["{unit}", "restart"]))
    assert "argv0_absolute" in codes(mk(argv=["systemctl", "restart", "{unit}"]))
    assert "undeclared" in codes(mk(argv=["/usr/bin/systemctl", "restart", "{name}"]))
    assert "placeholder_count" in codes(mk(argv=["/usr/bin/systemctl", "{unit}{unit}"]))
    p = {"path": {"type": "path", "roots": ["/var/log"]}}
    assert "placeholder_prefix" in codes(mk(argv=["/usr/bin/du", "--path={path}"], params=p))      # prefix only for int/enum/ident
    ok = mk(argv=["/usr/bin/du", "--max-depth={d}"], params={"d": {"type": "int", "min": 1, "max": 3}})
    assert "placeholder_prefix" not in codes(ok)
    assert "placeholder_prefix" in codes(mk(argv=["/usr/bin/du", "{d}x"], params={"d": {"type": "int", "min": 1, "max": 3}}))
    assert codes(mk(argv=["/usr/bin/find", "-exec", "{{}}", "+"], params={})) == set()             # {{ }} are literal braces


def test_lint_types_roots_risk():
    assert "missing_roots" in codes(mk(argv=["/usr/bin/du", "{p}"], params={"p": {"type": "path"}}))
    assert "free_string" in codes(mk(argv=["/usr/bin/echo", "{s}"], params={"s": {"type": "string"}}))     # I1
    diag = mk(kind="diag", risk="R0", reversibility=None, writes=["/var/log"])
    assert {"diag_writes", "r0_writes"} <= codes(diag)
    assert "diag_risk" in codes(mk(kind="diag", risk="R1", reversibility=None))
    assert "reversibility" in codes(mk(reversibility=None))
    assert "one_body" in codes(mk(builtin="disk_usage"))
    assert "timeout" in codes(mk(timeout_s=4000))
    assert "run_as_windows" in codes(mk(os="windows", run_as="root", argv=["C:\\x\\y.exe", "{unit}"]))
    assert "human_cmd_body" in codes(mk(human_cmd="systemctl restart {unit}"))
    assert "manifest_required" in codes({"id": "x.c", "rev": 1, "os": "linux", "kind": "act", "risk": "R1", "reversibility": "reversible",
                                         "rollback": "x.d", "builtin": "compress_older_than", "human_cmd": "gzip", "timeout_s": 60,
                                         "params": {"path": {"type": "path", "roots": ["/var/log"]}, "days": {"type": "int", "min": 1, "max": 9}}})
    assert "rollback_pack" in codes(mk(reversibility="reversible", rollback="x.missing"), {"templates": [BASE]})
    assert "pid_exe" in codes(mk(argv=["/usr/bin/jcmd", "{pid}", "GC.heap_info"], params={"pid": {"type": "pid"}}))


def test_pattern_broad_needs_justification():
    broad = mk(params={"unit": {"type": "unit", "pattern": "^.*\\.service$"}})
    assert "pattern_broad" in codes(broad)
    assert "pattern_broad" in codes(mk(params={"unit": {"type": "unit", "pattern": "^(ab|orders)x\\.service$"}}))
    assert "pattern_broad" in codes(mk(params={"unit": {"type": "unit", "pattern": "^orders[a-z]{0,80}\\.service$"}}))
    assert "pattern_broad" in codes(mk(params={"unit": {"type": "unit", "pattern": "orders\\.service"}}))
    justified = mk(params={"unit": {"type": "unit", "pattern": "^.*\\.service$"}}, justification="Basis ekibi: tek uygulama sunucusu")
    assert "pattern_broad" not in codes(justified)


def test_privacy_default_raw_without_parser():
    t = mk(kind="diag", risk="R0", reversibility=None, argv=["/usr/bin/systemctl", "status", "{unit}"])
    assert privacy_of(t) == "raw"
    t["output"] = {"parser": "systemctl_show"}
    assert privacy_of(t) == "structured"
    tail = {"id": "x.tail", "rev": 1, "os": "linux", "kind": "diag", "risk": "R0", "builtin": "log_tail", "human_cmd": "tail {path}",
            "params": {"path": {"type": "path", "roots": ["/var/log"]}, "n": {"type": "int", "min": 1, "max": 9}}, "timeout_s": 9,
            "privacy": "structured", "output": {"parser": "kv"}}
    assert "privacy_raw" in codes(tail)
    jd = mk(kind="diag", risk="R0", reversibility=None, argv=["/usr/bin/journalctl", "-u", "{unit}"], output={"parser": "kv"})
    assert "privacy_raw" in codes(jd)


def test_semantic_sha_ignores_titles():
    a = mk(title_tr="Servisi yeniden başlat", title_en="Restart")
    b = mk(title_tr="Yeniden başlat", title_en="Restart it", hint_tr="x")
    assert semantic_sha(a) == semantic_sha(b)
    assert semantic_sha(a) != semantic_sha(mk(timeout_s=61))
    assert semantic_sha(a) != semantic_sha(mk(rev=2))


def test_rendering_byte_equality():
    lin = mk(run_as="root")
    r = render_for_human(lin, {"unit": "orders-api.service"})
    assert r["argv"] == ["/usr/bin/systemctl", "restart", "orders-api.service"] == render_argv(lin, {"unit": "orders-api.service"})
    assert r["cmd"] == "sudo -n /usr/bin/systemctl restart orders-api.service"
    assert shlex.split(r["cmd"])[2:] == r["argv"]
    with pytest.raises(ParamError):
        render_argv(lin, {"unit": "orders$(reboot).service"})
    with pytest.raises(ParamError):
        render_argv(lin, {"unit": "orders.service", "extra": 1})
    win = {"id": "iis.apppool.recycle", "rev": 1, "os": "windows", "kind": "act", "risk": "R1", "reversibility": "transient", "timeout_s": 60,
           "argv": ["C:\\Windows\\System32\\inetsrv\\appcmd.exe", "recycle", "apppool", "/apppool.name:{pool}"], "params": {"pool": {"type": "iis_pool"}}}
    w = render_for_human(win, {"pool": "Orders Pool"})
    assert w["argv"][-1] == "/apppool.name:Orders Pool"
    assert w["cmdline"] == subprocess.list2cmdline(w["argv"]) == 'C:\\Windows\\System32\\inetsrv\\appcmd.exe recycle apppool "/apppool.name:Orders Pool"'
    assert w["cmd"] == "& 'C:\\Windows\\System32\\inetsrv\\appcmd.exe' 'recycle' 'apppool' '/apppool.name:Orders Pool'"
    packs = load_packs(str(ROOT / "agent_actions"))
    comp = next(t for t in packs["linux-act.tmpl.json"]["templates"] if t["id"] == "linux.logs.compress_old")
    h = render_for_human(comp, {"path": "/var/log/my app", "days": 7})
    assert h["argv"] is None and "'/var/log/my app'" in h["cmd"] and "-mtime +7" in h["cmd"] and "{} +" in h["cmd"]
    ps = next(t for t in packs["windows-svc.tmpl.json"]["templates"] if t["id"] == "win.service.restart")
    assert render_argv(ps, {"name": "OrdersSvc"})[:7] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                                          "C:\\ProgramData\\Watchover\\actions\\scripts\\Restart-WoService.ps1"]
    sap = next(t for t in packs["sap-diag.tmpl.json"]["templates"] if t["id"] == "sap.wp.table")
    assert render_for_human(sap, {"sid": "PRD", "nr": "00"})["cmd"].startswith("sudo -n -u prdadm /usr/sap/hostctrl/exe/sapcontrol -nr 00")
    dry = next(t for t in packs["linux-act.tmpl.json"]["templates"] if t["id"] == "linux.logrotate.force")
    assert render_argv(dry, {"conf": "nginx"}, mode="dry_run") == ["/usr/sbin/logrotate", "-d", "/etc/logrotate.d/nginx"]


def test_vendor_packs_lint_clean():
    res = lint_dir(str(ROOT / "agent_actions"))
    assert set(res) >= {"linux-diag.tmpl.json", "linux-act.tmpl.json", "windows-iis.tmpl.json", "windows-svc.tmpl.json",
                        "sap-diag.tmpl.json", "sap-act.tmpl.json"}
    assert {f: errors(x) for f, x in res.items() if errors(x)} == {}
    ids = {t["id"] for p in load_packs(str(ROOT / "agent_actions")).values() for t in p["templates"]}
    assert {"linux.disk.usage", "linux.dir.top", "linux.fs.lsof_deleted", "linux.logs.compress_old", "linux.logs.decompress_list",
            "linux.logrotate.force", "linux.service.restart", "iis.apppool.recycle", "win.service.restart", "sap.wp.table", "sap.wp.cancel"} <= ids
    bad = copy.deepcopy(load_packs(str(ROOT / "agent_actions"))["linux-diag.tmpl.json"])
    bad["templates"][3]["script"]["sha256"] = "1" * 64
    assert "script_sha_mismatch" in {f["code"] for f in lint_pack(bad, ROOT / "agent_actions" / "scripts")}


def test_catalogue_sync(tmp_path):
    db = Database(str(tmp_path / "k.db"))
    store.ensure(db)
    cat = Catalogue(db)
    first = cat.sync_vendor(str(ROOT / "agent_actions"))
    assert first["templates"] >= 25
    assert cat.sync_vendor(str(ROOT / "agent_actions"))["templates"] == 0          # idempotent (pack-set hash unchanged)
    t = cat.get("linux.disk.usage")
    assert t["status"] == "approved" and t["source"] == "vendor" and t["sha256"] == semantic_sha(t["body"])
    assert cat.get("sap.wp.cancel") is None                                          # the disabled pack loads as proposed
    assert db._exec("SELECT status FROM act_templates WHERE id='sap.wp.cancel'")[0]["status"] == "proposed"
    assert "linux.service.restart" in cat.approved()


def test_lint_cli(capsys):
    from watchover.act.__main__ import main
    assert main(["templates", "lint", str(ROOT / "agent_actions")]) == 0
    assert "0 error(s)" in capsys.readouterr().out
