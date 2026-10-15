"""DDL of the action agent (docs/design/action-agent.md §11) and small helpers. Idempotent: CREATE TABLE IF NOT EXISTS plus
ALTER TABLE for columns added later, the pattern every store already uses. No nullable column is part of a primary or unique key
(C9): keys that need the team use `team_key TEXT NOT NULL` ('*' installation-wide). The `actions` table itself (the case,
§11.1) is owned by watchover.actions."""
from __future__ import annotations

from typing import Dict, List, Tuple

# table -> column definitions; "{pk}" is the dialect's serial key (Database.pk)
TABLES: Dict[str, str] = {
    # 11.2 triggers, evidence, proposals, decisions
    "act_triggers": """id {pk}, case_id INTEGER, ts TEXT, source TEXT, key TEXT, host TEXT, severity TEXT, provenance TEXT,
        facts TEXT DEFAULT '{{}}', refs TEXT DEFAULT '[]'""",
    "act_evidence": """id {pk}, case_id INTEGER NOT NULL, eid TEXT NOT NULL, kind TEXT, ts TEXT, host TEXT, agent TEXT,
        provenance TEXT, text TEXT, fields TEXT DEFAULT '{{}}', slot TEXT DEFAULT '', value TEXT DEFAULT '', vtype TEXT DEFAULT '',
        source_eid TEXT DEFAULT '', ref TEXT DEFAULT '{{}}', quarantined INTEGER DEFAULT 0, qreason TEXT DEFAULT '', sha256 TEXT,
        UNIQUE (case_id, eid)""",
    "act_frozen_inputs": """id {pk}, case_id INTEGER NOT NULL, attempt INTEGER NOT NULL, input_sha TEXT, prompt TEXT,
        prompt_tokens INTEGER, aliases TEXT, value_labels TEXT, created_at TEXT, audit_seq INTEGER, UNIQUE (case_id, attempt)""",
    "act_proposals": """id {pk}, case_id INTEGER, attempt INTEGER DEFAULT 1, input_sha TEXT, selector TEXT, role TEXT,
        model_digest TEXT DEFAULT '', candidates TEXT, aliases TEXT, choice TEXT, runbook_key TEXT DEFAULT '', version INTEGER,
        params TEXT DEFAULT '{{}}', cite TEXT DEFAULT '[]', explain TEXT DEFAULT '', score REAL DEFAULT 0, valid INTEGER DEFAULT 1,
        repaired INTEGER DEFAULT 0, late INTEGER DEFAULT 0, invalid_reasons TEXT DEFAULT '', latency_ms INTEGER DEFAULT 0,
        sealed_hash TEXT, sealed_at TEXT, audit_seq INTEGER, revealed_at TEXT DEFAULT ''""",
    "act_decisions": """id {pk}, case_id INTEGER NOT NULL, attempt INTEGER NOT NULL, actor TEXT, choice TEXT,
        runbook_key TEXT DEFAULT '', version INTEGER, params TEXT DEFAULT '{{}}', reason_code TEXT DEFAULT '', note TEXT DEFAULT '',
        blind INTEGER, peeked INTEGER, agree_rules TEXT DEFAULT 'n/a', agree_model TEXT DEFAULT 'n/a', decided_at TEXT,
        level_at_decision INTEGER, UNIQUE (case_id, attempt)""",
    # 11.3 templates, actuators, runbooks, drafts
    "act_templates": """id TEXT NOT NULL, sha256 TEXT NOT NULL, rev INTEGER, os TEXT, kind TEXT, risk TEXT,
        reversibility TEXT DEFAULT '', cost TEXT DEFAULT 'light', body TEXT, title_tr TEXT, title_en TEXT, human_hint TEXT DEFAULT '',
        parser TEXT DEFAULT '', source TEXT, status TEXT, approved_by TEXT DEFAULT '', approved_at TEXT DEFAULT '',
        pack TEXT DEFAULT '', executor TEXT DEFAULT 'agent', lint TEXT DEFAULT '[]', justification TEXT DEFAULT '',
        PRIMARY KEY (id, sha256)""",
    "act_actuators": """agent_id INTEGER NOT NULL PRIMARY KEY, host TEXT, os TEXT, version TEXT, token_sha256 TEXT NOT NULL UNIQUE,
        pubkey TEXT, fingerprint TEXT, key_status TEXT, confirmed_by TEXT DEFAULT '', confirmed_at TEXT DEFAULT '',
        caps_sha256 TEXT, templates TEXT DEFAULT '[]', policy TEXT DEFAULT '{{}}', freeze_sha256 TEXT DEFAULT '',
        cosign_keys INTEGER DEFAULT 0, insecure_install INTEGER DEFAULT 0, allowlist_ok INTEGER, halt INTEGER DEFAULT 0,
        ledger_n INTEGER DEFAULT 0, ledger_head TEXT DEFAULT '', last_seq INTEGER DEFAULT 0, clock_skew_s INTEGER DEFAULT 0,
        last_hello TEXT, last_poll TEXT, next_seq INTEGER DEFAULT 1""",
    "act_runbooks": """id {pk}, key TEXT NOT NULL UNIQUE, team_id INTEGER, title TEXT, summary TEXT DEFAULT '', origin TEXT,
        origin_ref TEXT DEFAULT '', playbook_key TEXT DEFAULT '', status TEXT DEFAULT 'active', current_version INTEGER,
        killed INTEGER DEFAULT 0, created_by TEXT, created_at TEXT, updated_at TEXT""",
    "act_runbook_versions": """id {pk}, runbook_id INTEGER NOT NULL, version INTEGER NOT NULL, status TEXT, body TEXT,
        body_sha256 TEXT, step_hashes TEXT, risk TEXT, risk_effective TEXT, author TEXT, approval_id INTEGER, lint TEXT DEFAULT '[]',
        dry_run_ref TEXT DEFAULT '', tabletop INTEGER DEFAULT 0, change_note TEXT DEFAULT '', created_at TEXT,
        submitted_at TEXT DEFAULT '', decided_at TEXT DEFAULT '', UNIQUE (runbook_id, version)""",
    "act_drafts": """id {pk}, team_id INTEGER, source TEXT, signature TEXT, cases TEXT DEFAULT '[]', operators INTEGER, days INTEGER,
        ok_share REAL, skeleton TEXT, draft TEXT DEFAULT '', status TEXT, runbook_id INTEGER, created_at TEXT""",
    "act_playbook_links": """playbook_key TEXT NOT NULL, runbook_key TEXT NOT NULL, created_by TEXT, ts TEXT,
        PRIMARY KEY (playbook_key, runbook_key)""",
    # 11.4 runs, steps, jobs, approvals
    "act_runs": """id {pk}, case_id INTEGER, runbook_id INTEGER, version_id INTEGER, team_id INTEGER, host TEXT,
        hosts TEXT DEFAULT '[]', env TEXT, env_tier TEXT, host_class TEXT, criticality TEXT, start_mode TEXT, started_by TEXT,
        selector TEXT DEFAULT '', proposal_id INTEGER, params TEXT, param_sources TEXT, status TEXT, outcome TEXT DEFAULT '',
        label TEXT DEFAULT '', run_level INTEGER, mode_reasons TEXT DEFAULT '[]', change_ref TEXT DEFAULT '', veto_until TEXT DEFAULT '',
        audit_sample INTEGER DEFAULT 0, audit_verdict TEXT DEFAULT '', reviewed_by TEXT DEFAULT '', started_at TEXT,
        ended_at TEXT DEFAULT '', watch_until TEXT DEFAULT '', parent_run INTEGER""",
    "act_steps": """id {pk}, run_id INTEGER, seq INTEGER, step_key TEXT, type TEXT, step_hash TEXT, template_id TEXT DEFAULT '',
        template_sha256 TEXT DEFAULT '', executor TEXT DEFAULT '', mode INTEGER, binding TEXT DEFAULT '[]', status TEXT,
        params TEXT DEFAULT '{{}}', params_proposed TEXT DEFAULT '{{}}', params_source TEXT DEFAULT '{{}}', job_hash TEXT DEFAULT '',
        preview TEXT DEFAULT '', human_cmd TEXT DEFAULT '', approval_id INTEGER, job_id TEXT DEFAULT '', ext_ref TEXT DEFAULT '',
        performed_by TEXT DEFAULT '', human_claim TEXT DEFAULT '', deviation TEXT DEFAULT '', started_at TEXT DEFAULT '',
        ended_at TEXT DEFAULT '', exit_code INTEGER, fields TEXT DEFAULT '{{}}', display_tail TEXT DEFAULT '',
        display_until TEXT DEFAULT '', output_sha256 TEXT DEFAULT '', signed INTEGER DEFAULT 0, captures TEXT DEFAULT '{{}}',
        dry_output TEXT DEFAULT '', verify TEXT DEFAULT '{{}}', post_rollback_verify TEXT DEFAULT '{{}}', outcome TEXT DEFAULT '',
        side_effect TEXT DEFAULT '{{}}', note TEXT DEFAULT ''""",
    "act_host_locks": """host TEXT NOT NULL PRIMARY KEY, run_id INTEGER, step_id INTEGER, since TEXT""",
    "act_jobs": """id {pk}, job_id TEXT NOT NULL UNIQUE, step_id INTEGER, agent_id INTEGER, host TEXT, seq INTEGER, mode TEXT,
        executor TEXT, envelope TEXT, envelope_sha256 TEXT, nonce TEXT NOT NULL UNIQUE, not_before TEXT, expires_at TEXT, state TEXT,
        reject_code TEXT DEFAULT '', leased_at TEXT DEFAULT '', started_at TEXT DEFAULT '', finished_at TEXT DEFAULT '',
        result TEXT DEFAULT '', result_sha256 TEXT DEFAULT '', result_sig_ok INTEGER, ledger_n INTEGER, ledger_head TEXT DEFAULT '',
        handle TEXT DEFAULT ''""",
    "act_approvals": """id {pk}, subject TEXT, subject_id INTEGER, team_id INTEGER, job_hash TEXT DEFAULT '', need INTEGER,
        four_eyes INTEGER DEFAULT 0, stepup INTEGER DEFAULT 0, requester TEXT, requested_at TEXT, expires_at TEXT, status TEXT,
        escalation INTEGER DEFAULT 0, notified TEXT DEFAULT '[]', reason TEXT DEFAULT '', requires TEXT DEFAULT '[]',
        decided_at TEXT DEFAULT ''""",
    "act_votes": """id {pk}, approval_id INTEGER NOT NULL, voter TEXT NOT NULL, decision TEXT, reason TEXT DEFAULT '', ts TEXT,
        stepup_at TEXT DEFAULT '', stepup_method TEXT DEFAULT '', shown_at TEXT, fast INTEGER DEFAULT 0, audit_seq INTEGER,
        tags TEXT DEFAULT '[]', void INTEGER DEFAULT 0, UNIQUE (approval_id, voter)""",
    "act_stepups": """id {pk}, user_id INTEGER, at TEXT, method TEXT, ok INTEGER""",
    # 11.5 trust
    "act_cells": """id {pk}, cell_key TEXT NOT NULL UNIQUE, team_id INTEGER, runbook_key TEXT, step_key TEXT, env TEXT,
        host_class TEXT, step_hash TEXT DEFAULT '', level INTEGER, state TEXT DEFAULT 'active', paused_until TEXT DEFAULT '',
        frozen_reason TEXT DEFAULT '', inherit_target INTEGER, promo_freeze_until TEXT DEFAULT '', level_since TEXT,
        level_audit_seq INTEGER, t REAL DEFAULT 0, s_eff REAL DEFAULT 0, f_eff REAL DEFAULT 0, n_raw INTEGER DEFAULT 0,
        n_sys INTEGER DEFAULT 0, last_event_at TEXT DEFAULT '', last_ok_at TEXT DEFAULT '', updated_at TEXT""",
    "act_trust_events": """id {pk}, ts TEXT, component TEXT, cell_key TEXT, unit_key TEXT, runbook_key TEXT, step_key TEXT,
        selector TEXT DEFAULT '', team_id INTEGER, env TEXT, host_class TEXT, host TEXT, performer TEXT DEFAULT '', run_id INTEGER,
        step_id INTEGER, case_id INTEGER, outcome TEXT, credit REAL, penalty REAL, omega REAL DEFAULT 1, blind INTEGER DEFAULT 0,
        level INTEGER, system_exec INTEGER DEFAULT 0, version_id INTEGER, independent_ok INTEGER DEFAULT 0, dismissed INTEGER DEFAULT 0,
        archived INTEGER DEFAULT 0, archive_reason TEXT DEFAULT ''""",
    "act_promotions": """id {pk}, cell_key TEXT, from_level INTEGER, to_level INTEGER, basis TEXT, need INTEGER, approval_id INTEGER,
        status TEXT, created_at TEXT, decided_at TEXT DEFAULT '', expires_at TEXT""",
    "act_level_events": """id {pk}, cell_key TEXT, from_level INTEGER, to_level INTEGER, kind TEXT, reason TEXT, actor TEXT, ts TEXT,
        audit_seq INTEGER""",
    # 11.6 learning
    "act_examples": """id {pk}, task TEXT, src TEXT, tier TEXT, weight REAL, split TEXT, week TEXT, team_id INTEGER, case_id INTEGER,
        runbook_key TEXT DEFAULT '', host_class TEXT, op TEXT DEFAULT '', labeller TEXT DEFAULT '', input_sha TEXT, target_sha TEXT,
        messages TEXT, tokens INTEGER, rejected TEXT DEFAULT '', excluded INTEGER DEFAULT 0, reason TEXT DEFAULT '', created_at TEXT""",
    "act_datasets": """id {pk}, built_at TEXT, train_run_id INTEGER, policy TEXT, counts TEXT, ids_sha256 TEXT, card TEXT, canary TEXT,
        status TEXT, retired_models TEXT DEFAULT '[]'""",
    "act_dataset_examples": """dataset_id INTEGER NOT NULL, example_id INTEGER NOT NULL, PRIMARY KEY (dataset_id, example_id)""",
    "act_golden": """id {pk}, team_id INTEGER, scope TEXT, runbook_key TEXT DEFAULT '', input TEXT, input_sha TEXT NOT NULL UNIQUE,
        expected TEXT, safety INTEGER DEFAULT 0, hard_negative INTEGER DEFAULT 0, labeled_by TEXT, confirmed_by TEXT DEFAULT '',
        confirmer_policy INTEGER DEFAULT 0, status TEXT, version INTEGER, source_case INTEGER, created_at TEXT""",
    "act_models": """id {pk}, tag TEXT NOT NULL UNIQUE, digest TEXT, status TEXT, train_run_id INTEGER, dataset_id INTEGER, gate TEXT,
        perf TEXT DEFAULT '{{}}', shadow_from TEXT DEFAULT '', shadow_cases INTEGER DEFAULT 0, b INTEGER DEFAULT 0, c INTEGER DEFAULT 0,
        cusum REAL DEFAULT 0, mu0 REAL DEFAULT 0, cusum_wrong TEXT DEFAULT '{{}}', promoted_by TEXT DEFAULT '', promoted_at TEXT DEFAULT '',
        note TEXT DEFAULT ''""",
    "act_model_cache": """digest TEXT NOT NULL, input_sha TEXT NOT NULL, output TEXT, latency_ms INTEGER, created_at TEXT,
        PRIMARY KEY (digest, input_sha)""",
    "act_metrics_weekly": """week TEXT NOT NULL, team_key TEXT NOT NULL, team_id INTEGER, runbook_key TEXT NOT NULL DEFAULT '',
        selector TEXT NOT NULL DEFAULT '', metric TEXT NOT NULL, value REAL, n INTEGER,
        PRIMARY KEY (week, team_key, runbook_key, selector, metric)""",
    "act_matcher_weights": """id {pk}, ts TEXT, weights TEXT, eval_rb1 REAL, adopted INTEGER, role TEXT""",
    # 11.7 policy, safety, audit
    "act_policy": """key TEXT NOT NULL PRIMARY KEY, value TEXT, updated_by TEXT, updated_at TEXT""",
    "act_team_policy": """team_id INTEGER NOT NULL PRIMARY KEY, body TEXT, updated_by TEXT, updated_at TEXT""",
    "act_windows": """id {pk}, team_id INTEGER, name TEXT, kind TEXT, env TEXT, selector TEXT DEFAULT '{{}}', rule TEXT,
        enabled INTEGER DEFAULT 1, created_by TEXT, created_at TEXT""",
    "act_oncall": """id {pk}, team_id INTEGER, user_email TEXT, from_ts TEXT, to_ts TEXT, created_by TEXT""",
    "act_kill": """id {pk}, scope TEXT, ref TEXT, team_id INTEGER, active INTEGER, hard INTEGER DEFAULT 0, by_ TEXT, reason TEXT, ts TEXT,
        resumed_by TEXT DEFAULT '', resumed_at TEXT DEFAULT '', resume_reason TEXT DEFAULT '', resume_approval_id INTEGER""",
    "act_connectors": """id {pk}, kind TEXT, name TEXT NOT NULL UNIQUE, base_url TEXT, secret TEXT, ca TEXT DEFAULT '',
        team_ids TEXT DEFAULT '[]', allowed TEXT, allowed_hosts TEXT DEFAULT '[]', verified_by TEXT DEFAULT '', verified_at TEXT DEFAULT '',
        enabled INTEGER DEFAULT 1, last_ok TEXT DEFAULT '', last_error TEXT DEFAULT ''""",
    "act_channels": """team_id INTEGER NOT NULL PRIMARY KEY, kind TEXT, url TEXT, set_by TEXT, set_at TEXT""",
    "act_audit": """seq INTEGER NOT NULL PRIMARY KEY, ts TEXT, actor TEXT, actor_kind TEXT, event TEXT, object_kind TEXT,
        object_id TEXT, team_id INTEGER, payload_sha256 TEXT, prev_mac TEXT, mac TEXT""",
    "act_audit_payload": """seq INTEGER NOT NULL PRIMARY KEY, payload TEXT""",
    "act_audit_head": """id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1), seq INTEGER, mac TEXT""",
    "act_anchors": """id {pk}, seq INTEGER, mac TEXT, ts TEXT, sig TEXT, sinks TEXT DEFAULT '{{}}'""",
    "act_anchor_attest": """agent_id INTEGER NOT NULL, anchor_seq INTEGER NOT NULL, anchor_mac TEXT, ledger_head TEXT, sig TEXT,
        ts TEXT, PRIMARY KEY (agent_id, anchor_seq)""",
    "act_anchor_sinks": """id {pk}, kind TEXT, target TEXT, secret TEXT, enabled INTEGER DEFAULT 1, last_ok TEXT DEFAULT '',
        last_error TEXT DEFAULT '', set_by TEXT, set_at TEXT""",
    "act_actors": """pseudo TEXT NOT NULL PRIMARY KEY, email TEXT, created_at TEXT, erased_at TEXT DEFAULT ''""",
}

