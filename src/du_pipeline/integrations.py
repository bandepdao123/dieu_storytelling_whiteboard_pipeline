"""SDK-neutral, fake-friendly integration adapters."""
from __future__ import annotations
import hashlib,json,os,time,tempfile,uuid
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .adapters import SHEET_TABS,DRIVE,Role
DEFAULT_DRIVE_ROOT="1oXRgYvZ2eIbySwAjtXGDbV6_f3vq0u5k"
class IntegrationConfigurationError(RuntimeError):pass
class VerificationError(RuntimeError):pass
SHEET_HEADERS={
"PROJECT":("id","name","language","state","version","bible_json","style_json","references_json"),
"SCENES":("id","code","ord","start_ms","end_ms","text","state","approval_state","qa_state"),
"CHARACTERS":("project_id","name","description"),"ENVIRONMENTS":("project_id","name","description"),"PROPS":("project_id","name","description"),
"ASSETS":("id","scene_id","kind","uri","sha256","version","status","created_at"),
"GENERATIONS":("id","scene_id","number","provider","state","error","created_at"),
"QA":("scene_id","scene_code","qa_state","qa_json","duration_exception"),
"FEEDBACK":("id","scene_id","gate","decision","actor","created_at","command_id","command","scene_code"),
"COST_TIME":("kind","id","provider_stage","value","unit","created_at"),"LEARNINGS":("id","key","value_json","created_at")}
def require_oauth_path(env=os.environ):
 v=env.get("DU_GOOGLE_OAUTH_TOKEN_PATH")
 if not v:raise IntegrationConfigurationError("DU_GOOGLE_OAUTH_TOKEN_PATH is required")
 p=Path(v).expanduser()
 if not p.is_file() or not os.access(p,os.R_OK):raise IntegrationConfigurationError("Google OAuth token path does not exist or is unreadable")
 return p
def quote_a1(t):return "'"+str(t).replace("'","''")+"'"
def retry_call(fn,attempts=4,base_delay=.25,sleep=time.sleep):
 for n in range(attempts):
  try:return fn()
  except Exception as e:
   if n+1==attempts or not (getattr(e,'retryable',False) or getattr(e,'status_code',None) in (429,500,502,503,504)):raise
   sleep(base_delay*2**n)
