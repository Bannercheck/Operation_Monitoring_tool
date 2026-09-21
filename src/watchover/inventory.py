"""Inventory (CMDB-lite): every host the organisation runs, with the attributes operations needs when a log line arrives:
where it is (data centre, rack, VLAN / subnet), what it does (role, application, cluster), who owns it, how critical it is,
what it stores. Kept in the knowledge database; filled by hand, by CSV import, or by promoting hosts discovered in the
live feed. Matching: an event's host name, alias or IP is resolved to the inventory record and the record enriches the
event (environment, dc, rack, criticality, owner) before the engine sees it."""
from __future__ import annotations

import csv
import io
import ipaddress
import re
import threading
import time
from datetime import datetime, timezone

UTC = timezone.utc
FIELDS = ("hostname", "ip", "aliases", "env", "dc", "rack", "vlan", "subnet", "os", "role", "application", "cluster", "owner", "criticality",
          "storage", "vendor_model", "serial", "monitoring", "status", "tags", "notes", "source")
CRITICALITY = ("critical", "high", "normal", "low")
STATUS = ("active", "maintenance", "decommissioned")
CSV_ALIASES = {"host": "hostname", "host_name": "hostname", "name": "hostname", "fqdn": "hostname", "sunucu": "hostname", "makine": "hostname",
               "ip_address": "ip", "ipaddress": "ip", "ip adresi": "ip", "environment": "env", "ortam": "env", "site": "dc", "datacenter": "dc",
               "data_center": "dc", "veri_merkezi": "dc", "veri merkezi": "dc", "kabin": "rack", "location": "rack", "network": "vlan", "wlan": "vlan",
               "ag": "vlan", "ağ": "vlan", "operating_system": "os", "isletim_sistemi": "os", "service": "role", "servis": "role", "rol": "role",
               "app": "application", "uygulama": "application", "team": "owner", "sahip": "owner", "ekip": "owner", "is_kritikligi": "criticality",
               "kritiklik": "criticality", "criticality_level": "criticality", "depolama": "storage", "san": "storage", "model": "vendor_model",
               "vendor": "vendor_model", "seri_no": "serial", "durum": "status", "etiketler": "tags", "notlar": "notes", "not": "notes"}
_IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def norm_crit(v: str) -> str:
    v = (v or "").strip().lower()
    if v in CRITICALITY:
        return v
    if v in ("kritik", "critical", "yüksek", "yuksek", "1", "p1", "tier1", "tier 1", "business-critical", "core"):
        return "critical" if v in ("kritik", "critical", "1", "p1", "tier1", "tier 1", "business-critical", "core") else "high"
    if v in ("orta", "medium", "2", "p2", "standard"):
        return "normal"
    if v in ("düşük", "dusuk", "3", "p3", "test"):
        return "low"
    return "normal"


