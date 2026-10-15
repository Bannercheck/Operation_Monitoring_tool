"""Demo mode and the clean-up of everything the built-in simulator made.

WATCHOVER_DEMO=1 (default; the Docker installs): Operations › ⋯ › Simulator may be switched on. While it runs it feeds
synthetic hosts and, once, seeds demo inventory rows (owner "demo (simulator)"), two RUM apps (note "demo (simulator)"),
the "Sipariş → Fatura" business flow and the technology demo hosts.
WATCHOVER_DEMO=0 (production; the OpenShift chart): the simulator cannot be started (the API answers 403, the menu item is
hidden), so none of that is ever written.

clean(svc) deletes only simulator-made data and reports counts:
  * rows the simulator marks: stored live lines of sender "simulator", history minutes of that sender, inventory rows owned
    by "demo (simulator)", RUM apps noted "demo (simulator)" with their sessions and spans, the seeded business flow while
    its definition is unchanged, the simulator's lines in the live spool files;
  * host-keyed rows without a sender (spans, network flows, anomalies, threshold states, technology evidence, business
    events) only for the simulator's own host names, and only when no real source claims that name: an enrolled agent, a
    hand-made inventory row or any other sender in the history / stored lines / live window. A real "db-01" keeps its data.

    python -m watchover.demo status            # demo mode on / off
    python -m watchover.demo clean [--dry-run] # inside the dashboard container (./watchover.sh demo-clean)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

SIM_AGENT = "simulator"
DEMO_OWNER = "demo (simulator)"
DEMO_RUM_APPS = (("ik-mobil", "android"), ("ik-portal", "web"))
DEMO_PEERS = (("db-01", "172.16.1.55", "PostgreSQL"), ("cache-01", "172.16.1.60", "Redis"), ("kafka-01", "172.16.1.70", "Kafka"), ("hr-db", "172.16.2.10", "Oracle"))
DEMO_FLOW = {"name": "Sipariş → Fatura", "key_regex": r"sipariş\s+(\d{7})",
             "steps": [{"name": "Oluşturuldu", "match": "oluşturuldu"}, {"name": "Kredi onayı", "match": "kredi kontrolü onaylandı"},
                       {"name": "Sevkiyat", "match": "sevkiyat"}, {"name": "Fatura", "match": "fatura"}]}
CLAIM_DAYS = 30                           # another sender in the history this recent claims a demo host name
EXTRA_HOSTS = ("payroll-db",)             # backend lines of the simulated HR sessions (live.simulate_rum)
PURGERS: list = []                        # other stores of simulator lines (log tiers): fn(agent, hosts, dry_run) -> {name: count}


class DemoDisabled(Exception):
    """A demo-only action on a production installation (the API answers 403)."""


def enabled() -> bool:
    """Demo mode: WATCHOVER_DEMO unset or 1 (Docker installs keep their simulator); 0 / false / off = production."""
    return os.environ.get("WATCHOVER_DEMO", "1").strip().lower() not in ("0", "false", "no", "off")


def sim_hosts() -> set:
    """Every host name the simulator writes (live lines, metrics, flows, technology demo, seeded inventory)."""
    from .live import SIM_GPU_HOSTS, SIM_HOSTS
    from .techstack import SIM_TECH_EXTRA_INVENTORY, SIM_TECH_FLOWS, SIM_TECH_INVENTORY, SIM_TECH_LISTENS
    return (set(SIM_HOSTS) | set(SIM_GPU_HOSTS) | {r[0] for r in SIM_TECH_INVENTORY} | set(SIM_TECH_EXTRA_INVENTORY)
            | {r[0] for r in SIM_TECH_FLOWS} | {r[0] for r in SIM_TECH_LISTENS} | {r[0] for r in DEMO_PEERS} | set(EXTRA_HOSTS))


def _in(names, cases: bool = False) -> tuple:
    """SQL placeholders and parameters for `col IN (...)`; cases=True adds the upper-case spelling of each name (an index on
    the column stays usable, unlike LOWER(col))."""
    hs = sorted(set(names) | ({n.upper() for n in names} if cases else set()))
    return ",".join("?" * len(hs)), tuple(hs)


def claimed(svc, hosts: set | None = None) -> set:
    """Simulator host names some real source also uses; their unmarked (host-keyed) rows are never deleted."""
    hosts = {h.lower() for h in (hosts or sim_hosts())}
    kb = svc.kb; real: set = set()
    tables = set(kb.tables()); q, hp = _in(hosts, cases=True)
    since = (datetime.now(timezone.utc) - timedelta(days=CLAIM_DAYS)).strftime("%Y-%m-%dT%H:%M")
    probes = []
    if "slo_minute" in tables:                               # (minute leads the primary key: a bounded range, not the 400-day table)
        probes.append((f"SELECT DISTINCT host FROM slo_minute WHERE minute >= ? AND sender <> ? AND host IN ({q})", (since, SIM_AGENT, *hp)))
    if "live_batches" in tables and "host" in kb.columns("live_batches"):
        probes.append((f"SELECT DISTINCT host FROM live_batches WHERE agent <> ? AND host IN ({q})", (SIM_AGENT, *hp)))
    if "inventory" in tables:
        probes.append((f"SELECT hostname AS host FROM inventory WHERE COALESCE(owner, '') <> ? AND LOWER(hostname) IN ({q})", (DEMO_OWNER, *hp)))
    if "agents" in tables:
        probes.append((f"SELECT name AS host FROM agents WHERE LOWER(name) IN ({q})", hp))
    ldb = _live_db(svc)
    if ldb is not kb:                                        # stored lines in the log tier's hot files (logtiers.HotStore)
        probes.append((f"SELECT DISTINCT host FROM live_batches WHERE agent <> ? AND host IN ({q})", (SIM_AGENT, *hp), ldb))
    for sql, params, *db in probes:
        try:
            real |= {str(r["host"]).lower() for r in (db[0] if db else kb)._exec(sql, params)}
        except Exception:  # noqa: BLE001 - a missing column on an old schema only means "no evidence there"
            pass
    live = getattr(svc, "live", None)
    if live is not None:                                     # the running window: other senders of the same names
        with live.lock:
            for agent, hs in getattr(live, "metric_hosts", {}).items():
                if agent != SIM_AGENT:
                    real |= {h.lower() for h in hs} & hosts
            for o in live.buf:
                if o.host and o.host.lower() in hosts and o.attributes.get("agent") != SIM_AGENT:
                    real.add(o.host.lower())
    return real & hosts


def _live_db(svc):
    """Where the live store keeps its stored lines: the main database, or the log tier's hot day files (no DELETE there)."""
    db = getattr(getattr(svc, "live", None), "db", None)
    return db if db is not None else svc.kb


