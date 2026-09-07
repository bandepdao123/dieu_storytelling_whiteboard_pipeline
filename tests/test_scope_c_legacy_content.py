"""Actual HEAD schema-v12 producer, never a relabelled current checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '929511ebcf42c68f2530e40e5b81fa1f58b543d8'
PRODUCER = '''
import hashlib
from pathlib import Path
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
import sys
root=Path(sys.argv[1])
with Database(root/'legacy.db') as db:
 p=Pipeline(db); pid=p.init_project('legacy',scene_range=(1,3))
 audio=Path(p.status(pid)['project']['artifact_root'])/'audio'; audio.write_bytes(b'audio')
 p.import_audio(pid,str(audio),1000,hashlib.sha256(b'audio').hexdigest())
 p.import_srt(pid,[(0,1000,'historical scene')]); scene=p.plan_scenes(pid)[0]
 image=root/'image'; image.write_bytes(b'image'); p.add_artifact(pid,'IMAGE',image,scene['id'])
 p.record_image_attempt(scene['id'],False,error='fixture')
 from du_pipeline.contracts import QAEvidence
 p.record_image_attempt(scene['id'],True)
 qa=QAEvidence({'ok':True},1,'external fixture')
 p.record_scene_qa(scene['id'],qa); p.decide_scene(pid,scene['code'],'APPROVED','fixture owner')
 p.approve_post_batch(pid,qa,image,'fixture owner')
 p.checkpoint(pid,'legacy',{}); p.restore_latest_checkpoint(pid,'legacy')
 assert db.one('pragma user_version')[0]==12
 (root/'pid').write_text(pid)
'''

@pytest.fixture
def legacy(tmp_path):
 source=tmp_path/'baseline'
 paths=subprocess.check_output(['git','ls-tree','-r','--name-only',BASELINE,'src/du_pipeline'],cwd=ROOT,text=True).splitlines()
 for name in paths:
  target=source/name; target.parent.mkdir(parents=True,exist_ok=True)
  target.write_bytes(subprocess.check_output(['git','show',f'{BASELINE}:{name}'],cwd=ROOT))
 env={**os.environ,'PYTHONPATH':str(source/'src'),'PYTHONDONTWRITEBYTECODE':'1'}
 subprocess.run([sys.executable,'-c',PRODUCER,str(tmp_path)],env=env,check=True,capture_output=True,text=True)
 with Database(tmp_path/'legacy.db') as db:
  p=Pipeline(db); pid=(tmp_path/'pid').read_text()
  yield p,pid

def request(p,pid,**changes):
 row=p.db.one("select stages.* from stages join jobs on jobs.id=stages.job_id where jobs.project_id=? and name='legacy'",(pid,))
 args=dict(expected_revision=p.project(pid)['version'],checkpoint_sha256=json.loads(row['checkpoint_json'])['snapshot_sha256'],allow_legacy_content=True,role='OWNER')
 args.update(changes)
 return p.recover_legacy_checkpoint_content(pid,'legacy',**args)

def dump(p): return list(p.db.conn.iterdump())

@pytest.mark.parametrize('mode',['ok','no-optin','operator','replay'])
def test_actual_cli(legacy,mode):
 p,pid=legacy
 row=p.db.one("select checkpoint_json from stages where name='legacy'")
 args=[sys.executable,'-m','du_pipeline.cli','--db',str(p.db.path),'--role','OPERATOR' if mode=='operator' else 'OWNER','recover-legacy-content',pid,'legacy','--expected-revision',str(p.project(pid)['version']),'--checkpoint-sha256',json.loads(row['checkpoint_json'])['snapshot_sha256']]
 if mode!='no-optin': args+=['--allow-legacy-content']
 if mode=='replay': request(p,pid)
 before=dump(p)
 result=subprocess.run(args,cwd=ROOT,env={**os.environ,'PYTHONPATH':str(ROOT/'src'),'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True)
 assert result.returncode==(0 if mode=='ok' else 2),result.stderr
 if mode=='ok': assert json.loads(result.stdout)['historical_generation_claimed'] is False
 else: assert dump(p)==before

@pytest.mark.parametrize('mutation',['snapshot','inventory','bytes','identity','pause'])
def test_prewrite_race_rejects(legacy,monkeypatch,mutation):
 from contextlib import contextmanager
 p,pid=legacy; original=p._restore_bytes; committed=[]
 @contextmanager
 def changed(pid,artifacts):
  with original(pid,artifacts) as pins:
   if mutation=='snapshot':
    p.db.execute("update stages set checkpoint_json=checkpoint_json||' ' where name='legacy'")
   elif mutation=='inventory':
    p.db.execute("update artifacts set version=version+1 where project_id=?",(pid,))
   elif mutation=='pause': p.pause(pid)
   else:
    path=Path(artifacts[0]['uri'])
    if mutation=='identity':
     data=path.read_bytes(); path.rename(path.with_suffix('.original')); path.write_bytes(data)
    else: path.write_bytes(b'changed')
   committed.extend(dump(p))
   yield pins
 monkeypatch.setattr(p,'_restore_bytes',changed)
 with pytest.raises((PermissionError,ValueError)): request(p,pid)
 assert dump(p)==committed

@pytest.mark.parametrize('mutation',['digest','ownership','inventory','scene','job'])
def test_unprovable_snapshot_rejects(legacy,mutation):
 p,pid=legacy
 row=p.db.one("select * from stages where name='legacy'"); snap=json.loads(row['checkpoint_json'])
 if mutation=='digest': snap['scenes'][0]['text']='tampered'
 elif mutation=='ownership': snap['project']['id']='other'
 elif mutation=='inventory': snap['artifacts']=[]
 elif mutation=='scene': snap['scenes'][0]['text']='different source'
 else: snap['job']['project_id']='other'
 if mutation!='digest':
  payload={k:snap[k] for k in ('snapshot_version','data','project','job','scenes','artifacts')}
  snap['snapshot_sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 p.db.execute('update stages set checkpoint_json=? where id=?',(json.dumps(snap),row['id']))
 before=dump(p)
 with pytest.raises(ValueError): request(p,pid)
 assert dump(p)==before

def test_actual_legacy_content_new_generation_preserves_history(legacy):
 p,pid=legacy; old=p._checkpoint_generation(pid); revision=p.project(pid)['version']
 checkpoints=[tuple(r) for r in p.db.all('select * from stages')]
 attempts=[tuple(r) for r in p.db.all('select * from attempts')]; epoch=p._attempt_epoch(pid)
 sources=[tuple(r) for r in p.db.all('select * from audio')]
 before_bytes={r['uri']:Path(r['uri']).read_bytes() for r in p.db.all('select * from artifacts')}
 receipt=request(p,pid)
 assert receipt['expected_revision']==revision
 assert receipt['new_generation']==p._checkpoint_generation(pid)>old
 assert p.project(pid)['version']==revision+1
 assert [tuple(r) for r in p.db.all('select * from stages')]==checkpoints
 assert [tuple(r) for r in p.db.all('select * from attempts')]==attempts
 assert [tuple(r) for r in p.db.all('select * from audio')]==sources
 assert p._attempt_epoch(pid)==epoch
 assert p._attempt_count(p.status(pid)['scenes'][0]['id'])==2
 assert not p.db.one('select 1 from approvals where revoked_at is null')
 assert all(s['state']=='PLANNED' and s['qa_state']=='PENDING' and s['qa_json']=='{}' for s in p.status(pid)['scenes'])
 assert not p.db.one("select 1 from artifacts where status='ACTIVE'")
 assert not p.db.one("select 1 from jobs where state in ('QUEUED','RUNNING','PAUSED')")
 assert all(Path(path).read_bytes()==data for path,data in before_bytes.items())
 assert json.loads(p.db.one("select data_json from events where type='LEGACY_CONTENT_RECOVERED'")['data_json'])==receipt
 with pytest.raises(ValueError,match='already recovered'): request(p,pid)

@pytest.mark.parametrize('changes',[{'role':'OPERATOR'},{'role':'VIEWER'},{'allow_legacy_content':False},{'expected_revision':-1},{'checkpoint_sha256':'0'*64}])
def test_reject_authority_optin_revision_digest(legacy,changes):
 p,pid=legacy; before=dump(p)
 with pytest.raises((ValueError,PermissionError)): request(p,pid,**changes)
 assert dump(p)==before

def test_budget_exhaustion_and_fresh_v4(legacy):
 p,pid=legacy; sid=p.status(pid)['scenes'][0]['id']
 # Recovery leaves only one remaining attempt, not a fresh three-attempt budget.
 request(p,pid)
 p.record_image_attempt(sid,False,error='third')
 assert p._attempt_count(sid)==3
 with pytest.raises(ValueError,match='maximum 3'): p.queue_retry(sid)
 p.checkpoint(pid,'fresh-v4',{})
 p.restore_latest_checkpoint(pid,'fresh-v4')
 assert p._attempt_count(sid)==3


def test_default_restore_stays_closed(legacy):
 p,pid=legacy; before=dump(p)
 with pytest.raises(ValueError,match='legacy'): p.restore_latest_checkpoint(pid,'legacy')
 assert dump(p)==before

@pytest.mark.parametrize('outer',[False,True])
def test_receipt_and_mutation_rollback(legacy,monkeypatch,outer):
 p,pid=legacy; before=dump(p)
 class Fault(Exception): pass
 if outer:
  with pytest.raises(Fault):
   with p.db.transaction():
    request(p,pid)
    raise Fault()
 else:
  original=p._event
  def fail(pid,kind,data=None):
   original(pid,kind,data)
   if kind=='LEGACY_CONTENT_RECOVERED': raise Fault()
  monkeypatch.setattr(p,'_event',fail)
  with pytest.raises(Fault): request(p,pid)
 assert dump(p)==before
