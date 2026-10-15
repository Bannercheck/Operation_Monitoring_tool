# Red-team review — log-driven action agent

Status: security review, 2026-10-15. Target: `docs/design/action-agent.md` (the merged design). This is a design-only
review: no code changed. Reviewer stance: adversarial. Each row says the attack, its impact, whether the design as written
stops it, and the exact fix. "Stops it" means the design already closes the hole; "Partial" means a real residual remains;
"No" means the attack works against the design as written. Code facts were re-checked against the working tree
(`agent.py`, `src/watchover/{live,privacy,agents,teams,autotrain,ownership,rbac,vault}.py`, `receiver/main.go`,
`scripts/agent-install.sh`, the Dockerfiles and `docker-compose.yml`) at commit `1acbe0a` plus the uncommitted team work.

## Verdict summary

| # | Attack | Design status | Worst residual | Sev |
|---|---|---|---|---|
| 1 | Prompt injection through log content | **Stops it** (bound) | wrong-but-valid proposal on a bounded case | Low |
| 2 | Parameter smuggling (path, glob, unit, pid, host, unicode) | **Partial** | broad `unit`/`iis` patterns; pid recycle; host→AWX `limit` | Med |
| 3a | Compromised API server (A7) — execution | **Partial** (acknowledged) | every allowlisted template on every host, within local limits | High |
| 3b | Compromised API server (A7) — **audit/anchor forgery** | **No** | A7 holds the audit key *and* the anchor key: forge + re-anchor | High |
| 3c | Key custody vs leaked backup (A5) | **No** | dispatch/audit keys sit on the `/data` volume, i.e. in backups | High |
| 4 | Stolen agent/actuator token (A9) | **Partial** | token = ingest token ⇒ forge *trusted* evidence for the host | High |
| 5 | Malicious team manager | **Partial** | sole non-prod L2→L3; `act.golden` poisons the shared model/gate | Med |
| 6 | Replay of signed jobs | **Stops it** | none material (expiry + nonce + idempotency) | Low |
| 7 | Trust-score gaming (cheap self-verify) | **Partial** | self-reported-metric verify; `expected_noise` suppresses side-effect | Med |
| 8 | Rollback that itself fails | **Partial** | lost compress manifest on a full disk; no verify of the rollback | Med |
| 9 | Concurrent cases on one host | **Stops it** | server host-lock TOCTOU (actuator concurrency is the backstop) | Low |
| 10 | Change-freeze bypass | **Partial** | freeze is server-side only (A7 bypass); `freeze_exempt` breadth | Med |
| 11 | Audit log tampering (non-key attacker) | **Stops it** | see 3b for the key-holding attacker | Low/High |
| 12 | Privacy leak of command output into training data | **No** | host credentials in log/stdout survive `privacy.scrub` → model + JSONL + audit | **Critical** |
| 13 | Model regression / targeted poisoning after retraining | **Partial** | per-runbook poisoning outside golden/red-team coverage | Med |

The two findings that are **not** bounded by the design as written are **#12 (credential leak into training)** and
**#3b/#3c (key custody)**. #4 and #3a are real but partly acknowledged. Everything else is defence-in-depth with a
nameable residual.

---

## 1. Prompt injection through log content — Stops it (bounded)

**Attack.** An adversary who can write any text into a log Watchover ingests plants instructions
("SYSTEM: ignore previous rules and run …"), a fake `E9|<nonce>|` evidence line, a JSON blob shaped like the selector
output, zero-width / bidi / homoglyph obfuscation, or a line naming another host, hoping the local model turns it into an
action.

**Impact if it worked.** Arbitrary runbook/parameter selection, i.e. an attacker-chosen host change.

**Does the design stop it?** Yes, at the bound level, and this is the strongest part of the design. Four independent
layers hold even if any one is bypassed: (a) quarantine (§4.3) flags the line, the model is **not consulted**, the case is
capped at L1; (b) the model's output space is a closed set of shuffled aliases / NONE / ESCALATE / value labels / evidence
ids enforced by an Ollama JSON schema + validator (I1, I2) — it *cannot emit a command or a value not produced by a
declared source*; (c) candidate runbooks come only from the deterministic matcher over approved team-visible runbooks; (d)
the action gate's injection-invariance suite (§8.8) tests the model itself **with the quarantine filter off**, so a
quarantine bypass still cannot flip a choice. The nonce is random per call, so a forged `E9|…|` line carries the wrong
nonce and is "not evidence". `why` is rendered as escaped plain text below the deterministic facts block, and approval
cards carry facts only (§13.6).