class SheetsAdapter:
 def __init__(self,client,db,sleep=time.sleep,batch_size=200):self.client,self.db,self.sleep,self.batch_size=client,db,sleep,max(1,batch_size)
 def ensure(self,pid,dry_run=False):
  if dry_run:return {'dry_run':True,'tabs':list(SHEET_TABS)}
  r=self.db.one('select spreadsheet_id from integration_projects where project_id=?',(pid,));sid=r[0] if r else None
  if not sid:sid=retry_call(lambda:self.client.find_spreadsheet(pid),sleep=self.sleep) or retry_call(lambda:self.client.create_spreadsheet(pid),sleep=self.sleep)
  tabs=set(retry_call(lambda:self.client.get_tabs(sid),sleep=self.sleep));extra=tabs-set(SHEET_TABS)
  if extra:raise VerificationError('unexpected tabs: '+', '.join(sorted(extra)))
  missing=[t for t in SHEET_TABS if t not in tabs]
  if missing:retry_call(lambda:self.client.add_tabs(sid,missing),sleep=self.sleep)
  if set(retry_call(lambda:self.client.get_tabs(sid),sleep=self.sleep))!=set(SHEET_TABS):raise VerificationError('spreadsheet tab contract failed')
  self.db.execute('insert into integration_projects values(?,?) on conflict(project_id) do update set spreadsheet_id=excluded.spreadsheet_id',(pid,sid));return sid
 def sync(self,pid,dry_run=False):
  p=self.db.one('select * from projects where id=?',(pid,))
  if not p:raise ValueError('project not found')
  q=lambda s:[dict(x) for x in self.db.all(s,(pid,))]
  data={"PROJECT":[dict(p)],"SCENES":q('select id,code,ord,start_ms,end_ms,text,state,approval_state,qa_state from scenes where project_id=? order by ord'),"CHARACTERS":[],"ENVIRONMENTS":[],"PROPS":[],"ASSETS":q('select id,scene_id,kind,uri,sha256,version,status,created_at from artifacts where project_id=?'),"GENERATIONS":q('select a.id,a.scene_id,a.number,a.provider,a.state,a.error,a.created_at from attempts a join scenes s on s.id=a.scene_id where s.project_id=?'),"QA":q('select id scene_id,code scene_code,qa_state,qa_json,duration_exception from scenes where project_id=?'),"FEEDBACK":q('select id,scene_id,gate,decision,actor,created_at from approvals where project_id=?'),"COST_TIME":q("select 'COST' kind,id,provider provider_stage,amount value,currency unit,created_at from cost_observations where project_id=?")+q("select 'TIME' kind,id,stage provider_stage,seconds value,'seconds' unit,created_at from time_observations where project_id=?"),"LEARNINGS":q('select id,key,value_json,created_at from learning_metadata where project_id=?')}
  counts={t:len(data[t]) for t in SHEET_TABS}
  if dry_run:return {'dry_run':True,'tabs':list(SHEET_TABS),'actions':list(SHEET_TABS),'headers':SHEET_HEADERS,'counts':counts}
  sid=self.ensure(pid)
  for t in SHEET_TABS:
   rows=data[t] or [{h:'' for h in SHEET_HEADERS[t]}];rows=[{h:r.get(h,'') for h in SHEET_HEADERS[t]} for r in rows]
   for i in range(0,len(rows),self.batch_size):retry_call(lambda t=t,c=rows[i:i+self.batch_size]:self.client.batch_upsert(sid,quote_a1(t),c,'id'),sleep=self.sleep)
  return {'spreadsheet_id':sid,'counts':counts}
 def ingest_commands(self,pid,dry_run=False):
  if dry_run:return {'dry_run':True,'processed':0,'actions':['read FEEDBACK','validate rows','claim commands']}
  sid=self.ensure(pid);from .service import Pipeline,now
  outcomes=[];processed=0;owner=str(uuid.uuid4())
  for row in self.client.read_rows(sid,quote_a1('FEEDBACK')):
   key=str(row.get('command_id','')).strip();action=str(row.get('command','')).upper()
   state='QUARANTINED' if not key or action not in ('APPROVE','REJECT') else 'PROCESSING'
   if not key:key='row-'+hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()[:16]
   try:
    with self.db.transaction():self.db.execute('insert into integration_commands(source,external_id,project_id,created_at,state,lease_owner,lease_expires_at,detail) values(?,?,?,?,?,?,?,?)',('SHEET',key,pid,now(),state,owner,(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),None))
   except Exception:continue
   if state=='QUARANTINED':outcomes.append(state);continue
   try:
    with self.db.transaction():
     Pipeline(self.db).decide_scene(pid,str(row.get('scene_code','')),'APPROVED' if action=='APPROVE' else 'REJECTED',str(row.get('actor','sheet')),Role.REVIEWER)
     self.db.execute("update integration_commands set state='COMPLETED',lease_owner=NULL,lease_expires_at=NULL where source='SHEET' and project_id=? and external_id=?",(pid,key))
    state='COMPLETED';processed+=1
   except Exception as e:self.db.execute("update integration_commands set state='FAILED',detail=?,lease_owner=NULL where source='SHEET' and project_id=? and external_id=?",(type(e).__name__,pid,key));state='FAILED'
   outcomes.append(state)
  return {'processed':processed,'outcomes':outcomes}
