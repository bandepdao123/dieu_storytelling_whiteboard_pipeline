"""F10 restore: disposable public fixtures and deterministic independent writers."""
import json
from pathlib import Path
import pytest
from test_astra_phase2_jobs import pair, before_transaction, approve


def setup_restore(pair):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    aid = p.db.one("select id from artifacts where kind='IMAGE'")['id']
    p.checkpoint(pid, 'content', {'marker': 1})
    return aid


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
@pytest.mark.parametrize('change', ['pause', 'cancel', 'revision', 'inventory', 'replan', 'checkpoint', 'same-checkpoint', 'evidence', 'deleted'])
def test_restore_rejects_changed_expected_state(pair, monkeypatch, entry, change):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    expected = []
    def race():
        if change == 'pause': other.pause(pid)
        elif change == 'cancel': other.cancel_project(pid)
        elif change == 'revision': other.configure_project(pid, 'language', 'en')
        elif change == 'inventory': other.add_artifact(pid, 'PROMPT', root / 'image.png')
        elif change == 'replan': assert other.plan_scenes(pid)[0]['id'] == scene['id']
        elif change == 'checkpoint': other.checkpoint(pid, 'content', {'marker': 2})
        elif change == 'same-checkpoint': other.checkpoint(pid, 'content', {'marker': 1})
        elif change == 'evidence': other.decide_scene(pid, scene['code'], 'REJECTED', 'reviewer')
        else:
            # Fault fixture: deletion decision without moving bytes; no approvals fabricated.
            other.db.execute("update artifacts set status='DELETED' where id=?", (aid,))
        expected.append(list(other.db.conn.iterdump()))
    before_transaction(monkeypatch, p.db, race)
    with pytest.raises((PermissionError, ValueError, FileNotFoundError)):
        p.restore_artifact(aid) if entry == 'artifact' else p.restore_latest_checkpoint(pid, 'content')
    assert list(p.db.conn.iterdump()) == expected[0]


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
@pytest.mark.parametrize('change', ['missing', 'tamper', 'symlink'])
def test_restore_does_not_revive_bad_bytes(pair, monkeypatch, entry, change):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    image = root / 'image.png'
    def race():
        image.unlink()
        if change == 'tamper': image.write_bytes(b'tampered')
        if change == 'symlink': image.symlink_to(root / 'contact.png')
    before = list(p.db.conn.iterdump())
    before_transaction(monkeypatch, p.db, race)
    with pytest.raises((PermissionError, ValueError, OSError)):
        p.restore_artifact(aid) if entry == 'artifact' else p.restore_latest_checkpoint(pid, 'content')
    assert list(p.db.conn.iterdump()) == before
    if change == 'missing': assert not image.exists()
    elif change == 'tamper': assert image.read_bytes() == b'tampered'
    else: assert image.is_symlink()


def test_checkpoint_cannot_restore_reused_identical_scene_ids(pair):
    p, other, pid, scene, root = pair
    p.checkpoint(pid, 'content', {})
    assert p.plan_scenes(pid)[0]['id'] == scene['id']
    before = list(p.db.conn.iterdump())
    with pytest.raises(ValueError): p.restore_latest_checkpoint(pid, 'content')
    assert list(p.db.conn.iterdump()) == before


@pytest.mark.parametrize('stop', ['pause', 'cancel'])
def test_checkpoint_creation_requires_active_even_existing_job(pair, monkeypatch, stop):
    p, other, pid, scene, root = pair
    before_transaction(monkeypatch, p.db, lambda: other.pause(pid) if stop == 'pause' else other.cancel_project(pid))
    with pytest.raises(PermissionError): p.checkpoint(pid, 'content', {})
    assert not p.db.one("select 1 from stages where name='content'")


@pytest.mark.parametrize('entry', ['artifact', 'checkpoint'])
def test_restore_rejects_already_deleted_binary_even_if_present(pair, entry):
    p, other, pid, scene, root = pair
    aid = setup_restore(pair)
    other.db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    before = list(p.db.conn.iterdump())
    with pytest.raises((ValueError, FileNotFoundError)):
        p.restore_artifact(aid) if entry == 'artifact' else p.restore_latest_checkpoint(pid, 'content')
    assert list(p.db.conn.iterdump()) == before
