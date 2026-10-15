# Role-based teams and team-only data

Status: design, 2026-10-15. Owner of the change: the "role-based teams" workflow. Builds on `src/watchover/teams.py`
(teams, host rules, `Scope`) and `src/watchover/rbac.py` (stored roles, `PERMISSIONS`).

## Özet (TR)

* Yeni bir ekip oluşturulduğunda üç rol **otomatik** gelir: **Ekip yöneticisi**, **Operatör**, **İzleyici**. Her rolün
  yetkileri, kenar çubuğuyla aynı gruplarda (Ana sayfa, APM, Sinyaller, Müdahale, Zekâ, Güvenlik, Ayarlar) onay kutularıyla
  işaretlenir; grup başlığında "Tümü" kutusu vardır. Her üyenin o ekipte **tek bir ekip rolü** olur.
* Kullanıcının **kurulum rolü** (bugünkü global rol) kalır. Etkin yetki = kurulum rolü ∪ ekip rolleri. Ekip rolünden gelen
  bir yetki **yalnız o ekibin sunucularında** geçerlidir; kurulum rolünden gelen yetki kullanıcının tüm ekiplerinin
  sunucularında geçerlidir. Yönetici ve "Tüm sunucuları gör" yetkisi her şeyi görür; ekibi olmayan kullanıcı bugünkü gibi.
* Yeni kurulum rolü **Ekip üyesi**: kendi başına hiçbir yetkisi yoktur, ne göreceğini ekip rolü belirler.
* **Ekip envanteri**: ekip üyesinin eklediği sunucu (envanter kaydı, keşiften terfi, ekip kayıt anahtarıyla bağlanan ajan)
  o ekibin olur; envanter düzenleme, keşfedilen sunucular, ajanlar, tarama, eşik politikaları, bildirim alıcıları ve grupları
  ekip bazında sınırlanır. Başka ekibin sunucusu sahiplenilemez.
* Ekip yöneticisi yalnız kendi ekibinin üyelerini, rollerini (kendi yetkisini aşmadan) ve elle eklenen sunucularını yönetir.
  Kuralları, Ekip yöneticisi rolünü ve yönetici atamasını kurulum yöneticisi yapar.
* Geçiş: mevcut ekiplere roller eklenir, üyeler kurulum rollerine göre eşlenir; kurulum rolleri değişmediği için **kimse
  yetki kaybetmez**.

---

## 0. Today (what the design starts from)

| Piece | Where | Behaviour |
|---|---|---|
| Global roles | `rbac.py` `Roles`, table `roles(name, label, description, perms JSON, builtin)` | `admin` (always `ALL`), `operator`, `viewer` built in; custom roles from Users › Roles. `NEW_PERMS` upgrades untouched built-ins. |
| One role per user | `auth.py` `users.role` | `register()` defaults `operator` (`viewer` for self-registration); first account is `admin`. |
| Teams | `teams.py` tables `teams`, `team_hosts(team_id, host)`, `team_members(team_id, email)` | Hosts by rules (any matches) + explicit hosts; members are e-mails. |
| Scope | `Teams.scope()` via `Services.scope_of()` and the `user_scope` dependency (`api/security.py`) | `None` for admin / `scope.all` / no team (unless `teams_strict`); otherwise the union of the user's teams' hosts and service rules, cached 30 s per e-mail. |
| Checks | `require(perm)` → `roles.can(user["role"], perm)` | One global permission set; scope is permission-agnostic. |
| UI | `web/src/pages/Teams.tsx` (modal editor with live preview), `Users.tsx` (users, `RolesMatrix`, teams tab), `Layout.tsx` `ScopeBadge` | Permissions grouped by `page/act/sys/scope`, not by the sidebar. |

Permission use across `src/watchover/api/routers` (grep of `require("…")`, count per file):

```
page.ops    live 19, anomalies 3, thresholds 5          act.report  live 1          act.sim    live 2
page.apm    apm 8, bizflow 5                            page.rum    rum 4           act.rum    rum 5
page.map    map 1                                       page.data   datasets 26, actions 5, live 1
act.export  datasets 1                                  page.inv    inventory 6     act.inventory inventory 4
act.anomaly anomalies 7, bizflow 3                      page.pb     playbook 5      page.itsm  itsm 2
act.itsm    itsm 1                                      sys.notify  notify 15, thresholds 4, system 2
page.assist assist 4, knowledge 11, memory 7            page.llm    llm 14          page.parser grok 7
act.parser  grok 6                                      page.scan   scan 5          act.scan   scan 3
page.conn   agents 2                                    act.connect agents 8        page.src   sources 3
act.sources sources 6                                   page.users  users 9         sys.security users 1
page.teams  teams 6                                     page.readme system 1        sys.status system 5
sys.maint   system 2                                    sys.update  system 9        sys.auth   system 2
```

`user_scope` is applied in agents (2), anomalies (10), apm (9), assist (2), bizflow (6), inventory (4), live (21), map (2),
rum (5), thresholds (4). Not scoped today although host-bearing: `inventory` `/stats`, `/export`, `/{hostname}`, `PUT`,
`DELETE`, `/import`; `agents` `/{aid}` and every write; `scan` (all); `itsm /tickets`; `datasets`; `actions`; `notify`;
threshold policy CRUD. Those holes are closed by section 3.

Two page keys span several sidebar sections: `page.ops` opens Home, **Anomalies** (Signals) and **Thresholds** (Response);
`page.data` opens Datasets (Signals) and **Actions** (Response). They are split (1.2) so that every tick in the editor maps
to items of one sidebar section.

---

## 1. Model

### 1.1 Two kinds of role

| | Kurulum rolü (installation role) | Ekip rolü (team role) |
|---|---|---|
| Stored in | `roles` (unchanged table) | new `team_roles` |
| Held by | every account, exactly one (`users.role`) | every team membership, exactly one (`team_members.role_id`) |
| May contain | any permission except `team.manage` | only **team-assignable** permissions (`TEAM_PERMS`, 1.3) |
| Applies to | all hosts the user may see (all hosts for admin / `scope.all` / no team; else all of the user's teams' hosts) | only that team's hosts and team-owned objects |
| Edited by | `page.users` holders (no escalation, as today) | installation admins (`page.teams`), and the team's managers within limits (2.4) |

New built-in installation role **`member`** — label "Ekip üyesi" / "Team member", description "Kurulum genelinde yetkisi
yok; ne göreceğini ekip rolü belirler." perms `{"page.readme"}`, `builtin=1`. It is created the first time a team exists
(team creation or the migration in 3.6), not at `Roles.__init__`, so a fresh installation still lists exactly
`admin, operator, viewer` (keeps `tests/test_auth_admin.py::test_roles_and_permissions` green). Add `"member"` to
`auth.ROLES` so `register(role="member")` is accepted.

### 1.2 Permission catalogue grouped like the sidebar

`rbac.py` keeps `PERMISSIONS` (key → (group, tr, en)) for compatibility and adds:

