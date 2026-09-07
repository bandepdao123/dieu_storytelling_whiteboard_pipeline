"""Public resume admission regressions; no SQL approval fabrication."""
import json
import subprocess
import sys

import pytest
from test_astra_phase2_jobs import pair, approve, before_transaction
from du_pipeline.contracts import QAEvidence


def dump(p):
    return list(p.db.conn.iterdump())


@pytest.mark.parametrize('bulk', [False, True])
def test_stale_animation_rejected_atomically(pair, bulk):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = p.start_animation(pid)
    p.add_artifact(pid, 'SCENE_VIDEO', root / 'image.png', scene['id'])
    with pytest.raises(PermissionError):
        p.start_animation(pid)
    p.pause(pid) if bulk else p.pause_job(jid)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume(pid) if bulk else p.resume_job(jid)
    assert dump(p) == before


@pytest.mark.parametrize('kind', ['batch', 'animation'])
@pytest.mark.parametrize('bulk', [False, True])
def test_current_evidence_resume_and_contact_rereview(pair, kind, bulk):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = getattr(p, 'start_' + kind)(pid)
    p.approve_post_batch(pid, QAEvidence({'ok': True}, 1, 'external'), root / 'contact.png', 'human')
    p.pause(pid) if bulk else p.pause_job(jid)
    p.resume(pid) if bulk else p.resume_job(jid)
    assert p.db.one('select state from jobs where id=?', (jid,))[0] == 'QUEUED'


@pytest.mark.parametrize('mutation', ['image', 'qa', 'replan'])
@pytest.mark.parametrize('kind', ['batch', 'animation'])
def test_invalidated_jobs_cannot_resume(pair, mutation, kind):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = getattr(p, 'start_' + kind)(pid)
    p.pause_job(jid)
    if mutation == 'image':
        p.add_artifact(pid, 'IMAGE', root / 'image.png', scene['id'])
    elif mutation == 'qa':
        p.record_scene_qa(scene['id'], QAEvidence({'ok': True}, 1, 'changed'))
    else:
        p.plan_scenes(pid)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    assert dump(p) == before


def test_resume_evidence_read_holds_writer(pair, monkeypatch):
    import sqlite3
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = p.start_animation(pid)
    p.pause_job(jid)
    other.db.conn.execute('pragma busy_timeout=0')
    original = p._manifest_hash
    checked = []
    def gate(*args):
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.pause(pid)
        checked.append(True)
        return original(*args)
    monkeypatch.setattr(p, '_manifest_hash', gate)
    p.resume_job(jid)
    assert checked


@pytest.mark.parametrize('owner,deadline', [('worker', '2999-01-01'), ('worker', '2000-01-01'), (None, '2999-01-01')])
def test_lease_not_silently_stolen(pair, owner, deadline):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = p.start_animation(pid)
    # No worker API exists: synthetic lease state only, never fabricated evidence.
    p.db.execute('update jobs set lease_owner=?,lease_expires_at=? where id=?', (owner, deadline, jid))
    p.pause_job(jid)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    assert dump(p) == before


@pytest.mark.parametrize('stage,allowed', [('planning', True), ('image', True), ('qa', True), ('animation', False), ('assembly', False), ('upload', False)])
def test_owner_rerun_stage_prerequisites_and_history(pair, stage, allowed):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    proposal = p.propose_rerun(pid, stage)
    p.decide_rerun(proposal, True, 'owner')
    jid = p.apply_rerun(proposal)
    history = [dict(r) for r in p.db.all('select * from attempts')]
    decisions = [dict(r) for r in p.db.all("select * from events where type in ('RERUN_DECIDED','RERUN_APPLIED','IMAGE_EPOCH_STARTED')")]
    p.pause_job(jid)
    before = dump(p)
    if allowed:
        p.resume_job(jid)
    else:
        with pytest.raises(PermissionError):
            p.resume_job(jid)
        assert dump(p) == before
    assert [dict(r) for r in p.db.all('select * from attempts')] == history
    assert [dict(r) for r in p.db.all("select * from events where type in ('RERUN_DECIDED','RERUN_APPLIED','IMAGE_EPOCH_STARTED')")] == decisions


def test_unknown_job_kind_fails_closed(pair):
    p, other, pid, scene, root = pair
    jid = p._create_job(pid, 'X')  # fault fixture; not a supported admission API
    p.pause(pid)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume(pid)
    assert dump(p) == before


def test_retry_resume_budget_and_state(pair):
    p, other, pid, scene, root = pair
    p.record_image_attempt(scene['id'], False)
    p.queue_retry(scene['id'])
    jid = p.status(pid)['jobs'][-1]['id']
    p.pause_job(jid)
    p.resume_job(jid)
    p.pause_job(jid)
    p.record_image_attempt(scene['id'], True)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    assert dump(p) == before


def test_stale_project_resume_preserves_valid_other_project(pair):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    p.start_animation(pid)
    other_pid = p.init_project('unrelated')
    p.checkpoint(other_pid, 'input', {})
    p.pause(other_pid)
    unrelated = p.status(other_pid)
    p.add_artifact(pid, 'SCENE_VIDEO', root / 'image.png', scene['id'])
    p.pause(pid)
    before = dump(p)
    with pytest.raises(PermissionError):
        p.resume(pid)
    assert dump(p) == before
    assert p.status(other_pid) == unrelated
    p.resume(other_pid)
    assert p.project(other_pid)['state'] == 'ACTIVE'


def test_pause_committed_before_resume_writer_rejects(pair, monkeypatch):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = p.start_animation(pid)
    p.pause_job(jid)
    snapshots = []
    def pause():
        other.pause(pid)
        snapshots.append(dump(other))
    before_transaction(monkeypatch, p.db, pause)
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    assert dump(p) == snapshots[0]


def test_cli_bulk_cannot_bypass_stale_gate(pair):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    p.start_animation(pid)
    p.add_artifact(pid, 'SCENE_VIDEO', root / 'image.png', scene['id'])
    p.pause(pid)
    before = dump(p)
    path = p.db.one('pragma database_list')['file']
    result = subprocess.run([sys.executable, '-m', 'du_pipeline.cli', '--db', path, 'resume', pid], capture_output=True, text=True)
    assert result.returncode == 2, result.stdout + result.stderr
    assert json.loads(result.stderr)['error']
    assert dump(p) == before
