import sqlite3,pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.adapters import Role
from du_pipeline.contracts import OutputConfig,QAEvidence
from du_pipeline.inputs import normalize_text,narrator_metadata,normalize_document
from du_pipeline.policies import ResourceScheduler

def project(tmp_path):
 d=Database(tmp_path/'x.db'); p=Pipeline(d); pid=p.init_project('x',scene_range=(1,10),seed=42)
 p.import_audio(pid,'a',12000,'a'*64); p.import_srt(pid,[(0,6000,'one'),(6000,12000,'two')]); return d,p,pid,p.plan_scenes(pid,special_codes=('S002',))
def test_gates_retry_checkpoints_and_rerun(tmp_path):
 d,p,pid,scenes=project(tmp_path)
 with pytest.raises(PermissionError): p.start_batch(pid)
 for s in scenes:
  p.record_image_attempt(s['id'],True); p.record_scene_qa(s['id'],QAEvidence({'composition':True},1,'fake')); p.decide_scene(pid,s['code'],'APPROVED','r')
 assert p.start_batch(pid)
 with pytest.raises(PermissionError):p.start_animation(pid)
 with pytest.raises(ValueError):p.approve_post_batch(pid,False,'sheet','r')
 sheet=tmp_path/'sheet.png'; sheet.write_bytes(b'sheet')
 p.approve_post_batch(pid,QAEvidence({'contact_sheet':True},1,'fake'),str(sheet),'r'); assert p.start_animation(pid)
 with pytest.raises(PermissionError): p.record_image_attempt(scenes[0]['id'],False,error='x')
 proposal=p.propose_rerun(pid,'plan');
 with pytest.raises(PermissionError):p.apply_rerun(proposal)
 p.decide_rerun(proposal,True,'owner'); assert p.apply_rerun(proposal)
def test_defaults_inputs_fk_rbac_scheduler(tmp_path):
 o=OutputConfig(); assert (o.width,o.height,o.fps,o.music,o.final_hold_seconds)==(1920,1080,30,False,1.5)
 assert normalize_text(' a\x00  b ')['text']=='a b'; assert narrator_metadata('N')['count']==1
 with pytest.raises(RuntimeError): normalize_document('url','https://example.invalid')
 d,p,pid,_=project(tmp_path)
 with pytest.raises(PermissionError): p.pause(pid,Role.REVIEWER)
 with pytest.raises(sqlite3.IntegrityError):d.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,state,approval_state) values('bad','S1',1,0,1,'x','PLANNED','REQUIRED')")
 s=ResourceScheduler(); s.submit('image',1); s.submit('image',2); assert s.acquire()==('image',1) and s.acquire() is None; s.release('image'); assert s.acquire()==('image',2)
def test_scene_default_range_is_enforced(tmp_path):
 p=Pipeline(Database(tmp_path/'x.db')); pid=p.init_project('x'); p.import_audio(pid,'a',6000,'a'*64); p.import_srt(pid,[(0,6000,'x')])
 with pytest.raises(ValueError,match='50-360'):p.plan_scenes(pid)