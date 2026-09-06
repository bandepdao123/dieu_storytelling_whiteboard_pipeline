"""F02/F04 acceptance: public API only, real nonempty image/audio bytes."""
import hashlib
import json
import wave
from pathlib import Path

import pytest
from du_pipeline.contracts import QAEvidence
from du_pipeline.db import Database
from du_pipeline.media import AssemblyError
from du_pipeline.service import Pipeline

QA = QAEvidence({'visual': True}, 1, 'local-review')


def image(path, color=100):
    # Valid binary PPM, decoded by FFmpeg without optional Python libraries.
    path.write_bytes(b'P6\n16 16\n255\n' + bytes([color, 20, 30]) * 256)
    return path


@pytest.fixture
def workflow(tmp_path):
    db = Database(tmp_path / 'revision.db')
    p = Pipeline(db)
    pid = p.init_project('two scenes', scene_range=(2, 2))
    root = Path(p.status(pid)['project']['artifact_root'])
    audio = root / 'narration.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\x00\x00' * 16000)
    p.import_audio(pid, str(audio), 2000, hashlib.sha256(audio.read_bytes()).hexdigest())
    p.import_srt(pid, [(0, 1000, 'first'), (1000, 2000, 'second')])
    scenes = p.plan_scenes(pid)
    ids = []
    for s in scenes:
        ids.append(p.add_artifact(pid, 'IMAGE', image(root / (s['code'] + '.ppm')), s['id']))
        p.record_image_attempt(s['id'], True)
        p.record_scene_qa(s['id'], QA)
    yield p, pid, root, scenes, ids
    db.close()


def approve(p, pid, scenes):
    for s in scenes:
        p.decide_scene(pid, s['code'], 'APPROVED', 'human')


def batch(p, pid, root):
    p.approve_post_batch(pid, QA, image(root / 'contact.ppm'), 'batch-human')


