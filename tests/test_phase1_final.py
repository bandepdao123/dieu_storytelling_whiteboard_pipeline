import json, pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.adapters import Role, CommandError

def pipe(tmp_path, duration, cue):
 p=Pipeline(Database(tmp_path/'x.db')); pid=p.init_project('x',scene_range=(1,360))
 p.import_audio(pid,'a',duration,'0'*64); p.import_srt(pid,[cue]); return p,pid

@pytest.mark.parametrize('duration,exception',[(4000,True),(9000,True),(6000,False)])
def test_duration_semantic_exceptions(tmp_path,duration,exception):
 p,pid=pipe(tmp_path,duration,(0,duration,'unit')); s=p.plan_scenes(pid)
 assert bool(s[0]['duration_exception']) is exception
 if exception: assert json.loads(s[0]['duration_exception'])['reason']=='unavoidable_semantic_unit'

def test_duration_crosses_180_seconds(tmp_path):
 p,pid=pipe(tmp_path,185000,(0,185000,'cross')); scenes=p.plan_scenes(pid)
 assert any(x['end_ms']==180000 for x in scenes)
 assert all(not (x['start_ms']<180000<x['end_ms']) for x in scenes)

def test_command_schema_and_rbac_never_indexerror(tmp_path):
 p=Pipeline(Database(tmp_path/'x.db'))
 with pytest.raises(CommandError) as e:p.dispatch_discord('du-tao-du-an',Role.OWNER)
 assert e.value.code=='INVALID_ARGUMENTS'
 with pytest.raises(PermissionError):p.dispatch_discord('du-tao-du-an x',Role.OPERATOR)
 assert p.dispatch_discord('du-tao-du-an x',Role.OWNER)['project_id']

def test_cleanup_preserves_immutable_and_is_idempotent(tmp_path):
 p,pid=pipe(tmp_path,6000,(0,6000,'x')); keep=tmp_path/'keep'; drop=tmp_path/'drop'; keep.write_bytes(b'k'); drop.write_bytes(b'd')
 p.add_artifact(pid,'FINAL',str(keep)); p.add_artifact(pid,'IMAGE',str(drop)); p.db.execute("update artifacts set expires_at='2000-01-01T00:00:00+00:00'")
 assert p.cleanup()==1 and drop.exists() and keep.exists(); assert p.cleanup()==0
 with pytest.raises(PermissionError):p.cleanup(Role.OPERATOR)

def test_latest_checkpoint_and_rerun_version(tmp_path):
 p,pid=pipe(tmp_path,6000,(0,6000,'x')); p.checkpoint(pid,'image',{'n':1}); p.checkpoint(pid,'image',{'n':2})
 assert json.loads(p.restore_latest_checkpoint(pid,'image')['checkpoint_json'])['n']==2
 before=p.db.one('select version from projects where id=?',(pid,))['version']
 q=p.propose_rerun(pid,'image'); p.decide_rerun(q,True,'o'); p.apply_rerun(q)
 assert p.db.one('select version from projects where id=?',(pid,))['version']==before+1