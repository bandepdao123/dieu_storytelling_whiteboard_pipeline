"""Managed-copy rollback regressions; disposable public API fixtures only."""
from pathlib import Path
import pytest
from du_pipeline.contracts import QAEvidence
from test_astra_phase2_jobs import pair, approve


def invoke(p, pid, scene, source, operation):
    if operation == 'add':
        return p.add_artifact(pid, 'DOCUMENT', source)
    if operation == 'contact':
        return p.approve_post_batch(pid, QAEvidence({'ok': True}, 1, 'test'), source, 'reviewer')
    return p.replace_scene_artifact(scene['id'], source)


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
def test_successful_nested_copy_outer_rollback_leaves_no_unowned_copy(pair, tmp_path, operation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'
    source.write_bytes(b'caller bytes')
    before = set(root.iterdir())
    dump = list(p.db.conn.iterdump())
    with pytest.raises(RuntimeError, match='outer rollback'):
        with p.db.transaction():
            with pytest.raises(PermissionError, match='top-level'):
                invoke(p, pid, scene, source, operation)
            raise RuntimeError('outer rollback')
    assert list(p.db.conn.iterdump()) == dump
    assert source.read_bytes() == b'caller bytes'
    assert set(root.iterdir()) == before


@pytest.mark.parametrize('operation', ['add', 'contact', 'replace'])
def test_caught_inner_failure_compensates_copy(pair, tmp_path, monkeypatch, operation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'
    source.write_bytes(b'caller bytes')
    before = set(root.iterdir())
    original = p._event
    target = {'add': 'ARTIFACT_ADDED', 'contact': 'POST_BATCH_QA_RECORDED', 'replace': 'SCENE_ARTIFACT_REPLACED'}[operation]
    def fail(pid, typ, data=None):
        if typ == target: raise RuntimeError('inner failure')
        return original(pid, typ, data)
    monkeypatch.setattr(p, '_event', fail)
    with pytest.raises(RuntimeError, match='inner failure'):
        invoke(p, pid, scene, source, operation)
    with p.db.transaction():
        with pytest.raises(PermissionError, match='top-level'):
            invoke(p, pid, scene, source, operation)
    assert set(root.iterdir()) == before
    assert source.read_bytes() == b'caller bytes'


def test_add_failure_preserves_replaced_copy_and_source(pair, tmp_path, monkeypatch):
    p, other, pid, scene, root = pair
    source = tmp_path / 'external.png'
    source.write_bytes(b'caller bytes')
    replaced = []
    original = p._event
    def fail(pid, typ, data=None):
        if typ == 'ARTIFACT_ADDED':
            leaf = Path(p.db.one('select uri from artifacts where id=?', (data['artifact'],))['uri'])
            leaf.rename(tmp_path / 'original-copy')
            leaf.write_bytes(b'unknown replacement')
            source.rename(tmp_path / 'original-source')
            source.write_bytes(b'new caller bytes')
            replaced.append(leaf)
            raise RuntimeError('replace before compensation')
        return original(pid, typ, data)
    monkeypatch.setattr(p, '_event', fail)
    with pytest.raises(RuntimeError, match='replace before compensation'):
        p.add_artifact(pid, 'DOCUMENT', source)
    assert source.read_bytes() == b'new caller bytes'
    assert replaced[0].read_bytes() == b'unknown replacement'


@pytest.mark.parametrize('change', ['pause', 'replan', 'evidence'])
def test_contact_copy_revalidates_expected_evidence_without_writer(pair, tmp_path, monkeypatch, change):
    import shutil
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'contact.png'
    source.write_bytes(b'contact reviewed before change')
    original = shutil.copyfileobj
    fired = []
    before = set(root.iterdir())
    def copy(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        assert not p.db.conn.in_transaction
        if change == 'pause': other.pause(pid)
        elif change == 'replan': other.plan_scenes(pid)
        else:
            other.record_scene_qa(scene['id'], QAEvidence({'changed': True}, 1, 'new reviewer'))
            other.decide_scene(pid, scene['code'], 'APPROVED', 'new reviewer')
        fired.append(list(other.db.conn.iterdump()))
        return result
    monkeypatch.setattr(shutil, 'copyfileobj', copy)
    with pytest.raises(PermissionError):
        invoke(p, pid, scene, source, 'contact')
    assert fired
    assert list(p.db.conn.iterdump()) == fired[0]
    assert set(root.iterdir()) == before
