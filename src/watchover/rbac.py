"""Roles and permissions. A role is a named set of permission keys; three built-in roles ship with sane defaults and any number of
custom roles can be added from System › Users. Administrators always hold every permission (the built-in admin role cannot be
narrowed, so nobody can lock themselves out). Permission keys are checked by the UI: page.* decides what the navigation shows,
sys.* which System tabs open, act.* which actions are available on the pages."""
from __future__ import annotations

import json

PERMISSIONS: dict[str, tuple[str, str, str]] = {   # key: (group, Turkish label, English label)
    "page.ops": ("page", "Ana sayfa: canlı akış, metrikler, SLO", "Home: live feed, metrics, SLO"), "page.data": ("page", "Veri setleri", "Datasets"), "page.src": ("page", "Kaynaklar", "Sources"),
    "page.inv": ("page", "Envanter", "Inventory"), "page.map": ("page", "Hata haritası", "Failure map"), "page.assist": ("page", "Watchover'a sor", "Ask Watchover"),
    "page.llm": ("page", "LLM", "LLM"), "page.pb": ("page", "Playbook", "Playbook"), "page.itsm": ("page", "ITSM", "ITSM"), "page.conn": ("page", "Bağlantı ayarları", "Connection settings"),
    "page.apm": ("page", "APM: servisler ve izler", "APM: services and traces"), "page.rum": ("page", "Kullanıcı deneyimi (web ve mobil)", "User experience (web and mobile)"), "page.parser": ("page", "Parser desenleri", "Parser patterns"), "page.scan": ("page", "Zafiyet taraması", "Vulnerability scan"), "page.users": ("page", "Kullanıcılar ve roller sayfası", "Users and roles page"), "page.sys": ("page", "Sistem sayfası", "System page"), "page.readme": ("page", "README", "README"),
    "page.anom": ("page", "Anomaliler", "Anomalies"), "page.thr": ("page", "Eşikler", "Thresholds"), "page.actions": ("page", "Aksiyonlar", "Actions"),
    "page.logs": ("page", "Loglar (log gezgini)", "Logs (log explorer)"), "act.logalert": ("act", "Log sorgusundan uyarı kurma", "Create alerts from a log query"),
    "act.sim": ("act", "Simülasyon modunu açıp kapatma", "Toggle simulation mode"), "act.connect": ("act", "Sunucu bağlama (ajan kaydı)", "Connect a server (enrol an agent)"),
    "act.report": ("act", "SLO raporu indirme", "Download SLO reports"), "act.export": ("act", "Log dosyası dışa aktarma", "Export log files"),
    "act.sources": ("act", "Kaynak ekleme ve düzenleme", "Add and edit sources"), "act.inventory": ("act", "Envanter düzenleme", "Edit the inventory"),
    "act.anomaly": ("act", "Anomali takibi: adımlar, atama, kapatma", "Anomaly tracking: steps, assignment, closing"),
    "act.parser": ("act", "Parser deseni ekleme ve düzenleme", "Add and edit parser patterns"),
    "act.scan": ("act", "Zafiyet taraması çalıştırma (ağ kontrolü dahil)", "Run vulnerability scans (including network probe)"),
    "act.itsm": ("act", "ITSM entegrasyon ayarları", "ITSM integration settings"),
    "act.thresholds": ("act", "Ekip eşik politikalarını düzenleme", "Edit the team's threshold policies"),
    "act.alerts": ("act", "Ekip bildirim alıcıları, grupları ve kuralları", "Team alert recipients, groups and rules"),
    "scope.all": ("scope", "Tüm sunucuları gör (ekip kısıtını aşar)", "See every host (overrides team scope)"), "page.teams": ("page", "Ekipler ve sunucu grupları", "Teams and host groups"),
    "act.rum": ("act", "Web / mobil uygulama anahtarları ve kullanıcı arama (denetim kaydıyla)", "Web / mobile app keys and user lookup (audited)"),
    "sys.status": ("sys", "Sistem › Durum", "System › Status"), "sys.maint": ("sys", "Sistem › Bakım", "System › Maintenance"), "sys.update": ("sys", "Sistem › Güncelleme ve sürüm geçmişi", "System › Update and version history"),
    "sys.auth": ("sys", "Sistem › Giriş sağlayıcıları", "System › Sign-in providers"),
    "sys.notify": ("sys", "Sistem › Bildirimler", "System › Alerts"), "sys.security": ("sys", "Sistem › Güvenlik günlüğü", "System › Security log"),
    "team.manage": ("team", "Ekibi yönetme: üyeler, roller, sunucular", "Manage the team: members, roles, hosts"),
}
GROUPS = {"page": ("Sayfalar", "Pages"), "act": ("İşlemler", "Actions"), "sys": ("Sistem sekmeleri", "System tabs"), "scope": ("Görünürlük", "Visibility"), "team": ("Ekip", "Team")}
ALL = frozenset(PERMISSIONS)
SPLIT_PERMS = frozenset({"page.anom", "page.thr", "page.actions", "act.thresholds", "act.alerts", "team.manage"})    # role-based teams (v1.46)
NEW_PERMS = frozenset({"page.parser", "act.parser", "page.scan", "act.scan", "act.itsm", "page.apm", "page.rum", "act.rum", "page.teams"}) | SPLIT_PERMS   # added after roles started being stored in the database
LOG_PERMS = frozenset({"page.logs", "act.logalert"})     # log explorer (docs/design/logs.md)
NEW_PERMS = NEW_PERMS | LOG_PERMS

