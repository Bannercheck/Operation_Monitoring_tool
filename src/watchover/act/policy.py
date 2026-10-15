"""Installation policy of the action agent (docs/design/action-agent.md §15.2): DEFAULTS, stored overrides in `act_policy`
(every change audited), team overrides that may only be stricter (`act_team_policy`), and `act_mode`.

`act_mode` is "off" after an upgrade (K1, I14): no hook does anything. Turning it on starts with cases and guided mode; modes
from "approve" up need the residual-risk note accepted (§10.1) and real signing keys (§15.3 step 6)."""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Any, Dict, Optional

from .types import MODE_CAP, MODES, now

DEFAULTS: Dict[str, Any] = {
    "act_mode": "off", "act_prod_envs": ["prod", "production", "prd", "canli"],
    "act_z": 1.645, "act_half_life_success_d": 45, "act_half_life_failure_d": 90, "act_event_window_d": 365,
    "act_thresholds": {"exec": {"2": 0.75, "3": 0.90, "4": 0.95}, "sel": {"3": 0.85, "4": 0.92}, "hysteresis": 0.10, "theta_l1": 0},
    "act_host_weight_cap": 0.25, "act_promo_min_hosts": 3, "act_ceiling_min_runs": 30,
    "act_blind_collect": 30, "act_blind_max": 60, "act_blind_factor": 1.2, "act_audit_sample_min": 10, "act_audit_per_30d": 4,
    "act_review_backlog": {"runs": 3, "hours": 72},
    "act_reopen_min": 60, "act_watch_min": 15, "act_dormant_days": 45, "act_cases_per_min": 20, "act_group_min": 30,
    "act_approval_min": 15, "act_stepup_min": 10, "act_stepup_oidc_max_age_s": 120,
    "act_veto_s": {"R1": {"nonprod": 120, "prod": 300}, "R2": {"nonprod": 300, "prod": 600}, "R3": {"nonprod": 600}},
    "act_budgets": {"team_auto_per_h": 10, "team_l2_per_h": 30, "install_auto_per_h": 40, "install_auto_hosts_per_h": 20,
                    "team_diag_per_h": 60, "case_diag": 3, "runs_per_host_24h": 3, "cooldown_min": 30},
    "act_breaker": {"fails_30m": 3, "fail_share_2h": 0.2, "min_runs_2h": 5, "open_h": 2},
    "act_caps_lower": [{"match": {"host_class": "sap*", "env_tier": "prod"}, "max": 2}],
    "act_expected_noise_max": 5, "act_expected_noise_s_max": 300, "act_expect_silence_s_max": 120,
    "act_itsm_required": False, "act_itsm_wait_min": 30, "act_bizflow_min": 20,
    "act_key_custody": "keyd", "act_anchor_max_age_min": 120, "act_anchor_every_min": 60, "act_anchor_recipients": [],
    "act_llm_local_only": True, "act_llm_budget_per_hour": 120, "act_llm_queue": 200,
    "act_prompt_tokens": 1200, "act_evidence_lines": 12, "act_evidence_chars": 160, "act_select_num_predict": 64,
    "act_llm_timeout_s": {"min": 20, "max": 90, "factor": 2}, "act_llm_off_p95_s": 60,
    "act_eval_days": 7, "act_train_mix_max": 0.5, "act_train_mix_unit": "tokens", "act_train_max_examples": 300,
    "act_train_prompt_tokens": 600, "act_gate_injection_pairs": {"gpu": 60, "cpu": 30}, "act_golden_min_per_runbook": 10,
    "act_challenger_days": 7, "act_challenger_min_cases": 30, "act_train_share": [], "act_card_names": "initials",
    "act_teams_kind": "teams_workflow", "act_actor_retention_days": 730,
    "act_retention_d": {"raw_output": 7, "outputs": 90, "evidence": 400, "audit_payload": 400, "runs": 730, "examples": 365,
                        "datasets": 180},
    # stored acceptance of the residual-risk note (§10.1); required before act_mode >= approve
    "act_residual_risk_accepted": "",
}

