import hashlib,json,os,uuid
from datetime import datetime,timezone,timedelta
from .policies import validate_timing,required_approval

def now(): return datetime.now(timezone.utc).isoformat()
class Pipeline:
 def __init__(self,db): self.db=db
 def event(self,pid,typ,data={}): self.db.execute("insert into events(project_id,type,data_json,created_at) values(?,?,?,?)",(pid,typ,json.dumps(data),now()))
 def init_project(self,name,language="vi",seed=0,style=None,references=None,bible=None,image_provider="codex-gpt-image-2",whiteboard_mode="ask",transition="hard_cut"):
  if language not in ('vi','en'): raise ValueError('language')
  pid=str(uuid.uuid4()); self.db.execute("insert into projects values(?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,name,language,"ACTIVE",json.dumps(style or {}),json.dumps(references or []),json.dumps(bible or {}),image_provider,whiteboard_mode,seed,transition,None,now())); self.event(pid,"PROJECT_CREATED"); return pid
 def import_audio(self,pid,uri,duration_ms,sha256): self.db.execute("insert or replace into audio values(?,?,?,?)",(pid,uri,duration_ms,sha256)); self.event(pid,"AUDIO_IMPORTED")
 def import_srt(self,pid,cues):
  self.db.execute("delete from cues where project_id=?",(pid,))
  for i,(s,e,t) in enumerate(cues,1): self.db.execute("insert into cues(project_id,idx,start_ms,end_ms,text) values(?,?,?,?,?)",(pid,i,s,e,t))
  self.event(pid,"SRT_IMPORTED")
 def plan_scenes(self,pid,special_codes=()):
  audio=self.db.one("select * from audio where project_id=?",(pid,)); cues=self.db.all("select * from cues where project_id=? order by idx",(pid,))
  try: validate_timing(audio['duration_ms'] if audio else 0,[(x['start_ms'],x['end_ms'],x['text']) for x in cues])
  except Exception as e:
   self.db.execute("update projects set state='BLOCKED',blocked_reason=? where id=?",(str(e),pid)); self.event(pid,"TIMING_BLOCKED",{"error":str(e)}); raise
  self.db.execute("delete from scenes where project_id=?",(pid,))
  for i,c in enumerate(cues,1):
   code=f"S{i:03d}"; special=code in special_codes; gate=required_approval(code,special)
   self.db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state) values(?,?,?,?,?,?,?,?,?)",(pid,code,i,c['start_ms'],c['end_ms'],c['text'],special,"PLANNED","REQUIRED" if gate else "NOT_REQUIRED"))
  self.event(pid,"SCENES_PLANNED",{"count":len(cues)}); return [dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))]
 def scene(self,sid): return dict(self.db.one("select * from scenes where id=?",(sid,)))
 def record_image_attempt(self,sid,success,failed_binary=None,error=None,provider="codex-gpt-image-2"):
  scene=self.db.one("select * from scenes where id=?",(sid,)); n=self.db.one("select count(*) n from attempts where scene_id=?",(sid,))['n']+1
  if n>3: raise ValueError('maximum 3 attempts')
  if failed_binary and os.path.isfile(failed_binary): os.remove(failed_binary)
  state="SUCCEEDED" if success else "FAILED"; self.db.execute("insert into attempts(scene_id,number,provider,state,error,created_at) values(?,?,?,?,?,?)",(sid,n,provider,state,error,now()))
  scene_state="IMAGE_READY" if success else ("BLOCKED" if n==3 else "RETRYABLE")
  self.db.execute("update scenes set state=? where id=?",(scene_state,sid)); self.event(scene['project_id'],"IMAGE_ATTEMPT",{"scene":scene['code'],"number":n,"state":state})
 def add_artifact(self,pid,kind,uri,scene_id=None,parent_id=None):
  digest=hashlib.sha256(open(uri,'rb').read()).hexdigest(); version=self.db.one("select coalesce(max(version),0)+1 v from artifacts where project_id=? and kind=? and scene_id is ?",(pid,kind,scene_id))['v']; expires=(datetime.now(timezone.utc)+timedelta(days=3)).isoformat()
  return self.db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)",(pid,scene_id,kind,uri,digest,version,parent_id,"ACTIVE",now(),expires)).lastrowid
 def cleanup(self):
  rows=self.db.all("select * from artifacts where status='ACTIVE' and expires_at<?",(now(),)); count=0
  for r in rows:
   if os.path.isfile(r['uri']): os.remove(r['uri'])
   self.db.execute("update artifacts set status='DELETED',deleted_at=? where id=?",(now(),r['id'])); count+=1
  return count
 def observe_cost(self,pid,provider,currency,amount): self.db.execute("insert into cost_observations(project_id,provider,currency,amount,created_at) values(?,?,?,?,?)",(pid,provider,currency,amount,now()))
 def propose_dependency(self,pid,dependency,impact,proposer="system"): return self.db.execute("insert into impact_proposals(project_id,dependency,impact_json,state,proposer,created_at) values(?,?,?,?,?,?)",(pid,dependency,json.dumps(impact),"PROPOSED",proposer,now())).lastrowid
 def decide_dependency(self,i,approved,actor): self.db.execute("update impact_proposals set state=?,decider=?,decided_at=? where id=? and state='PROPOSED'",("APPROVED" if approved else "REJECTED",actor,now(),i))
 def apply_dependency(self,i):
  p=self.db.one("select * from impact_proposals where id=?",(i,))
  if not p or p['state']!='APPROVED': raise PermissionError('approval required')
  self.db.execute("update impact_proposals set state='APPLIED',applied_at=? where id=?",(now(),i)); self.event(p['project_id'],"DEPENDENCY_APPLIED",{"dependency":p['dependency']})
 def decide_scene(self,pid,code,decision,actor):
  s=self.db.one("select * from scenes where project_id=? and code=?",(pid,code));
  if not s: raise ValueError('scene not found')
  self.db.execute("update scenes set approval_state=? where id=?",(decision,s['id'])); self.db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at) values(?,?,?,?,?,?)",(pid,s['id'],"SCENE",decision,actor,now()))
 def pause(self,pid): self.db.execute("update projects set state='PAUSED' where id=?",(pid,)); self.event(pid,"PAUSED")
 def resume(self,pid): self.db.execute("update projects set state='ACTIVE' where id=?",(pid,)); self.event(pid,"RESUMED")
 def status(self,pid): return {"project":dict(self.db.one("select * from projects where id=?",(pid,))),"scenes":[dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))]}
 def report(self,pid): return {"costs":[dict(x) for x in self.db.all("select * from cost_observations where project_id=?",(pid,))],"errors":[dict(x) for x in self.db.all("select * from events where project_id=? and (type like '%BLOCKED' or data_json like '%error%')",(pid,))]}
