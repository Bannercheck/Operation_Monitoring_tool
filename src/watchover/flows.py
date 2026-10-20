"""Network flows from the agents (`agent.py --flows`): which process on which host holds connections to which endpoint.

They give the service map edges that no log has to mention: an ERP application server talking to its database, a portal to a
payment gateway, a batch job to an SFTP host. Remote IPs are named from the agents' own addresses and the inventory; the port
names the likely protocol (5432 PostgreSQL, 1521 Oracle, 3200 SAP, ...). Kept as a per-minute-free table: one row per
(host, process, direction, endpoint), with the last count, first / last seen and how many samples saw it.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
PORTS = {22: "ssh", 25: "smtp", 53: "dns", 80: "http", 88: "kerberos", 110: "pop3", 123: "ntp", 143: "imap", 389: "ldap", 443: "https", 445: "smb",
         465: "smtps", 587: "smtp", 636: "ldaps", 1433: "sqlserver", 1521: "oracle", 1522: "oracle", 2049: "nfs", 2181: "zookeeper", 3200: "sap-dialog",
         3201: "sap-dialog", 3300: "sap-rfc", 3301: "sap-rfc", 3306: "mysql", 3389: "rdp", 3600: "sap-ms", 4222: "nats", 5000: "http", 5432: "postgres",
         5601: "kibana", 5672: "amqp", 6379: "redis", 6443: "kubernetes", 7001: "weblogic", 8000: "http", 8080: "http", 8443: "https", 8600: "watchover",
         9000: "http", 9042: "cassandra", 9092: "kafka", 9200: "elasticsearch", 9300: "elasticsearch", 11211: "memcached", 15672: "rabbitmq",
         27017: "mongodb", 30015: "hana", 30013: "hana", 30041: "hana", 50000: "sap-http", 50001: "sap-https", 1414: "ibm-mq",
         0: "jdbc"}                                       # 0: an edge without a telling port (SAP PO's JDBC adapter: the database kind is unknown)
DB_PORTS = {1433, 1521, 1522, 3306, 5432, 6379, 9042, 27017, 30013, 30015, 30041, 11211}
PROXY_PROCS = {"nginx", "httpd", "apache2", "haproxy", "envoy", "traefik", "caddy"}


CATEGORY = {"jdbc": "db", "postgres": "db", "oracle": "db", "mysql": "db", "sqlserver": "db", "hana": "db", "mongodb": "db", "redis": "cache", "memcached": "cache",
            "cassandra": "db", "kafka": "messaging", "amqp": "messaging", "rabbitmq": "messaging", "ibm-mq": "messaging", "nats": "messaging",
            "zookeeper": "messaging", "http": "api", "https": "api", "weblogic": "api", "sap-http": "sap", "sap-https": "sap", "sap-dialog": "sap",
            "sap-rfc": "sap", "sap-ms": "sap", "ldap": "identity", "ldaps": "identity", "kerberos": "identity", "smtp": "mail", "smtps": "mail",
            "imap": "mail", "pop3": "mail", "ssh": "file", "smb": "file", "nfs": "file", "elasticsearch": "observability", "kibana": "observability",
            "watchover": "observability", "dns": "infra", "ntp": "infra", "kubernetes": "infra", "rdp": "remote"}


def category(protocol: str) -> str:
    return CATEGORY.get(protocol, "other")


def port_name(port: int) -> str:
    return PORTS.get(port) or (f"hana" if 30000 <= port < 40000 and str(port).endswith("15") else f"tcp/{port}")


_TAG = re.compile(r"^[a-z0-9][a-z0-9_.+-]{0,31}$")


_KNOWN: frozenset = frozenset()


def clean_tags(v) -> str:
    """The agent's flow.tech (comma-joined fingerprint tags) as stored: tags of the fixed dictionary only (techstack.TECH and its
    helper tags), sorted, at most 24, read from the first 2 KB. Anything else an agent might send (a list, a number, free text, a
    user name, markup) is dropped, so nothing but known tag names ever reaches the database."""
    global _KNOWN
    if not v or not isinstance(v, str):
        return ""
    if not _KNOWN:
        from .techstack import AMBIGUOUS_TAGS, HELPER_TAGS, TECH   # techstack imports this module: resolved at first use
        _KNOWN = frozenset(TECH) | HELPER_TAGS | AMBIGUOUS_TAGS
    tags = sorted({t for t in (x.strip().lower() for x in v[:2048].split(",")) if t in _KNOWN and _TAG.match(t)})
    return ",".join(tags[:24])


def _tags(v) -> frozenset:
    return frozenset(v.split(",")) if v else frozenset()



def _is_ip(s: str) -> bool:
    import ipaddress
    try:
        ipaddress.ip_address(str(s))
        return True
    except ValueError:
        return False

class FlowStore:
    def __init__(self, kb, agents=None, inventory=None):
        self.kb, self.agents, self.inventory = kb, agents, inventory
        self.lock = threading.Lock()
        kb._exec("""CREATE TABLE IF NOT EXISTS net_flows (host TEXT, process TEXT, dir TEXT, endpoint TEXT, env TEXT DEFAULT '', last_count INTEGER,
            samples INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, PRIMARY KEY (host, process, dir, endpoint))""")
        kb._exec("CREATE INDEX IF NOT EXISTS net_flows_seen ON net_flows(last_seen)")
        kb._exec("CREATE INDEX IF NOT EXISTS net_flows_endpoint ON net_flows(endpoint)")   # who calls a host: flows ending at its addresses
        try:
            if "tech" not in kb.columns("net_flows"):        # agent fingerprints (flow.tech), older tables get the column
                kb._exec("ALTER TABLE net_flows ADD COLUMN tech TEXT DEFAULT ''")
        except Exception:  # noqa: BLE001 - another process added it first
            pass
        from .demo import ensure_column
        ensure_column(kb, "net_flows")                       # 1 = an edge only the simulator reported (demo.py purges these)
        self._names: dict[str, str] = {}
        self._names_at = 0.0
        self.tech = None                                     # TechStack (set by Services): technology of each integration

    def add(self, events) -> int:
        """Flow observations (attributes flow.dir / flow.process / flow.remote | flow.port / flow.count) -> upserts."""
        from .demo import is_demo, keep_real
        rows = []
        for o in events:
            a = o.attributes or {}
            d = a.get("flow.dir")
            ep = a.get("flow.remote") if d == "out" else a.get("flow.port")
            if not ep:                                     # a parser chose the endpoint column as the message: "flow out java 10.0.0.9:5432" or the bare value
                m = re.search(r"(\S+:\d+|\d+)\s*$", o.message or "")
                ep = m.group(1) if m else None
            if d not in ("in", "out") or not ep or not o.host:
                continue
            try:
                n = int(a.get("flow.count") or 1)
            except (TypeError, ValueError):
                n = 1
            ts = (o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)).isoformat()
            rows.append((o.host[:120], str(a.get("flow.process") or "?")[:60], d, str(ep)[:80], o.environment or "", n, ts, ts, clean_tags(a.get("flow.tech")),
                         1 if is_demo(o) else 0))
        if not rows:
            return 0
        with self.lock:
            sql = ("INSERT INTO net_flows (host, process, dir, endpoint, env, last_count, samples, first_seen, last_seen, tech, demo) VALUES (?,?,?,?,?,?,1,?,?,?,?)"
                   + self.kb.upsert_suffix("host, process, dir, endpoint", "last_count=excluded.last_count, samples=net_flows.samples+1, last_seen=excluded.last_seen, "
                                           "env=excluded.env, tech=CASE WHEN excluded.tech <> '' THEN excluded.tech ELSE net_flows.tech END, " + keep_real("net_flows")))
            self.kb.executemany(sql, rows)
        return len(rows)

    def _ip_names(self) -> dict[str, str]:
        """ip -> host name, from the agents' last addresses and the inventory (cached a minute)."""
        import time
        if time.time() - self._names_at < 60:
            return self._names
        names: dict[str, str] = {}
        try:
            for r in self.kb._exec("SELECT hostname, ip FROM inventory WHERE ip <> ''"):
                names[str(r["ip"]).strip()] = r["hostname"]
        except Exception:  # noqa: BLE001
            pass
        try:
            for r in self.kb._exec("SELECT name, last_ip FROM agents WHERE last_ip <> ''"):
                names.setdefault(str(r["last_ip"]).strip(), r["name"])
        except Exception:  # noqa: BLE001
            pass
        self._names, self._names_at = names, time.time()
        return names

    def graph(self, minutes: int | None = 60, env: str | None = None) -> dict:
        """Hosts (with their processes) and outbound edges to named hosts or endpoints, from flows seen in the window."""
        where, p = [], []
        if minutes:
            where.append("last_seen >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        if env:
            where.append("env = ?"); p.append(env)
        rows = self.kb._exec("SELECT * FROM net_flows" + (" WHERE " + " AND ".join(where) if where else ""), tuple(p))
        names = self._ip_names()
        nodes: dict[str, dict] = {}
        edges: dict[tuple, dict] = {}
        listens: dict[str, list] = {}
        for r in rows:
            src = r["host"]
            nodes.setdefault(src, {"id": src, "kind": "host", "env": r["env"], "processes": set(), "agent": True})["processes"].add(r["process"])
            if r["dir"] == "in":
                listens.setdefault(src, []).append({"port": r["endpoint"], "process": r["process"], "peers": r["last_count"]})
                continue
            ip, _, port = r["endpoint"].rpartition(":")
            try:
                port_i = int(port)
            except ValueError:
                port_i = 0
            target = names.get(ip) or ip
            nodes.setdefault(target, {"id": target, "kind": "db" if port_i in DB_PORTS else "host", "env": "", "processes": set(), "agent": target in names.values(), "ip": ip})
            if port_i in DB_PORTS:
                nodes[target]["kind"] = "db"
            e = edges.setdefault((src, target, port_i), {"source": src, "target": target, "port": port_i, "protocol": port_name(port_i), "connections": 0,
                                                        "processes": set(), "last_seen": r["last_seen"], "samples": 0})
            e["connections"] += int(r["last_count"] or 0); e["processes"].add(r["process"]); e["samples"] += int(r["samples"] or 0)
            e["last_seen"] = max(e["last_seen"], r["last_seen"])
        return {"nodes": [{**n, "processes": sorted(n["processes"]), "listens": listens.get(n["id"], [])} for n in nodes.values()],
                "edges": [{**e, "processes": sorted(e["processes"]), "evidence": "flow"} for e in edges.values()]}

    def _rows(self, minutes: int | None, env: str | None = None, cols: str = "*") -> list[dict]:
        where, p = [], []
        if minutes:
            where.append("last_seen >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        if env:
            where.append("env = ?"); p.append(env)
        return self.kb._exec(f"SELECT {cols} FROM net_flows" + (" WHERE " + " AND ".join(where) if where else ""), tuple(p))

    def host_ips(self) -> dict[str, list[str]]:
        """host name -> its known addresses (agents' last addresses, the inventory)."""
        rev: dict[str, list[str]] = {}
        for ip, n in self._ip_names().items():
            rev.setdefault(n, []).append(ip)
        return {h: sorted(v) for h, v in rev.items()}

    def hosts(self, minutes: int | None = 1440, env: str | None = None) -> list[dict]:
        """Every host seen in flows (as a sender or as a named target) with how many integrations it has each way, and (with a
        TechStack attached) the top technologies running on it."""
        names = self._ip_names()
        out: dict[str, dict] = {}
        agents: set = set()
        procs: dict[str, dict] = {}
        for r in self._rows(minutes, env, "host, process, dir, endpoint, env, last_seen, tech"):
            agents.add(r["host"])
            pt = procs.setdefault(r["host"], {}).setdefault(r["process"], set())
            if r.get("tech"):
                pt.update(_tags(r["tech"]))
            h = out.setdefault(r["host"], {"host": r["host"], "env": r["env"], "agent": True, "outbound": set(), "inbound": set(), "protocols": set(), "last_seen": r["last_seen"]})
            h["last_seen"] = max(h["last_seen"], r["last_seen"])
            if r["dir"] == "in":
                h["inbound"].add(("listen", r["endpoint"]))
                h["protocols"].add(port_name(int(r["endpoint"])) if str(r["endpoint"]).isdigit() else r["endpoint"])
                continue
            ip, _, port = r["endpoint"].rpartition(":")
            proto = port_name(int(port)) if port.isdigit() else "tcp"
            tgt = names.get(ip) or ip
            h["outbound"].add((tgt, port)); h["protocols"].add(proto)
            t = out.setdefault(tgt, {"host": tgt, "env": "", "agent": False, "outbound": set(), "inbound": set(), "protocols": set(), "last_seen": r["last_seen"]})
            t["inbound"].add((r["host"], port)); t["protocols"].add(proto)
        tops = self.tech.host_names(procs, list(out), key=(minutes, env)) if self.tech is not None else {}
        rev = self.host_ips()
        return sorted(({**h, "agent": h["host"] in agents, "outbound": len(h["outbound"]), "inbound": len(h["inbound"]), "protocols": sorted(h["protocols"]),
                        "ips": rev.get(h["host"]) or ([h["host"]] if _is_ip(h["host"]) else []),
                        **({"tech": tops.get(h["host"], [])} if self.tech is not None else {})}
                       for h in out.values()), key=lambda x: (not x["agent"], -(x["outbound"] + x["inbound"]), x["host"]))

    def host_map(self, host: str, minutes: int | None = 1440, visible=None, manual_ok=None) -> dict:
        """One host's integrations: who connects to it (other agents' flows that end at its address, plus its listening ports),
        and what it connects to, grouped by category (database, messaging, API, SAP, identity, mail, file transfer ...). With a
        TechStack attached every integration carries "tech" (which technology it runs on) and the host its "stack"; `visible`
        (host -> bool) limits the evidence to hosts the reader may see, `manual_ok` (team_id -> bool) the manual overrides."""
        names = self._ip_names()
        my_ips = {ip for ip, n in names.items() if n == host}
        rows = self._where(minutes, "host = ?", (host,))     # indexed reads only: this host's rows and the flows that end at its addresses
        for ip in sorted(my_ips | {host}):
            rows += [r for r in self._where(minutes, "dir = 'out' AND endpoint >= ? AND endpoint < ?", (ip + ":", ip + ";")) if r["host"] != host]
        listens, outbound, inbound = [], {}, {}
        procs: set[str] = set()
        ctx = {"listen": {}, "proxy": {}, "procs": {}, "names": names, "minutes": minutes}   # what the TechStack needs: listeners and proxy upstreams of the peers
        if self.tech is not None:
            self._peer_ctx(ctx, rows, host, minutes)
        for r in rows:
            tg = _tags(r.get("tech"))
            if r["host"] == host:
                procs.add(r["process"])
                ctx["procs"].setdefault(r["process"], set()).update(tg)
                if r["dir"] == "in":
                    port = int(r["endpoint"]) if str(r["endpoint"]).isdigit() else 0
                    listens.append({"port": r["endpoint"], "protocol": port_name(port) if port else r["endpoint"], "process": r["process"], "peers": r["last_count"],
                                    "last_seen": r["last_seen"], "_tags": tg})
                    continue
                ip, _, port = r["endpoint"].rpartition(":")
                port_i = int(port) if port.isdigit() else 0
                proto = port_name(port_i)
                tgt = names.get(ip) or ip
                k = (tgt, port_i)
                o = outbound.setdefault(k, {"target": tgt, "ip": ip, "port": port_i, "protocol": proto, "category": category(proto), "known": tgt != ip,
                                            "processes": set(), "connections": 0, "samples": 0, "first_seen": r["first_seen"], "last_seen": r["last_seen"], "_procs": {}})
                o["processes"].add(r["process"]); o["connections"] += int(r["last_count"] or 0); o["samples"] += int(r["samples"] or 0)
                o["_procs"].setdefault(r["process"], set()).update(tg)
                o["first_seen"] = min(o["first_seen"], r["first_seen"]); o["last_seen"] = max(o["last_seen"], r["last_seen"])
            elif r["dir"] == "out":
                ip, _, port = r["endpoint"].rpartition(":")
                if ip in my_ips or ip == host:
                    port_i = int(port) if port.isdigit() else 0
                    k = (r["host"], port_i)
                    i = inbound.setdefault(k, {"source": r["host"], "port": port_i, "protocol": port_name(port_i), "category": category(port_name(port_i)),
                                               "processes": set(), "connections": 0, "last_seen": r["last_seen"], "_procs": {}})
                    i["processes"].add(r["process"]); i["connections"] += int(r["last_count"] or 0); i["last_seen"] = max(i["last_seen"], r["last_seen"])
                    i["_procs"].setdefault(r["process"], set()).update(tg)
        rev = self.host_ips()
        for i in inbound.values():                           # the caller's addresses, shown under its name on the map
            i["ips"] = rev.get(i["source"]) or ([i["source"]] if _is_ip(i["source"]) else [])
        fix = lambda d: {**d, "processes": sorted(d["processes"])}   # noqa: E731
        out_l = sorted((fix(o) for o in outbound.values()), key=lambda x: (x["category"], -x["connections"]))
        by_cat: dict[str, int] = {}
        for o in out_l:
            by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
        m = {"host": host, "ips": sorted(my_ips), "processes": sorted(procs), "listens": sorted(listens, key=lambda x: str(x["port"])),
             "outbound": out_l, "inbound": sorted((fix(i) for i in inbound.values()), key=lambda x: -x["connections"]), "categories": by_cat}
        if self.tech is not None:
            try:
                self.tech.annotate(m, ctx, visible=visible, manual_ok=manual_ok)
            except Exception:  # noqa: BLE001 - the map never fails because of the technology guess
                import logging
                logging.getLogger(__name__).exception("techstack annotate failed")
        for d in (*m["outbound"], *m["inbound"], *m["listens"]):
            d.pop("_procs", None); d.pop("_tags", None)
        return m

    def app_map(self, name: str, hosts, minutes: int | None = 1440, visible=None, manual_ok=None, cap: int = 60) -> dict:
        """An application's integrations: the maps of its member hosts folded into one. Callers and targets outside the application
        are its integrations, each with the member hosts it goes through ("via"); flows between two members are "internal". Same
        shape as host_map (the map draws it the same way) plus "members", "internal" and "app": True."""
        members = sorted(set(hosts))[:cap]
        maps = [self.host_map(h, minutes, visible, manual_ok) for h in members]
        mset = {h.lower() for h in members}
        mips = {ip for m in maps for ip in m["ips"]}
        inside = lambda peer, ip="": str(peer).lower() in mset or (ip and ip in mips)   # noqa: E731
        outb: dict = {}; inb: dict = {}; internal: dict = {}; stack: dict = {}
        listens, procs, rows = [], set(), []
        for h, m in zip(members, maps):
            rows.append({"host": h, "ips": m["ips"], "inbound": len(m["inbound"]), "outbound": len(m["outbound"]), "listens": len(m["listens"])})
            procs |= set(m["processes"])
            listens += [{**l, "host": h} for l in m["listens"]]
            for s in m.get("stack") or []:
                stack.setdefault(s.get("key") or s.get("name"), s)
            for o in m["outbound"]:
                if inside(o["target"], o.get("ip", "")):
                    k = (h, o["target"], o["port"])
                    e = internal.setdefault(k, {"source": h, "target": o["target"], "port": o["port"], "protocol": o["protocol"], "tech": o.get("tech"), "connections": 0})
                    e["connections"] += o["connections"]
                    continue
                x = outb.setdefault((o["target"], o["port"]), {**o, "processes": set(), "connections": 0, "samples": 0, "via": set()})
                x["processes"] |= set(o["processes"]); x["connections"] += o["connections"]; x["samples"] += o.get("samples", 0); x["via"].add(h)
                x["first_seen"] = min(x["first_seen"], o["first_seen"]); x["last_seen"] = max(x["last_seen"], o["last_seen"])
            for i in m["inbound"]:
                if inside(i["source"], (i.get("ips") or [""])[0]):
                    continue                                 # counted once, from the calling member's side
                x = inb.setdefault((i["source"], i["port"]), {**i, "processes": set(), "connections": 0, "via": set()})
                x["processes"] |= set(i["processes"]); x["connections"] += i["connections"]; x["via"].add(h)
                x["last_seen"] = max(x["last_seen"], i["last_seen"])
        fix = lambda d: {**d, "processes": sorted(d["processes"]), "via": sorted(d["via"])}   # noqa: E731
        out_l = sorted((fix(o) for o in outb.values()), key=lambda x: (x["category"], -x["connections"]))
        by_cat: dict[str, int] = {}
        for o in out_l:
            by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
        return {"host": name, "app": True, "members": rows, "truncated": max(0, len(set(hosts)) - cap), "ips": sorted(mips), "processes": sorted(procs),
                "listens": sorted(listens, key=lambda x: (x["host"], str(x["port"]))), "outbound": out_l,
                "inbound": sorted((fix(i) for i in inb.values()), key=lambda x: -x["connections"]),
                "internal": sorted(internal.values(), key=lambda x: -x["connections"]), "categories": by_cat, "stack": list(stack.values())}

    def _where(self, minutes: int | None, cond: str, params: tuple) -> list[dict]:
        where, p = [cond], list(params)
        if minutes:
            where.append("last_seen >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        return self.kb._exec("SELECT * FROM net_flows WHERE " + " AND ".join(where), tuple(p))

    def _peer_ctx(self, ctx: dict, rows: list, host: str, minutes: int | None) -> None:
        """Listening processes and proxy upstreams of this host, of the hosts it calls and (two hops deep) of what their proxies
        forward to: the server side of every integration, read by host (primary key) instead of scanning the window."""
        names = ctx["names"]
        need, loaded, extra = {host}, set(), list(rows)
        for depth in range(4):
            for r in extra:
                if r["host"] not in need:
                    continue
                if r["dir"] == "in":
                    ctx["listen"].setdefault((r["host"], str(r["endpoint"])), {}).setdefault(r["process"], set()).update(_tags(r.get("tech")))
                elif r["process"] in PROXY_PROCS:
                    ctx["proxy"].setdefault(r["host"], []).append((r["process"], r["endpoint"], int(r["last_count"] or 0), _tags(r.get("tech"))))
            loaded |= need
            nxt = set()
            for r in extra:
                if r["host"] in need and r["dir"] == "out" and (r["host"] == host or r["process"] in PROXY_PROCS):
                    ip = str(r["endpoint"]).rpartition(":")[0]
                    nxt.add(names.get(ip) or ip)
            need = nxt - loaded
            if not need or depth == 3:
                break
            extra = []
            hs = sorted(need)
            for i in range(0, len(hs), 200):
                chunk = hs[i:i + 200]
                extra += self._where(minutes, f"host IN ({','.join('?' * len(chunk))}) AND (dir = 'in' OR process IN ({','.join('?' * len(PROXY_PROCS))}))",
                                     (*chunk, *sorted(PROXY_PROCS)))

    def prune(self, days: int = 7) -> None:
        self.kb._exec("DELETE FROM net_flows WHERE last_seen < ?", ((datetime.now(UTC) - timedelta(days=days)).isoformat(),))