# ---------------------------------------------------------------- the catalogue, grouped like the sidebar
SECTIONS = [("home", "Ana sayfa", "Home"), ("apm", "APM", "APM"), ("signals", "Sinyaller", "Signals"), ("respond", "Müdahale", "Response"),
            ("intel", "Zekâ", "Intelligence"), ("security", "Güvenlik", "Security"), ("settings", "Ayarlar", "Settings")]
FEATURES: dict[str, tuple[str, str, bool]] = {       # key -> (section, kind view | action | manage, assignable in a team role)
    "page.ops": ("home", "view", True), "act.report": ("home", "action", True), "act.export": ("home", "action", True), "act.sim": ("home", "action", False),
    "page.apm": ("apm", "view", True), "page.rum": ("apm", "view", True), "act.rum": ("apm", "action", True), "page.map": ("apm", "view", True),
    "page.logs": ("signals", "view", True), "act.logalert": ("signals", "action", True),
    "page.data": ("signals", "view", True), "page.inv": ("signals", "view", True), "act.inventory": ("signals", "action", True),
    "page.anom": ("signals", "view", True), "act.anomaly": ("signals", "action", True),
    "page.actions": ("respond", "view", True), "page.thr": ("respond", "view", True), "act.thresholds": ("respond", "action", True), "page.pb": ("respond", "view", True),
    "page.itsm": ("respond", "view", True), "act.itsm": ("respond", "action", False), "act.alerts": ("respond", "action", True), "sys.notify": ("respond", "action", False),
    "page.assist": ("intel", "view", True), "page.llm": ("intel", "view", False), "page.parser": ("intel", "view", False), "act.parser": ("intel", "action", False),
    "page.scan": ("security", "view", True), "act.scan": ("security", "action", True),
    "page.conn": ("settings", "view", True), "act.connect": ("settings", "action", True), "page.readme": ("settings", "view", True), "team.manage": ("settings", "manage", True),
    "page.src": ("settings", "view", False), "act.sources": ("settings", "action", False), "page.users": ("settings", "view", False), "page.teams": ("settings", "manage", False),
    "page.sys": ("settings", "view", False), "sys.status": ("settings", "view", False), "sys.maint": ("settings", "action", False), "sys.update": ("settings", "action", False),
    "sys.auth": ("settings", "manage", False), "sys.security": ("settings", "view", False), "scope.all": ("settings", "manage", False),
}
REQUIRES: dict[str, str] = {"act.report": "page.ops", "act.export": "page.ops", "act.rum": "page.rum", "act.inventory": "page.inv", "act.anomaly": "page.anom",
                            "act.thresholds": "page.thr", "act.scan": "page.scan", "act.connect": "page.conn", "act.sources": "page.src", "act.parser": "page.parser"}
