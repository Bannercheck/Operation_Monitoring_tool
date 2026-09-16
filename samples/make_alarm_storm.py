"""Synthetic "alert storm" package in the S-A1 layout (alarms.json + alarms.csv + service_dependencies.csv +
host_inventory.csv), used to exercise the engine before the real package is available.

3.000 alarms, 27 services, 56 hosts, 32 dependency rows, 01:30-03:30. Ground truth is written to
samples/alarm_storm_truth.json so the reduction can be checked (which alarm belongs to which event).
"""
from __future__ import annotations

import csv
import io
import json
import random
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

rng = random.Random(7)
T0 = datetime(2026, 9, 10, 1, 30, tzinfo=timezone(timedelta(hours=3)))

SERVICES = ["api-gateway", "auth-api", "checkout-api", "payment-api", "order-api", "cart-api", "search-api", "catalog-api",
            "inventory-api", "notification-svc", "email-svc", "sms-svc", "report-svc", "billing-svc", "ledger-svc", "fraud-svc",
            "user-db", "order-db", "payment-db", "catalog-db", "redis-cache", "kafka", "object-storage", "cdn-edge",
            "monitoring-agent", "backup-svc", "batch-scheduler"]
assert len(SERVICES) == 27
DEPS = [("api-gateway", "auth-api", "senkron", "yuksek"), ("api-gateway", "checkout-api", "senkron", "yuksek"), ("api-gateway", "search-api", "senkron", "orta"),
        ("api-gateway", "catalog-api", "senkron", "orta"), ("checkout-api", "payment-api", "senkron", "yuksek"), ("checkout-api", "cart-api", "senkron", "yuksek"),
        ("checkout-api", "inventory-api", "senkron", "orta"), ("checkout-api", "order-api", "senkron", "yuksek"), ("payment-api", "payment-db", "senkron", "yuksek"),
        ("payment-api", "fraud-svc", "senkron", "orta"), ("payment-api", "ledger-svc", "asenkron", "orta"), ("order-api", "order-db", "senkron", "yuksek"),
        ("order-api", "kafka", "asenkron", "orta"), ("auth-api", "user-db", "senkron", "yuksek"), ("auth-api", "redis-cache", "senkron", "orta"),
        ("cart-api", "redis-cache", "senkron", "yuksek"), ("search-api", "catalog-db", "senkron", "orta"), ("catalog-api", "catalog-db", "senkron", "yuksek"),
        ("catalog-api", "object-storage", "senkron", "dusuk"), ("cdn-edge", "object-storage", "senkron", "orta"), ("notification-svc", "kafka", "asenkron", "orta"),
        ("notification-svc", "email-svc", "asenkron", "dusuk"), ("notification-svc", "sms-svc", "asenkron", "dusuk"), ("billing-svc", "ledger-svc", "senkron", "yuksek"),
        ("billing-svc", "payment-db", "senkron", "yuksek"), ("report-svc", "order-db", "senkron", "dusuk"), ("report-svc", "object-storage", "asenkron", "dusuk"),
        ("fraud-svc", "user-db", "senkron", "orta"), ("inventory-api", "order-db", "senkron", "orta"), ("backup-svc", "object-storage", "asenkron", "dusuk"),
        ("batch-scheduler", "kafka", "asenkron", "dusuk"), ("email-svc", "object-storage", "asenkron", "dusuk")]
assert len(DEPS) == 32

# 56 hosts: 2 per service, plus a few extras; dc / rack / env
HOSTS: list[dict] = []
for i, svc in enumerate(SERVICES):
    for n in (1, 2):
        dc = "dc1" if (i + n) % 3 else "dc2"
        rack = ["rack-A", "rack-B", "rack-C"][(i * 2 + n) % 3]
        env = "prod" if svc != "report-svc" else ("test" if n == 2 else "prod")
        HOSTS.append({"host": f"{svc.split('-')[0]}-{n:02d}", "servis": svc, "veri_merkezi": dc, "kabin": rack, "ortam": env,
                      "is_kritikligi": "yuksek" if svc.endswith(("db", "api", "gateway")) else "orta"})
