"""Kill switches (docs/design/action-agent.md §10.3). Budgets, host locks, loop guard, windows / freeze and the circuit breaker
join this module with the run engine (WP8).

* Global stop without the database: `python -m watchover.act kill --global --reason "…"` writes <WATCHOVER_HOME>/act.KILL;
  the dispatcher reads it (cached ≤ 1 s) before every dispatch.
* Stops in the database (`act_kill`): global, team, runbook, host, template. Stopping takes one person with a reason;
  resuming a team / runbook / host stop takes one `act.promote` holder; a global stop takes two distinct `act.policy` holders.
Every stop and resume is an audit event."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import List, Optional

from .types import now

SCOPES = ("global", "team", "runbook", "host", "template")
_PANIC = {"at": 0.0, "val": None}


def panic_file() -> Path:
    return Path(os.environ.get("WATCHOVER_HOME") or "data") / "act.KILL"


def panic_set(reason: str, by: str = "cli") -> Path:
    p = panic_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"reason": reason, "by": by, "ts": now()}, ensure_ascii=False), encoding="utf-8")
    _PANIC["at"] = 0.0
    return p


def panic_clear() -> None:
    try:
        panic_file().unlink()
    except FileNotFoundError:
        pass
    _PANIC["at"] = 0.0


def panic() -> Optional[dict]:
    """The panic file's content, cached for one second."""
    if time.monotonic() - _PANIC["at"] < 1.0:
        return _PANIC["val"]
    p = panic_file()
    val = None
    if p.exists():
        try:
            val = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            val = {"reason": "act.KILL", "by": "file"}
        val.setdefault("reason", ""); val["scope"] = "global"; val["file"] = True
    _PANIC.update(at=time.monotonic(), val=val)
    return val


class KillError(ValueError):
    def __init__(self, code: str, tr: str, en: str, status: int = 409):
        super().__init__(en)
        self.code, self.tr, self.en, self.status = code, tr, en, status

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en}


class Kills:
    def __init__(self, db, audit):
        self.db, self.audit = db, audit

    def active(self, team_ids: Optional[List[int]] = None) -> List[dict]:
        rows = self.db._exec("SELECT * FROM act_kill WHERE active=1 ORDER BY id DESC")
        if team_ids is not None:
            rows = [r for r in rows if r["scope"] == "global" or r["team_id"] is None or r["team_id"] in team_ids]
        p = panic()
        if p:
            rows = [{"id": 0, "scope": "global", "ref": "", "team_id": None, "active": 1, "hard": 1, "by_": p.get("by", ""),
                     "reason": p.get("reason", ""), "ts": p.get("ts", ""), "file": True}] + rows
        return rows

    def killed(self, team_id: Optional[int] = None, host: str = "", runbook: str = "", template: str = "") -> Optional[dict]:
        """The first stop that applies (checked at proposal, start, dispatch, agent fetch and pre-exec, I7)."""
        for k in self.active():
            s, ref = k["scope"], str(k.get("ref") or "")
            if s == "global" or (s == "team" and team_id is not None and ref == str(team_id)) or (s == "host" and host and ref == host) \
                    or (s == "runbook" and runbook and ref == runbook) or (s == "template" and template and ref == template):
                return k
        return None

    def stop(self, scope: str, ref: str, by: str, reason: str, team_id: Optional[int] = None, hard: bool = False) -> dict:
        if scope not in SCOPES:
            raise KillError("act_param_invalid", f"Geçersiz kapsam: {scope}", f"Invalid scope: {scope}", 422)
        if not (reason or "").strip():
            raise KillError("act_param_invalid", "Durdurma nedeni gerekli", "A reason is required", 422)
        ref = "" if scope == "global" else str(ref or "")
        with self.db.tx():
            kid = self.db._insert("INSERT INTO act_kill (scope, ref, team_id, active, hard, by_, reason, ts) VALUES (?,?,?,?,?,?,?,?)",
                                  (scope, ref, team_id, 1, 1 if hard else 0, self.audit.actor(by, "user"), reason.strip()[:300], now()))
            self.audit.write("kill.on", object_kind="kill", object_id=kid, actor=by, actor_kind="user", team_id=team_id,
                             payload={"scope": scope, "ref": ref, "hard": bool(hard), "reason": reason.strip()[:300]})
        return {"id": kid, "effective_within_s": 12}

    def resume(self, kid: int, by: str, reason: str = "") -> dict:
        rows = self.db._exec("SELECT * FROM act_kill WHERE id=?", (kid,))
        if not rows or not rows[0]["active"]:
            raise KeyError(kid)
        k = rows[0]
        me = self.audit.actor(by, "user")
        if k["scope"] == "global":                       # two distinct act.policy holders (§10.3); the first one is recorded
            first = (k.get("resumed_by") or "")
            if not first:
                with self.db.tx():
                    self.db._exec("UPDATE act_kill SET resumed_by=?, resume_reason=? WHERE id=?", (me, (reason or "")[:300], kid))
                    self.audit.write("kill.resume_vote", object_kind="kill", object_id=kid, actor=by, actor_kind="user",
                                     payload={"scope": "global", "reason": (reason or "")[:300]})
                return {"id": kid, "active": True, "votes": 1, "need": 2}
            if first == me:
                raise KillError("act_sod", "Genel durdurmayı ikinci bir kişi kaldırmalı", "A second person must resume a global stop", 403)
        with self.db.tx():
            self.db._exec("UPDATE act_kill SET active=0, resumed_by=?, resumed_at=?, resume_reason=? WHERE id=?",
                          ((k.get("resumed_by") + "," if k["scope"] == "global" and k.get("resumed_by") else "") + me, now(),
                           (reason or "")[:300], kid))
            self.audit.write("kill.off", object_kind="kill", object_id=kid, actor=by, actor_kind="user", team_id=k["team_id"],
                             payload={"scope": k["scope"], "ref": k["ref"], "reason": (reason or "")[:300]})
        return {"id": kid, "active": False, "votes": 2 if k["scope"] == "global" else 1, "need": 2 if k["scope"] == "global" else 1}


