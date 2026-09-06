import hashlib,json,os,uuid,random,shutil
from pathlib import Path
from datetime import datetime,timezone,timedelta
from .policies import validate_timing,required_approval,DurationPolicy
from .contracts import OutputConfig,ContactSheetArtifact,QAEvidence
from .policies import ResourceScheduler
from .adapters import Role,authorize,parse_discord,CommandError
from .inputs import normalize_document

def now(): return datetime.now(timezone.utc).isoformat()
class Pipeline:
 def __init__(self,db,duration_policy=None,scheduler=None): self.db=db; self.duration_policy=duration_policy or DurationPolicy(); self.scheduler=scheduler or ResourceScheduler()
 def _role(self,role,command):
  try:r=role if isinstance(role,Role) else Role(role)
  except Exception as e: raise PermissionError('valid role required') from e
  if not authorize(r,command): raise PermissionError(f'{r.value} cannot {command}')
 def _root(self,pid):
  p=self.db.one('select artifact_root from projects where id=?',(pid,))
  if not p: raise ValueError('project not found')
  root=Path(p['artifact_root'] or (self.db.path.parent/'artifacts'/pid)).resolve(); root.mkdir(parents=True,exist_ok=True); return root
 def _owned_path(self,pid,path,must_exist=True):
  root=self._root(pid); candidate=Path(path)
  if not candidate.is_absolute(): candidate=root/candidate
  candidate=candidate.resolve(strict=must_exist)
  try: candidate.relative_to(root)
  except ValueError as exc: raise PermissionError('path escapes managed artifact root') from exc
  return candidate
 def _hash(self,path):
  h=hashlib.sha256(); size=0
  with open(path,'rb') as stream:
   for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk); size+=len(chunk)
  return h.hexdigest(),size
 def _event(self,pid,typ,data=None): self.db.execute("insert into events(project_id,type,data_json,created_at) values(?,?,?,?)",(pid,typ,json.dumps(data or {}),now()))
 def event(self,*args,**kwargs):
  """Events are audit records and may only be emitted by service use-cases."""
  raise PermissionError('direct event insertion is not permitted')
 def init_project(self,name,language="vi",seed=0,style=None,references=None,bible=None,image_provider="codex-gpt-image-2",whiteboard_mode="ask",transition="hard_cut",scene_range=(50,360),retention_days=3,output=None,role=Role.OWNER):
  self._role(role,'init')
  if language not in ('vi','en') or not name.strip(): raise ValueError('project input')
  lo,hi=scene_range
  if lo<1 or hi<lo or hi>360: raise ValueError('scene range')
  out=output or OutputConfig(transition=transition)
  pid=str(uuid.uuid4()); root=(self.db.path.parent/'artifacts'/pid).resolve(); root.mkdir(parents=True,exist_ok=False)
  try:
   with self.db.transaction():
    self.db.execute("insert into projects(id,name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,blocked_reason,created_at,min_scenes,max_scenes,retention_days,output_json,version,artifact_root) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,name,language,"ACTIVE",json.dumps(style or {}),json.dumps(references or []),json.dumps(bible or {}),image_provider,whiteboard_mode,int(seed),transition,None,now(),lo,hi,retention_days,json.dumps(out.__dict__),1,str(root))); self._event(pid,"PROJECT_CREATED",{'seed':seed})
  except Exception: shutil.rmtree(root,ignore_errors=True); raise
  return pid
 def import_audio(self,pid,uri,duration_ms,sha256,role=Role.OPERATOR):
  self._role(role,'import'); self._active(pid); old=self.db.one('select * from audio where project_id=?',(pid,))
  if old and (old['uri'],old['duration_ms'],old['sha256'])==(uri,duration_ms,sha256):return
  with self.db.transaction():
   if old:self._invalidate(pid,'audio changed')
   self.db.execute("insert or replace into audio values(?,?,?,?)",(pid,uri,duration_ms,sha256)); self._event(pid,"AUDIO_IMPORTED")
 def _fence_evidence_mutation(self,pid,reason,scene_id=None,preserve_scene_approvals=False):
  """Fence work authorized by pre-mutation evidence; caller owns the transaction."""
  self.db.execute("update jobs set state='BLOCKED',updated_at=?,lease_owner=NULL,lease_expires_at=NULL where project_id=? and state in ('QUEUED','RUNNING','PAUSED')",(now(),pid))
  if preserve_scene_approvals:
   self.db.execute("update approvals set revoked_at=?,revoked_reason=? where project_id=? and gate!='SCENE' and revoked_at is null",(now(),reason,pid))
  elif scene_id is not None:
   self.db.execute("update approvals set revoked_at=?,revoked_reason=? where project_id=? and (gate!='SCENE' or scene_id=?) and revoked_at is null",(now(),reason,pid,scene_id))
   self.db.execute("update scenes set approval_state=case when special=1 or ord<=5 then 'REQUIRED' else 'NOT_REQUIRED' end where id=?",(scene_id,))
  else:
   self.db.execute("update approvals set revoked_at=?,revoked_reason=? where project_id=? and revoked_at is null",(now(),reason,pid))
   self.db.execute("update scenes set approval_state=case when special=1 or ord<=5 then 'REQUIRED' else 'NOT_REQUIRED' end where project_id=?",(pid,))
  self.db.execute('update projects set version=version+1 where id=?',(pid,))
  self._event(pid,'DOWNSTREAM_INVALIDATED',{'reason':reason})
 def _invalidate(self,pid,reason,clear_plan=True):
  self._fence_evidence_mutation(pid,reason)
  self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and status='ACTIVE'",(pid,))
  if clear_plan:
   self.db.execute("update artifacts set scene_id=NULL where project_id=? and scene_id is not null",(pid,))
   self.db.execute("update approvals set scene_id=NULL where project_id=? and scene_id is not null",(pid,))
   self.db.execute("delete from scenes where project_id=?",(pid,))
 def import_srt(self,pid,cues,role=Role.OPERATOR):
  self._role(role,'import'); self._active(pid)
  rows=list(cues)
  # Validate the complete replacement before touching persisted cues.
  audio=self.db.one('select duration_ms from audio where project_id=?',(pid,))
  validate_timing(audio['duration_ms'] if audio else 0,rows)
  old=[(x['start_ms'],x['end_ms'],x['text']) for x in self.db.all('select * from cues where project_id=? order by idx',(pid,))]
  if old==rows:return
  with self.db.transaction():
   if old:self._invalidate(pid,'srt changed')
   self.db.execute("delete from cues where project_id=?",(pid,))
   self.db.conn.executemany("insert into cues(project_id,idx,start_ms,end_ms,text) values(?,?,?,?,?)",[(pid,i,s,e,t) for i,(s,e,t) in enumerate(rows,1)])
   self._event(pid,"SRT_IMPORTED")
 def import_document(self,pid,kind,source,role=Role.OPERATOR,remote=None):
  self._role(role,'import'); self._active(pid); normalized=normalize_document(kind,source,remote)
  canonical=json.dumps({'kind':kind,'document':normalized},sort_keys=True,separators=(',',':'))
  old=self.db.one("select value_json from learning_metadata where project_id=? and key='normalized_input' order by id desc limit 1",(pid,))
  if old and old['value_json']==canonical:return normalized
  with self.db.transaction():
   if old:self._invalidate(pid,'document changed')
   self.db.execute("insert into learning_metadata(project_id,key,value_json,created_at) values(?,?,?,?)",(pid,'normalized_input',canonical,now()))
   self._event(pid,'SCRIPT_SOURCE_IMPORTED',{'kind':kind})
  return normalized
 def plan_scenes(self,pid,special_codes=(),role=Role.OPERATOR):
  self._role(role,'plan'); self._active(pid); project=self.db.one("select * from projects where id=?",(pid,)); audio=self.db.one("select * from audio where project_id=?",(pid,)); cues=self.db.all("select * from cues where project_id=? order by idx",(pid,))
  try: validate_timing(audio['duration_ms'] if audio else 0,[(x['start_ms'],x['end_ms'],x['text']) for x in cues])
  except Exception as e: self.db.execute("update projects set state='BLOCKED',blocked_reason=? where id=?",(str(e),pid)); self._event(pid,"TIMING_BLOCKED",{"error":str(e)}); raise
  # Semantic boundaries are cue boundaries, subdivided according to DurationPolicy.
  planned=[]
  for c in cues:
   # 180s changes policy.  Never let a generated scene straddle that boundary.
   units=[(c['start_ms'],c['end_ms'])]
   if c['start_ms']<180000<c['end_ms']: units=[(c['start_ms'],180000),(180000,c['end_ms'])]
   for us,ue in units:
    length=ue-us; lo,hi=self.duration_policy.bounds_at(us/1000); target=self.duration_policy.choose(us/1000,length/1000)*1000
    pieces=max(1,round(length/target))
    for n in range(pieces):
     s=round(us+n*length/pieces); e=round(us+(n+1)*length/pieces)
     exc=None if lo*1000<=e-s<=hi*1000 else json.dumps({'reason':'unavoidable_semantic_unit','actual_ms':e-s,'target_min_ms':lo*1000,'target_max_ms':hi*1000})
     planned.append((s,e,c['text'],exc))
  if not project['min_scenes']<=len(planned)<=project['max_scenes']: raise ValueError(f"scene count {len(planned)} outside configured {project['min_scenes']}-{project['max_scenes']}")
  existing=self.db.all("select start_ms,end_ms,text,duration_exception,special from scenes where project_id=? order by ord",(pid,))
  with self.db.transaction():
   # Replanning is a canonical-input mutation: fence old-version work and lineage.
   if existing:self._invalidate(pid,'scene plan changed')
   self.db.execute("delete from scenes where project_id=?",(pid,))
   for i,(s,e,text,exc) in enumerate(planned,1):
    code=f"S{i:03d}"; special=code in special_codes; gate=required_approval(code,special)
    self.db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state,duration_exception) values(?,?,?,?,?,?,?,?,?,?)",(pid,code,i,s,e,text,special,"PLANNED","REQUIRED" if gate else "NOT_REQUIRED",exc))
   self._event(pid,"SCENES_PLANNED",{"count":len(planned)}); self.checkpoint(pid,'plan',{'count':len(planned)},role)
  return [dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))]
 def scene(self,sid): return dict(self.db.one("select * from scenes where id=?",(sid,)))
 def queue_retry(self,sid,role=Role.OPERATOR):
  self._role(role,'retry'); s=self.db.one('select * from scenes where id=?',(sid,)); self._active(s['project_id'])
  if self.db.one('select count(*) n from attempts where scene_id=?',(sid,))['n']>=3: raise ValueError('maximum 3 attempts')
  with self.db.transaction():
   if self.db.execute("update scenes set state='RETRY_QUEUED' where id=? and state in ('RETRYABLE','BLOCKED')",(sid,)).rowcount!=1: raise PermissionError('illegal retry transition')
   self._create_job(s['project_id'],f'RETRY_IMAGE:{sid}')
  return self.scene(sid)
 def record_image_attempt(self,sid,success,failed_binary=None,error=None,provider="codex-gpt-image-2",role=Role.OPERATOR):
  self._role(role,'retry')
  scene=self.db.one("select * from scenes where id=?",(sid,))
  if not scene: raise ValueError('scene not found')
  self._active(scene['project_id'])
  n=self.db.one("select count(*) n from attempts where scene_id=?",(sid,))['n']+1
  if n>3: raise ValueError('maximum 3 attempts')
  if scene['state'] not in ('PLANNED','RETRY_QUEUED','RETRYABLE'): raise PermissionError('illegal image attempt transition')
  if scene['ord']>5 and not scene['special'] and not self._pilot_ready(scene['project_id']): raise PermissionError('pilot approval required before batch images')
  path=digest=size=None
  if failed_binary and os.path.isfile(failed_binary):
   try: owned=self._owned_path(scene['project_id'],failed_binary)
   except PermissionError: owned=None
   if owned: path=str(owned); digest,size=self._hash(owned)
  state="SUCCEEDED" if success else "FAILED"
  with self.db.transaction():
   self.db.execute("insert into attempts(scene_id,number,provider,state,error,failed_path,failed_sha256,failed_size,created_at) values(?,?,?,?,?,?,?,?,?)",(sid,n,provider,state,error,path,digest,size,now()))
   self.db.execute("update scenes set state=?,checkpoint_json=? where id=?",("IMAGE_READY" if success else ("BLOCKED" if n==3 else "RETRYABLE"),json.dumps({'attempt':n}),sid)); self._event(scene['project_id'],"IMAGE_ATTEMPT",{"scene":scene['code'],"number":n,"state":state})
  if path: Path(path).unlink(missing_ok=True)
 def record_scene_qa(self,sid,evidence,role=Role.REVIEWER):
  self._role(role,'approve')
  if not isinstance(evidence,QAEvidence): raise TypeError('QAEvidence required')
  s=self.db.one('select * from scenes where id=?',(sid,)); self._active(s['project_id'])
  if s['state']!='IMAGE_READY': raise PermissionError('QA requires ready image')
  payload={'checks':dict(evidence.checks),'score':evidence.score,'evaluator':evidence.evaluator}
  with self.db.transaction():
   self._fence_evidence_mutation(s['project_id'],'QA evidence changed',scene_id=sid)
   self.db.execute("update scenes set qa_state=?,qa_json=? where id=?",('PASS' if evidence.passed else 'FAIL',json.dumps(payload),sid))
 def _planned_ready(self,pid):
  """Require persisted, internally valid source timing and a complete scene plan."""
  project=self.db.one('select * from projects where id=?',(pid,)); audio=self.db.one('select * from audio where project_id=?',(pid,)); cues=self.db.all('select * from cues where project_id=? order by idx',(pid,)); scenes=self.db.all('select * from scenes where project_id=? order by ord',(pid,))
  if not project or not audio or not scenes: return False
  try: validate_timing(audio['duration_ms'],[(x['start_ms'],x['end_ms'],x['text']) for x in cues])
  except Exception: return False
  return project['min_scenes']<=len(scenes)<=project['max_scenes'] and [x['ord'] for x in scenes]==list(range(1,len(scenes)+1))
 def _scene_evidence_hash(self,sid):
  s=dict(self.db.one('select id,project_id,code,ord,start_ms,end_ms,text,special,state,qa_state,qa_json,duration_exception,continuity_json,checkpoint_json from scenes where id=?',(sid,)))
  artifacts=[dict(x) for x in self.db.all("select id,kind,sha256,version,parent_id,status from artifacts where scene_id=? and status='ACTIVE' order by id",(sid,))]
  return hashlib.sha256(json.dumps({'scene':s,'artifacts':artifacts},sort_keys=True,separators=(',',':')).encode()).hexdigest()
 def _pilot_ready(self,pid):
  if not self._planned_ready(pid): return False
  for s in self.db.all("select * from scenes where project_id=? and (ord<=5 or special=1)",(pid,)):
   if s['state']!='IMAGE_READY' or s['qa_state']!='PASS' or s['approval_state']!='APPROVED': return False
   if not self.db.one("select 1 from approvals where project_id=? and scene_id=? and gate='SCENE' and decision='APPROVED' and revoked_at is null and evidence_sha256=?",(pid,s['id'],self._scene_evidence_hash(s['id']))): return False
  return True
 def start_batch(self,pid,role=Role.OPERATOR):
  self._role(role,'batch')
  if not self._pilot_ready(pid): raise PermissionError('pilot S001-S005 and special representatives require approval')
  return self._create_job(pid,'BATCH_IMAGE')
 def approve_post_batch(self,pid,ai_qa,contact_sheet,actor,role=Role.REVIEWER):
  self._role(role,'approve')
  if not self._planned_ready(pid): raise PermissionError('valid audio/SRT and at least one planned scene required')
  if not isinstance(ai_qa,QAEvidence) or not ai_qa.passed or not contact_sheet or not os.path.isfile(contact_sheet): raise ValueError('typed AI QA evidence and persisted contact sheet required')
  if self.db.one("select 1 from scenes where project_id=? and (state!='IMAGE_READY' or qa_state!='PASS')",(pid,)): raise PermissionError('complete batch and every scene QA PASS required')
  with self.db.transaction():
   # Gate authorization above permits the trusted workflow to ingest its evidence.
   aid=self.add_artifact(pid,'CONTACT_SHEET',contact_sheet,role=Role.OWNER)
   version=self.db.one('select version from projects where id=?',(pid,))['version']; evidence=self._manifest_hash(pid)
   self.db.execute("insert into approvals(project_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?)",(pid,'POST_BATCH','APPROVED',actor,now(),version,evidence))
  a=self.db.one('select * from artifacts where id=?',(aid,)); return ContactSheetArtifact(a['id'],a['project_id'],a['uri'],a['sha256'],a['version'],a['status'])
 def _manifest_hash(self,pid):
  project=dict(self.db.one('select language,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,min_scenes,max_scenes,output_json,version from projects where id=?',(pid,)))
  scenes=[dict(x) for x in self.db.all('select id,code,ord,start_ms,end_ms,text,special,state,approval_state,qa_state,qa_json,duration_exception,continuity_json,checkpoint_json from scenes where project_id=? order by ord',(pid,))]
  artifacts=[dict(x) for x in self.db.all('select id,scene_id,kind,sha256,version,parent_id,status from artifacts where project_id=? order by id',(pid,))]
  return hashlib.sha256(json.dumps({'project':project,'scenes':scenes,'artifacts':artifacts},sort_keys=True,separators=(',',':')).encode()).hexdigest()
 def start_animation(self,pid,role=Role.OPERATOR):
  self._role(role,'animate')
  if not self._planned_ready(pid): raise PermissionError('valid audio/SRT and at least one planned scene required')
  version=self.db.one('select version from projects where id=?',(pid,))['version']
  if not self.db.one("select 1 from approvals where project_id=? and gate='POST_BATCH' and decision='APPROVED' and revoked_at is null and project_version=? and evidence_sha256=?",(pid,version,self._manifest_hash(pid))): raise PermissionError('current post-batch human approval required')
  return self._create_job(pid,'ANIMATION')
 def _active(self,pid):
  p=self.db.one('select state from projects where id=?',(pid,))
  if not p or p['state']!='ACTIVE': raise PermissionError('project is not active')
 def _create_job(self,pid,kind):
  self._active(pid); t=now(); version=self.db.one('select version from projects where id=?',(pid,))['version']
  with self.db.transaction():
   old=self.db.one("select id from jobs where project_id=? and kind=? and project_version=? and state in ('QUEUED','RUNNING','PAUSED')",(pid,kind,version))
   if old:return old['id']
   return self.db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",(pid,kind,'QUEUED',t,t,version)).lastrowid
 def create_job(self,*args,**kwargs):
  raise PermissionError('jobs must be created through a gated workflow')
 def pause_job(self,jid,role=Role.OPERATOR):
  self._role(role,'pause')
  if self.db.execute("update jobs set state='PAUSED',updated_at=? where id=? and state in ('QUEUED','RUNNING')",(now(),jid)).rowcount!=1: raise PermissionError('illegal job pause transition')
 def resume_job(self,jid,role=Role.OPERATOR):
  self._role(role,'resume')
  if self.db.execute("update jobs set state='QUEUED',updated_at=? where id=? and state='PAUSED' and project_version=(select version from projects where id=jobs.project_id) and exists(select 1 from projects where id=jobs.project_id and state='ACTIVE')",(now(),jid)).rowcount!=1: raise PermissionError('illegal job resume transition')
 def execute_images(self,scene_ids,outcomes):
  """Deterministic local executor; one scene failure never aborts siblings."""
  pending=iter(scene_ids); exhausted=False
  while not exhausted or self.scheduler.queued:
   while not exhausted and len(self.scheduler.queued)<self.scheduler.config.queue_size:
    try: self.scheduler.submit('image',next(pending))
    except StopIteration: exhausted=True
   admitted=self.scheduler.acquire()
   if not admitted:
    if exhausted: break
    continue
   kind,sid=admitted
   try:
    sequence=iter(outcomes.get(sid,(True,)))
    while self.db.one('select count(*) n from attempts where scene_id=?',(sid,))['n']<3:
     try: success=bool(next(sequence))
     except StopIteration: break
     self.record_image_attempt(sid,success,error=None if success else 'fake failure')
     if success: break
   finally: self.scheduler.release(kind)
  return [self.scene(s) for s in scene_ids]
 def checkpoint(self,pid,stage,data,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  with self.db.transaction():
   version=self.db.one('select version from projects where id=?',(pid,))['version']
   job=self.db.one("select * from jobs where project_id=? and project_version=? and state in ('QUEUED','RUNNING','PAUSED') order by id desc",(pid,version))
   jid=job['id'] if job else self._create_job(pid,'PIPELINE')
   payload={'snapshot_version':3,'data':data,'project':dict(self.db.one('select * from projects where id=?',(pid,))),'job':dict(self.db.one('select * from jobs where id=?',(jid,))),'scenes':[dict(x) for x in self.db.all('select * from scenes where project_id=? order by ord',(pid,))],'artifacts':[dict(x) for x in self.db.all('select * from artifacts where project_id=? order by id',(pid,))]}
   snapshot={**data,**payload,'snapshot_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
   self.db.execute("insert into stages(job_id,name,state,checkpoint_json) values(?,?,?,?) on conflict(job_id,name) do update set state=excluded.state,checkpoint_json=excluded.checkpoint_json",(jid,stage,'SUCCEEDED',json.dumps(snapshot)))
 def propose_rerun(self,pid,stage,role=Role.OPERATOR): self._role(role,'retry'); return self.db.execute("insert into proposals(project_id,stage,state,created_at) values(?,?,?,?)",(pid,stage,'PROPOSED',now())).lastrowid
 def decide_rerun(self,i,approved,actor,role=Role.OWNER):
  self._role(role,'approve')
  if self.db.execute("update proposals set state=?,actor=? where id=? and state='PROPOSED'",('APPROVED' if approved else 'REJECTED',actor,i)).rowcount!=1: raise PermissionError('illegal rerun decision')
 def apply_rerun(self,i,role=Role.OWNER):
  self._role(role,'dependency')
  p=self.db.one('select * from proposals where id=?',(i,))
  if not p or p['state']!='APPROVED': raise PermissionError('rerun approval required')
  order=['planning','image','qa','animation','assembly','upload']; stage={'plan':'planning'}.get(p['stage'],p['stage'])
  if stage not in order: raise ValueError('unknown stage')
  downstream=order[order.index(stage):]
  with self.db.transaction():
   self.db.execute("update jobs set state='BLOCKED',updated_at=? where project_id=? and state in ('QUEUED','RUNNING','PAUSED')",(now(),p['project_id']))
   arts=self.db.all("select * from artifacts where project_id=? and status='ACTIVE'",(p['project_id'],))
   for a in arts:
    if a['kind'].lower() in downstream:self.db.execute("update artifacts set status='SUPERSEDED' where id=?",(a['id'],))
   self.db.execute("update projects set version=version+1 where id=?",(p['project_id'],))
   if self.db.execute("update proposals set state='APPLIED' where id=? and state='APPROVED'",(i,)).rowcount!=1: raise PermissionError('rerun already applied')
   return self._create_job(p['project_id'],'RERUN:'+stage)
 def add_artifact(self,pid,kind,uri,scene_id=None,parent_id=None,role=Role.OPERATOR,_new_copies=None):
  self._role(role,'scene-replace'); source=Path(uri).resolve(strict=True); root=self._root(pid)
  # Ingest into owned storage; callers' arbitrary source URI is never later deleted.
  copied=False
  try: source.relative_to(root); managed=source
  except ValueError: managed=None
  # New registrations get exclusive ownership. Legacy shared URIs remain safe.
  if managed is None or self.db.one("select 1 from artifacts where project_id=? and uri=? limit 1",(pid,str(managed))):
   managed=root/(uuid.uuid4().hex+source.suffix); shutil.copyfile(source,managed); copied=True
   if _new_copies is not None: _new_copies.append(managed)
  try:
   digest,_=self._hash(managed); version=self.db.one("select coalesce(max(version),0)+1 v from artifacts where project_id=? and kind=? and scene_id is ?",(pid,kind,scene_id))['v']; days=self.db.one('select retention_days from projects where id=?',(pid,))['retention_days']; expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
   with self.db.transaction():
    aid=self.db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)",(pid,scene_id,kind,str(managed),digest,version,parent_id,"ACTIVE",now(),expires)).lastrowid
    self._event(pid,'ARTIFACT_ADDED',{'artifact':aid,'kind':kind})
   return aid
  except Exception:
   if copied: managed.unlink(missing_ok=True)
   raise
 def restore_artifact(self,artifact_id,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  a=self.db.one('select * from artifacts where id=?',(artifact_id,))
  if not a or a['status']=='DELETED' or not os.path.isfile(a['uri']): raise FileNotFoundError('artifact binary unavailable')
  with self.db.transaction():
   self._invalidate(a['project_id'],'artifact restored',clear_plan=False)
   # Artifact restoration is content restoration, never a lifecycle transition.
   self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and kind=? and scene_id is ? and status='ACTIVE'",(a['project_id'],a['kind'],a['scene_id']))
   self.db.execute("update artifacts set status='ACTIVE',deleted_at=NULL where id=?",(artifact_id,))
   self._event(a['project_id'],'ARTIFACT_RESTORED',{'artifact':artifact_id})
  return dict(self.db.one('select * from artifacts where id=?',(artifact_id,)))
 def cleanup(self,role=Role.OWNER,_lease_token=None):
  self._role(role,'cleanup')
  owned=_lease_token is None
  token=_lease_token or self.db.acquire_reconciliation_lease()
  try:return self._cleanup(token)
  finally:
   if owned:self.db.release_reconciliation_lease(token)
 def _cleanup(self,token):
  immutable=('INPUT','MANIFEST','DB','PROMPT','BIBLE','QA_METADATA','CHECKSUM','FINAL')
  rows=self.db.all("select * from artifacts where status in ('ACTIVE','SUPERSEDED') and expires_at<? and upper(kind) not in (%s)" % ','.join('?'*len(immutable)),(now(),*immutable))
  count=0
  for r in rows:
   try: path=self._owned_path(r['project_id'],r['uri'],must_exist=False)
   except (PermissionError,OSError): continue
   staged=path.with_name(path.name+'.deleting-'+uuid.uuid4().hex)
   moved=False
   try:
    # BEGIN IMMEDIATE serializes the row transition, shared-reference decision,
    # and rename.  A waiter therefore observes either the live reference or the
    # committed DELETED transition, never the old decision with the new state.
    with self.db.transaction():
     self.db.assert_reconciliation_lease(token)
     changed=self.db.execute("update artifacts set status='DELETED',deleted_at=? where id=? and status=?",(now(),r['id'],r['status'])).rowcount
     if not changed: continue # another cleanup connection already retired it
     live=self.db.one("select 1 from artifacts where project_id=? and uri=? and status!='DELETED' limit 1",(r['project_id'],r['uri']))
     if not live and path.is_file() and not path.is_symlink():
      path.replace(staged); moved=True
   except Exception:
    # A rename followed by rollback represents a live DB reference again.
    if moved and staged.is_file() and not staged.is_symlink() and not path.exists(): staged.replace(path)
    raise
   if moved: staged.unlink(missing_ok=True)
   count+=1
  return count
 def reconcile_deletions(self,_lease_token=None):
  """Resolve rename/commit/unlink crash windows with DB status as authority."""
  owned=_lease_token is None
  token=_lease_token or self.db.acquire_reconciliation_lease()
  try:
   return self._reconcile_deletions(token)
  finally:
   if owned:self.db.release_reconciliation_lease(token)
 def _reconcile_deletions(self,token):
  """Implementation fenced by the SQLite lease before every filesystem write."""
  for project in self.db.all("select id,artifact_root from projects where artifact_root is not null"):
   root=Path(project['artifact_root']).resolve()
   if not root.is_dir(): continue
   groups={}
   # Walk recursively without following directory symlinks, then handle all
   # copies for one original together in stable pathname order.
   for directory,dirs,files in os.walk(root,followlinks=False):
    dirs[:]=[d for d in dirs if not (Path(directory)/d).is_symlink()]
    for filename in files:
     if '.deleting-' not in filename: continue
     staged=Path(directory)/filename
     if staged.is_symlink() or not staged.is_file(): continue
     original=staged.with_name(filename.rsplit('.deleting-',1)[0])
     try:
      staged.resolve().relative_to(root)
      original.resolve(strict=False).relative_to(root)
     except (ValueError,OSError): continue
     if original.is_symlink() or (original.exists() and not original.is_file()): continue
     groups.setdefault(original,[]).append(staged)
   for original,staged_files in sorted(groups.items(),key=lambda item:str(item[0])):
    self.db.renew_reconciliation_lease(token)
    rows=self.db.all('select status,sha256 from artifacts where project_id=? and uri=? order by id',(project['id'],str(original)))
    if not rows: continue
    staged_files=sorted(staged_files,key=lambda p:p.name)
    live=[row for row in rows if row['status']!='DELETED']
    if not live:
     for staged in staged_files:
      with self.db.transaction():
       self.db.assert_reconciliation_lease(token); staged.unlink(missing_ok=True)
     continue
    live_hashes={row['sha256'] for row in live}
    # Only a byte-for-byte tracked live payload may be restored or discarded.
    # Unknown/mismatched files are deliberately left for operator inspection.
    original_hash=None
    if original.is_file() and not original.is_symlink():
     try: original_hash=self._hash(original)[0]
     except OSError: pass
    candidates=[]
    for staged in staged_files:
     try: digest=self._hash(staged)[0]
     except OSError: continue
     if digest in live_hashes: candidates.append((staged,digest))
    if original_hash not in live_hashes and not original.exists() and candidates:
     chosen,original_hash=candidates[0]
     with self.db.transaction():
      self.db.assert_reconciliation_lease(token); chosen.replace(original)
    if original_hash in live_hashes:
     for staged,digest in candidates:
      if digest==original_hash and staged.exists():
       with self.db.transaction():
        self.db.assert_reconciliation_lease(token); staged.unlink(missing_ok=True)
 def scheduled_cleanup(self,role=Role.OWNER):
  """Scheduled recovery and retention pass; safe to invoke repeatedly."""
  token=self.db.acquire_reconciliation_lease()
  try:
   self.reconcile_deletions(_lease_token=token)
   return self.cleanup(role,_lease_token=token)
  finally:self.db.release_reconciliation_lease(token)
 def cleanup_schedule(self): return {'task':'artifact_cleanup','interval_seconds':3600,'handler':self.scheduled_cleanup}
 def decide_scene(self,pid,code,decision,actor,role=Role.REVIEWER):
  self._role(role,'approve' if decision=='APPROVED' else 'reject'); s=self.db.one("select * from scenes where project_id=? and code=?",(pid,code))
  if not s: raise ValueError('scene not found')
  if decision not in ('APPROVED','REJECTED'): raise ValueError('decision')
  with self.db.transaction():
   self._fence_evidence_mutation(pid,'scene approval changed',preserve_scene_approvals=True)
   self.db.execute("update scenes set approval_state=? where id=?",(decision,s['id'])); evidence=self._scene_evidence_hash(s['id']); version=self.db.one('select version from projects where id=?',(pid,))['version']; self.db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?,?)",(pid,s['id'],"SCENE",decision,actor,now(),version,evidence))
 def cancel_project(self,pid,role=Role.OWNER):
  self._role(role,'project-cancel')
  with self.db.transaction():
   if self.db.execute("update projects set state='COMPLETED' where id=? and state!='COMPLETED'",(pid,)).rowcount!=1: raise PermissionError('project cannot be cancelled')
   self.db.execute("update jobs set state='BLOCKED',updated_at=?,lease_owner=NULL,lease_expires_at=NULL where project_id=? and state in ('QUEUED','RUNNING','PAUSED')",(now(),pid))
 def select_provider(self,pid,provider,role=Role.OPERATOR):
  self._role(role,'provider-select'); self._active(pid)
  if not provider.strip(): raise ValueError('provider required')
  if self.db.one('select image_provider from projects where id=?',(pid,))['image_provider']==provider:return
  with self.db.transaction():
   self._invalidate(pid,'image provider changed',clear_plan=False)
   self.db.execute('update projects set image_provider=? where id=?',(provider,pid))
 def select_preset(self,pid,preset,role=Role.OPERATOR):
  self._role(role,'preset-select'); self._active(pid); presets={'youtube':OutputConfig(),'presentation':OutputConfig(fps=30)}
  if preset not in presets: raise ValueError('unknown preset')
  # Preset identity is canonical evidence even when two presets currently resolve
  # to the same encoding values.
  value=json.dumps({'preset':preset,'config':presets[preset].__dict__},sort_keys=True); old=self.db.one('select output_json from projects where id=?',(pid,))['output_json']
  if json.loads(old)==json.loads(value):return
  with self.db.transaction():
   self._invalidate(pid,'output preset changed',clear_plan=False)
   self.db.execute('update projects set output_json=? where id=?',(value,pid))
 def configure_project(self,pid,key,value,role=Role.OWNER):
  self._role(role,'project-config'); self._active(pid); columns={'whiteboard_mode','transition','language'}
  if key not in columns: raise ValueError('unsupported configuration key')
  if self.db.one(f'select {key} from projects where id=?',(pid,))[key]==value:return
  with self.db.transaction():
   self._invalidate(pid,f'project configuration changed: {key}',clear_plan=key=='language')
   self.db.execute(f'update projects set {key}=? where id=?',(value,pid))
 def decide_gate(self,pid,gate,decision,actor,role=Role.REVIEWER):
  command='approve' if decision=='APPROVED' else 'reject'; self._role(role,command)
  if gate not in {'PILOT','BATCH','FINAL'} or decision not in {'APPROVED','REJECTED'}: raise ValueError('unsupported gate decision')
  self.db.execute("insert into approvals(project_id,gate,decision,actor,created_at) values(?,?,?,?,?)",(pid,gate,decision,actor,now()))
 def replace_scene_artifact(self,sid,uri,role=Role.OPERATOR):
  self._role(role,'scene-replace'); s=self.scene(int(sid)); self._active(s['project_id']); new_copies=[]
  try:
   with self.db.transaction():
    self._fence_evidence_mutation(s['project_id'],'scene artifact replaced',scene_id=s['id'])
    old=self.db.one("select * from artifacts where scene_id=? and kind='IMAGE' and status='ACTIVE' order by version desc",(s['id'],))
    if old:self.db.execute("update artifacts set status='SUPERSEDED' where id=?",(old['id'],))
    aid=self.add_artifact(s['project_id'],'IMAGE',uri,s['id'],old['id'] if old else None,role,_new_copies=new_copies)
    self._event(s['project_id'],'SCENE_ARTIFACT_REPLACED',{'scene':s['code'],'artifact':aid})
  except Exception:
   # Only compensate files created by this invocation. Re-check both ownership and
   # references after rollback so a pre-existing/shared or out-of-root path is safe.
   for copied in new_copies:
    try:
     managed=self._owned_path(s['project_id'],copied,must_exist=False)
     if not self.db.one('select 1 from artifacts where uri=?',(str(managed),)):
      managed.unlink(missing_ok=True)
    except (OSError,PermissionError):
     pass
   raise
  return aid
 def dispatch_discord(self,text,role,idempotency_key=None):
  """Dispatch a command, optionally behind an atomic canonical-effect receipt.

  Transport delivery/response is at-least-once.  When supplied, the stable key
  makes all core mutations and their result exactly-once; reuse for a different
  request fails closed.
  """
  if idempotency_key is not None:
   if not isinstance(idempotency_key,str) or not idempotency_key.strip(): raise ValueError('idempotency key required')
   role_value=role.value if isinstance(role,Role) else str(role)
   request_sha=hashlib.sha256(json.dumps({'text':text,'role':role_value},sort_keys=True,separators=(',',':')).encode()).hexdigest()
   with self.db.transaction():
    receipt=self.db.one('select request_sha256,response_json from command_receipts where idempotency_key=?',(idempotency_key,))
    if receipt:
     if receipt['request_sha256']!=request_sha: raise PermissionError('idempotency key reused for different request')
     return json.loads(receipt['response_json'])
    result=self.dispatch_discord(text,role)
    self.db.execute('insert into command_receipts(idempotency_key,request_sha256,response_json,created_at) values(?,?,?,?)',(idempotency_key,request_sha,json.dumps(result),now()))
    return result
  c=parse_discord(text); self._role(role,c.name)
  legacy={'approve':'scene-approve','reject':'scene-reject','pause':'project-pause','resume':'project-resume','retry':'scene-retry','status':'project-status'}
  c=type(c)(legacy.get(c.name,c.name),c.args)
  schemas={'project-create':(1,2),'project-status':(1,1),'project-config':(3,3),'project-start':(1,1),'project-pause':(1,1),'project-resume':(1,1),'project-cancel':(1,1),'import':(2,2),'plan':(1,1),'pilot-approve':(1,1),'pilot-reject':(1,1),'batch-approve':(1,1),'batch-reject':(1,1),'scene-status':(2,2),'scene-retry':(1,1),'scene-approve':(2,2),'scene-reject':(2,2),'scene-replace':(2,2),'stage-run':(2,2),'stage-rerun':(2,2),'checkpoint-list':(1,1),'checkpoint-restore':(2,2),'dependency-propose':(3,3),'dependency-approve':(1,1),'dependency-apply':(1,1),'provider-select':(2,2),'preset-select':(2,2),'cost-report':(1,1),'error-report':(1,1),'final-approve':(1,1)}
  if c.name not in schemas: raise CommandError('UNKNOWN_COMMAND',c.name)
  lo,hi=schemas[c.name]
  if not lo<=len(c.args)<=hi: raise CommandError('INVALID_ARGUMENTS',f'{c.name} expects {lo}..{hi} arguments')
  a=c.args
  try:
   if c.name=='project-create': return {'project_id':self.init_project(a[0],a[1] if len(a)>1 else 'vi',role=role)}
   if c.name in ('project-status','scene-status'): return self.status(a[0],role) if c.name=='project-status' else dict(self.db.one('select * from scenes where project_id=? and code=?',a))
   if c.name=='project-pause': self.pause(a[0],role)
   elif c.name in ('project-start','project-resume'): self.resume(a[0],role)
   elif c.name=='project-cancel': self.cancel_project(a[0],role)
   elif c.name=='import': return {'normalized':self.import_document(a[0],'text',a[1],role)}
   elif c.name=='plan': return self.plan_scenes(a[0],role=role)
   elif c.name=='scene-retry': return self.queue_retry(int(a[0]),role)
   elif c.name in ('scene-approve','scene-reject'): self.decide_scene(a[0],a[1],'APPROVED' if c.name.endswith('approve') else 'REJECTED','discord',role)
   elif c.name=='stage-run': return {'job_id':self.run_stage(a[0],a[1],role)}
   elif c.name=='stage-rerun': return {'proposal_id':self.propose_rerun(a[0],a[1],role)}
   elif c.name=='checkpoint-list': return [dict(x) for x in self.db.all('select stages.* from stages join jobs on jobs.id=stages.job_id where jobs.project_id=? order by stages.id',a)]
   elif c.name=='checkpoint-restore': return self.restore_latest_checkpoint(a[0],a[1],role)
   elif c.name=='dependency-propose': return {'proposal_id':self.propose_dependency(a[0],a[1],{'impact':a[2]},role=role)}
   elif c.name=='dependency-approve': self.decide_dependency(int(a[0]),True,'discord',role)
   elif c.name=='dependency-apply': self.apply_dependency(int(a[0]),role)
   elif c.name=='provider-select': self.select_provider(a[0],a[1],role)
   elif c.name=='preset-select': self.select_preset(a[0],a[1],role)
   elif c.name=='project-config': self.configure_project(a[0],a[1],a[2],role)
   elif c.name in ('cost-report','error-report'): return self.report(a[0],role)['costs' if c.name=='cost-report' else 'errors']
   elif c.name.endswith(('approve','reject')): self.decide_gate(a[0],c.name.split('-')[0].upper(),'APPROVED' if c.name.endswith('approve') else 'REJECTED','discord',role)
   elif c.name=='scene-replace': return {'artifact_id':self.replace_scene_artifact(int(a[0]),a[1],role)}
   return {'ok':True}
  except (ValueError,TypeError) as e:
   if isinstance(e,CommandError): raise
   raise CommandError('INVALID_ARGUMENT',str(e)) from e

 def run_stage(self,pid,stage,role=Role.OPERATOR):
  """Generic entry point may delegate to gates, never manufacture protected jobs."""
  normalized=stage.strip().lower().replace('_','-')
  if normalized in ('image','batch','batch-image'): return self.start_batch(pid,role)
  if normalized in ('animation','animate'): return self.start_animation(pid,role)
  if normalized in ('qa','assembly','final','upload'):
   raise PermissionError(f'{normalized} requires its dedicated evidence/gated workflow')
  if normalized in ('plan','planning'):
   # Planning is execution, not merely queuing a stage-shaped job.
   self.plan_scenes(pid,role=role)
   job=self.db.one("select * from jobs where project_id=? order by id desc",(pid,))
   return job['id']
  raise ValueError('unknown stage')

 def restore_latest_checkpoint(self,pid,stage,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  r=self.db.one("select stages.* from stages join jobs on jobs.id=stages.job_id where jobs.project_id=? and stages.name=? and stages.state='SUCCEEDED' order by stages.id desc",(pid,stage))
  if not r: raise ValueError('checkpoint not found')
  snap=json.loads(r['checkpoint_json']); keys={'snapshot_version','data','project','job','scenes','artifacts','snapshot_sha256'}
  if not keys.issubset(snap) or set(snap)-keys!=set(snap.get('data',{})) or any(snap[k]!=snap['data'][k] for k in set(snap)-keys) or snap['snapshot_version']!=3: raise ValueError('incomplete checkpoint snapshot')
  payload={k:snap[k] for k in keys-{'snapshot_sha256'}}; digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
  if not isinstance(snap['snapshot_sha256'],str) or digest!=snap['snapshot_sha256']: raise ValueError('checkpoint checksum mismatch')
  current=self.db.one('select * from projects where id=?',(pid,)); project_fields=('language','style_json','references_json','bible_json','image_provider','whiteboard_mode','seed','transition','min_scenes','max_scenes','retention_days','output_json')
  if not current or snap['project'].get('id')!=pid or any(k not in snap['project'] for k in project_fields): raise ValueError('checkpoint project mismatch/incomplete')
  scene_fields=('id','project_id','code','ord','start_ms','end_ms','text','special','state','approval_state','qa_state','qa_json','duration_exception','continuity_json','checkpoint_json'); canonical=('id','project_id','code','ord','start_ms','end_ms','text','special','duration_exception')
  if any(any(k not in s for k in scene_fields) for s in snap['scenes']): raise ValueError('incomplete checkpoint scenes')
  live_scenes=[dict(x) for x in self.db.all('select * from scenes where project_id=? order by ord',(pid,))]
  if [tuple(s[k] for k in canonical) for s in snap['scenes']] != [tuple(s[k] for k in canonical) for s in live_scenes] or [s['ord'] for s in snap['scenes']]!=list(range(1,len(snap['scenes'])+1)): raise ValueError('checkpoint scene identity/set/order mismatch')
  af=('id','project_id','scene_id','kind','uri','sha256','version','parent_id','status','created_at','expires_at','deleted_at'); immutable=tuple(k for k in af if k not in ('status','deleted_at'))
  if any(any(k not in a for k in af) for a in snap['artifacts']): raise ValueError('incomplete checkpoint artifacts')
  live=[dict(x) for x in self.db.all('select * from artifacts where project_id=? order by id',(pid,))]
  if [tuple(a[k] for k in immutable) for a in snap['artifacts']] != [tuple(a[k] for k in immutable) for a in live]: raise ValueError('checkpoint artifact inventory mismatch')
  ids={a['id'] for a in snap['artifacts']}; sids={s['id'] for s in snap['scenes']}
  if any(a['project_id']!=pid or (a['scene_id'] is not None and a['scene_id'] not in sids) or (a['parent_id'] is not None and a['parent_id'] not in ids) for a in snap['artifacts']): raise ValueError('checkpoint artifact lineage mismatch')
  for a in snap['artifacts']:
   if a['status']!='DELETED':
    try: path=self._owned_path(pid,a['uri']); actual=self._hash(path)[0]
    except (OSError,PermissionError) as exc: raise ValueError('checkpoint artifact binary unavailable') from exc
    if actual!=a['sha256']: raise ValueError('checkpoint artifact binary checksum mismatch')
  with self.db.transaction():
   version=current['version']+1
   self.db.execute("update jobs set state='BLOCKED',updated_at=?,lease_owner=NULL,lease_expires_at=NULL where project_id=? and state in ('QUEUED','RUNNING','PAUSED')",(now(),pid)); self.db.execute("update approvals set revoked_at=?,revoked_reason=? where project_id=? and revoked_at is null",(now(),'checkpoint restored',pid))
   # Restore content/configuration only.  The live lifecycle state and its
   # blocked_reason are deliberately absent, so historical ACTIVE snapshots can
   # never resurrect PAUSED/BLOCKED/COMPLETED projects.
   self.db.execute('update projects set '+','.join(f'{k}=?' for k in project_fields)+',version=? where id=?',tuple(snap['project'][k] for k in project_fields)+(version,pid))
   mutable=('state','approval_state','qa_state','qa_json','continuity_json','checkpoint_json')
   for s in snap['scenes']: self.db.execute('update scenes set '+','.join(f'{k}=?' for k in mutable)+' where id=? and project_id=?',tuple(s[k] for k in mutable)+(s['id'],pid))
   for a in snap['artifacts']: self.db.execute('update artifacts set status=?,deleted_at=? where id=? and project_id=?',(a['status'],a['deleted_at'],a['id'],pid))
   self._event(pid,'CHECKPOINT_RESTORED',{'stage':stage,'snapshot_sha256':snap['snapshot_sha256'],'manifest_sha256':self._manifest_hash(pid),'project_version':version})
  return dict(r)
 def observe_cost(self,pid,provider,currency,amount,role=Role.OPERATOR): self._role(role,'costs'); self.db.execute("insert into cost_observations(project_id,provider,currency,amount,created_at) values(?,?,?,?,?)",(pid,provider,currency,amount,now()))
 def propose_dependency(self,pid,dependency,impact,proposer="system",role=Role.OPERATOR): self._role(role,'dependency-propose'); return self.db.execute("insert into impact_proposals(project_id,dependency,impact_json,state,proposer,created_at) values(?,?,?,?,?,?)",(pid,dependency,json.dumps(impact),"PROPOSED",proposer,now())).lastrowid
 def decide_dependency(self,i,approved,actor,role=Role.OWNER):
  self._role(role,'approve')
  if self.db.execute("update impact_proposals set state=?,decider=?,decided_at=? where id=? and state='PROPOSED'",("APPROVED" if approved else "REJECTED",actor,now(),i)).rowcount!=1: raise PermissionError('illegal dependency decision')
 def apply_dependency(self,i,role=Role.OWNER):
  self._role(role,'dependency')
  p=self.db.one("select * from impact_proposals where id=?",(i,))
  if not p or p['state']!='APPROVED': raise PermissionError('approval required')
  with self.db.transaction():
   if self.db.execute("update impact_proposals set state='APPLIED',applied_at=? where id=? and state='APPROVED'",(now(),i)).rowcount!=1: raise PermissionError('dependency already applied')
   self._event(p['project_id'],"DEPENDENCY_APPLIED",{"dependency":p['dependency']})
 def pause(self,pid,role=Role.OPERATOR):
  self._role(role,'pause')
  with self.db.transaction():
   if self.db.execute("update projects set state='PAUSED' where id=? and state='ACTIVE'",(pid,)).rowcount!=1: raise PermissionError('only ACTIVE projects can pause')
   self.db.execute("update jobs set state='PAUSED',updated_at=?,lease_owner=NULL,lease_expires_at=NULL where project_id=? and state in ('QUEUED','RUNNING')",(now(),pid))
   self._event(pid,"PAUSED")
 def resume(self,pid,role=Role.OPERATOR):
  self._role(role,'resume'); p=self.db.one('select * from projects where id=?',(pid,))
  if not p or p['state']!='PAUSED': raise PermissionError('only PAUSED projects can resume')
  with self.db.transaction():
   if self.db.execute("update projects set state='ACTIVE' where id=? and state='PAUSED'",(pid,)).rowcount!=1: raise PermissionError('only PAUSED projects can resume')
   self.db.execute("update jobs set state='QUEUED',updated_at=? where project_id=? and state='PAUSED' and project_version=?",(now(),pid,p['version']))
   self._event(pid,"RESUMED")
 def status(self,pid,role=Role.OPERATOR): self._role(role,'status'); return {"project":dict(self.db.one("select * from projects where id=?",(pid,))),"scenes":[dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))],"jobs":[dict(x) for x in self.db.all('select * from jobs where project_id=?',(pid,))]}
 def report(self,pid,role=Role.OPERATOR):
  self._role(role,'cost-report')
  return {"costs":[dict(x) for x in self.db.all("select * from cost_observations where project_id=?",(pid,))],"errors":[dict(x) for x in self.db.all("select * from events where project_id=? and (type like '%BLOCKED' or data_json like '%error%')",(pid,))]}
