# Review: `action-agent.md` against the real code

Status: review, 2026-10-15. Scope: `docs/design/action-agent.md` (the merged design) checked for fit and feasibility against the
working tree (commit `1acbe0a` plus uncommitted team work, read only). No code was changed. Every finding names the file and line
it was checked against. The trust numbers in §5 come from a simulation of the design's own formula (§7.4); the script is in
Appendix A. Speed figures for the local model are stated assumptions to be replaced by a measured probe (C27).

Severity: **B** blocker (wrong or unsafe as written; fix before the package is built) · **M** major (works, but costs
correctness, safety or weeks) · **m** minor (consistency, wording).

---

## Özet (TR)

* Tasarım doğru yönde ve mevcut koda büyük ölçüde oturuyor: §1'deki olguların hepsi doğrulandı (tek bağlantı + RLock,
  `actions` ayrı SQLite dosyası, `/enroll` vekili, yöneticilerin MFA'yı atlaması, `llm_cfg("ops")`, rastgele eğitim bölmesi,
  `rbac_meta` işaretleri). Güven formülü küçük sayılarda tutarlı: 0 çalışma → 0, 3 temiz → 0,53, 10 → 0,79, 50 → 0,95;
  monotonluk 200 000 rastgele denemede bozulmadı; §7.4 ve §7.10'daki sayılar tutuyor (bir yazım hatası dışında).
* **Engelleyici dokuz düzeltme:**
  1. Go ön kontrolü açık modda ve eski paylaşılan anahtarla gelen isteği de geçiriyor; iş kanalı yalnız ajan jetonu kabul etmeli.
  2. `actions` tablosunun taşınması `mcp_server.py`, `app.py`, `migrate.py` ve `admin.py`'yi atlıyor: MCP yeni boş bir
     `actions.db` açar, veri ikiye bölünür, geri alma (rollback) aksiyonları kaybeder.
  3. Kanal dışı kurulum: `agent.py` ve paketler düz HTTP ile sunucudan iniyor; aynı sunucudan gelen `SHA256SUMS` hiçbir şey
     kanıtlamaz.
  4. `privacy.scrub` yapılandırılmış çıktıyı bozuyor: `bytes_before: 5368709120` → `[TEL]` (denendi).
  5. Seçim çağrısı CPU'da 3B modelle 20 sn'ye sığmaz; tasarımın üst sınırlarıyla istem `num_ctx 4096`'yı aşar.
  6. `lora.train` uzun örneklerde yanıtı keser, etiketler boş kalır → kayıp NaN; eğitim kapsayıcısı (6 GB) 3B için yetmez.
  7. Tarihli kopya günde bir ad alıyor (`watchover-ops:YYYYMMDD`): aynı gün ikinci geçiş, `act_models`'in sabitlediği
     şampiyonu sessizce değiştirir.
  8. Kör ölçüm sızıyor: ön teşhis en iyi adayın adımlarını çalıştırıyor, "Benzer vakalar" seçilen runbook'u gösteriyor.
  9. Demo 17.1 adım 5 test ortamında; imzalanan hücre prod. Ortamlar arası miras yok, ekranda "Çalıştır" değil "Rehberli" çıkar.
* Güven: tek erken doğrulama hatası haftalık bir runbook'u ~9 ay L2'nin altında tutar; günlük bir runbook %1'in üzerinde
  hata oranıyla L3'e, sıfırdan büyük hata oranıyla L4'e hiç çıkamaz; L3'teki denetim örneklemesiyle başlatma birimi L4'e
  ulaşamaz; `h_e` (sunucu sınırı) tanımsız; `ok_corrected` iki güvende birden ceza alıyor. Kurallar seçicisinin her haftalık
  yeniden uydurması `T_sel`'i sıfırlıyor.
* İş paketleri: WP1, WP6 ve WP11 tek ajan için fazla büyük; değişmez testleri WP19'a bırakılmamalı; dosya listesinde
  `lora.py`, `autotrain.py`, `api/__init__.py`, `live.py::make_handler`, `mcp_server.py`, `app.py`, `migrate.py`, `admin.py`,
  `inventory.py` eksik. Önerilen sıra ve "demo dilimi" §9'da.

---

## 0. Verdict

The design is buildable on this code base and its core ideas fit: the case as an extended `actions` row, the host-local
allowlist, the pull channel behind the existing `/enroll`-style proxy, two trusts with a Wilson bound, model output as a closed
choice, and `act_mode = off` by default. The facts table (§1) is accurate. What does not hold as written:

1. **Protocol gaps that weaken the security bound** (C1–C4): the Go pre-check, provenance read from the wrong attribute,
   unsigned installation of the actuator code, and a dispatcher check that misses the runbook body.
2. **Data relocation that misses four other openers of `actions`** (C5) and a few schema details that break on PostgreSQL
   (C9–C12).
3. **Local model budgets that do not fit a CPU-only box with a 3B model** (C25–C33): prompt size, timeouts, model residency,
   training memory and time, and the gate's call count.
4. **Trust edge cases** (C17–C24) that are mathematically consistent but operationally surprising, and one (C21) that makes
   run-unit L4 unreachable.
5. **Work packages** that are not all one-agent sized and leave invariant tests to the end (C40–C45).
6. **Demo steps** that need explicit demo controls and two scenario fixes (C46–C55).

None of this changes the architecture. Every correction is local.

---

## 1. Facts re-checked

| Design claim (§1) | Checked | Note |
|---|---|---|
| `ActionStore` in a separate `actions.db` on SQLite | `api/services.py:138`, `actions.py:15` | True. Also opened by `mcp_server.py:37`, `app.py:450`, `migrate.py:21,40`, `admin.py:175,201` (C5) |
| `visible()` and `remember_action` on PATCH | `api/routers/actions.py:35,79` | True |
| One connection per process, `RLock`, PG autocommit, SQLite commit per statement | `db.py:49,105-148` | True |
| Synchronous routes, one uvicorn process | `api/__main__.py:14` | True (no `workers`) |
| Go proxies `/enroll` to `:8601`, serves `/agent/install.sh` | `receiver/main.go:599,602` | True |
| Ingest stamps `attributes["agent"]` | `live.py:435` | True, but for legacy key and open mode it is the client's `X-Agent` header; only `attributes["agent_id"]` (`live.py:455`) proves a token (C2) |
| Agent 0.6.0, stdlib, root unit without `User=` | `agent.py:33`, `scripts/agent-install.sh:96-107` | True; no Windows installer or service wrapper exists (C15) |
| Admins skip MFA; skipped when OTP mail fails | `api/routers/auth.py:76-81` | True |
| `rbac_meta` markers, `NEW_PERMS` | `rbac.py:35-37,119-170` | True |
| Team alerts never post to a webhook | `notify.py:318` | True |
| `chat_messages` has no schema / seed | `llm.py:159-189` | True |
| `llm_cfg("ops")`, dated copy | `api/services.py:245`, `autotrain.py:194` | True, but the dated tag has day resolution (C30) |
| `training_bundle` random split; `load_examples` reads `messages` | `autolearn.py:105-116`, `lora.py:15` | True; `lora.train` also truncates silently (C29) |
| `cryptography>=42` core dependency | `pyproject.toml:12` | True |
| Inventory fields | `inventory.py:19-22` | True; `status` exists (`maintenance` / `decommissioned`), `monitoring` is not a URL (C14) |