# ---------------------------------------------------------------- WP8: time scale, windows and freeze, budgets
def time_scale() -> float:
    """WATCHOVER_ACT_TIME_SCALE (demo only, C49): 30 makes the 15-minute watch window take 30 s. Refused without act_demo."""
    raw = os.environ.get("WATCHOVER_ACT_TIME_SCALE", "").strip()
    if not raw:
        return 1.0
    from .policy import act_demo
    if not act_demo():
        return 1.0
    try:
        return max(1.0, min(float(raw), 3600.0))
    except ValueError:
        return 1.0


def _fnmatch_any(value: str, pats) -> bool:
    import fnmatch
    pats = [p for p in (pats or []) if p]
    return not pats or any(fnmatch.fnmatch(value or "", p) for p in pats)


def _local(at, tz: str):
    from datetime import timezone as _tz
    try:
        from zoneinfo import ZoneInfo
        return at.astimezone(ZoneInfo(tz or "Europe/Istanbul"))
    except Exception:  # noqa: BLE001 - unknown zone: UTC (the calendar row is still visible to people)
        return at.astimezone(_tz.utc)


class Windows:
    """Change windows and freezes (§6.7) from `act_windows`. A freeze caps act steps in scope at L1 (diagnose and verify go on);
    an unreadable freeze calendar treats production as frozen."""
    def __init__(self, db):
        self.db = db

    def rows(self, kind: str, team_id: Optional[int]) -> List[dict]:
        try:
            rows = self.db._exec("SELECT * FROM act_windows WHERE kind=? AND enabled=1", (kind,))
        except Exception:  # noqa: BLE001
            return [{"name": "calendar unreadable", "broken": True}]
        return [r for r in rows if r.get("team_id") is None or team_id is None or r["team_id"] == team_id]

    @staticmethod
    def _applies(row: dict, env: str, host_class: str, host: str) -> bool:
        if row.get("env") and (env or "") != row["env"]:
            return False
        try:
            sel = json.loads(row.get("selector") or "{}")
        except ValueError:
            sel = {}
        return _fnmatch_any(host_class, sel.get("host_class")) and _fnmatch_any(host, sel.get("hosts"))

    def frozen(self, team_id: Optional[int], env: str, host_class: str = "", host: str = "", at=None) -> Optional[dict]:
        from datetime import datetime as _dt, timezone as _tz
        at = at or _dt.now(_tz.utc)
        for r in self.rows("freeze", team_id):
            if r.get("broken"):
                return {"name": r["name"]} if (env or "").lower() in ("prod", "production", "prd", "canli") else None
            if not self._applies(r, env, host_class, host):
                continue
            try:
                rule = json.loads(r.get("rule") or "{}")
            except ValueError:
                return {"name": r.get("name") or "freeze"}
            day = _local(at, rule.get("tz", "")).strftime("%Y-%m-%d")
            for d in rule.get("dates") or []:
                if str(d.get("from", "")) <= day <= str(d.get("to", "")):
                    return {"name": r.get("name") or "freeze", "to": d.get("to")}
        return None

    def in_window(self, name: str, team_id: Optional[int], env: str = "", host_class: str = "", host: str = "", at=None) -> bool:
        from datetime import datetime as _dt, timezone as _tz
        at = at or _dt.now(_tz.utc)
        for r in self.rows("window", team_id):
            if r.get("broken") or r.get("name") != name or not self._applies(r, env, host_class, host):
                continue
            try:
                rule = json.loads(r.get("rule") or "{}")
            except ValueError:
                continue
            t = _local(at, rule.get("tz", ""))
            days = rule.get("days") or [1, 2, 3, 4, 5, 6, 7]
            cur = t.strftime("%H:%M")
            if t.isoweekday() in days and str(rule.get("from", "00:00")) <= cur < str(rule.get("to", "24:00")):
                return True
        return False


