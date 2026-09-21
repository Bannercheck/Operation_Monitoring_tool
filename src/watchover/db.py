"""One database for everything Watchover keeps: PostgreSQL in production, SQLite as the embedded fallback for a laptop or the tests.

    db = Database("postgresql://watchover:...@postgres:5432/watchover")   # or Database("knowledge.db")
    db._exec("SELECT ... WHERE id=?", (1,))       # '?' placeholders everywhere; translated for psycopg
    db._insert("INSERT ...", params) -> new id

The stores (Knowledge, Users, Roles, Notifier, ActionStore, Playbook, ...) share one Database. The SQL they write is the common
subset of both dialects; the few differences (serial keys, blobs, upserts) go through the helpers below.
"""
from __future__ import annotations

import base64
import os
import sqlite3
import threading
from decimal import Decimal

DEFAULT_SQLITE = "knowledge.db"


def q(cols) -> list[str]:
    """Quoted identifiers: a column such as "user" is a reserved word in PostgreSQL."""
    return [f'"{c}"' for c in cols]


def default_url() -> str:
    """DATABASE_URL (PostgreSQL) wins; without it the embedded SQLite file from KNOWLEDGE_DB / knowledge.db is used."""
    return os.environ.get("DATABASE_URL") or os.environ.get("KNOWLEDGE_DB", DEFAULT_SQLITE)


def is_postgres(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def describe(url: str) -> str:
    """Connection shown on the System page: never the password."""
    if not is_postgres(url):
        return f"sqlite · {url}"
    rest = url.split("://", 1)[1]
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"postgresql · {rest}"


class Database:
    def __init__(self, url: str | None = None):
        self.url = url or default_url()
        self.pg = is_postgres(self.url)
        self._lock = threading.RLock()                 # one connection per process; background threads (poller, alerts, learner) share it
        self.conn = None
        self._connect()

    # ---------------------------------------------------------------- connection
    def _connect(self) -> None:
        if self.pg:
            import psycopg  # type: ignore
            self.conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10, application_name="watchover")
        else:
            self.conn = sqlite3.connect(self.url, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def backend(self) -> str:
        return "postgresql" if self.pg else "sqlite"

    @property
    def label(self) -> str:
        return describe(self.url)

    @property
    def pk(self) -> str:
        return "SERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"

    @property
    def blob(self) -> str:
        return "BYTEA" if self.pg else "BLOB"

    # ---------------------------------------------------------------- statements
    def _q(self, sql: str, params: tuple) -> str:
        if not self.pg:
            return sql
        if params:
            return sql.replace("%", "%%").replace("?", "%s")
        return sql

    @staticmethod
    def _py(v):
        if isinstance(v, Decimal):
            return int(v) if v == v.to_integral_value() else float(v)
        if isinstance(v, memoryview):
            return bytes(v)
        return v

    def _exec(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            if self.pg:
                return self._exec_pg(sql, params)
            cur = self.conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else []
            self.conn.commit()
            return rows

    def _exec_pg(self, sql: str, params: tuple, retry: bool = True) -> list[dict]:
        import psycopg  # type: ignore
        try:
            cur = self.conn.execute(self._q(sql, params), params or None)
        except (psycopg.OperationalError, psycopg.InterfaceError):
            if not retry or not self.conn.closed and not self.conn.broken:
                raise
            self._connect()                            # the server restarted or the connection idled out: reconnect once and replay
            return self._exec_pg(sql, params, retry=False)
        if cur.description:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, (self._py(v) for v in r))) for r in cur.fetchall()]
        return []

    def _insert(self, sql: str, params: tuple) -> int:
        with self._lock:
            if self.pg:
                return int(self._exec(sql + " RETURNING id", params)[0]["id"])
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return int(cur.lastrowid)

    def upsert_suffix(self, conflict_cols: str, updates: str) -> str:
        """ON CONFLICT clause for INSERT ... (same syntax in both engines, SQLite needs the conflict target too)."""
        return f" ON CONFLICT({conflict_cols}) DO UPDATE SET {updates}"

    # ---------------------------------------------------------------- introspection, dump and restore (snapshots, migration)
    def tables(self) -> list[str]:
        if self.pg:
            rows = self._exec("SELECT tablename AS name FROM pg_tables WHERE schemaname = current_schema() ORDER BY tablename")
        else:
            rows = self._exec("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        return [r["name"] for r in rows]

    def columns(self, table: str) -> list[str]:
        if self.pg:
            return [r["column_name"] for r in self._exec(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name=? ORDER BY ordinal_position", (table,))]
        return [r["name"] for r in self._exec(f"PRAGMA table_info({table})")]

    def count(self, table: str) -> int:
        return int(self._exec(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])

    def size_bytes(self) -> int:
        try:
            if self.pg:
                return int(self._exec("SELECT pg_database_size(current_database()) AS b")[0]["b"])
            return int(self._exec("SELECT page_count * page_size AS b FROM pragma_page_count(), pragma_page_size()")[0]["b"])
        except Exception:  # noqa: BLE001
            return 0

    def dump(self) -> dict:
        """Every table as {columns, rows}; bytes are base64 so the result is plain JSON."""
        out = {}
        for t in self.tables():
            cols = self.columns(t)
            rows = []
            for r in self._exec(f"SELECT {', '.join(q(cols))} FROM {t}"):
                rows.append([{"__b64__": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray)) else v for v in (r[c] for c in cols)])
            out[t] = {"columns": cols, "rows": rows}
        return out

    def copy_table(self, table: str, columns: list[str], rows: list, replace: bool = False) -> int:
        """Insert rows into an existing table; conflicting keys are skipped (or, with replace, the table is emptied first)."""
        have = set(self.columns(table))
        keep = [i for i, c in enumerate(columns) if c in have]
        if not keep:
            return 0
        cols = [columns[i] for i in keep]
        sql = f"INSERT INTO {table} ({', '.join(q(cols))}) VALUES ({', '.join('?' * len(cols))}) ON CONFLICT DO NOTHING"
        if replace:
            self._exec(f"DELETE FROM {table}")
        n = 0
        with self._lock:
            for r in rows:
                vals = tuple(base64.b64decode(v["__b64__"]) if isinstance(v, dict) and "__b64__" in v else v for v in (r[i] for i in keep))
                before = self.count(table) if not self.pg else None
                res = self._exec(sql + (" RETURNING 1" if self.pg else ""), vals)
                n += len(res) if self.pg else (self.count(table) - before)
        self.reset_sequence(table)
        return n

    def restore(self, data: dict) -> dict:
        """Bring a dump back: tables present in the dump are emptied and refilled, others are left alone."""
        done = {}
        for t, d in data.items():
            if t in self.tables():
                done[t] = self.copy_table(t, d["columns"], d["rows"], replace=True)
        return done

    def reset_sequence(self, table: str) -> None:
        if self.pg and "id" in self.columns(table):
            self._exec(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