REQUIRES["act.logalert"] = "page.logs"
TEAM_ONLY = frozenset({"team.manage"})
TEAM_PERMS = frozenset(k for k, v in FEATURES.items() if v[2])
_V = {"page.ops", "act.report", "page.apm", "page.rum", "page.map", "page.data", "page.inv", "page.anom", "page.actions", "page.thr", "page.pb", "page.itsm", "page.readme"}
_O = _V | {"act.export", "act.rum", "act.inventory", "act.anomaly", "page.assist", "page.scan", "act.scan", "page.conn", "act.connect"}
_V.add("page.logs"); _O |= {"page.logs", "act.logalert"}          # log explorer: every team role sees its hosts' lines, operators set alerts
TEAM_TEMPLATES: dict[str, dict] = {
    "manager": {"label": ("Ekip yöneticisi", "Team manager"), "desc": ("Ekibin her şeyi: üyeler, roller, eşikler ve bildirimler dahil.", "Everything in the team, including members, roles, thresholds and alerts."),
                "perms": _O | {"act.thresholds", "act.alerts", "team.manage"}},
    "operator": {"label": ("Operatör", "Operator"), "desc": ("Ekibin sunucularında izleme ve günlük işler.", "Monitoring and day-to-day work on the team's hosts."), "perms": set(_O)},
    "viewer": {"label": ("İzleyici", "Viewer"), "desc": ("Ekibin sunucularını yalnız izler.", "Read-only view of the team's hosts."), "perms": set(_V)},
}
TEMPLATE_ORDER = ("manager", "operator", "viewer")
MEMBER_ROLE = {"label": ("Ekip üyesi", "Team member"), "desc": ("Kurulum genelinde yetkisi yok; ne göreceğini ekip rolü belirler.", "No installation-wide permission; the team role decides what the user sees."),
               "perms": {"page.readme"}}


def normalize(perms, team: bool = False) -> set[str]:
    """Known keys only, plus the view every ticked action needs. team=True keeps only team-assignable keys; an installation role
    never keeps team-only keys (team.manage)."""
    p = {str(x) for x in (perms or ())} & ALL
    p |= {REQUIRES[k] for k in p if k in REQUIRES}
    return (p & TEAM_PERMS) if team else (p - TEAM_ONLY)


def catalogue() -> dict:
    """The permission catalogue grouped like the sidebar (GET /api/teams/permissions, the role editors)."""
    secs = []
    for sid, tr, en in SECTIONS:
        items = [{"key": k, "kind": f[1], "team": f[2], "requires": REQUIRES.get(k), "tr": PERMISSIONS[k][1], "en": PERMISSIONS[k][2]}
                 for k, f in FEATURES.items() if f[0] == sid]
        secs.append({"id": sid, "tr": tr, "en": en, "items": items})
    return {"sections": secs, "team_perms": sorted(TEAM_PERMS),
            "templates": {k: {"tr": v["label"][0], "en": v["label"][1], "desc_tr": v["desc"][0], "desc_en": v["desc"][1], "perms": sorted(v["perms"])}
                          for k, v in TEAM_TEMPLATES.items()}}


