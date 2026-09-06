import json
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path/'p.db') as db:
        p=Pipeline(db); pid=p.init_project('rerun',scene_range=(1,2))
        root=tmp_path/'artifacts'/pid
        a=root/'audio'; a.write_bytes(b'a')
        p.import_audio(pid,str(a),6000,'a'*64)
        p.import_srt(pid,[(0,6000,'text')]); sid=p.plan_scenes(pid)[0]['id']
        yield p,pid,root,sid


def rerun(p,pid,stage):
    i=p.propose_rerun(pid,stage); p.decide_rerun(i,True,'owner'); return p.apply_rerun(i)


@pytest.mark.parametrize('stage,retired',[
    ('planning',{'IMAGE','CONTACT_SHEET','ANIMATION','SCENE_VIDEO','FINAL_VIDEO'}),
    ('image',{'IMAGE','CONTACT_SHEET','ANIMATION','SCENE_VIDEO','FINAL_VIDEO'}),
    ('qa',{'CONTACT_SHEET','ANIMATION','SCENE_VIDEO','FINAL_VIDEO'}),
    ('animation',{'ANIMATION','SCENE_VIDEO','FINAL_VIDEO'}),
    ('assembly',{'FINAL_VIDEO'}),('upload',set())])
def test_typed_artifact_matrix(setup,stage,retired):
    p,pid,root,sid=setup
    ids={}
    for kind in ('IMAGE','CONTACT_SHEET','ANIMATION','SCENE_VIDEO','FINAL_VIDEO','BIBLE','INPUT'):
        path=root/kind; path.write_bytes(kind.encode())
        ids[kind]=p.add_artifact(pid,kind,path,sid if kind in ('IMAGE','ANIMATION','SCENE_VIDEO') else None)
    rerun(p,pid,stage)
    for kind,aid in ids.items():
        assert p.db.one('select status from artifacts where id=?',(aid,))['status']==('SUPERSEDED' if kind in retired else 'ACTIVE'),kind
        assert (root/kind).exists()


def test_image_epoch_budget_and_history_survive_reopen_replan(setup):
    p,pid,root,sid=setup
    for _ in range(3): p.record_image_attempt(sid,False)
    original=[dict(r) for r in p.db.all('select * from attempts')]
    rerun(p,pid,'image')
    assert p.scene(sid)['state']=='PLANNED'
    for _ in range(3): p.record_image_attempt(sid,False)
    with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(sid,False)
    rows=p.db.all('select * from attempts')
    assert len(rows)==6
    assert [dict(r) for r in rows[:3]]==original
    rerun(p,pid,'qa')
    with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(sid,False)
    p.plan_scenes(pid)
    assert len(p.db.all('select * from attempts'))==6
    with Database(p.db.path) as reopened:
        assert len(reopened.all('select * from attempts'))==6


@pytest.mark.parametrize('stage',['planning','image','qa','animation','assembly','upload'])
def test_gate_job_matrix_and_unrelated_project(setup,stage):
    p,pid,root,sid=setup
    from du_pipeline.dependencies import IMPACT,Stage
    gates=('SCENE','PILOT','POST_BATCH','BATCH','FINAL','UPLOAD')
    for gate in gates:
        p.db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at) values(?,?,?,'APPROVED','fixture','old')",(pid,sid if gate=='SCENE' else None,gate))
    jobs={kind:p._create_job(pid,kind) for kind in ('BATCH_IMAGE','ANIMATION','ASSEMBLY','UPLOAD','UNKNOWN')}
    other=p.init_project('unrelated',scene_range=(1,1)); other_version=p.status(other)['project']['version']
    before=p.status(pid)['project']['version']
    rerun(p,pid,stage)
    assert p.status(other)['project']['version']==other_version
    assert p.status(pid)['project']['version']==before+(stage!='upload')
    for a in p.db.all('select * from approvals where project_id=?',(pid,)):
        assert bool(a['revoked_at'])==(a['gate'] in IMPACT[Stage(stage)].approvals)
    for kind,jid in jobs.items():
        row=p.db.one('select * from jobs where id=?',(jid,))
        assert row['state']==('BLOCKED' if stage!='upload' or kind=='UPLOAD' else 'QUEUED')
        if row['state']=='BLOCKED': assert row['lease_owner'] is None


def test_failed_rerun_transaction_does_not_reset_budget(setup,monkeypatch):
    p,pid,root,sid=setup
    for _ in range(3): p.record_image_attempt(sid,False)
    proposal=p.propose_rerun(pid,'image'); p.decide_rerun(proposal,True,'owner')
    original=p._event
    def fail(pid,typ,data=None):
        if typ=='RERUN_APPLIED': raise RuntimeError('crash')
        return original(pid,typ,data)
    monkeypatch.setattr(p,'_event',fail)
    with pytest.raises(RuntimeError): p.apply_rerun(proposal)
    assert p.db.one('select state from proposals where id=?',(proposal,))['state']=='APPROVED'
    assert not p.db.all("select * from events where type='IMAGE_EPOCH_STARTED'")
    with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(sid,False)


def test_unapproved_unknown_paused_rerun_no_effect(setup):
    p,pid,root,sid=setup
    i=p.propose_rerun(pid,'image')
    with pytest.raises(PermissionError): p.apply_rerun(i)
    unknown=p.propose_rerun(pid,'bogus'); p.decide_rerun(unknown,True,'owner')
    before=p.status(pid)
    with pytest.raises(ValueError): p.apply_rerun(unknown)
    assert p.status(pid)==before
    p.decide_rerun(i,True,'owner'); p.pause(pid)
    with pytest.raises(PermissionError): p.apply_rerun(i)
    assert p.db.one('select state from proposals where id=?',(i,))['state']=='APPROVED'


def test_replan_without_approved_epoch_does_not_reset_budget(setup):
    p,pid,root,sid=setup
    for _ in range(3): p.record_image_attempt(sid,False)
    new_sid=p.plan_scenes(pid)[0]['id']
    with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(new_sid,False)
    rerun(p,pid,'planning'); new_sid=p.plan_scenes(pid)[0]['id']
    p.record_image_attempt(new_sid,False)
    assert len(p.db.all('select * from attempts'))==4


def test_rerun_replay_does_not_grant_epoch(setup):
    p,pid,root,sid=setup
    i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'owner'); p.apply_rerun(i)
    before=[dict(r) for r in p.db.all('select * from events')]
    with pytest.raises(PermissionError): p.apply_rerun(i)
    assert [dict(r) for r in p.db.all('select * from events')]==before