Missing from §1 (each changes a decision):

* Threshold breaches already open anomalies: `thresholds.py:297` → `anomaly._upsert` → `on_open` (`anomaly.py:438`). The
  separate `Thresholds._step` hook duplicates every threshold trigger (C7).
* Metric samples carry no agent identity: `metric_of` keeps `(ts, host, metric, value, env)` only (`live.py:844-860`) (C2).
* The playbook also lives in its own SQLite file (`api/services.py:139`), so `Playbook.lookup` cannot read `act_playbook_links` (C13).
* Ollama in compose: `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=2`, 8 GB; trainer 6 GB, 3 threads
  (`docker-compose.yml:182-194,223,240`) (C26, C28).
* `ingest_guard` re-attributes a team agent's lines for foreign hosts to the sender and keeps the claimed name in
  `host.reported` (`ingest_guard.py:9-20`) (C3, C53).
* Error bodies follow `HTTPException(code, {"code", "tr", "en"})` → `{"detail": {...}}` (`api/routers/logs.py:28`) (C36).

---

## 2. Data model and database patterns

**C5 (B) — `actions` relocation misses four openers.** §15.3 step 1 copies `actions.db` into the shared database and renames
the file. Today `mcp_server.py:37` and `app.py:450` open `ACTIONS_DB` on SQLite, `migrate.py:21` copies it to PostgreSQL, and
`admin.py:175,201` snapshot and roll it back. After the rename, the MCP server and the Streamlit app create a new empty
`actions.db` and write rows nobody sees; a code rollback restores old code that reads the renamed file away.
Fix: one helper `actions.open_store(kb)` used by all five openers; do not rename the file (leave it read-only, marker
`actions_in_kb_v1` in `rbac_meta` prevents a second copy); `admin._db_paths` keeps it for snapshots; `migrate.FILES` skips it
once the marker exists. Own work package (WP0b, §9) with tests on the existing actions API, MCP `create_action` and a snapshot.

**C6 (M) — `Database.tx()` blocks the whole process.** The transaction holds the process `RLock` (`db.py:49`), so ingest,
alerts, thresholds and the learner wait for it. Fix: rule in `act/store.py` and a test: inside `with db.tx():` only SQL and
in-memory computation (HMAC, JSON); never HTTP, LLM, notify or file I/O. Land `tx()` with no callers first (WP0a) and test that
`_exec` / `_insert` / `executemany` called from another thread wait and commit normally.

**C9 (B) — nullable `team_id` in keys.** `act_metrics_weekly PRIMARY KEY (week, team_id, …)`: PostgreSQL makes key columns
`NOT NULL`, so installation-wide rows fail; SQLite allows NULL in the key and treats NULLs as distinct, so
`upsert_suffix` never conflicts and rows duplicate. Fix: `team_key TEXT NOT NULL` (`'*'` installation-wide, else the id) in
every composite or unique key; keep `team_id` as a plain nullable column for `ownership.TEAM_TABLES`.

**C10 (M) — `act_decisions.case_id UNIQUE` blocks a second decision.** Reopen (§4.2) and "Başka runbook dene" (§4.6) need
another decision on the same case. Fix: `UNIQUE (case_id, attempt)`; selection events reference the attempt.

**C11 (M) — append-only triggers break restore and migration.** `Database.restore()` and `copy_table(replace=True)` run
`DELETE FROM <table>` (`db.py:189-215`); the triggers on `act_audit` abort them, and `migrate.py` copies into a PostgreSQL
table whose triggers were created by `open_schema`. Fix: `restore()` treats `act_audit` as append-only (insert rows with
`seq > max(seq)`, never delete); `migrate.open_schema` creates the triggers after the copy; document that a restored
snapshot older than the anchors file shows as a chain break, by design.

**C12 (m) — time format.** Existing rows use `datetime.now(UTC).isoformat(timespec="seconds")` (`+00:00`); the design's
JSON uses `Z`. Range queries compare strings (`ts >= ?`, `notify.py:306`). Store `+00:00` everywhere; use `Z` only on the wire.

**C13 (m) — playbook links.** On SQLite the playbook is a separate database (`api/services.py:139`), so "`act_playbook_links`
lookup in `lookup()` results" (§15.1) cannot be done inside `Playbook`. Do the join in `act/importer.py` / the act router.

**C14 (m) — `url` slot source.** `inventory.monitoring` holds values such as `discovered` (`inventory.py:104`), not a URL.
Add an inventory attribute `health_url` (or a per-runbook `url` parameter with `roots`-style allowlist) for `http_check`.

**Other data-model notes (m):** `act_cells.cell_key` concatenates pool / cluster names with `/`; escape or hash the class.
`auth_events.detail` is truncated to 200 characters (`auth.py:135`), which cuts the anchor signature: store `seq` and the
first 32 hex characters of the MAC there, the full anchor in `act_anchors` and the anchors file. Keep `last_poll` and presence
("an operator has the case open", §10.5) in memory and flush every 60 s; 30 polls per second must not become 30 writes.

---

## 3. Triggers, hooks and cases

**C7 (M) — one threshold hook, not two.** Use the anomaly fan-out only; drop the `Thresholds._step` hook. The threshold
anomaly key `threshold:{env}:{host}:{metric}` maps to the family `metric:<name>`. No change to `anomaly.py` is needed: in
`api/services.py:169` set `on_open` to a small function that calls `memory.remember_anomaly` and `act.from_anomaly`.