```python
SECTIONS = [("home", "Ana sayfa", "Home"), ("apm", "APM", "APM"), ("signals", "Sinyaller", "Signals"),
            ("respond", "Müdahale", "Response"), ("intel", "Zekâ", "Intelligence"), ("security", "Güvenlik", "Security"),
            ("settings", "Ayarlar", "Settings")]
FEATURES: dict[str, tuple[str, str, bool]]   # key -> (section id, kind "view" | "action" | "manage", team-assignable)
REQUIRES: dict[str, str]                     # action -> the view it needs (ticking the action ticks the view)
TEAM_ONLY = frozenset({"team.manage"})
TEAM_PERMS: frozenset[str]                   # keys whose FEATURES[k][2] is True
TEAM_TEMPLATES: dict[str, dict]              # "manager" | "operator" | "viewer" -> {"label": (tr, en), "desc": (tr, en), "perms": set}
def normalize(perms, team: bool) -> set[str]  # ∩ known keys, ∪ REQUIRES closure; team=True: ∩ TEAM_PERMS; team=False: − TEAM_ONLY
def catalogue() -> dict                       # the GET /api/teams/permissions payload (4.1)
```

New keys (added to `PERMISSIONS`, `NEW_PERMS` and `GROUPS` gets `"team": ("Ekip", "Team")`):

| Key | TR | EN | group |
|---|---|---|---|
| `page.anom` | Anomaliler | Anomalies | page |
| `page.thr` | Eşikler | Thresholds | page |
| `page.actions` | Aksiyonlar | Actions | page |
| `act.thresholds` | Ekip eşik politikalarını düzenleme | Edit the team's threshold policies | act |
| `act.alerts` | Ekip bildirim alıcıları, grupları ve kuralları | Team alert recipients, groups and rules | act |
| `team.manage` | Ekibi yönetme: üyeler, roller, sunucular | Manage the team: members, roles, hosts | team |

`page.ops` is relabelled "Ana sayfa: canlı akış, metrikler, SLO" / "Home: live feed, metrics, SLO".

The full feature map. Templates: **M** Ekip yöneticisi, **O** Operatör, **V** İzleyici. "install" = only through an
installation role, never in a team role (shown in the team editor as a muted line, see 5.1).

| Section | Key | Kind | Team | M | O | V | Enforced in |
|---|---|---|---|---|---|---|---|
| Ana sayfa | `page.ops` | view | yes | ✓ | ✓ | ✓ | routers/live.py (scope `page.ops`) |
| | `act.report` → page.ops | action | yes | ✓ | ✓ | ✓ | live.py `/report` |
| | `act.export` → page.ops | action | yes | ✓ | ✓ | | datasets.py export, Ops file viewer |
| | `act.sim` | action | install | | | | live.py simulate / clear |
| APM | `page.apm` | view | yes | ✓ | ✓ | ✓ | apm.py, bizflow.py |
| | `page.rum` | view | yes | ✓ | ✓ | ✓ | rum.py reads |
| | `act.rum` → page.rum | action | yes | ✓ | ✓ | | rum.py app keys, audited user lookup |
| | `page.map` | view | yes | ✓ | ✓ | ✓ | map.py |
| Sinyaller | `page.data` | view | yes | ✓ | ✓ | ✓ | datasets.py, live.py `/analyze` |
| | `page.inv` | view | yes | ✓ | ✓ | ✓ | inventory.py reads, discovered |
| | `act.inventory` → page.inv | action | yes | ✓ | ✓ | | inventory.py writes |
| | `page.anom` (new) | view | yes | ✓ | ✓ | ✓ | anomalies.py reads (was `page.ops`), NotifyBell |
| | `act.anomaly` → page.anom | action | yes | ✓ | ✓ | | anomalies.py, bizflow.py writes |
| Müdahale | `page.actions` (new) | view | yes | ✓ | ✓ | ✓ | actions.py (was `page.data`) |
| | `page.thr` (new) | view | yes | ✓ | ✓ | ✓ | thresholds.py reads (was `page.ops`) |
| | `act.thresholds` (new) → page.thr | action | yes | ✓ | | | thresholds.py writes on team policies |
| | `page.pb` | view | yes (shared content) | ✓ | ✓ | ✓ | playbook.py |
| | `page.itsm` | view | yes | ✓ | ✓ | ✓ | itsm.py `/tickets` |
| | `act.itsm` | action | install | | | | itsm.py config |
| | `act.alerts` (new) | action | yes | ✓ | | | notify.py team rows; opens the Bildirimler page |
| | `sys.notify` | action | install | | | | notify.py, thresholds.py, system.py (installation-wide rows) |
| Zekâ | `page.assist` | view | yes (KB shared) | ✓ | ✓ | | assist.py, knowledge.py, memory.py |
| | `page.llm`, `page.parser`, `act.parser` | view / action | install | | | | llm.py, grok.py |
| Güvenlik | `page.scan` | view | yes | ✓ | ✓ | | scan.py reads |
| | `act.scan` → page.scan | action | yes | ✓ | ✓ | | scan.py host / all (catalog reload: install) |
| Ayarlar | `page.conn` | view | yes | ✓ | ✓ | | agents.py reads |
| | `act.connect` → page.conn | action | yes | ✓ | ✓ | | agents.py writes, team enrolment key (fleet key: install) |
| | `page.readme` | view | yes | ✓ | ✓ | ✓ | system.py README |
| | `team.manage` (new) | manage | team only | ✓ | | | teams.py team endpoints, "Ekibim" page |
| | `page.src`, `act.sources`, `page.users`, `page.teams`, `page.sys`, `sys.status`, `sys.maint`, `sys.update`, `sys.auth`, `sys.security`, `scope.all` | | install | | | | sources, users, teams, system routers |

So `TEMPLATES["viewer"]` = 13 keys, `["operator"]` = viewer + `act.export, act.rum, act.inventory, act.anomaly,
page.assist, page.scan, act.scan, page.conn, act.connect` (22), `["manager"]` = operator + `act.thresholds, act.alerts,
team.manage` (25) = `TEAM_PERMS`.

