"""Exact independent A/B review interleavings, offline real SQLite."""
import inspect
import sqlite3
import subprocess
from contextlib import contextmanager, ExitStack

import pytest

from du_pipeline.db import Database, SCHEMA_VERSION
from du_pipeline.service import Pipeline
from test_final_assembly import fixture, SuccessfulFakeAssembler


@pytest.mark.parametrize('levels', [1, 2])
@pytest.mark.parametrize('catch_inner', [False, True])
def test_A1_rollback_to_failure_never_commits(tmp_path, levels, catch_inner):
    with Database(tmp_path/'tx.db') as db:
        db.execute('create table probe(value text)')
        denied = []
        def auth(action, a, b, *rest):
            if action == sqlite3.SQLITE_SAVEPOINT and a == 'ROLLBACK' and not denied:
                denied.append(True)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        original = ValueError('business failure')
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            with db.transaction():
                db.execute("insert into probe values('outer')")
                with ExitStack() as stack:
                    for _ in range(levels-1): stack.enter_context(db.transaction())
                    db.conn.set_authorizer(auth)
                    try:
                        with db.transaction():
                            db.execute("insert into probe values('failed-inner')")
                            raise original
                    except (ValueError, sqlite3.DatabaseError) as exc:
                        assert exc is original
                        assert 'rollback-only' in ' '.join(exc.__notes__)
                        if not catch_inner: raise
                        with pytest.raises(sqlite3.DatabaseError, match='rollback-only'):
                            with db.transaction(): pytest.fail('poisoned scope entered')
                        db.execute("insert into probe values('after-caught-failure')")
                    finally:
                        db.conn.set_authorizer(None)
        assert denied
        assert not db.all('select * from probe')
        assert db._tx_depth == 0 and not db.conn.in_transaction
        with db.transaction(): db.execute("insert into probe values('healthy')")
        assert [r[0] for r in db.all('select * from probe')] == ['healthy']


@pytest.mark.parametrize('rollback_fails', [False, True])
def test_A2_cancel_actual_v12_closes_and_preserves_history(tmp_path, monkeypatch, rollback_fails):
    # Actual committed v12 constructor, not relabeled current schema.
    source = subprocess.check_output(['git','show','929511e:src/du_pipeline/db.py']).decode()
    namespace = {'__name__': 'historical_db'}
    exec(compile(source, 'historical_db.py', 'exec'), namespace)
    path = tmp_path/'migration.db'
    old = namespace['Database'](path)
    old.execute("insert into command_receipts values('historical',?, 'unchanged', 'old')", ('a'*64,))
    old.close()
    connect = sqlite3.connect
    with connect(path) as raw: before = list(raw.iterdump())
    captured = []
    cancel = KeyboardInterrupt('cancel migration')
    class Cancel(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            result = super().execute(sql, *a, **kw)
            if sql == f'PRAGMA user_version={SCHEMA_VERSION}':
                captured.append(self)
                raise cancel
            return result
        def rollback(self):
            if rollback_fails: raise sqlite3.OperationalError('rollback storage fault')
            return super().rollback()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=Cancel, **kw))
    try:
        with pytest.raises(KeyboardInterrupt) as caught: Database(path)
        assert caught.value is cancel
        with connect(path, timeout=0) as raw:
            raw.execute('begin immediate')
            assert raw.execute('pragma user_version').fetchone()[0] == 12
            assert list(raw.iterdump()) == before
        with pytest.raises(sqlite3.ProgrammingError, match='closed'): captured[0].execute('select 1')
    finally:
        for conn in captured: conn.close()
        monkeypatch.setattr(sqlite3, 'connect', connect)
    with Database(path) as db:
        assert db.one('pragma user_version')[0] == 15
        assert tuple(db.one('select * from command_receipts')) == ('historical', 'a'*64, 'unchanged', 'old')
        assert not db.all('pragma foreign_key_check')


def test_A1_original_error_and_failed_outer_rollback_discards_connection(tmp_path, monkeypatch):
    connect = sqlite3.connect
    class Broken(sqlite3.Connection):
        fail = False
        def rollback(self):
            if self.fail: raise sqlite3.OperationalError('outer rollback failed')
            return super().rollback()
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, factory=Broken, **kw))
    db = Database(tmp_path/'broken.db')
    db.execute('create table probe(value text)')
    conn = db.conn
    original = ValueError('original')
    with pytest.raises(ValueError) as caught:
        with db.transaction():
            db.execute("insert into probe values('failed')")
            conn.fail = True
            raise original
    assert caught.value is original
    assert 'rollback failed' in ' '.join(original.__notes__)
    assert db.conn is None and db._tx_depth == 0
    with pytest.raises(sqlite3.DatabaseError, match='unavailable'):
        with db.transaction(): pass
    with connect(db.path, timeout=0) as raw:
        raw.execute('begin immediate')
        assert not raw.execute('select * from probe').fetchall()


class Stop(BaseException): pass


def prepared(tmp_path):
    db, p, pid, root, *_ = fixture(tmp_path)
    def stop(point):
        if point == 'after_prepared_commit': raise Stop()
    p._publication_crash = stop
    with pytest.raises(Stop): p.assemble(pid, root/'final.mp4', assembler=SuccessfulFakeAssembler(root))
    p._publication_crash = lambda point: None
    return db, p, pid, root


