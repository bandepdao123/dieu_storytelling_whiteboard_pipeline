import hashlib,json,os,uuid,random
from datetime import datetime,timezone,timedelta
from .policies import validate_timing,required_approval,DurationPolicy
from .contracts import OutputConfig
from .adapters import Role,authorize,parse_discord

def now(): return datetime.now(timezone.utc).isoformat()
class Pipeline:
 def __init__(self,db,duration_policy=None): self.db=db; self.duration_policy=duration_policy or DurationPolicy()
 def _role(self,role,command):
  try:r=role if isinstance(role,Role) else Role(role)
  except Exception as e: raise PermissionError('valid role required') from e
  if not authorize(r,command): raise PermissionError(f'{r.value} cannot {command}')
 def event(self,pid,typ,data=None): self.db.execute("insert into events(project_id,type,data_json,created_at) values(?,?,?,?)",(pid,typ,json.dumps(data or {}),now()))
 def init_project(self,name,language="vi",seed=0,style=None,references=None,bible=None,image_provider="codex-gpt-image-2",whiteboard_mode="ask",transition="hard_cut",scene_range=(50,360),retention_days=3,output=None,role=Role.OWNER):
  self._role(role,'init')
  if language not in ('vi','en') or not name.strip(): raise ValueError('project input')
  lo,hi=scene_range
  if lo<1 or hi<lo or hi>360: raise ValueError('scene range')
  out=output or OutputConfig(transition=transition)
  pid=str(uuid.uuid4()); self.db.execute("insert into projects(id,name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,blocked_reason,created_at,min_scenes,max_scenes,retention_days,output_json,version) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,name,language,"ACTIVE",json.dumps(style or {}),json.dumps(references or []),json.dumps(bible or {}),image_provider,whiteboard_mode,int(seed),transition,None,now(),lo,hi,retention_days,json.dumps(out.__dict__),1)); self.event(pid,"PROJECT_CREATED",{'seed':seed}); return pid
 def import_audio(self,pid,uri,duration_ms,sha256,role=Role.OPERATOR): self._role(role,'import'); self.db.execute("insert or replace into audio values(?,?,?,?)",(pid,uri,duration_ms,sha256)); self.event(pid,"AUDIO_IMPORTED")
 def import_srt(self,pid,cues,role=Role.OPERATOR):
  self._role(role,'import'); self.db.execute("delete from cues where project_id=?",(pid,))
  for i,(s,e,t) in enumerate(cues,1): self.db.execute("insert into cues(project_id,idx,start_ms,end_ms,text) values(?,?,?,?,?)",(pid,i,s,e,t))
  self.event(pid,"SRT_IMPORTED")
 def plan_scenes(self,pid,special_codes=(),role=Role.OPERATOR):
  self._role(role,'plan'); project=self.db.one("select * from projects where id=?",(pid,)); audio=self.db.one("select * from audio where project_id=?",(pid,)); cues=self.db.all("select * from cues where project_id=? order by idx",(pid,))
  try: validate_timing(audio['duration_ms'] if audio else 0,[(x['start_ms'],x['end_ms'],x['text']) for x in cues])
  except Exception as e: self.db.execute("update projects set state='BLOCKED',blocked_reason=? where id=?",(str(e),pid)); self.event(pid,"TIMING_BLOCKED",{"error":str(e)}); raise
  # Semantic boundaries are cue boundaries, subdivided according to DurationPolicy.
  planned=[]
  for c in cues:
   cursor=c['start_ms']; length=c['end_ms']-cursor; target=self.duration_policy.choose(cursor/1000,length/1000)*1000
   pieces=max(1,round(length/target))
   for n in range(pieces):
    s=round(cursor+n*length/pieces); e=round(cursor+(n+1)*length/pieces); planned.append((s,e,c['text']))
  if not project['min_scenes']<=len(planned)<=project['max_scenes']: raise ValueError(f"scene count {len(planned)} outside configured {project['min_scenes']}-{project['max_scenes']}")
  self.db.execute("delete from scenes where project_id=?",(pid,))
  for i,(s,e,text) in enumerate(planned,1):
   code=f"S{i:03d}"; special=code in special_codes; gate=required_approval(code,special)
   self.db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state) values(?,?,?,?,?,?,?,?,?)",(pid,code,i,s,e,text,special,"PLANNED","REQUIRED" if gate else "NOT_REQUIRED"))
  self.event(pid,"SCENES_PLANNED",{"count":len(planned)}); self.checkpoint(pid,'plan',{'count':len(planned)}); return [dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))]
 def scene(self,sid): return dict(self.db.one("select * from scenes where id=?",(sid,)))
 def queue_retry(self,sid,role=Role.OPERATOR): self._role(role,'retry'); self.db.execute("update scenes set state='RETRY_QUEUED' where id=? and state in ('RETRYABLE','BLOCKED')",(sid,)); return self.scene(sid)
 def record_image_attempt(self,sid,success,failed_binary=None,error=None,provider="codex-gpt-image-2"):
  scene=self.db.one("select * from scenes where id=?",(sid,)); n=self.db.one("select count(*) n from attempts where scene_id=?",(sid,))['n']+1
  if n>3: raise ValueError('maximum 3 attempts')
  path=digest=size=None
  if failed_binary and os.path.isfile(failed_binary):
   path=os.path.abspath(failed_binary); data=open(path,'rb').read(); digest=hashlib.sha256(data).hexdigest(); size=len(data); os.remove(path)
  state="SUCCEEDED" if success else "FAILED"; self.db.execute("insert into attempts(scene_id,number,provider,state,error,failed_path,failed_sha256,failed_size,created_at) values(?,?,?,?,?,?,?,?,?)",(sid,n,provider,state,error,path,digest,size,now()))
  self.db.execute("update scenes set state=?,checkpoint_json=? where id=?",("IMAGE_READY" if success else ("BLOCKED" if n==3 else "RETRYABLE"),json.dumps({'attempt':n}),sid)); self.event(scene['project_id'],"IMAGE_ATTEMPT",{"scene":scene['code'],"number":n,"state":state})
 def _pilot_ready(self,pid): return not self.db.one("select 1 from scenes where project_id=? and approval_state='REQUIRED'",(pid,))
 def start_batch(self,pid,role=Role.OPERATOR):
  self._role(role,'batch')
  if not self._pilot_ready(pid): raise PermissionError('pilot S001-S005 and special representatives require approval')
  return self.create_job(pid,'BATCH_IMAGE')
 def approve_post_batch(self,pid,ai_qa,contact_sheet,actor,role=Role.REVIEWER):
  self._role(role,'approve')
  if not ai_qa or not contact_sheet: raise ValueError('AI QA and contact sheet required')
  self.db.execute("insert into approvals(project_id,gate,decision,actor,created_at) values(?,?,?,?,?)",(pid,'POST_BATCH','APPROVED',actor,now()))
 def start_animation(self,pid,role=Role.OPERATOR):
  self._role(role,'animate')
  if not self.db.one("select 1 from approvals where project_id=? and gate='POST_BATCH' and decision='APPROVED'",(pid,)): raise PermissionError('post-batch human approval required')
  return self.create_job(pid,'ANIMATION')
 def create_job(self,pid,kind):
  t=now(); return self.db.execute("insert into jobs(project_id,kind,state,created_at,updated_at) values(?,?,?,?,?)",(pid,kind,'QUEUED',t,t)).lastrowid
 def checkpoint(self,pid,stage,data):
  job=self.db.one("select * from jobs where project_id=? order by id desc",(pid,)); jid=job['id'] if job else self.create_job(pid,'PIPELINE')
  self.db.execute("insert into stages(job_id,name,state,checkpoint_json) values(?,?,?,?) on conflict(job_id,name) do update set state=excluded.state,checkpoint_json=excluded.checkpoint_json",(jid,stage,'SUCCEEDED',json.dumps(data)))
 def propose_rerun(self,pid,stage,role=Role.OPERATOR): self._role(role,'retry'); return self.db.execute("insert into proposals(project_id,stage,state,created_at) values(?,?,?,?)",(pid,stage,'PROPOSED',now())).lastrowid
 def decide_rerun(self,i,approved,actor,role=Role.OWNER): self._role(role,'approve'); self.db.execute("update proposals set state=?,actor=? where id=? and state='PROPOSED'",('APPROVED' if approved else 'REJECTED',actor,i))
 def apply_rerun(self,i):
  p=self.db.one('select * from proposals where id=?',(i,))
  if not p or p['state']!='APPROVED': raise PermissionError('rerun approval required')
  self.db.execute("update proposals set state='APPLIED' where id=?",(i,)); return self.create_job(p['project_id'],'RERUN:'+p['stage'])
 def add_artifact(self,pid,kind,uri,scene_id=None,parent_id=None):
  data=open(uri,'rb').read(); digest=hashlib.sha256(data).hexdigest(); version=self.db.one("select coalesce(max(version),0)+1 v from artifacts where project_id=? and kind=? and scene_id is ?",(pid,kind,scene_id))['v']; days=self.db.one('select retention_days from projects where id=?',(pid,))['retention_days']; expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
  return self.db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)",(pid,scene_id,kind,uri,digest,version,parent_id,"ACTIVE",now(),expires)).lastrowid
 def restore_artifact(self,artifact_id):
  a=self.db.one('select * from artifacts where id=?',(artifact_id,))
  if not a or a['status']=='DELETED' or not os.path.isfile(a['uri']): raise FileNotFoundError('artifact binary unavailable')
  self.db.execute('update projects set version=version+1,state=\'ACTIVE\' where id=?',(a['project_id'],)); self.event(a['project_id'],'ARTIFACT_RESTORED',{'artifact':artifact_id}); return dict(a)
 def cleanup(self):
  rows=self.db.all("select * from artifacts where status='ACTIVE' and expires_at<?",(now(),))
  for r in rows:
   if os.path.isfile(r['uri']): os.remove(r['uri'])
   self.db.execute("update artifacts set status='DELETED',deleted_at=? where id=?",(now(),r['id']))
  return len(rows)
 def cleanup_schedule(self): return {'task':'artifact_cleanup','interval_seconds':3600,'handler':self.cleanup}
 def decide_scene(self,pid,code,decision,actor,role=Role.REVIEWER):
  self._role(role,'approve' if decision=='APPROVED' else 'reject'); s=self.db.one("select * from scenes where project_id=? and code=?",(pid,code))
  if not s: raise ValueError('scene not found')
  if decision not in ('APPROVED','REJECTED'): raise ValueError('decision')
  self.db.execute("update scenes set approval_state=? where id=?",(decision,s['id'])); self.db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at) values(?,?,?,?,?,?)",(pid,s['id'],"SCENE",decision,actor,now()))
 def dispatch_discord(self,text,role):
  c=parse_discord(text); self._role(role,c.name)
  if c.name in ('approve','reject'): self.decide_scene(c.args[0],c.args[1],'APPROVED' if c.name=='approve' else 'REJECTED','discord',role)
  elif c.name=='retry': return self.queue_retry(int(c.args[0]),role)
  elif c.name=='pause': self.pause(c.args[0],role)
  elif c.name=='resume': self.resume(c.args[0],role)
  elif c.name=='status': return self.status(c.args[0],role)
  else: raise ValueError('unsupported command')
  return {'ok':True}
 def observe_cost(self,pid,provider,currency,amount): self.db.execute("insert into cost_observations(project_id,provider,currency,amount,created_at) values(?,?,?,?,?)",(pid,provider,currency,amount,now()))
 def propose_dependency(self,pid,dependency,impact,proposer="system"): return self.db.execute("insert into impact_proposals(project_id,dependency,impact_json,state,proposer,created_at) values(?,?,?,?,?,?)",(pid,dependency,json.dumps(impact),"PROPOSED",proposer,now())).lastrowid
 def decide_dependency(self,i,approved,actor): self.db.execute("update impact_proposals set state=?,decider=?,decided_at=? where id=? and state='PROPOSED'",("APPROVED" if approved else "REJECTED",actor,now(),i))
 def apply_dependency(self,i):
  p=self.db.one("select * from impact_proposals where id=?",(i,))
  if not p or p['state']!='APPROVED': raise PermissionError('approval required')
  self.db.execute("update impact_proposals set state='APPLIED',applied_at=? where id=?",(now(),i)); self.event(p['project_id'],"DEPENDENCY_APPLIED",{"dependency":p['dependency']})
 def pause(self,pid,role=Role.OPERATOR): self._role(role,'pause'); self.db.execute("update projects set state='PAUSED' where id=? and state='ACTIVE'",(pid,)); self.event(pid,"PAUSED")
 def resume(self,pid,role=Role.OPERATOR): self._role(role,'resume'); self.db.execute("update projects set state='ACTIVE' where id=? and state in ('PAUSED','BLOCKED')",(pid,)); self.event(pid,"RESUMED")
 def status(self,pid,role=Role.OPERATOR): self._role(role,'status'); return {"project":dict(self.db.one("select * from projects where id=?",(pid,))),"scenes":[dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))],"jobs":[dict(x) for x in self.db.all('select * from jobs where project_id=?',(pid,))]}
 def report(self,pid): return {"costs":[dict(x) for x in self.db.all("select * from cost_observations where project_id=?",(pid,))],"errors":[dict(x) for x in self.db.all("select * from events where project_id=? and (type like '%BLOCKED' or data_json like '%error%')",(pid,))]}
