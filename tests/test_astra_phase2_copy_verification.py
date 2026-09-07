"""Verification of bounded ownership policy, not durable copy recovery."""
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from test_astra_phase2_jobs import pair, approve
from test_astra_phase2_copy_ownership import invoke


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
@pytest.mark.parametrize('managed', [False, True])
@pytest.mark.parametrize('depth', [1, 2])
def test_public_copy_rejects_before_copy_or_mutation(pair, tmp_path, monkeypatch, operation, managed, depth):
    from contextlib import ExitStack
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = root / 'image.png' if managed else tmp_path / 'external.png'
    if not managed: source.write_bytes(b'caller')
    before = list(p.db.conn.iterdump())
    files = {x: x.read_bytes() for x in root.iterdir() if x.is_file()}
    def forbidden(*args, **kwargs): raise AssertionError('copy on reject')
    monkeypatch.setattr(shutil, 'copyfileobj', forbidden)
    with ExitStack() as stack:
        for _ in range(depth): stack.enter_context(p.db.transaction())
        with pytest.raises(PermissionError, match='top-level'):
            invoke(p, pid, scene, source, operation)
        assert list(p.db.conn.iterdump()) == before
    assert {x: x.read_bytes() for x in root.iterdir() if x.is_file()} == files


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
def test_incomplete_copy_failure_retains_unknown_bytes(pair, tmp_path, monkeypatch, operation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'; source.write_bytes(b'caller')
    before = set(root.iterdir()); dump = list(p.db.conn.iterdump())
    def partial(src, dst, *args, **kwargs):
        dst.write(b'partial'); dst.flush()
        raise OSError('copy interrupted')
    monkeypatch.setattr(shutil, 'copyfileobj', partial)
    with pytest.raises(OSError, match='copy interrupted'):
        invoke(p, pid, scene, source, operation)
    unknown = set(root.iterdir()) - before
    assert len(unknown) == 1
    assert next(iter(unknown)).read_bytes() == b'partial'
    assert list(p.db.conn.iterdump()) == dump
    assert source.read_bytes() == b'caller'


def test_copy_free_registration_outer_rollback_preserves_caller(pair):
    p, other, pid, scene, root = pair
    source = root / 'caller'
    source.write_bytes(b'caller owned')
    before = list(p.db.conn.iterdump())
    with pytest.raises(RuntimeError):
        with p.db.transaction():
            p.add_artifact(pid, 'DOCUMENT', source)
            raise RuntimeError('rollback')
    assert source.read_bytes() == b'caller owned'
    assert list(p.db.conn.iterdump()) == before


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
@pytest.mark.parametrize('fault', ['interrupt', 'alias', 'parent', 'reference', 'inplace'])
def test_failure_preserves_unknown_alias_referenced_bytes(pair, tmp_path, monkeypatch, operation, fault):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'; source.write_bytes(b'caller')
    original = p._event
    created = []
    target = {'add': 'ARTIFACT_ADDED', 'contact': 'POST_BATCH_QA_RECORDED', 'replace': 'SCENE_ARTIFACT_REPLACED'}[operation]
    def fail(pid, typ, data=None):
        if typ == target:
            leaf = Path(p.db.one('select uri from artifacts order by id desc')['uri'])
            created.append(leaf)
            if fault == 'inplace': leaf.write_bytes(b'unknown inplace bytes')
            if fault == 'alias': os.link(leaf, tmp_path / 'alias')
            if fault == 'parent':
                root.rename(tmp_path / 'pinned-root')
                root.mkdir(); (root / leaf.name).write_bytes(b'unknown parent bytes')
            raise KeyboardInterrupt('fault')
        return original(pid, typ, data)
    monkeypatch.setattr(p, '_event', fail)
    if fault == 'reference':
        # Commit reference after SQL rollback, before compensation writer acquisition.
        from contextlib import contextmanager
        original_tx = p.db.transaction
        @contextmanager
        def after_rollback(**kwargs):
            try:
                with original_tx(**kwargs): yield p.db
            except KeyboardInterrupt:
                if created:
                    other.add_artifact(pid, 'DOCUMENT', created[0])
                raise
        monkeypatch.setattr(p.db, 'transaction', after_rollback)
    with pytest.raises(KeyboardInterrupt, match='fault'):
        invoke(p, pid, scene, source, operation)
    assert source.read_bytes() == b'caller'
    if fault in ('alias', 'reference'): assert created[0].read_bytes() == b'caller'
    elif fault == 'inplace': assert created[0].read_bytes() == b'unknown inplace bytes'
    elif fault == 'parent':
        assert created[0].read_bytes() == b'unknown parent bytes'
        assert not (tmp_path / 'pinned-root' / created[0].name).exists()
    else: assert not created[0].exists()


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
def test_copy_writer_first_blocks_other_writer(pair, tmp_path, monkeypatch, operation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'; source.write_bytes(b'caller')
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._register_artifact
    checked = []
    def register(*args, **kwargs):
        with pytest.raises(sqlite3.OperationalError, match='locked'): other.pause(pid)
        checked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(p, '_register_artifact', register)
    invoke(p, pid, scene, source, operation)
    assert checked
    other.pause(pid)


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
def test_hard_exit_retains_unknown_copy_on_reopen(pair, tmp_path, operation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'; source.write_bytes(b'caller')
    before = set(root.iterdir())
    dump = list(p.db.conn.iterdump())
    code = '''
import os, sys
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.contracts import QAEvidence
db=Database(sys.argv[1]); p=Pipeline(db)
original=p._event
def crash(pid, typ, data=None):
    if typ=='ARTIFACT_ADDED': os._exit(73)
    return original(pid,typ,data)
p._event=crash
pid, sid, source, operation=sys.argv[2:]
if operation=='add': p.add_artifact(pid,'DOCUMENT',source)
elif operation=='replace': p.replace_scene_artifact(int(sid),source)
else: p.approve_post_batch(pid,QAEvidence({'ok':True},1,'test'),source,'reviewer')
'''
    result = subprocess.run([sys.executable, '-c', code, str(p.db.path), pid, str(scene['id']), str(source), operation])
    assert result.returncode == 73
    unknown = set(root.iterdir()) - before
    assert len(unknown) == 1
    assert next(iter(unknown)).read_bytes() == b'caller'
    assert list(p.db.conn.iterdump()) == dump
    from du_pipeline.db import Database
    with Database(p.db.path) as reopened:
        assert list(reopened.conn.iterdump()) == dump
        assert next(iter(unknown)).read_bytes() == b'caller'
    assert source.read_bytes() == b'caller'
