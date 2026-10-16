"""Demo controls of the run engine (docs/design/action-agent.md §16 WP8, §17.1; C46, C49). Refused unless the installation
runs with act_demo (WATCHOVER_ACT_DEMO=1); they enable nothing else.

    python -m watchover.act demo disk [--host web-prod-07] [--pct 93.4]   a /var/log disk case within seconds
    python -m watchover.act demo run-guided <case>                         walk the guided run; the simulated disk moves

The simulated disk is <WATCHOVER_HOME>/act_demo_disk.json ({host: use %}); verify's `metric_below` and the pasted `df -P`
output read it. With WATCHOVER_ACT_TIME_SCALE=30 the 15-minute watch window takes 30 s."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from .policy import act_demo
from .types import Trigger
from .verify import demo_disk_file

RUNBOOK = "linux-varlog-full"
AUTHOR, REVIEWER = "system", "demo-reviewer@watchover.local"


class DemoError(RuntimeError):
    status = 409
    code = "act_demo_only"

    def detail(self) -> dict:
        return {"code": self.code, "tr": "Yalnız demo kurulumunda (WATCHOVER_ACT_DEMO=1)", "en": "Demo installations only (WATCHOVER_ACT_DEMO=1)"}


def _need_demo() -> None:
    if not act_demo():
        raise DemoError("demo controls need act_demo")


def disk_set(host: str, pct: float) -> None:
    p = demo_disk_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        d = {}
    d[host] = round(float(pct), 1)
    p.write_text(json.dumps(d), encoding="utf-8")


def disk_get(host: str) -> Optional[float]:
    try:
        return float(json.loads(demo_disk_file().read_text(encoding="utf-8")).get(host))
    except (OSError, ValueError, TypeError):
        return None


def df_output(pct: float, mount: str = "/") -> str:
    size = 52403200                                                    # 50 GB in 1K blocks
    used = int(size * pct / 100)
    return (f"Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
            f"/dev/sda1        {size} {used} {size - used}      {int(round(pct))}% {mount}\n")


def ensure_runbook(act, reviewer: str = REVIEWER) -> dict:
    """The starter /var/log runbook adopted and published for the demo (a team would do this with a second person)."""
    _need_demo()
    if act.mode == "off":
        act.set_mode("guided", by="demo")
    act.ensure_starter()
    rb = act.runbooks.get(RUNBOOK)
    if rb.get("status") == "active" and rb.get("current_version"):
        return rb
    v = int(rb.get("current_version") or 1)
    ver = act.runbooks.version(RUNBOOK, v)
    with act.db.tx():
        act.db._exec("UPDATE act_runbooks SET status='active' WHERE key=?", (RUNBOOK,))
        act.audit.write("runbook.adopted", object_kind="runbook", object_id=RUNBOOK, actor="demo", actor_kind="system",
                        payload={"version": v, "demo": True})
    if ver["status"] == "draft":
        act.runbooks.submit(RUNBOOK, v, by=ver.get("author") or AUTHOR, tabletop=True)
    if act.runbooks.version(RUNBOOK, v)["status"] == "in_review":
        act.runbooks.review(RUNBOOK, v, by=reviewer, decision="approve", reason="demo")
    return act.runbooks.get(RUNBOOK)


def disk(act, host: str = "web-prod-07", pct: float = 93.4, reviewer: str = REVIEWER) -> Dict[str, Any]:
    """A disk-filling /var/log threshold breach on `host` → one case (through the normal trigger path)."""
    _need_demo()
    ensure_runbook(act, reviewer)
    disk_set(host, pct)
    trig = Trigger(source="threshold", key=f"threshold:disk:{host}", host=host, severity="high", env="prod",
                   title=f"Disk %{str(pct).replace('.', ',')} · /var/log", family="disk",
                   facts={"metric": "disk", "value": pct, "threshold": 90, "path": "/var/log"},
                   evidence=[{"kind": "metric", "text": f"disk %{str(pct).replace('.', ',')} (sınır 90) bağlama /", "host": host,
                              "fields": {"metric": "disk", "value": pct, "mount": "/"}},
                             {"kind": "log", "host": host, "text": "ERROR orders write failed: No space left on device (/var/log/app/orders.log)"}])
    act.emit(trig)
    act.drain()
    rows = act.db._exec("SELECT * FROM actions WHERE kind='case' AND host=? ORDER BY id DESC LIMIT 1", (host,))
    return dict(rows[0]) if rows else {}


def run_guided(act, cid: int, by: str = "demo@watchover.local", wait: bool = False, after_pct: float = 62.0) -> Dict[str, Any]:
    """Walk the guided run: paste the simulated outputs, choose compression, "Yaptım" frees the simulated disk, verify reads it,
    the watch window passes (time-scaled), the label is written."""
    _need_demo()
    eng = act.engine
    case = dict(act.db._exec("SELECT * FROM actions WHERE id=?", (cid,))[0])
    host = case.get("host") or ""
    run = eng.active_for_case(cid) or eng.start(cid, by, RUNBOOK, params={"path": "/var/log", "days": 7})
    rid = run["id"]
    for _ in range(40):
        run = eng.get(rid)
        if run["status"] not in ("running", "blocked", "verifying", "watching", "rolling_back"):
            break
        if run["status"] == "blocked":
            run = eng.proceed(rid, by)
            continue
        if run["status"] == "watching":
            if not wait:
                break
            time.sleep(1)
            eng.advance(rid)
            continue
        guided = [s for s in run["steps"] if s["status"] == "guided"]
        if not guided:
            if run["status"] == "running" and not any(s["status"] in ("dispatched", "running", "ready", "waiting") for s in run["steps"]):
                break
            time.sleep(0.2 if wait else 0)
            eng.advance(rid)
            if not wait:
                break
            continue
        s = guided[0]
        pct = disk_get(host) or 93.4
        if s["type"] == "choose":
            eng.choose(rid, s["id"], by, "a1")
        elif s["step_key"] == "d1" or (s["type"] == "verify"):
            eng.done(rid, s["id"], by, "ok", output=df_output(pct))
        elif s["step_key"] == "d2":
            eng.done(rid, s["id"], by, "ok", output="7812345678 /var/log/app\n402112 /var/log/nginx\n")
        elif s["step_key"] == "d3":
            eng.done(rid, s["id"], by, "ok", output="deleted_open_bytes=0 files=0")
        elif s["type"] == "act":
            disk_set(host, after_pct)                                    # the person compressed the logs: the disk frees up
            eng.done(rid, s["id"], by, "ok", note="demo: eski loglar sıkıştırıldı")
        else:
            eng.skip(rid, s["id"], by, "demo")
    return eng.get(rid)
