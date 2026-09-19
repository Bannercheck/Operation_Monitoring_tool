"""Roles and permissions. A role is a named set of permission keys; three built-in roles ship with sane defaults and any number of
custom roles can be added from System › Users. Administrators always hold every permission (the built-in admin role cannot be
narrowed, so nobody can lock themselves out). Permission keys are checked by the UI: page.* decides what the navigation shows,
sys.* which System tabs open, act.* which actions are available on the pages."""
from __future__ import annotations

import json

PERMISSIONS: dict[str, tuple[str, str, str]] = {   # key: (group, Turkish label, English label)
    "page.ops": ("page", "Operasyon sayfası", "Operations page"), "page.data": ("page", "Veri setleri", "Datasets"), "page.src": ("page", "Kaynaklar", "Sources"),
    "page.inv": ("page", "Envanter", "Inventory"), "page.map": ("page", "Hata haritası", "Failure map"), "page.assist": ("page", "Watchover'a sor", "Ask Watchover"),
    "page.llm": ("page", "LLM", "LLM"), "page.pb": ("page", "Playbook", "Playbook"), "page.itsm": ("page", "ITSM", "ITSM"), "page.conn": ("page", "Bağlantı ayarları", "Connection settings"),
    "page.users": ("page", "Kullanıcılar ve roller sayfası", "Users and roles page"), "page.sys": ("page", "Sistem sayfası", "System page"), "page.readme": ("page", "README", "README"),
    "act.sim": ("act", "Simülasyon modunu açıp kapatma", "Toggle simulation mode"), "act.connect": ("act", "Sunucu bağlama (ajan kaydı)", "Connect a server (enrol an agent)"),
    "act.report": ("act", "SLO raporu indirme", "Download SLO reports"), "act.export": ("act", "Log dosyası dışa aktarma", "Export log files"),
    "act.sources": ("act", "Kaynak ekleme ve düzenleme", "Add and edit sources"), "act.inventory": ("act", "Envanter düzenleme", "Edit the inventory"),
    "sys.status": ("sys", "Sistem › Durum", "System › Status"), "sys.maint": ("sys", "Sistem › Bakım", "System › Maintenance"), "sys.update": ("sys", "Sistem › Güncelleme ve sürüm geçmişi", "System › Update and version history"),
    "sys.auth": ("sys", "Sistem › Giriş sağlayıcıları", "System › Sign-in providers"),
    "sys.notify": ("sys", "Sistem › Bildirimler", "System › Alerts"), "sys.security": ("sys", "Sistem › Güvenlik günlüğü", "System › Security log"),
}
GROUPS = {"page": ("Sayfalar", "Pages"), "act": ("İşlemler", "Actions"), "sys": ("Sistem sekmeleri", "System tabs")}
ALL = frozenset(PERMISSIONS)
BUILTIN: dict[str, dict] = {
    "admin": {"label": ("Yönetici", "Administrator"), "desc": ("Her yetki; daraltılamaz.", "Every permission; cannot be narrowed."), "perms": set(ALL)},
    "operator": {"label": ("Operatör", "Operator"), "desc": ("Operasyon, veri, kaynak, envanter, playbook ve ITSM işleri; sistem yönetimi yok.", "Operations, data, sources, inventory, playbook and ITSM work; no system administration."),
                 "perms": {k for k in ALL if k.startswith("page.") and k not in ("page.sys", "page.users")} | {"act.sim", "act.connect", "act.report", "act.export", "act.sources", "act.inventory"}},
    "viewer": {"label": ("İzleyici", "Viewer"), "desc": ("Yalnız izleme: operasyon, veri setleri, harita, playbook, ITSM ve README.", "Read-only: operations, datasets, map, playbook, ITSM and README."),
               "perms": {"page.ops", "page.data", "page.map", "page.pb", "page.itsm", "page.readme", "act.report"}},
}


class Roles:
    def __init__(self, kb):
        self.kb = kb
        kb._exec("CREATE TABLE IF NOT EXISTS roles (name TEXT PRIMARY KEY, label TEXT DEFAULT '', description TEXT DEFAULT '', perms TEXT DEFAULT '[]', builtin INTEGER DEFAULT 0)")
        have = {r["name"] for r in kb._exec("SELECT name FROM roles")}
        for name, d in BUILTIN.items():
            if name not in have:
                kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES (?,?,?,?,1)", (name, d["label"][0], d["desc"][0], json.dumps(sorted(d["perms"])), ))

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
        return next((r for r in self.list() if r["name"] == name), None)

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
        perms = sorted(set(perms) & ALL)
        if self.get(name):
            self.kb._exec("UPDATE roles SET perms=?, label=COALESCE(NULLIF(?, ''), label), description=COALESCE(NULLIF(?, ''), description) WHERE name=?", (json.dumps(perms), label.strip(), description.strip(), name))
        else:
            self.kb._exec("INSERT INTO roles (name, label, description, perms, builtin) VALUES (?,?,?,?,0)", (name, label.strip() or name, description.strip(), json.dumps(perms)))
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

    def reset(self, name: str) -> None:
        """Put a built-in role back to its shipped permissions."""
        if name in BUILTIN:
            self.kb._exec("UPDATE roles SET perms=? WHERE name=?", (json.dumps(sorted(BUILTIN[name]["perms"])), name))