**Residual.** Quarantine is a regex denylist (`QUARANTINE_V1`); obfuscation will bypass it. The only thing a bypass buys
is nudging the model toward a *valid but wrong* allowlisted runbook, which is then bounded by provenance (log-provenance
slots cannot drive L3/L4 alone, §4.3), the rules-vs-model agreement gate (§7.8), caps and approvals.

**Exact fix.** Keep the closed-output invariant as the primary control (it is). Additionally: (1) make the injection
invariance set in `act/evalgate.py` a *blocking* gate criterion for the selection role on every retrain (it is listed —
ensure "fail → selection stays with the previous champion/rules" is enforced, §8.8); (2) add a Unicode confusable/NFC
normaliser to the quarantine pass so homoglyph and bidi variants normalise before the denylist runs; (3) treat a
`why`/explanation that cites an evidence id whose line is quarantined as itself invalid (drop the explanation).

## 2. Parameter smuggling — Partial

**Attack.** Push a hostile value through a typed parameter: `..`/glob/leading-`-`/NUL in a `path`; a systemd template unit
`orders@evil.service`; a recycled or spoofed `pid`; a `host`/`limit` that redirects a remote (AWX) job; Unicode look-alikes
for path separators; a shell metacharacter hoping for a shell.

**Impact.** Acting on an unintended file/unit/process/host.

**Does the design stop it?** Mostly. §5.5 is strict and well-matched to the code: `path` rejects `..`, glob chars
`*?[]{}`, leading `-`, NUL/newline, normalises, length ≤ 255, and the actuator re-runs `realpath` and re-checks the root
(symlink escape); everything is argv, never a shell; `argv[0]` is never a shell/interpreter; built-ins replace pipes;
Windows uses `list2cmdline` with `"`/`^` excluded from charsets; predicates are re-checked host-side; `host` is
scope-checked server-side and **never sent to the actuator** (the actuator acts on itself). WP4 explicitly tests `..`, NUL,
newline, leading `-`, glob, Unicode look-alikes and symlink escape. So raw shell injection and path escape are closed.

**Residuals (real).**
- **Over-broad per-template `pattern`.** `unit`/`win_service`/`iis_pool` safety depends on an author-written regex (e.g.
  `^(tomcat|orders|billing)…\.service$`). A loose pattern lets a log-provenance slot value select an unintended unit
  within the prefix. Capped at L1/L2 for log provenance, but still an unwanted restart.
- **pid recycling (TOCTOU).** A `pid` is captured by a diagnose step and re-checked for existence + `pid_exe` right before
  the kill, but a dead-and-recycled pid pointing at a *different* process with the *same* exe (two `java`/`disp+work`
  processes) passes the check. Relevant to `linux.proc.kill` and `sap.wp.cancel` (both R3).
