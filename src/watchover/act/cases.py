"""CaseRouter: triggers -> cases (docs/design/action-agent.md §4.1, §4.2, §4.5, §4.6).

A case is an `actions` row with kind='case'. One trigger either opens a case, joins an open one (merge: same key, or same family
on the same host within act_group_min), reopens a recently closed one (the run that closed it gets `relapse`), folds into a
storm parent (>= 5 cases in 10 min for one pool / service) or a flood case (> act_cases_per_min new cases per source and team).
Every transition and its audit row commit in one Database.tx()."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .. import privacy
from .types import (OPEN_PHASES, PHASE_STATUS, PRIORITY_OF, Trigger, env_tier, now, sha256_hex, team_key)

CLOSED = ("resolved", "auto_resolved", "dismissed")
LIVE = tuple(p for p in OPEN_PHASES) + ("escalated",)
SEV_SCORE = {"critical": 100, "high": 60, "medium": 30, "low": 10, "info": 10}
P_SEV = {"P1": "critical", "P2": "high", "P3": "medium", "P4": "low"}
# resource hints: different wordings of one problem join one family (§4.2); the mount itself comes from a diagnose step (C8)
HINTS = ((re.compile(r"(?i)no space left on device|disk (is )?full|ENOSPC|filesystem full|disk quota exceeded"), "metric:disk"),
         (re.compile(r"(?i)OutOfMemoryError|cannot allocate memory|out of memory|oom-killer"), "metric:memory"))
EVIDENCE_FIRST, EVIDENCE_NEWEST = 5, 20
STORM_N, STORM_MIN = 5, 10


def _ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _stem(host: str) -> str:
    h = (host or "").split(".")[0]
    return re.sub(r"[-_]?\d+$", "", h) or h


def resource_hint(*texts) -> str:
    blob = " ".join(str(t) for t in texts if t)
    for rx, fam in HINTS:
        if rx.search(blob):
            return fam
    return ""


def family_of(trig: Trigger) -> str:
    if trig.family:
        return trig.family
    f = trig.facts or {}
    if trig.source in ("threshold", "anomaly") and f.get("metric"):
        return f"metric:{f['metric']}"
    hint = resource_hint(trig.title, f.get("query", ""), " ".join(f.get("samples") or []))
    if hint:
        return hint
    if trig.source == "query":
        return f"logq:{f.get('rule_id', '')}"
    if trig.source == "incident":
        return f"tpl:{hashlib.sha1(str(f.get('root_template', '')).encode()).hexdigest()[:12]}"
    if trig.source == "bizflow":
        return f"flow:{f.get('flow_id', '')}:{f.get('step', '')}"
    if trig.source == "anomaly":
        return f"anomaly:{f.get('kind', '')}:{trig.service or ''}"
    return f"{trig.source}:{trig.key}"


def inbox_score(case: dict, me: str, my_teams, now_dt: Optional[datetime] = None) -> int:
    """priority = sev + needs_me + waiting + age − handled (§4.5)."""
    now_dt = now_dt or datetime.now(timezone.utc)
    sev = case.get("severity") or P_SEV.get(case.get("priority") or "", "medium")
    score = SEV_SCORE.get(sev, 30)
    owner = (case.get("owner") or "").strip().lower()
    if case.get("approval_for_me"):
        score += 80
    elif owner and owner == (me or "").lower():
        score += 50
    elif not owner and (case.get("team_id") in set(my_teams or ()) or case.get("team_id") is None):
        score += 20
    if case.get("phase") == "waiting_human":
        score += 30
    try:
        first = datetime.fromisoformat(str(case.get("first_seen") or case.get("created_at") or "").replace("Z", "+00:00"))
        score += int(min(30, max(0.0, (now_dt - first).total_seconds() / 60) / 2))
    except ValueError:
        pass
    if case.get("handled"):
        score -= 100
    return int(score)


class CaseRouter:
    def __init__(self, act):
        self.act = act
        self.db = act.db
        self.svc = act.svc
        self._owners: Dict[str, List[int]] = {}
        self._opened: Dict[tuple, deque] = {}

    # ---------------------------------------------------------------- context
    def flush(self) -> None:
        """End of a batch: teams.owners() is cached per batch only (it recomputes every team's host set per call)."""
        self._owners = {}

    def owners(self, host: str) -> List[int]:
        if not host or self.svc is None or getattr(self.svc, "teams", None) is None:
            return []
        if host not in self._owners:
            try:
                self._owners[host] = list(self.svc.teams.owners(host))
            except Exception:  # noqa: BLE001
                self._owners[host] = []
        return self._owners[host]

    def _inventory(self, host: str) -> dict:
        try:
            return (self.svc.inventory.resolve(host) or {}) if (self.svc is not None and host) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _pool(self, host: str) -> str:
        try:
            p = self.svc._pool_of(host) if self.svc is not None else None
        except Exception:  # noqa: BLE001
            p = None
        return p or _stem(host)

    # ---------------------------------------------------------------- trigger builders (hooks)
    def trigger_from_anomaly(self, a: Dict[str, Any]) -> Optional[Trigger]:
        if not a or a.get("status") == "learning":
            return None
        key = str(a.get("key") or "")
        if not key:
            return None
        detail = str(a.get("detail") or "")
        score = float(a.get("score") or 0)
        if key.startswith("threshold:"):
            src = "threshold"
            sev = "critical" if ("critical" in detail or score >= 5) else "high"
        else:
            src = "anomaly"
            sev = "high" if score >= 5 else "medium" if score >= 3.5 else "low"
        facts = {"kind": a.get("kind", ""), "metric": a.get("metric", ""), "value": a.get("observed"), "limit": a.get("baseline"),
                 "score": score, "anomaly_id": a.get("id")}
        host = str(a.get("host") or "")
        ev = []
        if host:                                                   # trusted only through the sender's agent_id on the host's metric batch (§4.3)
            live = getattr(self.svc, "live", None)
            aid = (getattr(live, "metric_agent", None) or {}).get(host) if live is not None else None
            ev.append({"kind": "metric", "host": host, "agent_id": aid, "ts": a.get("first_seen") or None,
                       "fields": {k: facts[k] for k in ("metric", "value", "limit") if facts.get(k) not in (None, "")}})
        return Trigger(source=src, key=key, host=host, env=str(a.get("env") or ""), service=str(a.get("service") or ""),
                       severity=sev, title=str(a.get("title") or key)[:200], facts={k: v for k, v in facts.items() if v not in (None, "")},
                       refs=[{"type": "anomaly", "id": a.get("id")}], ts=a.get("first_seen") or None, evidence=ev)

    def triggers_from_query(self, rule: Dict[str, Any], c: Dict[str, Any]) -> List[Trigger]:
        """Log-query alert (rule with open_case): one case per host for one or two hosts; LogWatch.count keeps only the top 3
        hosts, so three returned hosts mean "many" and key the case query:{rule}:* (§4.1)."""
        hosts = [h for h, _n in (c.get("hosts") or [])]
        samples = [privacy.scrub_strict(s)[:400] for s in (c.get("samples") or [])[:5]]
        facts = {"rule_id": rule.get("id"), "query": privacy.scrub_strict(rule.get("query") or "")[:400], "count": c.get("n"),
                 "window": rule.get("window_min"), "op": rule.get("op") or ">=", "threshold": rule.get("threshold"), "samples": samples}
        base = dict(source="query", severity=str(rule.get("severity") or "high"), team_id=rule.get("team_id"), facts=facts,
                    refs=[{"type": "notify_rule", "id": rule.get("id")}])
        name = str(rule.get("name") or f"#{rule.get('id')}")
        items = list(c.get("items") or [])[:5]                     # sample lines as evidence: the case is keyed to the real sender
        if len(hosts) >= 3 or not hosts:
            return [Trigger(key=f"query:{rule.get('id')}:*", host="", title=f"{name}: {c.get('n')} satır ({', '.join(hosts)}…)"[:200],
                            evidence=items, **base)]
        return [Trigger(key=f"query:{rule.get('id')}:{h}", host=h, title=f"{name}: {h}"[:200],
                        evidence=[i for i in items if (i.get("host") or "") == h], **base) for h in hosts]

    def triggers_from_analysis(self, a) -> List[Trigger]:
        out = []
        for inc in getattr(a, "incidents", []) or []:
            if inc.severity not in ("critical", "high"):
                continue
            root = a.signal_by_id.get(inc.root_cause_signal) if hasattr(a, "signal_by_id") else None
            if root is None or str(getattr(root, "severity", "")).lower() in ("debug", "info", "trace"):
                continue                                            # the root cause is noise: no case
            where = (inc.affected_hosts or [""])[0]
            pool = self._pool(where) if where else ""
            tpl = getattr(root, "template", "") or ""
            out.append(Trigger(source="incident", key=f"incident:{hashlib.sha1(tpl.encode()).hexdigest()[:12]}:{pool or where}",
                               host=where, severity=inc.severity, service=(inc.affected_services or [""])[0],
                               title=privacy.scrub_strict(inc.title)[:200],
                               facts={"root_template": privacy.scrub_strict(tpl)[:300], "signals": list(inc.signal_ids)[:10],
                                      "recovery": (inc.recovery or {}).get("kind", ""), "incident_id": inc.id,
                                      "alternatives": [x.get("signal") for x in (inc.root_cause_alternatives or [])][:3]},
                               refs=[{"type": "incident", "id": inc.id}],
                               evidence=[{"kind": "incident", "host": where, "text": getattr(root, "template", "") or inc.title,
                                          "fields": {"service": (inc.affected_services or [""])[0]}}]))
        return out

    def triggers_from_bizflow(self, stuck: List[Dict[str, Any]]) -> List[Trigger]:
        out = []
        for s in stuck or []:
            hosts = list(s.get("hosts") or [])
            out.append(Trigger(source="bizflow", key=f"bizflow:{s['flow_id']}:{s['step']}", host=hosts[0] if len(hosts) == 1 else "",
                               severity="high", team_id=s.get("team_id"),
                               title=f"İş akışı takıldı: {s.get('flow', '')} › {s.get('step_name', s['step'])} · {s.get('stuck', 0)} kayıt"[:200],
                               facts={"flow_id": s["flow_id"], "step": s["step"], "stuck": s.get("stuck", 0), "oldest_s": s.get("oldest_s"),
                                      "hosts": hosts[:5], "samples": list(s.get("samples") or [])[:3]},
                               refs=[{"type": "biz_flow", "id": s["flow_id"]}],
                               evidence=[{"kind": "bizflow", "host": hosts[0] if len(hosts) == 1 else "",
                                          "fields": {"flow_id": s["flow_id"], "step": s["step"], "stuck": s.get("stuck", 0)}}]))
        return out

    # ---------------------------------------------------------------- routing
    def route(self, trig: Trigger) -> Dict[str, Any]:
        if isinstance(trig, dict):
            trig = Trigger(**trig)
        pol = self.act.policy
        host = trig.host or ""
        inv = self._inventory(host)
        env = trig.env or str(inv.get("env") or "")
        team = trig.team_id
        if team is None:
            own = self.owners(host)
            team = own[0] if len(own) == 1 else None
        fam = family_of(trig)
        pool = self._pool(host) if host else ""
        ts = trig.ts or now()
        prio = PRIORITY_OF.get(trig.severity, "P3")
        nowdt = datetime.now(timezone.utc)
        group_min = int(pol.get("act_group_min", 30) or 30)
        reopen_min = int(pol.get("act_reopen_min", 60) or 60)
        db = self.db

        def find_open():
            rows = db._exec("SELECT * FROM actions WHERE kind='case' AND case_key=? AND phase IN (%s) ORDER BY id DESC LIMIT 1"
                            % ",".join("?" * len(LIVE)), (trig.key,) + LIVE)
            if rows:
                return rows[0]
            if host and fam:
                since = _ts(nowdt - timedelta(minutes=group_min))
                rows = db._exec("SELECT * FROM actions WHERE kind='case' AND host=? AND family=? AND last_seen>=? AND phase IN (%s) "
                                "ORDER BY id DESC LIMIT 1" % ",".join("?" * len(LIVE)), (host, fam, since) + LIVE)
                if rows:
                    return rows[0]
            return None

        with db.tx():
            # suppressed ("Gürültü, bastır") cases absorb their key until suppressed_until
            sup = db._exec("SELECT * FROM actions WHERE kind='case' AND case_key=? AND phase='suppressed' AND suppressed_until>? "
                           "ORDER BY id DESC LIMIT 1", (trig.key, now()))
            if sup:
                self._absorb(sup[0], trig, prio, notify=False)
                return {"case_id": sup[0]["id"], "action": "suppressed"}
            cur = find_open()
            if cur is not None:
                self._absorb(cur, trig, prio)
                return {"case_id": cur["id"], "action": "merged"}
            since = _ts(nowdt - timedelta(minutes=reopen_min))
            closed = db._exec("SELECT * FROM actions WHERE kind='case' AND case_key=? AND phase IN ('resolved','auto_resolved','dismissed') "
                              "AND closed_at>=? ORDER BY id DESC LIMIT 1", (trig.key, since))
            if closed:
                return self._reopen(closed[0], trig, prio)
            # flood: more than act_cases_per_min new cases per source and team in a minute fold into one "Sinyal seli" case
            fk = (trig.source, team_key(team))
            q = self._opened.setdefault(fk, deque())
            t = time.monotonic()
            while q and t - q[0] > 60:
                q.popleft()
            if len(q) >= int(pol.get("act_cases_per_min", 20) or 20):
                fkey = f"flood:{trig.source}:{team_key(team)}"
                fl = db._exec("SELECT * FROM actions WHERE kind='case' AND case_key=? AND phase IN (%s) ORDER BY id DESC LIMIT 1"
                              % ",".join("?" * len(LIVE)), (fkey,) + LIVE)
                if fl:
                    self._absorb(fl[0], trig, prio)
                    return {"case_id": fl[0]["id"], "action": "flood"}
                cid = self._open(Trigger(source=trig.source, key=fkey, severity=trig.severity, team_id=team,
                                         title=f"Sinyal seli: {trig.source} · {len(q) + 1}+ vaka/dk", facts={"source": trig.source}),
                                 env="", team=team, fam=f"flood:{trig.source}", pool="", inv={}, prio=prio)
                self._add_trigger(cid, trig)
                return {"case_id": cid, "action": "flood"}
            q.append(t)
            prev = db._exec("SELECT id FROM actions WHERE kind='case' AND case_key=? ORDER BY id DESC LIMIT 1", (trig.key,))
            cid = self._open(trig, env=env, team=team, fam=fam, pool=pool, inv=inv, prio=prio, prev_id=prev[0]["id"] if prev else None)
            self._loop_guard(cid, trig, host, team)
            storm = self._storm(cid, pool, trig.service, team)
        res = {"case_id": cid, "action": "opened"}
        if storm:
            res["storm_parent"] = storm
        return res

    # ---------------------------------------------------------------- pieces (all run inside the router's transaction)
    def _open(self, trig: Trigger, env: str, team: Optional[int], fam: str, pool: str, inv: dict, prio: str,
              prev_id: Optional[int] = None) -> int:
        ts = trig.ts or now()
        title = privacy.scrub_strict(trig.title or trig.key)[:200]
        sig = sha256_hex("|".join([trig.source, fam, pool, env_tier(env, tuple(self.act.policy.get("act_prod_envs") or ()))]))[:16]
        cid = self.db._insert(
            "INSERT INTO actions (incident_id, title, priority, status, owner, recommendation, evidence, created_at, updated_at, team_id, host, "
            "kind, case_key, family, signature, source, source_ref, env, env_tier, pool, host_class, service, severity, phase, hits, "
            "first_seen, last_seen, needs_human, prev_id, provenance) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str((trig.facts or {}).get("incident_id") or ""), title, prio, PHASE_STATUS["ready"], "", "", f"case:{trig.key}"[:300], now(),
             now(), team, trig.host or "", "case", trig.key, fam, sig, trig.source, json.dumps(trig.refs or [], ensure_ascii=False),
             env, env_tier(env, tuple(self.act.policy.get("act_prod_envs") or ())) if env else "", pool, pool, trig.service or "",
             trig.severity, "ready", 1, ts, ts, 1, prev_id, trig.provenance or ""))
        self._add_trigger(cid, trig)
        if trig.host:
            self.act.evidence.add_host_facts(cid, trig.host, inv, env)
        self.act.audit.write("case.opened", object_kind="case", object_id=cid, team_id=team,
                             payload={"key": trig.key, "source": trig.source, "family": fam, "severity": trig.severity, "phase": "ready",
                                      "prev_id": prev_id, "diagnosis": "none"})
        self._seal(cid)
        return cid

    def _seal(self, cid: int) -> None:
        """WP7 (C26): the frozen input is sealed in the transaction that makes the case ready, before any inbox shows it."""
        from .audit import AuditError
        try:
            self.act.shadow.seal(cid)
        except AuditError:
            raise
        except Exception as e:  # noqa: BLE001 - a matcher bug never loses the case; decide() seals late and says so
            self.act.stats["seal_error"] = str(e)[:200]
            return
        sel = getattr(self.act, "selector", None)
        if sel is not None:                                # WP13: the model answers later on the frozen input (never waited for)
            try:
                sel.submit(cid)
            except Exception:  # noqa: BLE001
                pass

    def _add_trigger(self, cid: int, trig: Trigger) -> None:
        self.db._exec("INSERT INTO act_triggers (case_id, ts, source, key, host, severity, provenance, facts, refs) VALUES (?,?,?,?,?,?,?,?,?)",
                      (cid, trig.ts or now(), trig.source, trig.key, trig.host or "", trig.severity, trig.provenance or "",
                       json.dumps(privacy.scrub_fields(trig.facts or {}), ensure_ascii=False, default=str),
                       json.dumps(trig.refs or [], ensure_ascii=False, default=str)))
        for ev in trig.evidence or []:
            self._add_evidence(cid, ev, None)

    def _add_evidence(self, cid: int, ev: Dict[str, Any], target: Optional[str] = None) -> None:
        """Through act/evidence.py (WP6b: provenance by agent_id, host.reported → claimed, slots, quarantine V2, scrub_strict,
        privacy: raw excluded); keep the first 5 and the newest 20 items with their slots (§4.2, §4.3)."""
        if target is None:
            row = self.db._exec("SELECT host FROM actions WHERE id=?", (cid,))
            target = (row[0]["host"] or "") if row else ""
        self.act.evidence.add(cid, ev, target)
        rows = self.db._exec("SELECT id, eid FROM act_evidence WHERE case_id=? AND kind<>'slot' ORDER BY id", (cid,))
        if len(rows) > EVIDENCE_FIRST + EVIDENCE_NEWEST:
            drop = rows[EVIDENCE_FIRST:len(rows) - EVIDENCE_NEWEST]
            self.db._exec("DELETE FROM act_evidence WHERE id IN (%s)" % ",".join(str(int(r["id"])) for r in drop))
            self.db._exec("DELETE FROM act_evidence WHERE case_id=? AND kind='slot' AND source_eid IN (%s)" % ",".join("?" * len(drop)),
                          (cid,) + tuple(r["eid"] for r in drop))

    def _absorb(self, case: dict, trig: Trigger, prio: str, notify: bool = True) -> None:
        best = min(case.get("priority") or "P4", prio)                  # priority may rise, never fall
        sev = case.get("severity") or trig.severity
        if SEV_SCORE.get(trig.severity, 0) > SEV_SCORE.get(sev, 0):
            sev = trig.severity
        self.db._exec("UPDATE actions SET hits=COALESCE(hits,0)+1, last_seen=?, priority=?, severity=?, updated_at=? WHERE id=?",
                      (now(), best, sev, now(), case["id"]))
        self._add_trigger(case["id"], trig)
        self.act.audit.write("trigger.merged", object_kind="case", object_id=case["id"], team_id=case.get("team_id"),
                             payload={"key": trig.key, "source": trig.source, "hits": int(case.get("hits") or 0) + 1})

    def _reopen(self, case: dict, trig: Trigger, prio: str) -> Dict[str, Any]:
        cid = case["id"]
        self.db._exec("UPDATE actions SET phase='ready', status=?, reopened=COALESCE(reopened,0)+1, hits=COALESCE(hits,0)+1, last_seen=?, "
                      "closed_at='', label_final=0, priority=?, needs_human=1, updated_at=? WHERE id=?",
                      (PHASE_STATUS["ready"], now(), min(case.get("priority") or "P4", prio), now(), cid))
        self._add_trigger(cid, trig)
        relapse = None
        if case.get("run_id"):                                            # the run that closed it receives the outcome relapse
            relapse = int(case["run_id"])
            self.db._exec("INSERT INTO act_trust_events (ts, component, cell_key, unit_key, runbook_key, step_key, team_id, env, host_class, host, "
                          "run_id, case_id, outcome, credit, penalty, level) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (now(), "exec", "", "", case.get("runbook_key") or "", "", case.get("team_id"), case.get("env") or "",
                           case.get("host_class") or "", case.get("host") or "", relapse, cid, "relapse", 0, 0.5, case.get("level_hint")))
        self.act.audit.write("case.reopened", object_kind="case", object_id=cid, team_id=case.get("team_id"),
                             payload={"key": trig.key, "reopened": int(case.get("reopened") or 0) + 1, "relapse_run": relapse})
        self._seal(cid)                                                   # a new attempt: its own frozen input and decision (C10)
        self._loop_guard(cid, trig, case.get("host") or "", case.get("team_id"))
        return {"case_id": cid, "action": "reopened", "relapse_run": relapse}

    def _loop_guard(self, cid: int, trig: Trigger, host: str, team: Optional[int]) -> None:
        """The third time within 24 h on the same host (runs of one runbook once runs exist, else cases of one key): never
        started by the system; needs a human, a banner, and one follow-up action "Kalıcı çözüm" for the team (§4.2)."""
        if not host:
            return
        since = _ts(datetime.now(timezone.utc) - timedelta(hours=24))
        r = self.db._exec("SELECT COUNT(*) AS n FROM actions WHERE kind='case' AND host=? AND case_key=? AND first_seen>=?",
                          (host, trig.key, since))[0]
        re_n = self.db._exec("SELECT COALESCE(SUM(reopened), 0) AS n FROM actions WHERE kind='case' AND host=? AND case_key=? AND last_seen>=?",
                             (host, trig.key, since))[0]
        n = int(r["n"] or 0) + int(re_n["n"] or 0)
        if n < 3:
            return
        self.db._exec("UPDATE actions SET needs_human=1, next_step=? WHERE id=?", ("Tekrarlıyor: otomatik çözüm kalıcı değil", cid))
        tag = f"loop:{host}:{trig.key}"[:300]
        week = _ts(datetime.now(timezone.utc) - timedelta(days=7))
        if not self.db._exec("SELECT 1 AS x FROM actions WHERE kind='action' AND evidence=? AND created_at>=?", (tag, week)):
            aid = self.db._insert("INSERT INTO actions (incident_id, title, priority, status, owner, recommendation, evidence, created_at, updated_at, "
                                  "team_id, host, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (f"case:{cid}", f"Kalıcı çözüm: {privacy.scrub_strict(trig.title or trig.key)[:150]}", "P2", "open", "",
                                   "Aynı sorun 24 saatte 3. kez açıldı; kök nedeni kalıcı olarak giderin.", tag, now(), now(), team, host, "action"))
            self.act.audit.write("case.loop_guard", object_kind="case", object_id=cid, team_id=team,
                                 payload={"key": trig.key, "host": host, "count_24h": n, "follow_up": aid})

    def _storm(self, cid: int, pool: str, service: str, team: Optional[int]) -> Optional[int]:
        """>= 5 cases within 10 min in one pool or for one service fold under a parent "Fırtına" case; children are capped at L2."""
        since = _ts(datetime.now(timezone.utc) - timedelta(minutes=STORM_MIN))
        for col, val, label in (("pool", pool, "havuzu"), ("service", service, "servisi")):
            if not val:
                continue
            rows = self.db._exec(f"SELECT id FROM actions WHERE kind='case' AND {col}=? AND first_seen>=? AND storm=0 AND parent_id IS NULL "
                                 f"AND case_key NOT LIKE 'storm:%%' AND phase IN (%s)" % ",".join("?" * len(LIVE)), (val, since) + LIVE)
            kids = [r["id"] for r in rows]
            parent = self.db._exec("SELECT * FROM actions WHERE kind='case' AND case_key=? AND phase IN (%s) ORDER BY id DESC LIMIT 1"
                                   % ",".join("?" * len(LIVE)), (f"storm:{col}:{val}",) + LIVE)
            if not parent and len(kids) < STORM_N:
                continue
            if parent:
                pid = parent[0]["id"]
            else:
                pid = self._open(Trigger(source="incident" if col == "service" else "anomaly", key=f"storm:{col}:{val}", severity="high", team_id=team,
                                         title=f"Fırtına: {val} {label}"), env="", team=team, fam=f"storm:{col}", pool=pool if col == "pool" else "",
                                 inv={}, prio="P2")
                self.db._exec("UPDATE actions SET storm=1 WHERE id=?", (pid,))
            if kids:
                self.db._exec("UPDATE actions SET parent_id=?, storm=1 WHERE id IN (%s)" % ",".join(str(int(k)) for k in kids), (pid,))
            total = self.db._exec("SELECT COUNT(*) AS n FROM actions WHERE parent_id=?", (pid,))[0]["n"]
            self.db._exec("UPDATE actions SET title=?, hits=?, last_seen=?, updated_at=? WHERE id=?",
                          (f"Fırtına: {val} {label} · {total} vaka", total, now(), now(), pid))
            self.act.audit.write("case.storm", object_kind="case", object_id=pid, team_id=team, payload={"children": kids, "total": total, col: val})
            return pid
        return None

    # ---------------------------------------------------------------- housekeeping transitions (act.case; audited)
    def transition(self, cid: int, phase: str, by: str, **fields) -> dict:
        if phase not in PHASE_STATUS:
            raise ValueError(f"unknown phase {phase}")
        rows = self.db._exec("SELECT * FROM actions WHERE id=? AND kind='case'", (cid,))
        if not rows:
            raise KeyError(cid)
        case = rows[0]
        sets = {"phase": phase, "status": PHASE_STATUS[phase], "updated_at": now()}
        if phase in CLOSED or phase in ("escalated", "suppressed"):
            sets["closed_at"] = now()
        if phase in ("ready", "running"):
            sets["closed_at"] = ""
        allowed = ("owner", "resolution", "outcome", "suppressed_until", "needs_human", "label")
        sets.update({k: v for k, v in fields.items() if k in allowed})
        if "resolution" in sets:
            sets["resolution"] = privacy.scrub_strict(str(sets["resolution"]))[:2000]
        again = phase == "ready" and case.get("phase") in CLOSED + ("escalated", "suppressed")
        if again:
            sets["reopened"] = int(case.get("reopened") or 0) + 1         # a reopened case is a new attempt (C10)
        with self.db.tx():
            self.db._exec(f"UPDATE actions SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), cid))
            self.act.audit.write(f"case.{phase}", object_kind="case", object_id=cid, actor=by, actor_kind="user", team_id=case.get("team_id"),
                                 payload={"from": case.get("phase"), "to": phase, **{k: v for k, v in sets.items() if k not in ("updated_at",)}})
            if again:
                self._seal(cid)
        return self.get(cid)

    def assign(self, cid: int, owner: str, by: str) -> dict:
        rows = self.db._exec("SELECT phase FROM actions WHERE id=? AND kind='case'", (cid,))
        if not rows:
            raise KeyError(cid)
        return self.transition(cid, rows[0]["phase"], by, owner=(owner or "").strip().lower())

    def get(self, cid: int) -> dict:
        rows = self.db._exec("SELECT * FROM actions WHERE id=? AND kind='case'", (cid,))
        if not rows:
            raise KeyError(cid)
        return rows[0]

    def detail(self, cid: int) -> dict:
        c = dict(self.get(cid))
        c["triggers"] = self.db._exec("SELECT ts, source, key, host, severity, provenance, facts, refs FROM act_triggers WHERE case_id=? ORDER BY id",
                                      (cid,))
        for t in c["triggers"]:
            for k in ("facts", "refs"):
                try:
                    t[k] = json.loads(t[k] or "null")
                except ValueError:
                    pass
        c["evidence"] = self.act.evidence.items(cid)
        c["slots"] = self.act.evidence.slots(cid)
        c["evidence_cap"] = self.act.evidence.cap(cid)
        sh = self.act.shadow
        hidden = sh.hidden(c)
        if hidden:                                                        # C51: attribution-free pre-diagnosis while blind
            for e in c["evidence"]:
                e["ref"] = {k: v for k, v in (e.get("ref") or {}).items() if k not in ("run_id", "step_id", "step_key", "job_id")}
                e["attribution"] = None
            c["runbook_key"] = ""
        c["proposal"] = sh.view(c)
        c["candidates"] = sh.candidates_view(c)
        c["similar"] = sh.similar(c)
        c["attempt"] = sh.attempt_of(c)
        d = sh.decision(cid, c["attempt"])
        c["decision"] = {k: d[k] for k in ("choice", "attempt", "blind", "peeked", "agree_rules", "agree_model", "reason_code", "decided_at")} \
            if d else None
        c["children"] = self.db._exec("SELECT id, title, host, phase, severity FROM actions WHERE parent_id=? ORDER BY id", (cid,))
        c["runs"] = self.db._exec("SELECT id, status, outcome, started_at, ended_at FROM act_runs WHERE case_id=? ORDER BY id", (cid,))
        c["timeline"] = [{"seq": e["seq"], "ts": e["ts"], "event": e["event"], "actor": e["actor"], "payload": e["payload"]}
                         for e in self.act.audit.events(object_kind="case", object_id=str(cid), limit=100)]
        if c.get("prev_id"):
            prev = self.db._exec("SELECT COUNT(*) AS n, MAX(closed_at) AS last FROM actions WHERE kind='case' AND case_key=? AND id<?",
                                 (c["case_key"], cid))[0]
            c["history"] = {"previous": int(prev["n"] or 0), "last_closed": prev["last"] or ""}
        c["subject"] = {"host": c.get("host"), "family": c.get("family")}
        return c

    def inbox(self, me: str, my_teams, visible, tab: str = "team", q: str = "", limit: int = 300) -> List[dict]:
        """Ordered inbox (§4.5): cases and open plain actions the caller may see."""
        if tab == "closed":
            rows = self.db._exec("SELECT * FROM actions WHERE (kind='case' AND phase IN ('resolved','auto_resolved','dismissed','suppressed')) "
                                 "OR (COALESCE(kind,'action')='action' AND status IN ('done','suppressed')) ORDER BY updated_at DESC LIMIT 1000")
        else:
            rows = self.db._exec("SELECT * FROM actions WHERE (kind='case' AND phase NOT IN ('resolved','auto_resolved','dismissed','suppressed') "
                                 "AND parent_id IS NULL) OR (COALESCE(kind,'action')='action' AND status IN ('open','in_progress')) "
                                 "ORDER BY id DESC LIMIT 3000")
        rows = [r for r in rows if visible(r)]
        if tab == "mine":
            rows = [r for r in rows if (r.get("owner") or "").lower() == (me or "").lower()]
        elif tab in ("approvals", "auto"):
            rows = []                                             # approvals (WP12) and supervised runs (WP16) arrive later
        if q:
            ql = q.lower()
            rows = [r for r in rows if ql in (r.get("title") or "").lower() or ql in (r.get("host") or "").lower()
                    or ql in (r.get("case_key") or "").lower()]
        nowdt = datetime.now(timezone.utc)
        out = []
        for r in rows:
            item = {k: r.get(k) for k in ("id", "kind", "title", "severity", "priority", "phase", "status", "host", "env", "pool", "team_id",
                                           "hits", "first_seen", "last_seen", "runbook_key", "level_hint", "next_step", "owner", "case_key",
                                           "source", "created_at", "updated_at", "reopened", "storm")}
            item["kind"] = item["kind"] or "action"
            if r.get("blind") and item["kind"] == "case" and self.act.shadow.hidden(r):
                item.update({"runbook_key": None, "next_step": "", "level_hint": None})     # a blind row names no runbook (C51)
            if item["kind"] == "action":
                item["severity"] = P_SEV.get(r.get("priority") or "", "medium")
                item["first_seen"] = r.get("created_at")
                item["last_seen"] = r.get("updated_at")
            item.update({"needs_human": bool(r.get("needs_human")), "blind": bool(r.get("blind")), "injection_suspect": bool(r.get("injection_suspect")),
                         "provenance": r.get("provenance") or "", "approval": None, "diag": {"done": 0, "total": 0}})
            item["children"] = int(self.db._exec("SELECT COUNT(*) AS n FROM actions WHERE parent_id=?", (r["id"],))[0]["n"]) if r.get("storm") else 0
            item["priority_score"] = inbox_score(item, me, my_teams, nowdt)
            out.append(item)
        out.sort(key=lambda x: (-x["priority_score"], -(datetime.fromisoformat(str(x.get("last_seen") or "1970-01-01T00:00:00+00:00")
                                                                                    .replace("Z", "+00:00")).timestamp())))
        return out[:limit]

    def manual(self, title: str, host: str, severity: str, team_id: Optional[int], ref: Optional[dict], by: str) -> Dict[str, Any]:
        trig = Trigger(source="manual", key=f"manual:{uuid.uuid4()}", host=host or "", severity=severity or "medium", title=title,
                       team_id=team_id, refs=[ref] if ref else [], facts={"by": self.act.audit.actor(by, "user")},
                       evidence=[{"kind": "note", "host": host or "", "text": title}])
        return self.route(trig)
