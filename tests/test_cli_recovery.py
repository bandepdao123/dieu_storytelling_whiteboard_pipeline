"""Recovery routes use public services; SQL below is inspection only."""
import json
import pytest
from du_pipeline.db import Database
from test_cli_media_workflow import cli


def test_measured_recovery_subprocess(tmp_path, monkeypatch):
    from test_cli_measured_narration import test_external_measured_full_media_subprocess
    test_external_measured_full_media_subprocess(tmp_path, monkeypatch)
    db=tmp_path/'local.db'
    pid=inspect(db,'select id from projects')[0]['id']
    scenes=inspect(db,'select * from scenes order by ord')
    attempts=inspect(db,'select * from attempts order by id')
    image=inspect(db,"select * from artifacts where kind='IMAGE' order by id")[0]
    oldfinal=inspect(db,"select * from artifacts where kind='FINAL_VIDEO'")[0]
    oldbytes=__import__('pathlib').Path(oldfinal['uri']).read_bytes()
    data=tmp_path/'checkpoint.json'; data.write_text('{"note":"reviewed fixture"}')
    cli(db,'checkpoint-create',pid,'reviewed',data)
    cli(db,'restore-artifact',image['id'])
    cli(db,'restore-checkpoint',pid,'reviewed')
    proposal=cli(db,'rerun-propose',pid,'image')['proposal_id']
    cli(db,'rerun-apply',proposal,ok=False)
    cli(db,'--role','OPERATOR','rerun-decide',proposal,'APPROVED','--actor','operator',ok=False)
    cli(db,'rerun-decide',proposal,'APPROVED','--actor','owner-original')
    cli(db,'restore-checkpoint',pid,'reviewed')
    cli(db,'rerun-apply',proposal,ok=False)  # stale decision, never implicit approval
    cli(db,'rerun-decide',proposal,'APPROVED','--actor','owner',ok=False)
    cli(db,'rerun-reapprove',proposal,'--actor','owner-current')
    cli(db,'--role','REVIEWER','rerun-apply',proposal,ok=False)
    cli(db,'rerun-apply',proposal)
    cli(db,'rerun-apply',proposal,ok=False)
    assert inspect(db,'select * from attempts order by id')==attempts
    events=inspect(db,"select * from events where type in ('RERUN_DECIDED','RERUN_APPLIED','IMAGE_EPOCH_STARTED') order by id")
    decisions=[e for e in events if e['type']=='RERUN_DECIDED']
    first,last=[json.loads(e['data_json']) for e in decisions]
    assert last['role']=='OWNER' and last['proposal']['actor']=='owner-current'
    assert last['previous_decision_event_id']==decisions[0]['id']
    assert last['project_version']>first['project_version']
    applied=[json.loads(e['data_json']) for e in events if e['type']=='RERUN_APPLIED']
    epochs=[json.loads(e['data_json']) for e in events if e['type']=='IMAGE_EPOCH_STARTED']
    assert len(applied)==len(epochs)==1
    assert applied[0]['decision_event_id']==epochs[0]['decision_event_id']==decisions[-1]['id']
    qa=tmp_path/'qa.json'; ppm=tmp_path/'image.ppm'
    root=__import__('pathlib').Path(oldfinal['uri']).parent
    output=root/'recovered.mp4'
    cli(db,'assemble',pid,output,ok=False)
    for scene in scenes:
        cli(db,'register-artifact',pid,'IMAGE',ppm,'--scene-id',scene['id'])
        cli(db,'image-attempt',scene['id'],'succeeded')
        cli(db,'scene-qa',scene['id'],qa)
        cli(db,'approve',pid,scene['code'],'--actor','fixture-human')
    cli(db,'assemble',pid,output,ok=False)
    cli(db,'post-batch',pid,ppm,qa,'--actor','fixture-human')
    final=cli(db,'assemble',pid,output)
    assert final['artifact_id']!=oldfinal['id'] and output.stat().st_size
    cli(db,'final-review',pid,final['artifact_id'],'APPROVED','--actor','fixture-human',ok=False)
    cli(db,'final-qa',pid,final['artifact_id'],qa)
    cli(db,'final-review',pid,final['artifact_id'],'APPROVED','--actor','fixture-human')
    assert cli(db,'--operational-wal','status-summary',pid)['gates']['final']['current']
    assert cli(db,'assemble',pid,output)['reused']
    assert __import__('pathlib').Path(oldfinal['uri']).read_bytes()==oldbytes
    history=inspect(db,'select * from attempts order by id')
    assert history[:len(attempts)]==attempts and len(history)==2*len(attempts)
    assert all(row['epoch']>attempts[0]['epoch'] and row['number']==1 for row in history[len(attempts):])
    cli(db,'restore-checkpoint',pid,'reviewed',ok=False)  # expanded inventory


