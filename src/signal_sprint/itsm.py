"""ITSM ticket integration: ServiceNow, Jira Service Management, OneDesk or any REST API, plus demo tickets.

Tickets are correlated with what the live page sees (error bursts, metric breaches, incident candidates) by
text overlap (services, hosts, error keywords) and time proximity, producing a 0..1 relevance with reasons.
Standard library only.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .normalize import parse_timestamp

UTC = timezone.utc
_CTX = ssl.create_default_context()
WORD_RE = re.compile(r"[a-zA-Z][\w\-]{2,}")
STOP = {"the", "and", "for", "with", "from", "this", "that", "not", "are", "was", "has", "have", "into", "when", "after", "before",
        "ve", "ile", "için", "bir", "olarak", "sonra", "önce", "hata", "error", "issue", "problem", "service", "servis"}


@dataclass
class Ticket:
    id: str
    title: str
    description: str = ""
    priority: str = ""
    status: str = ""
    created: datetime | None = None
    service: str = ""
    assignee: str = ""
    url: str = ""
    relevance: float = 0.0
    related: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.title} {self.description} {self.service}".lower()


# ---------------------------------------------------------------- fetchers
def _get(url: str, headers: dict, timeout: int = 30) -> dict | list:
    req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read())


def _auth_headers(user: str = "", password: str = "", token: str = "") -> dict:
    if token:
        return {"Authorization": f"Bearer {token}"}
    if user:
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}
    return {}


def fetch_servicenow(base_url: str, user: str = "", password: str = "", token: str = "",
                     query: str = "active=true^ORDERBYDESCsys_created_on", limit: int = 50) -> list[Ticket]:
    """ServiceNow Table API: GET /api/now/table/incident."""
    q = urllib.parse.urlencode({"sysparm_query": query, "sysparm_limit": limit,
                                "sysparm_fields": "number,short_description,description,priority,state,sys_created_on,cmdb_ci,assigned_to,sys_id"})
    doc = _get(f"{base_url.rstrip('/')}/api/now/table/incident?{q}", _auth_headers(user, password, token))
    out = []
    for r in doc.get("result", []):
        ci = r.get("cmdb_ci"); ci = ci.get("display_value", ci.get("value", "")) if isinstance(ci, dict) else (ci or "")
        asg = r.get("assigned_to"); asg = asg.get("display_value", "") if isinstance(asg, dict) else (asg or "")
        out.append(Ticket(r.get("number", r.get("sys_id", "")), r.get("short_description", ""), r.get("description", "") or "",
                          f"P{r['priority']}" if r.get("priority") else "", r.get("state", ""), parse_timestamp(r.get("sys_created_on")),
                          ci, asg, f"{base_url.rstrip('/')}/nav_to.do?uri=incident.do?sys_id={r.get('sys_id', '')}"))
    return out


def fetch_jira(base_url: str, user: str = "", password: str = "", token: str = "", query: str = "statusCategory != Done ORDER BY created DESC",
               limit: int = 50) -> list[Ticket]:
    """Jira / Jira Service Management: GET /rest/api/3/search?jql=..."""
    q = urllib.parse.urlencode({"jql": query, "maxResults": limit, "fields": "summary,description,priority,status,created,assignee,components"})
    doc = _get(f"{base_url.rstrip('/')}/rest/api/3/search?{q}", _auth_headers(user, password, token))
    out = []
    for i in doc.get("issues", []):
        f = i.get("fields", {})
        desc = f.get("description")
        if isinstance(desc, dict):  # ADF document -> plain text
            desc = " ".join(t.get("text", "") for blk in desc.get("content", []) for t in blk.get("content", []) if isinstance(t, dict))
        comps = ", ".join(c.get("name", "") for c in f.get("components", []) or [])
        out.append(Ticket(i.get("key", ""), f.get("summary", ""), desc or "", (f.get("priority") or {}).get("name", ""),
                          (f.get("status") or {}).get("name", ""), parse_timestamp(f.get("created")), comps,
                          (f.get("assignee") or {}).get("displayName", ""), f"{base_url.rstrip('/')}/browse/{i.get('key', '')}"))
    return out


def fetch_onedesk(base_url: str, user: str = "", password: str = "", token: str = "", query: str = "", limit: int = 50) -> list[Ticket]:
    """OneDesk REST (tickets endpoint); falls back to the generic mapper on the returned list."""
    doc = _get(f"{base_url.rstrip('/')}/tickets?{query}" if query else f"{base_url.rstrip('/')}/tickets", _auth_headers(user, password, token))
    return fetch_generic_from_doc(doc, {"id": "id", "title": "name", "description": "description", "priority": "priority",
                                        "status": "lifecycleStatus", "created": "creationDate", "service": "project", "assignee": "assignee"})[:limit]


def fetch_generic(url: str, headers: dict | None = None, mapping: dict | None = None, path: str = "") -> list[Ticket]:
    """Any REST endpoint returning a JSON list (or a document with a list under `path`), mapped by field names."""
    doc = _get(url, headers or {})
    from .connectors import dig
    if path:
        doc = dig(doc, path)
    return fetch_generic_from_doc(doc, mapping or {})


def fetch_generic_from_doc(doc, mapping: dict) -> list[Ticket]:
    from .parsers.json_parser import find_records
    rows = doc if isinstance(doc, list) else (find_records(doc) or [])
    m = {"id": "id", "title": "title", "description": "description", "priority": "priority", "status": "status", "created": "created",
         "service": "service", "assignee": "assignee", **mapping}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        g = lambda k: r.get(m[k], "")  # noqa: E731
        out.append(Ticket(str(g("id")), str(g("title") or r.get("summary") or r.get("name") or ""), str(g("description") or ""), str(g("priority") or ""),
                          str(g("status") or ""), parse_timestamp(g("created")), str(g("service") or ""), str(g("assignee") or "")))
    return out


SYSTEMS = {"servicenow": fetch_servicenow, "jira": fetch_jira, "onedesk": fetch_onedesk}


def demo_tickets(now: datetime | None = None) -> list[Ticket]:
    """Plausible open tickets for demos; a few line up with the simulator's incident bursts."""
    now = now or datetime.now(UTC)
    mk = lambda i, t, d, p, st, mins, svc, who: Ticket(i, t, d, p, st, now - timedelta(minutes=mins), svc, who)  # noqa: E731
    return [
        mk("INC0012345", "Payment API timeouts, customers cannot check out", "Connection timeout to db-01 reported by payment-api since 14:32", "P1", "In Progress", 6, "payment-api", "SRE on-call"),
        mk("INC0012346", "db-01 CPU saturated", "CPU above 90% on db-01, slow queries on orders table", "P2", "New", 9, "db-01", ""),
        mk("INC0012340", "Search results slow for some users", "search-api p95 latency 800ms in EU region", "P3", "In Progress", 140, "search-api", "Search team"),
        mk("INC0012331", "Disk usage warning on worker-01", "/var at 88%, log rotation not running", "P3", "New", 260, "worker-01", ""),
        mk("REQ0004410", "New VPN access for contractor", "Onboarding request", "P4", "Open", 600, "", "IT desk"),
        mk("INC0012299", "Email notifications delayed", "notification-service kafka lag growing", "P2", "On Hold", 1500, "notification-service", "Platform"),
    ]