class DriveAdapter:
 def __init__(self,client,root_id=None,chunk_size=1048576,sleep=time.sleep,max_chunks=100000):self.client=client;self.root_id=root_id or os.getenv('DU_GOOGLE_DRIVE_ROOT_ID',DEFAULT_DRIVE_ROOT);self.chunk_size=chunk_size;self.sleep=sleep;self.max_chunks=max_chunks
 def ensure_tree(self,pid,dry_run=False):
  if dry_run:return {x:None for x in DRIVE}
  root=retry_call(lambda:self.client.ensure_folder(self.root_id,pid),sleep=self.sleep);return {x:retry_call(lambda n=x:self.client.ensure_folder(root,n),sleep=self.sleep) for x in DRIVE}
 def upload(self,pid,local_path,folder='07_exports',manifest_path=None,dry_run=False):
  p=Path(local_path);size=p.stat().st_size;digest=hashlib.sha256(p.read_bytes()).hexdigest()
  if dry_run:return {'dry_run':True,'path':str(p),'size':size,'sha256':digest,'actions':['ensure tree','begin/resume','upload chunks','finish','verify checksum','atomic manifest']}
  folders=self.ensure_tree(pid);session,offset=retry_call(lambda:self.client.begin_upload(folders[folder],p.name,size,digest),sleep=self.sleep)
  if not isinstance(offset,int) or not 0<=offset<=size:raise VerificationError('invalid resume offset')
  with p.open('rb') as f:
   f.seek(offset);loops=0
   while offset<size:
    loops+=1
    if loops>self.max_chunks:raise VerificationError('upload progress bound exceeded')
    chunk=f.read(min(self.chunk_size,size-offset));new=retry_call(lambda:self.client.upload_chunk(session,offset,chunk),sleep=self.sleep)
    expected=offset+len(chunk)
    if not chunk or not isinstance(new,int) or isinstance(new,bool) or new!=expected:raise VerificationError('invalid upload offset')
    offset=new;f.seek(offset)
  rid=retry_call(lambda:self.client.finish_upload(session),sleep=self.sleep);meta=retry_call(lambda:self.client.metadata(rid),sleep=self.sleep)
  if meta.get('size') is None or int(meta['size'])!=size:raise VerificationError('remote size evidence missing or mismatch')
  remote=meta.get('sha256');algo='sha256'
  if remote is None:remote=meta.get('md5');algo='md5'
  expected=digest if algo=='sha256' else hashlib.md5(p.read_bytes()).hexdigest()
  if not remote or remote.lower()!=expected:raise VerificationError('remote content checksum unavailable or mismatch')
  result={'local_path':str(p),'remote_id':rid,'size':size,'sha256':digest,'verified':True};target=Path(manifest_path or p.parent/'upload-manifest.json');old=json.loads(target.read_text()) if target.exists() else []
  target.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(prefix='.'+target.name+'.',dir=target.parent)
  try:
   with os.fdopen(fd,'w',encoding='utf8') as out:json.dump([x for x in old if x.get('local_path')!=str(p)]+[result],out,indent=2);out.flush();os.fsync(out.fileno())
   os.replace(tmp,target);d=os.open(target.parent,os.O_RDONLY);os.fsync(d);os.close(d)
  finally:
   if os.path.exists(tmp):os.unlink(tmp)
  return result