BUILTIN: dict[str, dict] = {
    "admin": {"label": ("Yönetici", "Administrator"), "desc": ("Her yetki; daraltılamaz.", "Every permission; cannot be narrowed."), "perms": set(ALL)},
    "operator": {"label": ("Operatör", "Operator"), "desc": ("Operasyon, veri, kaynak, envanter, playbook ve ITSM işleri; sistem yönetimi yok.", "Operations, data, sources, inventory, playbook and ITSM work; no system administration."),
                 "perms": {k for k in ALL if k.startswith("page.") and k not in ("page.sys", "page.users", "page.teams")} | {"act.sim", "act.connect", "act.report", "act.export", "act.sources", "act.inventory", "act.anomaly", "act.parser", "act.scan", "act.itsm", "act.rum"}},
    "viewer": {"label": ("İzleyici", "Viewer"), "desc": ("Yalnız izleme: operasyon, veri setleri, harita, playbook, ITSM ve README.", "Read-only: operations, datasets, map, playbook, ITSM and README."),
               "perms": {"page.ops", "page.data", "page.map", "page.pb", "page.itsm", "page.readme", "act.report", "page.apm", "page.rum", "page.anom", "page.thr", "page.actions"}},
}
BUILTIN["operator"]["perms"].add("act.logalert")             # page.logs: every page.* key is in the operator role already
BUILTIN["viewer"]["perms"].add("page.logs")

# ---------------------------------------------------------------- action agent (docs/design/action-agent.md §10.9; migration auto_v1)
AUTO_DEF: dict[str, tuple] = {   # key: (TR, EN, team-assignable, requires, team templates); product owner: no act.* key in any team
    # template, each one is ticked by hand per team role (page.auto is view only)
    "page.auto": ("Otomasyon: runbook'lar, güven, öğrenme (görüntüleme)", "Automation: runbooks, trust, learning (view)", True, None, ("manager", "operator", "viewer")),
    "act.case": ("Vaka yürütme: karar, rehberli adım, teşhis, kapatma", "Work cases: decide, guided steps, diagnosis, close", True, "page.actions", ()),
    "act.execute": ("Adımı sistemle çalıştırma (Onaylı seviyede \"Çalıştır\")", "Run a step through the system (\"Run\" at Approved)", True, "page.actions", ()),
    "act.approve": ("Başkasının adımını onaylama; otomatik çalıştırmaları gözden geçirme", "Approve someone else's step; review automatic runs", True, "page.actions", ()),
    "act.runbook": ("Runbook yazma ve taslak düzenleme", "Write runbooks and edit drafts", True, "page.auto", ()),
    "act.publish": ("Runbook sürümü yayınlama (yazar olmayan)", "Publish a runbook version (not its author)", True, "page.auto", ()),
    "act.promote": ("Seviye yükseltme imzası, elle düşürme, dondurmayı ve durdurmayı kaldırma", "Sign promotions, demote, lift freezes and stops", True, "page.auto", ()),
    "act.kill": ("Otomasyonu acil durdurma (ekip, runbook, sunucu)", "Emergency-stop automation (team, runbook, host)", True, "page.auto", ()),
    "act.golden": ("Altın küme ve etiket düzeltme", "Golden set and label corrections", True, "page.auto", ()),
    "act.audit": ("Otomasyon denetim kaydını görme", "View the automation audit log", True, "page.auto", ()),
    "act.policy": ("Kurulum geneli otomasyon politikası: mod, tavanlar, bütçeler, bağlayıcılar, şablon kataloğu, model yükseltme, genel durdurmayı kaldırma",
                   "Installation automation policy: mode, caps, budgets, connectors, template catalogue, model promotion, global resume", False, "page.auto", ()),
}
for _k, (_tr, _en, _team, _req, _tpls) in AUTO_DEF.items():
    PERMISSIONS[_k] = ("page" if _k.startswith("page.") else "act", _tr, _en)
    FEATURES[_k] = ("respond", "view" if _k.startswith("page.") else ("manage" if _k == "act.policy" else "action"), _team)
    if _req:
        REQUIRES[_k] = _req
    for _t in _tpls:
        TEAM_TEMPLATES[_t]["perms"].add(_k)
AUTO_PERMS = frozenset(AUTO_DEF)
ALL = frozenset(PERMISSIONS)
TEAM_PERMS = frozenset(k for k, v in FEATURES.items() if v[2])
NEW_PERMS = NEW_PERMS | AUTO_PERMS
BUILTIN["admin"]["perms"] = set(ALL)
BUILTIN["operator"]["perms"] |= {"page.auto", "act.case", "act.runbook"}   # nothing that executes, approves, publishes or promotes
BUILTIN["viewer"]["perms"].add("page.auto")


