"""Audit regressions with actual nonempty artifacts and public approval fixtures."""
import hashlib

from du_pipeline.contracts import QAEvidence
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def approved_scene(tmp_path):
    db = Database(tmp_path / 'evidence.db')
    pipeline = Pipeline(db)
    pid = pipeline.init_project('canonical evidence', scene_range=(1, 1))
    root = tmp_path / 'artifacts' / pid
    audio = root / 'audio.wav'
    audio.write_bytes(b'nonempty audio evidence; not a decode fixture')
    pipeline.import_audio(pid, str(audio), 6000, hashlib.sha256(audio.read_bytes()).hexdigest())
    pipeline.import_srt(pid, [(0, 6000, 'one scene')])
    scene = pipeline.plan_scenes(pid)[0]
    image = root / 'image.png'
    image.write_bytes(b'nonempty image evidence; not a decode fixture')
    aid = pipeline.add_artifact(pid, 'IMAGE', image, scene['id'])
    pipeline.record_image_attempt(scene['id'], True)
    pipeline.record_scene_qa(scene['id'], QAEvidence({'visual': True}, 1, 'local-review'))
    pipeline.decide_scene(pid, scene['code'], 'APPROVED', 'human')
    return db, pipeline, pid, root, scene, aid


def test_nonempty_scene_summary_matches_actual_gate(tmp_path):
    db, pipeline, pid, root, scene, aid = approved_scene(tmp_path)
    try:
        assert pipeline._pilot_ready(pid) is True
        assert pipeline.status_summary(pid)['gates']['pilot']['current'] is True
    finally:
        db.close()


def test_summary_parity_with_superseded_parent_and_null_scene_artifact(tmp_path):
    db, pipeline, pid, root, scene, original = approved_scene(tmp_path)
    try:
        replacement = root / 'replacement.png'
        replacement.write_bytes(b'different nonempty image')
        child = pipeline.replace_scene_artifact(scene['id'], replacement)
        # Always perform fresh QA: inherited QA is a separate F04 regression.
        qa = QAEvidence({'visual': True}, 1, 'replacement-review')
        pipeline.record_scene_qa(scene['id'], qa)
        pipeline.decide_scene(pid, scene['code'], 'APPROVED', 'human')
        contact = root / 'contact.png'
        contact.write_bytes(b'nonempty contact sheet')
        sheet = pipeline.approve_post_batch(pid, qa, contact, 'batch-human')
        assert child != original and sheet.id != child
        assert pipeline._pilot_ready(pid) is True
        # Actual manifest gate and compact projection must agree too.
        assert pipeline.start_animation(pid)
        queries = []
        db.conn.set_trace_callback(lambda sql: queries.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
        before = db.conn.total_changes
        gates = pipeline.status_summary(pid)['gates']
        assert gates['post_batch']['current'] is True
        assert gates['pilot']['current'] is True
        assert len(queries) <= pipeline.STATUS_SUMMARY_QUERY_CEILING
        assert db.conn.total_changes == before
    finally:
        db.close()