INDEXES: Tuple[Tuple[str, str, str], ...] = (
    ("ix_act_triggers_case", "act_triggers", "case_id"), ("ix_act_triggers_key", "act_triggers", "key, ts"),
    ("ix_act_evidence_case", "act_evidence", "case_id"), ("ix_act_proposals_case", "act_proposals", "case_id, attempt"),
    ("ix_act_runs_case", "act_runs", "case_id"), ("ix_act_runs_host", "act_runs", "host, status"),
    ("ix_act_runs_team", "act_runs", "team_id, started_at"), ("ix_act_steps_run", "act_steps", "run_id, seq"),
    ("ix_act_jobs_agent", "act_jobs", "agent_id, state, seq"), ("ix_act_trust_cell", "act_trust_events", "cell_key, component, ts"),
    ("ix_act_trust_unit", "act_trust_events", "unit_key, ts"), ("ix_act_examples_split", "act_examples", "split, week"),
    ("ix_act_approvals_status", "act_approvals", "status, team_id"), ("ix_act_kill_active", "act_kill", "active, scope"),
)

# tables with a team_id column that ownership.TEAM_TABLES moves on team delete / transfer
TEAM_OWNED = tuple(t for t, ddl in TABLES.items() if "team_id INTEGER" in ddl and "team_id INTEGER NOT NULL" not in ddl)
APPEND_ONLY = ("act_audit", "act_audit_payload", "act_anchors")      # triggers and restore handling arrive with WP2 (C11)