- **Host smuggling into HTTP executors.** The agent `host` never reaches the actuator, but the AWX/Rundeck example binds
  `"limit": "{host}"` / `"filter": "name: <host>"`, which *is* sent to a remote orchestrator. If the case host is forged
  (see #4) the remote job can target a different host inside the connector's inventory.

**Exact fix.** (1) Lint warns (and requires reviewer justification) on `unit`/`win_service`/`iis_pool` patterns broader
than a bounded character budget or without an anchored service-name prefix; require a log-provenance `unit`/`pool` slot to
be corroborated by a `trusted` diagnose (`unit_exists` + the name appearing in `systemctl`/`appcmd` output) before any
autonomous (L3/L4) restart — fold this into the §4.3 corroboration rule. (2) Capture and re-validate a `(pid, start_time)`
pair, not just exe, and reject if the process start time changed since the diagnose capture. (3) The HTTP executor must
validate the rendered `limit`/filter against the *scoped* case host and the connector's allowed-host set, and refuse a host
not in both; the target is already "fixed in the runbook version, never a parameter" (§9.5) — enforce that in lint so a
`{host}` binding is only accepted when it resolves to the scope-checked case host.

## 3. Compromised Watchover server (A7)

### 3a. Execution reach — Partial (acknowledged)

**Attack.** Full control of the API process. The server holds `.act-dispatch.key`, so it signs job envelopes directly,
skipping every server-side control (trust, caps, approvals, kill, run gate — all of which are just server code it no longer
runs honestly).

**Impact.** A7 can dispatch **any template present in a host's allowlist, with any locally-valid parameters, on any host,
up to the host's local rate limits** — e.g. restart services fleet-wide, force log compression, or `sap.wp.cancel` where
installed. This is the design's residual #1 and the A7 row of §10.1.

**Does the design stop it?** No, and it correctly says so. The only remaining bound is host-side: the local allowlist
(server can never widen it), local validation, local limits (`max_act_per_hour` 20), the local halt file, and narrow
allowlists on critical hosts. Dispatch-key rotation does **not** help (A7 already holds the current key).

**Exact fix.** Make **co-signing (WP20) mandatory, not optional, for R2+ templates on `critical`/`high` hosts** before those
cells may exceed L2: a second signature from a key the API server does not hold (an approver's device / offline key) that the
actuator verifies. Until co-signing ships, document and default `local.json.max_risk: R0` (diagnose-only) on
`criticality in (critical, high)` hosts, so a server compromise cannot execute changes on the hosts that matter most. Ship
the residual-risk note (§10.1) as a *blocking* acknowledgement the `act.policy` holder must accept before enabling
`act_mode ≥ approve`.

### 3b. Audit and anchor forgery by a key-holding server — No (under-stated)

**Attack.** §9.3.1 places `.act-dispatch.key`, `.act-audit.key` (the HMAC key for the chain) **and** `.act-pseudo.key` all
"under `WATCHOVER_HOME`, mode 0600" on the API host. A7 reads all three. The audit chain's tamper-evidence (§10.7) rests on
the HMAC key being outside the attacker's reach — true for A5 (DB-only) but **false for A7**. With the audit key A7 can
rewrite `act_audit` rows and recompute valid `mac`/`prev_mac`; with the dispatch key it re-signs the hourly/again anchors
(`act_anchors`, `act-anchors.log`, the `auth_events` row). The append-only DB triggers only stop SQL-level edits, and A7 is
not limited to SQL.

**Impact.** A7 can forge a promotion to L4, a policy change, an approval/vote, or erase a kill event, and produce a chain +
anchors that `GET /api/act/audit/verify` reports as **green**. For events that never reached an actuator (promotions,
policy, approvals, label corrections, model promotions) there is *no* independent witness — the host ledgers only cover
dispatched jobs, and the external anchor file is "`chattr +a` recommended" / SIEM "optionally" (§10.7), i.e. not required.

**Does the design stop it?** No. The design's threat model treats audit tampering as an A5 concern (keyed chain beats a
DB-only attacker) and does not state that A7, which holds the keys, defeats the chain for non-dispatch events.

**Exact fix.** (1) **Separate key custody:** the audit HMAC key and the anchor-signing key must live on a different trust
boundary from the API process — a separate secret store / HSM / sidecar that signs anchors on request and never exports the
key — so an API-process compromise cannot recompute the chain. (2) **Mandatory external, append-only anchoring before L3:**
anchor every hour and on every kill/promotion/policy change to an independent-trust WORM/SIEM sink the API host cannot
rewrite, and make L3+ refuse to run while that sink is unreachable. (3) **Actuators co-sign the anchor:** actuators already
receive the anchor in `hello` and store it in their ledger — have them *sign* the head with their agent key (which A7 lacks)
so anchor continuity is attested by keys outside the API host. (4) The daily anchor email must go to a group outside the
administrators' control.

### 3c. Key material on the backup volume — No

**Attack.** `vault.key_path()` and the three act keys resolve under `settings.home()` / `WATCHOVER_HOME`, which in the
shipped `docker-compose.yml` is the `watchover-data:/data` volume — the same volume holding the database and the spool, and
the one an operator backs up. A5 is defined as "SQL access, leaked backup".

**Impact.** A leaked `/data` backup contains `.act-dispatch.key`, `.act-audit.key`, `.vault.key` and `.act-pseudo.key`.
That upgrades A5 from "read scrubbed rows, cannot sign or forge the chain" to **full dispatch-signing and audit-forging
capability** — directly contradicting the A5 bound ("no signing key in DB … forge audit rows (keyed MAC)"). "Outside the
DB" is not "outside the backup".

**Does the design stop it?** No. §9.3.1 only says the keys are files under `WATCHOVER_HOME`, mode 0600; the deployment puts
`WATCHOVER_HOME` on the backed-up data volume.

**Exact fix.** Store the act keys and the vault key on a **separate mount excluded from `/data` and from DB/volume backups**
(a dedicated `secrets` volume, a mounted secret, or an external secret manager), document that `WATCHOVER_HOME` keys must
not be co-located with database backups, and have `act doctor` warn when the key directory and the DB share a mount. Restate
the A5 bound as "cannot sign or forge *provided the key mount is not in the leaked artefact*".

## 4. Stolen agent / actuator token (A9) — Partial

**Attack.** An attacker steals a host's token (seen on the edge/receiver, from `agent.env`, or from a compromised but
not-root process). Per §9.3.2 the actuator's pull channel uses "the same base URL and token as `/ingest`", and
`receiver/main.go` / `live.py` authorise `/agent/v1/*` by that token.

