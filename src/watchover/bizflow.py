"""Business flows: one business key (an order, a leave request, a policy, a transfer) followed across every system that logs it.

A flow is a key pattern plus ordered steps:

    {"name": "Sipariş → Fatura", "key": "(?:sipariş|order)\\D{0,3}(\\d{6,10})", "hash_keys": false,
     "steps": [{"name": "Oluşturuldu", "match": "oluşturuldu|created"}, {"name": "Kredi onayı", "match": "kredi.*onay"}, ...]}

Every live event whose message carries the key and matches a step records (flow, key, step) with its first time, service, host
and whether it was an error. From those: the funnel (how many keys reached each step, conversion, median time between steps),
stuck items (reached a step, not the next one, for longer than the slowest 5 % of keys took), failing steps, and one key's
whole path across systems. Steps can be proposed from the logs themselves: the message templates that carry keys, in the order
they usually appear.

Keys are business identifiers, not people; when they could identify a person (an employee number in an HR flow) the flow's
`hash_keys` stores only a pseudonym.
"""
from __future__ import annotations

import json
import re
import statistics
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from .privacy import pseudonym, scrub

UTC = timezone.utc


FAIL = -1   # step slot for an error that carries the key but matches no step


def _failed_at(steps: dict, n: int) -> int:
    """The step a failure blocked: the one after the last step reached before it."""
    fail = steps[FAIL]["ts"]
    before = [i for i, r in steps.items() if i >= 0 and r["ts"] <= fail]
    return min(n - 1, (max(before) + 1) if before else 0)