class Roles:
    def __init__(self, kb):
        self.kb = kb
        self.version = 0                                  # bumped by every change: the access cache drops entries that predate it
        kb._exec("CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, label TEXT DEFAULT '', description TEXT DEFAULT '', perms TEXT DEFAULT '[]', builtin INTEGER DEFAULT 0)")
        kb._exec("CREATE TABLE IF NOT EXISTS rbac_meta (key TEXT PRIMARY KEY, value TEXT)")
        have = {r["name"]: set(json.loads(r["perms"] or "[]")) for r in kb._exec("SELECT name, perms FROM roles")}
        for name, d in BUILTIN.items():
            if name not in have:
                kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES (?,?,?,?,1)", (name, d["label"][0], d["desc"][0], json.dumps(sorted(d["perms"])), ))
            elif name != "admin" and have[name] == d["perms"] - NEW_PERMS:      # untouched default from an earlier version: take the permissions added since
                kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(d["perms"])), name))
        self._split_v1()
        self._logs_v1()
        self._auto_v1()

    # ---------------------------------------------------------------- one-shot steps (a marker each, so a later edit is never undone)
    def meta(self, key: str) -> str | None:
        rows = self.kb._exec("SELECT value FROM rbac_meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str = "1") -> None:
        self.kb._exec("DELETE FROM rbac_meta WHERE key=?", (key,))
        self.kb._exec("INSERT INTO rbac_meta (key, value) VALUES (?,?)", (key, value))

    def _split_v1(self) -> None:
        """page.ops used to open Home, Anomalies and Thresholds; page.data Datasets and Actions. Every stored role keeps what it could
        open: page.ops -> + page.anom, page.thr; page.data -> + page.actions. Once: an admin who unticks page.anom later keeps it off."""
        if self.meta("split_v1"):
            return
        for r in self.kb._exec("SELECT name, perms FROM roles"):
            if r["name"] == "admin":
                continue
            p = set(json.loads(r["perms"] or "[]"))
            add = ({"page.anom", "page.thr"} if "page.ops" in p else set()) | ({"page.actions"} if "page.data" in p else set())
            if add - p:
                self.kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(p | add)), r["name"]))
        self.set_meta("split_v1")

    def _logs_v1(self) -> None:
        """The log lines used to be a card of the operations page: every stored role (and team role) holding page.ops keeps them
        through page.logs; the built-in operator and team roles that manage alerts may also create alerts from a log query. Once."""
        if self.meta("logs_v1"):
            return
        for r in self.kb._exec("SELECT name, perms FROM roles"):
            if r["name"] == "admin":
                continue
            p = set(json.loads(r["perms"] or "[]"))
            add = ({"page.logs"} if "page.ops" in p else set()) | ({"act.logalert"} if r["name"] == "operator" and "page.ops" in p else set())
            if add - p:
                self.kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(p | add)), r["name"]))
        try:
            for r in self.kb._exec("SELECT id, perms FROM team_roles"):
                p = set(json.loads(r["perms"] or "[]"))
                add = ({"page.logs"} if "page.ops" in p else set()) | ({"act.logalert"} if "act.alerts" in p or "act.anomaly" in p else set())
                if add - p:
                    self.kb._exec("UPDATE team_roles SET perms=? WHERE id=?", (json.dumps(sorted(p | add)), r["id"]))
        except Exception:  # noqa: BLE001 - no team roles yet
            pass
        self.set_meta("logs_v1")
        self.version += 1

    def _auto_v1(self) -> None:
        """The action agent's keys (§10.9): every stored role and team role holding page.actions gets page.auto (view only); the
        untouched built-in operator also gets act.case and act.runbook. Nothing that executes, approves, publishes, promotes or
        sets policy is granted implicitly to an existing role. Once."""
        if self.meta("auto_v1"):
            return
        op_old = BUILTIN["operator"]["perms"] - AUTO_PERMS
        for r in self.kb._exec("SELECT name, perms FROM roles"):
            if r["name"] == "admin":
                continue
            p = set(json.loads(r["perms"] or "[]"))
            add = {"page.auto"} if "page.actions" in p else set()
            if r["name"] == "operator" and p - AUTO_PERMS == op_old:
                add |= {"page.auto", "act.case", "act.runbook"}
            if add - p:
                self.kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(p | add)), r["name"]))
        try:
            for r in self.kb._exec("SELECT id, perms FROM team_roles"):
                p = set(json.loads(r["perms"] or "[]"))
                if "page.actions" in p and "page.auto" not in p:
                    self.kb._exec("UPDATE team_roles SET perms=? WHERE id=?", (json.dumps(sorted(p | {"page.auto"})), r["id"]))
        except Exception:  # noqa: BLE001 - no team roles yet
            pass
        self.set_meta("auto_v1")
        self.version += 1

    def ensure_member(self) -> None:
        """The 'Ekip üyesi' installation role (no permission of its own; team roles decide). Created with the first team."""
        if not self.kb._exec("SELECT 1 FROM roles WHERE name='member'"):
            self.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES ('member',?,?,?,1)",
                          (MEMBER_ROLE["label"][0], MEMBER_ROLE["desc"][0], json.dumps(sorted(MEMBER_ROLE["perms"]))))
            self.version += 1

    def list(self) -> list[dict]:
        out = []
        for r in self.kb._exec("SELECT * FROM roles ORDER BY builtin DESC, name"):
            d = dict(r); d["perms"] = set(json.loads(d["perms"] or "[]")) & ALL; d["builtin"] = bool(d["builtin"])
            if d["name"] == "admin":
                d["perms"] = set(ALL)

            out.append(d)
        return out

    def names(self) -> list[str]:
        return [r["name"] for r in self.list()]

    def get(self, name: str) -> dict | None:
        rows = self.kb._exec("SELECT * FROM roles WHERE name=?", (name,))
        if not rows:
            return None
        d = dict(rows[0]); d["perms"] = set(ALL) if d["name"] == "admin" else set(json.loads(d["perms"] or "[]")) & ALL; d["builtin"] = bool(d["builtin"])
        return d

    def perms(self, name: str) -> set[str]:
        r = self.get(name)
        return set(r["perms"]) if r else set()

    def can(self, role: str, perm: str) -> bool:
        return role == "admin" or perm in self.perms(role)

    def save(self, name: str, perms: set[str] | list[str], label: str = "", description: str = "") -> dict:
        name = name.strip().lower().replace(" ", "_")
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("role name: letters, digits and underscores")
        if name == "admin":
            raise ValueError("the admin role cannot be changed")
        perms = sorted(normalize(perms, team=False))          # an installation role never holds team.manage; actions bring their view
        if self.get(name):
            self.kb._exec("UPDATE roles SET perms=?, label=COALESCE(NULLIF(?, ''), label), description=COALESCE(NULLIF(?, ''), description) WHERE name=?", (json.dumps(perms), label.strip(), description.strip(), name))
        else:
            self.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES (?,?,?,?,0)", (name, label.strip() or name, description.strip(), json.dumps(perms)))
        self.version += 1
        return self.get(name)

    def delete(self, name: str) -> None:
        r = self.get(name)
        if not r:
            return
        if r["builtin"]:
            raise ValueError("built-in roles cannot be deleted")
        if self.kb._exec("SELECT 1 FROM users WHERE role=? LIMIT 1", (name,)):
            raise ValueError("the role is still assigned to accounts")
        self.kb._exec("DELETE FROM roles WHERE name=?", (name,))
        self.version += 1

    def reset(self, name: str) -> None:
        """Put a built-in role back to its shipped permissions."""
        base = BUILTIN.get(name) or (MEMBER_ROLE if name == "member" else None)
        if base:
            self.kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(base["perms"])), name))
            self.version += 1
