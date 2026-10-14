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
         27017: "mongodb", 30015: "hana", 30013: "hana", 30041: "hana", 50000: "sap-http", 50001: "sap-https", 1414: "ibm-mq"}
DB_PORTS = {1433, 1521, 1522, 3306, 5432, 6379, 9042, 27017, 30013, 30015, 30041, 11211}


CATEGORY = {"postgres": "db", "oracle": "db", "mysql": "db", "sqlserver": "db", "hana": "db", "mongodb": "db", "redis": "cache", "memcached": "cache",
            "cassandra": "db", "kafka": "messaging", "amqp": "messaging", "rabbitmq": "messaging", "ibm-mq": "messaging", "nats": "messaging",
            "zookeeper": "messaging", "http": "api", "https": "api", "weblogic": "api", "sap-http": "sap", "sap-https": "sap", "sap-dialog": "sap",
            "sap-rfc": "sap", "sap-ms": "sap", "ldap": "identity", "ldaps": "identity", "kerberos": "identity", "smtp": "mail", "smtps": "mail",
            "imap": "mail", "pop3": "mail", "ssh": "file", "smb": "file", "nfs": "file", "elasticsearch": "observability", "kibana": "observability",
            "watchover": "observability", "dns": "infra", "ntp": "infra", "kubernetes": "infra", "rdp": "remote"}


def category(protocol: str) -> str:
    return CATEGORY.get(protocol, "other")


def port_name(port: int) -> str:
    return PORTS.get(port) or (f"hana" if 30000 <= port < 40000 and str(port).endswith("15") else f"tcp/{port}")


class FlowStore:
    def __init__(self, kb, agents=None, inventory=None):
        self.kb, self.agents, self.inventory = kb, agents, inventory
        self.lock = threading.Lock()
        kb._exec("""CREATE TABLE IF NOT EXISTS net_flows (host TEXT, process TEXT, dir TEXT, endpoint TEXT, env TEXT DEFAULT '', last_count INTEGER,
            samples INTEGER DEFAULT 1, first_seen TEXT, last_seen TEXT, PRIMARY KEY (host, process, dir, endpoint))""")
        kb._exec("CREATE INDEX IF NOT EXISTS net_flows_seen ON net_flows(last_seen)")
        self._names: dict[str, str] = {}
        self._names_at = 0.0

    def add(self, events) -> int:
        """Flow observations (attributes flow.dir / flow.process / flow.remote | flow.port / flow.count) -> upserts."""
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
            rows.append((o.host[:120], str(a.get("flow.process") or "?")[:60], d, str(ep)[:80], o.environment or "", n, ts, ts))
        if not rows:
            return 0
        with self.lock:
            sql = ("INSERT INTO net_flows (host, process, dir, endpoint, env, last_count, samples, first_seen, last_seen) VALUES (?,?,?,?,?,?,1,?,?)"
                   + self.kb.upsert_suffix("host, process, dir, endpoint", "last_count=excluded.last_count, samples=net_flows.samples+1, last_seen=excluded.last_seen, env=excluded.env"))
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

    def _rows(self, minutes: int | None, env: str | None = None) -> list[dict]:
        where, p = [], []
        if minutes:
            where.append("last_seen >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        if env:
            where.append("env = ?"); p.append(env)
        return self.kb._exec("SELECT * FROM net_flows" + (" WHERE " + " AND ".join(where) if where else ""), tuple(p))

    def hosts(self, minutes: int | None = 1440, env: str | None = None) -> list[dict]:
        """Every host seen in flows (as a sender or as a named target) with how many integrations it has each way."""
        names = self._ip_names()
        out: dict[str, dict] = {}
        for r in self._rows(minutes, env):
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
        agents = {r["host"] for r in self._rows(minutes, env)}
        return sorted(({**h, "agent": h["host"] in agents, "outbound": len(h["outbound"]), "inbound": len(h["inbound"]), "protocols": sorted(h["protocols"])}
                       for h in out.values()), key=lambda x: (not x["agent"], -(x["outbound"] + x["inbound"]), x["host"]))

    def host_map(self, host: str, minutes: int | None = 1440) -> dict:
        """One host's integrations: who connects to it (other agents' flows that end at its address, plus its listening ports),
        and what it connects to, grouped by category (database, messaging, API, SAP, identity, mail, file transfer ...)."""
        names = self._ip_names()
        my_ips = {ip for ip, n in names.items() if n == host}
        rows = self._rows(minutes)
        listens, outbound, inbound = [], {}, {}
        procs: set[str] = set()
        for r in rows:
            if r["host"] == host:
                procs.add(r["process"])
                if r["dir"] == "in":
                    port = int(r["endpoint"]) if str(r["endpoint"]).isdigit() else 0
                    listens.append({"port": r["endpoint"], "protocol": port_name(port) if port else r["endpoint"], "process": r["process"], "peers": r["last_count"],
                                    "last_seen": r["last_seen"]})
                    continue
                ip, _, port = r["endpoint"].rpartition(":")
                port_i = int(port) if port.isdigit() else 0
                proto = port_name(port_i)
                tgt = names.get(ip) or ip
                k = (tgt, port_i)
                o = outbound.setdefault(k, {"target": tgt, "ip": ip, "port": port_i, "protocol": proto, "category": category(proto), "known": tgt != ip,
                                            "processes": set(), "connections": 0, "samples": 0, "first_seen": r["first_seen"], "last_seen": r["last_seen"]})
                o["processes"].add(r["process"]); o["connections"] += int(r["last_count"] or 0); o["samples"] += int(r["samples"] or 0)
                o["first_seen"] = min(o["first_seen"], r["first_seen"]); o["last_seen"] = max(o["last_seen"], r["last_seen"])
            elif r["dir"] == "out":
                ip, _, port = r["endpoint"].rpartition(":")
                if ip in my_ips or ip == host:
                    port_i = int(port) if port.isdigit() else 0
                    k = (r["host"], port_i)
                    i = inbound.setdefault(k, {"source": r["host"], "port": port_i, "protocol": port_name(port_i), "category": category(port_name(port_i)),
                                               "processes": set(), "connections": 0, "last_seen": r["last_seen"]})
                    i["processes"].add(r["process"]); i["connections"] += int(r["last_count"] or 0); i["last_seen"] = max(i["last_seen"], r["last_seen"])
        fix = lambda d: {**d, "processes": sorted(d["processes"])}   # noqa: E731
        out_l = sorted((fix(o) for o in outbound.values()), key=lambda x: (x["category"], -x["connections"]))
        by_cat: dict[str, int] = {}
        for o in out_l:
            by_cat[o["category"]] = by_cat.get(o["category"], 0) + 1
        return {"host": host, "ips": sorted(my_ips), "processes": sorted(procs), "listens": sorted(listens, key=lambda x: str(x["port"])),
                "outbound": out_l, "inbound": sorted((fix(i) for i in inbound.values()), key=lambda x: -x["connections"]), "categories": by_cat}

    def prune(self, days: int = 7) -> None:
        self.kb._exec("DELETE FROM net_flows WHERE last_seen < ?", ((datetime.now(UTC) - timedelta(days=days)).isoformat(),))