# ---------------------------------------------------------------- correlation
def _tokens(text: str) -> set[str]:
    return {w.lower() for w in WORD_RE.findall(text) if w.lower() not in STOP}


def correlate(tickets: list[Ticket], signals: list[dict], breaches: list[dict], incidents: list[dict] | None = None,
              window_min: int = 30) -> list[Ticket]:
    """Score each ticket against live evidence.

    signals:   [{"id","template","services","hosts","severity","onset","count"}]  (error/bursting ones)
    breaches:  [{"metric","host","value","threshold"}]
    incidents: [{"id","title","services","hosts","started_at"}]
    """
    now = datetime.now(UTC)
    for tk in tickets:
        toks = _tokens(tk.text)
        best_text, related, reasons = 0.0, [], []
        for sg in signals:
            ents = {x.lower() for x in (sg.get("services", []) + sg.get("hosts", []))}
            ent_hit = toks & ents
            kw_hit = toks & _tokens(sg.get("template", "")) - {"path", "ip", "uuid", "hex"}
            score = min(1.0, 0.6 * bool(ent_hit) + 0.1 * len(kw_hit))
            if score > 0.15:
                best_text = max(best_text, score)
                related.append(sg["id"])
                reasons.append(f"{sg['id']}: " + ", ".join(sorted(ent_hit | set(list(kw_hit)[:3]))))
        for b in breaches:
            if b["host"].lower() in toks or b["metric"] in toks:
                best_text = max(best_text, 0.7)
                related.append(f"{b['metric']}@{b['host']}")
                reasons.append(f"{b['metric']} {b['value']:.0f}% on {b['host']} (≥{b['threshold']})")
        for inc in incidents or []:
            ents = {x.lower() for x in (inc.get("services", []) + inc.get("hosts", []))}
            if toks & ents:
                best_text = max(best_text, 0.8)
                related.append(inc["id"])
                reasons.append(f"{inc['id']}: " + ", ".join(sorted(toks & ents)))
        time_score = 0.0
        if tk.created:
            age = (now - tk.created).total_seconds() / 60
            onsets = [sg.get("onset") for sg in signals if sg.get("onset")] + [inc.get("started_at") for inc in incidents or [] if inc.get("started_at")]
            near = [o for o in onsets if abs((tk.created - o).total_seconds()) / 60 <= window_min]
            if near:
                time_score = 1.0
                reasons.append(f"opened within {window_min} min of {len(near)} burst(s)")
            elif age <= window_min:
                time_score = 0.5
        tk.relevance = round(min(1.0, 0.65 * best_text + 0.35 * time_score), 2)
        tk.related = sorted(set(related))
        tk.reasons = reasons[:4]
    tickets.sort(key=lambda x: (-x.relevance, -(x.created or now).timestamp()))  # newest first on ties
    return tickets
