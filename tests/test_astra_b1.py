"""B1 only: OWNER decisions, historical approvals, and atomic provenance."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from du_pipeline.adapters import Role
from du_pipeline.db import Database, SchemaVersionError
from du_pipeline.service import Pipeline


@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path/'p.db') as db:
        p=Pipeline(db); pid=p.init_project('B1',scene_range=(1,1))
        p.import_audio(pid,'unused',6000,'a'*64)
        p.import_srt(pid,[(0,6000,'text')]); sid=p.plan_scenes(pid)[0]['id']
        for _ in range(3): p.record_image_attempt(sid,False,error='preserve')
        yield p,pid,sid


def snapshot(db):
    return {t:[dict(r) for r in db.all('select * from '+t+' order by id')]
            for t in ('projects','proposals','events','attempts','jobs','scenes','artifacts','approvals')}


def decision(db):
    row=db.one("select * from events where type='RERUN_DECIDED' order by id desc")
    assert row is not None, 'durable OWNER decision provenance missing'
    return row,json.loads(row['data_json'])


@pytest.mark.parametrize('role',[Role.OWNER,Role.REVIEWER,Role.OPERATOR,'OWNER','REVIEWER','invalid',None])
@pytest.mark.parametrize('approved',[True,False])
def test_decision_role_matrix(setup,role,approved):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); before=snapshot(p.db)
    if role in (Role.OWNER,'OWNER'):
        p.decide_rerun(i,approved,'human',role=role)
        _,data=decision(p.db)
        assert data['role']=='OWNER' and data['proposal']['id']==i
        assert data['proposal']['actor']=='human'
        assert data['proposal']['state']==('APPROVED' if approved else 'REJECTED')
        assert data['project_version']==before['projects'][0]['version']
        assert not p.db.all("select * from events where type='IMAGE_EPOCH_STARTED'")
    else:
        with pytest.raises(PermissionError): p.decide_rerun(i,approved,'human',role=role)
        assert snapshot(p.db)==before


@pytest.mark.parametrize('role',[Role.OWNER,Role.REVIEWER,Role.OPERATOR])
def test_apply_role_matrix_and_history(setup,role):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'owner')
    receipt,data=decision(p.db); before=snapshot(p.db)
    if role!=Role.OWNER:
        with pytest.raises(PermissionError): p.apply_rerun(i,role=role)
        assert snapshot(p.db)==before
        return
    with Database(p.db.path) as reopened:
        p=Pipeline(reopened); p.apply_rerun(i,role=role)
        epoch=reopened.one("select * from events where type='IMAGE_EPOCH_STARTED'")
        applied=reopened.one("select * from events where type='RERUN_APPLIED'")
        assert json.loads(epoch['data_json'])['decision_event_id']==receipt['id']
        assert json.loads(applied['data_json'])['decision_event_id']==receipt['id']
        p.record_image_attempt(sid,False)
        assert [dict(r) for r in reopened.all('select * from attempts order by id')][:3]==before['attempts']
        assert reopened.one('select * from attempts order by id desc')['epoch']==epoch['id']
        after=snapshot(reopened)
        with pytest.raises(PermissionError): p.apply_rerun(i)
        with pytest.raises(PermissionError): p.decide_rerun(i,True,'owner',reapprove=True)
        assert snapshot(reopened)==after
        for sql in ('update events set data_json=data_json','delete from events'):
            with pytest.raises(sqlite3.IntegrityError,match='append only'): reopened.execute(sql)


@pytest.mark.parametrize('actor',['legacy-reviewer','owner'])
def test_candidate_unproven_approval_requires_explicit_reapproval(setup,actor):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image')
    # Candidate/v11 proposals stored only state + actor, not decision role.
    p.db.execute("update proposals set state='APPROVED',actor=? where id=?",(actor,i))
    before=snapshot(p.db)
    with pytest.raises(PermissionError): p.apply_rerun(i,role=Role.OWNER)
    with pytest.raises(PermissionError): p.decide_rerun(i,True,'fresh-owner')
    with pytest.raises(PermissionError): p.decide_rerun(i,True,'reviewer',role=Role.REVIEWER,reapprove=True)
    assert snapshot(p.db)==before
    p.decide_rerun(i,True,'fresh-owner',reapprove=True)
    _,data=decision(p.db)
    assert data['previous_proposal']==before['proposals'][0]
    assert data['previous_decision_event_id'] is None
    p.apply_rerun(i); p.record_image_attempt(sid,False)
    assert snapshot(p.db)['attempts'][:3]==before['attempts']


@pytest.mark.parametrize('change',['revision','stage','actor','created_at','project','receipt_role','receipt_revision','receipt_proposal','receipt_format','receipt_malformed'])
def test_stale_or_tampered_provenance_fails_closed(setup,change):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'owner')
    receipt,data=decision(p.db)
    if change=='revision': p.plan_scenes(pid)
    elif change=='project':
        other=p.init_project('other',scene_range=(1,1))
        p.db.execute('update proposals set project_id=? where id=?',(other,i))
    elif change in ('stage','actor','created_at'):
        p.db.execute('update proposals set '+change+'=? where id=?',('qa' if change=='stage' else 'tampered',i))
    else:
        # Explicit corruption simulation, not an authorization/setup shortcut.
        p.db.execute('drop trigger events_no_update')
        if change=='receipt_role': data['role']='REVIEWER'
        if change=='receipt_revision': data['project_version']+=1
        if change=='receipt_proposal': data['proposal']['id']+=1
        if change=='receipt_format': data['format']='unknown'
        p.db.execute('update events set data_json=? where id=?',('null' if change=='receipt_malformed' else json.dumps(data),receipt['id']))
    before=snapshot(p.db)
    with pytest.raises(PermissionError): p.apply_rerun(i)
    assert snapshot(p.db)==before


def test_stale_owner_fresh_reapproval_preserves_decisions(setup):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'first')
    first,_=decision(p.db); p.plan_scenes(pid)
    p.decide_rerun(i,True,'second',reapprove=True)
    second,data=decision(p.db)
    assert data['previous_decision_event_id']==first['id']
    assert dict(p.db.one('select * from events where id=?',(first['id'],)))==dict(first)
    p.apply_rerun(i)
    assert json.loads(p.db.one("select data_json from events where type='IMAGE_EPOCH_STARTED'")['data_json'])['decision_event_id']==second['id']


@pytest.mark.parametrize('point',['RERUN_DECIDED','IMAGE_EPOCH_STARTED','RERUN_APPLIED','job'])
def test_transaction_rollback_preserves_authorization_attempts(setup,monkeypatch,point):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image')
    if point!='RERUN_DECIDED': p.decide_rerun(i,True,'owner')
    before=snapshot(p.db); original=p._event
    def fail(pid,typ,data=None):
        original(pid,typ,data)
        if typ==point: raise RuntimeError('injected')
    monkeypatch.setattr(p,'_event',fail)
    if point=='job':
        monkeypatch.setattr(p,'_create_job',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('injected')))
    with pytest.raises(RuntimeError,match='injected'):
        if point=='RERUN_DECIDED': p.decide_rerun(i,True,'owner')
        else: p.apply_rerun(i)
    assert snapshot(p.db)==before
    with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(sid,False)


@pytest.mark.parametrize('stage',['planning','image','qa','animation','assembly','upload'])
def test_all_stages_reject_unproven_approval(setup,stage):
    p,pid,sid=setup; i=p.propose_rerun(pid,stage)
    p.db.execute("update proposals set state='APPROVED',actor='owner' where id=?",(i,))
    before=snapshot(p.db)
    with pytest.raises(PermissionError): p.apply_rerun(i)
    assert snapshot(p.db)==before


def test_reapproval_rollback_and_rejection_preserve_history(setup,monkeypatch):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'first')
    before=snapshot(p.db); original=p._event
    def fail(*a,**kw):
        original(*a,**kw)
        raise RuntimeError('reapproval audit')
    with monkeypatch.context() as m:
        m.setattr(p,'_event',fail)
        with pytest.raises(RuntimeError): p.decide_rerun(i,True,'second',reapprove=True)
    assert snapshot(p.db)==before
    p.decide_rerun(i,False,'rejecting-owner',reapprove=True)
    after=snapshot(p.db)
    assert after['events'][:len(before['events'])]==before['events']
    with pytest.raises(PermissionError): p.apply_rerun(i)
    with pytest.raises(PermissionError): p.decide_rerun(i,True,'owner',reapprove=True)
    assert snapshot(p.db)==after


def test_newest_invalid_receipt_never_falls_back(setup):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'owner')
    receipt,data=decision(p.db)
    # Corrupt append represents newer invalid authorization; old valid grant must not win.
    data['role']='REVIEWER'
    p.db.execute("insert into events(project_id,type,data_json,created_at) values(?,'RERUN_DECIDED',?,'injected')",(pid,json.dumps(data)))
    before=snapshot(p.db)
    with pytest.raises(PermissionError): p.apply_rerun(i)
    assert snapshot(p.db)==before
    p.decide_rerun(i,True,'fresh-owner',reapprove=True)
    p.apply_rerun(i)


@pytest.mark.parametrize('actor',['', '   ', None])
def test_actor_required_without_mutation(setup,actor):
    p,pid,sid=setup; i=p.propose_rerun(pid,'image'); before=snapshot(p.db)
    with pytest.raises(ValueError): p.decide_rerun(i,True,actor)
    assert snapshot(p.db)==before


@pytest.fixture
def actual_v11(tmp_path):
    repo=Path(__file__).resolve().parents[1]; package=tmp_path/'baseline'/'du_pipeline'; package.mkdir(parents=True)
    for name in ('__init__.py','db.py','service.py','policies.py','contracts.py','adapters.py','inputs.py','atomic_fs.py'):
        (package/name).write_bytes(subprocess.check_output(['git','show','3bfe33d:src/du_pipeline/'+name],cwd=repo))
    path=tmp_path/'legacy.db'
    code="""from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.adapters import Role
