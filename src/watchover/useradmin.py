"""Account administration from the terminal (the way out when nobody can sign in):

    watchover user list
    watchover user add EMAIL --admin [--name "Ad Soyad"] [--password '...']     (asks for the password when omitted)
    watchover user promote EMAIL            make an existing account administrator
    watchover user password EMAIL           set a new password (asks when --password is omitted); ends remembered sessions
    watchover user unlock EMAIL             lift the 15-minute lockout after too many failed attempts
    watchover user enable|disable EMAIL
    watchover user delete EMAIL

Runs against the same database as the application (DATABASE_URL, else KNOWLEDGE_DB, else ./knowledge.db in the code folder)."""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from . import auth as wo_auth
from .knowledge import Knowledge


def _users() -> wo_auth.Users:
    return wo_auth.Users(Knowledge(os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", "knowledge.db")))


def _password(given: str | None) -> str:
    if given:
        return given
    p1 = getpass.getpass("new password: "); p2 = getpass.getpass("again: ")
    if p1 != p2:
        raise SystemExit("passwords differ")
    return p1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchover user", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add"); a.add_argument("email"); a.add_argument("--name", default=""); a.add_argument("--admin", action="store_true"); a.add_argument("--password")
    for name in ("promote", "unlock", "enable", "disable", "delete"):
        sub.add_parser(name).add_argument("email")
    p = sub.add_parser("password"); p.add_argument("email"); p.add_argument("--password")
    args = ap.parse_args(argv)
    us = _users()
    if args.cmd == "list":
        rows = us.list()
        if not rows:
            print("no accounts"); return 0
        for r in rows:
            lock = us.locked_until(r["email"])
            print(f"{r['email']:<40} {r['role']:<9} {r['status']:<9} {r['provider']:<9} {r['name'] or '-':<24} last {r['last_login'][:16] or '-'}" + (f"  LOCKED until {lock.strftime('%H:%M')} UTC" if lock else ""))
        return 0
    email = args.email.strip().lower()
    if args.cmd == "add":
        u = us.register(email, _password(args.password), args.name)
        if args.admin and u["role"] != "admin":
            us.set_role(u["id"], "admin"); u = us.get(email)
        print(f"created {email} as {u['role']}"); return 0
    u = us.get(email)
    if not u:
        print(f"no account {email}", file=sys.stderr); return 1
    if args.cmd == "promote":
        us.set_role(u["id"], "admin"); print(f"{email} is now admin")
    elif args.cmd == "password":
        us.change_password(u["id"], _password(args.password)); print(f"password set for {email}")
    elif args.cmd == "unlock":
        us.unlock(email); print(f"{email} unlocked")
    elif args.cmd in ("enable", "disable"):
        us.set_status(u["id"], "active" if args.cmd == "enable" else "disabled"); print(f"{email} {args.cmd}d")
    elif args.cmd == "delete":
        us.delete(u["id"]); print(f"{email} deleted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
