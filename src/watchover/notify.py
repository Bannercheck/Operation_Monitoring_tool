"""Alerting: e-mail (SMTP) and SMS (HTTP gateway: Twilio, Netgsm, any template) to people and groups, driven by rules
evaluated against the live feed every minute. Deduplicated per condition with a cooldown; every delivery is logged.

Conditions: slo (availability under the SLO for the window), errors (ERROR+ per minute above a threshold),
metric (CPU / memory / disk / GPU above the scenario threshold), agent_offline (an enrolled agent silent longer than N
minutes), source_failing (a pull source in error), incident (a new incident card from live learning)."""
from __future__ import annotations

import base64
import json
import re
import smtplib
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr

UTC = timezone.utc
CONDITIONS = ("slo", "errors", "metric", "agent_offline", "source_failing", "incident")
SEVERITIES = ("critical", "high", "medium", "low")
# SMS gateways. fields: which inputs the settings form shows; number: how the recipient number is written (e164 +90…, digits 90…, national 0…);
# custom: the URL and body come from the provider's own integration document (operator corporate SMS contracts carry a per-customer endpoint);
# fail: a regex on the HTTP 200 body that still means rejection (several Turkish gateways answer 200 with an error code).
SMS_PRESETS = {
    "twilio": {"label": "Twilio", "url": "https://api.twilio.com/2010-04-01/Accounts/{account}/Messages.json", "method": "POST", "auth": "basic",
               "body": "From={from}&To={to}&Body={msg}", "content_type": "application/x-www-form-urlencoded", "fields": ["account", "token", "from"], "number": "e164"},
    "netgsm": {"label": "Netgsm", "url": "https://api.netgsm.com.tr/sms/send/get?usercode={user}&password={password}&gsmno={to}&message={msg}&msgheader={from}", "method": "GET", "auth": "none",
               "body": "", "content_type": "", "fields": ["user", "password", "header"], "number": "digits", "fail": r"^\s*(20|30|40|50|51|60|70|80|85)\b"},
    "iletimerkezi": {"label": "İleti Merkezi", "url": "https://api.iletimerkezi.com/v1/send-sms/json", "method": "POST", "auth": "none",
                     "body": '{"request": {"authentication": {"username": "{user}", "password": "{password}"}, "order": {"sender": "{from}", "message": {"text": "{msg}", "receipents": {"number": ["{to}"]}}}}}',
                     "content_type": "application/json", "fields": ["user", "password", "header"], "number": "digits", "fail": r'"code"\s*:\s*"?(?!200)\d'},
    "verimor": {"label": "Verimor", "url": "https://sms.verimor.com.tr/v2/send.json", "method": "POST", "auth": "none",
                "body": '{"username": "{user}", "password": "{password}", "source_addr": "{from}", "messages": [{"msg": "{msg}", "dest": "{to}"}]}',
                "content_type": "application/json", "fields": ["user", "password", "header"], "number": "digits"},
    "mutlucell": {"label": "Mutlucell", "url": "https://smsgw.mutlucell.com/smsgw-ws/sndblkex", "method": "POST", "auth": "none",
                  "body": '<smspack ka="{user}" pwd="{password}" org="{from}"><mesaj><metin>{msg}</metin><nums>{to}</nums></mesaj></smspack>',
                  "content_type": "text/xml", "fields": ["user", "password", "header"], "number": "digits", "fail": r"^\s*(2\d|30)\s*$"},
    "jetsms": {"label": "JetSMS (Biotekno)", "url": "https://service.jetsms.com.tr/SMS-Web/HttpSmsSend?Username={user}&Password={password}&Msisdns={to}&Messages={msg}&Originator={from}&TransmissionID={account}",
               "method": "GET", "auth": "none", "body": "", "content_type": "", "fields": ["user", "password", "header", "account"], "number": "digits"},
    "postaguvercini": {"label": "Posta Güvercini", "url": "https://www.postaguvercini.com/api_json/v1/Sms/Send_1_N", "method": "POST", "auth": "none",
                       "body": '{"user": "{user}", "password": "{password}", "sender": "{from}", "message": "{msg}", "receipients": ["{to}"]}',
                       "content_type": "application/json", "fields": ["user", "password", "header"], "number": "digits", "custom": True},
    "turkcell": {"label": "Turkcell Kurumsal Mesaj", "url": "", "method": "POST", "auth": "basic", "body": '{"from": "{from}", "to": "{to}", "text": "{msg}"}',
                 "content_type": "application/json", "fields": ["url", "user", "password", "header", "body"], "number": "digits", "custom": True},
    "turktelekom": {"label": "Türk Telekom Kurumsal SMS", "url": "", "method": "POST", "auth": "basic", "body": '{"from": "{from}", "to": "{to}", "text": "{msg}"}',
                    "content_type": "application/json", "fields": ["url", "user", "password", "header", "body"], "number": "digits", "custom": True},
    "vodafone": {"label": "Vodafone Kurumsal Mesaj", "url": "", "method": "POST", "auth": "basic", "body": '{"from": "{from}", "to": "{to}", "text": "{msg}"}',
                 "content_type": "application/json", "fields": ["url", "user", "password", "header", "body"], "number": "digits", "custom": True},
    "http": {"label": "HTTP gateway (template)", "url": "https://sms.example.com/send", "method": "POST", "auth": "bearer", "body": '{"to": "{to}", "text": "{msg}"}',
             "content_type": "application/json", "fields": ["url", "method", "auth", "content_type", "token", "from", "body"], "number": "e164", "custom": True},
}


