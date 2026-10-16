"""python -m watchover.act <command>

    doctor                    tables and row counts, mode, key custody, audit chain
    audit verify              walk the audit chain; exit 1 when it is broken ("Zincir kırık: #n")
    audit purge [--days N]    drop audit payloads older than the retention (rows and MACs stay; the chain keeps verifying)
    kill --global --reason R  write <WATCHOVER_HOME>/act.KILL (works with the database down); --resume removes it
    templates lint <dir>      lint every *.tmpl.json pack in a folder (exit 1 on errors)
    demo disk [--host H]      act_demo only: a /var/log disk case within seconds (simulated disk)
    demo run-guided <case>    act_demo only: walk the guided run; the simulated disk moves; --wait lets the watch window pass
    metrics [--week 2026-W42] compute the weekly agreement metrics (κ, FULL, FALSE-ACT, WRONG-ACT, MISS)
"""
from __future__ import annotations

import argparse
import json
import sys


def _db():
    from ..db import Database, default_url
    return Database(default_url())


def doctor(out=sys.stdout) -> int:
    from . import store
    from .audit import Audit
    from .keys import KeyClient
    from .policy import Policy
    db = _db()
    store.ensure(db)
    keys = KeyClient(None)
    audit = Audit(db, keys)
    pol = Policy(db, audit)
    keys.policy = pol
    ks = keys.status()
    from . import limits as killmod
    print(f"watchover act doctor · {db.label}", file=out)
    print(f"mode: {pol.mode} · custody: {ks['custody']}", file=out)
    counts = store.counts(db)
    missing = sorted(set(store.TABLES) - set(counts))
    print(f"tables: {len(counts)} act_* ({'all present' if not missing else 'missing: ' + ', '.join(missing)})", file=out)
    for t, n in counts.items():
        print(f"  {t:<24} {n:>8}", file=out)
    cases = db._exec("SELECT COUNT(*) AS n FROM actions WHERE kind='case'") if "actions" in db.tables() and "kind" in db.columns("actions") else [{"n": 0}]
    print(f"cases: {cases[0]['n']}", file=out)
    v = audit.verify()
    chain = "ok" if v["ok"] else f"BROKEN at #{v['broken_at']} ({v['reason']})"
    print(f"audit: {v['n']} events · chain: {chain} · mac: {', '.join(v['mac']) or '-'}"
          f"{' (placeholder: no keys yet)' if v.get('placeholder') else ''}", file=out)
    panic = killmod.panic_file()
    print(f"global stop file: {'PRESENT ' + str(panic) if panic.exists() else 'none'}", file=out)
    print(f"append-only triggers: {'present' if store.triggers_present(db) else 'MISSING'}", file=out)
    from .anchors import Anchors
    fr = Anchors(db, keys, audit, pol).freshness()
    print(f"external anchor: {'no sink configured' if not fr['configured'] else ('fresh' if fr['fresh'] else 'STALE')}"
          f"{'' if fr['age_min'] is None else ' (' + str(fr['age_min']) + ' min)'}", file=out)
    warns = list(ks["warnings"])
    if ks["custody"] == "embedded":
        warns += keys.shared_mount()
    from .. import vault
    vk = vault.key_path()
    if vault.secrets_dir() is None or vault.secrets_dir() not in vk.parents:
        warns.append(f"vault key {vk} is not on a separate secrets mount (WATCHOVER_VAULT_DIR): /data backups can decrypt vault values")
    for w in warns:
        print(f"warning: {w}", file=out)
    db.close()
    return 0 if v["ok"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="watchover.act", description="action agent administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    a = sub.add_parser("audit")
    a.add_argument("what", choices=["verify", "purge"])
    a.add_argument("--days", type=int, default=None)
    k = sub.add_parser("kill")
    k.add_argument("--global", dest="glob", action="store_true", required=True)
    k.add_argument("--reason", default="")
    k.add_argument("--resume", action="store_true")
    t = sub.add_parser("templates")
    t.add_argument("what", choices=["lint"])
    t.add_argument("path")
    d = sub.add_parser("demo")
    d.add_argument("what", choices=["disk", "run-guided"])
    d.add_argument("case", nargs="?", type=int)
    d.add_argument("--host", default="web-prod-07")
    d.add_argument("--pct", type=float, default=93.4)
    d.add_argument("--wait", action="store_true")
    m = sub.add_parser("metrics")
    m.add_argument("--week", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "demo":
        from . import ActService, demo
        from .demo import DemoError
        db = _db()
        act = ActService(db=db)
        try:
            if args.what == "disk":
                c = demo.disk(act, host=args.host, pct=args.pct)
                print(json.dumps({"case": c.get("id"), "title": c.get("title"), "phase": c.get("phase")}, ensure_ascii=False))
                return 0
            if not args.case:
                print("run-guided needs a case id", file=sys.stderr)
                return 2
            r = demo.run_guided(act, args.case, wait=args.wait)
            print(json.dumps({"run": r["id"], "status": r["status"], "label": r.get("label"),
                              "steps": [(s["step_key"], s["status"]) for s in r["steps"]]}, ensure_ascii=False))
            return 0
        except DemoError as e:
            print(e.detail()["tr"], file=sys.stderr)
            return 2
    if args.cmd == "metrics":
        from . import store
        from .metrics import weekly
        db = _db(); store.ensure(db)
        print(json.dumps(weekly(db, args.week), ensure_ascii=False, default=str))
        return 0
    if args.cmd == "doctor":
        return doctor()
    if args.cmd == "audit":
        from .audit import Audit
        from .keys import KeyClient
        from .policy import Policy
        from . import store
        db = _db(); store.ensure(db)
        keys = KeyClient(None); keys.policy = Policy(db)
        au = Audit(db, keys)
        if args.what == "purge":
            days = args.days if args.days is not None else int((keys.policy.get("act_retention_d") or {}).get("audit_payload", 400))
            print(json.dumps(au.purge_payloads(older_than_days=days, by="cli"), ensure_ascii=False))
            return 0
        v = au.verify()
        print(json.dumps(v, ensure_ascii=False))
        if not v["ok"]:
            print(f"Zincir kırık: #{v['broken_at']} ({v['reason']})", file=sys.stderr)
        return 0 if v["ok"] else 1
    if args.cmd == "kill":
        from . import limits as killmod
        if args.resume:
            killmod.panic_clear()
            print("global stop file removed (database stops, if any, need two act.policy holders in the UI)")
            return 0
        if not args.reason.strip():
            print("--reason is required", file=sys.stderr)
            return 2
        p = killmod.panic_set(args.reason)
        print(f"global stop: {p}")
        return 0
    if args.cmd == "templates":
        from .templates import lint_dir
        res = lint_dir(args.path)
        bad = 0
        for f, items in res.items():
            for it in items:
                bad += it["level"] == "error"
                print(f"{f}: {it['level']}: {it.get('template', '')} {it['code']} {it.get('detail', '')}".rstrip())
        print(f"{len(res)} pack(s), {bad} error(s)")
        return 1 if bad else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
