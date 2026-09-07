"""Real SQLite transaction failures; no production data or fabricated approvals."""
import sqlite3

import pytest

from du_pipeline.db import Database


@pytest.fixture
def db(tmp_path):
    with Database(tmp_path / 'transactions.db') as db:
        db.execute('CREATE TABLE tx_probe(value TEXT)')
        yield db


def values(db):
    return [r[0] for r in db.all('SELECT value FROM tx_probe ORDER BY rowid')]


def test_caught_nested_exception_rolls_back_only_inner(db):
    with db.transaction():
        db.execute("INSERT INTO tx_probe VALUES('outer')")
        with pytest.raises(ValueError):
            with db.transaction():
                db.execute("INSERT INTO tx_probe VALUES('inner')")
                raise ValueError('recoverable')
        assert values(db) == ['outer']
        with db.transaction():
            db.execute("INSERT INTO tx_probe VALUES('sibling')")
    assert values(db) == ['outer', 'sibling']
    assert db._tx_depth == 0


def test_outer_failure_rolls_back_successful_inner(db):
    with pytest.raises(ValueError):
        with db.transaction():
            with db.transaction():
                db.execute("INSERT INTO tx_probe VALUES('inner')")
            raise ValueError('outer')
    assert values(db) == []
    assert db._tx_depth == 0


def test_three_levels_caught_failure_preserves_parent(db):
    with db.transaction():
        with db.transaction():
            db.execute("INSERT INTO tx_probe VALUES('middle')")
            with pytest.raises(ValueError):
                with db.transaction():
                    db.execute("INSERT INTO tx_probe VALUES('leaf')")
                    raise ValueError('leaf')
            assert db._tx_depth == 2
        assert values(db) == ['middle']
    assert values(db) == ['middle']


@pytest.mark.parametrize('nested', [False, True])
def test_baseexception_recovers_depth_and_rolls_back(db, nested):
    class Stop(BaseException):
        pass

    def fail():
        with db.transaction():
            db.execute("INSERT INTO tx_probe VALUES('aborted')")
            raise Stop()

    if nested:
        with db.transaction():
            with pytest.raises(Stop):
                fail()
            assert db._tx_depth == 1
            assert values(db) == []
    else:
        with pytest.raises(Stop):
            fail()
    assert db._tx_depth == 0
    assert not db.conn.in_transaction
    assert values(db) == []


def test_real_commit_failure_recovers_depth_and_connection(db):
    db.execute('CREATE TABLE tx_parent(id INTEGER PRIMARY KEY)')
    db.execute('CREATE TABLE tx_child(pid INTEGER REFERENCES tx_parent(id) DEFERRABLE INITIALLY DEFERRED)')
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        with db.transaction():
            db.execute('INSERT INTO tx_child VALUES(42)')
    assert db._tx_depth == 0
    assert not db.conn.in_transaction
    assert db.all('SELECT * FROM tx_child') == []
    with db.transaction():
        db.execute('INSERT INTO tx_parent VALUES(42)')
        db.execute('INSERT INTO tx_child VALUES(42)')
    assert db.one('SELECT count(*) FROM tx_child')[0] == 1


@pytest.mark.parametrize('operation', ['BEGIN', 'RELEASE'])
def test_savepoint_control_failure_restores_depth(db, operation):
    denied = []

    def authorizer(action, arg1, arg2, database, source):
        if action == sqlite3.SQLITE_SAVEPOINT and arg1 == operation and not denied:
            denied.append(True)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with db.transaction():
        db.execute("INSERT INTO tx_probe VALUES('outer')")
        db.conn.set_authorizer(authorizer)
        try:
            with pytest.raises(sqlite3.DatabaseError, match='authorized'):
                with db.transaction():
                    db.execute("INSERT INTO tx_probe VALUES('inner')")
        finally:
            db.conn.set_authorizer(None)
        assert denied
        assert db._tx_depth == 1
        assert values(db) == ['outer']
        with db.transaction():
            db.execute("INSERT INTO tx_probe VALUES('recovered')")
    assert db._tx_depth == 0
    assert values(db) == ['outer', 'recovered']


def test_failed_begin_does_not_change_depth(db):
    db.conn.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.OperationalError):
            with db.transaction():
                pytest.fail('must not enter')
        assert db._tx_depth == 0
    finally:
        db.conn.rollback()