def format_number(number: str, style: str = "e164") -> str:
    """+90 555 111 22 33 / 0555… / 90555… -> the shape the gateway wants: e164 (+905551112233), digits (905551112233), national (05551112233)."""
    d = re.sub(r"[^\d]", "", number or "")
    if d.startswith("00"):
        d = d[2:]
    if len(d) == 10 and d.startswith("5"):
        d = "90" + d                                   # bare Turkish mobile number
    elif len(d) == 11 and d.startswith("05"):
        d = "9" + d
    if style == "digits":
        return d
    if style == "national":
        return "0" + d[2:] if d.startswith("90") else d
    return "+" + d


# ---------------------------------------------------------------- channels
def _fill(template: str, vals: dict) -> str:
    """Replace {to} {msg} ... placeholders only (JSON braces in the template are left alone)."""
    return re.sub(r"\{(" + "|".join(vals) + r")\}", lambda m: vals[m.group(1)], template)


def send_email(cfg: dict, to: list[str], subject: str, text: str, html: str | None = None) -> None:
    """cfg: host, port, security (starttls|ssl|none), user, password, from_addr, from_name."""
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "Watchover", cfg.get("from_addr") or cfg.get("user") or "watchover@localhost"))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    port = int(cfg.get("port") or (465 if cfg.get("security") == "ssl" else 587))
    if cfg.get("security") == "ssl":
        server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(cfg["host"], port, timeout=20)
    try:
        server.ehlo()
        if cfg.get("security", "starttls") == "starttls":
            server.starttls(context=ssl.create_default_context()); server.ehlo()
        if cfg.get("user"):
            server.login(cfg["user"], cfg.get("password", ""))
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