def ensure(db) -> List[str]:
    """Create every act_* table and index (idempotent; one transaction, so a fresh install pays one commit)."""
    created = []

    def run():
        have = set(db.tables())
        for name, cols in TABLES.items():
            if name not in have:
                db._exec(f"CREATE TABLE IF NOT EXISTS {name} ({cols.format(pk=db.pk)})")
                created.append(name)
        for ix, table, cols in INDEXES:
            db._exec(f"CREATE INDEX IF NOT EXISTS {ix} ON {table} ({cols})")
        if not db._exec("SELECT id FROM act_audit_head WHERE id=1"):
            db._exec("INSERT INTO act_audit_head (id, seq, mac) VALUES (1, 0, '')")

    try:
        if db.in_tx:
            run()
        else:
            with db.tx():
                run()
    except Exception:  # noqa: BLE001 - another process created a table first: retry once outside a transaction
        for name, cols in TABLES.items():
            try:
                db._exec(f"CREATE TABLE IF NOT EXISTS {name} ({cols.format(pk=db.pk)})")
            except Exception:  # noqa: BLE001
                pass
        for ix, table, cols in INDEXES:
            try:
                db._exec(f"CREATE INDEX IF NOT EXISTS {ix} ON {table} ({cols})")
            except Exception:  # noqa: BLE001
                pass
        if not db._exec("SELECT id FROM act_audit_head WHERE id=1"):
            db._exec("INSERT INTO act_audit_head (id, seq, mac) VALUES (1, 0, '') ON CONFLICT DO NOTHING")
    return created