**C8 (M) — per-mount disk is not a collector-only change.** `METRICS` is a closed tuple in `live.py:41` and
`thresholds.py:28`; policies validate against it (`thresholds.py:77`); rollups, history and the anomaly scan are keyed by those
four names. A `disks` field would be dropped by `metric_of`. Fix for v1: no collector change. The `mount` slot comes from the
diagnose step (`linux.disk.usage(path)` returns `mount`, provenance `trusted`, signed by the actuator); the server-side verify
of `linux-varlog-full` is `log_absent` plus the root `disk` metric only when `mount == "/"`. Per-mount metrics become a separate
monitoring feature.

**C3 (M) — provenance must read `host.reported`.** A team agent's line for another team's host arrives re-attributed to the
sender with the claimed name in `host.reported` (`ingest_guard.py`). The evidence item must show that name as "Beyan edilen
kaynak", and the case must be keyed to the real sender host. The spoofing drill (§17.5) only produces a `claimed` item on the
target when the sending agent is installation-wide (C53).

**C2 (B) — provenance from the token, not from the name.** `attributes["agent"]` is the `X-Agent` header for legacy-key and
open-mode senders (`live.py:996`). Rule: `trusted` ⇔ `attributes.agent_id` is set and that agent record's name (or an
inventory alias) equals the target host. Metric samples and threshold state carry no agent id, so "server-side measurement"
is not provably H's own. For any act step at L ≥ 3, require an actuator-signed diagnose result from H **in this run** as the
trusted item (I10); threshold state alone may support L ≤ 2.

**C39 (M) — dataset first actions must not flood the inbox.** §15.3 step 2 turns every `first:` row into a `ready` case.
Every historical dataset incident would then wait in İş kutusu and be eligible for blind sampling. Keep them `kind='action'`
(or `kind='case'` with phase `historical`, outside the inbox, not labelled).

**C38 (m) — legacy list.** `GET /api/actions` returns every row (`api/routers/actions.py:57`); after cases exist, the old
page shows them with a status dropdown that answers `409 act_case_managed`, including after `act_mode` is switched back to
off. Default the legacy list to `kind='action'`; `?kind=all` for the new inbox. `record_first_actions` reads the full list
(`api/services.py:388`): filter by kind there too.

**Other (m):** `teams.owners(host)` recomputes every team's host set per call (`teams.py:540`); cache it per CaseRouter batch.
`LogWatch.count` keeps only the top 3 hosts (`logwatch.py:180,197`), so "≤ 3 hosts match" cannot tell 4 from 40; key on
`query:{rule}:*` whenever three hosts are returned. Cache `act_mode` in `ActService.emit` (it is called from the anomaly and
alert threads).

---

## 4. Agent protocol, receiver and actuator

**C1 (B) — Go pre-check lets open mode and the legacy key through.** `auth.check` returns 200 with a nil agent for open mode
and for the shared key (`receiver/main.go:169-176`); the snippet in §9.3.2 only checks the code. Fix:
`ag, identity, code := s.auth.check(token(r)); if code != 200 || identity != "token" || ag == nil { 401 }`. The Python side must
also refuse `/agent/v1/*` unless `registry.verify(tok)` returned a record (`live.py:915-928` falls back to the key and to open
mode). Add both cases to `receiver/main_test.go` and `test_act_jobs.py`.

**C4 (B) — installation is not out-of-band.** `agent-install.sh` downloads `agent.py` from the dashboard
(`scripts/agent-install.sh:49`), usually over `http://…:8600`. If the packs and `SHA256SUMS` come the same way (§9.3.1), a
compromised server or a network attacker chooses the actuator code and its allowlist, and the bound in §10.1 A7 does not hold
from the first install. Fix: the actuator (agent file plus `agent_actions/`) ships as a release archive whose sha256 the host
administrator pins on the install line exactly like `--dispatch-pub` (`--actions-sha256 <hex>`); `--actions` refuses plain
HTTP unless `--insecure-install` is given; the installer never re-downloads `agent.py` for an existing actuator unit. State this
in residual risk 7.

**C15 (M) — Windows actuator is not buildable as written.** There is no Windows installer or service wrapper in the tree, and
a stdlib-only Python script cannot register a Windows service. Move Windows act packs to a later package; v1 Windows hosts are
guided-only. If needed earlier: a scheduled task at start-up (`schtasks /RU SYSTEM`) as the supervisor, and `icacls` checks as
designed. The IIS demo (§17.2) runs on `--actions-sim`.

**C16 (M) — `preexec_fn` in a threaded process.** The actuator uses worker threads (§9.3.5); `preexec_fn` is not safe with
threads. Python 3.9+ `Popen(user=…, group=…, extra_groups=…, umask=…)` does the drop without it.

**Job channel notes (m):**
* `make_handler(store, api_key, registry, rum)` and `start_receiver(...)` (`live.py:895,1030`) take no act hook; add a
  `jobs=None` parameter. With `WATCHOVER_API_RECEIVER=0` there is no job channel at all; say so in Ayarlar.
* Hardening: `ProtectSystem=strict` plus `ReadWritePaths` from `writes` breaks `logrotate -f` postrotate scripts and
  `journalctl --vacuum-*`; `PrivateTmp=yes` breaks the Java attach used by `jcmd` on older JVMs. Run every vendor act template
  under the generated unit in WP11 tests.
* Ed25519: the RFC 8032 reference in pure Python verifies in about 12 ms and matches `cryptography` on this machine
  (Appendix B), so residual risk 5 is about vectors, not speed. The reference code is not constant time: use it for
  verification; sign with `cryptography` when importable.
* The proxy's error text ("dashboard not reachable for enrolment") is reused for `/agent/v1/`; give the job route its own.

---

## 5. Trust formula on small numbers

The formula in §7.4 was simulated as written (H_s 45 d, H_f 90 d, z 1.645). Monotonicity held for 200 000 random weighted
cases (adding a failure never raised T, adding a success never lowered it). The calibration rows of §7.4 and the worked
examples of §7.10 reproduce (`10 ok, 60 d old` is 0.59, not 0.60).

### 5.1 0, 3, 10, 50 fresh runs with failures