HOSTS += [{"host": "stage-01", "servis": "checkout-api", "veri_merkezi": "dc1", "kabin": "rack-C", "ortam": "staging", "is_kritikligi": "dusuk"},
          {"host": "stage-02", "servis": "payment-api", "veri_merkezi": "dc1", "kabin": "rack-C", "ortam": "staging", "is_kritikligi": "dusuk"}]
assert len(HOSTS) == 56
HOST_BY_SVC: dict[str, list[dict]] = {}
for h in HOSTS:
    HOST_BY_SVC.setdefault(h["servis"], []).append(h)

TYPES = {"CPU_HIGH": ("CPU usage {v}% on {host}", 3), "MEM_HIGH": ("Memory usage {v}% on {host}", 3), "DISK_HIGH": ("Disk usage {v}% on /data ({host})", 3),
         "DISK_FULL": ("No space left on device /data ({host})", 1), "DB_CONN_TIMEOUT": ("Connection timeout to {dep} after {v}ms", 2),
         "DB_SLOW_QUERY": ("Slow query {v}ms on {dep}", 3), "HTTP_5XX": ("HTTP 5xx rate {v}% on {svc}", 2), "LATENCY_P95": ("p95 latency {v}ms on {svc}", 3),
         "UPSTREAM_TIMEOUT": ("Upstream timeout calling {dep}", 2), "QUEUE_LAG": ("Consumer lag {v} messages on {dep}", 3), "CERT_EXPIRY": ("TLS certificate expires in {v} days", 4),
         "HEARTBEAT_MISSED": ("Heartbeat missed from {host}", 2), "PING_LOSS": ("Packet loss {v}% to {host}", 2), "POWER_FEED": ("Power feed B lost on {rack} ({dc})", 1),
         "SWITCH_DOWN": ("ToR switch down on {rack} ({dc})", 1), "LOGIN_FAIL": ("Login failures {v}/min on {svc}", 4), "BACKUP_FAIL": ("Backup job failed on {host}", 3),
         "JOB_RETRY": ("Batch job retry #{v} on {svc}", 4), "CACHE_MISS": ("Cache miss ratio {v}% on {dep}", 4), "REPLICA_LAG": ("Replica lag {v}s on {dep}", 3),
         "OOM_KILL": ("OOM killer terminated {svc} on {host}", 2), "DEPLOY_START": ("Deployment started for {svc}", 5), "CONFIG_RELOAD": ("Config reloaded on {host}", 5),
         "AGENT_RESTART": ("Monitoring agent restarted on {host}", 4), "FS_INODES": ("Inode usage {v}% on {host}", 4), "NTP_DRIFT": ("Clock drift {v}ms on {host}", 4)}
assert len(TYPES) == 26
SOURCES = ["prometheus", "zabbix", "appdynamics", "cloudwatch", "syslog"]


def mk(t: datetime, svc: str, atype: str, host: dict | None = None, dep: str = "", v=None, sev=None) -> dict:
    tpl, base_sev = TYPES[atype]
    host = host or rng.choice(HOST_BY_SVC[svc])
    msg = tpl.format(v=v if v is not None else rng.randint(2, 99), host=host["host"], svc=svc, dep=dep or svc, rack=host["kabin"], dc=host["veri_merkezi"])
    return {"timestamp": t.isoformat(), "source_system": rng.choice(SOURCES), "host": host["host"], "service": svc, "severity": 6 - (sev or base_sev),
            "alarm_type": atype, "message": msg, "tags": {"veri_merkezi": host["veri_merkezi"], "kabin": host["kabin"], "ortam": host["ortam"]}}


alarms: list[tuple[dict, str]] = []          # (alarm, event label)

