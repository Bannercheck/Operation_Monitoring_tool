"""Pull sources: Watchover polls a log platform the organisation already runs and streams what it finds into the live
store, exactly as if an agent had shipped it. Standard library only; every kind is a small fetch() that returns canonical
rows, so adding a platform is one function.

Kinds: elasticsearch / opensearch (ECS or free fields), loki (Grafana Loki), splunk (REST export), graylog (universal
search), http (any JSON / text endpoint). A cursor (last timestamp) is persisted per source so polling never re-sends the
same window, and a rolling hash set drops the occasional duplicate at the boundary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Callable

UTC = timezone.utc

# ---------------------------------------------------------------- catalogue (what the UI shows and what each kind needs)
KINDS: dict[str, dict] = {
    "elasticsearch": {"label": "Elasticsearch", "icon": "🔎", "port": 9200, "selector": "index pattern", "selector_ex": "logs-*,filebeat-*",
                      "auth": ("none", "basic", "apikey", "bearer"), "path_hint": "https://es.example.com:9200",
                      "needs": "URL of a node or the cluster load balancer, an index pattern, a read-only user or API key (read on the indices).",
                      "logs": "Anything indexed: Filebeat / Logstash / Fluentd / Vector output, ECS fields (@timestamp, message, host.name, service.name, log.level) or free fields."},
    "opensearch": {"label": "OpenSearch", "icon": "🔎", "port": 9200, "selector": "index pattern", "selector_ex": "logs-*", "auth": ("none", "basic", "bearer"),
                   "path_hint": "https://opensearch.example.com:9200", "needs": "Same as Elasticsearch (the query API is identical).", "logs": "Same as Elasticsearch."},
    "loki": {"label": "Grafana Loki", "icon": "🌀", "port": 3100, "selector": "LogQL stream selector", "selector_ex": '{job="varlogs"}', "auth": ("none", "basic", "bearer"),
             "path_hint": "http://loki.example.com:3100", "needs": "Loki base URL and a stream selector; multi-tenant setups also need the X-Scope-OrgID header.",
             "logs": "Every stream Promtail / Alloy / Fluent Bit pushes; labels host / instance / app / job / container become host and service."},
    "splunk": {"label": "Splunk", "icon": "🟢", "port": 8089, "selector": "search (without the leading 'search')", "selector_ex": "index=main sourcetype=syslog",
               "auth": ("basic", "bearer"), "path_hint": "https://splunk.example.com:8089",
               "needs": "Management port 8089 (not the web port), a user with search rights or an authentication token.",
               "logs": "Whatever the search returns: _raw plus host, source, sourcetype; JSON events are parsed, others pass through the format detector."},
    "graylog": {"label": "Graylog", "icon": "🐙", "port": 9000, "selector": "query", "selector_ex": "level:<=4", "auth": ("basic", "apikey"),
                "path_hint": "http://graylog.example.com:9000", "needs": "REST API URL and a user or an access token (token as user name).",
                "logs": "All streams the user may read: GELF / syslog inputs with message, source, level, facility."},
    "http": {"label": "HTTP / JSON", "icon": "🌐", "port": 443, "selector": "JSON path to the record list (optional)", "selector_ex": "data.events",
             "auth": ("none", "basic", "apikey", "bearer"), "path_hint": "https://api.example.com/v1/events?since=-5m",
             "needs": "Any endpoint that returns a JSON list or plain log text (Datadog, Sumo, Papertrail exports, in-house APIs).",
             "logs": "JSON records (fields guessed: ts / time, level, host, service, message) or raw text lines through the format detector."},
}
KIND_ALIASES = {"opensearch": "elasticsearch"}
TS_KEYS = ("@timestamp", "timestamp", "ts", "time", "_time", "date", "event_time")
MSG_KEYS = ("message", "msg", "_raw", "log", "text", "event", "line")
HOST_KEYS = ("host", "hostname", "host.name", "source", "instance", "node", "server")
SVC_KEYS = ("service", "service.name", "app", "application", "job", "container", "sourcetype", "program", "logger")
LVL_KEYS = ("level", "log.level", "severity", "loglevel", "lvl", "priority")


@dataclass
class Source:
    id: int | None
    name: str
    kind: str
    url: str
    selector: str = ""
    auth: str = "none"
    user: str = ""
    secret: str = ""
    interval: int = 30
    env: str = ""
    site: str = ""
    enabled: bool = True
    verify_tls: bool = True
    headers: str = ""          # extra 'Key: Value' lines (X-Scope-OrgID, proxies)
    ts_field: str = ""         # override the timestamp field (Elasticsearch / http)
    lookback_min: int = 15     # first poll starts this far back
    cursor: str = ""
    last_ok: str = ""
    last_err: str = ""
    events: int = 0
    polls: int = 0

    def as_row(self) -> dict:
        return asdict(self)


def _ctx(verify: bool):
    if verify:
        return ssl.create_default_context()
    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
    return c


def _headers(src: Source, extra: dict | None = None) -> dict:
    h = {"Accept": "application/json", "User-Agent": "watchover-source/1.0"}
    for ln in (src.headers or "").splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1); h[k.strip()] = v.strip()
    if src.auth == "basic" and (src.user or src.secret):
        h["Authorization"] = "Basic " + base64.b64encode(f"{src.user}:{src.secret}".encode()).decode()
    elif src.auth == "bearer" and src.secret:
        h["Authorization"] = f"Bearer {src.secret}"
    elif src.auth == "apikey" and src.secret:
        kind = KIND_ALIASES.get(src.kind, src.kind)
        h["Authorization"] = f"ApiKey {src.secret}" if kind == "elasticsearch" else (f"Bearer {src.secret}" if kind == "http" else src.secret)
        if kind == "graylog":                                  # Graylog: token as user name, password 'token'
            h["Authorization"] = "Basic " + base64.b64encode(f"{src.secret}:token".encode()).decode()
    h.update(extra or {})
    return h


def _call(src: Source, url: str, method: str = "GET", body: bytes | None = None, extra: dict | None = None, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, data=body, method=method, headers=_headers(src, extra))
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx(src.verify_tls)) as r:
        return r.read()


def _pick(d: dict, keys: tuple) -> object:
    """First present key; dotted keys look into nested dicts (ECS: host.name)."""
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        if "." in k:
            cur = d
            for part in k.split("."):
                cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is None:
                    break
            if cur not in (None, ""):
                return cur
    return None


def _scalar(v) -> str:
    if isinstance(v, dict):
        return str(v.get("name") or v.get("hostname") or v.get("id") or json.dumps(v, ensure_ascii=False)[:120])
    if isinstance(v, list):
        return ", ".join(str(x) for x in v[:3])
    return str(v)


def _parse_ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        for div in (1.0, 1e3, 1e6, 1e9):                      # s / ms / µs / ns: the first scale that lands before year 2096
            if x / div < 4e9:
                return datetime.fromtimestamp(x / div, tz=UTC)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def row_from_record(rec: dict, ts_field: str = "") -> dict:
    """Canonical row {ts, level, host, service, msg, extra} from any JSON log record (ECS or free-form)."""
    ts = _parse_ts(rec.get(ts_field)) if ts_field else None
    ts = ts or _parse_ts(_pick(rec, TS_KEYS))
    msg = _pick(rec, MSG_KEYS)
    if isinstance(msg, dict):
        msg = msg.get("message") or json.dumps(msg, ensure_ascii=False)
    row = {"ts": (ts or datetime.now(UTC)).isoformat(), "level": _scalar(_pick(rec, LVL_KEYS) or ""), "host": _scalar(_pick(rec, HOST_KEYS) or ""),
           "service": _scalar(_pick(rec, SVC_KEYS) or ""), "msg": str(msg) if msg is not None else json.dumps(rec, ensure_ascii=False)[:1000]}
    return row


def to_jsonl(rows: list[dict], env: str = "") -> bytes:
    out = []
    for r in rows:
        d = {k: v for k, v in r.items() if v not in ("", None)}
        if env:
            d.setdefault("env", env)
        out.append(json.dumps(d, ensure_ascii=False))
    return ("\n".join(out) + "\n").encode()


# ---------------------------------------------------------------- fetchers: (source, since) -> (rows, new_cursor)
def fetch_elasticsearch(src: Source, since: datetime, limit: int) -> tuple[list[dict], str]:
    ts_field = src.ts_field or "@timestamp"
    body = {"size": limit, "sort": [{ts_field: {"order": "asc", "unmapped_type": "date"}}],
            "query": {"range": {ts_field: {"gt": since.isoformat()}}}}
    idx = urllib.parse.quote(src.selector.strip() or "*", safe="*,-_")
    data = _call(src, f"{src.url.rstrip('/')}/{idx}/_search", "POST", json.dumps(body).encode(), {"Content-Type": "application/json"})
    doc = json.loads(data or b"{}")
    hits = (doc.get("hits") or {}).get("hits") or []
    rows, last = [], since
    for h in hits:
        rec = h.get("_source") or {}
        row = row_from_record(rec, ts_field)
        if not row["host"] and isinstance(rec.get("agent"), dict):      # Beats put the shipper host under agent.hostname
            row["host"] = _scalar(rec["agent"].get("hostname", ""))
        rows.append(row)
        t = _parse_ts(rec.get(ts_field)) or _parse_ts(row["ts"])
        if t and t > last:
            last = t
    return rows, last.isoformat()


def fetch_loki(src: Source, since: datetime, limit: int) -> tuple[list[dict], str]:
    start_ns = int(since.timestamp() * 1e9); end_ns = int(time.time() * 1e9)
    q = urllib.parse.urlencode({"query": src.selector.strip() or '{job=~".+"}', "start": start_ns, "end": end_ns, "limit": limit, "direction": "forward"})
    doc = json.loads(_call(src, f"{src.url.rstrip('/')}/loki/api/v1/query_range?{q}") or b"{}")
    rows, last_ns = [], start_ns
    for stream in ((doc.get("data") or {}).get("result") or []):
        labels = stream.get("stream") or {}
        host = _pick(labels, ("host", "hostname", "instance", "node", "nodename", "node_name")) or ""
        svc = _pick(labels, ("app", "service", "service_name", "job", "container", "app_kubernetes_io_name", "unit")) or ""
        lvl = _pick(labels, ("level", "detected_level", "severity")) or ""
        for ns, line in stream.get("values") or []:
            ns = int(ns); last_ns = max(last_ns, ns)
            row = {"ts": datetime.fromtimestamp(ns / 1e9, tz=UTC).isoformat(), "host": str(host), "service": str(svc), "level": str(lvl), "msg": line}
            if line.lstrip().startswith("{"):                # JSON line inside the stream: take its fields
                try:
                    inner = row_from_record(json.loads(line)); row.update({k: v for k, v in inner.items() if v and k != "ts"})
                except ValueError:
                    pass
            rows.append(row)
    return rows, datetime.fromtimestamp((last_ns + 1) / 1e9, tz=UTC).isoformat() if rows else since.isoformat()


def fetch_splunk(src: Source, since: datetime, limit: int) -> tuple[list[dict], str]:
    search = src.selector.strip() or "index=main"
    if not search.lower().startswith(("search ", "|")):
        search = "search " + search
    form = urllib.parse.urlencode({"search": f"{search} | head {limit}", "output_mode": "json", "earliest_time": since.strftime("%Y-%m-%dT%H:%M:%S.000+00:00"), "latest_time": "now"}).encode()
    data = _call(src, f"{src.url.rstrip('/')}/services/search/jobs/export", "POST", form, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=90)
    rows, last = [], since
    for ln in data.decode("utf-8", "replace").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln).get("result") or {}
        except ValueError:
            continue
        if not rec:
            continue
        row = row_from_record(rec)
        row["host"] = row["host"] or str(rec.get("host", "")); row["service"] = row["service"] or str(rec.get("sourcetype") or rec.get("source") or "")
        rows.append(row)
        t = _parse_ts(rec.get("_time"))
        if t and t > last:
            last = t
    return rows, (last + timedelta(milliseconds=1)).isoformat() if rows else since.isoformat()


def fetch_graylog(src: Source, since: datetime, limit: int) -> tuple[list[dict], str]:
    q = urllib.parse.urlencode({"query": src.selector.strip() or "*", "from": since.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                "to": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "limit": limit, "sort": "timestamp:asc"})
    doc = json.loads(_call(src, f"{src.url.rstrip('/')}/api/search/universal/absolute?{q}") or b"{}")
    rows, last = [], since
    for m in doc.get("messages") or []:
        rec = m.get("message") or m
        row = row_from_record(rec)
        row["host"] = row["host"] or str(rec.get("source", "")); row["service"] = row["service"] or str(rec.get("facility") or rec.get("application_name") or "")
        rows.append(row)
        t = _parse_ts(rec.get("timestamp"))
        if t and t > last:
            last = t
    return rows, (last + timedelta(milliseconds=1)).isoformat() if rows else since.isoformat()


def fetch_http(src: Source, since: datetime, limit: int) -> tuple[list[dict], str]:
    """Generic endpoint: JSON list (optionally under a path) becomes rows; anything else is raw text lines."""
    url = src.url.replace("{since}", urllib.parse.quote(since.isoformat())).replace("{since_epoch}", str(int(since.timestamp())))
    data = _call(src, url)
    text = data.decode("utf-8", "replace")
    rows: list[dict] = []
    try:
        doc = json.loads(text)
        if src.selector.strip():
            for part in [p for p in src.selector.replace("[", ".").replace("]", "").split(".") if p]:
                doc = doc[int(part)] if isinstance(doc, list) else doc[part]
        if isinstance(doc, dict):
            doc = next((v for v in doc.values() if isinstance(v, list)), [doc])
        for rec in doc[:limit]:
            rows.append(row_from_record(rec, src.ts_field) if isinstance(rec, dict) else {"ts": datetime.now(UTC).isoformat(), "msg": str(rec)})
    except (ValueError, KeyError, IndexError, TypeError):
        rows = [{"ts": datetime.now(UTC).isoformat(), "msg": ln} for ln in text.splitlines() if ln.strip()][-limit:]
        rows = [{"raw": True, **r} for r in rows]
    last = max((_parse_ts(r["ts"]) for r in rows if _parse_ts(r["ts"])), default=since)
    return rows, max(last, since).isoformat()


FETCHERS: dict[str, Callable] = {"elasticsearch": fetch_elasticsearch, "opensearch": fetch_elasticsearch, "loki": fetch_loki,
                                 "splunk": fetch_splunk, "graylog": fetch_graylog, "http": fetch_http}


def fetch(src: Source, since: datetime | None = None, limit: int = 500) -> tuple[list[dict], str]:
    """Rows newer than the cursor (or the lookback window) and the cursor to persist."""
    if since is None:
        since = _parse_ts(src.cursor) if src.cursor else None
        since = since or datetime.now(UTC) - timedelta(minutes=max(src.lookback_min, 1))
    return FETCHERS[src.kind](src, since, limit)


# ---------------------------------------------------------------- persistence (same DB as the knowledge base)
class SourceStore:
    COLS = ("name", "kind", "url", "selector", "auth", "user", "secret", "interval", "env", "site", "enabled", "verify_tls", "headers",
            "ts_field", "lookback_min", "cursor", "last_ok", "last_err", "events", "polls")

    def __init__(self, kb):
        self.kb = kb
        pk = "SERIAL PRIMARY KEY" if kb.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        kb._exec(f"""CREATE TABLE IF NOT EXISTS sources (id {pk}, name TEXT NOT NULL, kind TEXT NOT NULL, url TEXT NOT NULL, selector TEXT DEFAULT '',
            auth TEXT DEFAULT 'none', "user" TEXT DEFAULT '', secret TEXT DEFAULT '', interval INTEGER DEFAULT 30, env TEXT DEFAULT '', site TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1, verify_tls INTEGER DEFAULT 1, headers TEXT DEFAULT '', ts_field TEXT DEFAULT '', lookback_min INTEGER DEFAULT 15,
            cursor TEXT DEFAULT '', last_ok TEXT DEFAULT '', last_err TEXT DEFAULT '', events INTEGER DEFAULT 0, polls INTEGER DEFAULT 0)""")

    def _row(self, r: dict) -> Source:
        from . import vault
        d = dict(r); d["enabled"] = bool(d.get("enabled", 1)); d["verify_tls"] = bool(d.get("verify_tls", 1)); d["secret"] = vault.decrypt(d.get("secret", "") or "")
        return Source(**{k: d.get(k, "") for k in ("id",) + self.COLS})

    def list(self) -> list[Source]:
        return [self._row(r) for r in self.kb._exec("SELECT * FROM sources ORDER BY name")]

    def get(self, sid: int) -> Source | None:
        rows = self.kb._exec("SELECT * FROM sources WHERE id=?", (sid,))
        return self._row(rows[0]) if rows else None

    def add(self, src: Source) -> Source:
        if any(s.name.lower() == src.name.strip().lower() for s in self.list()):
            raise ValueError(f"a source named '{src.name.strip()}' already exists")
        if src.kind not in FETCHERS:
            raise ValueError(f"unknown kind {src.kind}")
        cols = ", ".join(f'"{c}"' if c == "user" else c for c in self.COLS)
        from . import vault
        vals = tuple(int(v) if isinstance(v, bool) else (vault.encrypt(v) if c == "secret" else v) for c, v in ((c, getattr(src, c)) for c in self.COLS))
        sid = self.kb._insert(f"INSERT INTO sources ({cols}) VALUES ({', '.join('?' * len(self.COLS))})", vals)
        return self.get(sid)

    def update(self, sid: int, **fields) -> None:
        if not fields:
            return
        from . import vault
        if "secret" in fields:
            fields["secret"] = vault.encrypt(fields["secret"] or "")
        sets = ", ".join(f'"{k}"=?' if k == "user" else f"{k}=?" for k in fields)
        self.kb._exec(f"UPDATE sources SET {sets} WHERE id=?", tuple(int(v) if isinstance(v, bool) else v for v in fields.values()) + (sid,))

    def delete(self, sid: int) -> None:
        self.kb._exec("DELETE FROM sources WHERE id=?", (sid,))


# ---------------------------------------------------------------- poller: one background thread polls every enabled source
class Poller:
    """Polls each enabled source at its interval and ingests rows into the live store as agent '<source name>'."""

    def __init__(self, store: SourceStore, live, on_rows: Callable | None = None):
        self.store, self.live, self.on_rows = store, live, on_rows
        self.stop = threading.Event()
        self.due: dict[int, float] = {}
        self.seen: dict[int, deque] = {}
        self.thread = threading.Thread(target=self._loop, daemon=True, name="source-poller")

    def start(self) -> "Poller":
        self.thread.start(); return self

    def poll_one(self, src: Source, limit: int = 500) -> int:
        rows, cursor = fetch(src, limit=limit)
        seen = self.seen.setdefault(src.id, deque(maxlen=5000))
        keep = []
        for r in rows:
            h = hashlib.sha1(f"{r.get('ts')}|{r.get('host')}|{r.get('msg')}".encode()).hexdigest()
            if h in seen:
                continue
            seen.append(h); keep.append(r)
        n = 0
        if keep:
            n = self.live.ingest(f"{src.name}.jsonl", to_jsonl(keep, src.env), agent=src.name, env=src.env, site=src.site)
        self.store.update(src.id, cursor=cursor, last_ok=datetime.now(UTC).isoformat(timespec="seconds"), last_err="", events=src.events + n, polls=src.polls + 1)
        if self.on_rows:
            self.on_rows(src, keep)
        return n

    def _loop(self) -> None:
        while not self.stop.is_set():
            now = time.time()
            for src in self.store.list():
                if not src.enabled or now < self.due.get(src.id, 0):
                    continue
                self.due[src.id] = now + max(int(src.interval or 30), 5)
                try:
                    self.poll_one(src)
                except Exception as e:  # noqa: BLE001 - keep polling the others
                    self.store.update(src.id, last_err=f"{type(e).__name__}: {str(e)[:200]}", polls=src.polls + 1)
            self.stop.wait(2.0)


def test_source(src: Source, limit: int = 5) -> tuple[bool, str, list[dict]]:
    """Try one fetch over the lookback window; (ok, message, sample rows)."""
    try:
        rows, cursor = fetch(src, since=datetime.now(UTC) - timedelta(minutes=max(src.lookback_min, 1)), limit=limit)
        return True, f"{len(rows)} rows · cursor {cursor[:19]}", rows
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {(e.read() or b'').decode('utf-8', 'replace')[:200]}", []
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}", []
