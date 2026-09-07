import hashlib,json,os,uuid,random,shutil
from contextlib import ExitStack, contextmanager, closing
from pathlib import Path
from datetime import datetime,timezone,timedelta
from .policies import validate_timing,required_approval,DurationPolicy,local_visual_frames,TimingMismatch
from .contracts import OutputConfig,ContactSheetArtifact,QAEvidence,validate_local_output
from .evidence import (scene_evidence_hash,manifest_evidence_hash,
 scene_dependency_revision,image_subject,scene_qa_current,scene_approval_current)
from .policies import ResourceScheduler
from .adapters import Role,authorize,parse_discord,CommandError
from .inputs import normalize_document
from .media import AssemblyError
from .atomic_fs import (rename_noreplace,rename_noreplace_at,open_dir_beneath,
 snapshot_at,assert_identity_at,walk_dirs_at,fsync_file_at)

def now(): return datetime.now(timezone.utc).isoformat()
class Pipeline:
 # status_summary is a polling contract: every returned string is either an
 # enumerated/database identifier or truncated to these documented character caps.
 STATUS_TEXT_LIMITS={'project_name':128,'blocked_reason':256,'job_kind':48,'job_state':24}
 STATUS_SUMMARY_QUERY_CEILING=8
 STATUS_JOB_GROUP_LIMIT=8
 STATUS_INDICATOR_LIMIT=8
 def __init__(self,db,duration_policy=None,scheduler=None): self.db=db; self.duration_policy=duration_policy or DurationPolicy(); self.scheduler=scheduler or ResourceScheduler()
 def _role(self,role,command):
  try:r=role if isinstance(role,Role) else Role(role)
  except Exception as e: raise PermissionError('valid role required') from e
  if not authorize(r,command): raise PermissionError(f'{r.value} cannot {command}')
 def _root(self,pid):
  root=self._root_lookup(pid); root.mkdir(parents=True,exist_ok=True); return root
 def _root_lookup(self,pid):
  p=self.db.one('select artifact_root from projects where id=?',(pid,))
  if not p: raise ValueError('project not found')
  return Path(p['artifact_root'] or (self.db.path.parent/'artifacts'/pid)).absolute()
 def _owned_path(self,pid,path,must_exist=True,root=None):
  root=Path(root) if root is not None else self._root(pid); candidate=Path(path)
  if not candidate.is_absolute(): candidate=root/candidate
  candidate=candidate.resolve(strict=must_exist)
  try: candidate.relative_to(root)
  except ValueError as exc: raise PermissionError('path escapes managed artifact root') from exc
  return candidate
 def _publication_parent_fd(self,pid,path,create=False):
  root=self._root_lookup(pid); candidate=Path(path)
  if not candidate.is_absolute(): candidate=root/candidate
  try: rel=candidate.relative_to(root)
  except ValueError as exc: raise PermissionError('path escapes managed artifact root') from exc
  if not rel.name or rel.name in ('.','..'): raise PermissionError('publication leaf required')
  return open_dir_beneath(root,rel.parent,create=create,durable=True),rel.name
 @contextmanager
 def _publication_fds(self,pid,staging,final,create_final=False):
  """Pin both parents, including safe creation, and close partial opens."""
  sfd=ffd=None
  try:
   sfd,sname=self._publication_parent_fd(pid,staging)
   ffd,fname=self._publication_parent_fd(pid,final,create=create_final)
   yield sfd,sname,ffd,fname
  finally:
   if ffd is not None: os.close(ffd)
   if sfd is not None: os.close(sfd)
 def _hash_at(self,fd,name):
  with os.fdopen(os.open(name,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=fd),'rb') as stream:
   h=hashlib.sha256(); size=0
   for chunk in iter(lambda:stream.read(1024*1024),b''): h.update(chunk); size+=len(chunk)
  return h.hexdigest(),size
 def _safe_parent(self,root,path):
  root=Path(root); parent=Path(path).parent
  try: rel=parent.relative_to(root)
  except ValueError as exc: raise PermissionError('path escapes managed artifact root') from exc
  current=root
  if current.is_symlink(): raise PermissionError('managed root symlink is forbidden')
  for part in rel.parts:
   current=current/part
   if current.is_symlink(): raise PermissionError('symlinked output parent is forbidden')
  return parent
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
  validate_local_output(out.__dict__,transition)
  pid=str(uuid.uuid4()); root=(self.db.path.parent/'artifacts'/pid).resolve(); root.mkdir(parents=True,exist_ok=False)
  try:
   with self.db.transaction():
    self.db.execute("insert into projects(id,name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,blocked_reason,created_at,min_scenes,max_scenes,retention_days,output_json,version,artifact_root) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(pid,name,language,"ACTIVE",json.dumps(style or {}),json.dumps(references or []),json.dumps(bible or {}),image_provider,whiteboard_mode,int(seed),transition,None,now(),lo,hi,retention_days,json.dumps(out.__dict__),1,str(root))); self._event(pid,"PROJECT_CREATED",{'seed':seed})
  except Exception: shutil.rmtree(root,ignore_errors=True); raise
  return pid
 def import_narration(self,pid,source,role=Role.OPERATOR):
  """Measured PCM RIFF/WAV only; private owned bytes, no narration generation.

  Top-level only. Unknown crash residuals are retained for manual review.
  Other containers/codecs fail closed rather than trusting container duration.
  """
  import stat,io,wave,struct
  self._role(role,'import'); self._copy_boundary()
  with self.db.transaction(write=False):
   self.project(pid); self._active(pid); expected=self._contact_snapshot(pid)
  source=Path(source).absolute()
  if '..' in source.parts or source.is_relative_to(self._root_lookup(pid)):
   raise PermissionError('external nonaliased narration required')
  with ExitStack() as stack:
   parent=open_dir_beneath(source.parent); stack.callback(os.close,parent)
   leaf=os.open(source.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC,dir_fd=parent); stack.callback(os.close,leaf)
   before=os.fstat(leaf)
   if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise PermissionError('regular unaliased narration required')
   source_identity,unused=snapshot_at(parent,source.name)
   with self._managed_copy(pid,Path('/proc/self/fd')/str(leaf)) as (managed,fd,identity):
    self._copy_recheck(managed,fd,identity)
    owned=os.open(managed.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd); stack.callback(os.close,owned)
    initial=os.fstat(owned)
    if (initial.st_dev,initial.st_ino)!=identity or initial.st_nlink!=1: raise PermissionError('owned narration substituted')
    with os.fdopen(os.dup(owned),'rb') as stream: data=stream.read()
    # Parse and consume the exact hashed snapshot, not the external pathname.
    try:
     if data[:4]!=b'RIFF' or data[8:12]!=b'WAVE' or len(data)!=struct.unpack('<I',data[4:8])[0]+8: raise ValueError('complete RIFF WAV required')
     offset=12; chunks=[]
     while offset<len(data):
      if offset+8>len(data): raise ValueError('truncated WAV chunk')
      tag=data[offset:offset+4]; size=struct.unpack_from('<I',data,offset+4)[0]; offset+=8
      if offset+size>len(data): raise ValueError('truncated WAV chunk')
      chunks.append((tag,size)); offset+=size+(size%2)
     if offset!=len(data) or sum(tag==b'data' for tag,size in chunks)!=1: raise ValueError('ambiguous WAV data')
     with wave.open(io.BytesIO(data),'rb') as audio:
      frames=audio.getnframes(); rate=audio.getframerate(); width=audio.getsampwidth(); channels=audio.getnchannels()
      if audio.getcomptype()!='NONE' or frames<=0 or rate<=0: raise ValueError('positive PCM narration required')
      if next(size for tag,size in chunks if tag==b'data')!=frames*width*channels or len(audio.readframes(frames))!=frames*width*channels: raise ValueError('truncated narration')
      duration_ms=(frames*1000+rate//2)//rate
      if duration_ms<=0: raise ValueError('narration shorter than one millisecond')
    except (wave.Error,EOFError,struct.error) as exc: raise ValueError('invalid PCM WAV narration') from exc
    digest=hashlib.sha256(data).hexdigest()
    pinned,digest_check=snapshot_at(fd,managed.name)
    if digest_check!=digest: raise PermissionError('owned narration changed')
    fsync_file_at(fd,managed.name,pinned); os.fsync(fd)
    with self.db.transaction():
     self._artifact_recheck(pid,expected)
     self._copy_recheck(managed,fd,identity); assert_identity_at(fd,managed.name,pinned)
     current=os.fstat(owned)
     if (current.st_size,current.st_mtime_ns,current.st_ctime_ns,current.st_nlink)!=(initial.st_size,initial.st_mtime_ns,initial.st_ctime_ns,1): raise PermissionError('owned narration changed')
     assert_identity_at(parent,source.name,source_identity)
     current_source=os.fstat(leaf)
     if (current_source.st_dev,current_source.st_ino,current_source.st_size,current_source.st_mtime_ns,current_source.st_ctime_ns,current_source.st_nlink)!=(before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns,1): raise PermissionError('source changed during ingestion')
     self.import_audio(pid,str(managed),duration_ms,digest,role)
     aid=self._register_artifact(pid,'NARRATION',managed,digest)
     receipt={'artifact_id':aid,'uri':str(managed),'duration_ms':duration_ms,'sha256':digest,'measurement':'pcm-wav-owned-v1'}
     self._event(pid,'NARRATION_MEASURED',receipt)
     return receipt
 def import_audio(self,pid,uri,duration_ms,sha256,role=Role.OPERATOR):
  self._role(role,'import')
  with self.db.transaction():
   self._active(pid); old=self.db.one('select * from audio where project_id=?',(pid,))
   if old and (old['uri'],old['duration_ms'],old['sha256'])==(uri,duration_ms,sha256):return
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
  self.db.execute("update scenes set qa_state='PENDING',qa_json='{}' where project_id=?",(pid,))
  self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and status='ACTIVE'",(pid,))
  if clear_plan:
   self.db.execute("update artifacts set detached_scene_id=scene_id,scene_id=NULL where project_id=? and scene_id is not null",(pid,))
   self.db.execute("update approvals set scene_id=NULL where project_id=? and scene_id is not null",(pid,))
   self.db.execute("delete from scenes where project_id=?",(pid,))
 def assemble(self,pid,output,dry_run=False,role=Role.OPERATOR,assembler=None):
  from .media import LocalFinalAssembler,AssemblyError
  self._role(role,'animate')
  self._active(pid)
  capability=self.db.one('select output_json,transition from projects where id=?',(pid,))
  try: validate_local_output(json.loads(capability['output_json']),capability['transition'])
  except ValueError as exc: raise AssemblyError(str(exc)) from exc
  if not dry_run:
   self._publication_prerequisites()
   self.reconcile_publications()
  self._active(pid); root=self._root_lookup(pid) if dry_run else self._root(pid)
  if dry_run and not root.is_dir(): raise AssemblyError('managed artifact root is absent')
  requested=Path(output) if Path(output).is_absolute() else root/Path(output)
  self._safe_parent(root,requested)
  if requested.is_symlink(): raise AssemblyError('output symlink alias forbidden')
  out=self._owned_path(pid,output,False,root=root); self._safe_parent(root,out)
  project=self.db.one('select * from projects where id=?',(pid,)); version=project['version']; audio=self.db.one('select * from audio where project_id=?',(pid,))
  if not audio: raise AssemblyError('narration audio required')
  ap=self._owned_path(pid,audio['uri']); ah=self._hash(ap)[0]
  if ah!=audio['sha256']: raise AssemblyError('audio checksum mismatch')
  scenes=self.db.all('select * from scenes where project_id=? order by ord',(pid,))
  if not scenes or [s['ord'] for s in scenes]!=list(range(1,len(scenes)+1)): raise AssemblyError('exact ordered scene set required')
  try: allocated=local_visual_frames(audio['duration_ms'],[(s['start_ms'],s['end_ms']) for s in scenes])
  except TimingMismatch as exc: raise AssemblyError('scene/audio duration mismatch: '+str(exc)) from exc
  # Only a registered final at its exact canonical destination can be a reuse
  # candidate. All other names (including hardlink aliases) remain forbidden.
  candidate=self.db.one("select a.id from artifacts a join final_assemblies f on f.artifact_id=a.id where a.project_id=? and a.kind='FINAL_VIDEO' and a.status='ACTIVE' and a.uri=?",(pid,str(out)))
  aliases=[ap]+[self._owned_path(pid,r['uri'],False) for r in self.db.all('select id,uri from artifacts where project_id=?',(pid,)) if not candidate or r['id']!=candidate['id']]
  if any(out==p or (out.exists() and p.exists() and os.path.samefile(out,p)) for p in aliases): raise AssemblyError('output aliases managed input/artifact')
  gate_artifacts=self.db.all('select * from artifacts where project_id=?',(pid,))
  gate_approvals=self.db.all('select * from approvals where project_id=? order by gate,scene_id,id',(pid,))
  selected=[]; frame_total=sum(allocated)
  for s,frames in zip(scenes,allocated):
   if s['state'] not in ('IMAGE_READY','ANIMATED') or s['qa_state']!='PASS' or s['approval_state'] in ('REQUIRED','REJECTED'): raise AssemblyError('scene QA/approval gate failed')
   rows=self.db.all("select * from artifacts where scene_id=? and status='ACTIVE' and kind in ('ANIMATION','SCENE_VIDEO','IMAGE') order by version desc,id",(s['id'],))
   videos=[a for a in rows if a['kind'] in ('ANIMATION','SCENE_VIDEO')]; pool=videos or [a for a in rows if a['kind']=='IMAGE']
   if not pool: raise AssemblyError('exactly one eligible current scene artifact required')
   if len(pool)!=1: raise AssemblyError('duplicate eligible current scene artifact')
   a=pool[0]; path=self._owned_path(pid,a['uri'])
   if frames < 1: raise AssemblyError('scene rounds to zero frames')
   if self._hash(path)[0]!=a['sha256']: raise AssemblyError('scene artifact checksum mismatch')
   if not scene_qa_current(project,s,gate_artifacts): raise AssemblyError('scene QA subject/dependency gate failed')
   if (s['approval_state']=='APPROVED' or s['ord']<=5 or s['special']) and not scene_approval_current(project,s,gate_artifacts,gate_approvals): raise AssemblyError('stale scene approval evidence')
   selected.append((s,a,path,a['kind']=='IMAGE',frames))
  if sum(x[4] for x in selected)!=frame_total: raise AssemblyError('frame allocation mismatch')
  # QA subjects and contact sheets remain relevant even when a clip is selected.
  for dependency in gate_artifacts:
   if dependency['status']=='ACTIVE' and (dependency['scene_id'] is not None or dependency['kind']=='CONTACT_SHEET'):
    if self._hash(self._owned_path(pid,dependency['uri']))[0]!=dependency['sha256']: raise AssemblyError('input dependency checksum mismatch')
  engine=assembler or LocalFinalAssembler(root); current_evidence=self._manifest_hash(pid)
  current_scenes={s['id'] for s in scenes if scene_approval_current(project,s,gate_artifacts,gate_approvals)}
  approvals=[{k:a[k] for k in ('id','gate','scene_id','actor','project_version','evidence_sha256')} for a in gate_approvals if a['decision']=='APPROVED' and a['revoked_at'] is None and ((a['gate']=='SCENE' and a['scene_id'] in current_scenes) or (a['gate']!='SCENE' and a['project_version']==version))]
  if not any(a['gate']=='POST_BATCH' and a['scene_id'] is None and a['evidence_sha256']==current_evidence for a in approvals): raise AssemblyError('current post-batch human approval required')
  lineage=[{'type':'NARRATION_AUDIO','sha256':ah}]+[{'type':'SCENE_VISUAL','ord':s['ord'],'artifact_id':a['id'],'sha256':a['sha256']} for s,a,_,_,_ in selected]
  manifest={'project_id':pid,'project_version':version,'current_evidence_sha256':current_evidence,'requested_output':str(out),'narration':{'uri':str(ap),'sha256':ah,'duration_ms':audio['duration_ms']},'audio':{'sha256':ah,'duration_ms':audio['duration_ms']},'approvals':approvals,'parent_lineage':lineage,'scenes':[{'scene_id':s['id'],'ord':s['ord'],'source_artifact_id':a['id'],'source_kind':a['kind'],'source_version':a['version'],'parent_id':a['parent_id'],'sha256':a['sha256'],'frames':frames} for s,a,_,_,frames in selected],'output_config':{'width':1920,'height':1080,'fps':30,'video_codec':'h264','pixel_format':'yuv420p','audio_codec':'aac','tolerance_seconds':1/30+.020},'tool_versions':None if dry_run else engine.tool_versions()}
  identity={k:v for k,v in manifest.items() if k not in ('requested_output','tool_versions','approvals')}
  batch_row=self.db.one("select data_json from events where project_id=? and type='POST_BATCH_QA_RECORDED' order by id desc limit 1",(pid,))
  batch_qa=json.loads(batch_row['data_json']) if batch_row else {}
  if batch_qa.get('schema_version')!=2 or batch_qa.get('evidence_sha256')!=current_evidence or batch_qa.get('project_version')!=version or not any(a['id']==batch_qa.get('approval_id') and a['gate']=='POST_BATCH' for a in approvals): raise AssemblyError('current persisted post-batch QA required')
  if not QAEvidence(batch_qa['checks'],batch_qa['score'],batch_qa['evaluator']).passed or not any(a['id']==batch_qa['subject']['artifact_id'] and a['sha256']==batch_qa['subject']['sha256'] and a['kind']=='CONTACT_SHEET' and a['status']=='ACTIVE' for a in gate_artifacts): raise AssemblyError('post-batch QA subject mismatch')
  identity['batch_qa']=batch_qa
  identity['schema_version']=2
  evidence=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest(); manifest['input_evidence_sha256']=evidence; manifest['input_identity']=identity
  old=self.db.one("select a.*,f.manifest_json from artifacts a join final_assemblies f on f.artifact_id=a.id where f.evidence_sha256=? and a.status='ACTIVE'",(evidence,))
  if not dry_run and old and Path(old['uri']).resolve()==out and self._hash(self._owned_path(pid,old['uri']))[0]==old['sha256']:
   from .evidence import digest
   saved=json.loads(old['manifest_json'])
   if saved.get('input_identity')!=identity or digest({k:v for k,v in saved.items() if k!='manifest_sha256'})!=saved.get('manifest_sha256'): raise AssemblyError('legacy or invalid reuse manifest')
   engine.validate_final(out,audio['duration_ms']/1000)
   if self._hash(out)[0]!=old['sha256']: raise AssemblyError('output changed during reuse QA')
   with self.db.transaction():
    self._active(pid)
    if self._manifest_hash(pid)!=current_evidence or not self.db.one("select 1 from artifacts where id=? and status='ACTIVE' and sha256=?",(old['id'],old['sha256'])): raise AssemblyError('project/evidence changed during reuse')
    if not dry_run:
     fresh=self.assemble(pid,root/('.verify-'+uuid.uuid4().hex+'.mp4'),dry_run=True,assembler=engine)
     if fresh['manifest']['input_evidence_sha256']!=evidence: raise AssemblyError('input identity changed during reuse')
   return {'artifact_id':old['id'],'output':old['uri'],'reused':True,'manifest':json.loads(old['manifest_json'])}
  if out.exists(): raise AssemblyError('output already exists')
  commands=[engine.normalize_command(path,root/f'.segment-{i}.mp4',frames/30,image,frames=frames) for i,(s,a,path,image,frames) in enumerate(selected)]; commands += [engine.concat_command(root/'.concat.txt',root/'.visual.mp4'),engine.mux_command(root/'.visual.mp4',ap,root/'.final.tmp.mp4')]; manifest['commands']=commands
  if dry_run:
   manifest['manifest_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
   return {'dry_run':True,'output':str(out),'manifest':manifest,'commands':commands}
  work=Path(__import__('tempfile').mkdtemp(prefix='.assembly-',dir=root))
  try:
   # Snapshot validated bytes first; all ffmpeg inputs below are private immutable names.
   snap_audio=work/'audio.input'; shutil.copyfile(ap,snap_audio)
   if self._hash(snap_audio)[0]!=ah: raise AssemblyError('audio changed while snapshotting')
   seg=[]
   for i,(s,a,path,image,frames) in enumerate(selected):
    source=work/f'source-{i:06}{path.suffix}'; shutil.copyfile(path,source)
    if self._hash(source)[0]!=a['sha256']: raise AssemblyError('scene source changed while snapshotting')
    q=work/f'{i:06}.mp4'; engine.run(engine.normalize_command(source,q,frames/30,image,frames=frames)); seg.append(q)
   listing=work/'concat.txt'; listing.write_text(''.join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in seg)); visual=work/'visual.mp4'; engine.run(engine.concat_command(listing,visual)); staged=work/'final.mp4'; engine.run(engine.mux_command(visual,snap_audio,staged)); manifest['ffprobe']=engine.validate_final(staged,audio['duration_ms']/1000)
   manifest['manifest_sha256']=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
   token=uuid.uuid4().hex; pubdir=root/'.publications'; durable=pubdir/(token+'.staged.mp4')
   lease=self.db.acquire_reconciliation_lease()
   try:
    with ExitStack() as fds:
     pubfd=open_dir_beneath(root,'.publications',create=True,durable=True); fds.callback(os.close,pubfd)
     workfd=open_dir_beneath(root,work.relative_to(root),durable=True); fds.callback(os.close,workfd)
     identity,digest=snapshot_at(workfd,staged.name); size=identity[5]
     fsync_file_at(workfd,staged.name,identity); os.fsync(workfd)
     created=now()
     payload=dict(zip(self.PUBLICATION_COLUMNS,(token,pid,version,evidence,current_evidence,str(durable),str(out),digest,size,json.dumps(manifest,sort_keys=True),'PREPARED',None,created,created,None)))
     with self.db.transaction():
      self.db.assert_reconciliation_lease(lease)
      current=self.db.one('select version,state from projects where id=?',(pid,))
      if not current or current['version']!=version or current['state']!='ACTIVE' or self._manifest_hash(pid)!=current_evidence: raise AssemblyError('project/evidence changed during assembly')
      if out.exists() or self.db.one('select 1 from publication_journal where final_path=?',(str(out),)): raise AssemblyError('output already exists')
      self.db.execute("insert into publication_intents values(?,?,?,?,?,'INTENT',?,?,NULL)",(token,pid,str(staged),json.dumps(identity),json.dumps(payload,sort_keys=True),created,created))
      self._publication_crash('before_intent_commit')
     self._publication_crash('after_intent_commit')
     with self.db.transaction():
      self.db.assert_reconciliation_lease(lease)
      self._publication_crash('before_staging_move'); assert_identity_at(workfd,staged.name,identity)
      rename_noreplace_at(workfd,staged.name,pubfd,durable.name)
      self._publication_crash('after_staging_move')
      self._publication_crash('before_staging_fsync')
      fsync_file_at(pubfd,durable.name,identity); os.fsync(pubfd); os.fsync(workfd)
      self._publication_crash('after_staging_fsync')
      self._insert_prepared(payload,token)
   finally: self.db.release_reconciliation_lease(lease)
   self._publication_crash('after_prepared_commit')
   try:
    self.reconcile_publications()
    row=self.db.one('select state,artifact_id,error from publication_journal where token=?',(token,))
    if row['state']!='COMMITTED': raise AssemblyError('publication failed: '+(row['error'] or row['state']))
   except Exception as exc:
    # Keep the journal recoverable while restoring the promise that a failed
    # synchronous call does not leave its requested destination exposed.
    try:
     with ExitStack() as fds:
      ffd,fname=self._publication_parent_fd(pid,out); fds.callback(os.close,ffd)
      sfd,sname=self._publication_parent_fd(pid,durable); fds.callback(os.close,sfd)
      try: exists=__import__('stat').S_ISREG(os.stat(fname,dir_fd=ffd,follow_symlinks=False).st_mode)
      except FileNotFoundError: exists=False
      if exists:
       rollback_lease=self.db.acquire_reconciliation_lease()
       try:
        with self.db.transaction():
         self.db.assert_reconciliation_lease(rollback_lease)
         state=self.db.one('select state from publication_journal where token=?',(token,))
         if state and state['state'] in ('PREPARED','MANUAL_REVIEW'):
          owned_identity,owned_digest=snapshot_at(ffd,fname)
          if owned_identity!=identity or owned_digest!=digest: raise PermissionError('rollback ownership uncertain')
          assert_identity_at(ffd,fname,identity)
          rename_noreplace_at(ffd,fname,sfd,sname)
          fsync_file_at(sfd,sname,identity); os.fsync(sfd); os.fsync(ffd)
       finally: self.db.release_reconciliation_lease(rollback_lease)
    except OSError as cleanup:
     with self.db.transaction():
      self.db.execute("update publication_journal set state='MANUAL_REVIEW',updated_at=?,error=? where token=? and state='PREPARED'",(now(),'REGISTRATION_FAILED; rollback rename failed: '+str(cleanup),token)); self._event(pid,'FINAL_PUBLICATION_MANUAL_REVIEW',{'token':token,'reason':str(cleanup)})
    raise
   row=self.db.one('select state,artifact_id,error from publication_journal where token=?',(token,))
   if row['state']!='COMMITTED': raise AssemblyError('publication failed: '+(row['error'] or row['state']))
   self._publication_crash('after_registration_before_cleanup')
   final=self.db.one('select uri from artifacts where id=?',(row['artifact_id'],)); return {'artifact_id':row['artifact_id'],'output':final['uri'],'reused':False,'manifest':manifest}
  finally: shutil.rmtree(work,ignore_errors=True)
 PUBLICATION_COLUMNS=('token','project_id','project_version','evidence_sha256','current_evidence_sha256','staging_path','final_path','output_sha256','output_size','manifest_json','state','artifact_id','created_at','updated_at','error')
 PUBLICATION_RECOVERY_LIMIT=100
 def _publication_crash(self,point): pass
 def _insert_prepared(self,payload,token):
  self._publication_crash('before_prepared_insert')
  self.db.execute('insert into publication_journal values('+','.join('?' for _ in self.PUBLICATION_COLUMNS)+')',tuple(payload[k] for k in self.PUBLICATION_COLUMNS))
  self._publication_crash('after_prepared_insert')
  self.db.execute("update publication_intents set state='PREPARED',updated_at=?,error=NULL where token=? and state='INTENT'",(now(),token))
 def _publication_prerequisites(self):
  from .media import AssemblyError
  if self.db._tx_depth or self.db.conn.in_transaction: raise AssemblyError('publication requires a top-level durable transaction')
  if self.db.one('pragma synchronous')[0]<2: raise AssemblyError('publication requires SQLite synchronous FULL or EXTRA')
 def _publication_manual(self,lease,table,token,pid,error):
  assert table in ('publication_intents','publication_journal')
  with self.db.transaction():
   self.db.assert_reconciliation_lease(lease)
   self.db.execute(f"update {table} set state='MANUAL_REVIEW',updated_at=?,error=? where token=? and state in ('INTENT','PREPARED')",(now(),error,token))
   self._event(pid,'FINAL_PUBLICATION_MANUAL_REVIEW',{'token':token,'reason':error})
 def _validate_publication_payload(self,r,root):
  # Pure typed validation precedes ALL directory creation, opens and fsyncs.
  if not isinstance(r,dict) or set(r)!=set(self.PUBLICATION_COLUMNS): raise ValueError('invalid publication fields')
  for key in ('token','project_id','created_at','updated_at','manifest_json'):
   if type(r[key]) is not str or not r[key] or '\x00' in r[key]: raise ValueError('invalid '+key)
  if '/' in r['token'] or r['token'] in ('.','..'): raise ValueError('invalid token')
  for key in ('project_version','output_size'):
   if type(r[key]) is not int or r[key] < (1 if key=='project_version' else 0): raise ValueError('invalid '+key)
  for key in ('evidence_sha256','current_evidence_sha256','output_sha256'):
   if type(r[key]) is not str or len(r[key])!=64 or any(c not in '0123456789abcdef' for c in r[key]): raise ValueError('invalid '+key)
  for key in ('staging_path','final_path'):
   self._validate_publication_path(r[key],root)
  if r['staging_path']==r['final_path'] or r['state']!='PREPARED' or r['artifact_id'] is not None or r['error'] is not None: raise ValueError('invalid prepared publication')
  if not isinstance(json.loads(r['manifest_json']),dict): raise ValueError('invalid manifest')
 def _validate_publication_path(self,value,root):
  if type(value) is not str or not value or '\x00' in value: raise ValueError('invalid publication path')
  path=Path(value)
  if not path.is_absolute() or '..' in path.parts or path==root: raise ValueError('invalid publication path')
  path.relative_to(root)
  return path
 def _validate_publication_receipt(self,intent,root):
  r=json.loads(intent['journal_json']); identity=json.loads(intent['identity_json'])
  self._validate_publication_payload(r,root)
  if r['token']!=intent['token'] or r['project_id']!=intent['project_id']: raise ValueError('receipt ownership mismatch')
  if not isinstance(identity,list) or len(identity)!=7 or any(type(v) is not int for v in identity): raise ValueError('invalid inode identity')
  if any(v<0 for v in identity[:6]) or not __import__('stat').S_ISREG(identity[4]) or identity[5]!=r['output_size']: raise ValueError('invalid inode identity')
  source=self._validate_publication_path(intent['source_path'],root)
  if Path(r['staging_path'])!=root/'.publications'/(intent['token']+'.staged.mp4') or source.name!='final.mp4' or source.parent.parent!=root or not source.parent.name.startswith('.assembly-'): raise ValueError('invalid intent paths')
  if Path(r['final_path']) in (source,Path(r['staging_path'])): raise ValueError('aliased intent paths')
  return r,identity,source
 def _recover_intent(self,lease,intent):
  # Parse only the receipt; DB failures are deliberately outside row-error catches.
  try:
   root=self._root_lookup(intent['project_id'])
   r,identity,source=self._validate_publication_receipt(intent,root); staging=Path(r['staging_path'])
  except (ValueError,TypeError,KeyError) as exc:
   self._publication_manual(lease,'publication_intents',intent['token'],intent['project_id'],'INVALID_INTENT: '+str(exc)); return
  with ExitStack() as fds:
   sfd,sname=self._publication_parent_fd(intent['project_id'],staging); fds.callback(os.close,sfd)
   # Validate the destination without following it, even before creating PREPARED.
   ffd,fname=self._publication_parent_fd(intent['project_id'],r['final_path'],create=True); fds.callback(os.close,ffd)
   try: os.stat(fname,dir_fd=ffd,follow_symlinks=False)
   except FileNotFoundError: pass
   else: raise PermissionError('intent destination ownership uncertain')
   try:
    wfd,wname=self._publication_parent_fd(intent['project_id'],source); fds.callback(os.close,wfd)
   except FileNotFoundError: wfd=None; wname=source.name
   def probe(fd,name):
    if fd is None:return None
    try:return snapshot_at(fd,name)
    except FileNotFoundError:return None
   staged=probe(sfd,sname); original=probe(wfd,wname)
   if staged is not None and original is not None: raise PermissionError('both intent locations exist')
   found=staged or original
   if found is None:
    with self.db.transaction():
     self.db.assert_reconciliation_lease(lease)
     self.db.execute("update publication_intents set state='ABORTED',updated_at=?,error='MISSING_INTENT_BYTES' where token=? and state='INTENT'",(now(),intent['token']))
    return
   if found!=(tuple(identity),r['output_sha256']) or identity[5]!=r['output_size']: raise PermissionError('intent inode or digest ownership uncertain')
   current=self.db.one('select version,state from projects where id=?',(intent['project_id'],))
   if not current or current['version']!=r['project_version'] or current['state']!='ACTIVE' or self._manifest_hash(intent['project_id'])!=r['current_evidence_sha256']:
    with self.db.transaction():
     self.db.assert_reconciliation_lease(lease)
     self.db.execute("update publication_intents set state='ABORTED',updated_at=?,error='STALE_EVIDENCE' where token=? and state='INTENT'",(now(),intent['token']))
    return
   with self.db.transaction():
    self.db.assert_reconciliation_lease(lease)
    collision=self.db.one('select token from publication_journal where token=? or final_path=? or evidence_sha256=?',(r['token'],r['final_path'],r['evidence_sha256']))
    if collision: raise PermissionError('intent journal reservation collision')
    if original:
     fsync_file_at(wfd,wname,identity); assert_identity_at(wfd,wname,identity)
     rename_noreplace_at(wfd,wname,sfd,sname)
    fsync_file_at(sfd,sname,identity); os.fsync(sfd)
    if wfd is not None: os.fsync(wfd)
    else:
     rootfd=open_dir_beneath(root)
     try: os.fsync(rootfd)
     finally: os.close(rootfd)
    self._insert_prepared(r,intent['token'])
 def reconcile_publications(self,_lease_token=None):
  """Bounded receipt/journal recovery; never discover or delete unknown bytes."""
  from .media import AssemblyError
  self._publication_prerequisites()
  owned=_lease_token is None; lease=_lease_token or self.db.acquire_reconciliation_lease()
  try:
   for intent in self.db.all("select * from publication_intents where state='INTENT' order by created_at,token limit ?",(self.PUBLICATION_RECOVERY_LIMIT,)):
    self.db.renew_reconciliation_lease(lease)
    try: self._recover_intent(lease,dict(intent))
    except OSError as exc:
     self._publication_manual(lease,'publication_intents',intent['token'],intent['project_id'],'INTENT_FILESYSTEM_OR_OWNERSHIP: '+str(exc))
   for row in self.db.all("select * from publication_journal where state='PREPARED' order by created_at,token limit ?",(self.PUBLICATION_RECOVERY_LIMIT,)):
    self.db.renew_reconciliation_lease(lease); r=dict(row); pid=r['project_id']
    receipt=self.db.one('select * from publication_intents where token=?',(r['token'],))
    try:
     root=self._root_lookup(pid); self._validate_publication_payload(r,root)
     if receipt:
      saved,identity,source=self._validate_publication_receipt(dict(receipt),root)
      if any(saved[k]!=r[k] for k in self.PUBLICATION_COLUMNS if k not in ('updated_at','error')): raise ValueError('journal/receipt ownership mismatch')
     staging=Path(r['staging_path']); final=Path(r['final_path'])
    except (ValueError,TypeError,KeyError) as exc:
     self._publication_manual(lease,'publication_journal',r['token'],pid,'INVALID_PUBLICATION: '+str(exc)); continue
    try:
     with self._publication_fds(pid,staging,final,create_final=True) as (sfd,sname,ffd,fname):
      self._reconcile_publication_row(lease,r,pid,staging,final,sfd,sname,ffd,fname)
    except OSError as exc:
     self._publication_manual(lease,'publication_journal',r['token'],pid,'PUBLICATION_FILESYSTEM_OR_OWNERSHIP: '+str(exc))
   return [dict(x) for x in self.db.all('select * from publication_journal order by created_at,token limit ?',(self.PUBLICATION_RECOVERY_LIMIT,))]
  finally:
   if owned:self.db.release_reconciliation_lease(lease)
 def _reconcile_publication_row(self,lease,r,pid,staging,final,sfd,sname,ffd,fname):
    current=self.db.one('select version,state from projects where id=?',(pid,)); valid=bool(current and current['version']==r['project_version'] and current['state']=='ACTIVE' and self._manifest_hash(pid)==r['current_evidence_sha256'])
    try:
     smode=os.stat(sname,dir_fd=sfd,follow_symlinks=False).st_mode; sf=__import__('stat').S_ISREG(smode)
    except FileNotFoundError: sf=False
    try:
     fmode=os.stat(fname,dir_fd=ffd,follow_symlinks=False).st_mode; ff=__import__('stat').S_ISREG(fmode)
    except FileNotFoundError: ff=False
    if ('smode' in locals() and not sf) or ('fmode' in locals() and not ff): raise PermissionError('nonregular publication leaf')
    source=final if ff else staging if sf else None
    identity,digest=snapshot_at(ffd if ff else sfd,fname if ff else sname) if source is not None else (None,None)
    matching=source is not None and (digest,identity[5])==(r['output_sha256'],r['output_size'])
    receipt=self.db.one('select identity_json from publication_intents where token=?',(r['token'],))
    if receipt and source is not None:
     try: expected=json.loads(receipt['identity_json'])
     except (ValueError,TypeError) as exc: raise PermissionError('invalid publication ownership receipt') from exc
     if tuple(expected)!=identity: raise PermissionError('publication inode ownership uncertain')
    if sf and ff: raise PermissionError('both publication locations exist; ownership uncertain')
    if not valid or not matching:
     reason='STALE_EVIDENCE' if not valid else 'MISSING_OR_TAMPERED_BYTES'; cleanup_error=None
     if not valid and matching and source==final:
      quarantine=final.with_name(final.name+'.aborted-'+r['token'])
      try:
       with self.db.transaction():
        self.db.assert_reconciliation_lease(lease); assert_identity_at(ffd,fname,identity)
        rename_noreplace_at(ffd,fname,ffd,quarantine.name)
        fsync_file_at(ffd,quarantine.name,identity); os.fsync(ffd)
      except OSError as exc: cleanup_error=str(exc)
     state='MANUAL_REVIEW' if cleanup_error or (source is not None and not matching) else 'ABORTED'; error=reason+((': '+cleanup_error) if cleanup_error else '')
     with self.db.transaction():
      self.db.assert_reconciliation_lease(lease); self.db.execute("update publication_journal set state=?,updated_at=?,error=? where token=? and state='PREPARED'",(state,now(),error,r['token'])); self._event(pid,'FINAL_PUBLICATION_'+state,{'token':r['token'],'reason':error})
     return
    if not ff:
     try:
      with self.db.transaction():
       self._publication_crash('before_promotion')
       self.db.assert_reconciliation_lease(lease)
       current=self.db.one('select version,state from projects where id=?',(pid,))
       if not current or current['version']!=r['project_version'] or current['state']!='ACTIVE' or self._manifest_hash(pid)!=r['current_evidence_sha256']:
        self.db.execute("update publication_journal set state='ABORTED',updated_at=?,error='STALE_EVIDENCE_BEFORE_PROMOTION' where token=? and state='PREPARED'",(now(),r['token']))
        self._event(pid,'FINAL_PUBLICATION_ABORTED',{'token':r['token'],'reason':'STALE_EVIDENCE_BEFORE_PROMOTION'})
        return
       fsync_file_at(sfd,sname,identity); assert_identity_at(sfd,sname,identity)
       rename_noreplace_at(sfd,sname,ffd,fname)
     except FileExistsError as exc:
      error='DESTINATION_COLLISION: atomic no-replace promotion refused: '+str(exc)
      with self.db.transaction():
       self.db.assert_reconciliation_lease(lease); self.db.execute("update publication_journal set state='ABORTED',updated_at=?,error=? where token=? and state='PREPARED'",(now(),error,r['token'])); self._event(pid,'FINAL_PUBLICATION_ABORTED',{'token':r['token'],'reason':error})
      return
     except OSError as exc:
      error='ATOMIC_PROMOTION_UNAVAILABLE: '+str(exc)
      with self.db.transaction():
       self.db.assert_reconciliation_lease(lease); self.db.execute("update publication_journal set state='MANUAL_REVIEW',updated_at=?,error=? where token=? and state='PREPARED'",(now(),error,r['token'])); self._event(pid,'FINAL_PUBLICATION_MANUAL_REVIEW',{'token':r['token'],'reason':error})
      return
     self._publication_crash('after_rename_before_registration')
    self._publication_crash('before_final_fsync')
    with self.db.transaction():
     self.db.assert_reconciliation_lease(lease)
     fsync_file_at(ffd,fname,identity); os.fsync(ffd); os.fsync(sfd)
    self._publication_crash('after_final_fsync')
    current=self.db.one('select version,state from projects where id=?',(pid,))
    if not current or current['version']!=r['project_version'] or current['state']!='ACTIVE' or self._manifest_hash(pid)!=r['current_evidence_sha256']:
     quarantine=final.with_name(final.name+'.aborted-'+r['token']); cleanup_error=None
     try:
      with self.db.transaction():
       self.db.assert_reconciliation_lease(lease); assert_identity_at(ffd,fname,identity)
       rename_noreplace_at(ffd,fname,ffd,quarantine.name)
       fsync_file_at(ffd,quarantine.name,identity); os.fsync(ffd)
     except OSError as exc: cleanup_error=str(exc)
     state='MANUAL_REVIEW' if cleanup_error else 'ABORTED'; error='STALE_EVIDENCE_AFTER_PROMOTION'+((': '+cleanup_error) if cleanup_error else '')
     with self.db.transaction():
      self.db.assert_reconciliation_lease(lease); self.db.execute("update publication_journal set state=?,updated_at=?,error=? where token=? and state='PREPARED'",(state,now(),error,r['token'])); self._event(pid,'FINAL_PUBLICATION_'+state,{'token':r['token'],'reason':error})
     return
    with self.db.transaction():
     self._publication_crash('before_registration')
     self.db.assert_reconciliation_lease(lease); current=self.db.one('select version,state from projects where id=?',(pid,))
     if not current or current['version']!=r['project_version'] or current['state']!='ACTIVE' or self._manifest_hash(pid)!=r['current_evidence_sha256']:
      # This decision owns the writer: compensate before returning and before
      # any registration SQL. Keep the pinned dirfd, lease and original inode.
      quarantine=final.with_name(final.name+'.aborted-'+r['token'])
      self.db.assert_reconciliation_lease(lease); assert_identity_at(ffd,fname,identity)
      rename_noreplace_at(ffd,fname,ffd,quarantine.name)
      fsync_file_at(ffd,quarantine.name,identity); os.fsync(ffd)
      self.db.execute("update publication_journal set state='ABORTED',updated_at=?,error='STALE_EVIDENCE_AT_REGISTRATION' where token=? and state='PREPARED'",(now(),r['token']))
      self._event(pid,'FINAL_PUBLICATION_ABORTED',{'token':r['token'],'reason':'STALE_EVIDENCE_AT_REGISTRATION'})
      return
     assert_identity_at(ffd,fname,identity)
     self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and kind='FINAL_VIDEO' and status='ACTIVE'",(pid,))
     version=self.db.one("select coalesce(max(version),0)+1 v from artifacts where project_id=? and kind='FINAL_VIDEO' and scene_id is null and detached_scene_id is null",(pid,))['v']; days=self.db.one('select retention_days from projects where id=?',(pid,))['retention_days']; expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
     aid=self.db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,NULL,'FINAL_VIDEO',?,?,?,NULL,'ACTIVE',?,?)",(pid,str(final),r['output_sha256'],version,now(),expires)).lastrowid
     self._event(pid,'ARTIFACT_ADDED',{'artifact':aid,'kind':'FINAL_VIDEO'})
     self.db.execute('insert into final_assemblies values(?,?,?,?,?,?)',(aid,pid,r['project_version'],r['evidence_sha256'],r['manifest_json'],now())); self.db.execute("update publication_journal set state='COMMITTED',artifact_id=?,updated_at=?,error=NULL where token=? and state='PREPARED'",(aid,now(),r['token'])); self._event(pid,'FINAL_ASSEMBLED',{'artifact':aid,'evidence_sha256':r['evidence_sha256'],'publication_token':r['token']})
 def import_srt(self,pid,cues,role=Role.OPERATOR):
  self._role(role,'import'); self._active(pid)
  rows=list(cues)
  with self.db.transaction():
   self._active(pid)
   # Validate against the narration owned by this mutation's write snapshot.
   audio=self.db.one('select duration_ms from audio where project_id=?',(pid,))
   validate_timing(audio['duration_ms'] if audio else 0,rows)
   old=[(x['start_ms'],x['end_ms'],x['text']) for x in self.db.all('select * from cues where project_id=? order by idx',(pid,))]
   if old==rows:return
   if old:self._invalidate(pid,'srt changed')
   self.db.execute("delete from cues where project_id=?",(pid,))
   self.db.conn.executemany("insert into cues(project_id,idx,start_ms,end_ms,text) values(?,?,?,?,?)",[(pid,i,s,e,t) for i,(s,e,t) in enumerate(rows,1)])
   self._event(pid,"SRT_IMPORTED")
 def import_document(self,pid,kind,source,role=Role.OPERATOR,remote=None):
  self._role(role,'import'); self._active(pid); normalized=normalize_document(kind,source,remote)
  canonical=json.dumps({'kind':kind,'document':normalized},sort_keys=True,separators=(',',':'))
  with self.db.transaction():
   self._active(pid)
   old=self.db.one("select value_json from learning_metadata where project_id=? and key='normalized_input' order by id desc limit 1",(pid,))
   if old and old['value_json']==canonical:return normalized
   if old or self.db.one('select 1 from scenes where project_id=?',(pid,)):self._invalidate(pid,'document changed')
   self.db.execute("insert into learning_metadata(project_id,key,value_json,created_at) values(?,?,?,?)",(pid,'normalized_input',canonical,now()))
   self._event(pid,'SCRIPT_SOURCE_IMPORTED',{'kind':kind})
  return normalized
 def _planning_snapshot(self,pid):
  # Full source rows matter: first imports need not advance project.version.
  return (dict(self.db.one('select * from projects where id=?',(pid,))),
   [dict(x) for x in self.db.all('select * from audio where project_id=?',(pid,))],
   [dict(x) for x in self.db.all('select * from cues where project_id=? order by idx',(pid,))],
   [dict(x) for x in self.db.all('select * from scenes where project_id=? order by ord',(pid,))],
   self.db.one("select coalesce(max(id),0) id from events where project_id=? and type='SCENES_PLANNED'",(pid,))['id'])
 def plan_scenes(self,pid,special_codes=(),role=Role.OPERATOR):
  self._role(role,'plan'); special_codes=tuple(special_codes)
  with self.db.transaction(write=False):
   self._active(pid); expected=self._planning_snapshot(pid)
  project,audios,cues,existing,_=expected; audio=audios[0] if audios else None
  validate_local_output(json.loads(project['output_json']),project['transition'])
  local_visual_frames(audio['duration_ms'] if audio else 0,[(x['start_ms'],x['end_ms']) for x in cues])
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
  local_visual_frames(audio['duration_ms'],[(s,e) for s,e,_,_ in planned])
  if not project['min_scenes']<=len(planned)<=project['max_scenes']: raise ValueError(f"scene count {len(planned)} outside configured {project['min_scenes']}-{project['max_scenes']}")
  with self.db.transaction():
   self._active(pid)
   if self._planning_snapshot(pid)!=expected: raise PermissionError('stale planning source/revision; retry planning')
   # Replanning is a canonical-input mutation: fence old-version work and lineage.
   if existing:self._invalidate(pid,'scene plan changed')
   self.db.execute("delete from scenes where project_id=?",(pid,))
   for i,(s,e,text,exc) in enumerate(planned,1):
    code=f"S{i:03d}"; special=code in special_codes; gate=required_approval(code,special)
    self.db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,special,state,approval_state,duration_exception) values(?,?,?,?,?,?,?,?,?,?)",(pid,code,i,s,e,text,special,"PLANNED","REQUIRED" if gate else "NOT_REQUIRED",exc))
   self._event(pid,"SCENES_PLANNED",{"count":len(planned)}); self.checkpoint(pid,'plan',{'count':len(planned)},role)
   result=[dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))]
  return result
 def project(self,pid):
  row=self.db.one('select * from projects where id=?',(pid,))
  if row is None: raise ValueError('project not found')
  return dict(row)
 def scene(self,sid):
  row=self.db.one('select * from scenes where id=?',(sid,))
  if row is None: raise ValueError('scene not found')
  return dict(row)
 def _attempt_epoch(self,pid):
  return self.db.one("select coalesce(max(id),0) epoch from events where project_id=? and type='IMAGE_EPOCH_STARTED'",(pid,))['epoch']
 def _attempt_count(self,sid):
  s=self.scene(sid); epoch=self._attempt_epoch(s['project_id'])
  return self.db.one('select count(*) n from attempts where project_id=? and scene_code=? and epoch=?',(s['project_id'],s['code'],epoch))['n']
 def queue_retry(self,sid,role=Role.OPERATOR):
  self._role(role,'retry')
  with self.db.transaction():
   s=self.scene(sid); self._active(s['project_id'])
   if self._attempt_count(sid)>=3: raise ValueError('maximum 3 attempts')
   if self.db.execute("update scenes set state='RETRY_QUEUED' where id=? and state in ('RETRYABLE','BLOCKED')",(sid,)).rowcount!=1: raise PermissionError('illegal retry transition')
   self._create_job(s['project_id'],f'RETRY_IMAGE:{sid}')
  return self.scene(sid)
 def allocate_attempt_scratch(self,sid,payload,role=Role.OPERATOR):
  from .scratch import allocate
  self._role(role,'retry'); return allocate(self,sid,payload)
 def _scratch_crash(self,point): pass
 def record_image_attempt(self,sid,success,failed_binary=None,error=None,provider="codex-gpt-image-2",role=Role.OPERATOR,*,scratch_receipt=None):
  from .scratch import consume
  self._role(role,'retry')
  if scratch_receipt and self.db._tx_depth: raise RuntimeError('scratch consumption requires an independent journal transaction')
  if scratch_receipt and failed_binary is not None: raise ValueError('choose scratch receipt or diagnostic path')
  with self.db.transaction():
   scene=self.db.one("select * from scenes where id=?",(sid,))
   if not scene: raise ValueError('scene not found')
   self._active(scene['project_id']); n=self._attempt_count(sid)+1
   if n>3: raise ValueError('maximum 3 attempts')
   if scene['state'] not in ('PLANNED','RETRY_QUEUED','RETRYABLE'): raise PermissionError('illegal image attempt transition')
   if scene['ord']>5 and not scene['special'] and not self._pilot_ready(scene['project_id']): raise PermissionError('pilot approval required before batch images')
   path=digest=size=None
   if scratch_receipt:
    receipt=consume(self,scene,n,scratch_receipt,success)
    path,digest,size=receipt['path'],receipt['sha256'],receipt['size']
   elif failed_binary:
    # Diagnostic only: pinned no-follow regular-file reads, never deletion.
    try:
     fd,name=self._publication_parent_fd(scene['project_id'],failed_binary)
     try:
      identity,digest=snapshot_at(fd,name); size=identity[5]
      candidate=Path(failed_binary); path=str(candidate if candidate.is_absolute() else self._root_lookup(scene['project_id'])/candidate)
     finally: os.close(fd)
    except (OSError,ValueError): path=digest=size=None
   state="SUCCEEDED" if success else "FAILED"
   aid=self.db.execute("insert into attempts(scene_id,number,provider,state,error,failed_path,failed_sha256,failed_size,created_at,project_id,scene_code,epoch) values(?,?,?,?,?,?,?,?,?,?,?,?)",(sid,n,provider,state,error,path,digest,size,now(),scene['project_id'],scene['code'],self._attempt_epoch(scene['project_id']))).lastrowid
   self.db.execute("update scenes set state=?,checkpoint_json=? where id=?",("IMAGE_READY" if success else ("BLOCKED" if n==3 else "RETRYABLE"),json.dumps({'attempt':n}),sid)); self._event(scene['project_id'],"IMAGE_ATTEMPT",{"scene":scene['code'],"number":n,"state":state,'epoch':self._attempt_epoch(scene['project_id']),'attempt_id':aid})
   if scratch_receipt:self._event(scene['project_id'],'SCRATCH_CONSUMED',{'token':scratch_receipt,'attempt_id':aid,'success':bool(success)})
  if scratch_receipt:
   self._scratch_crash('after_attempt_commit')
   self.reconcile_deletions()
 def record_scene_qa(self,sid,evidence,role=Role.REVIEWER):
  self._role(role,'approve')
  if not isinstance(evidence,QAEvidence): raise TypeError('QAEvidence required')
  with self.db.transaction():
   s=self.db.one('select * from scenes where id=?',(sid,))
   if s is None: raise ValueError('scene not found')
   self._active(s['project_id'])
   if s['state']!='IMAGE_READY': raise PermissionError('QA requires ready image')
   project=self.db.one('select * from projects where id=?',(s['project_id'],))
   artifacts=self.db.all('select * from artifacts where scene_id=?',(sid,))
   subject=image_subject(s,artifacts)
   if subject is None: raise PermissionError('QA requires exactly one current IMAGE artifact')
   artifact=next(a for a in artifacts if a['id']==subject['artifact_id'])
   if self._hash(self._owned_path(s['project_id'],artifact['uri']))[0]!=subject['sha256']: raise PermissionError('QA image checksum mismatch')
   payload={'schema_version':2,'subject':subject,'dependency_revision':scene_dependency_revision(project,s),'checks':dict(evidence.checks),'score':evidence.score,'evaluator':evidence.evaluator}
   self._fence_evidence_mutation(s['project_id'],'QA evidence changed',scene_id=sid)
   self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and status='ACTIVE' and ((scene_id=? and kind in ('ANIMATION','SCENE_VIDEO')) or kind in ('FINAL_VIDEO','CONTACT_SHEET'))",(s['project_id'],sid))
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
  artifacts=[dict(x) for x in self.db.all("select id,scene_id,kind,sha256,version,parent_id,status from artifacts where scene_id=? and status='ACTIVE' order by id",(sid,))]
  return scene_evidence_hash(s,artifacts)
 def _pilot_ready(self,pid):
  if not self._planned_ready(pid): return False
  project=self.db.one('select * from projects where id=?',(pid,))
  artifacts=self.db.all('select * from artifacts where project_id=?',(pid,))
  approvals=self.db.all('select * from approvals where project_id=?',(pid,))
  for s in self.db.all("select * from scenes where project_id=? and (ord<=5 or special=1)",(pid,)):
   if not scene_approval_current(project,s,artifacts,approvals): return False
  return True
 def start_batch(self,pid,role=Role.OPERATOR):
  self._role(role,'batch')
  with self.db.transaction():
   self._active(pid)
   if not self._pilot_ready(pid): raise PermissionError('pilot S001-S005 and special representatives require approval')
   return self._create_job(pid,'BATCH_IMAGE')
 def _contact_snapshot(self,pid):
  return (self._planning_snapshot(pid),
   [dict(x) for x in self.db.all('select * from artifacts where project_id=? order by id',(pid,))],
   [dict(x) for x in self.db.all('select * from approvals where project_id=? order by id',(pid,))])
 def approve_post_batch(self,pid,ai_qa,contact_sheet,actor,role=Role.REVIEWER):
  self._role(role,'approve'); self._copy_boundary()
  if not isinstance(ai_qa,QAEvidence) or not ai_qa.passed or not contact_sheet or not os.path.isfile(contact_sheet): raise ValueError('typed AI QA evidence and persisted contact sheet required')
  with self.db.transaction(write=False):
   self._active(pid)
   if not self._planned_ready(pid): raise PermissionError('valid audio/SRT and at least one planned scene required')
   if self.db.one("select 1 from scenes where project_id=? and (state!='IMAGE_READY' or qa_state!='PASS')",(pid,)): raise PermissionError('complete batch and every scene QA PASS required')
   expected=self._contact_snapshot(pid)
  with self._managed_copy(pid,contact_sheet) as (managed,fd,identity):
   digest,_=self._hash(managed)
   with self.db.transaction():
    self._active(pid)
    if self._contact_snapshot(pid)!=expected: raise PermissionError('stale contact project/evidence/scene identity; retry review')
    self._copy_recheck(managed,fd,identity)
    project=self.db.one('select * from projects where id=?',(pid,))
    artifacts=self.db.all('select * from artifacts where project_id=?',(pid,))
    if not self._pilot_ready(pid) or any(not scene_qa_current(project,s,artifacts) for s in self.db.all('select * from scenes where project_id=?',(pid,))): raise PermissionError('current scene QA/approval required')
    aid=self._register_artifact(pid,'CONTACT_SHEET',managed,digest)
    version=project['version']; evidence=self._manifest_hash(pid)
    approval=self.db.execute("insert into approvals(project_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,?,?,?,?,?,?)",(pid,'POST_BATCH','APPROVED',actor,now(),version,evidence)).lastrowid
    self._event(pid,'POST_BATCH_QA_RECORDED',{'schema_version':2,'approval_id':approval,'project_version':version,'evidence_sha256':evidence,'subject':{'artifact_id':aid,'sha256':digest},'checks':ai_qa.checks,'score':ai_qa.score,'evaluator':ai_qa.evaluator})
    a=self.db.one('select * from artifacts where id=?',(aid,))
    result=ContactSheetArtifact(a['id'],a['project_id'],a['uri'],a['sha256'],a['version'],a['status'])
  return result
 def _manifest_hash(self,pid):
  project=dict(self.db.one('select language,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,min_scenes,max_scenes,output_json,version from projects where id=?',(pid,)))
  scenes=[dict(x) for x in self.db.all('select id,code,ord,start_ms,end_ms,text,special,state,approval_state,qa_state,qa_json,duration_exception,continuity_json,checkpoint_json from scenes where project_id=? order by ord',(pid,))]
  artifacts=[dict(x) for x in self.db.all('select id,scene_id,kind,sha256,version,parent_id,status from artifacts where project_id=? order by id',(pid,))]
  return manifest_evidence_hash(project,scenes,artifacts)
 def start_animation(self,pid,role=Role.OPERATOR):
  self._role(role,'animate')
  with self.db.transaction():
   self._active(pid)
   if not self._planned_ready(pid): raise PermissionError('valid audio/SRT and at least one planned scene required')
   version=self.db.one('select version from projects where id=?',(pid,))['version']
   if not self.db.one("select 1 from approvals where project_id=? and gate='POST_BATCH' and decision='APPROVED' and revoked_at is null and project_version=? and evidence_sha256=?",(pid,version,self._manifest_hash(pid))): raise PermissionError('current post-batch human approval required')
   return self._create_job(pid,'ANIMATION')
 def _active(self,pid):
  p=self.db.one('select state from projects where id=?',(pid,))
  if not p or p['state']!='ACTIVE': raise PermissionError('project is not active')
 def _create_job(self,pid,kind):
  # Gated callers hold this same write transaction across evidence and admission.
  with self.db.transaction():
   self._active(pid); t=now(); version=self.db.one('select version from projects where id=?',(pid,))['version']
   old=self.db.one("select id from jobs where project_id=? and kind=? and project_version=? and state in ('QUEUED','RUNNING','PAUSED')",(pid,kind,version))
   if old:return old['id']
   return self.db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",(pid,kind,'QUEUED',t,t,version)).lastrowid
 def create_job(self,*args,**kwargs):
  raise PermissionError('jobs must be created through a gated workflow')
 def pause_job(self,jid,role=Role.OPERATOR):
  self._role(role,'pause')
  if self.db.execute("update jobs set state='PAUSED',updated_at=? where id=? and state in ('QUEUED','RUNNING')",(now(),jid)).rowcount!=1: raise PermissionError('illegal job pause transition')
 def _resume_gate(self,job):
  """Current prerequisites, not historical admission, authorize requeue.

  PIPELINE and OWNER planning are orchestration (may precede source import).
  OWNER image starts a fresh pilot, unlike BATCH_IMAGE. QA needs current images;
  animation/assembly need current POST_BATCH; upload needs current final review.
  Unknown kinds and any residual lease fail closed (no worker takeover API).
  Caller holds the writer transaction, including project bulk resume.
  """
  pid=job['project_id']; kind=job['kind']
  if job['state']!='PAUSED' or job['project_version']!=self.project(pid)['version']:
   raise PermissionError('illegal job resume transition')
  if job['lease_owner'] is not None or job['lease_expires_at'] is not None:
   raise PermissionError('job lease requires explicit reconciliation before resume')
  if kind in ('PIPELINE','RERUN:planning'): return
  if not self._planned_ready(pid): raise PermissionError('current valid plan required for resume')
  if kind=='RERUN:image': return
  if kind=='BATCH_IMAGE':
   if not self._pilot_ready(pid): raise PermissionError('current pilot approval required for resume')
  elif kind.startswith('RETRY_IMAGE:'):
   try: sid=int(kind.split(':',1)[1]); scene=self.scene(sid)
   except ValueError as exc: raise PermissionError('invalid retry scene') from exc
   if scene['project_id']!=pid or scene['state']!='RETRY_QUEUED' or self._attempt_count(sid)>=3:
    raise PermissionError('current retry scene/budget required')
   if scene['ord']>5 and not scene['special'] and not self._pilot_ready(pid): raise PermissionError('current pilot approval required for retry')
  elif kind in ('ANIMATION','RERUN:animation','RERUN:assembly'):
   version=self.project(pid)['version']
   if not self.db.one("select 1 from approvals where project_id=? and gate='POST_BATCH' and decision='APPROVED' and revoked_at is null and project_version=? and evidence_sha256=?",(pid,version,self._manifest_hash(pid))): raise PermissionError('current post-batch human approval required')
  elif kind=='RERUN:qa':
   artifacts=self.db.all('select * from artifacts where project_id=?',(pid,))
   if any(s['state']!='IMAGE_READY' or image_subject(s,artifacts) is None for s in self.db.all('select * from scenes where project_id=?',(pid,))): raise PermissionError('current ready images required for QA')
  elif kind=='RERUN:upload':
   if not self._status_summary_snapshot(pid)['gates']['final']['current']: raise PermissionError('current final approval required for upload')
  else: raise PermissionError('unsupported job resume kind')
 def resume_job(self,jid,role=Role.OPERATOR):
  self._role(role,'resume')
  with self.db.transaction():
   job=self.db.one('select * from jobs where id=?',(jid,))
   if not job: raise PermissionError('illegal job resume transition')
   self._active(job['project_id']); self._resume_gate(job)
   if self.db.execute("update jobs set state='QUEUED',updated_at=? where id=? and state='PAUSED' and project_version=? and lease_owner is null and lease_expires_at is null",(now(),jid,job['project_version'])).rowcount!=1: raise PermissionError('illegal job resume transition')
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
    while self._attempt_count(sid)<3:
     try: success=bool(next(sequence))
     except StopIteration: break
     self.record_image_attempt(sid,success,error=None if success else 'fake failure')
     if success: break
   finally: self.scheduler.release(kind)
  return [self.scene(s) for s in scene_ids]
 def checkpoint(self,pid,stage,data,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  with self.db.transaction():
   self._active(pid)
   version=self.db.one('select version from projects where id=?',(pid,))['version']
   job=self.db.one("select * from jobs where project_id=? and project_version=? and state in ('QUEUED','RUNNING','PAUSED') order by id desc",(pid,version))
   jid=job['id'] if job else self._create_job(pid,'PIPELINE')
   payload={'snapshot_version':3,'data':data,'project':dict(self.db.one('select * from projects where id=?',(pid,))),'job':dict(self.db.one('select * from jobs where id=?',(jid,))),'scenes':[dict(x) for x in self.db.all('select * from scenes where project_id=? order by ord',(pid,))],'artifacts':[dict(x) for x in self.db.all('select * from artifacts where project_id=? order by id',(pid,))]}
   payload.update(snapshot_version=4,identity=uuid.uuid4().hex,plan_identity=self._checkpoint_generation(pid))
   snapshot={**data,**payload,'snapshot_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
   self.db.execute("insert into stages(job_id,name,state,checkpoint_json) values(?,?,?,?) on conflict(job_id,name) do update set state=excluded.state,checkpoint_json=excluded.checkpoint_json",(jid,stage,'SUCCEEDED',json.dumps(snapshot)))
 def propose_rerun(self,pid,stage,role=Role.OPERATOR): self._role(role,'retry'); return self.db.execute("insert into proposals(project_id,stage,state,created_at) values(?,?,?,?)",(pid,stage,'PROPOSED',now())).lastrowid
 def _rerun_decision(self,p):
  # Read newest first: never fall back from a stale/invalid decision to an old grant.
  for row in self.db.all("select * from events where project_id=? and type='RERUN_DECIDED' order by id desc",(p['project_id'],)):
   try:
    data=json.loads(row['data_json'])
    if not isinstance(data,dict) or type(data.get('proposal_id')) is not int: raise ValueError('invalid receipt')
   except (ValueError,TypeError) as exc: raise PermissionError('invalid rerun decision provenance') from exc
   if data['proposal_id']==p['id']: return row,data
  return None,None
 def decide_rerun(self,i,approved,actor,role=Role.OWNER,*,reapprove=False):
  self._role(role,'dependency')  # OWNER-only, unlike generic scene approval.
  if type(approved) is not bool or type(reapprove) is not bool: raise ValueError('boolean rerun decision required')
  if not isinstance(actor,str) or not actor.strip(): raise ValueError('rerun decision actor required')
  with self.db.transaction():
   p=self.db.one('select * from proposals where id=?',(i,))
   expected='APPROVED' if reapprove else 'PROPOSED'
   if not p or p['state']!=expected: raise PermissionError('illegal rerun decision; pending approvals require explicit reapprove=True')
   previous,unused=self._rerun_decision(p)
   version=self.db.one('select version from projects where id=?',(p['project_id'],))['version']
   if self.db.execute('update proposals set state=?,actor=? where id=? and state=?',('APPROVED' if approved else 'REJECTED',actor,i,expected)).rowcount!=1: raise PermissionError('illegal rerun decision')
   decided=dict(self.db.one('select * from proposals where id=?',(i,)))
   self._event(p['project_id'],'RERUN_DECIDED',{'format':'rerun-decision-v1','proposal_id':i,'proposal':decided,'role':Role.OWNER.value,'project_version':version,'previous_proposal':dict(p),'previous_decision_event_id':previous['id'] if previous else None})
 def apply_rerun(self,i,role=Role.OWNER):
  from .dependencies import IMPACT,Stage,normalize_stage,job_stage
  self._role(role,'dependency')
  with self.db.transaction():
   p=self.db.one('select * from proposals where id=?',(i,))
   if not p or p['state']!='APPROVED': raise PermissionError('rerun approval required')
   stage=normalize_stage(p['stage']); impact=IMPACT[stage]; pid=p['project_id']; self._active(pid)
   receipt,decision=self._rerun_decision(p)
   version=self.db.one('select version from projects where id=?',(pid,))['version']
   if (not receipt or decision.get('format')!='rerun-decision-v1' or decision.get('role')!=Role.OWNER.value
       or decision.get('proposal')!=dict(p) or type(decision.get('project_version')) is not int
       or decision['project_version']!=version or not isinstance(p['actor'],str) or not p['actor'].strip()):
    raise PermissionError('current OWNER rerun decision provenance required; explicitly reapprove pending proposal')
   decision_id=receipt['id']
   if self.db.execute("update proposals set state='APPLIED' where id=? and state='APPROVED'",(i,)).rowcount!=1: raise PermissionError('rerun already applied')
   # All old-version execution is fenced, including unknown job kinds. Typed
   # descendants are reported separately; never relabel historical jobs as new.
   jobs=self.db.all("select id,kind from jobs where project_id=? and state in ('QUEUED','RUNNING','PAUSED')",(pid,))
   for job in jobs:
    if stage!=Stage.UPLOAD or job_stage(job['kind']) in impact.jobs:
     self.db.execute("update jobs set state='BLOCKED',updated_at=?,lease_owner=NULL,lease_expires_at=NULL where id=?",(now(),job['id']))
   retired=[]
   for a in self.db.all("select id,kind from artifacts where project_id=? and status='ACTIVE'",(pid,)):
    if a['kind'] in impact.artifacts:
     self.db.execute("update artifacts set status='SUPERSEDED' where id=?",(a['id'],)); retired.append(a['id'])
   gates=set(impact.approvals)
   # Scene approval binds its active clips/state: retire it only where that
   # local evidence actually changed, without discarding IMAGE subject QA.
   if stage==Stage.ANIMATION:
    for s in self.db.all('select * from scenes where project_id=?',(pid,)):
     changed=s['state']=='ANIMATED' or self.db.one('select 1 from artifacts where scene_id=? and id in (%s)' % (','.join('?' for _ in retired) or 'NULL'),(s['id'],*retired))
     if changed:
      self.db.execute("update approvals set revoked_at=?,revoked_reason='animation rerun' where scene_id=? and gate='SCENE' and revoked_at is null",(now(),s['id']))
      self.db.execute("update scenes set state='IMAGE_READY',approval_state=case when special=1 or ord<=5 then 'REQUIRED' else 'NOT_REQUIRED' end where id=?",(s['id'],))
   for gate in gates:
    self.db.execute("update approvals set revoked_at=?,revoked_reason=? where project_id=? and gate=? and revoked_at is null",(now(),'rerun:'+stage.value,pid,gate))
   if impact.reset_qa:
    self.db.execute("update scenes set qa_state='PENDING',qa_json='{}',approval_state=case when special=1 or ord<=5 then 'REQUIRED' else 'NOT_REQUIRED' end,state=case when state='ANIMATED' then 'IMAGE_READY' else state end where project_id=?",(pid,))
   if impact.new_epoch:
    self._event(pid,'IMAGE_EPOCH_STARTED',{'proposal_id':i,'decision_event_id':decision_id,'stage':stage.value,'budget':3})
    self.db.execute("update scenes set state='PLANNED',checkpoint_json='{}' where project_id=?",(pid,))
   if stage!=Stage.UPLOAD:self.db.execute('update projects set version=version+1 where id=?',(pid,))
   self._event(pid,'RERUN_APPLIED',{'proposal_id':i,'decision_event_id':decision_id,'stage':stage.value,'artifacts':retired,'approval_gates':sorted(gates),'dependent_jobs':[j['id'] for j in jobs if job_stage(j['kind']) in impact.jobs],'execution_fence':stage!=Stage.UPLOAD})
   return self._create_job(pid,'RERUN:'+stage.value)
 def _copy_boundary(self):
  if self.db.conn.in_transaction:
   raise PermissionError('managed copy requires a top-level operation; caller-owned transaction nesting is unsupported')
 @contextmanager
 def _managed_copy(self,pid,uri):
  # This boundary owns bytes until the enclosing top-level SQL commit returns.
  # No durable receipt: process-crash residuals are unknown and MUST be retained.
  self._copy_boundary()
  root=self._root_lookup(pid); name=uuid.uuid4().hex+Path(uri).suffix
  managed=root/name; fd=open_dir_beneath(root,Path('.')); identity=None; inode_fd=None; completed=None
  try:
   with os.fdopen(os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd),'wb') as dst:
    st=os.fstat(dst.fileno()); identity=(st.st_dev,st.st_ino)
    inode_fd=os.dup(dst.fileno())  # prevent inode reuse until compensation finishes
    with open(uri,'rb') as src: shutil.copyfileobj(src,dst)
   st=os.fstat(inode_fd); completed=(st.st_size,st.st_mtime_ns,st.st_ctime_ns)
   yield managed,fd,identity
  except BaseException:
   if identity is not None:
    try:
     with self.db.transaction():
      current=os.stat(name,dir_fd=fd,follow_symlinks=False)
      referenced=False
      for row in self.db.all('select uri from artifacts union all select uri from audio'):
       try:
        st=os.stat(row['uri'])
        if (st.st_dev,st.st_ino)==identity: referenced=True; break
       except OSError:
        if row['uri']==str(managed): referenced=True; break
      if (current.st_dev,current.st_ino)==identity and current.st_nlink==1 and not referenced and completed==(current.st_size,current.st_mtime_ns,current.st_ctime_ns):
       os.unlink(name,dir_fd=fd)
    except (OSError,PermissionError): pass
   raise
  finally:
   if inode_fd is not None: os.close(inode_fd)
   os.close(fd)
 def _copy_recheck(self,managed,fd,identity):
  current=os.stat(managed.name,dir_fd=fd,follow_symlinks=False)
  resolved=managed.stat(follow_symlinks=False)
  if (current.st_dev,current.st_ino)!=identity or (resolved.st_dev,resolved.st_ino)!=identity:
   raise PermissionError('managed copy identity changed')
 def _register_artifact(self,pid,kind,managed,digest,scene_id=None,parent_id=None):
  # SQL-only composition: never copies, never owns caller bytes, never commits.
  if not self.db.conn.in_transaction: raise RuntimeError('artifact registration requires transaction')
  version=self.db.one("select coalesce(max(version),0)+1 v from artifacts where project_id=? and kind=? and scene_id is ? and detached_scene_id is null",(pid,kind,scene_id))['v']; days=self.db.one('select retention_days from projects where id=?',(pid,))['retention_days']; expires=(datetime.now(timezone.utc)+timedelta(days=days)).isoformat()
  aid=self.db.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,parent_id,status,created_at,expires_at) values(?,?,?,?,?,?,?,?,?,?)",(pid,scene_id,kind,str(managed),digest,version,parent_id,"ACTIVE",now(),expires)).lastrowid
  if kind=='IMAGE' and scene_id is not None:
   self._fence_evidence_mutation(pid,'image subject changed',scene_id=scene_id)
   self.db.execute("update scenes set qa_state='PENDING',qa_json='{}',state=case when state='ANIMATED' then 'IMAGE_READY' else state end where id=?",(scene_id,))
   self.db.execute("update artifacts set status='SUPERSEDED' where project_id=? and status='ACTIVE' and ((scene_id=? and kind in ('ANIMATION','SCENE_VIDEO')) or kind in ('FINAL_VIDEO','CONTACT_SHEET'))",(pid,scene_id))
  self._event(pid,'ARTIFACT_ADDED',{'artifact':aid,'kind':kind})
  return aid
 def add_artifact(self,pid,kind,uri,scene_id=None,parent_id=None,role=Role.OPERATOR):
  self._role(role,'scene-replace'); source=Path(uri).resolve(strict=True)
  with self.db.transaction(write=False):
   self.project(pid); self._active(pid)
   if scene_id is not None and self.scene(scene_id)['project_id']!=pid: raise ValueError('scene project mismatch')
   if parent_id is not None:
    parent=self.db.one('select project_id from artifacts where id=?',(parent_id,))
    if parent is None or parent['project_id']!=pid: raise ValueError('parent artifact not found in project')
   expected=self._contact_snapshot(pid)
  root=self._root_lookup(pid)
  # Copy-free managed registration remains composable. Recheck ownership under
  # the writer; a racing registration retries as a top-level exclusive copy.
  if source.is_relative_to(root):
   with self.db.transaction():
    self._artifact_recheck(pid,expected)
    if not self.db.one('select 1 from artifacts where uri=?',(str(source),)):
     digest,_=self._hash(source)
     return self._register_artifact(pid,kind,source,digest,scene_id,parent_id)
  self._copy_boundary()
  with self._managed_copy(pid,source) as (managed,fd,identity):
   digest,_=self._hash(managed)
   with self.db.transaction():
    self._artifact_recheck(pid,expected)
    self._copy_recheck(managed,fd,identity)
    return self._register_artifact(pid,kind,managed,digest,scene_id,parent_id)
 def _artifact_recheck(self,pid,expected):
  # Plan event identity prevents ABA from reusable scene integers, even when
  # text is identical. Full source/evidence rows fence first imports and QA.
  self._active(pid)
  if self._contact_snapshot(pid)!=expected: raise PermissionError('stale artifact project/evidence/scene identity; retry')
 def _checkpoint_generation(self,pid):
  return self.db.one("select coalesce(max(id),0) id from events where project_id=? and type='SCENES_PLANNED'",(pid,))['id']
 def _restore_snapshot(self,pid):
  # Full rows include lifecycle, QA/approval, deletion status and v14 provenance.
  return (self._planning_snapshot(pid),
   [dict(x) for x in self.db.all('select * from artifacts where project_id=? order by id',(pid,))],
   [dict(x) for x in self.db.all('select * from approvals where project_id=? order by id',(pid,))],
   [dict(x) for x in self.db.all('select stages.* from stages join jobs on jobs.id=stages.job_id where jobs.project_id=? order by stages.id',(pid,))],
   [dict(x) for x in self.db.all('select * from jobs where project_id=? order by id',(pid,))])
 @contextmanager
 def _restore_bytes(self,pid,artifacts):
  # Hash outside our writer; no byte mutation, hence no copy compensation.
  with ExitStack() as stack:
   pins=[]
   for a in artifacts:
    fd,name=self._publication_parent_fd(pid,a['uri']); stack.callback(os.close,fd)
    identity,digest=snapshot_at(fd,name)
    if digest!=a['sha256']: raise ValueError('restore artifact binary checksum mismatch')
    pins.append((a['uri'],fd,name,identity))
   yield pins
 def _restore_recheck(self,pid,expected,pins):
  if self._restore_snapshot(pid)!=expected: raise PermissionError('stale restore project/evidence/inventory/checkpoint; retry')
  for uri,fd,name,identity in pins:
   assert_identity_at(fd,name,identity)
   # Re-open only directories to prove the public namespace still names our pin.
   current,leaf=self._publication_parent_fd(pid,uri)
   try:
    if (os.fstat(current).st_dev,os.fstat(current).st_ino)!=(os.fstat(fd).st_dev,os.fstat(fd).st_ino): raise PermissionError('restore parent substituted')
    assert_identity_at(current,leaf,identity)
   finally: os.close(current)
 def restore_artifact(self,artifact_id,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  with self.db.transaction(write=False):
   row=self.db.one('select * from artifacts where id=?',(artifact_id,))
   if not row or row['status']=='DELETED': raise FileNotFoundError('artifact binary unavailable')
   a=dict(row); expected=self._restore_snapshot(a['project_id'])
  with self._restore_bytes(a['project_id'],[a]) as pins:
   with self.db.transaction():
    self._restore_recheck(a['project_id'],expected,pins)
    self._invalidate(a['project_id'],'artifact restored',clear_plan=False)
    # Content only; stopped lifecycle and detached provenance remain untouched.
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
 def _deletion_live(self,uri):
  # URI ownership is global, not just the retiring project's view.
  return (self.db.one("select 1 from artifacts where uri=? and status!='DELETED' limit 1",(uri,))
          or self.db.one('select 1 from audio where uri=? limit 1',(uri,)))
 def _deletion_snapshot(self,fd,name):
  try:return snapshot_at(fd,name)
  except FileNotFoundError:return None
 def _deletion_move(self,fd,source,destination,identity):
  assert_identity_at(fd,source,identity)
  rename_noreplace_at(fd,source,fd,destination)
  # Never destroy a leaf substituted between verification and rename.
  assert_identity_at(fd,destination,identity)
 def _deletion_unlink(self,fd,name,identity):
  assert_identity_at(fd,name,identity)
  os.unlink(name,dir_fd=fd)
 def _cleanup(self,token):
  if self.db._tx_depth: raise RuntimeError('cleanup requires an independent journal transaction')
  immutable=('INPUT','MANIFEST','DB','PROMPT','BIBLE','QA_METADATA','CHECKSUM','FINAL')
  rows=self.db.all("select * from artifacts where status in ('ACTIVE','SUPERSEDED') and expires_at<? and upper(kind) not in (%s)" % ','.join('?'*len(immutable)),(now(),*immutable))
  count=0
  for r in rows:
   try: fd,name=self._publication_parent_fd(r['project_id'],r['uri'])
   except (ValueError,OSError): continue
   staged=name+'.deleting-f01-'+uuid.uuid4().hex
   moved=False
   try:
    try: before=self._deletion_snapshot(fd,name)
    except OSError: continue
    if before and before[1]!=r['sha256']: continue
    # Append-only durable ownership receipt BEFORE the rename transaction.
    # It survives rollback/crash; artifact status remains the decision authority.
    if before:
     with self.db.transaction():
      self.db.assert_reconciliation_lease(token)
      self._event(r['project_id'],'DELETION_PREPARED',{'uri':r['uri'],'staged':staged,'identity':before[0],'sha256':before[1],'artifact':r['id']})
    try:
     with self.db.transaction():
      self.db.assert_reconciliation_lease(token)
      current=self.db.one('select * from artifacts where id=?',(r['id'],))
      if not current or any(current[k]!=r[k] for k in ('uri','sha256','status','expires_at')): continue
      changed=self.db.execute("update artifacts set status='DELETED',deleted_at=? where id=? and status=?",(now(),r['id'],r['status'])).rowcount
      if not changed: continue
      if before and not self._deletion_live(r['uri']):
       self._deletion_move(fd,name,staged,before[0]); moved=True
    except Exception:
     # Retain the same pinned parent through rollback. Collision/lost fencing
     # leaves the receipt and payload for reconciliation, never overwrites.
     if moved:
      try:
       with self.db.transaction():
        self.db.assert_reconciliation_lease(token)
        self._deletion_move(fd,staged,name,before[0])
      except Exception: pass
     raise
    if moved:
     with self.db.transaction():
      self.db.assert_reconciliation_lease(token)
      if not self._deletion_live(r['uri']):
       self._deletion_unlink(fd,staged,before[0])
    count+=1
   finally: os.close(fd)
  return count
 def reconcile_deletions(self,_lease_token=None):
  """Resolve rename/commit/unlink crash windows with DB status as authority."""
  owned=_lease_token is None
  token=_lease_token or self.db.acquire_reconciliation_lease()
  try:
   from .scratch import reconcile
   reconcile(self,token)
   return self._reconcile_deletions(token)
  finally:
   if owned:self.db.release_reconciliation_lease(token)
 def _reconcile_deletions(self,token):
  """Pinned traversal; receipts bind new staging, hashes gate legacy recovery."""
  for project in self.db.all("select id,artifact_root from projects where artifact_root is not null"):
   root=Path(project['artifact_root']).absolute()
   try: rootfd=open_dir_beneath(root)
   except OSError: continue
   try:
    receipts={}
    for event in self.db.all("select data_json from events where project_id=? and type='DELETION_PREPARED' order by id",(project['id'],)):
     receipt=json.loads(event['data_json']); receipts[receipt['staged']]=receipt
    # Explicitly close the generator on exceptional exits as well as exhaustion.
    with closing(walk_dirs_at(rootfd)) as directories:
     for relative,fd in directories:
      for staged in sorted(os.listdir(fd)):
       if '.deleting-' not in staged: continue
       name=staged.rsplit('.deleting-',1)[0]
       if not name: continue
       uri=str(root/relative/name)
       self.db.renew_reconciliation_lease(token)
       try:
        with self.db.transaction():
         self.db.assert_reconciliation_lease(token)
         rows=self.db.all('select status,sha256 from artifacts where uri=? order by id',(uri,))
         if not rows: continue
         candidate=self._deletion_snapshot(fd,staged)
         if not candidate: continue
         identity,digest=candidate
         receipt=receipts.get(staged)
         if staged.startswith(name+'.deleting-f01-') and not receipt: continue
         if receipt and (receipt['uri']!=uri or tuple(receipt['identity'])!=identity or receipt['sha256']!=digest): continue
         # Legacy staging has no receipt: exact tracked bytes are mandatory,
         # including all-DELETED rows. A suffix alone never authorizes deletion.
         if digest not in {row['sha256'] for row in rows}: continue
         live=[row for row in rows if row['status']!='DELETED']
         audio=self.db.one('select sha256 from audio where uri=?',(uri,))
         if not live and not audio:
          self.db.assert_reconciliation_lease(token)
          self._deletion_unlink(fd,staged,identity)
          continue
         live_hashes={row['sha256'] for row in live}
         if audio: live_hashes.add(audio['sha256'])
         if digest not in live_hashes: continue
         original=self._deletion_snapshot(fd,name)
         self.db.assert_reconciliation_lease(token)
         if original is None:
          self._deletion_move(fd,staged,name,identity)
         elif original[1]==digest:
          assert_identity_at(fd,name,original[0])
          self._deletion_unlink(fd,staged,identity)
       except OSError:
        # Collision, symlink, changed identity or unknown bytes: retain for review.
        continue
   finally: os.close(rootfd)
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
   s=self.db.one('select * from scenes where id=?',(s['id'],))
   project=self.db.one('select * from projects where id=?',(pid,))
   artifacts=self.db.all('select * from artifacts where scene_id=?',(s['id'],))
   if decision=='APPROVED':
    if not scene_qa_current(project,s,artifacts): raise PermissionError('current subject-bound QA required')
    subject=image_subject(s,artifacts); artifact=next(a for a in artifacts if a['id']==subject['artifact_id'])
    if self._hash(self._owned_path(pid,artifact['uri']))[0]!=subject['sha256']: raise PermissionError('QA image checksum mismatch')
   self.db.execute("update approvals set revoked_at=?,revoked_reason='scene decision replaced' where project_id=? and scene_id=? and gate='SCENE' and revoked_at is null",(now(),pid,s['id']))
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
  value=json.dumps({'preset':preset,'config':presets[preset].__dict__},sort_keys=True)
  with self.db.transaction():
   self._active(pid)
   old=self.db.one('select output_json from projects where id=?',(pid,))['output_json']
   if json.loads(old)==json.loads(value):return
   self._invalidate(pid,'output preset changed',clear_plan=False)
   self.db.execute('update projects set output_json=? where id=?',(value,pid))
 def configure_project(self,pid,key,value,role=Role.OWNER):
  self._role(role,'project-config'); self._active(pid); columns={'whiteboard_mode','transition','language'}
  if key not in columns: raise ValueError('unsupported configuration key')
  if key=='transition': validate_local_output({},value)
  with self.db.transaction():
   self._active(pid)
   if self.db.one(f'select {key} from projects where id=?',(pid,))[key]==value:return
   self._invalidate(pid,f'project configuration changed: {key}',clear_plan=key=='language')
   self.db.execute(f'update projects set {key}=? where id=?',(value,pid))
 def _final_subject(self,pid,aid,assembler=None):
  from .media import AssemblyError,LocalFinalAssembler
  from .evidence import digest
  self._active(pid)
  row=self.db.one("select a.*,f.manifest_json,f.project_version assembly_version,f.evidence_sha256 assembly_evidence from artifacts a join final_assemblies f on f.artifact_id=a.id where a.project_id=? and a.id=? and a.kind='FINAL_VIDEO' and a.status='ACTIVE'",(pid,aid))
  if not row: raise PermissionError('current registered FINAL_VIDEO required')
  manifest=json.loads(row['manifest_json']); payload={k:v for k,v in manifest.items() if k!='manifest_sha256'}
  if digest(payload)!=manifest.get('manifest_sha256') or manifest.get('input_identity',{}).get('schema_version')!=2 or digest(manifest['input_identity'])!=row['assembly_evidence']: raise PermissionError('legacy or invalid final manifest')
  version=self.db.one('select version from projects where id=?',(pid,))['version']
  if version!=row['assembly_version'] or self._manifest_hash(pid)!=manifest['current_evidence_sha256']: raise PermissionError('stale final input evidence')
  # Re-enter full input gates and byte checks without publishing another output.
  check=self.assemble(pid,self._root_lookup(pid)/('.review-'+uuid.uuid4().hex+'.mp4'),dry_run=True,assembler=assembler)
  if check['manifest']['input_evidence_sha256']!=row['assembly_evidence']: raise PermissionError('stale final input identity')
  path=self._owned_path(pid,row['uri'])
  if self._hash(path)[0]!=row['sha256']: raise AssemblyError('final output checksum mismatch')
  (assembler or LocalFinalAssembler(self._root_lookup(pid))).validate_final(path,manifest['audio']['duration_ms']/1000)
  if self._hash(path)[0]!=row['sha256']: raise AssemblyError('final output changed during QA')
  fresh=self.assemble(pid,self._root_lookup(pid)/('.review-check-'+uuid.uuid4().hex+'.mp4'),dry_run=True,assembler=assembler)
  if fresh['manifest']['input_evidence_sha256']!=row['assembly_evidence']: raise PermissionError('final inputs changed during QA')
  return {'schema_version':2,'project_id':pid,'project_version':version,'artifact_id':aid,'sha256':row['sha256'],'manifest_sha256':manifest['manifest_sha256'],'input_evidence_sha256':row['assembly_evidence'],'current_evidence_sha256':manifest['current_evidence_sha256']}
 def record_final_qa(self,pid,aid,qa,role=Role.REVIEWER,assembler=None):
  self._role(role,'approve')
  if not isinstance(qa,QAEvidence): raise ValueError('typed final QA required')
  with self.db.transaction():
   subject=self._final_subject(pid,aid,assembler)
   receipt={'schema_version':2,'subject':subject,'checks':qa.checks,'score':qa.score,'evaluator':qa.evaluator}
   self.db.execute("update approvals set revoked_at=?,revoked_reason='final QA changed' where project_id=? and gate='FINAL' and revoked_at is null",(now(),pid))
   self._event(pid,'FINAL_QA_RECORDED',receipt)
  return receipt
 def review_final(self,pid,aid,decision,actor,role=Role.REVIEWER,assembler=None):
  from .evidence import digest
  self._role(role,'approve' if decision=='APPROVED' else 'reject')
  if decision not in ('APPROVED','REJECTED') or not actor.strip(): raise ValueError('final decision and actor required')
  with self.db.transaction():
   subject=self._final_subject(pid,aid,assembler)
   row=self.db.one("select id,data_json from events where project_id=? and type='FINAL_QA_RECORDED' order by id desc limit 1",(pid,))
   qa=json.loads(row['data_json']) if row else {}
   if qa.get('subject')!=subject or not QAEvidence(qa['checks'],qa['score'],qa['evaluator']).passed: raise PermissionError('current persisted passing final QA required')
   evidence=digest({'subject':subject,'qa_event_id':row['id'],'qa':qa})
   self.db.execute("update approvals set revoked_at=?,revoked_reason='final review superseded' where project_id=? and gate='FINAL' and revoked_at is null",(now(),pid))
   approval=self.db.execute("insert into approvals(project_id,gate,decision,actor,created_at,project_version,evidence_sha256) values(?,'FINAL',?,?,?,?,?)",(pid,decision,actor,now(),subject['project_version'],evidence)).lastrowid
   self._event(pid,'FINAL_REVIEWED',{'approval_id':approval,'subject':subject,'qa_event_id':row['id'],'qa':qa,'evidence_sha256':evidence})
  return approval
 def decide_gate(self,pid,gate,decision,actor,role=Role.REVIEWER):
  command='approve' if decision=='APPROVED' else 'reject'; self._role(role,command)
  if gate not in {'PILOT','BATCH','FINAL'} or decision not in {'APPROVED','REJECTED'}: raise ValueError('unsupported gate decision')
  raise PermissionError(f'{gate} requires its dedicated evidence-bound review workflow; generic gate decisions are unsupported')
 def replace_scene_artifact(self,sid,uri,role=Role.OPERATOR):
  self._role(role,'scene-replace'); self._copy_boundary()
  with self.db.transaction(write=False):
   s=self.scene(int(sid)); pid=s['project_id']; self._active(pid)
   expected=self._planning_snapshot(pid)
  with self._managed_copy(pid,uri) as (managed,fd,identity):
   digest,_=self._hash(managed)
   with self.db.transaction():
    self._active(pid)
    if self._planning_snapshot(pid)!=expected: raise PermissionError('stale scene identity/revision/dependency; retry replacement')
    self._copy_recheck(managed,fd,identity)
    old=self.db.one("select * from artifacts where scene_id=? and kind='IMAGE' and status='ACTIVE' order by version desc",(s['id'],))
    if old:self.db.execute("update artifacts set status='SUPERSEDED' where id=?",(old['id'],))
    aid=self._register_artifact(pid,'IMAGE',managed,digest,s['id'],old['id'] if old else None)
    self._event(pid,'SCENE_ARTIFACT_REPLACED',{'scene':s['code'],'artifact':aid})
  return aid
 def _command_copy_fence(self,key,request,attempt):
  row=self.db.one('select * from command_copy_requests where idempotency_key=?',(key,))
  if not row or row['request_sha256']!=request or row['attempt']!=attempt or row['state']!='PREPARING' or row['lease_expires_at']<=now():
   raise PermissionError('command copy lease/fence lost; manual review required')
 def _dispatch_copy_command(self,c,role,key,request):
  self._role(role,'scene-replace'); self._copy_boundary()
  if len(c.args)!=2: raise CommandError('INVALID_ARGUMENTS','scene-replace expects 2 arguments')
  try: sid=int(c.args[0])
  except ValueError as e: raise CommandError('INVALID_ARGUMENT','scene id required') from e
  attempt=uuid.uuid4().hex; blocked=None
  with self.db.transaction():
   receipt=self.db.one('select * from command_receipts where idempotency_key=?',(key,))
   if receipt:
    if receipt['request_sha256']!=request: raise PermissionError('idempotency key reused for different request')
    return json.loads(receipt['response_json'])
   row=self.db.one('select * from command_copy_requests where idempotency_key=?',(key,))
   if row:
    if row['request_sha256']!=request: raise PermissionError('idempotency key reused for different request')
    if row['state']=='PREPARING' and row['lease_expires_at']<=now():
     self.db.execute("update command_copy_requests set state='MANUAL_REVIEW',error='EXPIRED_COPY_OWNERSHIP_UNKNOWN' where idempotency_key=?",(key,))
    blocked='command copy in progress' if row['state']=='PREPARING' and row['lease_expires_at']>now() else 'command copy manual review required'
   else:
    s=self.scene(sid); pid=s['project_id']; self._active(pid); expected=self._contact_snapshot(pid)
    self.db.execute("insert into command_copy_requests(idempotency_key,request_sha256,attempt,lease_expires_at,state,expected_json,created_at) values(?,?,?,?,'PREPARING',?,?)",(key,request,attempt,(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),json.dumps(expected),now()))
  if blocked: raise PermissionError(blocked)
  try:
   with self._managed_copy(pid,c.args[1]) as (managed,fd,identity):
    owned,digest=snapshot_at(fd,managed.name)
    if tuple(owned[:2])!=identity: raise PermissionError('managed copy identity changed')
    ctime=os.stat(managed.name,dir_fd=fd,follow_symlinks=False).st_ctime_ns
    from .atomic_fs import fsync_file_at
    fsync_file_at(fd,managed.name,owned); os.fsync(fd)
    preparation=json.dumps({'uri':str(managed),'identity':owned,'ctime_ns':ctime,'sha256':digest})
    with self.db.transaction():
     self._command_copy_fence(key,request,attempt)
     self.db.execute('update command_copy_requests set preparation_json=? where idempotency_key=?',(preparation,key))
    with self.db.transaction():
     self._command_copy_fence(key,request,attempt)
     self._artifact_recheck(pid,expected)
     self._copy_recheck(managed,fd,identity); assert_identity_at(fd,managed.name,owned)
     if os.stat(managed.name,dir_fd=fd,follow_symlinks=False).st_ctime_ns!=ctime: raise PermissionError('managed copy content changed')
     old=self.db.one("select * from artifacts where scene_id=? and kind='IMAGE' and status='ACTIVE' order by version desc",(sid,))
     if old:self.db.execute("update artifacts set status='SUPERSEDED' where id=?",(old['id'],))
     aid=self._register_artifact(pid,'IMAGE',managed,digest,sid,old['id'] if old else None)
     self._event(pid,'SCENE_ARTIFACT_REPLACED',{'scene':s['code'],'artifact':aid})
     result={'artifact_id':aid}
     # Recheck at the result boundary too: no late worker can publish success.
     self._command_copy_fence(key,request,attempt)
     self.db.execute('insert into command_receipts(idempotency_key,request_sha256,response_json,created_at) values(?,?,?,?)',(key,request,json.dumps(result),now()))
     self.db.execute("update command_copy_requests set state='SUCCEEDED' where idempotency_key=? and attempt=?",(key,attempt))
    return result
  except BaseException:
   with self.db.transaction():
    self.db.execute("update command_copy_requests set state='MANUAL_REVIEW',error='COPY_ATTEMPT_FAILED_REVIEW_REQUIRED' where idempotency_key=? and attempt=? and state='PREPARING'",(key,attempt))
   raise
 def dispatch_discord(self,text,role,idempotency_key=None):
  """Dispatch a command, optionally behind an atomic canonical-effect receipt.

  Transport delivery/response is at-least-once.  When supplied, the stable key
  makes all core mutations and their result exactly-once; reuse for a different
  request fails closed.
  """
  if idempotency_key is not None:
   if not isinstance(idempotency_key,str) or not idempotency_key.strip(): raise ValueError('idempotency key required')
   command=parse_discord(text)
   role_value=role.value if isinstance(role,Role) else str(role)
   request_sha=hashlib.sha256(json.dumps({'text':text,'role':role_value},sort_keys=True,separators=(',',':')).encode()).hexdigest()
   if command.name=='scene-replace': return self._dispatch_copy_command(command,role,idempotency_key,request_sha)
   with self.db.transaction():
    pending=self.db.one('select request_sha256 from command_copy_requests where idempotency_key=?',(idempotency_key,))
    if pending: raise PermissionError('idempotency key reused for different request')
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
   if c.name=='project-status': return self.status(a[0],role)
   if c.name=='scene-status':
    scene=self.db.one('select * from scenes where project_id=? and code=?',a)
    if scene is None: raise CommandError('INVALID_ARGUMENT','scene not found')
    return dict(scene)
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
  except ValueError as e:
   # Service validation uses ValueError; programming TypeError must propagate.
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

 def recover_legacy_checkpoint_content(self,pid,stage,*,expected_revision,checkpoint_sha256,allow_legacy_content=False,role=Role.OPERATOR):
  """OWNER content-only revalidation; never reconstruct missing source history."""
  if role not in (Role.OWNER,'OWNER'): raise PermissionError('OWNER required for legacy content recovery')
  if allow_legacy_content is not True: raise PermissionError('explicit allow_legacy_content=True required; see docs/legacy-content-recovery.md')
  with self.db.transaction(write=False):
   r,snap,current,fields=self._validated_checkpoint(pid,stage,legacy_content=True)
   if snap['job'].get('project_id')!=pid or snap['job'].get('id')!=r['job_id']: raise ValueError('checkpoint job ownership mismatch; use intact backup')
   if type(expected_revision) is not int or current['version']!=expected_revision: raise ValueError('stale expected revision; inspect current project version')
   if snap['snapshot_sha256']!=checkpoint_sha256: raise ValueError('checkpoint digest differs from OWNER selection; inspect original checkpoint')
   for event in self.db.all("select data_json from events where project_id=? and type='LEGACY_CONTENT_RECOVERED'",(pid,)):
    receipt=json.loads(event['data_json'])
    if receipt['checkpoint_id']==r['id']: raise ValueError('checkpoint already recovered; use fresh v4 checkpoint')
   # v3 never captured audio/cues/normalized documents. Leave these rows alone;
   # pin the current audio bytes but do not assert historical source provenance.
   audio=self.db.one('select * from audio where project_id=?',(pid,))
   if not audio: raise ValueError('current narration required; recover source separately before legacy content recovery')
   local_visual_frames(audio['duration_ms'],[(s['start_ms'],s['end_ms']) for s in snap['scenes']])
   validate_local_output(json.loads(snap['project']['output_json']),snap['project']['transition'])
   expected=self._restore_snapshot(pid)
   artifacts=[a for a in snap['artifacts'] if a['status']!='DELETED']+[dict(audio)]
  with self._restore_bytes(pid,artifacts) as pins, self.db.transaction():
   self._restore_recheck(pid,expected,pins)
   # Receipt replay is rechecked under the writer too (events are append-only).
   if any(json.loads(e['data_json'])['checkpoint_id']==r['id'] for e in self.db.all("select data_json from events where project_id=? and type='LEGACY_CONTENT_RECOVERED'",(pid,))): raise ValueError('checkpoint already recovered; use fresh v4 checkpoint')
   self._invalidate(pid,'legacy content recovered',clear_plan=False)
   # Settings/retention/provider selection are not recovery authority. Only
   # creative content configuration is copied; original source rows stay intact.
   content_fields=('language','style_json','references_json','bible_json')
   self.db.execute('update projects set '+','.join(f'{k}=?' for k in content_fields)+' where id=?',tuple(snap['project'][k] for k in content_fields)+(pid,))
   self.db.execute("update scenes set state='PLANNED',qa_state='PENDING',qa_json='{}',continuity_json='{}',checkpoint_json='{}' where project_id=?",(pid,))
   self._event(pid,'SCENES_PLANNED',{'count':len(snap['scenes']),'origin':'legacy_content_recovery','historical_generation_claimed':False})
   receipt={'checkpoint_id':r['id'],'checkpoint_job_id':r['job_id'],'checkpoint_sha256':snap['snapshot_sha256'],'checkpoint_bytes_sha256':hashlib.sha256(r['checkpoint_json'].encode()).hexdigest(),'project_id':pid,'stage':stage,'expected_revision':expected_revision,'new_revision':self.project(pid)['version'],'new_generation':self._checkpoint_generation(pid),'recovered_fields':list(content_fields),'source_policy':'current source retained; historical source unavailable','historical_generation_claimed':False}
   self._event(pid,'LEGACY_CONTENT_RECOVERED',receipt)
   return receipt
 def _validated_checkpoint(self,pid,stage,legacy_content=False):
  r=self.db.one("select stages.* from stages join jobs on jobs.id=stages.job_id where jobs.project_id=? and stages.name=? and stages.state='SUCCEEDED' order by stages.id desc",(pid,stage))
  if not r: raise ValueError('checkpoint not found')
  snap=json.loads(r['checkpoint_json']); keys={'snapshot_version','data','project','job','scenes','artifacts','snapshot_sha256'}
  if snap.get('snapshot_version')==4: keys|={'identity','plan_identity'}
  if not keys.issubset(snap) or set(snap)-keys!=set(snap.get('data',{}))-keys or any(snap[k]!=snap['data'][k] for k in set(snap)-keys) or snap['snapshot_version'] not in (3,4): raise ValueError('incomplete checkpoint snapshot')
  if legacy_content and snap['snapshot_version']!=3: raise ValueError('legacy recovery requires v3; use ordinary restore-checkpoint for v4')
  if snap['snapshot_version']==4:
   if not isinstance(snap['identity'],str) or len(snap['identity'])!=32 or snap['plan_identity']!=self._checkpoint_generation(pid): raise ValueError('checkpoint plan generation mismatch')
  elif snap['scenes'] and not legacy_content:
   # Legacy v3 has no generation receipt: preserve history, fail closed rather
   # than infer identity from reusable scene integers or rewrite old snapshots.
   raise ValueError('legacy checkpoint lacks scene generation identity; manual review required')
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
  if any(a.get('detached_scene_id')!=b.get('detached_scene_id') for a,b in zip(snap['artifacts'],live)): raise ValueError('checkpoint detached provenance mismatch')
  if any(a['status']!=b['status'] and 'DELETED' in (a['status'],b['status']) for a,b in zip(snap['artifacts'],live)): raise ValueError('checkpoint deletion ownership mismatch')
  return dict(r),snap,dict(current),project_fields
 def restore_latest_checkpoint(self,pid,stage,role=Role.OPERATOR):
  self._role(role,'checkpoint-list')
  with self.db.transaction(write=False):
   r,snap,current,project_fields=self._validated_checkpoint(pid,stage)
   expected=self._restore_snapshot(pid)
  with self._restore_bytes(pid,[a for a in snap['artifacts'] if a['status']!='DELETED']) as pins, self.db.transaction():
   self._restore_recheck(pid,expected,pins)
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
  self._role(role,'resume')
  with self.db.transaction():
   p=self.db.one('select * from projects where id=?',(pid,))
   if not p or p['state']!='PAUSED': raise PermissionError('only PAUSED projects can resume')
   # Validate every candidate before mutating any job or lifecycle/event row.
   for job in self.db.all("select * from jobs where project_id=? and state='PAUSED' and project_version=?",(pid,p['version'])):
    self._resume_gate(job)
   if self.db.execute("update projects set state='ACTIVE' where id=? and state='PAUSED' and version=?",(pid,p['version'])).rowcount!=1: raise PermissionError('only PAUSED projects can resume')
   self.db.execute("update jobs set state='QUEUED',updated_at=? where project_id=? and state='PAUSED' and project_version=?",(now(),pid,p['version']))
   self._event(pid,"RESUMED")
 def status(self,pid,role=Role.OPERATOR):
  self._role(role,'status')
  with self.db.transaction(write=False):
   return {"project":self.project(pid),"scenes":[dict(x) for x in self.db.all("select * from scenes where project_id=? order by ord",(pid,))],"jobs":[dict(x) for x in self.db.all('select * from jobs where project_id=?',(pid,))]}
 def status_summary(self,pid,role=Role.OPERATOR):
  """Read-only compact polling view (at most 8 SELECTs, no per-scene output).

  ``current`` means subject/dependency-bound active scene evidence, or an
  exact current-global-version manifest for project-level gates.
  Text caps are exposed by ``STATUS_TEXT_LIMITS``; indicators are capped at 8.
  """
  self._role(role,'status')
  with self.db.transaction(write=False):
   return self._status_summary_snapshot(pid)
 def _status_summary_snapshot(self,pid):
  p=self.db.one('select * from projects where id=?',(pid,))
  if not p: raise ValueError('project not found')
  cut=lambda value,limit: None if value is None else str(value)[:limit]
  project={'id':p['id'],'name':cut(p['name'],self.STATUS_TEXT_LIMITS['project_name']),'language':p['language'],'state':p['state'],'blocked':p['state']=='BLOCKED','blocked_reason':cut(p['blocked_reason'],self.STATUS_TEXT_LIMITS['blocked_reason']),'version':p['version']}
  scenes=[dict(r) for r in self.db.all('select * from scenes where project_id=? order by ord',(pid,))]
  dimensions={'scene':{'total':len(scenes)},'approval':{},'qa':{}}
  for s in scenes:
   for dimension,key in (('scene','state'),('approval','approval_state'),('qa','qa_state')):
    value=s[key]; dimensions[dimension][value]=dimensions[dimension].get(value,0)+1
  artifacts=[dict(r) for r in self.db.all('select id,scene_id,kind,sha256,version,parent_id,status from artifacts where project_id=? order by id',(pid,))]
  approvals=[dict(r) for r in self.db.all("select a.*, (select e.data_json from events e where e.project_id=a.project_id and e.type='FINAL_REVIEWED' and json_extract(e.data_json,'$.approval_id')=a.id order by e.id desc limit 1) final_receipt from approvals a where a.project_id=?",(pid,))]
  gate_rows={}
  for a in approvals:
   row=gate_rows.setdefault(a['gate'],{'approved':0,'rejected':0,'active_approved':0}); row[a['decision'].lower()]=row.get(a['decision'].lower(),0)+1
   if a['decision']=='APPROVED' and a['revoked_at'] is None and (a['gate']=='SCENE' or a['project_version']==p['version']): row['active_approved']+=1
  manifest_hash=manifest_evidence_hash(p,scenes,artifacts)
  required_scenes=[s for s in scenes if s['ord']<=5 or s['special']==1]
  artifacts_by_scene={s['id']:[] for s in required_scenes}
  for a in artifacts:
   if a['scene_id'] in artifacts_by_scene and a['status']=='ACTIVE': artifacts_by_scene[a['scene_id']].append(a)
  pilot_current=bool(required_scenes)
  for s in required_scenes:
   if not scene_approval_current(p,s,artifacts_by_scene[s['id']],approvals): pilot_current=False
  def gate(key,current):
   row=gate_rows.get(key,{}); return {'approved':row.get('approved',0),'rejected':row.get('rejected',0),'active':row.get('active_approved',0)>0,'current':bool(current),'evidence':'SCENE_DEPENDENCY_CURRENT' if key=='SCENE' else 'CURRENT_VERSION_ACTIVE'}
  manifest_current=lambda key:any(a['gate']==key and a['scene_id'] is None and a['decision']=='APPROVED' and a['revoked_at'] is None and a['project_version']==p['version'] and a['evidence_sha256']==manifest_hash for a in approvals)
  from .evidence import digest
  def final_current(a):
   try:
    r=json.loads(a['final_receipt']); subject=r['subject']
    return bool(a['gate']=='FINAL' and a['decision']=='APPROVED' and a['revoked_at'] is None and a['project_version']==p['version'] and subject['project_id']==pid and subject['project_version']==p['version'] and subject['current_evidence_sha256']==manifest_hash and r['qa']['subject']==subject and QAEvidence(r['qa']['checks'],r['qa']['score'],r['qa']['evaluator']).passed and digest({'subject':subject,'qa_event_id':r['qa_event_id'],'qa':r['qa']})==a['evidence_sha256'] and any(x['id']==subject['artifact_id'] and x['sha256']==subject['sha256'] and x['kind']=='FINAL_VIDEO' and x['status']=='ACTIVE' for x in artifacts))
   except (KeyError,TypeError,ValueError): return False
  gates={'pilot':gate('SCENE',pilot_current),'post_batch':gate('POST_BATCH',manifest_current('POST_BATCH')),'final':gate('FINAL',any(final_current(a) for a in approvals))}
  job_groups=self.db.all("select kind,state,count(*) count from jobs where project_id=? and project_version=? group by kind,state order by count(*) desc,kind,state limit ?",(pid,p['version'],self.STATUS_JOB_GROUP_LIMIT+1))
  jobs=[{'kind':cut(r['kind'],self.STATUS_TEXT_LIMITS['job_kind']),'state':cut(r['state'],self.STATUS_TEXT_LIMITS['job_state']),'count':r['count']} for r in job_groups[:self.STATUS_JOB_GROUP_LIMIT]]
  if len(job_groups)>self.STATUS_JOB_GROUP_LIMIT:
   other=self.db.one("select count(*) count,count(distinct kind||char(0)||state) groups from jobs where project_id=? and project_version=? and (kind,state) not in (select kind,state from jobs where project_id=? and project_version=? group by kind,state order by count(*) desc,kind,state limit ?)",(pid,p['version'],pid,p['version'],self.STATUS_JOB_GROUP_LIMIT))
   jobs.append({'kind':'OTHER','state':'OTHER','count':other['count'],'unknown_group_count':other['groups']})
  stale=self.db.one('select count(*) n from jobs where project_id=? and (project_version is null or project_version!=?)',(pid,p['version']))['n']
  indicators=[dict(r) for r in self.db.all("select code,timestamp from (select 'IMAGE_ATTEMPT_FAILED' code,a.created_at timestamp from attempts a join scenes s on s.id=a.scene_id where s.project_id=? and a.state='FAILED' union all select case when state='FAILED' then 'JOB_FAILED' else 'JOB_BLOCKED' end,updated_at from jobs where project_id=? and project_version=? and state in ('FAILED','BLOCKED')) order by timestamp desc limit ?",(pid,pid,p['version'],self.STATUS_INDICATOR_LIMIT))]
  return {'project':project,'scene_counts':dimensions['scene'],'approval_counts':dimensions['approval'],'qa_counts':dimensions['qa'],'gates':gates,'current_jobs':jobs,'stale_job_count':stale,'indicators':indicators}
 def report(self,pid,role=Role.OPERATOR):
  self._role(role,'cost-report')
  self.project(pid)
  return {"costs":[dict(x) for x in self.db.all("select * from cost_observations where project_id=?",(pid,))],"errors":[dict(x) for x in self.db.all("select * from events where project_id=? and (type like '%BLOCKED' or data_json like '%error%')",(pid,))]}