**Impact.**
- **Job disclosure:** the attacker polls `GET /agent/v1/jobs` and reads that host's queued envelopes — template ids,
  parameters, approver pseudonyms, `audit_seq`. Information leak only (bounded to one host).
- **Evidence forgery (the real issue):** the same token works on `/ingest`, and `live.py::_accept` stamps
  `attributes["agent"] = <agent name>`, which §4.3 treats as **`trusted` provenance** ("metric or log line whose
  `attributes.agent` equals the target host"). So a stolen token lets the attacker fabricate *trusted* metrics and log
  lines for that host — e.g. "disk /var/log 96 %" or an OOM line — which produce trusted slots and can drive a case toward
  an autonomous action. This partially breaks I10: "trusted" is defined by the token-stamped identity, and the token is
  stolen. A1 (log author) is only supposed to reach `log` provenance; a token holder reaches `trusted`.

**Does the design stop it?** Partly. Posting a valid `hello` or job *result* needs the Ed25519 agent key (checked by
`svc.act.jobs`), not the token — so the attacker cannot confirm a new key or forge results. The autonomous path is still
gated by trust, agreement, approvals, veto, caps and loop/blast limits, and the action runs on the *real* host (the attacker
holds the token, not root on the host), so the worst outcome is a bounded, allowlisted, unnecessary change (mild
service-restart/compression DoS), not arbitrary execution. But the elevation of injected content from `log` to `trusted`
is a genuine weakening of I10.

**Exact fix.** (1) **Separate the actuator pull token from the ingest token** with distinct scopes, so a leaked
ingest/collector token cannot poll jobs and vice versa; the actuator transport should present the Ed25519 agent identity
(it already has the key) rather than relying on the bearer token for anything beyond rate-limiting. (2) For the
**autonomous (L3/L4) trigger slot**, require the triggering measurement to come from a *server-side* source (threshold
engine state, synthetic probe) rather than an agent-self-reported metric fact; a self-reported metric may open a case and
support guided/approved work but must be corroborated before it drives a system start. This keeps a stolen token from
single-handedly manufacturing the evidence that unlocks autonomy. (3) Rotate tokens on any `bad_sig`/ledger-mismatch alarm
for that host (the design already kills the host; also force token rotation).

## 5. Malicious team manager — Partial

**Attack.** A holder of `team.manage` (plus the manager template's `act.approve`, `act.publish`, `act.promote`,
`act.golden`, `act.audit`, `act.kill` per §10.9) turns their legitimate team authority against the system.

**Impact / what they can and cannot do.**
- **Cannot** alone promote a prod step to L3/L4 (two distinct signers; L3→L4 needs an installation `act.policy` holder),
  cannot approve their own request (requester ≠ approver, enforced for admins too, I6), cannot publish their own runbook
  (author ≠ reviewer), cannot widen a host allowlist, cannot add a webhook URL (`act_channels` is `act.policy`-only),
  cannot steal another team's host (`teams.claim` rejects `other_team`), cannot raise caps (lower only). These SoD rules
  are solid and mirror the existing RBAC escalation guards verified in `team-scope-audit.md`.
- **Can**, unilaterally: take any **non-prod** step cell and non-prod run unit to **L3 (supervised automatic)** — non-prod
  L2→L3 and run-unit L2→L3 need only **one** signer (§7.5) — and declare themselves on-call (`act.promote` → `POST
  /act/oncall`) to satisfy the L3 supervision gate. Bounded to their own team's hosts, but it removes the per-step human on
  non-prod with a single actor.
- **Can poison the shared model and the gate via `act.golden`.** A team manager adds golden items and relabels decided
  cases ("Etiketi düzelt", gold tier weight 2.0). The golden set is the frozen pass criterion of the cross-team action gate
  (§8.8) and gold examples are always kept uncapped (§8.7). Unless per-team adapters are used (an open question, §18.3),
  one shared model learns from every team, so a manager's golden/relabel choices steer behaviour on *other* teams' similar
  cases and shape what counts as "passing".

**Does the design stop it?** The execution-side SoD is strong; the **learning-side authority of a single team manager is
too broad**, and the sole-signer non-prod L3 is worth narrowing for critical hosts.

**Exact fix.** (1) A `act.golden` label correction or golden addition that can affect the **cross-team** model (i.e. while
per-team adapters are not in use) must be confirmed by an installation `act.policy` holder, not only a second team
labeller; per-team golden items may gate only that team's scope. (2) Require the **operator/manager whose decisions trained
or labelled** a case to be *excluded from the evaluation labels* used to score the model on that case (operator-disjoint
eval split) so one person cannot both teach and grade. (3) For `criticality in (critical, high)` hosts, require the second
signer even at **non-prod** L2→L3, and require an installation `act.policy` holder (not `team.manage`) as one prod L3
signer. (4) An on-call entry a manager creates for themselves should not satisfy the L3 "supervisor present" gate for a run
that same manager started (reuse the requester ≠ supervisor idea).

## 6. Replay of signed jobs — Stops it

**Attack.** Capture a validly signed job envelope (on the edge, A6, or from a compromised server, A7) and re-send it to the
actuator later, or re-order queued jobs, to re-execute a change.

**Impact if it worked.** Duplicate or out-of-order host changes (e.g. a second restart, a second compression).

**Does the design stop it?** Yes. Four overlapping controls (§9.3.3–§9.3.4): (a) `expires_at = not_before + 5 min` (act) /
10 min (diag) with a 120 s skew tolerance — a replayed envelope is almost always already expired (check #3,
`not_yet`/`expired`); (b) `nonce`/`job_id` recorded in `seen.json` for 7 days → `duplicate` returns the *stored* result,
nothing re-runs (check #4); (c) the server accepts one result per `job_id` (idempotent), and results are agent-signed so a
captured result cannot be re-pointed at a new job; (d) `clock_skew_below(120 s)` caps the host at L1 and names the skew, so
clock rollback to defeat expiry is itself detected.

**Residual.** `seen.json` lives in the host state dir; a reinstall/wipe resets it, but expiry still protects (the window is
minutes, the nonce window is days). Envelope `seq` is present but the actuator is not required to enforce monotonicity.

**Exact fix (hardening only).** Have the actuator enforce **monotonic `seq`** per agent (reject a job whose `seq` ≤ the
highest already executed) so reordering/replay is refused even inside the expiry window, and persist the last-executed
`(seq, nonce)` in the signed ledger so it survives a `seen.json` loss. No structural change needed.

## 7. Trust-score gaming — Partial

**Attack.** Inflate `T_exec`/`T_sel` with cheap, self-confirming runs to reach L3/L4, either to earn autonomy for a step
that later turns harmful, or to make a risky step look proven.

**Impact.** Unattended execution of a step that was never really validated.

**Does the design stop it?** Largely. The design is deliberately defensive: outcomes not clicks (`unverified` earns 0,
k 0.25; a "Yaptım" claim never earns trust, §7.3); the verify lint demands ≥ 1 server-side check or a diag template;
L2→L3 requires **system-executed** runs by ≥ 2 performers across ≥ 3 hosts with corrections < 10 %; selection trust is
measured **blind** with asymmetric false-action cost and a κ ceiling that a dominant runbook cannot inflate; visible
agreements are ignored (anchoring); the host weight cap limits one host's contribution; promotions need human signatures
and dry-run proof. Residual #6 ("weak `expect` → weak score") is acknowledged.

**Residuals (real).**
- **Self-reported-metric verify.** A `verify` built on `metric_below` reads the *host's own* reported metric. A compromised
  host (A8, "can lie about its own results and metrics") makes every verify pass and, in a small host class, carries up to
  50 % of the cell weight (`max(0.25, 1/N)` with N = 2). It thus manufactures a clean record for its own cell.
- **`expected_noise` suppresses side-effect detection.** The side-effect watch criteria (a) error-rate rise and (b) new
  ERROR templates both *exclude* the runbook's `expected_noise` list (§4.7). A careless or malicious author widens
  `expected_noise`/`expected_noise_s` so the action's own fallout is treated as expected → no `side_effect` label → no
  demotion → inflated trust. (Criteria (c) new anomaly, (d) agent silence, (e) SLO drop are not excludable — good — so the
  suppression is partial, but real for error-signature side effects.)

**Exact fix.** (1) To *promote* a cell above L2 (unattended), require at least one verify check whose data source is
**independent of the acting host** — a synthetic probe from the server/another host, an SLO from aggregated clients, or a
log-signature observed by a different collector; a self-reported-metric verify may keep a cell guided/approved but must not
count toward L3/L4 promotion evidence. (2) Lint-bound `expected_noise` (a small reviewer-justified allowlist), always keep
criteria (c)/(d)/(e) active regardless of `expected_noise`, and record *suppressed* side-effect hits so a reviewer can see
what was masked. (3) Lower the single-host weight floor for *promotion* evidence (e.g. a class with < 3 hosts cannot reach
L4 — the design already caps rare runbooks by saturation; make "≥ 3 distinct hosts" a hard L3 prerequisite, which §7.5
already lists — enforce it in the promotion gate, not only on the screen).

## 8. Rollback that itself fails — Partial

**Attack.** Force the failure path and then make the rollback fail, or make a rollback report success while the host is
still broken.

**Impact.** A host left in an unknown/degraded state after an automated change.

**Does the design stop it?** The *declared-failure* path is handled: §10.6 and failure mode #30 escalate a failed rollback
to `escalated` + P1 + a host-level act-kill until a person clears it ("Sunucunun durumu bilinmiyor"), and the run state
machine has `rollback_failed → escalate`. `rolled_back` costs k 3 and demotes to L1.

**Residuals (real).**
- **Lost rollback manifest on a full disk.** `compress_older_than` writes its file list to `state/compress-<job_id>.json`
  and `decompress_list` restores *from that list*. The remediation runs precisely when a disk is full; if the manifest
  write fails or is partial, the rollback has nothing to enumerate. The state dir may itself be on the affected filesystem.
- **No verify of the rollback.** A rollback that exits 0 but does not actually restore service (e.g. decompress ran but the
  disk is still full because the real cause was deleted-open files) is recorded as `rolled_back` — the system believes it
  recovered and does not escalate.

**Exact fix.** (1) Write the rollback manifest (pre-image/journal) **before** mutating, `fsync` it, verify free space for
it, and **fail closed** (refuse the act) if the manifest cannot be durably written; provide a manifest-independent fallback
for `data_preserving` steps (decompress can re-derive the set as "`*.gz` newer than the run start under the root"). (2)
Require a **verify step after every rollback** that re-checks the original failure condition; a failed post-rollback verify
must escalate as `rollback_failed`, not settle as `rolled_back`. (3) Keep the host act-kill until the post-rollback verify
passes, not merely until the rollback command returns.

## 9. Concurrent cases on the same host — Stops it

**Attack.** Two cases (or two operators clicking "Çalıştır") drive two act steps onto one host at once to interleave
changes.

**Impact.** Racing changes, corrupted host state.

**Does the design stop it?** Yes. The per-host lock (§10.4, "Active act steps per host = 1"), the `no_other_run_on_host`
precondition (`else: stop`, naming the holder), and the single `act-engine` dispatch thread serialise acts; diagnosis is
read-only and may run concurrently. The decisive backstop is **host-side**: the actuator's `local.json act_concurrency: 1`
(check #9, `busy`) refuses a second concurrent act regardless of server state, and a host is owned by at most one team so
there is no cross-team race. Failure mode #20 covers it.

**Residual.** The server-side lock is a check; if it is not acquired in the *same* transaction that flips the step to
`dispatched`, two near-simultaneous L2 "Çalıştır" calls could both pass the check (TOCTOU). The actuator concurrency limit
still prevents double execution, but one job would be rejected late (`busy`) rather than cleanly queued.

**Exact fix.** Acquire the host lock inside `Database.tx()` in the same transaction that transitions the step to
`dispatched` (`SELECT … FOR UPDATE` on PostgreSQL, `BEGIN IMMEDIATE` under the process lock on SQLite), so the lock is
atomic with dispatch; keep the actuator's `act_concurrency: 1` as the authoritative backstop.

## 10. Change-freeze bypass — Partial

**Attack.** Execute a change during a declared freeze or outside a required window.

**Impact.** Unauthorised change during a business-critical freeze.

**Does the design stop it?** Server-side, yes: the `window` cap in `effective_level` forces L1 while `frozen(host, now)`,
preconditions are re-checked right before each act step's dispatch (so a freeze starting mid-run caps later steps), an
unreadable calendar treats production as frozen, and "Acil değişiklik" needs four-eyes + a reason + (when configured) an
ITSM emergency change.

**Residuals (real).**
- **Freeze is a server-side-only control.** The actuator only knows `local.json` `hours` (daily windows) and `max_risk`; it
  has **no freeze-date calendar**. A compromised server (A7, #3a) therefore bypasses change-freeze entirely within the
  host's local `hours`/`max_risk`. Freeze provides no defence in depth against the one adversary (A7) most likely to abuse
  it.
- **`freeze_exempt` breadth.** §6.7 says `freeze_exempt` is "meant for diagnose-only runbooks" but the design does not state
  that it is *enforced* to contain only R0 diagnose/verify steps. A malicious author who can get the one-time four-eyes to
  set the flag could attach it to a runbook with act steps and bypass freeze without per-execution four-eyes.

**Exact fix.** (1) Let `local.json` carry freeze date ranges (host-side), and for `criticality in (critical, high)` hosts
have host automation set `local.json.max_risk: R0` for the freeze window, so freeze is enforced on the host, not only the
server. (2) Lint-enforce that `freeze_exempt` is accepted **only** when every step of the runbook is diagnose/verify (R0);
any act step forces the per-execution "Acil değişiklik" four-eyes path instead.

## 11. Audit log tampering — Stops it (non-key attacker); see #3b for the key holder

**Attack.** Edit, delete, reorder or forge audit rows to hide a promotion, a kill, an unsafe action or a policy change.

**Impact.** Loss of accountability; a forged promotion looks legitimate.

**Does the design stop it?** Against a **DB-only / SQL attacker (A5)**, yes: the keyed HMAC chain (key outside the DB),
append-only PostgreSQL/SQLite triggers, the head lock (single writer, audit-before-act in the same transaction so a failed
audit write blocks the action), hourly + event anchors, and `GET /api/act/audit/verify` cross-checking the chain against
the anchor file, `auth_events` and actuator ledgers — any edit surfaces as "Zincir kırık: #n", caps L2 everywhere until two
`act.policy` holders acknowledge. This is a strong design for the A5 case and is tested in WP2.

**Residual.** A **key-holding attacker (A7)** defeats it for non-dispatch events — see **finding #3b** and its fix. Also
the append-only triggers do not stop a filesystem/root attacker rewriting the SQLite file (residual #4, acknowledged;
anchors + ledgers detect, cannot prevent).

**Exact fix.** As #3b: move the audit/anchor keys off the API host's trust boundary, make external append-only anchoring to
an independent sink mandatory before L3, and have actuators co-sign the anchor head with keys the API host does not hold.

## 12. Privacy leak of command output into training data — Critical (not bounded)

**Attack.** A diagnose or guided step's output, or pasted "Çıktıyı yapıştır" text, contains a host-side secret that is not
in Watchover's vault — a database/service credential in a connection string, a keystore or environment password printed on
error, a session cookie, an access-key id, or a private-key block — surfaced by templates such as `linux.log.tail`
(tails an arbitrary app log under the template root) or any parser's `stdout_tail`. The design relies on `privacy.scrub`
(embedded in the actuator and re-run on the server, §9.3.5, §8.7 step 4, §10.8) to strip it before it becomes evidence, a
selector prompt, a training example, or an audit payload.

**Impact.** `privacy.scrub` (in `src/watchover/privacy.py`) is a **denylist** that masks only labelled `password|token|
secret|api_key|authorization` `key=value`/`key:"value"` forms, `Bearer` tokens ≥ 12 chars, e-mails, IBANs, Turkish phone
numbers, TCKN and card numbers. It does **not** mask:
- credentials in a URL *userinfo* position (`scheme://user:secret@host`) — only the `password=` query form is caught;
- environment-style or flag-style credentials whose key has no word boundary before the matched word
  (`PGPASSWORD=…`, `-DkeyStorePassword=…`, `client_secret=…`) or a space-separated value (`--password …`, `-p…`);
- high-entropy unlabelled secrets — cloud access-key ids, session-cookie values, opaque API keys that are not `Bearer`;
- private-key / certificate blocks (PEM).

The I11 test described in §10.8 only "greps … for **vault-decrypted** values" — i.e. it proves vault secrets do not leak,
but it does **not** test host-planted credentials that never passed through the vault. So a tailed application log that
prints a JDBC URL or a keystore password on error flows, unmasked, into (a) `act_evidence`, (b) the §8.3 selector **prompt
to the local model**, (c) the `act_select` **training example** (JSONL), and (d) the **audit payload** — and is then
**memorised in the fine-tuned model weights** (extractable by anyone who can query the model) and readable in the training
export by any `page.llm` holder. Delexicalisation (§8.7 step 3) rewrites host/service/path *tokens* but passes line
*content* through `privacy.scrub`, which is the weak link. This is both a credential-exfiltration and a KVKK exposure, and
it is the control the whole learning loop depends on.

**Does the design stop it?** No. Scrubbing twice with the same weak regex does not help, and the acceptance test only
covers vault values.

**Exact fix.** (1) **Allowlist, don't denylist, what becomes evidence/training.** For a template with a named parser, keep
only the **parser's structured captures** (e.g. `{files, bytes_before, bytes_after}`, `{use_pct, avail_gb}`) in evidence
and in training; treat raw `stdout_tail` as **display-only**, retained briefly, never an `act_select` input. A diagnose
template that returns free-form log text (`log_tail`) is marked `privacy: raw` and its output is excluded from training by
default and shown behind a reveal. (2) **Strengthen `privacy.scrub`** to cover URL userinfo, `PGPASSWORD`/env-password and
`-p`/`--password` flag forms, keystore/truststore passwords, PEM key/cert blocks, and a high-entropy-token heuristic (long
mixed-case/base64 runs adjacent to credential-ish keys), with parity between the actuator twin and the server copy (WP11
already requires scrub parity — extend the corpus). (3) **Fix the I11 test** to plant realistic host-side credential canaries
(DB URL, keystore password, access-key id, PEM header) in simulated command output and assert none appear in envelopes,
prompts, `act_examples`, `act_audit_payload`, memory lessons, or the training export — not only vault values. (4) Run the
same canary check as a **gate** on the built training dataset (`act_datasets`) before any LoRA run, and fail the dataset if
a canary survives.

## 13. Model regression / targeted poisoning after retraining — Partial

**Attack.** Degrade the selector via retraining: broad regression (worse everywhere) or targeted poisoning (systematically
mis-select for one runbook/case class while passing the gate). Levers: an insider operator's decisions (per-operator
training cap 20 %), a compromised host's self-verified "ok" outcomes (A8), collusion by two operators (defeating the
"≥ 2 performers / ≥ 2 operators" thresholds), or shaping the golden set via `act.golden` (see #5).

**Impact.** Autonomous selection of the wrong action on cases the attacker chose.

**Does the design stop it?** Broad and safety-relevant regression: largely yes, and this is a strong part of the design —
time-split eval, a frozen golden set, a shipped + customer red-team set, schema-validity ≥ 99 %, `UNSAFE = 0` required,
injection invariance with the filter off, per-runbook non-regression (RB@1 drop ≤ 0.05 on runbooks with ≥ 10 items), a
paired b/c binomial test against the champion, `FALSE-ACT ≤ max(0.02, champion)`, trust bound to the model version (nothing
inherited), run units capped at L2 until 20 live blind decisions under the new tag, and one-sided CUSUM on daily FALSE-ACT
that re-pins the previous champion. Without an LLM everything still works (I12).

**Residuals (real).**
- **CUSUM watches only FALSE-ACT** (proposing act when the human chose NONE). A poisoning that swaps one *act* runbook for
  another plausible *act* runbook (both "act", so not a false action) with valid parameters is caught only by RB@1/FULL
  drift and per-runbook regression **on golden items** — if that case class is absent from the golden/red-team set, it is
  missed until the ≤ 10 % L3/L4 audit sample happens to catch it.
- **Self-grading.** The eval labels are human decisions, some made by the same operators whose decisions trained the model;
  a colluding pair can both teach and (via their own later decisions) validate.
- **Golden-set authority** of a single team manager feeds the cross-team gate (finding #5).

**Exact fix.** (1) Require **per-runbook golden coverage** before that runbook's model-started runs may exceed L2: a runbook
with no frozen golden items may be matched by the model but not drive an autonomous start. (2) Add a **selection-stability
check across model versions** on the frozen golden set *per runbook* (not only aggregate RB@1) so a targeted per-class
regression fails the gate. (3) Use an **operator-disjoint eval split**: exclude a case from the evaluation labels when its
decision/label came from an operator who also contributed training examples for that runbook, so no one grades their own
teaching. (4) Extend CUSUM (or a second monitor) to **wrong-act drift** (FULL/RB@1 per runbook), not only FALSE-ACT. (5)
Gate golden/relabel authority as in #5 (installation `act.policy` confirmation for cross-team-affecting changes).

---

## Cross-cutting note on the threat model

The design's §10.1 table is good and honest about A1, A5, A6, A8, A9 and the A7 *execution* bound. The two places it
**under-states** the adversary are: (i) it treats the audit key as "outside the DB" (true) but not "inside the API host's
and the backup's reach" (also true) — so A5-with-a-backup and A7 both defeat the chain for non-dispatch events (findings
#3b, #3c, #11); and (ii) it treats self-reported host metrics/logs as `trusted` by token identity, so a stolen token (A9)
or a lying host (A8) can manufacture the "trusted evidence" that I10 and the run gate rely on (findings #4, #7). The single
highest-value fix is **#12** (stop host credentials reaching the model and the training export), because it turns every
diagnose step into a potential credential-exfiltration channel and the loss is permanent (baked into model weights). The
next is **key custody** (#3b/#3c): move the audit/anchor/dispatch keys off the API host's and the backup's trust boundary
and make external, actuator-attested anchoring mandatory before any cell reaches L3.