import sys
with Database(sys.argv[1]) as db:
 p=Pipeline(db); pid=p.init_project('legacy',scene_range=(1,1)); p.import_audio(pid,'unused',6000,'a'*64); p.import_srt(pid,[(0,6000,'old')]); sid=p.plan_scenes(pid)[0]['id']
 for _ in range(3): p.record_image_attempt(sid,False,error='legacy')
 i=p.propose_rerun(pid,'image'); p.decide_rerun(i,True,'legacy-reviewer',role=Role.REVIEWER)
"""
    subprocess.run([sys.executable,'-c',code,str(path)],check=True,cwd=tmp_path,env=dict(os.environ,PYTHONPATH=str(package.parent),PYTHONDONTWRITEBYTECODE='1'))
    return path


def test_actual_v11_pending_reviewer_migration_and_reapproval(actual_v11):
    with sqlite3.connect(actual_v11) as old:
        old.row_factory=sqlite3.Row
        assert old.execute('pragma user_version').fetchone()[0]==11
        attempts=[dict(r) for r in old.execute('select * from attempts order by id')]
        proposal=dict(old.execute('select * from proposals').fetchone())
        events=[dict(r) for r in old.execute('select * from events order by id')]
    with Database(actual_v11) as db:
        p=Pipeline(db); i=proposal['id']; sid=attempts[0]['scene_id']
        assert dict(db.one('select * from proposals'))==proposal
        assert [dict(r) for r in db.all('select * from events order by id')]==events
        before=snapshot(db)
        with pytest.raises(PermissionError): p.apply_rerun(i)
        assert snapshot(db)==before
        with pytest.raises(ValueError,match='maximum 3'): p.record_image_attempt(sid,False)
        p.decide_rerun(i,True,'fresh-owner',role=Role.OWNER,reapprove=True)
        assert decision(db)[1]['previous_proposal']==proposal
        p.apply_rerun(i); p.record_image_attempt(sid,False)
        rows=[dict(r) for r in db.all('select * from attempts order by id')]
        assert [{k:r[k] for k in attempts[0]} for r in rows[:3]]==attempts
        assert all(r['epoch']==0 for r in rows[:3]) and rows[-1]['epoch']>0
        assert not db.all('pragma foreign_key_check')


@pytest.mark.parametrize('fault',['final_validation','orphan'])
def test_actual_v11_pending_migration_atomicity(actual_v11,monkeypatch,fault):
    if fault=='orphan':
        with sqlite3.connect(actual_v11) as c: c.execute('update attempts set scene_id=999')
    with sqlite3.connect(actual_v11) as c: before=list(c.iterdump())
    connect=sqlite3.connect
    class FailValidation(sqlite3.Connection):
        def execute(self,sql,*a,**kw):
            if sql=='PRAGMA foreign_key_check': raise sqlite3.IntegrityError('injected validation')
            return super().execute(sql,*a,**kw)
    if fault=='final_validation': monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:connect(*a,factory=FailValidation,**kw))
    with pytest.raises((sqlite3.IntegrityError,SchemaVersionError)): Database(actual_v11)
    with connect(actual_v11) as c:
        assert c.execute('pragma user_version').fetchone()[0]==11
        assert list(c.iterdump())==before