def send_sms(cfg: dict, to: str, text: str) -> str:
    """cfg: preset (a SMS_PRESETS key), url, method, auth (none|basic|bearer), user, password, token, from, account, body, content_type.
    Placeholders {to} {msg} {from} {user} {password} {account} {token} are filled (URL-encoded in the URL / form body, escaped in JSON / XML).
    A custom preset takes the URL / body from cfg; a fixed one always uses the catalogue's. A 200 whose body matches the preset's fail regex raises."""
    preset = SMS_PRESETS.get(cfg.get("preset", "http"), SMS_PRESETS["http"])
    custom = preset.get("custom", False)
    url_t = (cfg.get("url") or preset["url"]) if custom else preset["url"]
    method = ((cfg.get("method") if custom else "") or preset["method"]).upper()
    body_t = (cfg.get("body") or preset["body"]) if custom else preset["body"]
    ctype = ((cfg.get("content_type") if custom else "") or preset["content_type"])
    if not url_t:
        raise RuntimeError("the provider's endpoint URL is missing")
    to = format_number(to, preset.get("number", "e164"))
    vals = {"to": to, "msg": text[:600], "from": cfg.get("from", ""), "user": cfg.get("user", ""), "password": cfg.get("password", ""), "account": cfg.get("account", ""), "token": cfg.get("token", "")}
    url = _fill(url_t, {k: urllib.parse.quote(str(v), safe="") for k, v in vals.items()})
    if "json" in ctype:
        body = _fill(body_t, {k: json.dumps(str(v))[1:-1] for k, v in vals.items()}).encode()
    elif "xml" in ctype:
        import html as _h
        body = _fill(body_t, {k: _h.escape(str(v), quote=True) for k, v in vals.items()}).encode()
    else:
        body = _fill(body_t, {k: urllib.parse.quote_plus(str(v)) for k, v in vals.items()}).encode() if body_t else None
    headers = {"User-Agent": "watchover-notify/1.0"}
    if ctype:
        headers["Content-Type"] = ctype
    auth = ((cfg.get("auth") if custom else "") or preset["auth"])
    if auth == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(f"{cfg.get('account') or cfg.get('user', '')}:{cfg.get('token') or cfg.get('password', '')}".encode()).decode()
    elif auth == "bearer" and (cfg.get("token") or cfg.get("password")):
        headers["Authorization"] = f"Bearer {cfg.get('token') or cfg.get('password')}"
    req = urllib.request.Request(url, data=body if method != "GET" else None, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        out = r.read().decode("utf-8", "replace")[:300]
    if preset.get("fail") and re.search(preset["fail"], out):
        raise RuntimeError(f"gateway rejected the message: {out[:80]}")
    return out


# ---------------------------------------------------------------- recipients, groups, rules, alert log
class Notifier:
    def __init__(self, kb, channels_fn=None):
        """channels_fn() -> {"email": cfg, "sms": cfg} from the settings store (secrets stay in config.json, mode 0600)."""
        self.kb, self.channels_fn = kb, channels_fn or (lambda: {})
        pk = "SERIAL PRIMARY KEY" if kb.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        kb._exec(f"CREATE TABLE IF NOT EXISTS recipients (id {pk}, name TEXT NOT NULL, email TEXT DEFAULT '', phone TEXT DEFAULT '', groups TEXT DEFAULT '', enabled INTEGER DEFAULT 1, note TEXT DEFAULT '')")
        kb._exec(f"CREATE TABLE IF NOT EXISTS notify_groups (id {pk}, name TEXT NOT NULL UNIQUE, email TEXT DEFAULT '', note TEXT DEFAULT '')")
        kb._exec(f"""CREATE TABLE IF NOT EXISTS notify_rules (id {pk}, name TEXT NOT NULL, condition TEXT NOT NULL, threshold REAL DEFAULT 0, env TEXT DEFAULT '',
            severity TEXT DEFAULT 'high', channels TEXT DEFAULT 'email', targets TEXT DEFAULT '', cooldown_min INTEGER DEFAULT 30, enabled INTEGER DEFAULT 1)""")
        kb._exec(f"""CREATE TABLE IF NOT EXISTS alerts (id {pk}, ts TEXT, rule_id INTEGER, key TEXT, severity TEXT, title TEXT, body TEXT, channels TEXT, recipients TEXT,
            ok INTEGER, detail TEXT DEFAULT '')""")
        kb._exec("CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, ts)")

    # ---- recipients / groups
    def add_recipient(self, name: str, email: str = "", phone: str = "", groups: str = "", note: str = "") -> int:
        if not (email or phone):
            raise ValueError("an e-mail address or a phone number is required")
        return self.kb._insert("INSERT INTO recipients (name, email, phone, groups, note) VALUES (?,?,?,?,?)", (name.strip(), email.strip().lower(), phone.strip(), groups.strip().lower(), note.strip()))

    def recipients(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM recipients ORDER BY name")]

    def update_recipient(self, rid: int, **f) -> None:
        if f:
            self.kb._exec(f"UPDATE recipients SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", tuple(int(v) if isinstance(v, bool) else v for v in f.values()) + (rid,))

    def delete_recipient(self, rid: int) -> None:
        self.kb._exec("DELETE FROM recipients WHERE id=?", (rid,))

    def add_group(self, name: str, email: str = "", note: str = "") -> int:
        """A group is a label on recipients and optionally a distribution list address of its own (ops-team@corp.com)."""
        return self.kb._insert("INSERT INTO notify_groups (name, email, note) VALUES (?,?,?)", (name.strip().lower(), email.strip().lower(), note.strip()))

    def groups(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM notify_groups ORDER BY name")]

    def delete_group(self, gid: int) -> None:
        self.kb._exec("DELETE FROM notify_groups WHERE id=?", (gid,))

    def resolve(self, targets: str) -> tuple[list[str], list[str]]:
        """'ops, @dba, ali@corp.com, +90555...' -> (emails, phones): names / group labels / literal addresses."""
        emails, phones = [], []
        recs, grps = self.recipients(), {g["name"]: g for g in self.groups()}
        for tok in [x.strip() for x in (targets or "").split(",") if x.strip()]:
            low = tok.lower().lstrip("@")
            if "@" in tok and not tok.startswith("@"):
                emails.append(tok.lower()); continue
            if tok.startswith("+") or tok.replace(" ", "").isdigit():
                phones.append(tok.replace(" ", "")); continue
            members = [r for r in recs if r["enabled"] and low in [g.strip() for g in r["groups"].split(",")]]
            if low in grps or members:                       # a group record, or just a label people carry
                if low in grps and grps[low]["email"]:
                    emails.append(grps[low]["email"])
                for r in members:
                    if r["email"]: emails.append(r["email"])
                    if r["phone"]: phones.append(r["phone"])
                continue
            for r in recs:
                if r["enabled"] and r["name"].lower() == low:
                    if r["email"]: emails.append(r["email"])
                    if r["phone"]: phones.append(r["phone"])
        return sorted(set(emails)), sorted(set(phones))

    # ---- rules
    def add_rule(self, name: str, condition: str, threshold: float = 0, env: str = "", severity: str = "high", channels: str = "email", targets: str = "", cooldown_min: int = 30) -> int:
        if condition not in CONDITIONS:
            raise ValueError("unknown condition")
        return self.kb._insert("INSERT INTO notify_rules (name, condition, threshold, env, severity, channels, targets, cooldown_min) VALUES (?,?,?,?,?,?,?,?)",
                               (name.strip(), condition, float(threshold or 0), env.strip().lower(), severity, channels, targets.strip(), int(cooldown_min)))

    def rules(self) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM notify_rules ORDER BY id")]

    def update_rule(self, rid: int, **f) -> None:
        if f:
            self.kb._exec(f"UPDATE notify_rules SET {', '.join(f'{k}=?' for k in f)} WHERE id=?", tuple(int(v) if isinstance(v, bool) else v for v in f.values()) + (rid,))

    def delete_rule(self, rid: int) -> None:
        self.kb._exec("DELETE FROM notify_rules WHERE id=?", (rid,))

    # ---- dispatch
    def recent(self, key: str, minutes: int) -> bool:
        since = (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
        return bool(self.kb._exec("SELECT 1 FROM alerts WHERE key=? AND ts>=? LIMIT 1", (key, since)))   # failed attempts count too: no retry storm

    def alerts(self, n: int = 50) -> list[dict]:
        return [dict(r) for r in self.kb._exec("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (n,))]

    def dispatch(self, rule: dict, key: str, title: str, body: str, force: bool = False) -> dict:
        """Send one alert for a rule unless the same key fired within the cooldown; returns the log row."""
        if not force and self.recent(key, int(rule.get("cooldown_min") or 30)):
            return {"skipped": "cooldown"}
        emails, phones = self.resolve(rule.get("targets", ""))
        chans = [c.strip() for c in (rule.get("channels") or "email").split(",") if c.strip()]
        cfg = self.channels_fn() or {}
        sent, errors = [], []
        if "email" in chans and emails:
            try:
                if not cfg.get("email", {}).get("host"):
                    raise RuntimeError("SMTP is not configured")
                send_email(cfg["email"], emails, f"[Watchover] {title}", body, _html(title, body, rule.get("severity", "high")))
                sent.append(f"email:{len(emails)}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"email: {type(e).__name__}: {str(e)[:120]}")
        if "sms" in chans and phones:
            for ph in phones:
                try:
                    if not cfg.get("sms", {}).get("url") and not cfg.get("sms", {}).get("preset"):
                        raise RuntimeError("SMS gateway is not configured")
                    send_sms(cfg["sms"], ph, f"Watchover {rule.get('severity', 'high').upper()}: {title} — {body[:200]}")
                    sent.append(f"sms:{ph}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"sms {ph}: {type(e).__name__}: {str(e)[:100]}")
        ok = bool(sent) and not errors
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "rule_id": rule.get("id"), "key": key, "severity": rule.get("severity", "high"), "title": title[:200],
               "body": body[:1000], "channels": ",".join(chans), "recipients": ", ".join(emails + phones)[:500], "ok": int(ok), "detail": "; ".join(errors)[:400] if errors else ", ".join(sent)}
        self.kb._exec("INSERT INTO alerts (ts, rule_id, key, severity, title, body, channels, recipients, ok, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      tuple(row[k] for k in ("ts", "rule_id", "key", "severity", "title", "body", "channels", "recipients", "ok", "detail")))
        return row

    def test_channel(self, channel: str, target: str) -> tuple[bool, str]:
        cfg = self.channels_fn() or {}
        try:
            if channel == "email":
                send_email(cfg.get("email", {}), [target], "[Watchover] test", "Watchover e-mail alerting works.", _html("Test", "Watchover e-mail alerting works.", "low"))
            else:
                return True, send_sms(cfg.get("sms", {}), target, "Watchover SMS alerting works.")
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


def _html(title: str, body: str, severity: str) -> str:
    col = {"critical": "#f87171", "high": "#fb923c", "medium": "#fbbf24", "low": "#60a5fa"}.get(severity, "#60a5fa")
    import html as _h
    return (f'<div style="font-family:-apple-system,Inter,Arial,sans-serif;max-width:640px"><div style="border-left:5px solid {col};padding:10px 14px;background:#f8fafc">'
            f'<div style="font-size:11px;letter-spacing:.08em;color:{col};font-weight:700">{severity.upper()}</div><h2 style="margin:4px 0 8px;font-size:18px;color:#0f172a">{_h.escape(title)}</h2>'
            f'<pre style="white-space:pre-wrap;font-family:inherit;font-size:13px;color:#334155;margin:0">{_h.escape(body)}</pre></div>'
            f'<p style="font-size:11px;color:#94a3b8">Watchover · operations signal desk</p></div>')


# ---------------------------------------------------------------- evaluation against the live feed
class AlertEngine:
    def __init__(self, notifier: Notifier, live, registry=None, sources=None, every: float = 60.0):
        self.n, self.live, self.registry, self.sources, self.every = notifier, live, registry, sources, every
        self.stop = threading.Event()
        self.runs = 0
        self.last_run = ""
        self.last_fired: list[str] = []

    def evaluate(self, now_iso: str | None = None) -> list[dict]:
        from . import scenario
        fired = []
        for rule in self.n.rules():
            if not rule["enabled"]:
                continue
            env = rule["env"] or None
            cond, thr = rule["condition"], float(rule["threshold"] or 0)
            try:
                if cond == "slo":
                    s = self.live.slo(15, env)
                    if s["availability"] is not None and s["total"] >= 20 and s["availability"] < (thr or s["slo"]["availability"]):
                        fired.append(self.n.dispatch(rule, f"slo:{env or 'all'}", f"Availability {s['availability']*100:.2f}% below SLO ({(thr or s['slo']['availability'])*100:.1f}%)" + (f" · {env}" if env else ""),
                                                     f"{s['errors']} ERROR+ of {s['total']} events in the last 15 minutes; error budget left {(s['error_budget'] or 0)*100:.0f}%."))
                elif cond == "errors":
                    st_ = self.live.stats(5, env)
                    per_min = st_["errors"] / 5 if st_.get("errors") is not None else 0
                    if per_min >= (thr or 10):
                        fired.append(self.n.dispatch(rule, f"errors:{env or 'all'}", f"{per_min:.0f} ERROR+ per minute" + (f" · {env}" if env else ""),
                                                     f"{st_['errors']} ERROR+ events in 5 minutes; busiest services: " + ", ".join(f"{a} ({b})" for a, b in list(st_.get("services", []))[:5])))
                elif cond == "metric":
                    ms = self.live.metric_stats(5, env)
                    for b in ms.get("breaches", []):
                        limit = thr or scenario.METRIC_THRESHOLDS.get(b["metric"], 90)
                        if b["value"] >= limit:
                            fired.append(self.n.dispatch(rule, f"metric:{b['host']}:{b['metric']}", f"{b['host']} {b['metric']} {b['value']:.0f}% (limit {limit:.0f}%)", f"Host {b['host']} · {b['metric']} at {b['value']:.0f}% in the last 5 minutes."))
                elif cond == "agent_offline" and self.registry is not None:
                    from datetime import datetime as _dt
                    for a in self.registry.list():
                        if a["status"] != "active" or not a["last_seen"]:
                            continue
                        silent = (datetime.now(UTC) - _dt.fromisoformat(a["last_seen"])).total_seconds() / 60
                        if silent >= (thr or 5):
                            fired.append(self.n.dispatch(rule, f"offline:{a['name']}", f"Agent {a['name']} silent for {silent:.0f} min", f"Last seen {a['last_seen'][:16]} from {a['last_ip'] or '-'} · env {a['env'] or '-'} · site {a['site'] or '-'}."))
                elif cond == "source_failing" and self.sources is not None:
                    for s_ in self.sources.list():
                        if s_.enabled and s_.last_err:
                            fired.append(self.n.dispatch(rule, f"source:{s_.name}", f"Source {s_.name} failing", f"{s_.kind} at {s_.url}: {s_.last_err[:200]}"))
                elif cond == "incident":
                    rows = self.n.kb._exec("SELECT id, title, text, key FROM lessons WHERE kind='pattern' AND updated_at >= ? ORDER BY id DESC LIMIT 5",
                                           ((datetime.now(UTC) - timedelta(minutes=max(int(self.every // 60) * 2, 20))).isoformat(timespec="seconds"),))
                    for r in rows:
                        fired.append(self.n.dispatch(rule, f"incident:{r['key']}", f"Incident pattern: {r['title'][:120]}", r["text"][:600]))
            except Exception as e:  # noqa: BLE001
                fired.append({"error": f"{cond}: {type(e).__name__}: {str(e)[:120]}"})
        self.runs += 1
        self.last_run = now_iso or datetime.now(UTC).isoformat(timespec="seconds")
        self.last_fired = [f.get("title", f.get("skipped", f.get("error", ""))) for f in fired]
        return fired

    def start(self) -> "AlertEngine":
        def loop():
            while not self.stop.wait(self.every):
                try:
                    self.evaluate()
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=loop, daemon=True, name="alert-engine").start()
        return self
