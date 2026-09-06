"""Deletion regressions: caller-supplied paths are not deletion authority."""
import hashlib
import os

import pytest

from du_pipeline.db import Database
from du_pipeline.service import Pipeline


@pytest.mark.parametrize('subject', ['active', 'narration', 'hardlink', 'unregistered', 'external'])
def test_failed_binary_without_ownership_receipt_is_retained(tmp_path, subject):
    db = Database(tmp_path / 'deletion.db')
    try:
        pipeline = Pipeline(db)
        pid = pipeline.init_project('deletion ownership', scene_range=(1, 1))
        root = tmp_path / 'artifacts' / pid
        audio = root / 'audio.wav'
        audio.write_bytes(b'narration')
        pipeline.import_audio(pid, str(audio), 6000, hashlib.sha256(audio.read_bytes()).hexdigest())
        pipeline.import_srt(pid, [(0, 6000, 'scene')])
        scene = pipeline.plan_scenes(pid)[0]
        image = root / 'image.png'
        image.write_bytes(b'active image')
        aid = pipeline.add_artifact(pid, 'IMAGE', image, scene['id'])
        candidate = image
        if subject == 'narration':
            candidate = audio
        elif subject == 'hardlink':
            candidate = root / 'alias.png'
            os.link(image, candidate)
        elif subject == 'unregistered':
            candidate = root / 'unregistered.png'
            candidate.write_bytes(b'not an attempt receipt')
        elif subject == 'external':
            candidate = tmp_path / 'external.png'
            candidate.write_bytes(b'outside managed root')
        expected = candidate.read_bytes()
        pipeline.record_image_attempt(scene['id'], False, failed_binary=str(candidate), error='local failure')
        assert candidate.exists(), 'an arbitrary failed_binary path must not authorize deletion'
        assert candidate.read_bytes() == expected
        assert image.read_bytes() == b'active image'
        assert audio.read_bytes() == b'narration'
        assert db.one('select status from artifacts where id=?', (aid,))['status'] == 'ACTIVE'
        assert pipeline.scene(scene['id'])['state'] == 'RETRYABLE'
    finally:
        db.close()
