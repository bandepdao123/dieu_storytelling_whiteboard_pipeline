"""Inverse order, ownership and rollback verification for bounded restore."""
import hashlib
import json
import sqlite3
from pathlib import Path
import pytest
import du_pipeline.service as service
from test_astra_phase2_jobs import pair, before_transaction
from test_astra_phase2_restore import setup_restore


def invoke(p, pid, aid, entry):
    return p.restore_artifact(aid) if entry == 'artifact' else p.restore_latest_checkpoint(pid, 'content')


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_restore_event_failure_rolls_back_without_byte_effects(pair, monkeypatch, entry, nested, error):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    original = p._event
    def fail(pid, typ, data=None):
        if typ in ('ARTIFACT_RESTORED', 'CHECKPOINT_RESTORED'): raise error('rollback')
        return original(pid, typ, data)
    monkeypatch.setattr(p, '_event', fail)
    before = list(p.db.conn.iterdump())
    files = {x: x.read_bytes() for x in root.iterdir() if x.is_file()}
    if nested:
        with p.db.transaction():
            with pytest.raises(error): invoke(p, pid, aid, entry)
            assert list(p.db.conn.iterdump()) == before
    else:
        with pytest.raises(error): invoke(p, pid, aid, entry)
    assert list(p.db.conn.iterdump()) == before
    assert {x: x.read_bytes() for x in files} == files
    assert p.db._tx_depth == 0


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_successful_nested_restore_outer_rollback_is_db_and_byte_atomic(pair, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    before = list(p.db.conn.iterdump())
    with pytest.raises(RuntimeError):
        with p.db.transaction():
            invoke(p, pid, aid, entry)
            raise RuntimeError('outer')
    assert list(p.db.conn.iterdump()) == before
    assert (root / 'image.png').is_file()


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_restore_writer_blocks_stop_then_stop_preserves_content(pair, monkeypatch, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._restore_recheck
    called = []
    def recheck(*args):
        original(*args)
        with pytest.raises(sqlite3.OperationalError, match='locked'): other.pause(pid)
        called.append(True)
    monkeypatch.setattr(p, '_restore_recheck', recheck)
    invoke(p, pid, aid, entry)
    assert called
    other.pause(pid)
    assert p.status(pid)['project']['state'] == 'PAUSED'


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
@pytest.mark.parametrize('stop', ['pause', 'cancel'])
def test_preexisting_stopped_project_allows_content_only_restore(pair, entry, stop):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    other.pause(pid) if stop == 'pause' else other.cancel_project(pid)
    before = p.status(pid)['project']
    invoke(p, pid, aid, entry)
    after = p.status(pid)['project']
    assert (after['state'], after['blocked_reason']) == (before['state'], before['blocked_reason'])
    assert after['version'] == before['version'] + 1


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_hash_does_not_hold_writer_and_rejects_committed_cleanup(pair, monkeypatch, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    # Deterministic TTL fixture only; cleanup uses its real public lease/dirfd protocol.
    p.db.execute("update artifacts set expires_at='2000-01-01' where id=?", (aid,))
    p.checkpoint(pid, 'content', {})
    original = service.snapshot_at
    called = []
    def snapshot(fd, name):
        result = original(fd, name)
        if not called:
            called.append(True)
            assert not p.db.conn.in_transaction
            assert other.cleanup() == 1
        return result
    monkeypatch.setattr(service, 'snapshot_at', snapshot)
    with pytest.raises((PermissionError, ValueError, OSError)): invoke(p, pid, aid, entry)
    assert called
    assert p.db.one('select status from artifacts where id=?', (aid,))['status'] == 'DELETED'
    assert not (root / 'image.png').exists()


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_restore_rejects_parent_substitution_without_touching_outside(pair, monkeypatch, tmp_path, entry):
    p, other, pid, scene, root = pair
    nested = root / 'nested'; nested.mkdir()
    image = nested / 'owned'; image.write_bytes(b'owned')
    aid = p.add_artifact(pid, 'PROMPT', image)
    p.checkpoint(pid, 'content', {})
    outside = tmp_path / 'outside'; outside.mkdir()
    (outside / 'owned').write_bytes(b'outside')
    before = list(p.db.conn.iterdump())
    def race():
        nested.rename(root / 'saved')
        nested.symlink_to(outside, target_is_directory=True)
    before_transaction(monkeypatch, p.db, race)
    with pytest.raises((PermissionError, ValueError, OSError)): invoke(p, pid, aid, entry)
    assert list(p.db.conn.iterdump()) == before
    assert (outside / 'owned').read_bytes() == b'outside'
    assert (root / 'saved' / 'owned').read_bytes() == b'owned'


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_restore_inverse_order_cleanup_cannot_delete_during_mutation(pair, monkeypatch, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    p.db.execute("update artifacts set expires_at='2000-01-01' where id=?", (aid,))
    p.checkpoint(pid, 'content', {})
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._restore_recheck
    def recheck(*args):
        original(*args)
        with pytest.raises(sqlite3.OperationalError, match='locked'): other.cleanup()
    monkeypatch.setattr(p, '_restore_recheck', recheck)
    invoke(p, pid, aid, entry)
    assert (root / 'image.png').is_file()
    assert other.cleanup() == 1
    assert not (root / 'image.png').exists()


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_restore_initial_tampering_rejected_without_sql_changes(pair, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    (root / 'image.png').write_bytes(b'bad before entry')
    before = list(p.db.conn.iterdump())
    with pytest.raises(ValueError, match='checksum'): invoke(p, pid, aid, entry)
    assert list(p.db.conn.iterdump()) == before


def test_checkpoint_snapshot_replacement_has_new_identity_and_keeps_upsert_contract(pair):
    p, other, pid, scene, root = pair
    p.checkpoint(pid, 'content', {'n': 1})
    old = dict(p.db.one("select * from stages where name='content'"))
    p.checkpoint(pid, 'content', {'n': 2})
    new = dict(p.db.one("select * from stages where name='content'"))
    assert old['id'] == new['id']
    assert json.loads(old['checkpoint_json'])['identity'] != json.loads(new['checkpoint_json'])['identity']
    assert json.loads(p.restore_latest_checkpoint(pid, 'content')['checkpoint_json'])['n'] == 2


def test_legacy_scene_snapshot_retained_unmodified_and_fail_closed(pair):
    p, other, pid, scene, root = pair
    p.checkpoint(pid, 'content', {})
    row = p.db.one("select * from stages where name='content'")
    snap = json.loads(row['checkpoint_json'])
    del snap['identity']; del snap['plan_identity']; del snap['snapshot_sha256']
    snap['snapshot_version'] = 3
    snap['snapshot_sha256'] = hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Historical-format fault fixture; never fabrication of an approval.
    p.db.execute('update stages set checkpoint_json=? where id=?', (json.dumps(snap), row['id']))
    before = list(p.db.conn.iterdump())
    with pytest.raises(ValueError, match='legacy'): p.restore_latest_checkpoint(pid, 'content')
    assert list(p.db.conn.iterdump()) == before


def test_checkpoint_after_replan_restores_detached_provenance_without_rebinding(pair):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    p.plan_scenes(pid)
    before = dict(p.db.one('select * from artifacts where id=?', (aid,)))
    assert before['scene_id'] is None and before['detached_scene_id'] == scene['id']
    p.checkpoint(pid, 'new-generation', {})
    p.restore_latest_checkpoint(pid, 'new-generation')
    assert dict(p.db.one('select * from artifacts where id=?', (aid,))) == before


def test_checkpoint_write_lock_and_eventless_insert_rollback(pair, monkeypatch):
    p, other, pid, scene, root = pair
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._checkpoint_generation
    def generation(pid):
        with pytest.raises(sqlite3.OperationalError, match='locked'): other.pause(pid)
        return original(pid)
    monkeypatch.setattr(p, '_checkpoint_generation', generation)
    before = list(p.db.conn.iterdump())
    with pytest.raises(RuntimeError):
        with p.db.transaction():
            p.checkpoint(pid, 'content', {})
            raise RuntimeError('outer')
    assert list(p.db.conn.iterdump()) == before
