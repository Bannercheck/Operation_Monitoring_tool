"""User experience (RUM) for web and mobile apps: what a person did, step by step, and what each step cost on the backend.

Sensors (browser `rum.js`, Android / iOS / Flutter / React Native SDKs in `sensors/`) post batches of events for one session:

    {"app": "ik-mobil", "platform": "android", "version": "3.2.1", "session": "<random>", "user": "<optional id>",
     "device": {"os": "Android 14", "model": "SM-S911B", "net": "cellular", "carrier": "Turkcell"},
     "events": [{"t": "view" | "action" | "http" | "error" | "crash" | "start", "ts": <epoch ms>, "name": "...", "dur": <ms>,
                 "trace": "<32 hex>", "span": "<16 hex>", "parent": "<16 hex>", "method", "url", "status", "msg", "stack",
                 "vitals": {"lcp", "inp", "cls", "fcp", "ttfb"}, "cold": true}]}

Every step becomes a span in the trace model (kind `user` for web, `mobile` for apps, `client` for the network calls they
made), with the session and a pseudonymous user. A user step starts a trace whose id the sensor puts into the `traceparent`
header of the calls it triggers, so backend logs that carry that header join the same waterfall by id. Errors and crashes also
enter the live feed as ERROR events, so the anomaly engine and incidents see them like any other log.

KVKK / personal data, enforced here whatever a sensor sends: user ids are pseudonymised (keyed HMAC, never stored raw), every
text (names, messages, stacks, labels) is scrubbed (TCKN, phone, e-mail, IBAN, card, secrets), URLs and screen names lose their
identifiers and query strings, client IPs are not stored, user agents are reduced to OS / browser family.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from .privacy import path_template, pseudonym, scrub

UTC = timezone.utc
PLATFORMS = ("web", "android", "ios", "flutter", "react-native", "other")
VITALS_GOOD = {"lcp": 2500, "inp": 200, "cls": 0.1, "fcp": 1800, "ttfb": 800}
VITALS_POOR = {"lcp": 4000, "inp": 500, "cls": 0.25, "fcp": 3000, "ttfb": 1800}
MAX_EVENTS = 500


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def _ua_family(ua: str) -> tuple[str, str]:
    ua = ua or ""
    os_ = ("Android" if "Android" in ua else "iOS" if re.search(r"iPhone|iPad|iOS", ua) else "Windows" if "Windows" in ua else
           "macOS" if "Mac OS X" in ua else "Linux" if "Linux" in ua else "")
    br = ("Edge" if "Edg/" in ua else "Chrome" if "Chrome/" in ua else "Firefox" if "Firefox/" in ua else "Safari" if "Safari/" in ua else "")
    return os_, br


def _clean_name(name: str, kind: str) -> str:
    n = str(name or "").strip()
    if not n:
        return "-"
    if n.startswith(("/", "http://", "https://")):
        return path_template(n)[:160]
    return scrub(n)[:120]


class RumStore:
    def __init__(self, kb, traces, live=None, settings=None):
        self.kb, self.traces, self.live = kb, traces, live
        self.settings = settings or (lambda: {})
        self.lock = threading.Lock()
        self.received = self.rejected = 0
        self._pruned = time.time()
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS rum_apps (id {pk}, name TEXT UNIQUE NOT NULL, key TEXT UNIQUE NOT NULL, platform TEXT DEFAULT 'web',
            origins TEXT DEFAULT '', sample_pct INTEGER DEFAULT 100, status TEXT DEFAULT 'active', created_at TEXT, note TEXT DEFAULT '')""")
        kb._exec("""CREATE TABLE IF NOT EXISTS rum_sessions (session_id TEXT PRIMARY KEY, app TEXT, platform TEXT, version TEXT, user_ref TEXT,
            os TEXT, browser TEXT, model TEXT, net TEXT, carrier TEXT, started TEXT, last TEXT, views INTEGER DEFAULT 0, actions INTEGER DEFAULT 0,
            requests INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, crashes INTEGER DEFAULT 0, duration_ms DOUBLE PRECISION DEFAULT 0, entry TEXT DEFAULT '', exit TEXT DEFAULT '')""")
        kb._exec("CREATE INDEX IF NOT EXISTS rum_sessions_app ON rum_sessions(app, last)")
        kb._exec("CREATE INDEX IF NOT EXISTS rum_sessions_user ON rum_sessions(user_ref)")
        from .demo import ensure_column
        ensure_column(kb, "rum_apps"); ensure_column(kb, "rum_sessions")   # 1 = the simulator's HR apps and their sessions (demo.py)

    # ---------------------------------------------------------------- apps (keys the sensors carry; public by nature, origin-bound)
    def apps(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM rum_apps ORDER BY name")]

    def app_by_key(self, key: str) -> dict | None:
        if not key:
            return None
        rows = self.kb._exec("SELECT * FROM rum_apps WHERE key=? AND status='active'", (key,))
        return dict(rows[0]) if rows else None

    def create_app(self, name: str, platform: str = "web", origins: str = "", sample_pct: int = 100, note: str = "", demo: bool = False) -> dict:
        name = re.sub(r"[^\w.\- ]", "", name or "").strip()[:60]
        if not name:
            raise ValueError("app name required")
        if platform not in PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(PLATFORMS)}")
        if self.kb._exec("SELECT id FROM rum_apps WHERE name=?", (name,)):
            raise ValueError(f"an app named '{name}' exists")
        key = "pk_" + secrets.token_urlsafe(18)
        self.kb._insert("INSERT INTO rum_apps (name, key, platform, origins, sample_pct, status, created_at, note, demo) VALUES (?,?,?,?,?,'active',?,?,?)",
                        (name, key, platform, origins.strip()[:1000], max(1, min(100, int(sample_pct))), _now(), note[:200], int(bool(demo))))
        return self.app_by_key(key)

    def update_app(self, aid: int, **kw) -> dict:
        cols = {k: v for k, v in kw.items() if k in ("origins", "sample_pct", "status", "note", "platform") and v is not None}
        if "sample_pct" in cols:
            cols["sample_pct"] = max(1, min(100, int(cols["sample_pct"])))
        if "status" in cols and cols["status"] not in ("active", "disabled"):
            raise ValueError("status: active | disabled")
        for k, v in cols.items():
            self.kb._exec(f"UPDATE rum_apps SET {k}=? WHERE id=?", (v, aid))
        rows = self.kb._exec("SELECT * FROM rum_apps WHERE id=?", (aid,))
        if not rows:
            raise KeyError(aid)
        return dict(rows[0])

    def rotate_key(self, aid: int) -> dict:
        self.kb._exec("UPDATE rum_apps SET key=? WHERE id=?", ("pk_" + secrets.token_urlsafe(18), aid))
        return dict(self.kb._exec("SELECT * FROM rum_apps WHERE id=?", (aid,))[0])

    def delete_app(self, aid: int) -> None:
        self.kb._exec("DELETE FROM rum_apps WHERE id=?", (aid,))

    @staticmethod
    def origin_allowed(app: dict, origin: str | None) -> bool:
        """Browser posts must come from one of the app's origins (empty list: any). Mobile SDKs send no Origin."""
        allowed = [o.strip().rstrip("/") for o in (app.get("origins") or "").replace("\n", ",").split(",") if o.strip()]
        if not origin or not allowed:
            return True
        o = origin.rstrip("/")
        return any(o == a or (a.startswith("*.") and o.endswith(a[1:])) or a == "*" for a in allowed)

    # ---------------------------------------------------------------- intake
    def ingest(self, app: dict, payload: bytes | dict, ua: str = "") -> dict:
        """One sensor batch -> spans (user / mobile / client / log), session upsert, ERROR events into the live feed."""
        doc = json.loads(payload) if isinstance(payload, (bytes, str)) else payload
        if not isinstance(doc, dict):
            raise ValueError("expected a JSON object")
        sess = re.sub(r"[^\w\-]", "", str(doc.get("session") or ""))[:64] or _hex(8)
        platform = str(doc.get("platform") or app.get("platform") or "web").lower()
        platform = platform if platform in PLATFORMS else "other"
        kind = "user" if platform == "web" else "mobile"
        version = scrub(str(doc.get("version") or ""))[:40]
        dev = doc.get("device") if isinstance(doc.get("device"), dict) else {}
        os_, br = _ua_family(ua) if platform == "web" else (scrub(str(dev.get("os") or ""))[:40], "")
        user_ref = pseudonym(str(doc.get("user") or "")) if doc.get("user") else ""
        svc = app["name"]
        from .demo import DEMO_OWNER
        demo = bool(app.get("demo")) or app.get("note") == DEMO_OWNER    # the simulator's demo app: every row it yields is demo (demo.py)
        events = [e for e in (doc.get("events") or []) if isinstance(e, dict)][:MAX_EVENTS]
        spans, errors_out = [], []
        counts = {"view": 0, "action": 0, "http": 0, "error": 0, "crash": 0}
        first = last = None
        entry = exit_ = ""
        for e in events:
            t = str(e.get("t") or "")
            try:
                ts = datetime.fromtimestamp(float(e.get("ts")) / 1000, UTC)
            except (TypeError, ValueError, OverflowError, OSError):
                ts = datetime.now(UTC)
            if abs((ts - datetime.now(UTC)).total_seconds()) > 7 * 86400:
                ts = datetime.now(UTC)
            first = ts if first is None or ts < first else first
            dur = e.get("dur")
            try:
                dur = max(0.0, min(float(dur), 3_600_000.0)) if dur is not None else None
            except (TypeError, ValueError):
                dur = None
            last_ts = ts + timedelta(milliseconds=dur or 0)
            last = last_ts if last is None or last_ts > last else last
            trace = re.sub(r"[^0-9a-fA-F]", "", str(e.get("trace") or ""))[:32] or None
            span = re.sub(r"[^0-9a-fA-F]", "", str(e.get("span") or ""))[:16] or None
            parent = re.sub(r"[^0-9a-fA-F]", "", str(e.get("parent") or ""))[:16] or None
            base = {"env": scrub(str(doc.get("env") or ""))[:20], "host": "", "user_ref": user_ref, "session_id": sess, "ref": f"rum:{svc}:{sess}",
                    "severity": "INFO", "status": 0, "error": 0, "has_duration": dur is not None, "duration_ms": round(dur or 0.0, 3), "start": ts,
                    "service": svc, "trace_id": trace, "span_id": span, "parent_id": parent, "link": "explicit" if trace else "session",
                    "confidence": 1.0 if trace else 0.7, "demo": int(demo)}
            if t in ("view", "action", "start"):
                name = _clean_name(e.get("name") or ("app start" if t == "start" else ""), t)
                counts["view" if t in ("view", "start") else "action"] += 1
                if t == "view":
                    entry = entry or name; exit_ = name
                attrs = {"step": t, "platform": platform, "version": version}
                if isinstance(e.get("vitals"), dict):
                    for k in VITALS_GOOD:
                        v = e["vitals"].get(k)
                        if isinstance(v, (int, float)) and v >= 0:
                            attrs[k] = round(float(v), 4 if k == "cls" else 1)
                if t == "start":
                    attrs["cold"] = bool(e.get("cold"))
                op = ("app start" + (" (cold)" if e.get("cold") else "")) if t == "start" else ({"view": "view ", "action": ""}[t] + name)
                spans.append({**base, "kind": kind, "operation": op[:160], "message": op[:300], "attrs": attrs})
            elif t == "http":
                counts["http"] += 1
                method = str(e.get("method") or "GET").upper()[:8]
                try:
                    st = int(e.get("status") or 0)
                except (TypeError, ValueError):
                    st = 0
                url = str(e.get("url") or "")
                host = re.sub(r"^https?://([^/]+).*$", r"\1", url) if url.startswith("http") else ""
                op = f"{method} {path_template(url)}"
                err = int(st >= 500 or st == 0)
                spans.append({**base, "kind": "client", "operation": op[:160], "message": op[:300], "status": st, "error": err,
                              "severity": "ERROR" if err else "INFO", "attrs": {"http.method": method, "http.status": st or None, "peer": host[:120], "platform": platform}})
            elif t in ("error", "crash"):
                counts[t] += 1
                msg = scrub(str(e.get("msg") or t))[:300]
                stack = scrub(str(e.get("stack") or ""))[:2000]
                spans.append({**base, "kind": "log", "has_duration": False, "duration_ms": 0.0, "operation": ("crash " if t == "crash" else "error ") + msg[:120],
                              "message": msg, "error": 1, "severity": "CRITICAL" if t == "crash" else "ERROR", "attrs": {"step": t, "platform": platform, "version": version, "stack": stack[:600]}})
                errors_out.append({"ts": ts.isoformat(), "level": "critical" if t == "crash" else "error", "service": svc, "env": base["env"] or "prod",
                                   "msg": f"[{platform} {version}] {'CRASH ' if t == 'crash' else ''}{msg}", "rum.session": sess, "rum.user_ref": user_ref,
                                   "rum.trace": trace or "", "rum.kind": t})   # rum.* keys: the error is a span already, the live copy feeds anomalies only
        if spans:
            self.traces.add_spans(spans)
        if errors_out and self.live is not None:
            try:
                from .demo import SIM_AGENT                      # a demo app's errors are simulator lines (sender "simulator")
                self.live.ingest(f"rum-{svc}.jsonl", ("\n".join(json.dumps(x) for x in errors_out) + "\n").encode(), SIM_AGENT if demo else f"rum:{svc}")
            except Exception:  # noqa: BLE001
                pass
        if first is not None:
            self._upsert_session(sess, svc, platform, version, user_ref, os_, br, scrub(str(dev.get("model") or ""))[:40], str(dev.get("net") or "")[:20],
                                 scrub(str(dev.get("carrier") or ""))[:40], first, last or first, counts, entry, exit_, demo)
        with self.lock:
            self.received += 1
            due = time.time() - self._pruned > 3600
            if due:
                self._pruned = time.time()
        if due:
            try:
                self.prune(int((self.settings() or {}).get("rum_retention_days", 30) or 30))
            except Exception:  # noqa: BLE001
                pass
        return {"session": sess, "spans": len(spans), "errors": counts["error"] + counts["crash"]}

    def _upsert_session(self, sess, app, platform, version, user_ref, os_, br, model, net, carrier, first, last, c, entry, exit_, demo=False):
        rows = self.kb._exec("SELECT * FROM rum_sessions WHERE session_id=?", (sess,))
        if rows:
            r = rows[0]
            started = min(r["started"], first.isoformat()); lst = max(r["last"], last.isoformat())
            dur = (datetime.fromisoformat(lst) - datetime.fromisoformat(started)).total_seconds() * 1000
            self.kb._exec("UPDATE rum_sessions SET last=?, views=views+?, actions=actions+?, requests=requests+?, errors=errors+?, crashes=crashes+?, duration_ms=?, "
                          "exit=CASE WHEN ?<>'' THEN ? ELSE exit END, user_ref=CASE WHEN user_ref='' THEN ? ELSE user_ref END, started=? WHERE session_id=?",
                          (lst, c["view"], c["action"], c["http"], c["error"], c["crash"], dur, exit_, exit_, user_ref, started, sess))
        else:
            self.kb._exec("INSERT INTO rum_sessions (session_id, app, platform, version, user_ref, os, browser, model, net, carrier, started, last, views, actions, requests, "
                          "errors, crashes, duration_ms, entry, exit, demo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (sess, app, platform, version, user_ref, os_, br, model, net, carrier, first.isoformat(), last.isoformat(), c["view"], c["action"],
                           c["http"], c["error"], c["crash"], (last - first).total_seconds() * 1000, entry, exit_, int(bool(demo))))

    # ---------------------------------------------------------------- read
    def sessions(self, app: str | None = None, platform: str | None = None, user_ref: str | None = None, errors: bool = False, minutes: int | None = 1440,
                 version: str | None = None, limit: int = 100) -> list[dict]:
        where, p = [], []
        if minutes:
            where.append("last >= ?"); p.append((datetime.now(UTC) - timedelta(minutes=minutes)).isoformat())
        for col, v in (("app", app), ("platform", platform), ("user_ref", user_ref), ("version", version)):
            if v:
                where.append(f"{col} = ?"); p.append(v)
        if errors:
            where.append("(errors > 0 OR crashes > 0)")
        q = "SELECT * FROM rum_sessions" + (" WHERE " + " AND ".join(where) if where else "") + f" ORDER BY last DESC LIMIT {max(1, min(int(limit), 500))}"
        return [dict(r) for r in self.kb._exec(q, tuple(p))]

    def range_summary(self, s: datetime, e: datetime) -> dict:
        """Web / mobile sessions active in [s, e) (the operations screen's session view): sessions, distinct users, errors + crashes."""
        r = self.kb._exec("SELECT COUNT(*) AS n, COUNT(DISTINCT CASE WHEN user_ref <> '' THEN user_ref END) AS u, COALESCE(SUM(errors + crashes), 0) AS e "
                          "FROM rum_sessions WHERE last >= ? AND started < ?", (s.astimezone(UTC).isoformat(), e.astimezone(UTC).isoformat()))
        r = dict(r[0]) if r else {}
        return {"sessions": int(r.get("n") or 0), "users": int(r.get("u") or 0), "errors": int(r.get("e") or 0)}

    def session(self, sid: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM rum_sessions WHERE session_id=?", (sid,))
        if not rows:
            return None
        steps = self.kb._exec("SELECT trace_id, span_id, parent_id, start_ms, duration_ms, service, operation, kind, status, error, severity, message, attrs "
                              "FROM spans WHERE session_id=? ORDER BY start_ms LIMIT 2000", (sid,))
        trace_ids = sorted({s["trace_id"] for s in steps if s["trace_id"] and s["kind"] in ("user", "mobile", "client")})
        backend = {}
        if trace_ids:
            for i in range(0, len(trace_ids), 200):
                chunk = trace_ids[i:i + 200]
                for r in self.kb._exec(f"SELECT trace_id, COUNT(*) AS n, COUNT(DISTINCT service) AS svcs, MAX(error) AS err FROM spans WHERE trace_id IN ({','.join('?' * len(chunk))}) "
                                       f"AND kind NOT IN ('user','mobile','client') AND service <> ? GROUP BY trace_id", (*chunk, rows[0]["app"])):
                    backend[r["trace_id"]] = {"spans": int(r["n"]), "services": int(r["svcs"]), "error": int(r["err"] or 0)}
        out = []
        for s in steps:
            a = json.loads(s["attrs"]) if s.get("attrs") else {}
            out.append({"ts": datetime.fromtimestamp(s["start_ms"] / 1000, UTC).isoformat(), "kind": s["kind"], "step": a.get("step") or ("http" if s["kind"] == "client" else s["kind"]),
                        "name": s["operation"], "duration_ms": s["duration_ms"], "status": s["status"], "error": s["error"], "trace_id": s["trace_id"],
                        "backend": backend.get(s["trace_id"]), "vitals": {k: a[k] for k in VITALS_GOOD if k in a}, "stack": a.get("stack", ""), "message": s["message"]})
        return {**dict(rows[0]), "steps": out}

    def overview(self, minutes: int | None = 1440, app: str | None = None) -> dict:
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat() if minutes else ""
        cond, p = ("WHERE last >= ?", [since]) if since else ("", [])
        if app:
            cond = (cond + " AND " if cond else "WHERE ") + "app = ?"; p.append(app)
        apps = []
        for r in self.kb._exec(f"SELECT app, platform, COUNT(*) AS sessions, COUNT(DISTINCT CASE WHEN user_ref<>'' THEN user_ref END) AS users, SUM(views) AS views, "
                               f"SUM(actions) AS actions, SUM(requests) AS requests, SUM(errors) AS errors, SUM(crashes) AS crashes, "
                               f"SUM(CASE WHEN errors>0 OR crashes>0 THEN 1 ELSE 0 END) AS bad, SUM(CASE WHEN crashes>0 THEN 1 ELSE 0 END) AS crashed, "
                               f"AVG(duration_ms) AS avg_ms FROM rum_sessions {cond} GROUP BY app, platform ORDER BY sessions DESC", tuple(p)):
            d = dict(r)
            d["crash_free"] = round(1 - (d["crashed"] or 0) / d["sessions"], 4) if d["sessions"] else None
            d["error_free"] = round(1 - (d["bad"] or 0) / d["sessions"], 4) if d["sessions"] else None
            apps.append(d)
        sp_cond, sp = ["kind IN ('user','mobile')"], []
        if since:
            sp_cond.append("ts >= ?"); sp.append(since)
        if app:
            sp_cond.append("service = ?"); sp.append(app)
        steps = self.kb._exec(f"SELECT service, operation, duration_ms, attrs, error FROM spans WHERE {' AND '.join(sp_cond)} ORDER BY id DESC LIMIT 20000", tuple(sp))
        vit: dict[str, dict[str, list]] = {}
        per_step: dict[tuple, list] = {}
        starts: dict[str, list] = {}
        for s in steps:
            a = json.loads(s["attrs"]) if s["attrs"] else {}
            for k in VITALS_GOOD:
                if k in a:
                    vit.setdefault(s["service"], {}).setdefault(k, []).append(a[k])
            per_step.setdefault((s["service"], s["operation"], a.get("step", "")), []).append(s["duration_ms"])
            if a.get("step") == "start":
                starts.setdefault(s["service"], []).append(s["duration_ms"])

        def p(vals: list, q: float):
            v = sorted(vals)
            return v[min(len(v) - 1, int(q * len(v)))] if v else None

        def grade(k, v):
            return None if v is None else "good" if v <= VITALS_GOOD[k] else "poor" if v > VITALS_POOR[k] else "needs"
        vitals = {svc: {k: {"p75": p(v, 0.75), "n": len(v), "grade": grade(k, p(v, 0.75))} for k, v in d.items()} for svc, d in vit.items()}
        slow = sorted(({"app": k[0], "step": k[2], "name": k[1], "count": len(v), "p50": p(v, 0.5), "p75": p(v, 0.75), "p95": p(v, 0.95)} for k, v in per_step.items()
                       if k[2] in ("view", "action") and len(v) >= 2), key=lambda x: -(x["p75"] or 0))[:15]
        err_cond, ep = ["kind='log'", "error=1", "ref LIKE 'rum:%'"], []
        if since:
            err_cond.append("ts >= ?"); ep.append(since)
        if app:
            err_cond.append("service = ?"); ep.append(app)
        errs = [dict(r) for r in self.kb._exec(f"SELECT service AS app, message, severity, COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions, MAX(ts) AS last "
                                               f"FROM spans WHERE {' AND '.join(err_cond)} GROUP BY service, message, severity ORDER BY n DESC LIMIT 15", tuple(ep))]
        return {"apps": apps, "vitals": vitals, "app_start": {k: {"p50": p(v, 0.5), "p95": p(v, 0.95), "n": len(v)} for k, v in starts.items()},
                "slow_steps": slow, "errors": errs, "thresholds": {"good": VITALS_GOOD, "poor": VITALS_POOR}}

    def prune(self, days: int = 30) -> int:
        cut = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        n = int(self.kb._exec("SELECT COUNT(*) AS n FROM rum_sessions WHERE last < ?", (cut,))[0]["n"])
        self.kb._exec("DELETE FROM rum_sessions WHERE last < ?", (cut,))
        return n
