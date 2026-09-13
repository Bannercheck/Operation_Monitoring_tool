"""Generate samples/demo_mixed.zip: a planted incident chain inside background noise.

DB latency (db.log) -> payment-api timeouts (app.jsonl) -> checkout 500s (checkout.log)
-> PAYMENT_FAILURE alert (alerts.csv). Plus a separate disk-full incident on worker-02.
"""
import io, json, random, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

random.seed(7)
T0 = datetime(2026, 9, 16, 14, 0, tzinfo=timezone.utc)
ts = lambda m, s=0: (T0 + timedelta(minutes=m, seconds=s))
app, chk, db, al = [], [], [], []
# background noise: 40 minutes of healthy traffic
for m in range(0, 45):
    for _ in range(random.randint(8, 14)):
        s = random.randint(0, 59)
        app.append({"ts": ts(m, s).isoformat(), "level": "info", "service": "payment-api", "host": f"prd-api-0{random.randint(1,3)}",
                    "msg": f"POST /pay 200 in {random.randint(40,120)}ms user={random.randint(1000,9999)}"})
    for _ in range(random.randint(3, 6)):
        chk.append(f"{ts(m, random.randint(0,59)):%Y-%m-%d %H:%M:%S} INFO checkout-api: GET /cart 200 {random.randint(10,40)}ms")
    db.append(f"{ts(m, 5):%b %d %H:%M:%S} db-01 postgres[4412]: checkpoint complete: wrote {random.randint(100,900)} buffers")
    if m % 7 == 0:
        app.append({"ts": ts(m, 30).isoformat(), "level": "warn", "service": "payment-api", "host": "prd-api-02", "msg": "cache miss ratio 0.31 above 0.30"})
# incident 1: DB latency at 14:31 -> payment timeouts 14:32 -> checkout 500 14:33 -> alert 14:35
for s in range(0, 60, 4):
    db.append(f"{ts(31, s):%b %d %H:%M:%S} db-01 postgres[4412]: LOG: duration: {random.randint(4000,9000)}.{random.randint(100,999)} ms  statement: SELECT * FROM orders WHERE id = {random.randint(1,99999)}")
for m in (32, 33, 34):
    for s in range(0, 60, 3):
        app.append({"ts": ts(m, s).isoformat(), "level": "error", "service": "payment-api", "host": f"prd-api-0{random.randint(1,3)}",
                    "msg": f"Connection timeout to db-01 (172.16.1.55:5432) after 5000ms user={random.randint(1000,9999)} request={random.randint(100000,999999)}"})
for m in (33, 34, 35):
    for s in range(0, 60, 5):
        chk.append(f"{ts(m, s):%Y-%m-%d %H:%M:%S} ERROR checkout-api: POST /checkout 500 upstream payment-api timeout req={random.randint(100000,999999)}")
al.append(f"{ts(35, 10):%Y-%m-%d %H:%M:%S},critical,PAYMENT_FAILURE,payment-api,prd-api-01,payment failure rate 38% over 5m")
al.append(f"{ts(36, 0):%Y-%m-%d %H:%M:%S},warning,HTTP_5XX_RATE,checkout-api,prd-api-02,5xx rate 21% over 5m")
# incident 2 (unrelated): disk full on worker-02 at 14:10
for s in range(0, 60, 10):
    db.append(f"{ts(10, s):%b %d %H:%M:%S} worker-02 cron[911]: ERROR: No space left on device writing /var/spool/report_{random.randint(1,50)}.csv")
al.append(f"{ts(11, 0):%Y-%m-%d %H:%M:%S},warning,DISK_USAGE,worker,worker-02,disk usage 97% on /var")

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    zf.writestr("app.jsonl", "\n".join(json.dumps(x) for x in app) + "\n")
    zf.writestr("checkout.log", "\n".join(chk) + "\n")
    zf.writestr("db.log", "\n".join(db) + "\n")
    zf.writestr("alerts.csv", "time,severity,alertname,service,host,description\n" + "\n".join(al) + "\n")
Path(__file__).with_name("demo_mixed.zip").write_bytes(buf.getvalue())
print("wrote demo_mixed.zip", len(app) + len(chk) + len(db) + len(al), "events")
