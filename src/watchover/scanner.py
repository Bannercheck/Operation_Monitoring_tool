"""Vulnerability scanner for inventory hosts (defensive vulnerability management).

Two deterministic, offline passes plus one optional network pass:
  1. **Advisory match** — the software and versions recorded on a host (os, application, vendor_model, tags, notes)
     are matched against a curated advisory catalog (`data/advisories.json`, plus the operator's own
     `<home>/advisories.json`). A finding is raised when a detected product's version is below the fixed version.
     No packets are sent; this reads only what the CMDB already knows.
  2. **Posture checks** — configuration weaknesses derived from the inventory record alone: an end-of-life OS, a
     critical host with monitoring off, an active production host with no owner, a decommissioned host still marked
     reachable, etc. Deterministic, offline.
  3. **Reachability probe (opt-in, off by default)** — a plain TCP connect (and a short, passive banner read) to a
     small set of well-known management ports, to flag services that are exposed and, where the banner advertises a
     version, feed pass 1. It sends no payloads and never authenticates; it only observes. Requires the `act.scan`
     permission and an explicit request, and is rate-limited. This is the same visibility a defender gets from
     `nmap -sV` against their own estate; it is not an exploitation tool.

Findings are stored per host so the Security page can show current exposure and history.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc
_HERE = Path(__file__).resolve().parent
SEV_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

# End-of-life markers: a substring in the OS string -> (label, why). Deterministic, conservative.
EOL_OS = [
    ("windows server 2008", "Windows Server 2008/R2 — extended support ended 2020-01"),
    ("windows server 2012", "Windows Server 2012/R2 — extended support ended 2023-10"),
    ("windows 7", "Windows 7 — end of life 2020-01"),
    ("centos 6", "CentOS 6 — end of life 2020-11"),
    ("centos 7", "CentOS 7 — end of life 2024-06"),
    ("centos 8", "CentOS 8 — end of life 2021-12"),
    ("ubuntu 14.04", "Ubuntu 14.04 — end of standard support 2019-04"),
    ("ubuntu 16.04", "Ubuntu 16.04 — end of standard support 2021-04"),
    ("ubuntu 18.04", "Ubuntu 18.04 — end of standard support 2023-05"),
    ("debian 8", "Debian 8 (jessie) — end of life 2020-06"),
    ("debian 9", "Debian 9 (stretch) — end of life 2022-06"),
    ("debian 10", "Debian 10 (buster) — end of life 2024-06"),
    ("rhel 6", "RHEL 6 — end of maintenance 2020-11"),
    ("rhel 7", "RHEL 7 — end of maintenance 2024-06"),
    ("solaris", "Oracle Solaris — verify a supported release and patch level"),
    ("esxi 6", "VMware ESXi 6.x — end of general support 2022-10"),
]

# Product aliases: how a product appears in inventory text -> canonical key used by the advisory catalog.
ALIASES = {
    "openssh": ["openssh", "ssh "], "openssl": ["openssl"], "nginx": ["nginx"],
    "apache": ["apache", "httpd", "apache2"], "log4j": ["log4j", "log4j2"], "linux": ["kernel", "linux "],
    "sudo": ["sudo"], "polkit": ["polkit", "pkexec"], "docker": ["docker", "containerd", "runc"],
    "kubernetes": ["kubernetes", "k8s", "kubelet"], "windows": ["windows server", "windows "],
    "exchange": ["exchange"], "spring": ["spring framework", "spring-core", "spring "],
    "activemq": ["activemq"], "commons-text": ["commons-text", "commons text"], "zlib": ["zlib"],
    "curl": ["curl", "libcurl"], "libwebp": ["libwebp", "webp"], "f5big-ip": ["big-ip", "big ip", "f5 "],
    "fortios": ["fortios", "fortigate"], "panos": ["pan-os", "panos", "palo alto"],
    "cisco-ios-xe": ["ios xe", "ios-xe"], "moveit": ["moveit"], "barracuda": ["barracuda esg", "barracuda"],
}
# Ports probed in the optional reachability pass: port -> service label. Management / commonly exposed only.
PROBE_PORTS = {22: "ssh", 23: "telnet", 80: "http", 443: "https", 445: "smb", 3306: "mysql",
               3389: "rdp", 5432: "postgres", 6379: "redis", 8080: "http-alt", 9200: "elasticsearch",
               11211: "memcached", 27017: "mongodb"}
# Exposing these services to an untrusted network is itself a finding, regardless of version.
RISKY_SERVICES = {"telnet": ("high", "Telnet is cleartext; replace with SSH"),
                  "smb": ("medium", "SMB exposed; restrict to management network"),
                  "rdp": ("high", "RDP exposed; put behind VPN / gateway, enable NLA"),
                  "redis": ("high", "Redis exposed; bind to localhost, require auth"),
                  "memcached": ("high", "Memcached exposed; UDP amplification risk, bind to localhost"),
                  "mongodb": ("high", "MongoDB exposed; enable auth, bind to management network"),
                  "elasticsearch": ("high", "Elasticsearch exposed; enable security, restrict access"),
                  "mysql": ("medium", "Database port exposed to the network"),
                  "postgres": ("medium", "Database port exposed to the network")}

_VER = re.compile(r"(\d+(?:\.\d+){0,3}[a-z]?\d*)")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_ver(v: str) -> tuple:
    """Version to a comparable tuple; a trailing letter (1.9.5p2, 9.3p2) becomes an extra ordered field."""
    parts = []
    for chunk in re.split(r"[.\-_]", v.strip().lower()):
        m = re.match(r"^(\d+)([a-z].*)?$", chunk)
        if m:
            parts.append((int(m.group(1)), m.group(2) or ""))
        elif chunk.isdigit():
            parts.append((int(chunk), ""))
    return tuple(parts) or ((0, ""),)


def _lt(a: str, b: str) -> bool:
    """a < b as version tuples (missing fields treated as 0)."""
    ta, tb = _parse_ver(a), _parse_ver(b)
    for i in range(max(len(ta), len(tb))):
        x = ta[i] if i < len(ta) else (0, "")
        y = tb[i] if i < len(tb) else (0, "")
        if x != y:
            return x < y
    return False


def load_catalog(home: str | None = None) -> list[dict]:
    cat = json.loads((_HERE / "data" / "advisories.json").read_text())
    if home:
        extra = Path(home) / "advisories.json"
        if extra.exists():
            try:
                user = json.loads(extra.read_text())
                if isinstance(user, list):
                    cat = cat + [a for a in user if a.get("id") and a.get("product")]
            except (ValueError, OSError):
                pass
    return cat


def detect_products(text: str) -> list[tuple[str, str]]:
    """(product, version) pairs found in a blob of inventory text. Version is the number nearest the product token."""
    t = f" {text.lower()} "
    out = []
    for product, needles in ALIASES.items():
        for nd in needles:
            i = t.find(nd)
            if i < 0:
                continue
            window = t[i:i + len(nd) + 24]
            m = _VER.search(window)
            out.append((product, m.group(1) if m else ""))
            break
    return out


def match_advisories(products: list[tuple[str, str]], catalog: list[dict]) -> list[dict]:
    by_product: dict[str, list[dict]] = {}
    for a in catalog:
        by_product.setdefault(a["product"], []).append(a)
    found = []
    for product, ver in products:
        for a in by_product.get(product, []):
            # No version known -> flag as "unconfirmed" (needs verification) rather than asserting vulnerable.
            vulnerable = _lt(ver, a["lt"]) if ver else None
            if vulnerable is False:
                continue
            found.append({"id": a["id"], "product": product, "version": ver or "?", "fixed": a["lt"],
                          "severity": a["severity"], "cvss": a.get("cvss"), "title": a["title"], "fix": a.get("fix", ""),
                          "kind": "cve", "confidence": "confirmed" if ver else "unconfirmed"})
    return found


def posture_checks(rec: dict) -> list[dict]:
    out = []
    os_s = str(rec.get("os", "")).lower()
    crit = str(rec.get("criticality", "")).lower()
    status = str(rec.get("status", "")).lower()
    mon = str(rec.get("monitoring", "")).lower()
    for needle, why in EOL_OS:
        if needle in os_s:
            out.append({"id": "EOL-OS", "kind": "posture", "severity": "high", "title": why,
                        "fix": "Upgrade to a supported release", "confidence": "confirmed"})
            break
    if crit in ("critical", "high") and mon in ("", "no", "none", "off", "false", "0"):
        out.append({"id": "NO-MONITORING", "kind": "posture", "severity": "medium",
                    "title": "Critical host without monitoring", "fix": "Attach an agent / monitoring", "confidence": "confirmed"})
    if status == "active" and crit in ("critical", "high") and not str(rec.get("owner", "")).strip():
        out.append({"id": "NO-OWNER", "kind": "posture", "severity": "low",
                    "title": "Critical active host has no owner", "fix": "Assign an owner", "confidence": "confirmed"})
    if not os_s.strip():
        out.append({"id": "NO-OS", "kind": "posture", "severity": "low",
                    "title": "OS/version unknown — cannot assess patch level", "fix": "Record OS and version", "confidence": "confirmed"})
    return out


def probe_host(host: str, ports: dict | None = None, timeout: float = 1.5) -> list[dict]:
    """Opt-in, passive reachability check: TCP connect + short banner read. Sends no payloads, never authenticates.
    Returns one entry per OPEN port with the service label and any banner text observed."""
    ports = ports or PROBE_PORTS
    open_ports = []
    for port, svc in ports.items():
        try:
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(min(timeout, 1.0))
                banner = ""
                try:
                    if port == 443:
                        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
                        with ctx.wrap_socket(s, server_hostname=host) as ss:
                            cert = ss.getpeercert()
                            banner = f"TLS {ss.version()}"
                    else:
                        data = s.recv(96)
                        banner = data.decode("latin-1", "replace").strip().replace("\r", " ").replace("\n", " ")[:80]
                except (OSError, ssl.SSLError):
                    banner = ""
                open_ports.append({"port": port, "service": svc, "banner": banner})
        except (OSError, socket.timeout):
            continue
    return open_ports


def findings_from_probe(open_ports: list[dict], catalog: list[dict]) -> list[dict]:
    out = []
    for p in open_ports:
        svc = p["service"]
        if svc in RISKY_SERVICES:
            sev, why = RISKY_SERVICES[svc]
            out.append({"id": f"EXPOSED-{svc.upper()}", "kind": "exposure", "severity": sev,
                        "title": f"{svc} reachable on port {p['port']}: {why}", "fix": why,
                        "confidence": "confirmed", "port": p["port"], "banner": p.get("banner", "")})
        if p.get("banner"):
            for product, ver in detect_products(p["banner"]):
                if ver:
                    out.extend(match_advisories([(product, ver)], catalog))
    return out


def scan_record(rec: dict, catalog: list[dict], probe: bool = False, timeout: float = 1.5) -> dict:
    """All findings for one inventory host. `probe` turns on the optional network reachability pass."""
    text = " ".join(str(rec.get(f, "")) for f in ("os", "application", "vendor_model", "role", "cluster", "tags", "notes"))
    findings = match_advisories(detect_products(text), catalog) + posture_checks(rec)
    ports = []
    if probe:
        target = str(rec.get("ip") or rec.get("hostname") or "").strip()
        if target:
            ports = probe_host(target)
            findings += findings_from_probe(ports, catalog)
    # dedupe by (id, port)
    seen, uniq = set(), []
    for f in findings:
        k = (f["id"], f.get("port"))
        if k not in seen:
            seen.add(k); uniq.append(f)
    uniq.sort(key=lambda f: (-SEV_RANK.get(f["severity"], 0), -(f.get("cvss") or 0)))
    worst = uniq[0]["severity"] if uniq else "none"
    return {"hostname": rec.get("hostname", ""), "ip": rec.get("ip", ""), "findings": uniq, "open_ports": ports,
            "counts": {s: sum(1 for f in uniq if f["severity"] == s) for s in ("critical", "high", "medium", "low")},
            "worst": worst, "scanned_at": _now(), "probed": probe}


class ScanStore:
    """Latest scan result per host plus run history, in the shared knowledge database."""

    def __init__(self, kb, home: str | None = None):
        self.kb = kb
        self.home = home
        self.catalog = load_catalog(home)
        self.lock = threading.Lock()
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS scan_results (id {pk}, hostname TEXT NOT NULL UNIQUE, ip TEXT DEFAULT '',
            worst TEXT DEFAULT 'none', critical INTEGER DEFAULT 0, high INTEGER DEFAULT 0, medium INTEGER DEFAULT 0, low INTEGER DEFAULT 0,
            probed INTEGER DEFAULT 0, findings TEXT DEFAULT '[]', open_ports TEXT DEFAULT '[]', scanned_at TEXT)""")
        kb._exec(f"CREATE TABLE IF NOT EXISTS scan_runs (id {pk}, ts TEXT, hosts INTEGER, findings INTEGER, critical INTEGER, high INTEGER, probed INTEGER, note TEXT DEFAULT '')")

    def reload_catalog(self) -> int:
        self.catalog = load_catalog(self.home)
        return len(self.catalog)

    def _save(self, res: dict) -> None:
        c = res["counts"]
        self.kb._exec(
            "INSERT INTO scan_results (hostname, ip, worst, critical, high, medium, low, probed, findings, open_ports, scanned_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?) " + self.kb.upsert_suffix("hostname",
            "ip=excluded.ip, worst=excluded.worst, critical=excluded.critical, high=excluded.high, medium=excluded.medium, low=excluded.low, "
            "probed=excluded.probed, findings=excluded.findings, open_ports=excluded.open_ports, scanned_at=excluded.scanned_at"),
            (res["hostname"], res["ip"], res["worst"], c["critical"], c["high"], c["medium"], c["low"], 1 if res["probed"] else 0,
             json.dumps(res["findings"], ensure_ascii=False), json.dumps(res["open_ports"], ensure_ascii=False), res["scanned_at"]))

    def scan_host(self, rec: dict, probe: bool = False) -> dict:
        res = scan_record(rec, self.catalog, probe=probe)
        with self.lock:
            self._save(res)
        return res

    def scan_all(self, records: list[dict], probe: bool = False) -> dict:
        crit = high = total = 0
        for rec in records:
            res = self.scan_host(rec, probe=probe)
            total += len(res["findings"]); crit += res["counts"]["critical"]; high += res["counts"]["high"]
        self.kb._exec("INSERT INTO scan_runs (ts, hosts, findings, critical, high, probed) VALUES (?,?,?,?,?,?)",
                      (_now(), len(records), total, crit, high, 1 if probe else 0))
        return {"hosts": len(records), "findings": total, "critical": crit, "high": high, "probed": probe, "ts": _now()}

    def result(self, hostname: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM scan_results WHERE hostname=?", (hostname.strip().lower(),))
        return self._row(rows[0]) if rows else None

    def results(self) -> list[dict]:
        return [self._row(r) for r in self.kb._exec("SELECT * FROM scan_results ORDER BY CASE worst WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END DESC, critical DESC, high DESC")]

    @staticmethod
    def _row(r: dict) -> dict:
        d = dict(r)
        for k in ("findings", "open_ports"):
            try:
                d[k] = json.loads(d.get(k) or "[]")
            except (TypeError, ValueError):
                d[k] = []
        return d

    def runs(self, n: int = 10) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (n,))]

    def stats(self) -> dict:
        rows = self.results()
        agg = {"hosts_scanned": len(rows), "catalog": len(self.catalog)}
        for s in ("critical", "high", "medium", "low"):
            agg[s] = sum(r.get(s, 0) for r in rows)
        agg["exposed"] = sum(1 for r in rows if r.get("worst") in ("critical", "high"))
        last = self.runs(1)
        agg["last_run"] = last[0] if last else None
        return agg