class Inventory:
    def __init__(self, kb):
        self.kb = kb
        pk = kb.pk
        cols = ", ".join(f"{f} TEXT DEFAULT ''" for f in FIELDS if f != "hostname")
        kb._exec(f"CREATE TABLE IF NOT EXISTS inventory (id {pk}, hostname TEXT NOT NULL UNIQUE, {cols}, created_at TEXT, updated_at TEXT)")
        self._index: dict[str, dict] | None = None
        self._index_at = 0.0
        self.lock = threading.Lock()

    # ---- CRUD
    def list(self, q: str = "") -> list[dict]:
        rows = [dict(r) for r in self.kb._exec("SELECT * FROM inventory ORDER BY dc, hostname")]
        if q:
            ql = q.lower()
            rows = [r for r in rows if any(ql in str(v).lower() for v in r.values())]
        return rows

    def get(self, hostname: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM inventory WHERE hostname=?", (hostname.strip().lower(),))
        return dict(rows[0]) if rows else None

    def upsert(self, rec: dict) -> dict:
        """Insert or update by hostname (lower-cased); only known fields are stored."""
        h = str(rec.get("hostname", "")).strip().lower()
        if not h:
            raise ValueError("hostname is required")
        vals = {f: str(rec.get(f, "") or "").strip() for f in FIELDS if f != "hostname"}
        vals["criticality"] = norm_crit(vals["criticality"])
        vals["status"] = vals["status"].lower() if vals["status"].lower() in STATUS else "active"
        vals["env"] = vals["env"].lower()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        if self.get(h):
            sets = ", ".join(f"{k}=?" for k in vals)
            self.kb._exec(f"UPDATE inventory SET {sets}, updated_at=? WHERE hostname=?", (*vals.values(), now, h))
        else:
            cols = ", ".join(["hostname", *vals, "created_at", "updated_at"])
            self.kb._exec(f"INSERT INTO inventory ({cols}) VALUES ({', '.join('?' * (len(vals) + 3))})", (h, *vals.values(), now, now))
        self._index = None
        return self.get(h)

    def delete(self, hostname: str) -> None:
        self.kb._exec("DELETE FROM inventory WHERE hostname=?", (hostname.strip().lower(),))
        self._index = None

    # ---- CSV
    @staticmethod
    def csv_template() -> bytes:
        return (",".join(FIELDS[:-1]) + "\n" + "db-01,10.20.1.15,db01.corp.local,prod,IST-DC1,R12,VLAN-120,10.20.1.0/24,RHEL 9,database,Oracle ERP,ora-rac-1,DBA,critical,"
                "SAN LUN-042 2TB,Dell R760,ABC123,agent:db-01,active,\"oracle,core\",\n").encode()

    def import_csv(self, data: bytes) -> tuple[int, list[str]]:
        text = data.decode("utf-8-sig", "replace")
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t") if text.strip() else csv.excel
        rd = csv.DictReader(io.StringIO(text), dialect=dialect)
        n, errors = 0, []
        for i, row in enumerate(rd, 2):
            rec = {}
            for k, v in row.items():
                if k is None:
                    continue
                key = k.strip().lower().replace(" ", "_")
                key = CSV_ALIASES.get(key, CSV_ALIASES.get(k.strip().lower(), key))
                if key in FIELDS:
                    rec[key] = v
            rec.setdefault("source", "csv")
            try:
                self.upsert(rec); n += 1
            except ValueError as e:
                errors.append(f"line {i}: {e}")
        return n, errors

    def export_csv(self) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(FIELDS)
        for r in self.list():
            w.writerow([r.get(f, "") for f in FIELDS])
        return buf.getvalue().encode("utf-8-sig")

    # ---- matching
    def index(self) -> dict[str, dict]:
        """name / alias / ip (lower-cased) -> record, rebuilt at most every 5 s."""
        with self.lock:
            if self._index is None or time.time() - self._index_at > 5:
                idx: dict[str, dict] = {}
                for r in self.list():
                    if r.get("status") == "decommissioned":
                        continue
                    keys = [r["hostname"], r["hostname"].split(".")[0], r.get("ip", "")] + [a.strip() for a in (r.get("aliases") or "").split(",")]
                    for k in keys:
                        k = k.strip().lower()
                        if k:
                            idx.setdefault(k, r)
                self._index, self._index_at = idx, time.time()
            return self._index

    def resolve(self, host: str) -> dict | None:
        if not host:
            return None
        idx = self.index()
        h = host.strip().lower()
        return idx.get(h) or idx.get(h.split(".")[0])

    def enrich(self, o) -> bool:
        """Attach inventory attributes to an observation; an IP host becomes its hostname. Returns True when matched."""
        r = self.resolve(o.host)
        if not r:
            return False
        if _IP.match(o.host or "") and r["hostname"] != o.host:
            o.attributes["ip"] = o.host
            o.host = r["hostname"]
        if not o.environment and r.get("env"):
            o.environment = r["env"]
        for k in ("dc", "rack", "vlan", "owner", "criticality", "role", "application", "cluster"):
            if r.get(k):
                o.attributes.setdefault("inv_" + k, r[k])
        o.attributes.setdefault("site", r.get("dc", "")) if r.get("dc") else None
        return True

    def as_engine(self) -> dict[str, dict]:
        """host -> {service, dc, rack, env, criticality} in the shape the engine's storm / scoring code expects."""
        return {r["hostname"]: {"service": r.get("role") or r.get("application") or "", "dc": r.get("dc", ""), "rack": r.get("rack", ""),
                                "env": r.get("env", ""), "criticality": "critical" if r.get("criticality") in ("critical", "high") else r.get("criticality", "")}
                for r in self.list() if r.get("status") != "decommissioned"}

    def discovered(self, seen_hosts: dict[str, str], agents: list[dict] | None = None) -> list[dict]:
        """Hosts seen in the live feed / agent registry that the inventory does not know yet."""
        out = []
        for h, env in sorted(seen_hosts.items()):
            if h and not self.resolve(h):
                out.append({"hostname": h, "env": env or "", "ip": h if _IP.match(h) else "", "source": "discovered"})
        for a in agents or []:
            if a.get("name") and not self.resolve(a["name"]) and all(x["hostname"] != a["name"] for x in out):
                out.append({"hostname": a["name"], "env": a.get("env", ""), "dc": a.get("site", ""), "ip": a.get("last_ip", "") if not _IP.match(a["name"]) else "", "source": "discovered"})
        return out

    def stats(self, seen_hosts: dict[str, str] | None = None) -> dict:
        rows = self.list()
        seen = seen_hosts or {}
        matched = sum(1 for h in seen if self.resolve(h))
        return {"hosts": len(rows), "critical": sum(1 for r in rows if r.get("criticality") == "critical"), "dcs": len({r.get("dc") for r in rows if r.get("dc")}),
                "seen": len(seen), "matched": matched, "unmatched": len(seen) - matched}
