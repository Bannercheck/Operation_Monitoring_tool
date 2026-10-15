"""Database.tx() (WP0a): one transaction for several statements, on SQLite and (when a test server is set) PostgreSQL."""
import os
import threading
import time

import pytest

from watchover.db import Database

PG_URL = os.environ.get("WATCHOVER_TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request, tmp_path):
    if request.param == "postgresql" and not PG_URL:
        pytest.skip("WATCHOVER_TEST_DATABASE_URL not set")
    d = Database(str(tmp_path / "tx.db")) if request.param == "sqlite" else Database(PG_URL)
    d._exec("DROP TABLE IF EXISTS tx_t")
    d._exec(f"CREATE TABLE tx_t (id {d.pk}, v TEXT)")
    yield d
    d._exec("DROP TABLE IF EXISTS tx_t")
    d.close()


def _vals(d):
    return sorted(r["v"] for r in d._exec("SELECT v FROM tx_t"))


def test_commit(db):
    with db.tx():
        db._insert("INSERT INTO tx_t (v) VALUES (?)", ("a",))
        db._exec("INSERT INTO tx_t (v) VALUES (?)", ("b",))
        db.executemany("INSERT INTO tx_t (v) VALUES (?)", [("c",), ("d",)])
        assert db.in_tx
    assert not db.in_tx
    assert _vals(db) == ["a", "b", "c", "d"]


def test_rollback_on_error(db):
    db._exec("INSERT INTO tx_t (v) VALUES (?)", ("keep",))
    with pytest.raises(ValueError):
        with db.tx():
            db._insert("INSERT INTO tx_t (v) VALUES (?)", ("gone",))
            db._exec("UPDATE tx_t SET v=? WHERE v=?", ("changed", "keep"))
            raise ValueError("boom")
    assert _vals(db) == ["keep"]
    db._exec("INSERT INTO tx_t (v) VALUES (?)", ("after",))   # the connection is usable again and commits on its own
    assert _vals(db) == ["after", "keep"]


def test_nested_refused(db):
    with db.tx():
        with pytest.raises(RuntimeError):
            with db.tx():
                pass
        db._exec("INSERT INTO tx_t (v) VALUES (?)", ("outer",))
    assert _vals(db) == ["outer"]


def test_other_thread_waits_then_commits(db):
    started, done = threading.Event(), threading.Event()
    errors = []

    def other():
        started.set()
        try:
            db._insert("INSERT INTO tx_t (v) VALUES (?)", ("thread",))
            db.executemany("INSERT INTO tx_t (v) VALUES (?)", [("thread2",)])
            db._exec("INSERT INTO tx_t (v) VALUES (?)", ("thread3",))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        done.set()

    with db.tx():
        db._exec("INSERT INTO tx_t (v) VALUES (?)", ("tx",))
        t = threading.Thread(target=other)
        t.start()
        started.wait(2)
        time.sleep(0.15)
        assert not done.is_set()                       # blocked on the process lock while the transaction is open
        assert _vals(db) == ["tx"]
    t.join(5)
    assert not errors and done.is_set()
    assert _vals(db) == ["thread", "thread2", "thread3", "tx"]


def test_other_thread_after_rollback(db):
    done = threading.Event()

    def other():
        db._exec("INSERT INTO tx_t (v) VALUES (?)", ("thread",))
        done.set()

    with pytest.raises(KeyError):
        with db.tx():
            db._exec("INSERT INTO tx_t (v) VALUES (?)", ("rolled",))
            t = threading.Thread(target=other)
            t.start()
            time.sleep(0.1)
            raise KeyError("x")
    t.join(5)
    assert done.is_set()
    assert _vals(db) == ["thread"]                     # the waiting statement committed on its own, not with the rolled-back one


def test_existing_autocommit_unchanged(tmp_path):
    path = str(tmp_path / "plain.db")
    d = Database(path)
    d._exec(f"CREATE TABLE tx_t (id {d.pk}, v TEXT)")
    d._insert("INSERT INTO tx_t (v) VALUES (?)", ("x",))
    other = Database(path)                             # a second connection sees the row at once: every statement commits
    assert [r["v"] for r in other._exec("SELECT v FROM tx_t")] == ["x"]
    other.close()
    d.close()
