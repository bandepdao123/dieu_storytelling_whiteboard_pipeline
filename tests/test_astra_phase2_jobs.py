"""Deterministic two-connection interleavings, public evidence setup only."""
import hashlib
from contextlib import contextmanager
from pathlib import Path

import pytest

from du_pipeline.contracts import QAEvidence
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


@pytest.fixture
def pair(tmp_path):
    with Database(tmp_path / 'jobs.db') as a:
        p = Pipeline(a)
        pid = p.init_project('race', scene_range=(1, 10))
        root = Path(p.status(pid)['project']['artifact_root'])
        audio = root / 'audio.wav'
        audio.write_bytes(b'local test audio')
        p.import_audio(pid, str(audio), 6000, hashlib.sha256(audio.read_bytes()).hexdigest())
        p.import_srt(pid, [(0, 6000, 'scene')])
        scene = p.plan_scenes(pid)[0]
        with Database(tmp_path / 'jobs.db') as b:
            yield p, Pipeline(b), pid, scene, root


def approve(p, pid, scene, root):
    image = root / 'image.png'
    image.write_bytes(b'local image fixture; no decoder required for job admission')
    p.add_artifact(pid, 'IMAGE', image, scene['id'])
    p.record_image_attempt(scene['id'], True)
    qa = QAEvidence({'ok': True}, 1, 'local-test')
    p.record_scene_qa(scene['id'], qa)
    p.decide_scene(pid, scene['code'], 'APPROVED', 'reviewer')
    contact = root / 'contact.png'
    contact.write_bytes(b'contact')
    p.approve_post_batch(pid, qa, contact, 'reviewer')


def before_transaction(monkeypatch, db, action):
    original = db.transaction
    fired = []

    @contextmanager
    def interleave(**kwargs):
        if kwargs.get('write', True) and not fired:
            fired.append(True)
            action()
        with original(**kwargs):
            yield db

    monkeypatch.setattr(db, 'transaction', interleave)
    return fired


def jobs(p, pid):
    return p.status(pid)['jobs']


@pytest.mark.parametrize('entry', ['batch', 'animation', 'stage-image', 'stage-animation', 'primitive'])
@pytest.mark.parametrize('stop', ['pause', 'cancel'])
def test_stop_before_job_transaction_cannot_enqueue(pair, monkeypatch, entry, stop):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    previous = jobs(p, pid)
    fired = before_transaction(monkeypatch, p.db,
                               lambda: other.pause(pid) if stop == 'pause' else other.cancel_project(pid))
    with pytest.raises(PermissionError):
        if entry == 'batch':
            p.start_batch(pid)
        elif entry == 'animation':
            p.start_animation(pid)
        elif entry.startswith('stage-'):
            p.run_stage(pid, entry.removeprefix('stage-'))
        else:
            p._create_job(pid, 'TEST_BOUNDARY')
    assert fired
    assert len(jobs(p, pid)) == len(previous)
    assert not any(j['state'] == 'QUEUED' for j in jobs(p, pid))


@pytest.mark.parametrize('entry', ['batch', 'animation'])
def test_evidence_revoked_before_admission_transaction(pair, monkeypatch, entry):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    previous = jobs(p, pid)
    fired = before_transaction(monkeypatch, p.db,
                               lambda: other.decide_scene(pid, scene['code'], 'REJECTED', 'second-reviewer'))
    with pytest.raises(PermissionError):
        getattr(p, 'start_' + entry)(pid)
    assert fired
    assert len(jobs(p, pid)) == len(previous)
    assert not any(j['state'] == 'QUEUED' for j in jobs(p, pid))


def test_retry_budget_rechecked_under_write_transaction(pair, monkeypatch):
    p, other, pid, scene, root = pair
    p.record_image_attempt(scene['id'], False)
    p.record_image_attempt(scene['id'], False)
    previous = jobs(p, pid)
    fired = before_transaction(monkeypatch, p.db,
                               lambda: other.record_image_attempt(scene['id'], False))
    with pytest.raises(ValueError, match='maximum 3 attempts'):
        p.queue_retry(scene['id'])
    assert fired
    assert p.scene(scene['id'])['state'] == 'BLOCKED'
    assert len(jobs(p, pid)) == len(previous)
    assert p.db.one('SELECT count(*) FROM attempts')[0] == 3


def test_retry_pause_does_not_leave_partial_scene_state(pair, monkeypatch):
    p, other, pid, scene, root = pair
    p.record_image_attempt(scene['id'], False)
    previous = jobs(p, pid)
    before_transaction(monkeypatch, p.db, lambda: other.pause(pid))
    with pytest.raises(PermissionError):
        p.queue_retry(scene['id'])
    assert p.scene(scene['id'])['state'] == 'RETRYABLE'
    assert len(jobs(p, pid)) == len(previous)


def test_current_job_idempotency_and_pause_after_admission(pair):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    jid = p.start_batch(pid)
    assert other.start_batch(pid) == jid
    other.pause(pid)
    assert next(j for j in jobs(p, pid) if j['id'] == jid)['state'] == 'PAUSED'
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    other.resume(pid)
    assert next(j for j in jobs(p, pid) if j['id'] == jid)['state'] == 'QUEUED'
    p.pause_job(jid)
    other.decide_scene(pid, scene['code'], 'REJECTED', 'reviewer')
    with pytest.raises(PermissionError):
        p.resume_job(jid)
    assert next(j for j in jobs(p, pid) if j['id'] == jid)['state'] == 'BLOCKED'


def test_resume_uses_current_revision_after_other_connection_cycle(pair, monkeypatch):
    p, other, pid, scene, root = pair
    p.pause(pid)
    latest = []

    def cycle():
        other.resume(pid)
        other.plan_scenes(pid)
        latest.append(jobs(other, pid)[-1]['id'])
        other.pause(pid)

    fired = before_transaction(monkeypatch, p.db, cycle)
    p.resume(pid)
    assert fired
    current = p.status(pid)['project']
    assert current['state'] == 'ACTIVE'
    job = next(j for j in jobs(p, pid) if j['id'] == latest[0])
    assert job['project_version'] == current['version']
    assert job['state'] == 'QUEUED'


@pytest.mark.parametrize('entry', ['batch', 'animation'])
def test_gate_read_holds_write_lock_against_second_connection(pair, monkeypatch, entry):
    import sqlite3

    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    other.db.conn.execute('PRAGMA busy_timeout=0')
    name = '_pilot_ready' if entry == 'batch' else '_manifest_hash'
    original = getattr(p, name)
    checked = []

    def gated(*args):
        result = original(*args)
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.pause(pid)
        assert other.db._tx_depth == 0
        checked.append(True)
        return result

    monkeypatch.setattr(p, name, gated)
    jid = getattr(p, 'start_' + entry)(pid)
    assert checked
    other.pause(pid)
    assert next(j for j in jobs(p, pid) if j['id'] == jid)['state'] == 'PAUSED'


def test_retry_caught_job_failure_rolls_back_inner_scope(pair, monkeypatch):
    p, other, pid, scene, root = pair
    p.record_image_attempt(scene['id'], False)
    previous = jobs(p, pid)
    original = p._create_job

    def fail(*args):
        original(*args)
        raise RuntimeError('after insert')

    monkeypatch.setattr(p, '_create_job', fail)
    with p.db.transaction():
        with pytest.raises(RuntimeError, match='after insert'):
            p.queue_retry(scene['id'])
        assert p.scene(scene['id'])['state'] == 'RETRYABLE'
        assert jobs(p, pid) == previous
    assert other.scene(scene['id'])['state'] == 'RETRYABLE'