@pytest.mark.parametrize('args',[
    ('checkpoint-create','unknown','qa','missing.json'),
    ('restore-checkpoint','unknown','qa'), ('restore-artifact',99999),
    ('rerun-propose','unknown','image'), ('rerun-decide',99999,'APPROVED','--actor','owner'),
    ('rerun-reapprove',99999,'--actor','owner'), ('rerun-apply',99999),
])
def test_missing_recovery_ids(tmp_path,args):
    db=tmp_path/'db'; cli(db,'init','exists')
    result=cli(db,*args,ok=False)
    assert result['error']['code'] in ('INVALID_REQUEST','DATABASE_UNAVAILABLE')
    assert 'unknown' not in str(result) and 'missing.json' not in str(result)


def test_rejected_and_invalid_requests(tmp_path):
    db=tmp_path/'db'; pid=cli(db,'init','validation')['project_id']
    cli(db,'rerun-propose',pid,'not-a-stage',ok=False)
    assert not inspect(db,'select * from proposals')
    proposal=cli(db,'rerun-propose',pid,'plan')['proposal_id']
    assert inspect(db,'select stage from proposals')[0]['stage']=='planning'
    cli(db,'rerun-decide',proposal,'APPROVED','--actor',' ',ok=False)
    cli(db,'rerun-decide',proposal,'REJECTED','--actor','owner')
    cli(db,'rerun-apply',proposal,ok=False)
    cli(db,'rerun-reapprove',proposal,'--actor','owner',ok=False)
    assert not inspect(db,"select * from events where type='RERUN_APPLIED'")
    data=tmp_path/'data'; data.write_text('{}')
    cli(db,'checkpoint-create',pid,' ',data,ok=False)
    cli(db,'checkpoint-create',pid,'qa',tmp_path/'missing',ok=False)
    data.write_bytes(b'\xff')
    cli(db,'checkpoint-create',pid,'qa',data,ok=False)


def inspect(db, sql, args=()):
    with Database.open_existing(db, readonly=True, operational_wal=True) as d:
        return [dict(r) for r in d.all(sql, args)]


@pytest.mark.parametrize('command', ['checkpoint-create','restore-artifact','restore-checkpoint','rerun-propose','rerun-decide','rerun-reapprove','rerun-apply'])
def test_recovery_command_exists(tmp_path, command):
    import subprocess, sys
    r=subprocess.run([sys.executable,'-m','du_pipeline.cli',command,'--help'],capture_output=True,text=True)
    assert r.returncode==0, r.stderr


def test_recovery_minimal(tmp_path):
    db=tmp_path/'db'; pid=cli(db,'init','recovery')['project_id']
    data=tmp_path/'data.json'; data.write_text('{"note":"operator checkpoint"}')
    cli(db,'checkpoint-create',pid,'qa',data)
    snap=json.loads(inspect(db,'select checkpoint_json from stages')[0]['checkpoint_json'])
    assert snap['snapshot_version']==4 and snap['identity'] and snap['data']=={'note':'operator checkpoint'}
    cli(db,'restore-checkpoint',pid,'qa')
    proposal=cli(db,'rerun-propose',pid,'qa')['proposal_id']
    cli(db,'rerun-apply',proposal,ok=False)
    cli(db,'--role','REVIEWER','rerun-decide',proposal,'APPROVED','--actor','reviewer',ok=False)
    cli(db,'rerun-decide',proposal,'APPROVED','--actor','owner-one')
    cli(db,'rerun-reapprove',proposal,'--actor','owner-two')
    cli(db,'rerun-apply',proposal)
    cli(db,'rerun-apply',proposal,ok=False)
    cli(db,'rerun-reapprove',proposal,'--actor','owner-two',ok=False)


@pytest.mark.parametrize('text',['[]','null','{"x":NaN}','{"x":Infinity}','{"x":1,"x":2}','{"x":{"y":1,"y":2}}','{"snapshot_version":3}','{"x":'])
def test_checkpoint_strict_file(tmp_path,text):
    db=tmp_path/'db'; pid=cli(db,'init','input')['project_id']
    data=tmp_path/'private-data.json'; data.write_text(text)
    assert cli(db,'checkpoint-create',pid,'qa',data,ok=False)['error']['code']=='INVALID_REQUEST'
    assert not inspect(db,'select * from stages')