Built-in installation roles after the split: `operator` gains `page.anom, page.thr, page.actions` automatically (its
comprehension takes every `page.*` except sys / users / teams); `viewer` gets `page.anom, page.thr, page.actions` added to
its literal set. Neither gets `act.thresholds` / `act.alerts` (no widening of today's operator).

### 1.3 Team roles

```sql
CREATE TABLE IF NOT EXISTS team_roles (id {pk}, team_id INTEGER NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL,
    description TEXT DEFAULT '', perms TEXT DEFAULT '[]', template TEXT DEFAULT '', position INTEGER DEFAULT 0,
    updated_at TEXT, UNIQUE (team_id, key))
ALTER TABLE team_members ADD COLUMN role_id INTEGER          -- NULL only transiently; the migration fills it
ALTER TABLE teams ADD COLUMN default_role_id INTEGER         -- role for members added without one (the İzleyici role)
```

* **Creation**: `Teams.save(..., tid=None)` inserts the team, then the three template roles (`key` = template key,
  `template` = same, `label` = Turkish label, `position` 0/1/2), sets `default_role_id` to the viewer role, then members.
  With `copy_roles_from=<tid>` it copies that team's roles (labels, perms, template markers) instead of the templates.
* `key` of a custom role: slug of the label (ASCII, lowercase, `_`), unique in the team.
* Every save goes through `normalize(perms, team=True)`: unknown and installation-only keys are dropped silently,
  `REQUIRES` views are added.
* A team keeps at least one role. Deleting a role still held by members is refused (409) unless `move_to` names another
  role of the same team. Deleting the default role moves `default_role_id` to the role with the fewest permissions.
* Reset: a role with a `template` marker can be put back to `TEAM_TEMPLATES[template]["perms"]`.

### 1.4 Membership

`team_members(team_id, email, role_id)`: one role per (user, team). A user may be in several teams with different roles.
Members are still keyed by lower-case e-mail (unchanged; `auth.Users` keys by e-mail too).

---

## 2. Effective permissions

New module `src/watchover/access.py` (pure logic, unit-testable without FastAPI):

```python
@dataclass(frozen=True)
class Membership:
    team_id: int; team: str; role_id: int; role_key: str; role_label: str
    perms: frozenset          # normalize(team role perms, team=True)
    hosts: frozenset          # Teams.team_hosts(team) — rules + explicit hosts
    service_rules: tuple      # the team's field == "service" rules

class Access:
    email: str; role: str; all: bool; strict: bool
    global_perms: frozenset   # roles.perms(role) − TEAM_ONLY (admin: ALL)
    memberships: tuple[Membership, ...]
    perms: frozenset          # effective = global_perms ∪ ⋃ m.perms   (admin: ALL)
    def can(self, perm) -> bool
    def scope(self, perm: Optional[str] = None) -> Optional[Scope]
    def team_ids(self, perm) -> list[int]       # teams in which perm applies
    def can_in(self, team_id, perm) -> bool
    def install(self, perm) -> bool             # perm valid installation-wide
    def manages(self, team_id) -> bool

class AccessResolver:
    def __init__(self, roles, teams, settings_fn): ...
    def of(self, user: dict) -> Access          # cached per e-mail for 30 s, keyed by (role, teams.version, roles.version, strict)
    def invalidate(self) -> None
```

Rules (G = installation role perms, R_T = team role perms in team T, H(T) = hosts of T):

1. **Admin or `scope.all` ∈ G** → `all=True`; `scope(any) = None`; `perms = G ∪ ⋃R_T` (admin: `ALL`). Team roles
   cannot widen data scope for them (they already see everything).
2. **No team** → `perms = G`; `scope = None`, or an empty `Scope` when `teams_strict` (unchanged behaviour).
3. **In one or more teams** → `perms = G ∪ ⋃R_T`. For a permission `p`:
   * `p ∈ G` → applies in **all** the user's teams: `scope(p)` = ⋃ H(T) over every membership.
   * `p ∉ G` → applies only where granted: `scope(p)` = ⋃ H(T) over memberships with `p ∈ R_T`.
   * `scope(None)` (legacy `user_scope`) = ⋃ H(T) over every membership — identical to today's `Teams.scope()`.
4. `can_in(T, p)` = `all and p ∈ perms`, or T is a membership and (`p ∈ G` or `p ∈ R_T`).
5. `install(p)` = `p ∈ G` and (`all` or (no membership and not strict)). Used for installation-wide objects: the fleet
   enrolment key, global threshold policies, global recipients / groups / alert rules, scan catalogue reload, ITSM and
   LLM settings. A team-scoped user whose installation role holds `sys.notify` can therefore no longer edit
   installation-wide policies (the one intentional tightening; built-in `operator` / `viewer` never held `sys.notify`).
6. `manages(T)` = `"page.teams" ∈ G` or (T is a membership and `"team.manage" ∈ R_T`).

Worked example. Ayşe: installation role `member` (`page.readme`), EA = Operatör, İK = İzleyici.
`perms` = readme ∪ operator ∪ viewer. `scope("page.apm")` = EA ∪ İK hosts (both roles hold it).
`scope("act.inventory")` = EA hosts only, so `PUT /api/inventory` for an İK host → 403. Nav shows Envanter (she can
view both teams' inventories) and the edit buttons appear only on EA rows (`row.team_ids ∩ teamsWith("act.inventory")`).

Caching: `Teams` gains `version` (incremented by `invalidate()`); `Roles` gains `version` (incremented by `save`,
`delete`, `reset`). The resolver drops an entry when either version moved or the user's role changed, so edits apply on
the next request; the 30 s TTL only covers host-set drift (new hosts matching rules). `Teams.list()` should cache the
per-team `matched` sets for the same 30 s as `facts()` (today it recomputes every team on each call; `require` will hit
the resolver on every request).

---

## 3. Data: a team sees only its hosts' data, and owns what it adds

### 3.1 Three classes of data

| Class | Examples | Rule |
|---|---|---|
| Host-bound data | live feed, metrics, SLO, anomalies, APM, RUM, maps, inventory, discovered hosts, agents, scan results, ITSM correlation, threshold overview / state | filtered by `scope(<the endpoint's permission>)` |
| Team-owned objects | explicit hosts, inventory records a team added, agents enrolled by a team, threshold policies, recipients, groups, alert rules, datasets, actions, RUM apps a team created | carry `team_id`; listed to members of that team (and to installation-wide users); edited with the team permission in that team |
| Shared reference content | playbook, knowledge base, memory, parser patterns, LLM settings | not host-bound; readable with the permission, editable as today. Documented limit in 3.7 |

### 3.2 Host ownership (team-owned inventory)

`team_hosts` becomes the ownership record:

```sql
ALTER TABLE team_hosts ADD COLUMN origin TEXT DEFAULT 'manual'    -- manual | inventory | promote | agent | import
ALTER TABLE team_hosts ADD COLUMN added_by INTEGER                 -- users.id; the API returns the display name, never the e-mail
ALTER TABLE team_hosts ADD COLUMN added_at TEXT DEFAULT ''
CREATE TABLE IF NOT EXISTS team_enroll_keys (team_id INTEGER PRIMARY KEY, key TEXT NOT NULL, created_at TEXT)
```

Definitions: a host is **owned** by team T when it is an explicit host of T; **visible** to T when it is in H(T)
(explicit or matched by a rule; overlap between teams stays allowed, e.g. a DBA team with `db-*`). A host is
**unowned** when no team sees it.

`Teams.claim(tid, hosts, origin, by) -> (added, rejected)` is the single entry point. A host is accepted when:

1. it is already visible to T (it just becomes explicit), or
2. it is unknown to Watchover (not in `facts()`, not in inventory): a new host, pre-registered by the team, or
3. it is unowned and the caller is an installation admin (`page.teams`), or `teams_self_claim` is on (new setting,
   default `False`, added to `settings.py` defaults and `api/routers/system.py`'s editable keys).

Otherwise it is rejected with reason `other_team` (visible to another team; the other team is never named) or `unowned`
(known but in no team: an installation admin assigns it). Installation admins may pass `move=True` to take an explicit
host away from another team. Explicit duplicates left by older versions (same host explicit in two teams) are reported
in `GET /api/teams` as `conflicts` and resolved by an installation admin.

Who calls `claim`:

| Path | Origin | Team |
|---|---|---|
| `PUT /api/inventory` creating a new hostname | `inventory` | the caller's team with `act.inventory` (`team_id` in the body when there are several, else 400) |
| `POST /api/inventory/import` | `import` | `?team_id=`; each row validated like the PUT, failures land in `errors` |
| `POST /api/inventory/promote` | `promote` | the caller's team; the hosts were already visible, so this pins them explicitly |
| `POST /api/agents` (per-agent token) | `agent` | `team_id` as above |
| agent self-enrolment with a **team enrolment key** | `agent` | the key's team |
| `POST /api/teams/{tid}/hosts` | `manual` | `tid` (team managers: rules 1-3; installation admins: any, `move` allowed) |

Self-enrolment: `AgentRegistry.self_enroll(key, …)` keeps its signature (called from `src/watchover/live.py`, which
must not change). It tries the fleet key first, then team keys (constant-time compare against every row of
`team_enroll_keys`, through a hook `self.team_hooks` that `Services` sets to `self.teams` right after creating Teams).
With a team key, the name must pass `claim` rules 1-2 (else `PermissionError("name belongs to another team")` → 403 at
the receiver proxy), the agent row gets `team_id`, and the host is claimed with origin `agent`.

### 3.3 Endpoint rules by area

Every existing `sc=Depends(user_scope)` becomes `sc=Depends(perm_scope("<the permission of the same endpoint>"))`
(4.2). Additional rules:

**Inventory** (`page.inv` reads, `act.inventory` writes)
* `GET /inventory`, `/discovered` — unchanged filter, now `perm_scope("page.inv")`. Rows get `team_ids: [int]` (teams of
  the caller that see the host) and, for installation-wide users, `teams: [name]`.
* `GET /inventory/stats`, `/export` — computed over the scoped rows (`Inventory.stats(seen, allow=sc.allows)`).
* `GET /inventory/{hostname}` — 404 when outside scope.
* `PUT /inventory` — existing record: host must be in `scope("act.inventory")` else 403. New record: `claim` (3.2); 409
  `host belongs to another team`, 403 `host is known but unowned: ask an installation admin`, 400 `team_id required`.
* `DELETE /inventory/{hostname}` — in `scope("act.inventory")`; deleting the record does not release ownership.
* Discovered hosts: a team sees only discovered hosts in its scope; unowned discovered hosts are shown to installation
  admins with a "Sahipsiz" badge and an "Ekibe ata" action (`POST /api/teams/{tid}/hosts`).

**Agents** (`page.conn`, `act.connect`)
* `GET /agents/{aid}`, revoke / reactivate / rotate / delete — the agent name must be in the scope of the permission,
  else 404.
* Fleet key endpoints (`/agents/enroll-key*`) — `install("act.connect")`. Teams use `/api/teams/{tid}/enroll-key*`.
* `ALTER TABLE agents ADD COLUMN team_id INTEGER`.

**Thresholds** (`page.thr`, `act.thresholds`, `sys.notify`)
* `ALTER TABLE threshold_policies ADD COLUMN team_id INTEGER` (NULL = installation-wide).
* A team policy only matches hosts in H(team): `Thresholds.matches(p, host, …)` additionally requires
  `p.team_id is None or host in team_hosts(p.team_id)` (`Thresholds` receives `team_hosts_fn` from Services).
  Inside a team policy, scope `all` means "all hosts of the team", `env` / `glob` / `pool` / `host` narrow within it.
* Precedence in `effective()`: sort key `(team policy?, SPECIFICITY[scope], id)`, highest wins — the team that owns
  the host overrides installation defaults; among team policies, the more specific, then the newest. The overview's
  "source" column shows "Ekip EA · env prod" or "Kurulum geneli · …".
* Writes: `team_id = NULL` → `install("sys.notify")`; `team_id = T` → `can_in(T, "act.thresholds")` or
  `install("sys.notify")`. Team policies may use only `email` and `sms` channels and targets that resolve to the
  team's own recipients / groups (no raw addresses, no webhook: an arbitrary URL is an SSRF / exfiltration path).
* `GET /thresholds` for a scoped user: installation-wide policies (read-only, `editable: false`) plus their teams'
  policies. `POST /preview` intersects with H(team) when `team_id` is set.

**Alert recipients, groups and rules** (`act.alerts`, `sys.notify`)
* `ALTER TABLE recipients|notify_groups|notify_rules|alerts ADD COLUMN team_id INTEGER`.
* Lists: installation-wide users see all rows with a `team` column; others see rows of teams where they hold
  `act.alerts`. Writes on a row: `team_id NULL` → `install("sys.notify")`; else `can_in(team_id, "act.alerts")` or
  `install("sys.notify")`. `notify_groups.name` stays globally unique (UI suggests the "EA-" prefix).
* A team alert rule evaluates only over H(team): `live.slo/stats/metric_stats` get `host=Scope.host_filter()` of the
  team, breaches are filtered by host. Dispatch resolves targets inside the team. Delivered alerts carry `team_id`.
* `GET /notify/alerts`: scoped users see alerts with `team_id` in their `act.alerts` teams. `POST /notify/test` stays
  `install("sys.notify")`; teams get `POST /notify/recipients/{rid}/test`.
* Nav: the Bildirimler item shows for `sys.notify` **or** `act.alerts`.

**Anomalies, APM, RUM, map, assistant, live** — `perm_scope` with the endpoint's permission (`page.anom` for anomaly
reads, `act.anomaly` for writes, `page.apm`, `page.rum`, `act.rum`, `page.map`, `page.assist`, `page.ops`,
`act.report`). RUM apps created by a scoped user are pinned to the team by appending the service rule
`{"field": "service", "op": "equals", "value": <app>, "origin": "rum"}` (`_clean_rules` keeps an optional `origin`).
User lookup (`/rum/lookup`, pseudonymous, audited) only inside apps the team sees; the audit entry records the team.

**Datasets and actions** (`page.data`, `page.actions`)
* Dataset records get `team_id` (in memory today; persisted wherever dataset metadata is persisted). Upload, fetch,
  from-source, samples and live `/analyze` by a scoped user set `team_id` (body / form field when several teams hold
  `page.data`); live `/analyze` keeps only observations in `scope("page.data")`. Scoped users list, open, export and
  delete only their teams' datasets; installation-wide users see all, and may re-assign with `PATCH /datasets/{key}
  {"team_id"}`.
* `ALTER TABLE actions ADD COLUMN team_id INTEGER` and `host TEXT DEFAULT ''`. Actions created from an anomaly carry
  its host; first actions of a dataset carry the dataset's `team_id`. Visible when `team_id` ∈ the user's teams or
  `scope("page.actions").allows(host)`; rows with neither are installation-wide only.

**Scan** — results, `/results/{hostname}` filtered by `scope("page.scan")`; `POST /scan/host/{h}` needs
`h ∈ scope("act.scan")`; `POST /scan/all` scans only inventory rows in that scope; `/catalog/reload` is
`install("act.scan")`.

**ITSM** — `/tickets` correlates with the scoped live signals and breaches and keeps only tickets with at least one
correlated host in `scope("page.itsm")`; tickets without a correlated host are installation-wide only. Live `refresh`
stays limited to `install("act.itsm")`.

### 3.4 Members and KVKK

* A team manager never receives the account list (`GET /users` stays `page.users`). Members are added by exact e-mail;
  unknown, disabled or pending accounts all return the same `404 account not found or cannot be added` (no account
  state oracle).
* `GET /api/teams/{tid}/members` for non-managers returns only `{name, role_label}` (for assignee pickers); managers and
  installation admins also get `email`. `added_by` on hosts is returned as a display name.
* Every team change writes `auth_events` (`event="team"`, detail like `EA: +member operator`, `EA: role operator perms
  changed`, `EA: host claimed web-12 (inventory)`), visible in System › Güvenlik günlüğü. No secret ever goes in.
* Nothing in this design returns raw RUM user ids, session ids or IPs beyond today's (pseudonym / scrub unchanged).

### 3.5 What a team manager may do (and may not)

May, in teams where `manages(T)`:
* add / remove members (existing accounts by e-mail) and set their team role;
* create, rename, edit, reset and delete roles of T, with permissions ⊆ their own R_T (no escalation);
* add explicit hosts under the claim rules, release explicit hosts;
* see T's matched hosts, rules (read-only), owned objects counts, enrolment key (with `act.connect`).

May not (installation admins only, i.e. `page.teams`): create / delete / rename teams; edit rules; see known-hosts or
the unowned list; grant `team.manage` or edit any role holding it; assign a role holding `team.manage` or change the
role of a member who holds one; change their own membership or role; touch another team.

### 3.6 Migration (startup, idempotent, no access lost)

Markers in a new `rbac_meta (key TEXT PRIMARY KEY, value TEXT)` table so one-shot steps do not repeat (otherwise an admin
who deliberately unticks `page.anom` would see it come back after a restart).

1. `split_v1`: every stored role with `page.ops` gets `page.anom` and `page.thr`; with `page.data` gets `page.actions`.
   Built-in defaults already contain them; `NEW_PERMS` lists the six new keys so untouched built-ins upgrade.
2. ALTERs from 1.3, 3.2, 3.3 (try / except, the pattern of `auth.Users` `must_change`; works on SQLite and PostgreSQL).
3. `team_roles_v1`: for each team without roles → create the three template roles and `default_role_id`. For each
   member with `role_id IS NULL` → by installation role: `admin` → manager, `operator` → operator, everything else
   (`viewer`, `member`, custom roles) → viewer. If any team exists, create the `member` installation role.
4. Existing explicit `team_hosts` rows get `origin='manual'`. Existing policies, recipients, groups, rules, agents,
   datasets and actions keep `team_id = NULL` (installation-wide).

Why nothing breaks: installation roles are untouched, and G still applies in all the user's teams, so for every member
`perms ⊇ old perms` and `scope(p ∈ G)` = old scope. The only visible difference: members mapped to İzleyici gain the
template's read-only views their installation role lacked (for built-in `viewer` that is only `page.inv`), and only for
**their own team's** hosts; `operator` → Operatör adds nothing (the template is a subset). Users in no team, admins and
`scope.all` holders are unaffected; `teams_strict` keeps its meaning. To hand control to team roles an admin sets
members' installation role to "Ekip üyesi" (Users page shows which members are wider than their team role, 5.2).

`tests/test_teams.py`, `tests/test_api.py::test_users_roles_and_permissions` and
`tests/test_auth_admin.py::test_roles_and_permissions` must stay green unchanged (`/teams/mine` keeps its exact shape;
membership details go to `/auth/me`).

### 3.7 Deliberate limits (documented in the UI hint)

* Knowledge base, memory and playbook are shared: lessons learnt from one team's incident are searchable by others.
  A later step can tag lessons with `team_id`.
* Inside a team-owned dataset the observations are not host-filtered (the uploader chose the file).
* A service whose traces span several teams' hosts is visible to each of them (`allows_service` by hosts): the service list,
  its RED figures and operations (rollups carry no host). Single traces and spans are host-scoped (security review): a team
  sees the spans that ran on its hosts or whose service matches one of its service rules, so emitting lines under another
  team's service name opens no trace of that team.
* Installation-wide alert rules and policies keep seeing every host.

### 3.8 Team senders post only for the team's hosts (security review)

`ingest_guard.HostGuard`, asked once per batch by `LiveStore._accept`, so the Python receiver and the Go parse path
(`ingest_parsed`, spool consumer) apply the same rule. A team sender is an agent whose record carries a team (team enrolment
key, or enrolled by a team account; the team is read from the agent row by the `agent_id` the receiver stamped after the
token check, never from the request) or a team-owned pull source. Per distinct host of the batch: the team's host is kept;
another team's host, or a brand-new name another team's host rule matches, is re-attributed to the sender's own host (the
name the line gave stays in `host.reported`); a host no team owns is claimed for the team when it is brand-new (or
`teams_self_claim` is on), at most 30 per sender per minute, else re-attributed. Installation-wide senders (fleet key, admin
enrolment without a team, the legacy shared key) are not limited. `Teams.claim` applies the same host-rule check, so a team
manager cannot pre-register another team's future host names either.

---

## 4. API

Pydantic models use `Optional[...]`, `List[...]`, `Union[...]` (Python 3.9). While touching `users.py`, `actions.py` and
`anomalies.py`, replace their `str | None` model fields with `Optional[str]` (they fail on 3.9 at import).

### 4.1 Teams router (`/api/teams`)

Static paths are declared before `/{tid}`.

| Method, path | Who | Body / query | Response |
|---|---|---|---|
| `GET /teams` | `page.teams` → all; else teams where `manages` (403 when none) | | `{"teams": [Team], "fields", "ops"}` |
| `GET /teams/permissions` | any signed-in user | | `Catalogue` |
| `GET /teams/mine` | any | | **unchanged**: `{"all", "teams": [name], "hosts"}` |
| `GET /teams/known-hosts` | `page.teams` | | unchanged |
| `GET /teams/unowned` | `page.teams` | | `[{"host", "env", "ip", "application", "seen"}]` |
| `POST /teams/preview` | `page.teams` | `TeamIn` | unchanged |
| `POST /teams` | `page.teams` | `TeamIn` | 201 `Team` (with three roles) |
| `GET /teams/{tid}` | `manages(tid)` | | `Team` |
| `PUT /teams/{tid}` | `page.teams` | `TeamIn` | `Team` |
| `DELETE /teams/{tid}` | `page.teams` | `?transfer=install` or `?transfer=<tid>` | 204; 409 `{"detail", "owned": {...}}` when it owns objects and no transfer |
| `POST /teams/{tid}/roles` | `manages` | `TeamRoleIn` | 201 `Role` |
| `PUT /teams/{tid}/roles/{rid}` | `manages` | `TeamRoleIn` | `Role` |
| `POST /teams/{tid}/roles/{rid}/reset` | `manages` | | `Role` |
| `DELETE /teams/{tid}/roles/{rid}` | `manages` | `?move_to=<rid>` | 204; 409 when held and no `move_to` |
| `GET /teams/{tid}/members` | member of tid or `page.teams` | | `[{"name", "role_id", "role_label", "email"?}]` |
| `PUT /teams/{tid}/members/{email}` | `manages` | `{"role_id": int}` | `Membership` (adds or changes) |
| `DELETE /teams/{tid}/members/{email}` | `manages` | | 204 |
| `POST /teams/{tid}/hosts` | `manages` | `{"hosts": [str], "move": false}` | `{"added": [str], "rejected": [{"host", "reason"}]}` |
| `DELETE /teams/{tid}/hosts/{host}` | `manages` | | 204 (explicit hosts only; 409 for rule-matched) |
| `GET /teams/{tid}/enroll-key` | `can_in(tid, "act.connect")` | | `{"key": "wk_…" or "", "enabled": bool}` |
| `POST /teams/{tid}/enroll-key/rotate` | same | | `{"key"}` |
| `DELETE /teams/{tid}/enroll-key` | same | | `{"ok": true}` |

```python
class Rule(BaseModel):            # unchanged + origin
    field: str
    op: str = "glob"
    value: str
    origin: Optional[str] = None  # "manual" | "rum"

class MemberIn(BaseModel):
    email: str
    role: Optional[str] = None    # role key in this team ("manager" | "operator" | "viewer" | custom)
    role_id: Optional[int] = None

class TeamIn(BaseModel):
    name: str
    description: str = ""
    rules: List[Rule] = []
    hosts: List[str] = []
    members: List[Union[str, MemberIn]] = []   # a plain e-mail keeps the member's current role, new ones get the default role
    copy_roles_from: Optional[int] = None      # POST only

class TeamRoleIn(BaseModel):
    label: str
    description: str = ""
    perms: List[str] = []
```

Rules on role writes: `perms = normalize(perms, team=True)`; a team manager without `page.teams` gets 403 when
`perms ⊄ own R_T`, when `team.manage ∈ perms`, or when the edited role holds `team.manage`. Member writes: 403 for own
membership, for roles holding `team.manage` and for members holding one (unless `page.teams`).

`Team`:

```json
{"id": 3, "name": "EA", "description": "", "rules": [{"field": "host", "op": "glob", "value": "ea-*"}],
 "hosts": ["ea-db-01"], "matched": ["ea-app-01", "ea-db-01"],
 "host_meta": [{"host": "ea-db-01", "origin": "inventory", "added_by": "Ayşe Y.", "added_at": "2026-10-15T09:12:00+00:00"}],
 "members": ["ea@corp.com"],
 "memberships": [{"email": "ea@corp.com", "name": "Ali", "role_id": 11, "global_role": "operator", "wider": ["act.sim"]}],
 "roles": [{"id": 10, "key": "manager", "label": "Ekip yöneticisi", "description": "…", "perms": ["…"], "template": "manager", "members": 0, "locked": true},
           {"id": 11, "key": "operator", "label": "Operatör", "perms": ["…"], "template": "operator", "members": 1, "locked": false},
           {"id": 12, "key": "viewer", "label": "İzleyici", "perms": ["…"], "template": "viewer", "members": 0, "locked": false}],
 "default_role_id": 12,
 "owned": {"thresholds": 0, "recipients": 0, "groups": 0, "rules": 0, "agents": 1, "datasets": 0},
 "conflicts": [], "can_manage": true}
```

`hosts`, `members` and `matched` keep today's meaning (the existing UI and tests read them). `locked` is computed for
the caller (true when they may not edit that role). `wider` lists installation-role permissions in `TEAM_PERMS` that the
team role lacks (shown only to `page.teams` holders).

`Catalogue` (`GET /teams/permissions`):

```json
{"sections": [{"id": "home", "tr": "Ana sayfa", "en": "Home",
               "items": [{"key": "page.ops", "kind": "view", "team": true, "requires": null, "tr": "Ana sayfa: canlı akış, metrikler, SLO", "en": "…"},
                         {"key": "act.report", "kind": "action", "team": true, "requires": "page.ops", "tr": "SLO raporu indirme", "en": "…"},
                         {"key": "act.sim", "kind": "action", "team": false, "requires": null, "tr": "Simülasyon modunu açıp kapatma", "en": "…"}]}],
 "templates": {"manager": {"tr": "Ekip yöneticisi", "en": "Team manager", "desc_tr": "…", "desc_en": "…", "perms": ["…"]},
               "operator": {...}, "viewer": {...}},
 "team_perms": ["…"]}
```

### 4.2 Security dependencies (`api/security.py`, additive)

```python
def access(request: Request, user: dict = Depends(current_user)) -> Access   # services.access_of(user)
def require(perm: str)            # same signature; now `access.can(perm)` (effective permissions)
def perm_scope(perm: str)         # dependency -> Optional[Scope]; 403 unless access.can(perm); returns access.scope(perm)
def require_install(perm: str)    # 403 unless access.install(perm)
def pick_team(acc: Access, perm: str, team_id: Optional[int]) -> Optional[int]
    # None for installation-wide callers without team_id; team_id must be in acc.team_ids(perm) (else 403);
    # scoped caller without team_id: the only team, else HTTPException(400, "team_id required")
def user_scope(...)               # kept, = access.scope(None)
```

`Services` (small additive hunks; `services.py` is shared with another workflow): create
`self.access = AccessResolver(self.roles, self.teams, wo_settings.load)` after Teams, add
`def access_of(self, user): return self.access.of(user)`, make `scope_of` return `self.access_of(user).scope(None)`, set
`self.agents.team_hooks = self.teams` and pass `team_hosts_fn` to Thresholds.

### 4.3 Auth and users

`GET /auth/me` adds fields (existing ones unchanged; `permissions` becomes the effective set):

```json
{"id": 7, "email": "ea@corp.com", "name": "Ali", "role": "member", "must_change": false,
 "permissions": ["act.inventory", "page.inv", "…"], "global_permissions": ["page.readme"],
 "all": false,
 "teams": [{"id": 3, "name": "EA", "role_id": 11, "role_label": "Operatör", "permissions": ["…"], "hosts": 12, "manage": false}]}
```

* `GET /users` rows gain `teams: [{"id", "name", "role_label"}]`.
* `POST /users` (`UserIn`) gains `team_id: Optional[int] = None`, `team_role_id: Optional[int] = None`; with a team and
  no explicit `role`, the installation role becomes `member`.
* `GET /roles` adds `"sections"` (the catalogue) so the installation role editor groups by sidebar too.
* `PUT /roles/{name}` saves `normalize(perms, team=False)`; role, user-role and team writes call
  `svc.access.invalidate()`.

### 4.4 Other routers (contract deltas only)

* `PUT /inventory`, `POST /inventory/promote`, `POST /agents`: body accepts `team_id: Optional[int]`; inventory import
  accepts `?team_id=`. Responses add `team_ids`.
* `PolicyIn` (thresholds), `RecipientIn`, `GroupIn`, `RuleIn` (notify): `team_id: Optional[int] = None`. List rows add
  `team_id`, `team` (name) and `editable`.
* Dataset uploads: optional form / body field `team_id`; dataset summaries add `team_id`, `team`.
* `POST /notify/recipients/{rid}/test` (new): `can_in(row.team_id, "act.alerts")` or `install("sys.notify")`.
* Anomalies: reads `page.anom`, writes `act.anomaly`; actions: `page.actions`; thresholds reads: `page.thr`.

---

## 5. UI

Shared component `web/src/components/PermGrid.tsx`: renders the catalogue as a matrix — rows grouped by section, one
column per role. Used by the team editor (several columns) and by Users › Kurulum rolleri (one column).

### 5.1 Team editor (`Teams.tsx`)

The modal keeps its size (`wide`) and gets a segmented control (`.seg`) with four parts; "Önizleme" stays visible on the
Sunucular part. Team managers open the same editor as a page at `/team` ("Ekibim", 5.4) with rules read-only.

```
EA · Düzenle                                              [Genel] [Sunucular] [Roller] [Üyeler]
─ Roller ─────────────────────────────────────────────────────────────────────────────────────
Her ekip üç hazır rolle gelir. İşaretlediğiniz özellikler yalnız bu ekibin sunucularında geçerlidir.
                                   Ekip yöneticisi 🔒   Operatör ▾        İzleyici ▾       [+ Rol]
                                   0 üye                1 üye            0 üye
ANA SAYFA                          ☑ 3/3                ☑ 3/3            ▣ 2/3
  Ana sayfa: canlı akış…  görüntüleme  ☑                ☑                ☑
  SLO raporu indirme      işlem        ☑                ☑                ☑
  Log dosyası dışa aktarma işlem       ☑                ☑                ☐
  Kurulum rolüyle verilir: Simülasyon modu
APM                                ☑ 4/4                ☑ 4/4            ▣ 3/4
  …
AYARLAR                            ☑ 4/4                ▣ 3/4            ▣ 1/4
  Ekibi yönetme: üyeler…  yönetim      ☑                ☐                ☐
                                                         [Kaydet]
```

* Section rows: uppercase small muted label (like sidebar group labels), one tri-state checkbox per role column (checked
  / indeterminate / empty) that selects or clears the section's team-assignable items, and `n/m` count.
* Item rows: label, a muted kind chip (`görüntüleme` / `işlem` / `yönetim`), one checkbox per role. Ticking an action
  ticks its `requires` view; unticking a view unticks the actions that need it (same closure as the server).
* Installation-only items are not checkboxes: one muted line per section, "Kurulum rolüyle verilir: …".
* Column header menu (▾): Yeniden adlandır, Açıklama, Şablona dön (template roles), Varsayılan rol yap, Sil (asks for
  "Üyeleri şu role taşı" when the role has members). `+ Rol` adds a column (name input, starts empty or as a copy of a
  chosen column).
* Locked columns (🔒, tooltip "Ekip yöneticisi rolünü yalnız kurulum yöneticisi düzenler") render disabled checkboxes; cells
  a team manager may not grant (not in their own role) are disabled with tooltip "Kendi rolünüzde olmayan yetki verilemez".
* One "Kaydet" saves every changed column (`PUT /teams/{tid}/roles/{rid}` per dirty role); unsaved columns show a dot.
* The matrix sits in `.table-wrap` (horizontal scroll inside the card, never the page) with a sticky first column.

Üyeler part: table Kullanıcı (name, muted e-mail) · Ekip rolü (select of the team's roles) · Kurulum rolü (badge; amber
chip "geniş" with the `wider` list as tooltip, and for installation admins an inline button "Ekip üyesi yap") · remove.
Add row: installation admins pick from the user list (searchable select); team managers type an e-mail ("E-posta ile
üye ekle") and choose a role.

Sunucular part: rules editor (as today; read-only for team managers), explicit hosts as chips with origin
(elle / envanter / keşif / ajan / içe aktarma) and ×, "Sunucu ekle" input that shows per-host rejections inline
(`başka bir ekibe ait`, `sahipsiz sunucu: kurulum yöneticisi atar`), the live preview card, and for installation admins a
collapsed "Sahipsiz sunucular (n)" list with "Bu ekibe ata". Holders of `act.connect` see "Ekip kayıt anahtarı" (masked
key, copy, yenile, kapat, and the install one-liner with the key).

Teams list table: Ekip · Kurallar · Sunucu · Roller (chips "1 yönetici · 3 operatör · 2 izleyici") · İşlemler. New team
dialog has "Rolleri şuradan kopyala" (optional team select); otherwise the three templates are created.

### 5.2 Users page (`Users.tsx`)

* Tabs: Kullanıcılar · **Kurulum rolleri** (was Roller) · Ekipler · Olaylar.
* Users table: "Kurulum rolü" select (now includes Ekip üyesi), new "Ekipler" column with chips "EA · Operatör", a team
  filter above the table, and a muted "geniş" chip when the installation role grants team-assignable permissions that
  none of the user's team roles has.
* Yeni kullanıcı: optional "Ekip" and "Ekip rolü" selects; choosing a team switches the installation role default to
  Ekip üyesi.
* Kurulum rolleri: the role list stays on the left; the right side uses `PermGrid` with one column, sections in sidebar
  order, `team.manage` hidden. Lead text: "Kurulum rolü kullanıcının tüm ekiplerinde geçerlidir. Yalnız ekip rolleriyle
  yönetmek için kullanıcıya Ekip üyesi verin."

### 5.3 Top bar team badge (`Layout.tsx` `ScopeBadge`)

Reads `user.teams` and `user.all` from `/auth/me` (no extra request). Hidden for `all` and for users in no team (unless
strict, then an amber "ekip yok · 0 sunucu"). One team: `👥 EA · Operatör · 12 sunucu`; several: `👥 EA, İK · 18
sunucu`. Click opens a small popover card: one row per membership (ekip, rol, sunucu sayısı, "Ekibi yönet" link when
`manage`) and the hint `tm_scope_hint`. Calm `.badge blue`, no new colours.

### 5.4 Navigation and pages

* Sidebar: Anomaliler uses `page.anom`, Aksiyonlar `page.actions`, Eşikler `page.thr`; Bildirimler is visible for
  `sys.notify` or `act.alerts` (`Item.any?: string[]`). New Ayarlar item **Ekibim** (`/team`, perm `team.manage`, hidden
  when `page.teams`) rendering `pages/MyTeam.tsx` (team picker when managing several).
* `auth.tsx`: `User` gains `teams`, `all`, `global_permissions`; add `canIn(teamId, perm)` and
  `teamsWith(perm): number[]`. `can()` is unchanged (effective permissions).
* Pages with team-owned writes (Inventory, Connections, Thresholds, Notify, Datasets) show an "Ekip" select only when
  `teamsWith(perm).length > 1`; rows show a muted team chip for installation-wide users; edit buttons appear only when
  the row's team allows it. NotifyBell and the home page's anomaly widgets check `page.anom`.

### 5.5 i18n keys (insert before `tm_f_host:`)

```
tmr_tab_general ["Genel","General"] · tmr_tab_hosts ["Sunucular","Hosts"] · tmr_tab_roles ["Roller","Roles"] · tmr_tab_members ["Üyeler","Members"]
tmr_lead ["Her ekip üç hazır rolle gelir. İşaretlediğiniz özellikler yalnız bu ekibin sunucularında geçerlidir.","Every team starts with three ready-made roles. What you tick applies only to this team's hosts."]
tmr_role_add ["Rol ekle","Add role"] · tmr_role_rename ["Yeniden adlandır","Rename"] · tmr_role_reset ["Şablona dön","Back to template"]
tmr_role_default ["Varsayılan rol yap","Make default role"] · tmr_role_move ["Üyeleri şu role taşı","Move members to"] · tmr_role_copy ["Rolleri şuradan kopyala","Copy roles from"]
tmr_all ["Tümü","All"] · tmr_kind_view ["görüntüleme","view"] · tmr_kind_action ["işlem","action"] · tmr_kind_manage ["yönetim","manage"]
tmr_install_only ["Kurulum rolüyle verilir: {list}","Granted by installation roles only: {list}"]
tmr_locked ["Ekip yöneticisi rolünü yalnız kurulum yöneticisi düzenler.","Only installation admins edit the team manager role."]
tmr_no_grant ["Kendi rolünüzde olmayan yetki verilemez.","You cannot grant a permission your own role lacks."]
tmr_member_role ["Ekip rolü","Team role"] · tmr_global_role ["Kurulum rolü","Installation role"] · tmr_wider ["geniş","wider"]
tmr_wider_tip ["Kurulum rolü bu üyeye ekip rolünün vermediği yetkiler veriyor: {list}","The installation role grants permissions the team role does not: {list}"]
tmr_make_member ["Ekip üyesi yap","Make team member"] · tmr_add_email ["E-posta ile üye ekle","Add member by e-mail"]
tmr_origin_manual ["elle","by hand"] · tmr_origin_inventory ["envanter","inventory"] · tmr_origin_promote ["keşif","discovery"] · tmr_origin_agent ["ajan","agent"] · tmr_origin_import ["içe aktarma","import"]
tmr_host_add ["Sunucu ekle","Add host"] · tmr_rej_other ["başka bir ekibe ait","belongs to another team"] · tmr_rej_unowned ["sahipsiz sunucu: kurulum yöneticisi atar","unowned host: an installation admin assigns it"]
tmr_unowned ["Sahipsiz sunucular","Unowned hosts"] · tmr_assign ["Bu ekibe ata","Assign to this team"]
tmr_enroll ["Ekip kayıt anahtarı","Team enrolment key"] · tmr_enroll_hint ["Bu anahtarla bağlanan sunucular otomatik olarak bu ekibe katılır.","Servers enrolled with this key join this team automatically."]
tmr_my_team ["Ekibim","My team"] · tmr_manage ["Ekibi yönet","Manage team"] · tmr_team ["Ekip","Team"] · tmr_install_wide ["Kurulum geneli","Installation-wide"]
tmr_team_required ["Bu yetki birden çok ekipte var: ekip seçin.","You hold this in several teams: pick one."]
tmr_owned_block ["Ekibin sahip olduğu kayıtlar var: {list}.","The team owns records: {list}."] · tmr_transfer_install ["Kurulum geneline devret ve sil","Transfer to installation and delete"]
tmr_roles_count ["{m} yönetici · {o} operatör · {v} izleyici","{m} managers · {o} operators · {v} viewers"]
us_global_roles ["Kurulum rolleri","Installation roles"] · us_teams_col ["Ekipler","Teams"]
us_global_lead ["Kurulum rolü kullanıcının tüm ekiplerinde geçerlidir. Yalnız ekip rolleriyle yönetmek için kullanıcıya Ekip üyesi verin.","The installation role applies in all of the user's teams. To manage access only through team roles, give the user Team member."]
```

Section and permission labels come from the catalogue (API), not from i18n. Styles appended to `styles.css`:
`.perm-matrix` (sticky first column, centred checkbox cells, `var(--line)` row borders), `.perm-sec` (section row:
uppercase, `font-size: 11px`, muted, letter-spacing as the sidebar `.group`), `.perm-kind` (muted chip), `.perm-col.dirty`
(dot after the column label), `.scope-pop` (badge popover). Only existing CSS variables; both themes.

---

## 6. Tests

New `tests/test_team_roles.py` (fixture like `tests/test_teams.py`: temp env, `create_app()`, `TestClient`):

1. **Templates**: `POST /teams` creates exactly manager / operator / viewer with `TEAM_TEMPLATES` perms and
   `default_role_id` = viewer; `copy_roles_from` copies custom roles and perms; the `member` installation role exists
   after the first team.
2. **Normalize**: a team role saved with `sys.update`, `scope.all`, `page.llm`, `bogus` keeps none of them;
   `act.inventory` alone yields `{act.inventory, page.inv}`; an installation role never keeps `team.manage`.
3. **Effective permissions**: member-role user in EA (Operatör) and İK (İzleyici): `/auth/me` permissions = union,
   `teams` lists both with role labels; `PUT /inventory` on an İK host → 403, on an EA host → 200; `GET /apm/services`
   shows both teams' services; `POST /anomalies/{id}/status` on an İK anomaly → 404; same user with installation role
   `operator` → the İK write succeeds (G applies in all teams).
4. **No team / strict / admin / scope.all**: unchanged behaviour (re-assert the `test_teams.py` expectations).
5. **Team manager limits**: may change Operatör perms within their own; 403 granting a perm outside their role, granting
   `team.manage`, editing the manager role, assigning it, changing own role, touching another team, calling
   `/teams/known-hosts`, `/teams/unowned`, `PUT /teams/{tid}`; may add a member by e-mail; unknown and disabled e-mails
   give the same 404 body; `GET /teams` returns only managed teams; `/teams/{tid}/members` hides e-mails from non-managers.
6. **Host claims**: new hostname via inventory → explicit host with origin `inventory`, visible to the team, 404 for
   another team's `GET /inventory/{h}`; host visible to another team → 409 / `other_team`; known unowned → `unowned`
   unless `teams_self_claim`; installation admin `move=True` works; inventory stats / export / import scoped; import rows
   outside scope land in `errors`.
7. **Agents**: team enrolment key self-enrolment sets `team_id` and claims the host; a name visible to another team is
   refused; fleet-key endpoints 403 for scoped users; `/agents/{aid}` of another team → 404.
8. **Thresholds**: a team `all` policy affects only team hosts in `effective()`; team beats installation at any
   specificity; scoped user cannot create a NULL-team policy, can create a team one with `act.thresholds`; webhook channel
   and foreign targets rejected for team policies; preview intersects with the team.
9. **Notify**: team recipients / groups / rules CRUD by `act.alerts`, listing filtered; a team rule evaluates only team
   hosts; alert history filtered by `team_id`.
10. **Datasets and actions**: a dataset uploaded by a scoped user carries `team_id` and is invisible to another team; an
    action from an anomaly carries the host and follows its scope.
11. **Migration**: build the old schema (teams + members, roles with `page.ops` / `page.data`), start the app: roles
    exist, members mapped by installation role, `split_v1` adds `page.anom/page.thr/page.actions` once (remove one,
    restart, it stays removed), every user's `/live/events` host set is identical before and after.
12. **Delete**: team owning a policy → 409 with `owned`; `?transfer=install` moves it to NULL-team and disables it; a team
    with only explicit hosts deletes with 204.
13. **Cache**: changing a member's role or a role's perms takes effect on the next request (no 30 s wait).

Also: `tests/test_teams.py`, `tests/test_api.py`, `tests/test_auth_admin.py` unchanged and green; full suite
`python -m pytest -q -p no:cacheprovider`. Web: `npx tsc --noEmit -p .` and `npm run build`; a playwright flow
(scratchpad `flow3.mjs` style): create team → Roller tab → untick İzleyici › APM section → save → reload shows it;
Üyeler → set role; sign in as the member → sidebar shows only ticked sections, badge reads "EA · İzleyici · n sunucu".

## 7. Implementation order

Each step ships green (`make test`) and on its own:

1. `rbac.py` catalogue, split keys, `normalize`, templates, `rbac_meta` + `split_v1`; routers switch to `page.anom`,
   `page.thr`, `page.actions`; Layout nav keys.
2. `team_roles`, membership roles, `team_roles_v1` migration, `access.py`, `security.py` (`require` via access,
   `perm_scope`, `require_install`, `pick_team`), `Services.access_of`, `/auth/me`; every `user_scope` →
   `perm_scope(...)`.
3. Teams router role / member / host endpoints, manager limits, audit events.
4. Host ownership: `claim`, inventory / agents / enrolment keys, scan, ITSM scoping.
5. Team-owned thresholds and notify; datasets and actions.
6. UI: `PermGrid`, team editor parts, Users page, badge, Ekibim, team selects on the affected pages; i18n; CSS.
