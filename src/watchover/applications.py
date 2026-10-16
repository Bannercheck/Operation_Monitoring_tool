"""Business applications ("Uygulamalar"): the unit an operator thinks in, on top of the per-host model.

An application has a name, a description, an owning team (NULL: installation-wide), a criticality, tags, SLO targets (availability %,
p95 ms) and membership rules (`members`, JSON):

    hosts       explicit host names                           inv_apps    inventory `application` values (case-insensitive)
    pools       host pools (inventory cluster / name stem)     services    service names, exact or a glob (hr-*, *-api)
    namespaces  the OTel resource attribute service.namespace  rum_apps    user-experience apps (web / mobile), by name
    flows       business flow ids

Member hosts = explicit ∪ inventory value ∪ pool. Member services = exact names ∪ globs ∪ the services seen under a namespace ∪ the
RUM app names. A log line belongs to an application when its host is a member host or its service is a member service; the log
query `app:<name>` expands to exactly that (logquery.expand_apps), and a threshold policy may target an application (thresholds.py,
precedence host > host pattern > application > pool > environment > all).

Health comes from what is already counted, never from a scan of raw lines: the live minute rollups (lines, ERROR+ and latency per
host × service: Rollup.fold_groups), the trace rollups (RED per service), the threshold state and the open anomalies. Answers are
cached for 15 s per viewer scope.

Visibility (enforced here and in the router, for every caller: API, log query, MCP tools): an installation-wide viewer sees every
application. A team member sees the applications owned by one of their teams and the installation-wide applications that touch
their scope; another team's application does not exist for them. Owning an application never widens host visibility: that stays
with the team's host rules and explicit hosts (Teams.claim is the single gate; "Ekibe sunucu ata" runs the application's hosts
through it). Every number is computed over the member hosts and services the viewer may see.

Discovery proposes applications from inventory `application` values, OTel service.namespace values and obvious service-name groups,
and lists the hosts and services no application covers. A proposal never changes an existing application unless the user merges
it explicitly.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

UTC = timezone.utc
CRITICALITY = ("critical", "high", "normal", "low")
KINDS = ("hosts", "inv_apps", "pools", "services", "namespaces", "rum_apps", "flows")
NS_ATTR = "service.namespace"
SILENT_S = 300                       # a member host with no line for 5 minutes is silent
TTL = 15.0                           # health / series cache
MAX_MEMBERS = 500
NB = 23                              # latency buckets (rollup.BOUNDS + 1)
INBOUND = ("server", "internal", "consumer", "user", "mobile")
NO_MATCH = "\x00no-application-member"
GENERIC = frozenset({"api", "app", "apps", "web", "www", "svc", "service", "services", "server", "srv", "prd", "prod", "test", "tst", "dev", "stg",
                     "staging", "qa", "uat", "int", "ext", "backend", "frontend", "worker", "db", "node", "k8s", "kube", "core", "common", "main"})
NAME_BAD = re.compile(r'["\\\x00-\x1f]')
_PURGER = {"store": None}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _wild(p: str) -> bool:
    return any(c in p for c in "*?[")


def _q(hist: list, q: float) -> Optional[float]:
    from .rollup import _q as rq
    return rq(hist, q) if hist and sum(hist) else None


class NameTaken(ValueError):
    """Another application already has this name (names are unique installation-wide: queries and policies use them)."""


@dataclass
class Members:
    hosts: dict                      # host -> how it joined: explicit | inventory | pool
    services: set                    # exact service names, lower case
    globs: list                      # service patterns, lower case
    namespaces: list
    rum_apps: list
    flows: list

    def svc_match(self, name) -> bool:
        if not name or name == "-":
            return False
        n = str(name).lower()
        return n in self.services or any(fnmatch.fnmatchcase(n, g) for g in self.globs)


def clean_members(m: Optional[dict]) -> dict:
    """Known kinds only, each a de-duplicated list (flows: ids); a comma / newline separated string is split."""
    out: dict = {}
    m = m or {}
    for k in KINDS:
        vals = m.get(k) or []
        if isinstance(vals, str):
            vals = re.split(r"[,\n]", vals)
        if k == "flows":
            ids = []
            for v in vals:
                try:
                    ids.append(int(v))
                except (TypeError, ValueError):
                    raise ValueError("flows: business flow ids")
            out[k] = sorted(set(ids))[:MAX_MEMBERS]
        else:
            vs = [str(v).strip()[:200] for v in vals if str(v or "").strip()]
            if any(NAME_BAD.search(v) for v in vs):
                raise ValueError(f"{k}: quotes, backslashes and control characters are not allowed")
            out[k] = list(dict.fromkeys(vs))[:MAX_MEMBERS]
    return out


def scope_key(sc) -> str:
    if sc is None:
        return "*"
    raw = "\n".join(sorted(sc.hosts)) + "|" + json.dumps(sc.service_rules, sort_keys=True) + "|" + ",".join(sorted(sc.teams))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class Applications:
    def __init__(self, kb, svc=None):
        self.kb, self.svc = kb, svc
        self.lock = threading.Lock()
        self.version = 0
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS applications (id {pk}, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '', team_id INTEGER,
            criticality TEXT DEFAULT 'normal', tags TEXT DEFAULT '[]', slo_availability REAL, slo_p95_ms REAL, members TEXT DEFAULT '{{}}',
            origin TEXT DEFAULT 'manual', created_by TEXT DEFAULT '', created_at TEXT, updated_at TEXT)""")
        kb._exec("""CREATE TABLE IF NOT EXISTS app_namespaces (namespace TEXT NOT NULL, service TEXT NOT NULL, host TEXT NOT NULL DEFAULT '',
            last_seen TEXT, PRIMARY KEY (namespace, service, host))""")
        kb._exec("CREATE TABLE IF NOT EXISTS app_dismissed (key TEXT NOT NULL, owner TEXT NOT NULL, at TEXT, PRIMARY KEY (key, owner))")
        self._rows: Optional[tuple] = None
        self._mcache: dict = {}
        self._uni: Optional[tuple] = None
        self._suni: Optional[tuple] = None
        self._ns: Optional[tuple] = None
        self._ns_pending: dict = {}
        self._ns_flush = time.time()
        self._hidx: Optional[tuple] = None
        self._cache: dict = {}
        self._alerts: Optional[tuple] = None
        self._trows: dict = {}
        self.on_change: Optional[Callable] = None      # thresholds cache reset (set by Services)
        if svc is not None:
            self._wire(svc)

    def _wire(self, svc) -> None:
        teams = getattr(svc, "teams", None)
        if teams is not None:
            def count(tid):
                return int(self.kb._exec("SELECT COUNT(*) AS n FROM applications WHERE team_id=?", (tid,))[0]["n"])

            def move(tid, to):
                self.kb._exec("UPDATE applications SET team_id=? WHERE team_id=?", (to, tid))
                self.invalidate()
            teams.owned_hooks["applications"] = (count, move)
        _PURGER["store"] = self
        from . import demo
        if _demo_purge not in demo.PURGERS:
            demo.PURGERS.append(_demo_purge)

    # ---------------------------------------------------------------- CRUD
    def invalidate(self) -> None:
        with self.lock:
            self.version += 1
            self._rows = None
            self._mcache.clear()
            self._hidx = None
            self._cache.clear()
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _row(r: dict) -> dict:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        try:
            d["members"] = clean_members(json.loads(d.get("members") or "{}"))
        except ValueError:
            d["members"] = clean_members({})
        for k in ("slo_availability", "slo_p95_ms"):
            d[k] = None if d.get(k) is None else float(d[k])
        return d

    def list(self) -> list:
        hit = self._rows
        if hit and hit[0] == self.version:
            return [dict(x) for x in hit[1]]
        rows = [self._row(r) for r in self.kb._exec("SELECT * FROM applications ORDER BY name")]
        self._rows = (self.version, rows)
        return [dict(x) for x in rows]

    def get(self, aid: int) -> dict:
        for a in self.list():
            if a["id"] == int(aid):
                return a
        raise KeyError(f"application {aid}")

    def by_name(self, name: str) -> Optional[dict]:
        low = (name or "").strip().lower()
        return next((a for a in self.list() if a["name"].lower() == low), None)

    def save(self, d: dict, aid: Optional[int] = None, by: str = "", origin: str = "manual") -> dict:
        """Create or update. `d`: name, description, team_id (the router decides it), criticality, tags, slo_availability (%),
        slo_p95_ms, members. A rename carries the application-scoped threshold policies along."""
        name = str(d.get("name") or "").strip()[:80]
        if not name:
            raise ValueError("application name required")
        if NAME_BAD.search(name):
            raise ValueError("application name: quotes, backslashes and control characters are not allowed")
        crit = str(d.get("criticality") or "normal").strip().lower()
        if crit not in CRITICALITY:
            raise ValueError(f"criticality must be one of {', '.join(CRITICALITY)}")
        tags = d.get("tags") or []
        if isinstance(tags, str):
            tags = tags.split(",")
        tags = list(dict.fromkeys(str(x).strip()[:40] for x in tags if str(x or "").strip()))[:20]
        sa, sp = d.get("slo_availability"), d.get("slo_p95_ms")
        if sa not in (None, ""):
            sa = float(sa)
            if not 0 < sa <= 100:
                raise ValueError("slo_availability: a percentage above 0 and at most 100")
        else:
            sa = None
        if sp not in (None, ""):
            sp = float(sp)
            if not 0 < sp <= 600000:
                raise ValueError("slo_p95_ms: above 0 ms and at most 600000 ms")
        else:
            sp = None
        members = clean_members(d.get("members"))
        team = d.get("team_id")
        team = None if team in (None, "") else int(team)
        desc = str(d.get("description") or "")[:500]
        now = _now()
        with self.lock:
            dup = [r for r in self.kb._exec("SELECT id, name FROM applications") if r["name"].lower() == name.lower() and (aid is None or r["id"] != int(aid))]
            if dup:                                            # case-insensitive in Python: SQLite's LOWER() folds ASCII only (Ödeme / ödeme)
                raise NameTaken(f"an application named '{name}' exists")
            if aid is None:
                aid = self.kb._insert("""INSERT INTO applications (name, description, team_id, criticality, tags, slo_availability, slo_p95_ms, members, origin,
                    created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      (name, desc, team, crit, json.dumps(tags, ensure_ascii=False), sa, sp, json.dumps(members, ensure_ascii=False), origin,
                                       by[:200], now, now))
                old = None
            else:
                rows = self.kb._exec("SELECT name FROM applications WHERE id=?", (int(aid),))
                if not rows:
                    raise KeyError(f"application {aid}")
                old = rows[0]["name"]
                self.kb._exec("""UPDATE applications SET name=?, description=?, team_id=?, criticality=?, tags=?, slo_availability=?, slo_p95_ms=?, members=?,
                    updated_at=? WHERE id=?""", (name, desc, team, crit, json.dumps(tags, ensure_ascii=False), sa, sp, json.dumps(members, ensure_ascii=False),
                                                  now, int(aid)))
        if old is not None and old != name:
            for pid in self._policies(old):
                self.kb._exec("UPDATE threshold_policies SET scope_value=? WHERE id=?", (name, pid))
        self.invalidate()
        return self.get(aid)

    def delete(self, aid: int) -> int:
        """Delete; the application-scoped threshold policies that named it are disabled (kept for the record). Returns how many."""
        a = self.get(aid)
        ids = self._policies(a["name"], enabled_only=True)
        for pid in ids:
            self.kb._exec("UPDATE threshold_policies SET enabled=0 WHERE id=?", (pid,))
        n = len(ids)
        self.kb._exec("DELETE FROM applications WHERE id=?", (int(aid),))
        self.invalidate()
        return n

    def _policies(self, name: str, enabled_only: bool = False) -> list:
        """Ids of the threshold policies whose application scope names `name` (case-insensitive)."""
        try:
            rows = self.kb._exec("SELECT id, scope_value, enabled FROM threshold_policies WHERE scope='application'")
        except Exception:  # noqa: BLE001 - no thresholds table (engine used alone)
            return []
        return [r["id"] for r in rows if (r["scope_value"] or "").lower() == name.lower() and (not enabled_only or r["enabled"])]

    def add_members(self, aid: int, add: dict) -> dict:
        """Append to the membership lists (the "assign" of unmatched hosts / services; an explicit merge of a proposal)."""
        a = self.get(aid)
        m = {k: list(v) for k, v in a["members"].items()}     # a copy: the cached row stays as stored if the save is refused
        add = clean_members(add)
        for k in KINDS:
            m[k] = (sorted(set(m[k]) | set(add[k])) if k == "flows" else list(dict.fromkeys(m[k] + add[k])))[:MAX_MEMBERS]
        return self.save({**a, "members": m}, aid)

    # ---------------------------------------------------------------- what Watchover knows (hosts, services, namespaces)
    def _hosts_universe(self) -> dict:
        """host -> {env, application, pool}: inventory, agents, live counters (Teams.facts + the rollup), cached 30 s."""
        now = time.time()
        if self._uni and now - self._uni[0] < 30:
            return self._uni[1]
        out: dict = {}
        svc = self.svc
        facts: dict = {}
        if svc is not None and getattr(svc, "teams", None) is not None:
            try:
                facts = svc.teams.facts()
            except Exception:  # noqa: BLE001
                facts = {}
        else:
            try:
                for r in self.kb._exec("SELECT hostname, env, application, cluster FROM inventory"):
                    facts[r["hostname"]] = {"env": r.get("env"), "application": r.get("application"), "cluster": r.get("cluster")}
            except Exception:  # noqa: BLE001
                facts = {}
        r = getattr(getattr(svc, "live", None), "rollup", None)
        hosts = set(facts) | ({h for h in list(r.hosts) if h and h != "-"} if r is not None else set())
        for h in hosts:
            d = facts.get(h) or {}
            pool = ""
            if r is not None:
                try:
                    pool = r.pool_of(h)
                except Exception:  # noqa: BLE001
                    pool = ""
            else:
                from .rollup import pool_stem
                pool = d.get("cluster") or pool_stem(h)
            out[h] = {"env": d.get("env") or (r.host_env.get(h) if r is not None else "") or "", "application": str(d.get("application") or "").strip(),
                      "pool": pool or ""}
        self._uni = (now, out)
        return out

    def _service_universe(self) -> dict:
        """service -> hosts it ran on (live counters of the last hour, stored spans of the last day), plus trace-only services, RUM
        apps and the services seen under a namespace; cached 60 s."""
        now = time.time()
        if self._suni and now - self._suni[0] < 60:
            return self._suni[1]
        out: dict = defaultdict(set)
        svc = self.svc
        r = getattr(getattr(svc, "live", None), "rollup", None)
        if r is not None:
            with r.lock:
                names = list(r.hosts)
                for c in list(r.svc.values()):
                    for (h, s) in c:
                        out[s].add(names[h])
        tr = getattr(svc, "traces", None)
        if tr is not None:
            try:
                for s, hs in tr.service_hosts(1440).items():
                    out[s] |= set(hs)
                since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
                for row in self.kb._exec("SELECT DISTINCT service FROM svc_minute WHERE minute >= ?", (since,)):
                    out.setdefault(row["service"], set())
            except Exception:  # noqa: BLE001
                pass
        rum = getattr(svc, "rum", None)
        if rum is not None:
            try:
                for a in rum.apps():
                    out.setdefault(a["name"], set())
            except Exception:  # noqa: BLE001
                pass
        for ns in self.namespaces().values():
            for s in ns["services"]:
                out.setdefault(s, set())
            for s, hs in ns["svc_hosts"].items():
                out[s] |= hs
        for k in ("", "-", None):
            out.pop(k, None)
        res = dict(out)
        self._suni = (now, res)
        return res

    def observe(self, events) -> None:
        """Live ingest hook: remember which services (and hosts) carry each OTel service.namespace. Bounded, flushed every 30 s."""
        pend = self._ns_pending
        now = time.time()
        for o in events:
            a = o.attributes
            if not a or not o.service:
                continue
            ns = a.get(NS_ATTR)
            if ns:
                if len(pend) < 20000:
                    pend[(str(ns)[:120], o.service[:200], (o.host or "")[:200])] = now
        if pend and now - self._ns_flush > 30:
            self.flush_namespaces()

    def flush_namespaces(self) -> int:
        self._ns_flush = time.time()
        pend, self._ns_pending = self._ns_pending, {}
        if not pend:
            return 0
        rows = [(ns, s, h, datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")) for (ns, s, h), t in pend.items()]
        try:
            self.kb.executemany("INSERT INTO app_namespaces (namespace, service, host, last_seen) VALUES (?,?,?,?)"
                                + self.kb.upsert_suffix("namespace, service, host", "last_seen=excluded.last_seen"), rows)
            if int(self._ns_flush) % 3600 < 31:
                cut = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
                self.kb._exec("DELETE FROM app_namespaces WHERE last_seen < ?", (cut,))
        except Exception:  # noqa: BLE001 - best effort; the pending set is small and refills from the feed
            return 0
        self._ns = None
        return len(rows)

    def namespaces(self) -> dict:
        """namespace -> {"services": set, "hosts": set, "svc_hosts": {service: hosts}} (last 30 days), cached 30 s."""
        now = time.time()
        if self._ns and now - self._ns[0] < 30 and not self._ns_pending:
            return self._ns[1]
        out: dict = {}

        def add(ns, s, h):
            d = out.setdefault(ns, {"services": set(), "hosts": set(), "svc_hosts": defaultdict(set)})
            d["services"].add(s)
            if h:
                d["hosts"].add(h); d["svc_hosts"][s].add(h)
        try:
            cut = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
            for r in self.kb._exec("SELECT namespace, service, host FROM app_namespaces WHERE last_seen >= ?", (cut,)):
                add(r["namespace"], r["service"], r["host"])
        except Exception:  # noqa: BLE001
            pass
        for (ns, s, h) in list(self._ns_pending):
            add(ns, s, h)
        self._ns = (now, out)
        return out

    # ---------------------------------------------------------------- membership
    def members(self, app: dict) -> Members:
        key = (app.get("updated_at"), json.dumps(app["members"], sort_keys=True))
        now = time.time()
        hit = self._mcache.get(app["id"])
        if hit and hit[0] == key and now - hit[1] < 30:
            return hit[2]
        m = app["members"]
        hosts: dict = {h: "explicit" for h in m["hosts"]}
        inv = {x.lower() for x in m["inv_apps"]}
        pools = {x.lower() for x in m["pools"]}
        if inv or pools:
            for h, d in sorted(self._hosts_universe().items()):
                if h in hosts:
                    continue
                if inv and d["application"].lower() in inv:
                    hosts[h] = "inventory"
                elif pools and (d["pool"] or "").lower() in pools:
                    hosts[h] = "pool"
        exact, globs = set(), []
        for s in m["services"]:
            (globs.append(s.lower()) if _wild(s) else exact.add(s.lower()))
        if m["namespaces"]:
            nsmap = {k.lower(): v for k, v in self.namespaces().items()}
            for n in m["namespaces"]:
                exact |= {s.lower() for s in (nsmap.get(n.lower()) or {}).get("services", ())}
        exact |= {a.lower() for a in m["rum_apps"]}
        mem = Members(hosts, exact, globs, list(m["namespaces"]), list(m["rum_apps"]), list(m["flows"]))
        self._mcache[app["id"]] = (key, now, mem)
        return mem

    def apps_of_host(self, host: str) -> list:
        """Names of the applications a host is a member of (thresholds: application scope and the alert text). Cached 30 s."""
        now = time.time()
        hit = self._hidx
        if not hit or hit[0] != self.version or now - hit[1] > 30:
            idx: dict = defaultdict(list)
            for a in self.list():
                for h in self.members(a).hosts:
                    idx[h].append(a["name"])
            hit = self._hidx = (self.version, now, dict(idx))
        return hit[2].get(host, [])

    # ---------------------------------------------------------------- visibility
    def _team_names(self) -> dict:
        try:
            return {t["id"]: t["name"] for t in self.svc.teams.list()}
        except Exception:  # noqa: BLE001
            return {}

    def visible(self, app: dict, sc) -> bool:
        """Installation-wide viewer: always. Team member: an application of one of their teams, or an installation-wide one that
        touches their scope (a member host they see, or a member service their scope allows)."""
        if sc is None:
            return True
        if app.get("team_id") is not None:
            return self._team_names().get(app["team_id"]) in set(sc.teams)
        m = self.members(app)
        if any(sc.allows(h) for h in m.hosts):
            return True
        su = self._service_universe()
        return any(m.svc_match(s) and sc.allows_service(s, hs) for s, hs in su.items())

    def visible_list(self, sc) -> list:
        return [a for a in self.list() if self.visible(a, sc)]

    def scoped(self, app: dict, sc) -> tuple:
        """(member hosts the viewer sees -> via, service predicate over what the viewer may see, host filter for service lines)."""
        m = self.members(app)
        if sc is None:
            return dict(m.hosts), m.svc_match, None
        su = self._service_universe()
        hosts = {h: v for h, v in m.hosts.items() if sc.allows(h)}
        return hosts, (lambda name, m=m: m.svc_match(name) and sc.allows_service(name, su.get(name))), sc.allows

    def view(self, app: dict, sc, writable: bool = False) -> dict:
        """The record as this viewer may read it: explicit hosts they cannot see are left out."""
        d = dict(app)
        d["members"] = dict(app["members"])
        if sc is not None:
            d["members"]["hosts"] = [h for h in app["members"]["hosts"] if sc.allows(h)]
        d["team"] = self._team_names().get(app.get("team_id"), "") if app.get("team_id") is not None else ""
        d["editable"] = bool(writable)
        return d

    # ---------------------------------------------------------------- log query (logquery.expand_apps)
    def query_members(self, value: str, wild: bool, scope) -> Optional[tuple]:
        """(hosts, exact services, service globs) of the visible applications whose name equals `value` (or matches it as a glob),
        hosts narrowed to the scope; None when no visible application matches (the caller falls back to the old service alias)."""
        low = (value or "").strip().lower()
        hit = [a for a in self.list() if (fnmatch.fnmatchcase(a["name"].lower(), low) if wild else a["name"].lower() == low) and self.visible(a, scope)]
        if not hit:
            return None
        hosts, exact, globs = set(), set(), []
        for a in hit:
            m = self.members(a)
            hosts |= {h for h in m.hosts if scope is None or scope.allows(h)}
            exact |= m.services
            globs += [g for g in m.globs if g not in globs]
        return hosts, exact, globs

    def names(self, scope) -> list:
        return [a["name"] for a in self.visible_list(scope)]

    # ---------------------------------------------------------------- health
    def _cached(self, key, fn):
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < TTL and hit[1] == self.version:
            return hit[2]
        val = fn()
        if len(self._cache) > 400:
            self._cache.clear()
        self._cache[key] = (now, self.version, val)
        return val

    def _trace_rows(self, minutes: int) -> list:
        now = time.time()
        hit = self._trows.get(minutes)
        if hit and now - hit[0] < TTL:
            return hit[1]
        tr = getattr(self.svc, "traces", None)
        rows = []
        if tr is not None:
            try:
                rows = [r for r in tr._rollups(tr._since(minutes)) if r["kind"] in INBOUND]
            except Exception:  # noqa: BLE001
                rows = []
        self._trows[minutes] = (now, rows)
        return rows

    def _alert_rows(self) -> tuple:
        now = time.time()
        if self._alerts and now - self._alerts[0] < TTL:
            return self._alerts[1], self._alerts[2]
        thr, an = [], []
        try:
            thr = self.kb._exec("SELECT host, metric, level, value, limit_value, since, policy_id FROM threshold_state WHERE level <> 'ok'")
        except Exception:  # noqa: BLE001
            pass
        try:
            an = self.kb._exec("SELECT id, kind, host, service, env, metric, title, status, score, first_seen, last_seen FROM anomalies "
                               "WHERE status IN ('open', 'ack') ORDER BY last_seen DESC LIMIT 5000")
        except Exception:  # noqa: BLE001
            pass
        self._alerts = (now, thr, an)
        return thr, an

    @staticmethod
    def _an_match(r: dict, hosts: dict, svc_ok, host_ok) -> bool:
        h = r.get("host") or ""
        if h and h in hosts:
            return True
        s = r.get("service") or ""
        return bool(s and svc_ok(s) and (not h or host_ok is None or host_ok(h)))

    def _fold(self, groups: dict, minutes, host_ok, step: Optional[int] = None) -> dict:
        r = getattr(getattr(self.svc, "live", None), "rollup", None)
        if r is None:
            return {}
        host_groups: dict = defaultdict(list)
        for gid, (hosts, _s, _h) in groups.items():
            for h in hosts:
                host_groups[h].append(gid)
        fns = [(gid, g[1]) for gid, g in groups.items()]
        return r.fold_groups(minutes, host_groups, lambda s: [gid for gid, f in fns if f(s)], host_ok, step)

    def _trace_fold(self, groups: dict, minutes: int, step: Optional[int] = None) -> dict:
        out: dict = {}
        memo: dict = {}
        tmemo: dict = {}
        for r in self._trace_rows(minutes):
            s = r["service"]
            gs = memo.get(s)
            if gs is None:
                gs = memo[s] = [gid for gid, g in groups.items() if g[1](s)]
            if not gs:
                continue
            b = None
            if step:
                t = tmemo.get(r["minute"])
                if t is None:
                    try:
                        t = tmemo[r["minute"]] = int(datetime.strptime(r["minute"], "%Y-%m-%dT%H:%M").replace(tzinfo=UTC).timestamp())
                    except ValueError:
                        continue
                b = (t // (step * 60)) * step * 60
            for gid in gs:
                d = out.setdefault(gid, {"calls": 0, "errors": 0, "hist": [0] * NB, "services": {}, "series": {}})
                d["calls"] += r["n"]; d["errors"] += r["err"]
                for i, c in enumerate(r["hist"][:NB]):
                    d["hist"][i] += c
                x = d["services"].setdefault(s, [0, 0, [0] * NB])
                x[0] += r["n"]; x[1] += r["err"]
                for i, c in enumerate(r["hist"][:NB]):
                    x[2][i] += c
                if b is not None:
                    y = d["series"].setdefault(b, [0, 0, [0] * NB])
                    y[0] += r["n"]; y[1] += r["err"]
                    for i, c in enumerate(r["hist"][:NB]):
                        y[2][i] += c
        return out

    @staticmethod
    def slo(app: dict, calls: int, errs: int, t_p95, lines: int, lerrs: int, l_p95) -> dict:
        ta, tp = app.get("slo_availability"), app.get("slo_p95_ms")
        src = "traces" if calls else "logs" if lines else None
        avail = (1 - errs / calls) if calls else (1 - lerrs / lines) if lines else None
        p95 = t_p95 if t_p95 is not None else l_p95
        if ta is None and tp is None:
            state = "none"
        elif avail is None and p95 is None:
            state = "unknown"
        elif (ta is not None and avail is not None and avail * 100 < ta) or (tp is not None and p95 is not None and p95 > tp):
            state = "breach"
        else:
            state = "ok"
        budget = None
        if ta is not None and ta < 100 and avail is not None:
            budget = round(max(0.0, 1 - (1 - avail) / (1 - ta / 100)), 4)
        return {"availability": None if avail is None else round(avail, 6), "p95_ms": p95, "target_availability": ta, "target_p95_ms": tp,
                "state": state, "source": src, "budget_left": budget}

    def health(self, apps: list, sc, minutes: int = 60) -> dict:
        """app id -> health over the last `minutes` (5..60, the live counters' reach), as the viewer `sc` may see it."""
        minutes = max(5, min(int(minutes or 60), 60))
        key = ("h", tuple(sorted((a["id"], a.get("updated_at")) for a in apps)), scope_key(sc), minutes)
        return self._cached(key, lambda: self._health(apps, sc, minutes))

    def _health(self, apps: list, sc, minutes: int) -> dict:
        groups = {a["id"]: self.scoped(a, sc) for a in apps}
        step = max(1, minutes // 20)
        logs = self._fold(groups, minutes, None if sc is None else sc.allows, step)
        tr = self._trace_fold(groups, minutes)
        thr, an = self._alert_rows()
        r = getattr(getattr(self.svc, "live", None), "rollup", None)
        last_seen = r.last_seen if r is not None else {}
        now = time.time()
        m0 = (int(now // 60) - minutes + 1) * 60
        out = {}
        for a in apps:
            hosts, svc_ok, host_ok = groups[a["id"]]
            lg = logs.get(a["id"]) or {"lines": 0, "errors": 0, "hist": [], "hosts": {}, "services": {}, "svc_hosts": {}, "series": {}}
            t = tr.get(a["id"]) or {"calls": 0, "errors": 0, "hist": [], "services": {}, "series": {}}
            rep = sil = unk = 0
            for h in hosts:
                v = last_seen.get(h)
                if not v:
                    unk += 1
                elif now - v > SILENT_S:
                    sil += 1
                else:
                    rep += 1
            crit = warn = 0
            lv: dict = {}
            for s in thr:
                if s["host"] in hosts:
                    if s["level"] == "crit":
                        crit += 1
                    elif s["level"] == "warn":
                        warn += 1
                    if s["level"] in ("warn", "crit") and lv.get(s["host"]) != "crit":
                        lv[s["host"]] = s["level"]
            anomalies = sum(1 for x in an if self._an_match(x, hosts, svc_ok, host_ok))
            l_p95, t_p95 = _q(lg["hist"], 0.95), _q(t["hist"], 0.95)
            slo = self.slo(a, t["calls"], t["errors"], t_p95, lg["lines"], lg["errors"], l_p95)
            er = lg["errors"] / lg["lines"] if lg["lines"] else None
            ter = t["errors"] / t["calls"] if t["calls"] else None
            worst_h = None
            for h, (ln, e) in lg["hosts"].items():
                cand = {"host": h, "error_rate": round(e / ln, 4) if ln else 0, "errors": e, "level": lv.get(h, "ok")}
                rank = ({"crit": 2, "warn": 1}.get(cand["level"], 0), cand["error_rate"] if ln >= 20 else 0, e)
                if worst_h is None or rank > worst_h[0]:
                    worst_h = (rank, cand)
            for h, level in lv.items():
                if h not in lg["hosts"] and (worst_h is None or ({"crit": 2, "warn": 1}[level], 0, 0) > worst_h[0]):
                    worst_h = (({"crit": 2, "warn": 1}[level], 0, 0), {"host": h, "error_rate": None, "errors": 0, "level": level})
            worst_s = None
            for s, (n, e, hh) in t["services"].items():
                if n >= 10 and (worst_s is None or (e / n, e) > worst_s[0]):
                    worst_s = ((e / n, e), {"service": s, "error_rate": round(e / n, 4), "errors": e, "source": "traces", "p95_ms": _q(hh, 0.95)})
            if worst_s is None:
                for s, (n, e) in lg["services"].items():
                    if n >= 20 and e and (worst_s is None or (e / n, e) > worst_s[0]):
                        worst_s = ((e / n, e), {"service": s, "error_rate": round(e / n, 4), "errors": e, "source": "logs", "p95_ms": None})
            reasons = []
            if slo["state"] == "breach":
                reasons.append("slo_breach")
            if crit:
                reasons.append("thr_crit")
            if hosts and rep == 0 and sil > 0:
                reasons.append("all_silent")
            if warn:
                reasons.append("thr_warn")
            if anomalies:
                reasons.append("anomaly")
            if sil and rep:
                reasons.append("silent")
            if (er or 0) >= 0.05 or (ter or 0) >= 0.05:
                reasons.append("errors_high")
            has = bool(lg["lines"] or t["calls"] or rep)
            status = ("crit" if any(x in reasons for x in ("slo_breach", "thr_crit", "all_silent")) else
                      "warn" if reasons else "ok" if has else "nodata")
            step_s = step * 60
            spark = [lg["series"].get(m0 + i * step_s, [0, 0])[0] for i in range((minutes + step - 1) // step)]
            out[a["id"]] = {
                "status": status, "reasons": reasons, "window_min": minutes,
                "requests": {"calls": t["calls"], "errors": t["errors"], "error_rate": None if ter is None else round(ter, 5),
                             "per_min": round(t["calls"] / minutes, 2), "p95_ms": t_p95},
                "logs": {"lines": lg["lines"], "errors": lg["errors"], "error_rate": None if er is None else round(er, 5),
                         "per_min": round(lg["lines"] / minutes, 1), "p95_ms": l_p95},
                "slo": slo, "hosts": {"total": len(hosts), "reporting": rep, "silent": sil, "unknown": unk, "via_service": len(lg["svc_hosts"])},
                "alerts": {"crit": crit, "warn": warn, "anomalies": anomalies},
                "worst_host": worst_h[1] if worst_h else None, "worst_service": worst_s[1] if worst_s else None, "spark": spark}
        return out

    # ---------------------------------------------------------------- one application in detail
    def detail(self, app: dict, sc, minutes: int = 60) -> dict:
        minutes = max(5, min(int(minutes or 60), 60))
        return self._cached(("d", app["id"], app.get("updated_at"), scope_key(sc), minutes), lambda: self._detail(app, sc, minutes))

    def _detail(self, app: dict, sc, minutes: int) -> dict:
        h = self.health([app], sc, minutes)[app["id"]]
        hosts, svc_ok, host_ok = self.scoped(app, sc)
        grp = {app["id"]: (hosts, svc_ok, host_ok)}
        lg = self._fold(grp, minutes, None if sc is None else sc.allows).get(app["id"]) or {"hosts": {}, "services": {}, "svc_hosts": {}}
        t = self._trace_fold(grp, minutes).get(app["id"]) or {"services": {}}
        thr, an = self._alert_rows()
        lv: dict = {}
        for s in thr:
            if s["host"] in hosts and lv.get(s["host"]) != "crit":
                lv[s["host"]] = s["level"]
        uni = self._hosts_universe()
        r = getattr(getattr(self.svc, "live", None), "rollup", None)
        last_seen = r.last_seen if r is not None else {}
        now = time.time()

        def host_row(name, via, counts):
            ln, e = counts or (0, 0)
            seen = last_seen.get(name)
            age = round(now - seen) if seen else None
            return {"host": name, "via": via, "env": (uni.get(name) or {}).get("env", ""), "pool": (uni.get(name) or {}).get("pool", ""),
                    "lines": ln, "errors": e, "error_rate": round(e / ln, 4) if ln else None, "per_min": round(ln / minutes, 1),
                    "last_seen_s": age, "state": "unknown" if age is None else "silent" if age > SILENT_S else "reporting", "level": lv.get(name, "ok")}
        host_rows = [host_row(x, via, lg["hosts"].get(x)) for x, via in sorted(hosts.items())]
        svc_host_rows = [host_row(x, "service", c) for x, c in sorted(lg["svc_hosts"].items(), key=lambda kv: -kv[1][0])][:200]
        names = set(lg["services"]) | set(t["services"])
        su = self._service_universe()
        names |= {s for s in su if svc_ok(s)}
        services = []
        for s in names:
            ln, e = lg["services"].get(s, (0, 0))
            n, te, hh = t["services"].get(s, (0, 0, []))
            services.append({"service": s, "member": bool(svc_ok(s)), "lines": ln, "errors": e, "error_rate": round(e / ln, 4) if ln else None,
                             "calls": n, "call_errors": te, "call_error_rate": round(te / n, 4) if n else None, "rps": round(n / minutes / 60, 3),
                             "p95_ms": _q(hh, 0.95) if hh else None})
        services.sort(key=lambda x: (not x["member"], -(x["call_errors"] + x["errors"]), -(x["calls"] + x["lines"])))
        m = self.members(app)
        nsmap = self.namespaces()
        rum = []
        try:
            have = {a["name"] for a in self.svc.rum.apps()}
        except Exception:  # noqa: BLE001
            have = set()
        for n in m.rum_apps:
            if sc is None or sc.allows_service(n):
                rum.append({"name": n, "exists": n in have})
        flows = []
        try:
            teams = set(sc.teams) if sc is not None else None
            tn = self._team_names()
            for f in self.svc.bizflows.list():
                if f["id"] in m.flows and (teams is None or f.get("team_id") is None or tn.get(f.get("team_id")) in teams):
                    flows.append({"id": f["id"], "name": f["name"]})
        except Exception:  # noqa: BLE001
            pass
        return {"health": h, "hosts": host_rows, "service_hosts": svc_host_rows, "services": services[:300],
                "namespaces": [{"namespace": n, "services": sorted(s for s in (nsmap.get(n) or {}).get("services", ()) if svc_ok(s))} for n in m.namespaces],
                "rum_apps": rum, "flows": flows, "log_query": self.log_query(app)}

    @staticmethod
    def log_query(app: dict) -> str:
        n = app["name"]
        return f'app:"{n}"' if re.search(r'[\s():"]', n) or n.startswith("-") else f"app:{n}"

    def series(self, app: dict, sc, minutes: int = 60) -> dict:
        minutes = max(5, min(int(minutes or 60), 10080))
        return self._cached(("s", app["id"], app.get("updated_at"), scope_key(sc), minutes), lambda: self._series(app, sc, minutes))

    def _series(self, app: dict, sc, minutes: int) -> dict:
        hosts, svc_ok, host_ok = self.scoped(app, sc)
        grp = {app["id"]: (hosts, svc_ok, host_ok)}
        step = 1 if minutes <= 60 else 5 if minutes <= 360 else 15 if minutes <= 1440 else 60 if minutes <= 4320 else 180
        now_m = int(time.time() // 60)
        first = (now_m - minutes + 1) // step * step
        rows: dict = {}
        source = "live"
        if minutes <= 60:
            lg = self._fold(grp, minutes, None if sc is None else sc.allows, step).get(app["id"]) or {"series": {}}
            first = now_m - minutes + 1
            for b, (ln, e) in lg["series"].items():
                rows.setdefault(b, {})["lines"] = ln; rows[b]["errors"] = e
        else:
            source = "history"
            for b, (ln, e) in self._history(hosts, svc_ok, minutes, step).items():
                rows.setdefault(b, {})["lines"] = ln; rows[b]["errors"] = e
        t = self._trace_fold(grp, minutes, step).get(app["id"]) or {"series": {}}
        for b, (n, e, hh) in t["series"].items():
            x = rows.setdefault(b, {})
            x["calls"] = n; x["call_errors"] = e; x["p95_ms"] = _q(hh, 0.95)
        out = []
        for i in range((minutes + step - 1) // step + 1):
            b = (first + i * step) * 60
            if b > now_m * 60:
                break
            x = rows.get(b, {})
            ln, e, n, ce = x.get("lines", 0), x.get("errors", 0), x.get("calls", 0), x.get("call_errors", 0)
            out.append({"t": b, "lines": ln, "errors": e, "error_rate": round(e / ln, 4) if ln else None, "calls": n, "call_errors": ce,
                        "call_error_rate": round(ce / n, 4) if n else None, "p95_ms": x.get("p95_ms")})
        return {"step_min": step, "source": source, "series": out}

    def _history(self, hosts: dict, svc_ok, minutes: int, step: int) -> dict:
        """Lines / ERROR+ per bucket beyond the live counters: the member hosts' minute history (slo_minute), or for an application
        without member hosts its services' hourly history (slo_hour_service)."""
        since = datetime.now(UTC) - timedelta(minutes=minutes)
        out: dict = defaultdict(lambda: [0, 0])
        sz = step * 60

        def bucket(s: str) -> Optional[int]:
            try:
                t = int(datetime.strptime(s, "%Y-%m-%dT%H:%M" if len(s) > 13 else "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp())
            except ValueError:
                return None
            return t // sz * sz
        try:
            if hosts:
                hs = sorted(hosts)
                for i in range(0, len(hs), 400):
                    chunk = hs[i:i + 400]
                    for r in self.kb._exec(f"SELECT minute, SUM(total) AS t, SUM(errors) AS e FROM slo_minute WHERE minute >= ? AND host IN ({','.join('?' * len(chunk))}) "
                                           "GROUP BY minute", (since.strftime("%Y-%m-%dT%H:%M"), *chunk)):
                        b = bucket(r["minute"])
                        if b is not None:
                            out[b][0] += int(r["t"] or 0); out[b][1] += int(r["e"] or 0)
            else:
                for r in self.kb._exec("SELECT hour, service, SUM(total) AS t, SUM(errors) AS e FROM slo_hour_service WHERE hour >= ? GROUP BY hour, service",
                                       (since.strftime("%Y-%m-%dT%H"),)):
                    if svc_ok(r["service"]):
                        b = bucket(r["hour"])
                        if b is not None:
                            out[b][0] += int(r["t"] or 0); out[b][1] += int(r["e"] or 0)
        except Exception:  # noqa: BLE001 - no history tables (engine used alone)
            pass
        return out

    def events(self, app: dict, sc, minutes: int = 1440, limit: int = 200) -> list:
        """Anomalies (threshold breaches included) on the application's hosts / services: open ones and those seen in the range."""
        hosts, svc_ok, host_ok = self.scoped(app, sc)
        since = (datetime.now(UTC) - timedelta(minutes=max(5, min(int(minutes), 43200)))).isoformat(timespec="seconds")
        try:
            rows = self.kb._exec("SELECT id, kind, key, env, host, service, metric, title, detail, status, score, hits, first_seen, last_seen, owner FROM anomalies "
                                 "WHERE last_seen >= ? OR status IN ('open', 'ack') ORDER BY last_seen DESC LIMIT 5000", (since,))
        except Exception:  # noqa: BLE001
            return []
        return [r for r in rows if self._an_match(r, hosts, svc_ok, host_ok)][:max(1, min(int(limit), 500))]

    def thresholds_view(self, app: dict, sc) -> dict:
        """Open threshold states on the member hosts the viewer sees, and the policies that name this application."""
        hosts, _s, _h = self.scoped(app, sc)
        thr, _an = self._alert_rows()
        states = [dict(s) for s in thr if s["host"] in hosts]
        pol = []
        try:
            tn = self._team_names()
            teams = set(sc.teams) if sc is not None else None
            ids = set(self._policies(app["name"]))
            for p in self.kb._exec("SELECT * FROM threshold_policies WHERE scope='application' ORDER BY metric, id"):
                if p["id"] in ids and (teams is None or p.get("team_id") is None or tn.get(p.get("team_id")) in teams):
                    pol.append({**dict(p), "team": tn.get(p.get("team_id"), "") if p.get("team_id") is not None else ""})
        except Exception:  # noqa: BLE001
            pass
        return {"states": states, "policies": pol, "hosts": sorted(hosts)}

    # ---------------------------------------------------------------- discovery
    def dismissed(self, owner: str) -> set:
        try:
            return {r["key"] for r in self.kb._exec("SELECT key FROM app_dismissed WHERE owner IN ('*', ?)", (owner,))}
        except Exception:  # noqa: BLE001
            return set()

    def dismiss(self, key: str, owner: str) -> None:
        self.kb._exec("DELETE FROM app_dismissed WHERE key=? AND owner=?", (key[:300], owner))
        self.kb._exec("INSERT INTO app_dismissed (key, owner, at) VALUES (?,?,?)", (key[:300], owner, _now()))

    def discover(self, sc, owner: str = "*") -> dict:
        """Proposals (inventory application values, OTel namespaces, service-name groups) and what no application covers, as the
        viewer may see it. Existing applications are only compared with, never changed."""
        uni = self._hosts_universe()
        su = self._service_universe()
        vis_h = {h for h in uni if sc is None or sc.allows(h)}
        vis_s = {s for s, hs in su.items() if sc is None or sc.allows_service(s, hs)}
        apps = self.visible_list(sc)
        mems = [(a, self.members(a)) for a in apps]
        gone = self.dismissed(owner)
        names = {a["name"].lower(): a["id"] for a in apps}
        props = []

        def add(key, source, name, hosts, services, members):
            if key in gone or (not hosts and not services):
                return
            props.append({"key": key, "source": source, "name": name, "hosts": sorted(hosts)[:200], "host_count": len(hosts),
                          "services": sorted(services)[:200], "service_count": len(services), "members": members, "exists": names.get(name.lower())})
        by_inv: dict = defaultdict(set)
        for h in vis_h:
            v = uni[h]["application"]
            if v:
                by_inv[v].add(h)
        covered_inv = {x.lower() for a, _m in mems for x in a["members"]["inv_apps"]}
        for v, hs in sorted(by_inv.items()):
            if v.lower() not in covered_inv:
                add("inv:" + v.lower(), "inventory", v, hs, set(), {"inv_apps": [v]})
        covered_ns = {x.lower() for a, _m in mems for x in a["members"]["namespaces"]}
        ns_svcs: set = set()
        for ns, d in sorted(self.namespaces().items()):
            ss = {s for s in d["services"] if s in vis_s}
            ns_svcs |= {s.lower() for s in ss}
            if ns.lower() not in covered_ns:
                add("ns:" + ns.lower(), "otel", ns, {h for h in d["hosts"] if h in vis_h}, ss, {"namespaces": [ns]})
        matched = lambda s: any(m.svc_match(s) for _a, m in mems)   # noqa: E731
        groups: dict = defaultdict(list)
        for s in vis_s:
            mm = re.match(r"^([a-z][a-z0-9]{1,30})([-_.])", s.lower())
            if mm and mm.group(1) not in GENERIC and not matched(s) and s.lower() not in ns_svcs:
                groups[(mm.group(1), mm.group(2))].append(s)
        for (pre, sep), ss in sorted(groups.items()):
            if len(ss) >= 2:
                add("svc:" + pre, "services", pre, set(), set(ss), {"services": [f"{pre}{sep}*"]})
        in_app = set()
        for _a, m in mems:
            in_app |= set(m.hosts)
        un_h = sorted(h for h in vis_h if h not in in_app)
        un_s = sorted(s for s in vis_s if not matched(s))
        return {"proposals": props, "unmatched_hosts": [{"host": h, "env": uni[h]["env"], "pool": uni[h]["pool"], "application": uni[h]["application"]} for h in un_h[:500]],
                "unmatched_services": [{"service": s, "hosts": len(su.get(s) or ())} for s in un_s[:500]],
                "counts": {"hosts": len(un_h), "services": len(un_s)}}

    def catalog(self, sc) -> dict:
        """What the editor offers: hosts, inventory application values, pools, services, namespaces the viewer may see."""
        uni = self._hosts_universe()
        su = self._service_universe()
        hs = sorted(h for h in uni if sc is None or sc.allows(h))
        return {"hosts": hs[:3000], "inv_apps": sorted({uni[h]["application"] for h in hs if uni[h]["application"]}),
                "pools": sorted({uni[h]["pool"] for h in hs if uni[h]["pool"]}),
                "services": sorted(s for s, h in su.items() if sc is None or sc.allows_service(s, h))[:3000],
                "namespaces": sorted(ns for ns, d in self.namespaces().items() if sc is None or any(sc.allows_service(s, su.get(s)) for s in d["services"]))}

    # ---------------------------------------------------------------- demo mode
    def seed_demo(self, flow_id: Optional[int] = None) -> int:
        """Sample applications over the simulator's hosts and services (demo mode only; origin 'demo', removed by the demo clean-up)."""
        from . import demo
        if not demo.enabled():
            return 0
        n = 0
        for d in DEMO_APPS:
            if self.by_name(d["name"]) is not None:
                continue
            m = dict(d["members"])
            if d.get("flow") and flow_id:
                m["flows"] = [flow_id]
            try:
                self.save({**d, "members": m}, origin="demo", by="demo (simulator)")
                n += 1
            except ValueError:
                pass
        return n


DEMO_APPS = (
    {"name": "Ödeme", "description": "Ödeme ve sepet API'leri (demo)", "criticality": "critical", "tags": ["demo", "finans"], "slo_availability": 99.5,
     "slo_p95_ms": 800, "members": {"hosts": ["prd-api-01", "prd-api-02", "pay-api-01"], "services": ["payment-api", "checkout-api"]}},
    {"name": "İnsan kaynakları", "description": "İK portalı, mobil uygulama ve bordro (demo)", "criticality": "high", "tags": ["demo", "ik"],
     "slo_availability": 99.0, "slo_p95_ms": 1500, "members": {"hosts": ["hr-app-01", "hr-db", "payroll-db"], "services": ["hr-*"], "rum_apps": ["ik-portal", "ik-mobil"]}},
    {"name": "SAP ERP", "description": "SAP ECC, HANA ve ERP entegrasyonu (demo)", "criticality": "high", "tags": ["demo", "erp"], "slo_availability": 99.5,
     "members": {"hosts": ["sap-ecc-01", "hana-01", "erp-int-01"]}},
    {"name": "Sipariş", "description": "Arama, kimlik ve bildirim servisleri; Sipariş → Fatura akışı (demo)", "criticality": "normal", "tags": ["demo"], "flow": True,
     "members": {"hosts": ["worker-01"], "services": ["search-api", "auth-api", "notification-service"]}},
)


def _demo_purge(agent: str, hosts, dry_run: bool) -> dict:
    """demo.clean: the sample applications the simulator created (origin 'demo')."""
    st = _PURGER.get("store")
    if st is None:
        return {}
    try:
        rows = st.kb._exec("SELECT id FROM applications WHERE origin='demo'")
    except Exception:  # noqa: BLE001
        return {}
    if not dry_run:
        for r in rows:
            try:
                st.delete(r["id"])
            except KeyError:
                pass
    return {"applications": len(rows)}
