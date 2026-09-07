"""F09/F16 public boundary regressions; SQL only injects legacy stored configs."""
import hashlib
import json
from pathlib import Path
import pytest
from du_pipeline.contracts import OutputConfig, QAEvidence
from du_pipeline.db import Database
from du_pipeline.media import AssemblyError
from du_pipeline.policies import TimingMismatch
from du_pipeline.service import Pipeline

@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path / 'test.db') as db:
        p = Pipeline(db)
        pid = p.init_project('timing', scene_range=(1, 20))
        root = Path(p.status(pid)['project']['artifact_root'])
        yield p, pid, root

def sources(p, pid, root, duration, cues):
    audio = root / 'audio.wav'
    audio.write_bytes(b'original narration fixture')
    p.import_audio(pid, str(audio), duration, hashlib.sha256(audio.read_bytes()).hexdigest())
    p.import_srt(pid, cues)
    return audio

@pytest.mark.parametrize('duration,cues', [
    (13000, [(1000,6000,'first'),(7000,12500,'second')]),
    (6000, [(1,6000,'start')]),
    (6000, [(0,3000,'a'),(3001,6000,'b')]),
    (6000, [(0,5999,'tail')]),
    (6000, [(0,5500,'tail tolerance')]),
    (6000, [(0,1,'subframe'),(1,6000,'rest')]),
])
def test_import_allowed_but_local_planning_rejects_without_mutation(setup, duration, cues):
    p,pid,root = setup
    audio = sources(p,pid,root,duration,cues)
    before = list(p.db.conn.iterdump())
    with pytest.raises(TimingMismatch, match='local assembly.*unsupported.*Recovery'):
        p.plan_scenes(pid)
    assert list(p.db.conn.iterdump()) == before
    assert audio.read_bytes() == b'original narration fixture'
    # With no assembly-ready plan, assembly remains blocked too.
    with pytest.raises(AssemblyError):
        p.assemble(pid, root/'out.mp4', dry_run=True)

@pytest.mark.parametrize('kwargs', [dict(width=3840),dict(height=2160),dict(fps=60),dict(transition='crossfade'),dict(final_hold_seconds=2)])
def test_output_config_rejects_unsupported(kwargs):
    with pytest.raises(ValueError, match='local output.*unsupported'):
        OutputConfig(**kwargs)

def test_configure_transition_rejects_before_invalidation(setup):
    p,pid,root = setup
    sources(p,pid,root,6000,[(0,6000,'raw text')]); p.plan_scenes(pid)
    before = list(p.db.conn.iterdump())
    with pytest.raises(ValueError, match='local output.*unsupported'):
        p.configure_project(pid,'transition','crossfade')
    assert list(p.db.conn.iterdump()) == before

@pytest.mark.parametrize('legacy', [dict(width=3840),dict(fps=60),dict(transition='crossfade'),dict(final_hold_seconds=2),dict(audio_codec='opus')])
@pytest.mark.parametrize('entry', ['plan','dry','real'])
def test_legacy_config_rejected_before_mutation_or_encode(setup, monkeypatch, legacy, entry):
    p,pid,root = setup
    sources(p,pid,root,6000,[(0,6000,'raw')]); p.plan_scenes(pid)
    value = {**OutputConfig().__dict__, **legacy}
    with p.db.transaction():
        p.db.execute('update projects set output_json=? where id=?',(json.dumps({'preset':'youtube','config':value}),pid))
    before = list(p.db.conn.iterdump())
    def forbidden(*args, **kwargs): pytest.fail('reconciliation/encode reached')
    monkeypatch.setattr(p,'reconcile_publications',forbidden)
    with pytest.raises((ValueError,AssemblyError), match='local output.*unsupported'):
        if entry=='plan': p.plan_scenes(pid)
        else: p.assemble(pid,root/'out.mp4',dry_run=entry=='dry')
    assert list(p.db.conn.iterdump()) == before
    assert not (root/'out.mp4').exists()

@pytest.mark.parametrize('entry', ['plan','dry','real'])
def test_legacy_project_transition_rejected(setup, entry):
    p,pid,root=setup
    sources(p,pid,root,6000,[(0,6000,'text')]); p.plan_scenes(pid)
    with p.db.transaction():
        p.db.execute("update projects set transition='crossfade' where id=?",(pid,))
    before=list(p.db.conn.iterdump())
    with pytest.raises((ValueError,AssemblyError),match='local output.*unsupported'):
        if entry=='plan': p.plan_scenes(pid)
        else: p.assemble(pid,root/'out.mp4',dry_run=entry=='dry')
    assert list(p.db.conn.iterdump())==before
    p.configure_project(pid,'transition','hard_cut')
    assert p.plan_scenes(pid)

def test_init_conflicting_transition_fails_without_root_or_project(setup):
    p,pid,root=setup
    before=list(p.db.conn.iterdump()); directories=set(root.parent.iterdir())
    with pytest.raises(ValueError,match='local output.*unsupported'):
        p.init_project('bad',transition='crossfade',output=OutputConfig())
    assert list(p.db.conn.iterdump())==before
    assert set(root.parent.iterdir())==directories

@pytest.mark.parametrize('preset', ['youtube','presentation'])
def test_supported_preset_plans_without_source_rewrite(setup,preset):
    p,pid,root=setup
    sources(p,pid,root,6000,[(0,6000,' text ')]); p.select_preset(pid,preset)
    assert p.plan_scenes(pid)[0]['text']==' text '

@pytest.mark.parametrize('end,boundary,expected', [(6050,3050,[92,90]),(6150,3150,[94,90]),(6001,3001,[90,90])])
def test_contiguous_quantization_public_plan_and_assembly(setup,end,boundary,expected):
    p,pid,root = setup
    cues=[(0,boundary,'  original\ntext  '),(boundary,end,'second')]
    audio=sources(p,pid,root,end,cues)
    scenes=p.plan_scenes(pid)
    assert [(s['start_ms'],s['end_ms'],s['text']) for s in scenes]==cues
    qa=QAEvidence({'ok':True},1,'test')
    for s in scenes:
        image=root/s['code']; image.write_bytes(b'image')
        p.add_artifact(pid,'IMAGE',image,s['id']); p.record_image_attempt(s['id'],True)
        p.record_scene_qa(s['id'],qa); p.decide_scene(pid,s['code'],'APPROVED','reviewer')
    contact=root/'contact'; contact.write_bytes(b'contact')
    p.approve_post_batch(pid,qa,contact,'reviewer')
    result=p.assemble(pid,root/'out.mp4',dry_run=True)
    assert [s['frames'] for s in result['manifest']['scenes']]==expected
    assert audio.read_bytes()==b'original narration fixture'