def _delete(kb, tables: set, counts: dict, key: str, table: str, where: str, params: tuple, dry_run: bool) -> None:
    if table not in tables:
        counts[key] = 0
        return
    n = int(kb._exec(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params)[0]["n"])
    if n and not dry_run:
        kb._exec(f"DELETE FROM {table} WHERE {where}", params)
    counts[key] = counts.get(key, 0) + n


def _spool_files(svc) -> list:
    sp = getattr(getattr(svc, "live", None), "spool", None)
    if not sp:
        return []
    sp = Path(sp)
    return [p for p in [sp] + sorted(sp.parent.glob(sp.name + ".*")) if p.is_file()]


def _clean_spool(svc, dry_run: bool) -> int:
    """The live spool (events.jsonl and its rotations): drop the lines of sender "simulator" (the trainer reads these files)."""
    tail = '"agent": "simulator"}'
    n = 0
    for p in _spool_files(svc):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            keep = [ln for ln in lines if not ln.rstrip().endswith(tail)]
            if len(keep) == len(lines):
                continue
            n += len(lines) - len(keep)
            if not dry_run:
                tmp = p.with_name(p.name + ".demo-clean")
                tmp.write_text("".join(keep), encoding="utf-8")
                os.replace(tmp, p)
        except OSError:                                      # a busy or vanished rotation is skipped, never fatal
            continue
    return n