RESIDUAL_RISK_TR = ("Sunucu ele geçirilirse en kötü durumda, normal ve düşük kritiklikteki sunucularda izin listelerinde bulunan "
                    "şablonlar sunucunun kendi sınırları içinde çalıştırılabilir; kritik ve yüksek kritiklikteki sunucularda ortak "
                    "imza olmadan yalnız teşhis çalışır. Kritik sunucularda izin listesini dar tutun.")
RESIDUAL_RISK_EN = ("If the server is compromised, in the worst case allowlisted templates can run on normal and low criticality "
                    "hosts within each host's own limits; on critical and high hosts only diagnosis runs without a co-signature. "
                    "Keep the allowlist narrow on critical hosts.")

# demo-only keys: refused unless the installation runs with act_demo (WATCHOVER_ACT_DEMO=1)
DEMO_ONLY = ("act_demo", "act_time_scale")
# team overrides: only these keys, only in the stricter direction (higher = stricter / lower = stricter)
TEAM_HIGHER = ("act_thresholds", "act_veto_s", "act_watch_min", "act_blind_collect", "act_ceiling_min_runs", "act_promo_min_hosts")
TEAM_LOWER = ("act_budgets", "act_host_weight_cap", "act_cases_per_min", "act_approval_min")


class PolicyError(ValueError):
    """A refused policy change; `code` is one of the §12.3 error codes."""
    def __init__(self, code: str, tr: str, en: str, status: int = 409, **extra):
        super().__init__(en)
        self.code, self.tr, self.en, self.status, self.extra = code, tr, en, status, extra

    def detail(self) -> dict:
        return {"code": self.code, "tr": self.tr, "en": self.en, **self.extra}


def act_demo() -> bool:
    return os.environ.get("WATCHOVER_ACT_DEMO", "").strip().lower() in ("1", "true", "yes", "on")


def _cmp_stricter(base, new, higher: bool) -> bool:
    if isinstance(base, dict) and isinstance(new, dict):
        return all(k in base and _cmp_stricter(base[k], v, higher) for k, v in new.items())
    if isinstance(base, (int, float)) and isinstance(new, (int, float)) and not isinstance(new, bool):
        return new >= base if higher else new <= base
    return base == new