| Runs | Failures | S | F | T | Level the score supports |
|---|---|---|---|---|---|
| 0 | — | 0 | 0 | 0.000 | L1 (publish level) |
| 3 | 0 | 3 | 0 | 0.526 | L1 |
| 3 | 1 `verify_failed` | 2 | 2 | 0.182 | L1; 18 more clean runs to 0.75 |
| 3 | 1 `side_effect` | 2 | 4 | 0.117 | L1 |
| 3 | 2 `verify_failed` | 1 | 4 | 0.046 | L1 |
| 10 | 0 | 10 | 0 | 0.787 | L2 |
| 10 | 1 `verify_failed` | 9 | 2 | 0.573 | L1 |
| 10 | 1 `side_effect` | 9 | 4 | 0.465 | L1 |
| 10 | 2 `verify_failed` | 8 | 4 | 0.431 | L1 |
| 50 | 0 | 50 | 0 | 0.949 | L3 (L4 needs 52) |
| 50 | 1 `verify_failed` | 49 | 2 | 0.888 | L2 |
| 50 | 1 `side_effect` | 49 | 4 | 0.842 | L2 |
| 50 | 2 `verify_failed` | 48 | 4 | 0.839 | L2 |

Sensible: zero evidence gives zero, three clean runs are "Yetersiz kanıt", ten clean runs earn L2, one failure always costs a
level's worth of evidence. What surprises operators is in 5.2–5.4.

### 5.2 Ceilings by frequency and failure rate (steady state, `verify_failed` k = 2)

| Frequency | p = 0 | 1 % | 2 % | 5 % | 10 % |
|---|---|---|---|---|---|
| 3 a day | 0.986 | 0.932 | 0.888 | 0.781 | 0.643 |
| daily | 0.960 | 0.901 | 0.855 | 0.743 | 0.605 |
| 3 a week | 0.911 | 0.852 | 0.804 | 0.692 | 0.556 |
| weekly | 0.774 | 0.723 | 0.680 | 0.577 | 0.456 |

A daily runbook reaches L3 only below about 1 % failures and L4 only with none; a weekly runbook reaches L2 only with none and
needs about 112 days of clean weekly runs to get there. One `verify_failed` keeps a weekly runbook below 0.75 for about 273
days, and keeps a daily runbook below 0.95 for about 215 days. This is defensible policy, but it must be visible.

**C17 (M) — show the reachable ceiling.** Compute from the cell's last 90 days of rate and failure share the highest level its
score can reach, and show it next to "Sonraki seviye": "Bu sıklık ve hata oranıyla en fazla Onaylı". Replace "Nadir:
otomatikleşmeyecek" with this computed sentence.

**C18 (M) — early verify defects.** The first runs of a new runbook often fail because the verify is mistuned, and one such
failure costs months (5.1, 5.2). Allow an `act.promote` holder to mark a failure "runbook kusuru, vN'de düzeltildi" when a
newer approved version changed that step's `verify`; the event is archived (audited), not deleted. Today the only way out is a
new version that resets every success of the step too.

### 5.3 Definitions to fix

**C19 (M) — `h_e` is ill-posed.** "Each host contributes at most max(0.25, 1/N) of the cell's total" cannot be met when fewer
than 1/cap hosts have events; an iterative cap drives a lone host's weight to zero, a one-pass cap gives 10 clean runs on one
host of a four-host class S = 2.5, T = 0.48 instead of 0.79. The worked examples silently assume h = 1. Fix: apply the cap only
for promotions to L ≥ 3 (where A8 matters), define it one-pass as `h = min(1, cap · W_total / W_host)`, keep "≥ 3 distinct
hosts" as the L2 → L3 criterion, and add host counts to every calibration example.

**C20 (M) — events with both credit and penalty.** `ok_corrected` (c 0.5, k 0.5) matches both decay branches. Decay the credit
with H_s and the penalty with H_f. Better: a corrected parameter is a selection error, already counted in `T_sel`
("Blind: same runbook, a parameter differs"); record `ok_corrected` as c 1, k 0 for `T_exec` and keep the separate
"corrections < 10 % of the last 20" criterion.

**C22 (M) — one outcome per step.** A failed verify followed by a rollback records `verify_failed` (k 2) and `rolled_back`
(k 3)? Define the step outcome as the worst single outcome (`rolled_back` supersedes `verify_failed`; `side_effect` supersedes
both). In the demo (§17.1 step 6) this is the difference between T 0.60 and 0.46.