class BizFlows:
    def __init__(self, kb):
        self.kb = kb
        self.lock = threading.Lock()
        pk = kb.pk
        kb._exec(f"""CREATE TABLE IF NOT EXISTS biz_flows (id {pk}, name TEXT UNIQUE NOT NULL, key_regex TEXT NOT NULL, steps TEXT DEFAULT '[]',
            hash_keys INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, created_at TEXT)""")
        kb._exec("""CREATE TABLE IF NOT EXISTS biz_events (flow_id INTEGER, bkey TEXT, step INTEGER, ts TEXT, service TEXT, host TEXT, error INTEGER DEFAULT 0,
            message TEXT DEFAULT '', PRIMARY KEY (flow_id, bkey, step))""")
        kb._exec("CREATE INDEX IF NOT EXISTS biz_events_ts ON biz_events(flow_id, ts)")
        self._compiled: tuple[float, list] | None = None

    # ---------------------------------------------------------------- definitions
    def list(self) -> list[dict]:
        out = []
        for r in self.kb._exec("SELECT * FROM biz_flows ORDER BY name"):
            d = dict(r); d["steps"] = json.loads(d["steps"] or "[]"); d["hash_keys"] = bool(d["hash_keys"]); d["enabled"] = bool(d["enabled"])
            out.append(d)
        return out

    def get(self, fid: int) -> dict:
        for f in self.list():
            if f["id"] == fid:
                return f
        raise KeyError(fid)

    @staticmethod
    def _check(key_regex: str, steps: list) -> list[dict]:
        rx = re.compile(key_regex, re.I)
        if rx.groups < 1:
            raise ValueError("the key pattern needs one capturing group: (...) around the identifier")
        clean = []
        for s in steps or []:
            name, match = str(s.get("name", "")).strip()[:60], str(s.get("match", "")).strip()[:300]
            if not name or not match:
                raise ValueError("every step needs a name and a match pattern")
            re.compile(match, re.I)
            clean.append({"name": name, "match": match, "service": str(s.get("service", "")).strip()[:80]})
        if len(clean) < 2:
            raise ValueError("a flow needs at least two steps")
        return clean

    def save(self, name: str, key_regex: str, steps: list, hash_keys: bool = False, enabled: bool = True, fid: int | None = None) -> dict:
        steps = self._check(key_regex, steps)
        name = name.strip()[:80]
        if not name:
            raise ValueError("flow name required")
        if fid is None:
            fid = self.kb._insert("INSERT INTO biz_flows (name, key_regex, steps, hash_keys, enabled, created_at) VALUES (?,?,?,?,?,?)",
                                  (name, key_regex, json.dumps(steps, ensure_ascii=False), int(hash_keys), int(enabled), datetime.now(UTC).isoformat(timespec="seconds")))
        else:
            self.kb._exec("UPDATE biz_flows SET name=?, key_regex=?, steps=?, hash_keys=?, enabled=? WHERE id=?",
                          (name, key_regex, json.dumps(steps, ensure_ascii=False), int(hash_keys), int(enabled), fid))
        self._compiled = None
        return self.get(fid)

    def delete(self, fid: int) -> None:
        self.kb._exec("DELETE FROM biz_events WHERE flow_id=?", (fid,))
        self.kb._exec("DELETE FROM biz_flows WHERE id=?", (fid,))
        self._compiled = None

    def _flows(self) -> list:
        import time
        if self._compiled is None or time.time() - self._compiled[0] > 30:
            comp = []
            for f in self.list():
                if not f["enabled"]:
                    continue
                try:
                    comp.append((f, re.compile(f["key_regex"], re.I), [(i, re.compile(s["match"], re.I), s.get("service", "")) for i, s in enumerate(f["steps"])]))
                except re.error:
                    continue
            self._compiled = (time.time(), comp)
        return self._compiled[1]

    # ---------------------------------------------------------------- intake
    def add_events(self, events) -> int:
        flows = self._flows()
        if not flows:
            return 0
        rows = []
        for o in events:
            msg = o.message or ""
            if not msg:
                continue
            for f, krx, steps in flows:
                m = krx.search(msg)
                if not m:
                    continue
                key = m.group(1)
                if f["hash_keys"]:          # the masked key replaces the raw one in the stored message too
                    raw, key = key, pseudonym(key, "k")
                    msg = msg.replace(raw, key)
                else:
                    key = key[:80]
                ts = (o.timestamp if o.timestamp.tzinfo else o.timestamp.replace(tzinfo=UTC)).isoformat()
                err = int(o.severity in ("ERROR", "CRITICAL"))
                for i, srx, svc in steps:
                    if svc and o.service != svc:
                        continue
                    if srx.search(msg):
                        rows.append((f["id"], key, i, ts, o.service or "", o.host or "", err, scrub(msg)[:240]))
                        break
                else:
                    if err:     # an error carrying the key that is no step of its own: the failure that blocks the next step
                        rows.append((f["id"], key, FAIL, ts, o.service or "", o.host or "", 1, scrub(msg)[:240]))
        if rows:
            sql = ("INSERT INTO biz_events (flow_id, bkey, step, ts, service, host, error, message) VALUES (?,?,?,?,?,?,?,?)"
                   + self.kb.upsert_suffix("flow_id, bkey, step", "error=CASE WHEN excluded.error > biz_events.error THEN excluded.error ELSE biz_events.error END"))
            with self.lock:
                self.kb.executemany(sql, rows)
        return len(rows)

    # ---------------------------------------------------------------- analysis
    def _items(self, fid: int, minutes: int | None, hosts=None) -> dict[str, dict[int, dict]]:
        where, p = "flow_id=?", [fid]
        if minutes:
            where += " AND bkey IN (SELECT bkey FROM biz_events WHERE flow_id=? AND ts >= ?)"
            p += [fid, (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()]
        items: dict[str, dict[int, dict]] = defaultdict(dict)
        for r in self.kb._exec(f"SELECT * FROM biz_events WHERE {where}", tuple(p)):
            if hosts is not None and r["host"] and r["host"] not in hosts:
                continue
            items[r["bkey"]][int(r["step"])] = r
        return items

    def funnel(self, fid: int, minutes: int | None = 1440, hosts=None) -> dict:
        f = self.get(fid)
        n = len(f["steps"])
        items = self._items(fid, minutes, hosts)
        now = datetime.now(UTC)
        reached = [0] * n
        errors = [0] * n
        gaps: list[list[float]] = [[] for _ in range(n)]
        for steps in items.values():
            if FAIL in steps:
                errors[_failed_at(steps, n)] += 1
            for i in range(n):
                if i in steps:
                    reached[i] += 1
                    errors[i] += int(steps[i]["error"] or 0)
                    if i + 1 in steps:
                        gaps[i].append((datetime.fromisoformat(steps[i + 1]["ts"]) - datetime.fromisoformat(steps[i]["ts"])).total_seconds())
        limits = []
        for g in gaps:
            gs = sorted(x for x in g if x >= 0)
            limits.append(max(60.0, gs[min(len(gs) - 1, int(len(gs) * 0.95))] * 1.5) if len(gs) >= 5 else 900.0)
        stuck = [0] * n
        for steps in items.values():
            last = max(steps)
            if 0 <= last < n - 1:
                waited = (now - datetime.fromisoformat(steps[last]["ts"])).total_seconds()
                if waited > limits[last]:
                    stuck[last] += 1
        first = reached[0] or max(reached) or 1
        out_steps = []
        for i, s in enumerate(f["steps"]):
            g = sorted(gaps[i])
            out_steps.append({"index": i, "name": s["name"], "reached": reached[i], "of_first": round(reached[i] / first, 4) if first else 0,
                              "from_prev": round(reached[i] / reached[i - 1], 4) if i and reached[i - 1] else None, "errors": errors[i], "stuck": stuck[i],
                              "to_next_p50_s": statistics.median(g) if g else None, "to_next_p95_s": g[min(len(g) - 1, int(len(g) * 0.95))] if g else None,
                              "stuck_after_s": round(limits[i]) if i < n - 1 else None})
        complete = sum(1 for st in items.values() if n - 1 in st)
        return {"flow": f, "items": len(items), "completed": complete, "completion": round(complete / len(items), 4) if items else None, "steps": out_steps}

    def items(self, fid: int, state: str = "stuck", minutes: int | None = 1440, limit: int = 100, hosts=None) -> list[dict]:
        f = self.get(fid)
        n = len(f["steps"])
        fun = self.funnel(fid, minutes, hosts)
        limits = {s["index"]: s["stuck_after_s"] for s in fun["steps"]}
        now = datetime.now(UTC)
        out = []
        for key, steps in self._items(fid, minutes, hosts).items():
            last = max(steps)
            waited = (now - datetime.fromisoformat(steps[last]["ts"])).total_seconds()
            is_stuck = 0 <= last < n - 1 and waited > (limits.get(last) or 900)
            has_err = any(s["error"] for s in steps.values())
            fail = steps.get(FAIL)
            if (state == "stuck" and not is_stuck) or (state == "error" and not has_err) or (state == "done" and last != n - 1):
                continue
            start = min(datetime.fromisoformat(s["ts"]) for s in steps.values())
            out.append({"key": key, "last_step": f["steps"][last]["name"] if last >= 0 else "", "last_index": last, "steps_done": len([k for k in steps if k >= 0]),
                        "waiting_s": round(waited), "started": start.isoformat(timespec="seconds"), "error": int(has_err), "stuck": int(is_stuck),
                        "last_service": steps[last]["service"], "last_host": steps[last]["host"],
                        "failure": {"at": f["steps"][_failed_at(steps, n)]["name"], "message": fail["message"], "service": fail["service"], "ts": fail["ts"]} if fail else None})
        return sorted(out, key=lambda x: (-x["stuck"], -x["waiting_s"]))[:limit]

    def item(self, fid: int, key: str) -> dict:
        f = self.get(fid)
        rows = self.kb._exec("SELECT * FROM biz_events WHERE flow_id=? AND bkey=? ORDER BY ts", (fid, key))
        done = {int(r["step"]): r for r in rows}
        fail = done.get(FAIL)
        return {"key": key, "flow": f["name"],
                "failure": {"at": f["steps"][_failed_at(done, len(f["steps"]))]["name"], "ts": fail["ts"], "service": fail["service"], "host": fail["host"],
                            "message": fail["message"]} if fail else None,
                "steps": [{"index": i, "name": s["name"], **({"ts": done[i]["ts"], "service": done[i]["service"], "host": done[i]["host"],
                                                                                              "error": done[i]["error"], "message": done[i]["message"]} if i in done else {"missing": True})}
                                                        for i, s in enumerate(f["steps"])]}

    # ---------------------------------------------------------------- step suggestions from the logs
    @staticmethod
    def suggest(key_regex: str, observations) -> list[dict]:
        """Message templates that carry a key, ordered by where they usually sit in a key's path (median rank), with counts."""
        from .analysis import template_of
        rx = re.compile(key_regex, re.I)
        if rx.groups < 1:
            raise ValueError("the key pattern needs one capturing group")
        per_key: dict[str, list] = defaultdict(list)
        tmpl_svc: dict[str, Counter] = defaultdict(Counter)
        for o in observations:
            m = rx.search(o.message or "")
            if m:
                tpl = template_of(o.message)
                per_key[m.group(1)].append((o.timestamp, tpl))
                tmpl_svc[tpl][o.service or ""] += 1
        ranks: dict[str, list[float]] = defaultdict(list)
        for evs in per_key.values():
            evs.sort()
            seen = []
            for _, t in evs:
                if t not in seen:
                    seen.append(t)
            for i, t in enumerate(seen):
                ranks[t].append(i / max(1, len(seen) - 1))
        out = []
        for t, r in ranks.items():
            if len(r) < 2:
                continue
            words = [w for w in re.findall(r"[a-zçğıöşü]{4,}", t) if w not in ("order", "sipariş", "siparis")][:3]
            out.append({"template": t, "keys": len(r), "position": round(statistics.median(r), 3), "service": tmpl_svc[t].most_common(1)[0][0],
                        "match": "|".join(words) if words else re.escape(t[:30]), "name": " ".join(words[:2]).capitalize() or t[:30]})
        return sorted(out, key=lambda x: (x["position"], -x["keys"]))[:12]

    def prune(self, days: int = 30) -> None:
        self.kb._exec("DELETE FROM biz_events WHERE ts < ?", ((datetime.now(UTC) - timedelta(days=days)).isoformat(),))

    def check_stuck(self, min_stuck: int = 20, minutes: int = 1440) -> list[dict]:
        """Steps of enabled flows with >= min_stuck stuck items, or an item waiting longer than 2 x the step's p95 (the action
        agent opens a case per flow step, docs/design/action-agent.md §4.1). Sample keys are hashed when the flow hashes keys."""
        import hashlib
        out = []
        for f in self.list():
            if not f.get("enabled"):
                continue
            try:
                fun = self.funnel(f["id"], minutes)
                stuck_items = self.items(f["id"], "stuck", minutes, limit=500)
            except Exception:  # noqa: BLE001 - one broken flow never stops the others
                continue
            for st in fun["steps"]:
                at = [x for x in stuck_items if x["last_index"] == st["index"]]
                if not at:
                    continue
                p95 = st.get("to_next_p95_s") or 0
                oldest = max(x["waiting_s"] for x in at)
                if len(at) < min_stuck and not (p95 and oldest > 2 * p95):
                    continue
                keys = [x["key"] for x in at[:3]]
                if f.get("hash_keys"):
                    keys = [hashlib.sha256(str(k).encode()).hexdigest()[:12] for k in keys]
                out.append({"flow_id": f["id"], "flow": f["name"], "step": st["index"], "step_name": st["name"], "stuck": len(at),
                            "oldest_s": oldest, "hosts": sorted({x["last_host"] for x in at if x.get("last_host")})[:5], "samples": keys,
                            "team_id": f.get("team_id")})
        return out