class DiscordBridge:
 def __init__(self,pipeline,allowlist,lease_seconds=300,owner=None):self.pipeline,self.allowlist,self.lease_seconds,self.owner=pipeline,allowlist,lease_seconds,owner or str(uuid.uuid4())
 def status(self,message_id):
  r=self.pipeline.db.one('select response_json,state from discord_messages where message_id=?',(message_id,))
  if not r:return {'type':'KHONG_TIM_THAY','ok':False,'message':'Không tìm thấy tin nhắn.'}
  if r[0]:return json.loads(r[0])
  return {'type':'DANG_XU_LY','ok':False,'message':'Lệnh đang xử lý hoặc cần đối soát.','state':r[1] or 'PROCESSING'}
 def dispatch(self,message_id,user_id,text,dry_run=False):
  from .adapters import parse_discord,authorize,CommandError
  from .service import now
  if dry_run:
   if user_id not in self.allowlist:return {'type':'TU_CHOI','ok':False,'message':'Bạn không có quyền sử dụng bot.'}
   try:c=parse_discord(text)
   except CommandError as e:return {'type':'LOI_LENH','ok':False,'message':str(e)}
   return {'type':'DRY_RUN','ok':authorize(Role(self.allowlist[user_id]),c.name),'message':'Lệnh hợp lệ và được phép; chưa thực thi.'}
  def audit(o,d):self.pipeline.db.execute('insert into integration_attempts(source,external_id,user_id,outcome,detail,created_at) values(?,?,?,?,?,?)',('DISCORD',message_id,user_id,o,d,now()))
  if user_id not in self.allowlist:audit('DENIED','unauthorized');return {'type':'TU_CHOI','ok':False,'message':'Bạn không có quyền sử dụng bot.'}
  # A message id is scoped to its original authorized caller.  Check that
  # identity before replaying either a response or lease state; this also
  # preserves idempotency when retry text differs or is no longer parseable.
  existing=self.pipeline.db.one('select response_json,state,lease_expires_at,user_id from discord_messages where message_id=?',(message_id,))
  if existing and existing[3]!=user_id:audit('DENIED','message_owner');return {'type':'TU_CHOI','ok':False,'message':'Bạn không có quyền truy cập lệnh này.'}
  if existing and existing[0]:audit('REPLAY','cached');return json.loads(existing[0])
  if existing and existing[2] and existing[2]>=datetime.now(timezone.utc).isoformat():audit('ACTIVE_LEASE','processing');return self.status(message_id)
  try:c=parse_discord(text)
  except CommandError as e:audit('INVALID',e.code);return {'type':'LOI_LENH','ok':False,'message':str(e)}
  if not authorize(Role(self.allowlist[user_id]),c.name):audit('DENIED','rbac');return {'type':'TU_CHOI','ok':False,'message':'Vai trò không được phép thực hiện lệnh này.'}
  expiry=(datetime.now(timezone.utc)+timedelta(seconds=self.lease_seconds)).isoformat();nowiso=datetime.now(timezone.utc).isoformat()
  with self.pipeline.db.transaction():
   old=self.pipeline.db.one('select response_json,state,lease_expires_at,user_id from discord_messages where message_id=?',(message_id,))
   if old and old[3]!=user_id:audit('DENIED','message_owner');return {'type':'TU_CHOI','ok':False,'message':'Bạn không có quyền truy cập lệnh này.'}
   if old and old[0]:audit('REPLAY','cached');return json.loads(old[0])
   if old and old[2] and old[2]>=nowiso:audit('ACTIVE_LEASE','processing');return self.status(message_id)
   if old:self.pipeline.db.execute("update discord_messages set state='PROCESSING',lease_owner=?,lease_expires_at=? where message_id=?",(self.owner,expiry,message_id))
   else:self.pipeline.db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state,lease_owner,lease_expires_at) values(?,?,NULL,?,'PROCESSING',?,?)",(message_id,user_id,now(),self.owner,expiry))
  try:r={'type':'THANH_CONG','ok':True,'message':'Đã thực thi lệnh.','data':self.pipeline.dispatch_discord(text,Role(self.allowlist[user_id]))}
  except Exception as e:r={'type':'LOI_LENH','ok':False,'message':type(e).__name__}
  with self.pipeline.db.transaction():
   self.pipeline.db.execute('update discord_messages set response_json=?,state=?,error=?,lease_owner=NULL,lease_expires_at=NULL where message_id=?',(json.dumps(r),'COMPLETED' if r['ok'] else 'FAILED',None if r['ok'] else r['type'],message_id))
   self.pipeline.db.execute('insert into integration_attempts(source,external_id,user_id,outcome,detail,created_at) values(?,?,?,?,?,?)',('DISCORD',message_id,user_id,'COMPLETED' if r['ok'] else 'FAILED',r['type'],now()))
  return r