# EVENT A (burst, 02:14): payment-db slow + timeouts -> payment-api -> checkout-api -> api-gateway 5xx
tA = T0 + timedelta(minutes=44)
for i in range(40):
    alarms.append((mk(tA + timedelta(seconds=rng.randint(0, 600)), "payment-db", rng.choice(["DB_SLOW_QUERY", "REPLICA_LAG"]), v=rng.randint(800, 4000)), "A"))
for i in range(120):
    alarms.append((mk(tA + timedelta(seconds=40 + rng.randint(0, 660)), "payment-api", "DB_CONN_TIMEOUT", dep="payment-db", v=5000), "A"))
for i in range(90):
    alarms.append((mk(tA + timedelta(seconds=90 + rng.randint(0, 700)), "checkout-api", "UPSTREAM_TIMEOUT", dep="payment-api"), "A"))
for i in range(60):
    alarms.append((mk(tA + timedelta(seconds=120 + rng.randint(0, 720)), "api-gateway", "HTTP_5XX", v=rng.randint(5, 40)), "A"))
for i in range(30):
    alarms.append((mk(tA + timedelta(seconds=150 + rng.randint(0, 700)), "billing-svc", "DB_CONN_TIMEOUT", dep="payment-db", v=5000), "A"))

# EVENT B (slow burn, 01:35 -> 03:20): order-db disk fills, then order-api slow, report-svc fails
for i in range(70):
    t = T0 + timedelta(minutes=5 + i * 1.5)
    alarms.append((mk(t, "order-db", "DISK_HIGH", host=HOST_BY_SVC["order-db"][0], v=min(99, 80 + i // 4), sev=3 if i < 55 else 2), "B"))
for i in range(25):
    alarms.append((mk(T0 + timedelta(minutes=95 + rng.randint(0, 15)), "order-db", "DISK_FULL", host=HOST_BY_SVC["order-db"][0]), "B"))
for i in range(60):
    alarms.append((mk(T0 + timedelta(minutes=97 + rng.randint(0, 14)), "order-api", "DB_SLOW_QUERY", dep="order-db", v=rng.randint(3000, 9000)), "B"))
for i in range(20):
    alarms.append((mk(T0 + timedelta(minutes=99 + rng.randint(0, 12)), "report-svc", "JOB_RETRY", v=rng.randint(1, 5), sev=3), "B"))
for i in range(15):
    alarms.append((mk(T0 + timedelta(minutes=100 + rng.randint(0, 10)), "inventory-api", "LATENCY_P95", v=rng.randint(1500, 4000)), "B"))

# EVENT C (burst, 02:48, dc2 rack-B power feed): every host on that rack loses heartbeat / ping
rackB = [h for h in HOSTS if h["veri_merkezi"] == "dc2" and h["kabin"] == "rack-B"]
tC = T0 + timedelta(minutes=78)
alarms.append((mk(tC, rackB[0]["servis"], "POWER_FEED", host=rackB[0]), "C"))
for h in rackB:
    for i in range(12):
        alarms.append((mk(tC + timedelta(seconds=20 + rng.randint(0, 400)), h["servis"], rng.choice(["HEARTBEAT_MISSED", "PING_LOSS"]), host=h, v=100), "C"))
for h in rackB:
    for i in range(3):
        alarms.append((mk(tC + timedelta(seconds=60 + rng.randint(0, 400)), h["servis"], "HTTP_5XX", host=h, v=100), "C"))

# EVENT D (independent, 03:05): redis-cache OOM -> cart-api / auth-api cache misses and latency
tD = T0 + timedelta(minutes=95)
for i in range(8):
    alarms.append((mk(tD + timedelta(seconds=rng.randint(0, 120)), "redis-cache", "OOM_KILL", host=HOST_BY_SVC["redis-cache"][1]), "D"))
for i in range(50):
    alarms.append((mk(tD + timedelta(seconds=30 + rng.randint(0, 500)), "cart-api", "CACHE_MISS", dep="redis-cache", v=rng.randint(60, 99), sev=2), "D"))
for i in range(40):
    alarms.append((mk(tD + timedelta(seconds=40 + rng.randint(0, 500)), "auth-api", "LATENCY_P95", v=rng.randint(900, 2500), sev=2), "D"))
for i in range(20):
    alarms.append((mk(tD + timedelta(seconds=60 + rng.randint(0, 500)), "auth-api", "LOGIN_FAIL", v=rng.randint(50, 300), sev=3), "D"))

# EVENT E (small, 02:30): kafka consumer lag -> notification backlog (asynchronous, low severity)
tE = T0 + timedelta(minutes=60)
for i in range(25):
    alarms.append((mk(tE + timedelta(seconds=rng.randint(0, 900)), "kafka", "QUEUE_LAG", v=rng.randint(10000, 90000), sev=3), "E"))
for i in range(30):
    alarms.append((mk(tE + timedelta(seconds=120 + rng.randint(0, 900)), "notification-svc", "QUEUE_LAG", dep="kafka", v=rng.randint(5000, 40000), sev=3), "E"))

# NOISE: background chatter across the whole window (steady rate, low severity, spread over everything)
n_noise = 3000 - len(alarms)
noise_types = ["CPU_HIGH", "MEM_HIGH", "CERT_EXPIRY", "NTP_DRIFT", "CONFIG_RELOAD", "DEPLOY_START", "AGENT_RESTART", "FS_INODES", "JOB_RETRY", "LOGIN_FAIL", "CACHE_MISS", "BACKUP_FAIL"]
for i in range(n_noise):
    svc = rng.choice(SERVICES)
    atype = rng.choice(noise_types)
    sev = 4 if atype not in ("CONFIG_RELOAD", "DEPLOY_START") else 5
    if rng.random() < 0.08:
        sev = 3
    alarms.append((mk(T0 + timedelta(seconds=rng.randint(0, 7200)), svc, atype, v=rng.randint(1, 80), sev=sev), "noise"))

alarms.sort(key=lambda a: a[0]["timestamp"])
truth = {}
rows = []
for i, (a, label) in enumerate(alarms, 1):
    a = {"alarm_id": f"ALM-{i:05d}", **a}
    rows.append(a)
    truth[a["alarm_id"]] = label
assert len(rows) == 3000

csv_buf = io.StringIO()
w = csv.writer(csv_buf)
w.writerow(["alarm_id", "timestamp", "source_system", "host", "service", "severity", "alarm_type", "message", "veri_merkezi", "kabin", "ortam"])
for a in rows:
    w.writerow([a["alarm_id"], a["timestamp"], a["source_system"], a["host"], a["service"], a["severity"], a["alarm_type"], a["message"],
                a["tags"]["veri_merkezi"], a["tags"]["kabin"], a["tags"]["ortam"]])
dep_buf = io.StringIO()
w = csv.writer(dep_buf); w.writerow(["kaynak_servis", "hedef_servis", "bagimlilik_tipi", "kritiklik"]); w.writerows(DEPS)
inv_buf = io.StringIO()
w = csv.DictWriter(inv_buf, fieldnames=["host", "servis", "veri_merkezi", "kabin", "ortam", "is_kritikligi"]); w.writeheader(); w.writerows(HOSTS)

out = Path(__file__).with_name("alarm_storm.zip")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("alarms.json", json.dumps(rows, ensure_ascii=False, indent=1))
    z.writestr("alarms.csv", csv_buf.getvalue())
    z.writestr("service_dependencies.csv", dep_buf.getvalue())
    z.writestr("host_inventory.csv", inv_buf.getvalue())
    z.writestr("VERI_SOZLUGU.md", "# Veri sözlüğü (sentetik)\n\nseverity: 5 = kritik ... 1 = bilgi (S-A1 ile aynı ölçek). Alarm tipleri: " + ", ".join(TYPES) + "\n")
Path(__file__).with_name("alarm_storm_truth.json").write_text(json.dumps(truth), encoding="utf-8")
from collections import Counter
print(out, len(rows), "alarms;", dict(Counter(truth.values())))
