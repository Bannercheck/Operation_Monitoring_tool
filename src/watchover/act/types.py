"""Frozen shapes of the action agent (docs/design/action-agent.md §2.1, §4.1, §9.1): every other act module and the API
contracts of §12 build on these, so they change only with a design revision.

Timestamps are stored as ISO-8601 UTC with "+00:00" (like every other table, range queries compare strings, C12); "Z" appears
only on the wire (`wire_ts`). Canonical JSON (hashes, MACs, job hashes): UTF-8, sorted keys, separators (",", ":"),
ensure_ascii=False, no floats (fractions travel as strings).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- vocabularies
MODES = ("off", "suggest", "guided", "approve", "supervised", "auto")
MODE_CAP = {"off": 0, "suggest": 0, "guided": 1, "approve": 2, "supervised": 3, "auto": 4}

LEVELS = {0: ("Yalnız öneri", "Suggest only"), 1: ("Rehberli", "Guided"), 2: ("Onaylı", "Approved"),
          3: ("Gözetimli otomatik", "Supervised automatic"), 4: ("Otomatik", "Automatic")}
RISKS = ("R0", "R1", "R2", "R3", "R4")
RISK_LABELS = {"R0": ("Salt okunur", "Read-only"), "R1": ("Düşük", "Low"), "R2": ("Orta", "Medium"), "R3": ("Yüksek", "High"),
               "R4": ("Kritik", "Critical")}
REVERSIBILITY = ("transient", "data_preserving", "reversible", "irreversible")

SOURCES = ("query", "threshold", "anomaly", "incident", "dataset", "bizflow", "manual", "external")
SEVERITIES = ("critical", "high", "medium", "low", "info")
PHASES = ("new", "diagnosing", "ready", "running", "waiting_human", "verifying", "watching", "resolved", "auto_resolved",
          "failed", "rolled_back", "escalated", "dismissed", "suppressed")
OPEN_PHASES = ("new", "diagnosing", "ready", "running", "waiting_human", "verifying", "watching", "failed", "rolled_back")
TERMINAL_PHASES = ("resolved", "auto_resolved", "escalated", "dismissed", "suppressed")
PHASE_STATUS = {"new": "open", "diagnosing": "open", "ready": "open", "escalated": "open",
                "running": "in_progress", "waiting_human": "in_progress", "verifying": "in_progress", "watching": "in_progress",
                "failed": "in_progress", "rolled_back": "in_progress",
                "resolved": "done", "auto_resolved": "done", "dismissed": "suppressed", "suppressed": "suppressed"}
PRIORITY_OF = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P4", "info": "P4"}

STEP_TYPES = ("diagnose", "act", "verify", "rollback", "manual", "choose", "wait", "notify", "itsm")
TEMPLATE_KINDS = ("diag", "act")
EXECUTORS = ("manual", "agent", "http", "itsm", "notify")
HTTP_KINDS = ("awx", "rundeck", "jenkins", "generic")
JOB_MODES = ("diagnose", "dry_run", "execute", "verify", "rollback")
OUTCOMES = ("ok", "ok_corrected", "unverified", "deviated", "exec_error", "verify_failed", "rolled_back", "rollback_failed",
            "side_effect", "unsafe_blocked", "relapse", "aborted", "not_needed", "escalated")
# worst first: each step has exactly one outcome, the worst one (C22)
OUTCOME_ORDER = ("side_effect", "rollback_failed", "unsafe_blocked", "rolled_back", "verify_failed", "exec_error", "deviated",
                 "unverified", "ok")
ACTOR_KINDS = ("user", "system", "agent", "model", "mcp")


# ---------------------------------------------------------------- helpers
def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stored_ts(value) -> str:
    """Any timestamp (datetime, "…Z", "…+03:00", epoch seconds) as the stored UTC form "YYYY-MM-DDTHH:MM:SS+00:00"."""
    if value is None or value == "":
        return now()
    if isinstance(value, (int, float)):
        d = datetime.fromtimestamp(float(value), timezone.utc)
    elif isinstance(value, datetime):
        d = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def wire_ts(stored: str) -> str:
    return stored[:-6] + "Z" if stored and stored.endswith("+00:00") else stored


def _no_floats(v):
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("canonical JSON cannot carry NaN or infinity")
        return int(v) if v.is_integer() else repr(v)
    if isinstance(v, dict):
        return {str(k): _no_floats(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_no_floats(x) for x in v]
    return v


def canonical(obj) -> str:
    return json.dumps(_no_floats(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data) -> str:
    if not isinstance(data, (bytes, bytearray)):
        data = (data if isinstance(data, str) else canonical(data)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def team_key(team_id: Optional[int]) -> str:
    """Key form of a team in unique keys (C9): '*' installation-wide, else the id."""
    return "*" if team_id is None else str(int(team_id))


def env_tier(env: str, prod_envs=("prod", "production", "prd", "canli")) -> str:
    return "prod" if (env or "").strip().lower() in prod_envs else "nonprod"


def cell_key(team_id: Optional[int], runbook: str, step: str, env: str, host_class: str) -> str:
    """t:<team_key>/<runbook>/<step>/<env>/<sha1(host_class)[:12]> (pool and cluster names may contain '/')."""
    return f"t:{team_key(team_id)}/{runbook}/{step}/{env}/{hashlib.sha1((host_class or '').encode()).hexdigest()[:12]}"


def level_label(level: Optional[int], lang: str = "tr") -> str:
    if level is None:
        return ""
    tr, en = LEVELS.get(int(level), ("?", "?"))
    return tr if lang == "tr" else en


class Level(int):
    """L0..L4; int subclass so min(cell, caps...) works directly (I3)."""
    def __new__(cls, v: int):
        v = int(v)
        if not 0 <= v <= 4:
            raise ValueError("level must be 0..4")
        return super().__new__(cls, v)

    @property
    def label(self) -> str:
        return level_label(int(self))


# ---------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Trigger:
    """One normalised signal event that opens or joins a case (§4.1); also the body of POST /api/act/triggers."""
    source: str
    key: str
    host: str = ""
    ts: str = ""
    env: str = ""
    service: str = ""
    severity: str = "medium"
    title: str = ""
    team_id: Optional[int] = None
    family: str = ""
    facts: Dict[str, Any] = field(default_factory=dict)
    refs: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    provenance: str = ""

    def __post_init__(self):
        if self.source not in SOURCES:
            raise ValueError(f"unknown trigger source {self.source!r}")
        if not self.key:
            raise ValueError("trigger key required")
        object.__setattr__(self, "ts", stored_ts(self.ts or None))
        sev = (self.severity or "medium").lower()
        object.__setattr__(self, "severity", sev if sev in SEVERITIES else "medium")

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Evidence:
    eid: str
    kind: str                       # log | metric | anomaly | incident | bizflow | diag | slot | note
    text: str = ""
    ts: str = ""
    host: str = ""
    agent_id: Optional[int] = None
    reported: str = ""
    source: str = ""                # trusted | claimed (who delivered it)
    provenance: str = ""            # trusted | trusted_signed | log | claimed | derived | human
    fields: Dict[str, Any] = field(default_factory=dict)
    slot: str = ""
    value: str = ""
    ref: Dict[str, Any] = field(default_factory=dict)
    quarantined: bool = False


@dataclass(frozen=True)
class CellKey:
    team_id: Optional[int]
    runbook: str
    step: str
    env: str
    host_class: str

    def __str__(self) -> str:
        return cell_key(self.team_id, self.runbook, self.step, self.env, self.host_class)


@dataclass(frozen=True)
class Case:
    id: int
    title: str
    phase: str
    host: str = ""
    team_id: Optional[int] = None
    case_key: str = ""
    family: str = ""
    source: str = ""
    severity: str = ""
    priority: str = "P3"
    hits: int = 0


@dataclass(frozen=True)
class Template:
    id: str
    rev: int
    os: str
    kind: str                       # diag | act
    risk: str
    sha256: str
    body: Dict[str, Any] = field(default_factory=dict)
    reversibility: str = ""
    cost: str = "light"
    executor: str = "agent"
    title_tr: str = ""
    title_en: str = ""


@dataclass(frozen=True)
class Step:
    id: str
    type: str
    title: str = ""
    template: str = ""
    executor: str = ""              # '' = the template's executor (agent), or manual / http / itsm / notify
    exec: Dict[str, Any] = field(default_factory=dict)   # connector binding, e.g. {"awx": {"connector": .., "job_template": 42}}
    args: Dict[str, Any] = field(default_factory=dict)
    body: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Runbook:
    key: str
    version: int
    title: str
    risk: str
    steps: Tuple[Step, ...] = ()
    team_id: Optional[int] = None
    body: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Plan:
    """Built by executor.plan(); shown to people; hashed for approvals (§9.1). `job_hash` binds what a person sees to what runs."""
    executor: str
    host: str
    template_id: str
    template_sha256: str
    params: Dict[str, Any]
    mode: str
    preview: Any = ()
    human_cmd: str = ""
    job_hash: str = ""


@dataclass(frozen=True)
class Job:
    job_id: str
    step_id: int
    host: str
    mode: str
    executor: str
    template_id: str = ""
    template_sha256: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    seq: int = 0


@dataclass(frozen=True)
class JobResult:
    state: str                      # ok | failed | timeout | rejected | cancelled | expired | unknown
    exit_code: Optional[int] = None
    duration_ms: int = 0
    output: str = ""
    truncated: bool = False
    facts: Dict[str, Any] = field(default_factory=dict)
    reject_code: str = ""
    error: str = ""


@dataclass(frozen=True)
class Outcome:
    outcome: str
    credit: float = 0.0
    penalty: float = 0.0

    def __post_init__(self):
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")


def worst(outcomes) -> str:
    """The one outcome of a step: the worst of what happened (C22)."""
    seen = [o for o in outcomes if o]
    for o in OUTCOME_ORDER:
        if o in seen:
            return o
    return seen[0] if seen else ""


def job_hash(host: str, template_id: str, template_sha256: str, params: dict, mode: str, version_sha: str, step_key: str) -> str:
    return sha256_hex({"host": host, "template_id": template_id, "template_sha256": template_sha256, "params": params,
                       "mode": mode, "runbook_version_sha256": version_sha, "step_key": step_key})
