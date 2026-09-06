"""Public-API QA prerequisites for older isolated safety regression fixtures."""
from pathlib import Path
from du_pipeline.contracts import QAEvidence


def ready_qa(p, scene):
    pid, sid = scene['project_id'], scene['id']
    root = Path(p.status(pid)['project']['artifact_root'])
    image = root / f'qa-{sid}.ppm'
    image.write_bytes(b'P6\n1 1\n255\n\x80\x40\x20')
    aid = p.add_artifact(pid, 'IMAGE', image, sid)
    if p.scene(sid)['state'] == 'PLANNED':
        p.record_image_attempt(sid, True)
    p.record_scene_qa(sid, QAEvidence({'visual': True}, 1, 'fixture-review'))
    return aid