def final_writer_hook(db, action):
    original = db.transaction
    calls = []
    @contextmanager
    def transaction(**kwargs):
        caller = inspect.currentframe().f_back.f_back
        # Source semantic anchor remains exact even as preceding lines change.
        lines, start = inspect.getsourcelines(Pipeline._reconcile_publication_row)
        index = caller.f_lineno-start
        if caller.f_code.co_name == '_reconcile_publication_row' and "self._publication_crash('before_registration')" in ''.join(lines[index:index+3]):
            calls.append(True)
            action()
        with original(**kwargs): yield db
    db.transaction = transaction
    return original, calls


def test_B1_exact_final_writer_pause_quarantines_synchronously(tmp_path):
    db, p, pid, root = prepared(tmp_path)
    with db, Database(db.path) as other:
        original, calls = final_writer_hook(db, lambda: Pipeline(other).pause(pid))
        p.reconcile_publications()
        assert calls == [True]
        assert not db.all('select * from final_assemblies')
        assert not (root/'final.mp4').exists()
        assert db.one('select state from publication_journal')[0] == 'ABORTED'
        assert list(root.glob('final.mp4.aborted-*'))[0].read_bytes() == b'generated'
        db.transaction = original
        p.reconcile_publications()
        assert not (root/'final.mp4').exists()


@pytest.mark.parametrize('fault', ['lost_lease', 'changed_inode', 'journal_failure', 'collision'])
def test_B1_final_writer_compensation_fences(tmp_path, fault):
    from du_pipeline.db import LeaseUnavailable
    db, p, pid, root = prepared(tmp_path)
    final = root/'final.mp4'
    token = db.one('select token from publication_journal')[0]
    quarantine = root/('final.mp4.aborted-'+token)
    with db, Database(db.path) as other:
        def action():
            Pipeline(other).pause(pid)
            if fault == 'lost_lease': other.execute("update reconciliation_leases set owner_token='other'")
            elif fault == 'changed_inode':
                final.rename(root/'owned-held')
                final.write_bytes(b'unknown replacement')
            elif fault == 'collision': quarantine.write_bytes(b'unknown quarantine')
            else:
                other.execute("create trigger fail_abort before update on publication_journal when NEW.state='ABORTED' begin select raise(abort,'rollback journal failure'); end")
        original, calls = final_writer_hook(db, action)
        if fault == 'lost_lease':
            with pytest.raises(LeaseUnavailable): p.reconcile_publications()
        elif fault == 'journal_failure':
            with pytest.raises(sqlite3.IntegrityError, match='rollback journal failure'): p.reconcile_publications()
        else: p.reconcile_publications()
        assert calls == [True]
        assert not db.all('select * from final_assemblies')
        state = db.one('select state from publication_journal')[0]
        if fault == 'lost_lease':
            assert final.read_bytes() == b'generated' and not quarantine.exists()
            assert state == 'PREPARED'
        elif fault == 'changed_inode':
            assert final.read_bytes() == b'unknown replacement'
            assert (root/'owned-held').read_bytes() == b'generated'
            assert not quarantine.exists() and state == 'MANUAL_REVIEW'
        elif fault == 'collision':
            assert final.read_bytes() == b'generated'
            assert quarantine.read_bytes() == b'unknown quarantine' and state == 'MANUAL_REVIEW'
        else:
            assert not final.exists() and quarantine.read_bytes() == b'generated'
            assert state == 'PREPARED'
            db.transaction = original
            other.execute('drop trigger fail_abort')
            p.reconcile_publications()
            assert db.one('select state from publication_journal')[0] == 'ABORTED'
            assert quarantine.read_bytes() == b'generated'


def test_B1_inverse_registration_writer_blocks_pause(tmp_path):
    db, p, pid, root = prepared(tmp_path)
    with db, Database(db.path) as other:
        other.execute('pragma busy_timeout=0')
        calls = []
        def hook(point):
            if point == 'before_registration':
                with pytest.raises(sqlite3.OperationalError, match='locked'): Pipeline(other).pause(pid)
                calls.append(True)
        p._publication_crash = hook
        p.reconcile_publications()
        assert calls == [True]
        assert db.one('select count(*) from final_assemblies')[0] == 1
        assert db.one('select state from publication_journal')[0] == 'COMMITTED'
        assert (root/'final.mp4').read_bytes() == b'generated'
        Pipeline(other).pause(pid)
        p.reconcile_publications()
        assert (root/'final.mp4').read_bytes() == b'generated'


def test_B1_stale_row_does_not_block_other_prepared_row(tmp_path, monkeypatch):
    import test_final_assembly
    db, p, pid, root = prepared(tmp_path)
    # Reuse only the DB connection; create another genuinely approved fixture
    # project through public APIs, with its own evidence and ownership receipt.
    monkeypatch.setattr(test_final_assembly, 'Database', lambda path: db)
    # Assembly normally reconciles older rows first; suspend that preflight only
    # while staging the second crash fixture, then restore real recovery.
    with monkeypatch.context() as staging:
        staging.setattr(Pipeline, 'reconcile_publications', lambda *a, **kw: [])
        _, _, second_pid, second_root = prepared(tmp_path)
    with db, Database(db.path) as other:
        calls = []
        def action():
            if not calls: Pipeline(other).pause(pid)
            calls.append(True)
        original, boundaries = final_writer_hook(db, action)
        p.reconcile_publications()
        assert len(boundaries) == 2
        assert not (root/'final.mp4').exists()
        assert (second_root/'final.mp4').read_bytes() == b'generated'
        assert db.one('select count(*) from final_assemblies')[0] == 1
        assert db.one('select project_id from final_assemblies')[0] == second_pid
        db.transaction = original
        p.reconcile_publications()
        assert db.one('select count(*) from final_assemblies')[0] == 1