class Budgets:
    """Run budgets computed from the database (§10.4), so they survive restarts. Exceeding one lowers the level (never blocks
    guided work); the loop guard (same runbook on the same host ≤ 3 per 24 h, ≥ 30 min apart) blocks a system start."""
    def __init__(self, db, policy):
        self.db, self.policy = db, policy

    def _b(self) -> dict:
        return dict(self.policy.get("act_budgets") or {})

    def runbook_host(self, runbook_id: int, host: str, at, exclude_run: Optional[int] = None) -> Optional[str]:
        from datetime import timedelta as _td
        b = self._b()
        since = (at - _td(hours=24)).isoformat(timespec="seconds")
        rows = self.db._exec("SELECT id, started_at FROM act_runs WHERE runbook_id=? AND host=? AND started_at>=? ORDER BY started_at DESC",
                             (runbook_id, host, since))
        rows = [r for r in rows if r["id"] != exclude_run]
        if len(rows) >= int(b.get("runs_per_host_24h", 3) or 3):
            return f"runs_per_host_24h:{len(rows)}"
        if rows:
            from datetime import datetime as _dt
            try:
                last = _dt.fromisoformat(rows[0]["started_at"])
                if (at - last).total_seconds() < int(b.get("cooldown_min", 30) or 30) * 60:
                    return "cooldown"
            except ValueError:
                pass
        return None

    def diag(self, team_id: Optional[int], case_id: int, at) -> Optional[str]:
        """Automatic diagnosis: ≤ case_diag jobs per case open, ≤ team_diag_per_h per team and hour."""
        from datetime import timedelta as _td
        b = self._b()
        n_case = self.db._exec("SELECT COUNT(*) AS n FROM act_jobs j JOIN act_steps s ON s.id=j.step_id JOIN act_runs r ON r.id=s.run_id "
                               "WHERE r.case_id=? AND j.mode='diagnose'", (case_id,))[0]["n"]
        if int(n_case or 0) >= int(b.get("case_diag", 3) or 3) * 4:     # per run of a case: a runbook's own diagnose steps
            return "case_diag"
        since = (at - _td(hours=1)).isoformat(timespec="seconds")
        tw, ta = ("IS NULL", ()) if team_id is None else ("=?", (team_id,))
        n_team = self.db._exec(f"SELECT COUNT(*) AS n FROM act_jobs j JOIN act_steps s ON s.id=j.step_id JOIN act_runs r ON r.id=s.run_id "
                               f"WHERE r.team_id {tw} AND j.mode='diagnose' AND j.leased_at>=?", ta + (since,))[0]["n"]
        if int(n_team or 0) >= int(b.get("team_diag_per_h", 60) or 60):
            return "team_diag_per_h"
        return None