class Policy:
    """Installation policy. Reads are cached for `ttl` seconds (the anomaly and alert threads ask on every event)."""

    def __init__(self, db, audit=None, ttl: float = 2.0):
        self.db, self.audit, self.ttl = db, audit, ttl
        self._cache: Optional[Dict[str, Any]] = None
        self._at = 0.0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- reads
    def stored(self) -> Dict[str, Any]:
        out = {}
        for r in self.db._exec("SELECT key, value FROM act_policy"):
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, ValueError):
                continue
        return out

    def all(self) -> Dict[str, Any]:
        with self._lock:
            if self._cache is not None and time.monotonic() - self._at < self.ttl:
                return self._cache
        merged = copy.deepcopy(DEFAULTS)
        try:
            merged.update(self.stored())
        except Exception:  # noqa: BLE001 - a missing table (very first start) reads as the defaults
            pass
        with self._lock:
            self._cache, self._at = merged, time.monotonic()
        return merged

    def get(self, key: str, default=None):
        return self.all().get(key, DEFAULTS.get(key, default))

    @property
    def mode(self) -> str:
        m = self.get("act_mode", "off")
        return m if m in MODES else "off"

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # ---------------------------------------------------------------- writes (audited, in one transaction with the audit row)
    def check(self, key: str, value, custody: str = "none") -> None:
        if key not in DEFAULTS and key not in DEMO_ONLY:
            raise PolicyError("act_param_invalid", f"Bilinmeyen ayar: {key}", f"Unknown setting: {key}", status=422)
        if key in DEMO_ONLY and not act_demo():
            raise PolicyError("act_param_invalid", "Bu ayar yalnız demo kurulumunda geçerli", "Demo installations only", status=422)
        if key == "act_mode":
            if value not in MODES:
                raise PolicyError("act_param_invalid", f"Geçersiz mod: {value}", f"Invalid mode: {value}", status=422)
            if MODE_CAP[value] >= 2:
                if not self.get("act_residual_risk_accepted"):
                    raise PolicyError("act_risk_unaccepted", "Önce kalan risk notu kabul edilmeli",
                                      "Accept the residual-risk note first", note_tr=RESIDUAL_RISK_TR, note_en=RESIDUAL_RISK_EN)
                if custody == "none":
                    raise PolicyError("act_signing_unavailable", "İmza anahtarları yok: sistem yürütmesi kapalı",
                                      "No signing keys: system execution is disabled", status=503)
        if key == "act_key_custody" and value not in ("keyd", "embedded"):
            raise PolicyError("act_param_invalid", "Anahtar saklama keyd ya da embedded olmalı", "Custody must be keyd or embedded", status=422)
        if key == "act_key_custody" and value == "embedded" and not act_demo():
            raise PolicyError("act_param_invalid", "Gömülü anahtar yalnız demo kurulumunda", "Embedded custody is demo-only", status=422)

    def set(self, key: str, value, by: str = "system", actor_kind: str = "user", custody: str = "none") -> Dict[str, Any]:
        self.check(key, value, custody)
        old = self.get(key)
        ts = now()
        db = self.db

        def write():
            db._exec("DELETE FROM act_policy WHERE key=?", (key,))
            db._exec("INSERT INTO act_policy (key, value, updated_by, updated_at) VALUES (?,?,?,?)",
                     (key, json.dumps(value, ensure_ascii=False), by, ts))
            if self.audit is not None:
                self.audit.write("policy.changed", object_kind="policy", object_id=key, actor=by, actor_kind=actor_kind,
                                 payload={"key": key, "from": old, "to": value})

        if db.in_tx:
            write()
        else:
            with db.tx():
                write()
        self.invalidate()
        return {"key": key, "value": value, "previous": old, "updated_at": ts}

    # ---------------------------------------------------------------- team overrides (stricter only)
    def team(self, team_id: Optional[int]) -> Dict[str, Any]:
        base = self.all()
        if team_id is None:
            return base
        rows = self.db._exec("SELECT body FROM act_team_policy WHERE team_id=?", (team_id,))
        if not rows:
            return base
        try:
            body = json.loads(rows[0]["body"] or "{}")
        except ValueError:
            return base
        out = copy.deepcopy(base)
        for k, v in body.items():
            if self.stricter(k, base.get(k), v):
                if isinstance(out.get(k), dict) and isinstance(v, dict):
                    out[k] = _merge(out[k], v)
                else:
                    out[k] = v
        return out

    @staticmethod
    def stricter(key: str, base, new) -> bool:
        if key == "act_blind_collect":
            return isinstance(new, (int, float)) and base <= new <= DEFAULTS["act_blind_max"]
        if key in TEAM_HIGHER:
            return _cmp_stricter(base, new, True)
        if key in TEAM_LOWER:
            return _cmp_stricter(base, new, False)
        if key == "act_caps_lower":
            return isinstance(new, list) and all(x in new for x in (base or []))
        return False

    def set_team(self, team_id: int, body: Dict[str, Any], by: str) -> Dict[str, Any]:
        base = self.all()
        bad = [k for k, v in body.items() if not self.stricter(k, base.get(k), v)]
        if bad:
            raise PolicyError("act_param_invalid", "Ekip ayarı yalnız daha sıkı olabilir: " + ", ".join(bad),
                              "Team settings may only be stricter: " + ", ".join(bad), status=422)
        ts = now()

        def write():
            self.db._exec("DELETE FROM act_team_policy WHERE team_id=?", (team_id,))
            self.db._exec("INSERT INTO act_team_policy (team_id, body, updated_by, updated_at) VALUES (?,?,?,?)",
                          (team_id, json.dumps(body, ensure_ascii=False), by, ts))
            if self.audit is not None:
                self.audit.write("policy.changed", object_kind="team_policy", object_id=str(team_id), actor=by, team_id=team_id,
                                 payload={"team_id": team_id, "body": body})

        if self.db.in_tx:
            write()
        else:
            with self.db.tx():
                write()
        return {"team_id": team_id, "body": body, "updated_at": ts}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out