def clean(svc, dry_run: bool = False) -> dict:
    """Delete what the simulator made (see the module doc); returns {"counts": {...}, "kept_hosts": [...], "hosts": [...]}."""
    if getattr(svc, "sim_stop", None) is not None and not dry_run:
        svc.sim_stop.set(); svc.sim_stop = None              # a running simulator would write it all again (its purge below spares real hosts)
    kb = svc.kb
    tables = set(kb.tables())
    hosts = sim_hosts()
    real = claimed(svc, hosts)
    own = hosts - real                                       # names only the simulator ever used
    counts: dict = {}
    d = lambda key, table, where, params=(): _delete(kb, tables, counts, key, table, where, params, dry_run)   # noqa: E731
    # ---- marked by the simulator itself
    d("live_lines", "live_batches", "agent = ?", (SIM_AGENT,))
    pending: dict = {}
    ldb = _live_db(svc)
    if ldb is not kb:                                        # hot day files: counted here, removed by a registered purger (PURGERS)
        try:
            n = int(ldb._exec("SELECT COUNT(*) AS n FROM live_batches WHERE agent = ?", (SIM_AGENT,))[0]["n"])
        except Exception:  # noqa: BLE001
            n = 0
        if n:
            pending["live_lines_hot"] = n
    d("history_minutes", "slo_minute", "sender = ?", (SIM_AGENT,))
    d("inventory", "inventory", "owner = ?", (DEMO_OWNER,))
    demo_apps = []
    if "rum_apps" in tables:
        demo_apps = [r["name"] for r in kb._exec("SELECT name FROM rum_apps WHERE note = ?", (DEMO_OWNER,))]
    if demo_apps:
        qa, pa = _in(demo_apps)
        if "rum_sessions" in tables:
            d("rum_spans", "spans", f"session_id IN (SELECT session_id FROM rum_sessions WHERE app IN ({qa}))", pa)
        d("rum_sessions", "rum_sessions", f"app IN ({qa})", pa)
        d("rum_apps", "rum_apps", f"name IN ({qa}) AND note = ?", (*pa, DEMO_OWNER))
    else:
        counts.update({"rum_spans": 0, "rum_sessions": 0, "rum_apps": 0})
    counts["biz_flows"] = 0
    if "biz_flows" in tables:
        for f in kb._exec("SELECT id, key_regex, steps FROM biz_flows WHERE name = ?", (DEMO_FLOW["name"],)):
            try:
                steps = [{"name": s.get("name"), "match": s.get("match")} for s in json.loads(f["steps"] or "[]")]
            except (TypeError, ValueError, AttributeError):
                steps = []
            if f["key_regex"] == DEMO_FLOW["key_regex"] and steps == DEMO_FLOW["steps"]:   # an edited flow is the operator's now
                d("biz_events", "biz_events", "flow_id = ?", (f["id"],))
                d("biz_flows", "biz_flows", "id = ?", (f["id"],))
    # ---- host-keyed rows of names only the simulator used
    if own:
        qh, ph = _in(own, cases=True)
        for key, table in (("spans", "spans"), ("net_flows", "net_flows"), ("anomalies", "anomalies"), ("threshold_states", "threshold_state"),
                           ("tech_seen", "tech_seen"), ("biz_events", "biz_events")):
            d(key, table, f"host IN ({qh})", ph)
    counts["spool_lines"] = _clean_spool(svc, dry_run)
    for fn in PURGERS:                                       # log tiers and other stores registered by their modules
        try:
            for k, v in (fn(SIM_AGENT, own, dry_run) or {}).items():
                counts[k] = counts.get(k, 0) + int(v or 0)
            pending.pop("live_lines_hot", None)              # the tier's own purger owns its files
        except Exception:  # noqa: BLE001 - one store failing must not hide the counts of the others
            counts.setdefault("errors", 0); counts["errors"] += 1
    live = getattr(svc, "live", None)
    if live is not None:                                     # the running window: events, metrics, rollups, the sender itself
        with live.lock:
            mem = sum(1 for o in live.buf if o.attributes.get("agent") == SIM_AGENT)
            mh = getattr(live, "metric_hosts", {}).get(SIM_AGENT)
            if mh is not None and not dry_run:
                mh.difference_update(real)                   # a real "db-01" keeps its metric rows in the window
        counts["live_window"] = mem
        if not dry_run:
            live.purge(SIM_AGENT, tuple(sorted(own)))
    if not dry_run:                                          # cached lookups forget the deleted rows; a later simulator run seeds again
        for obj, attr, val in ((getattr(svc, "inventory", None), "_index", None), (getattr(svc, "bizflows", None), "_compiled", None),
                               (svc, "_sim_inventory", False), (svc, "_sim_flow", False)):
            if obj is not None and hasattr(obj, attr):
                setattr(obj, attr, val)
    # pending: simulator lines this clean-up cannot delete (log tier files without a purger); they age out with the tier's retention
    return {"dry_run": dry_run, "counts": counts, "total": sum(v for v in counts.values() if isinstance(v, int)),
            "hosts": sorted(own), "kept_hosts": sorted(real), "pending": pending}


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m watchover.demo", description="Demo mode status and the clean-up of simulator-made data.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="demo mode on / off")
    c = sub.add_parser("clean", help="delete only what the simulator made (counts printed as JSON)")
    c.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps({"demo": enabled()}))
        return 0
    os.environ.setdefault("WATCHOVER_SELFMON", "0")
    from .api.services import Services
    svc = Services(background=False)                         # no receiver, no threads: the database and the spool files only
    try:
        out = clean(svc, dry_run=a.dry_run)
        if not a.dry_run:
            try:
                svc.users._event("cli", "demo-clean", True, f"demo data removed: {out['total']} rows ({json.dumps(out['counts'], sort_keys=True)})")
            except Exception:  # noqa: BLE001 - the audit log never blocks the clean-up
                pass
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