def test_sequential_scene_approvals_allow_assembly(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    batch(p, pid, root)
    result = p.assemble(pid, root / 'final.mp4', dry_run=True)
    assert [s['source_artifact_id'] for s in result['manifest']['scenes']] == ids
    assert len([a for a in result['manifest']['approvals'] if a['gate'] == 'SCENE']) == 2


def test_sequential_scene_approvals_agree_with_summary(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    assert p.start_batch(pid)
    assert p.status_summary(pid)['gates']['pilot']['current']


def test_replacement_clears_qa_and_rejects_approval_on_old_qa(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    sid = scenes[0]['id']
    p.replace_scene_artifact(sid, image(root / 'replacement.ppm', 200))
    assert p.scene(sid)['qa_state'] == 'PENDING'
    assert json.loads(p.scene(sid)['qa_json']) == {}
    with pytest.raises(PermissionError, match='QA'):
        p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')
    assert not p.status_summary(pid)['gates']['pilot']['current']


def test_qa_records_exact_image_subject_and_dependency_revision(workflow):
    p, pid, root, scenes, ids = workflow
    payload = json.loads(p.scene(scenes[0]['id'])['qa_json'])
    assert payload['subject']['artifact_id'] == ids[0]
    assert payload['subject']['sha256'] == hashlib.sha256((root / 'S001.ppm').read_bytes()).hexdigest()
    assert len(payload['dependency_revision']) == 64
    assert payload['schema_version'] == 2


def test_reqa_one_scene_preserves_other_scene_approval(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    other = p.scene(scenes[1]['id'])['qa_json']
    p.replace_scene_artifact(scenes[0]['id'], image(root / 'new.ppm', 210))
    p.record_scene_qa(scenes[0]['id'], QA)
    p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')
    assert p.scene(scenes[1]['id'])['qa_json'] == other
    assert p.status_summary(pid)['gates']['pilot']['current']
    batch(p, pid, root)
    assert p.assemble(pid, root / 'final.mp4', dry_run=True)['dry_run']


@pytest.mark.parametrize('change', ['config', 'preset', 'registration'])
def test_dependency_changes_reset_qa(workflow, change):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    if change == 'config':
        p.configure_project(pid, 'whiteboard_mode', 'hand')
    elif change == 'preset':
        p.select_preset(pid, 'youtube')
    else:
        p.add_artifact(pid, 'IMAGE', image(root / 'another.ppm', 220), scenes[0]['id'])
    assert p.scene(scenes[0]['id'])['qa_state'] == 'PENDING'
    with pytest.raises(PermissionError, match='QA'):
        p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')


@pytest.mark.parametrize('change', ['qa', 'image', 'planning'])
def test_upstream_rerun_resets_subject_qa(workflow, change):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    proposal = p.propose_rerun(pid, change)
    p.decide_rerun(proposal, True, 'owner')
    p.apply_rerun(proposal)
    assert p.scene(scenes[0]['id'])['qa_state'] == 'PENDING'
    assert not p.status_summary(pid)['gates']['pilot']['current']


def test_first_document_import_invalidates_existing_scene_evidence(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    p.import_document(pid, 'text', 'new canonical source')
    assert not p.status_summary(pid)['gates']['pilot']['current']
    with pytest.raises(PermissionError):
        p.start_batch(pid)


def test_qa_change_supersedes_derived_visuals(workflow):
    p, pid, root, scenes, ids = workflow
    clip = root / 'derived.mp4'
    clip.write_bytes(b'derived')
    aid = p.add_artifact(pid, 'SCENE_VIDEO', clip, scenes[0]['id'], ids[0])
    p.record_scene_qa(scenes[0]['id'], QA)
    assert p.db.one('select status from artifacts where id=?', (aid,))['status'] == 'SUPERSEDED'


def test_replacement_of_animated_scene_allows_fresh_image_qa(workflow):
    p, pid, root, scenes, ids = workflow
    # Fault/state fixture: no public animation completion API exists yet.
    p.db.execute("update scenes set state='ANIMATED' where id=?", (scenes[0]['id'],))
    p.replace_scene_artifact(scenes[0]['id'], image(root / 'fresh.ppm', 180))
    assert p.scene(scenes[0]['id'])['state'] == 'IMAGE_READY'
    p.record_scene_qa(scenes[0]['id'], QA)


@pytest.mark.parametrize('corruption', ['legacy', 'subject_id', 'subject_hash', 'dependency', 'checks', 'config', 'continuity', 'approval_version', 'approval_hash'])
def test_corrupt_or_legacy_evidence_fails_all_scene_gates(workflow, corruption):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    batch(p, pid, root)
    sid = scenes[0]['id']
    qa = json.loads(p.scene(sid)['qa_json'])
    # Deliberate corruption only; all acceptance approvals were created via API.
    if corruption == 'legacy':
        qa = {'checks': {'visual': True}, 'score': 1, 'evaluator': 'legacy'}
    elif corruption == 'subject_id': qa['subject']['artifact_id'] = ids[1]
    elif corruption == 'subject_hash': qa['subject']['sha256'] = '0' * 64
    elif corruption == 'dependency': qa['dependency_revision'] = '0' * 64
    elif corruption == 'checks': qa['checks'] = {'visual': False}
    elif corruption == 'config': p.db.execute("update projects set seed=seed+1 where id=?", (pid,))
    elif corruption == 'continuity': p.db.execute("update scenes set continuity_json='{}changed' where id=?", (sid,))
    elif corruption == 'approval_version': p.db.execute("update approvals set project_version=999999 where scene_id=?", (sid,))
    elif corruption == 'approval_hash': p.db.execute("update approvals set evidence_sha256=? where scene_id=?", ('0' * 64, sid))
    if corruption in ('legacy', 'subject_id', 'subject_hash', 'dependency', 'checks'):
        p.db.execute('update scenes set qa_json=? where id=?', (json.dumps(qa), sid))
    assert not p.status_summary(pid)['gates']['pilot']['current']
    with pytest.raises(PermissionError): p.start_batch(pid)
    with pytest.raises(AssemblyError): p.assemble(pid, root / 'bad.mp4', dry_run=True)


def test_rejection_revokes_previous_decisions_and_global_jobs(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    job = p.start_batch(pid)
    p.decide_scene(pid, scenes[0]['code'], 'REJECTED', 'human')
    assert not p.status_summary(pid)['gates']['pilot']['current']
    assert p.db.one('select state from jobs where id=?', (job,))['state'] == 'BLOCKED'
    assert not p.db.one("select 1 from approvals where scene_id=? and decision='APPROVED' and revoked_at is null", (scenes[0]['id'],))
    p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')
    assert p.status_summary(pid)['gates']['pilot']['current']


def test_evidence_survives_reopen_without_schema_rewrite(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    original = p.scene(scenes[0]['id'])['qa_json']
    with Database(p.db.path) as reopened:
        other = Pipeline(reopened)
        assert other.scene(scenes[0]['id'])['qa_json'] == original
        assert other.status_summary(pid)['gates']['pilot']['current']


def test_tampered_subject_cannot_get_new_human_approval(workflow):
    p, pid, root, scenes, ids = workflow
    (root / 'S001.ppm').write_bytes(b'tampered')
    with pytest.raises(PermissionError, match='checksum'):
        p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')


def test_new_qa_cannot_be_attached_to_missing_or_ambiguous_image(workflow):
    p, pid, root, scenes, ids = workflow
    p.add_artifact(pid, 'IMAGE', image(root / 'duplicate.ppm'), scenes[0]['id'])
    with pytest.raises(PermissionError, match='exactly one'):
        p.record_scene_qa(scenes[0]['id'], QA)


def test_same_bytes_new_artifact_still_requires_new_qa(workflow):
    p, pid, root, scenes, ids = workflow
    new_id = p.replace_scene_artifact(scenes[0]['id'], root / 'S001.ppm')
    assert new_id != ids[0]
    assert p.scene(scenes[0]['id'])['qa_state'] == 'PENDING'
    with pytest.raises(PermissionError):
        p.decide_scene(pid, scenes[0]['code'], 'APPROVED', 'human')


def test_replacement_failure_rolls_back_qa_and_approvals(workflow, monkeypatch):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    before = p.status(pid)
    real = p._event
    def fail(pid, kind, *args):
        if kind == 'SCENE_ARTIFACT_REPLACED': raise RuntimeError('injected')
        return real(pid, kind, *args)
    monkeypatch.setattr(p, '_event', fail)
    with pytest.raises(RuntimeError, match='injected'):
        p.replace_scene_artifact(scenes[0]['id'], image(root / 'failed.ppm', 230))
    assert p.status(pid) == before
    assert p.status_summary(pid)['gates']['pilot']['current']


def test_noop_config_preserves_qa_and_scene_approval(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    before = p.status(pid)
    p.configure_project(pid, 'whiteboard_mode', 'ask')
    assert p.status(pid) == before
    assert p.status_summary(pid)['gates']['pilot']['current']


def test_real_public_multiscene_assembly(workflow):
    p, pid, root, scenes, ids = workflow
    approve(p, pid, scenes)
    batch(p, pid, root)
    result = p.assemble(pid, root / 'final.mp4')
    assert Path(result['output']).stat().st_size > 0
    assert [s['source_artifact_id'] for s in result['manifest']['scenes']] == ids
    assert result['manifest']['ffprobe']['streams']