def act_tables(db) -> List[str]:
    return [t for t in db.tables() if t.startswith("act_")]


def counts(db) -> Dict[str, int]:
    return {t: db.count(t) for t in sorted(act_tables(db))}


def key_columns(db, tables=None) -> List[Tuple[str, str, str, bool]]:
    """(table, key kind, column, nullable) for every column of a primary or unique key (C9 introspection).
    A SQLite INTEGER PRIMARY KEY (the rowid alias of a serial key) cannot hold NULL and is reported as not nullable."""
    tables = list(tables or act_tables(db))
    out = []
    if db.pg:
        for t in tables:
            rows = db._exec(
                "SELECT tc.constraint_type AS kind, kcu.column_name AS col, c.is_nullable AS nullable "
                "FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu "
                "ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema AND tc.table_name = kcu.table_name "
                "JOIN information_schema.columns c ON c.table_schema = tc.table_schema AND c.table_name = tc.table_name "
                "AND c.column_name = kcu.column_name WHERE tc.table_schema = current_schema() AND tc.table_name = ? "
                "AND tc.constraint_type IN ('PRIMARY KEY', 'UNIQUE')", (t,))
            out += [(t, r["kind"], r["col"], r["nullable"] == "YES") for r in rows]
        return out
    for t in tables:
        info = db._exec(f"PRAGMA table_info({t})")
        notnull = {r["name"]: bool(r["notnull"]) for r in info}
        pk = [r for r in info if r["pk"]]
        for r in pk:
            rowid = len(pk) == 1 and (r["type"] or "").upper() == "INTEGER"
            out.append((t, "PRIMARY KEY", r["name"], not (notnull[r["name"]] or rowid)))
        for ix in db._exec(f"PRAGMA index_list({t})"):
            if ix["unique"] and ix["origin"] == "u":
                for c in db._exec(f"PRAGMA index_info({ix['name']})"):
                    out.append((t, "UNIQUE", c["name"], not notnull.get(c["name"], False)))
    return out