**C23 (M) — demotion lines that do not exist or oscillate.** θ for L1 is undefined (no threshold for L0 → L1). Diagnose cells
start at L4 with T = 0, so the generic rule ("after a failure, T < θ − 0.10 → −1") demotes them on the first `exec_error`, and
the automatic "diagnose cell → L4" promotion raises them again at 02:30. Fix: R0 diagnose cells use only the
"two consecutive `exec_error` → L2" rule and return to L4 after three clean runs; `verify` steps carry no trust events (their
result is the act step's outcome); define θ(1) = 0.

**C24 (M) — review backlog is a cap, not a demotion.** "3 sampled L3 runs unreviewed > 72 h → L2" would need new signatures to
return after a long holiday. Model it as a cap `review_backlog` (like `dormant` and `loop`) that lifts when the queue is
reviewed.

**C21 (B) — run-unit L4 is unreachable.** At L3 the audit sample `clamp(10 %, 50 %, 4 / starts per 30 d)` gives a daily unit
about 8.7 effective labelled decisions at saturation (T_sel ceiling 0.76) and three-a-day units 19.5 (ceiling 0.88); L4 needs
0.92 on 32. At L2, 30 % blind gives a daily unit a ceiling of 0.878 (L3 needs 0.85: reachable, but one false action at
saturation leaves 0.754). Fix: while a unit collects for its next level, sample at
`rate = clamp(10 %, 60 %, 1.2 · N_target · ln 2 / (H_s · starts per day))` (daily toward L4: 59 %); drop to the 4-a-month rate
only once the unit holds L4. Show the cost ("Gözetimli'den Otomatik'e: her iki çalıştırmadan biri gözden geçirilecek").

**C31 (M) — rules refits reset `T_sel`.** Each adopted refit is a new selector id `rules@<n>` (§8.2) and trust follows the
selector (§8.9), so every Sunday adoption drops units that relied on the rules selector. Treat a refit like a model: the new
weights shadow as a challenger on the same blind cases (deterministic, free), the old id stays the qualifying selector until
the new one meets the unit's threshold.

**C37 (m) — wording.** With penalty weights counted as trials, T is a conservative score, not a 95 % bound. The hover text
"Güven alt sınırı %84" should read "Güven puanı %84".

---

## 6. Local model on a CPU-only box with a 3B model

Assumptions (replace with the probe of C27): a 3B model quantised to 4 bits in Ollama on 4 vCPU decodes about 6–10 tokens per
second and reads prompts at about 40–80 tokens per second; Turkish log text is about 3 characters per token.

**C25 (B) — the select call does not fit.** With the limits in §8.3 (40 evidence lines × 300 characters, 5 candidates, 3
similar cases, system text) the prompt reaches about 14 000 characters ≈ 4 700 tokens, above `num_ctx 4096`, so Ollama
truncates it. Even a 1 200-token prompt takes 15–30 s to read; `why` (≤ 300 characters ≈ 100–150 tokens) plus the JSON keys
take another 15–25 s to generate. The 20 s timeout and the gate's "p95 ≤ 20 s" fail on every case. Fix:
* select returns only `{"choice", "params", "cite"}` (≈ 30–40 tokens, `num_predict 64`); no `why`;
* hard prompt budget ≤ 1 200 tokens, measured with the tokenizer estimate, dropping lowest-ranked evidence first (trusted
  slots and trigger facts are kept); evidence lines ≤ 160 characters, ≤ 12 lines;
* explanations become a separate, lazy `act_explain` call made only when someone opens "Neden bu?", cancellable, or the
  deterministic template text (§8.12) by default;
* all model calls run on one bounded `act-llm` worker queue, never in request threads, the CaseRouter or the engine tick.

**C26 (B) — sealing must not wait for the model.** §8.4 seals every selector's proposal "before the case is shown in any
inbox", while §14 says "cases never wait on the model". On CPU, with `OLLAMA_NUM_PARALLEL=1` (`docker-compose.yml:182`) and chat
calls in the same queue, that wait is minutes. Fix: freeze the model input at `ready` (`input_sha` of the rendered prompt,
sealed with the rules proposal); the model may answer later on that frozen input, before the reveal for blind cases. The seal
then proves the model never saw the decision. The challenger runs as a nightly batch on the day's frozen inputs, so it never
competes with live traffic (§8.11 already has the nightly replay; make it the only challenger path on CPU).

**C28 (M) — model residency.** Compose keeps at most two models loaded (`OLLAMA_MAX_LOADED_MODELS=2`, "chat + embeddings").
Champion, challenger, chat and embeddings are four; alternating calls reload a 2 GB model each time. With C26 the daytime set is
chat + embeddings + champion: raise the default to 3 when `act_mode ≥ suggest` and size the Ollama memory limit for it (≈ 2.5 GB
per 3B model at a 4k context); the challenger loads only during the nightly batch.

**C27 (M) — hardware-relative limits.** Add a probe (one 1 000-token prompt with a 32-token answer per model tag, on model
change and weekly) stored in `act_models.perf`. Timeout = clamp(20 s, 90 s, 2 × measured); the gate's latency criterion becomes
"p95 ≤ timeout"; when the measured p95 exceeds 60 s, the select role switches itself off with the banner "Model bu donanımda
yavaş: kural tabanlı öneriler".

**C29 (B) — `lora.train` turns long examples into NaN.** `encode` truncates prompt + answer to `max_len` 1024 and masks the
prompt (`lora.py:63-69`). When the prompt alone reaches 1024 tokens, every label is −100, the loss is NaN and AdamW writes NaN
into the adapter; the run burns hours and fails the gate. §15.1 says "`lora.py`: none". Fix in `lora.py`: skip and count
examples whose prompt exceeds `max_len − 64` (reported in `train_stats.json` next to `over_1024`); render `act_select` training
prompts in a compact form ≤ 600 tokens.

**C32 (M) — training memory and time.** `lora.train` loads the base in float32 (`lora.py:59`): a 3B base needs about 12 GB for
weights alone, ≥ 16 GB with activations; the trainer container is limited to 6 GB and 3 threads (`docker-compose.yml:223,240`),
so a 3B base does not train there. Time: forward plus backward through a frozen base is about 4 × params × tokens ≈ 1.1e13
FLOP for a 900-token example; at an assumed 100–200 GFLOP/s that is 1–2 minutes per example, so 600 action examples × 2 epochs
take 18–36 hours. Fix: cap the action share by **tokens**, not by count (`act_train_mix_max` 50 % of tokens); at most 300 action
examples per run, one epoch for action tasks; document sizing ("3B eğitimi: ≥ 16 GB, gece boyu"); keep the default training base
from `settings.py:21` (1.5B) for installations below that.

**C33 (M) — the trainer loop must not render examples every minute.** `AutoTrainer.state()` runs every 60 s and calls
`training_examples` (`autotrain.py:88`). If §8.7's pipeline (render, delexicalise, re-quarantine, cap) is called from there, the
trainer burns CPU continuously. Materialise action examples into `act_examples` in the 02:00 job; `training_examples` reads
rows; `state()` counts with SQL.

**C30 (B) — dated copies collide within a day.** `autotrain.py:194` names the copy `watchover-ops:YYYYMMDD`. A second passing run
on the same day (forced runs, "train now") overwrites the tag that `act_models` pinned as champion or challenger, so the action
selector silently changes weights without its gate. Fix: `watchover-ops:<YYYYMMDD-HHMM>` or `:r<train_run_id>`; `act_models`
stores the tag and the Ollama digest and refuses to serve a tag whose digest changed.

**Action gate cost (M).** Replay ≥ 30, the golden set, 60 clean/injected pairs, the red-team set, plus champion baselines is
250–400 calls; at 15–30 s per call on 4 vCPU that is 1–3 hours on the Ollama host that also serves live traffic. Cache
outputs per `(model digest, input_sha)` (deterministic at temperature 0 with a seed, C27), run the gate in the night window,
and on CPU installations use 30 injection pairs instead of 60.

**Single audit writer (M).** The trainer process should not write `act_*` tables or audit events. It writes
`train_runs.gate["action"]`; the API's 02:30 job registers the challenger and writes the audit event. One process owns the
chain head and `.act-audit.key`.

**Training targets (M).** Humans do not write `why` or `cite`. Define the `act_select` target as `{"choice", "params", "cite"}`
with `cite` computed deterministically (trigger evidence ids + the evidence ids that produced the chosen parameter values).
Never copy model text into a target (I16 in spirit).

**Ollama structured output (m).** JSON-schema `format` needs a recent Ollama; probe `/api/version` and fall back to
`format: "json"` plus the validator. Pass `seed` as `options.seed` (`llm.py:166` builds `options`).

**Retrieval vs delexicalisation (m).** §8.10 delexicalises `run` lessons yet promises that Ask Watchover answers "web-07'de
disk dolunca geçen sefer ne yapıldı?". Keep host names in `run` lessons (they are team-scoped by `ownership.kb_for`) and
delexicalise at prompt and training render time only. Conversely, case `resolution` lessons enter the chat training set as
they are (`autolearn.py:90`); write case closures as kind `run` (not in that query) or scrub host names there.

---

## 7. Safety and KVKK

**C34 (B) — scrub structured output field by field.** `privacy.scrub` on built-in JSON output turns
`"bytes_before": 5368709120` into `[TEL]` (PHONE matches ten digits starting with 5, `privacy.py:74`; tried) and about 10 % of
13–19-digit numbers into `[KART]` (Luhn, `privacy.py:84`). The server-side parsers then fail or mis-read sizes and timestamps.
Fix: built-ins return JSON; the actuator parses first, scrubs only string values and pseudonymises declared columns, and keeps
numbers. Raw text output is scrubbed for display only, never parsed after scrubbing.

**C35 (M) — the dispatcher must check the runbook body.** A5 assumes a database-only attacker cannot steer a dispatch. For an L4
step with no approval, parameters come from the runbook version body (defaults, bindings), which is a database row. Before
every L ≥ 2 dispatch verify (a) the cell's latest level event MAC, (b) `act_runbook_versions.body_sha256` against the MAC'd
`runbook.approved` audit payload, (c) the vote's `job_hash` against its MAC'd audit payload; any mismatch refuses the dispatch
and raises the chain alarm.

**C51 (M) — blind measurement leaks.** Pre-diagnosis runs "the top candidate's" diagnose steps (§4.4), so the step titles on a
blind case reveal the rules choice; "Benzer vakalar · aynı runbook · başarılı" (§13.3) does the same. Both inflate agreement for
the rules selector, and the model's prompt sees the same similar cases. Fix: on blind cases run the case-kind diagnosis pack (or
the union of all candidates' diagnose steps) without runbook attribution, and show similar cases without their runbook until
the decision.

**C36 (m) — error shape.** The existing pattern returns `{"detail": {"code", "tr", "en"}}` (`api/routers/logs.py:28`); §12.2
shows `code` at the top level. Align §12.2/§12.3 with the existing shape so the web client handles both the same way.

**Step-up for SSO (M).** `428 act_stepup_unavailable` for SSO accounts without SMTP leaves single-sign-on installations without
four-eyes approvals. The OIDC flow already exists (`api/routers/auth.py:223-270`): use re-authentication with `prompt=login` and
`max_age=0` and check `auth_time` as the SSO step-up.

**Approval routing (m).** The admin bypass in `Access.can_in` (`access.py:76-80`: `all` users hold every key in every team)
conflicts with "an installation-wide permission does not cover a team-owned host unless …" (§10.9). Add a dedicated helper
`acc.can_approve(team_id)` instead of reusing `can_in`.

**Teams cards (m).** Microsoft has been retiring the legacy Office 365 connector webhooks that accept MessageCard. Make the
Workflows (Adaptive Card) variant the default and keep MessageCard for installations that still have a working connector.

---

## 8. API and UI fit

* `api/__init__.py:53` includes routers from an explicit tuple; the four `act_*` routers must be added there (missing in §15.1).
* Deep links: `App.tsx` already renders `<Login />` on any path when signed out and keeps the path after a local sign-in; only
  the SSO button hard-codes `next=/ops` (`web/src/pages/Login.tsx:49`). Pass the current path; the server already validates
  `next` (`api/routers/auth.py:214`).
* `Layout.tsx` lives in `web/src/components/` (§2.3 and WP10 name it without the folder). The `respond` group exists
  (`components/Layout.tsx:30`).
* `Llm.tsx` has a `quality` tab (`pages/Llm.tsx:25`); the action gate card belongs there, as designed.
* The current `Actions.tsx` is 40 lines; rewriting it as the inbox is a new page in practice. Keep the legacy table as the
  "Ekibin › Aksiyonlar" sub-view for `kind='action'`.

---

## 9. Work packages: order, size, green suite

**C40 (M) — WP1 does three unrelated risky things.** Split: **WP0a** `Database.tx()` with no callers (C6); **WP0b** actions
relocation with all five openers (C5); **WP1** the `act` package skeleton, policy, `act_mode`, DDL. WP0a/WP0b change no
behaviour and must pass the whole existing suite on SQLite and PostgreSQL.

**C41 (M) — WP11 is three to four packages.** Split: **WP11a** server job channel, Go and Python routes (C1), envelopes,
leases, a fake actuator; **WP11b** actuator core for Linux (allowlist loader, permission check, validators, predicates,
built-ins, argv executor, field-aware scrub (C34), ledger, halt, limits) plus `--actions-sim`; **WP11c** Ed25519 both ways and
key confirmation; **WP11d** installer, pinned archive (C4), systemd hardening tests; **WP11e** Windows (later, C15).

**C42 (M) — WP6 bundles six hooks with evidence, provenance and quarantine.** Split into WP6a (Trigger, CaseRouter, merge,
reopen, loop, storm, flood, the anomaly / logwatch / manual hooks, inbox read API) and WP6b (evidence, slots, provenance
from `agent_id` (C2), `host.reported` (C3), quarantine). Drop the `agent.py` `disks` change (C8).

**C43 (M) — invariants belong to the package that creates them.** Move each invariant test out of WP19: I14 into WP1, I9 and
I15 into WP2, I1 and I11 into WP4/WP11b, I6 into WP5/WP12, I2 into WP13, I10 into WP6b, I13 into WP8/WP11a, I16 into WP14.
WP19 keeps the aggregate suite and the drills.

**C44 (M) — keep the keyed chain off the critical path.** WP5 needs identity rules and an `audit.write()` interface, not the
HMAC chain. Ship WP1 with an audit interface whose MAC is a placeholder; WP2 replaces it. System execution stays refused
without keys (`503 act_signing_unavailable`), so nothing unsafe ships in between.

**C45 (M) — missing files.** Add to §15.1 and the packages: `lora.py` (C29, WP14), `autotrain.py` dated tag and challenger
handover (C30, single writer; WP14), `api/__init__.py` (routers; WP3), `live.py::make_handler` / `start_receiver` (`jobs`;
WP11a), `mcp_server.py`, `app.py`, `migrate.py`, `admin.py` (C5; WP0b), `inventory.py` (`health_url`, C14; WP17),
`web/src/pages/Login.tsx` (SSO `next`; WP12), `docker-compose.yml` (Ollama loaded-model and trainer memory notes, C28/C32; WP14).

Revised order (packages in a stage run in parallel; each ends with `make test` green and `act_mode = off` unchanged):

```
0: WP0a tx() · WP0b actions relocation
1: WP1 skeleton, policy, DDL, audit interface
2: WP3 rbac/step-up · WP4 templates/params · WP2 keyed chain (off the critical path)
3: WP5 runbooks/approvals · WP6a cases/hooks · WP11a job channel + fake actuator
4: WP6b evidence/provenance/quarantine · WP7 matcher/sealing/blind
5: WP8 engine/manual/verify/labels · WP11b actuator core + sim · WP13 selector (C25-C27)
6: WP9 trust (C17-C24) · WP10 UI 1 · WP14 learning (C29-C33) · WP15 mining
7: WP11c Ed25519 + key confirmation · WP12 L2 execution
8: WP11d installer + hardening · WP16 L3/L4 (C21, C24) · WP17 HTTP/ITSM · WP18 UI 2
9: WP19 demo pack + drills          later: WP11e Windows, WP20
critical path: WP0a → WP1 → WP5 → WP7 → WP8 → WP9 → WP12 → WP16 → WP19
```

Demo slice for §17.1 steps 1–5 (smallest set that shows the user's request end to end): WP0a, WP0b, WP1, WP3, WP4, WP5, WP6a,
WP6b, WP7, WP8, WP9, WP10, WP11a, WP11b (sim), WP12. WP2 and WP11c are needed only for the drills in §17.5.

---

## 10. Demo achievability

| Step | Problem | Fix |
|---|---|---|
| 17.1 / 1 | Thresholds and alerts are evaluated every 60 s (`thresholds.py` `start(every=60)`, `notify.py:471`) and policies default to `for_min` 3: up to four minutes to the first case, against one minute in the script | Demo policy `for_min 0`; `python -m watchover.act demo disk` calls `thresholds.evaluate()` and `alerts.evaluate()` right after the generator, or posts the Trigger to `/api/act/triggers` (C46) |
| 17.1 / 1 | Per-mount metric does not exist (C8) | The sim emits `disk`; the mount comes from the diagnose step |
| 17.1 / 2 | Blind is a 30 % hash draw | Demo team policy: blind 100 % for step 2, 0 % for step 5 (C47) |
| 17.1 / 2 | "kaynak: kural + model" needs a registered champion that exists in the laptop's Ollama | Seed `act_models` with a dated tag created on the laptop; precompute on the frozen input (C26); the line falls back to "kaynak: kural" without a model |
| 17.1 / 2 | §12.2 shows `path` ambiguous between E3 (mount) and E2 (log path), but the runbook binds `path` from `evidence.mount` and `default` only | Fix the example (one value, not ambiguous) or add `evidence.path` to the binding (C55) |
| 17.1 / 3 | The presenter cannot "run the command on the host" of a simulated server; without a state change the verify fails | `demo run-guided <case>` applies the step to the sandbox and moves the simulated metric (C48) |
| 17.1 / 3 | Watch window range is 5–240 min; "compressed to 30 s" is not allowed | A demo time scale (`WATCHOVER_ACT_TIME_SCALE`, refused unless `act_demo` is set) (C49) |
| 17.1 / 4 | "T 0.77, 9 runs": nine runs spread over 60 days give T 0.69 | Seed at least 10 clean runs in the last 21 days, ≥ 2 operators (C50) |
| 17.1 / 5 | `web-test-02` is another environment; the cell signed in step 4 is prod; inheritance never crosses environments, so the step shows Rehberli | Use a second prod host of the same class, or seed and sign the test cell too (C52) |
| 17.1 / 5 | If the demo host's criticality is high or critical, R1 becomes R2 effective and prod R2 needs four-eyes and step-up, contradicting "R1 needs no step-up" | Seed `normal` criticality for the step-5 host |
| 17.1 / 5 | The phone must reach the dashboard and the Teams webhook needs internet egress at the venue | Keep the e-mail / in-app fallback and a second browser window on `/approvals/:id` |
| 17.1 / 6 | Double outcome (verify_failed + rolled_back) | C22 |
| 17.3 | "Gözetimli in test" needs `act_mode = supervised`, an on-call entry and a signed L3 cell | Seed them; show the countdown on test only |
| 17.5 spoof | With a team agent the line is re-attributed to the sender (`host.reported`), no case on `db-01` | Use an installation-wide agent for the drill and show `host.reported` (C53) |
| 17.5 audit | The SQLite trigger blocks the edit | Show the block, then drop the trigger and edit: "Zincir kırık" (C54) |
| 17.7 | 7 minutes with a CPU model | Precompute model proposals during the talk; never wait on the model on stage |

---

## 11. Small inconsistencies

* §7.4 table: "10 ok, all 60 days old" is 0.59.
* §7.4 reachability: the saturation counts are right for zero failures, but reaching a threshold close to the ceiling takes long:
  weekly to L2 ≈ 112 days, three a week to L3 ≈ 126 days, daily to L3 ≈ 30 days (Appendix A).
* §12.2 decide response: `agree_rules: params_diff` while the rules selector should not pick among ambiguous values (§8.2:
  "several distinct values ⇒ ambiguous").
* §1 says act calls use `act_select`, `act_explain`, `act_draft`; §8.1 adds `act_next`.
* §2.3 `Layout.tsx` path (§8).

---

## 12. Corrections (the list)

| ID | Sev | Design § | Correction |
|---|---|---|---|
| C1 | B | 9.3.2 | Go `/agent/v1/` pre-check must require `identity == "token"` and a non-nil agent; Python must refuse key/open mode on `/agent/v1/*` |
| C2 | B | 4.3, I10 | `trusted` from `attributes.agent_id`, not `attributes.agent`; metric samples carry no agent id, so L ≥ 3 needs an actuator-signed diagnose result from H in the run |
| C3 | M | 4.3 | Read `host.reported` (ingest_guard re-attribution) into evidence |
| C4 | B | 9.3.1, 18 | Actuator code and packs from a pinned release archive (`--actions-sha256`), no plain-HTTP install, never re-downloaded from the server |
| C5 | B | 15.3 | Relocating `actions` must cover `mcp_server.py`, `app.py`, `migrate.py`, `admin.py`; one opener helper; do not rename the file |
| C6 | M | 15.1 db.py | `tx()` holds the process lock: only SQL inside; land with no callers first |
| C7 | M | 4.1 | Drop the `Thresholds._step` hook; threshold breaches already arrive through anomaly `on_open`; fan out in `services.py` |
| C8 | M | 4.3, WP6 | No per-mount collector change in v1; `mount` from the diagnose step |
| C9 | B | 11.6 | No nullable `team_id` in primary/unique keys; use `team_key TEXT NOT NULL` |
| C10 | M | 11.2 | `act_decisions` unique per (case, attempt) |
| C11 | M | 10.7 | `restore()` / `migrate` must respect append-only `act_audit` |
| C12 | m | 11 | Store `+00:00` timestamps like the existing code |
| C13 | m | 15.1 | Playbook link lookup in the act layer (separate SQLite file) |
| C14 | m | 4.3 | `health_url` inventory attribute; `monitoring` is not a URL |
| C15 | M | 9.3, 9.4 | No Windows service path exists; Windows act steps later, guided-only in v1 |
| C16 | M | 9.3.5 | `Popen(user=, group=, extra_groups=)` instead of `preexec_fn` |
| C17 | M | 7.4, 7.9 | Show the reachable ceiling from rate and failure share |
| C18 | M | 7.7 | Archive a failure fixed by a later version's verify change (signed, audited) |
| C19 | M | 7.4 | Define `h_e` one-pass and apply it only for promotions to L ≥ 3 |
| C20 | M | 7.3 | Split decay for events with both c and k; `ok_corrected` counts in `T_sel`, not `T_exec` |
| C21 | B | 8.4, 7.5 | Audit/blind rate sized to the next level's target; run-unit L4 is unreachable otherwise |
| C22 | M | 7.3 | One outcome per step (worst wins) |
| C23 | M | 7.7 | θ(1) = 0; diagnose cells only use the consecutive-error rule; verify steps have no trust events |
| C24 | M | 7.7 | Review backlog as a cap, not a demotion |
| C25 | B | 8.3 | Select returns choice/params/cite only; prompt ≤ 1 200 tokens; explanations lazy; one `act-llm` worker queue |
| C26 | B | 8.4, 14 | Seal the frozen input at `ready`; the model answers later; challenger as a nightly batch |
| C27 | M | 8.3, 8.8 | Per-model speed probe; timeouts and gate latency relative to measured hardware |
| C28 | M | 8.9 | Ollama loaded-model limit and memory sized for chat + embeddings + champion |
| C29 | B | 15.1 lora.py | `lora.train` must skip over-length examples (NaN loss); compact training prompts |
| C30 | B | 8.8, 8.9 | Dated tag with time or run id; pin the digest |
| C31 | M | 8.2 | Rules refit shadows as a challenger; the old rules selector stays qualifying |
| C32 | M | 8.7 | Token-based mix cap, ≤ 300 action examples, one epoch; trainer memory sizing for 3B |
| C33 | M | 8.7 | Materialise action examples nightly; `state()` counts with SQL |
| C34 | B | 9.3.5, 10.10 | Field-aware scrubbing of structured output (`[TEL]` / `[KART]` corrupt numbers) |
| C35 | M | 10.1 A5 | Dispatcher verifies the runbook body hash and the vote `job_hash` against MAC'd audit payloads |
| C36 | m | 12 | Error bodies in the existing `{"detail": {"code", "tr", "en"}}` shape |
| C37 | m | 7.9 | "Güven puanı", not "alt sınır" |
| C38 | m | 4.6 | Legacy `/api/actions` lists `kind='action'` by default |
| C39 | M | 15.3 | Dataset first actions stay plain actions (no inbox flood) |
| C40 | M | 16 | Split WP1 into WP0a, WP0b, WP1 |
| C41 | M | 16 | Split WP11 into 11a–11e |
| C42 | M | 16 | Split WP6 into 6a, 6b |
| C43 | M | 16 | Invariant tests in the package that introduces each invariant |
| C44 | M | 16 | Audit interface first; keyed chain off the critical path |
| C45 | M | 15.1 | Add the missing files (lora, autotrain, api/__init__, live handler, mcp_server, app, migrate, admin, inventory, Login, compose) |
| C46 | M | 17.1 | Demo trigger command and `for_min 0` |
| C47 | m | 17.1 | Demo blind rate override |
| C48 | M | 17.1 | `demo run-guided` changes sandbox state for the guided step |
| C49 | m | 17.1 | Demo time scale for the watch window |
| C50 | m | 17.1 | Seed ≥ 10 clean runs in the last 21 days |
| C51 | B | 4.4, 13.3 | Blind cases: no top-candidate diagnosis attribution, no runbook in similar cases |
| C52 | B | 17.1 | Step 5 on a prod host of the promoted class, or seed and sign the test cell |
| C53 | m | 17.5 | Spoof drill with an installation-wide agent |
| C54 | m | 17.5 | Audit drill shows the trigger block first |
| C55 | m | 12.2, 6.1 | Fix the ambiguous-path example vs the runbook binding |

Also (no ID): SSO step-up through OIDC re-authentication; `can_approve` helper instead of `can_in`; Workflows card as the Teams
default; Ollama version probe for JSON-schema `format`; keep host names in `run` lessons and delexicalise at render time;
single audit-writer process; action gate output cache and 30 injection pairs on CPU.

---

## Appendix A — trust simulation

```python
import math
Z = 1.645
def wilson(S, F):
    n = S + F
    if n <= 0: return 0.0
    p = S / n
    return (p + Z*Z/(2*n) - Z*math.sqrt(p*(1-p)/n + Z*Z/(4*n*n))) / (1 + Z*Z/n)
def trust(events, Hs=45, Hf=90):            # events: (age_days, credit, penalty)
    S = sum(c * 2**(-a/Hs) for a, c, k in events)
    F = sum(k * 2**(-a/Hf) for a, c, k in events)
    return wilson(S, F)
# steady state at `rate` runs/day with failure share p (k = 2):
#   S = rate*(1-p)*Hs/ln2 ;  F = rate*p*2*Hf/ln2
# blind / audit saturation: S = rate*b*Hs/ln2
```

Reproduced values: §7.4 calibration 0.53 / 0.65 / 0.72 / 0.77 / 0.86 / 0.90 / 0.95; 20 ok + one `verify_failed` /
`rolled_back` / `side_effect` = 0.759 / 0.715 / 0.676; §7.10 execution example S 11.25, F 1.84, T 0.641, ten more clean runs
0.757; selection example 0.741 / 0.816; hysteresis 0.866 / 0.828.

## Appendix B — pure-Python Ed25519

The RFC 8032 section 6 reference code, run on this machine with a 600-byte message: sign ≈ 9 ms, verify ≈ 12 ms; the signature
equals the one `cryptography` produces for the same key and message.
