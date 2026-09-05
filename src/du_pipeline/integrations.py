"""SDK-neutral live integration boundaries; SQLite remains authoritative."""
from __future__ import annotations
import hashlib, json, os, time
from pathlib import Path
from .adapters import SHEET_TABS, DRIVE, Role

DEFAULT_DRIVE_ROOT="1oXRgYvZ2eIbySwAjtXGDbV6_f3vq0u5k"
class IntegrationConfigurationError(RuntimeError): pass
class VerificationError(RuntimeError): pass

def require_oauth_path(env=os.environ):
    """Validate token location without opening it or initiating OAuth."""
    value=env.get("DU_GOOGLE_OAUTH_TOKEN_PATH")
    if not value: raise IntegrationConfigurationError("DU_GOOGLE_OAUTH_TOKEN_PATH is required")
    path=Path(value).expanduser()
    if not path.is_file(): raise IntegrationConfigurationError("Google OAuth token path does not exist")
    if not os.access(path,os.R_OK): raise IntegrationConfigurationError("Google OAuth token is not readable")
    return path

def quote_a1(title): return "'"+str(title).replace("'","''")+"'"
def retry_call(fn,attempts=4,base_delay=.25,sleep=time.sleep):
    for n in range(attempts):
        try:return fn()
        except Exception as exc:
            retryable=getattr(exc,"status_code",None) in (429,500,502,503,504) or getattr(exc,"retryable",False)
            if not retryable or n+1==attempts: raise
            sleep(base_delay*2**n)

class SheetsAdapter:
    """Fake-friendly client: find/create/get_tabs/add_tabs/batch_upsert/read_rows."""
    def __init__(self,client,db,sleep=time.sleep,batch_size=200): self.client,self.db,self.sleep,self.batch_size=client,db,sleep,max(1,batch_size)
    def ensure(self,pid,dry_run=False):
        row=self.db.one("select spreadsheet_id from integration_projects where project_id=?",(pid,)); sid=row[0] if row else None
        if not sid:sid=retry_call(lambda:self.client.find_spreadsheet(pid),sleep=self.sleep)
        if not sid and not dry_run:sid=retry_call(lambda:self.client.create_spreadsheet(pid),sleep=self.sleep)
        if dry_run:return {"dry_run":True,"tabs":list(SHEET_TABS)}
        tabs=set(retry_call(lambda:self.client.get_tabs(sid),sleep=self.sleep)); extra=tabs-set(SHEET_TABS)
        if extra:raise VerificationError("unexpected tabs: "+", ".join(sorted(extra)))
        missing=[x for x in SHEET_TABS if x not in tabs]
        if missing:retry_call(lambda:self.client.add_tabs(sid,missing),sleep=self.sleep)
        final=tuple(retry_call(lambda:self.client.get_tabs(sid),sleep=self.sleep))
        if len(final)!=11 or set(final)!=set(SHEET_TABS):raise VerificationError("spreadsheet tab contract failed")
        self.db.execute("insert into integration_projects values(?,?) on conflict(project_id) do update set spreadsheet_id=excluded.spreadsheet_id",(pid,sid)); return sid
    def sync(self,pid,dry_run=False):
        sid=self.ensure(pid,dry_run); p=self.db.one("select id,name,language,state,version from projects where id=?",(pid,))
        if not p:raise ValueError("project not found")
        data={"PROJECT":[dict(p)],"SCENES":[dict(x) for x in self.db.all("select id,code,ord,start_ms,end_ms,state,approval_state,qa_state from scenes where project_id=? order by ord",(pid,))]}
        if dry_run:return {"dry_run":True,"counts":{k:len(v) for k,v in data.items()}}
        for tab,rows in data.items():
            for i in range(0,len(rows),self.batch_size):retry_call(lambda t=tab,c=rows[i:i+self.batch_size]:self.client.batch_upsert(sid,quote_a1(t),c,"id"),sleep=self.sleep)
        return {"spreadsheet_id":sid,"counts":{k:len(v) for k,v in data.items()}}
    def ingest_commands(self,pid,dry_run=False):
        sid=self.ensure(pid,dry_run)
        if dry_run:return {"dry_run":True,"processed":0}
        from .service import Pipeline,now
        done=0
        for row in self.client.read_rows(sid,quote_a1("FEEDBACK")):
            key=str(row.get("command_id","")).strip(); action=str(row.get("command","")).upper()
            if not key or action not in ("APPROVE","REJECT") or self.db.one("select 1 from integration_commands where source='SHEET' and external_id=?",(key,)):continue
            with self.db.transaction():
                Pipeline(self.db).decide_scene(pid,str(row.get("scene_code","")),"APPROVED" if action=="APPROVE" else "REJECTED",str(row.get("actor","sheet")),Role.REVIEWER)
                self.db.execute("insert into integration_commands values('SHEET',?,?,?)",(key,pid,now()))
            done+=1
        return {"processed":done}

class DriveAdapter:
    """Fake-friendly resumable client boundary."""
    def __init__(self,client,root_id=None,chunk_size=1048576,sleep=time.sleep):self.client=client;self.root_id=root_id or os.getenv("DU_GOOGLE_DRIVE_ROOT_ID",DEFAULT_DRIVE_ROOT);self.chunk_size=chunk_size;self.sleep=sleep
    def ensure_tree(self,pid,dry_run=False):
        if dry_run:return {x:None for x in DRIVE}
        root=retry_call(lambda:self.client.ensure_folder(self.root_id,pid),sleep=self.sleep);return {x:retry_call(lambda n=x:self.client.ensure_folder(root,n),sleep=self.sleep) for x in DRIVE}
    def upload(self,pid,local_path,folder="07_exports",manifest_path=None,dry_run=False):
        path=Path(local_path);size=path.stat().st_size;digest=hashlib.sha256(path.read_bytes()).hexdigest()
        if dry_run:return {"dry_run":True,"path":str(path),"size":size,"sha256":digest}
        folders=self.ensure_tree(pid);session,offset=retry_call(lambda:self.client.begin_upload(folders[folder],path.name,size,digest),sleep=self.sleep)
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset<size:
                chunk=stream.read(self.chunk_size);offset=retry_call(lambda o=offset,c=chunk:self.client.upload_chunk(session,o,c),sleep=self.sleep)
        rid=retry_call(lambda:self.client.finish_upload(session),sleep=self.sleep);meta=retry_call(lambda:self.client.metadata(rid),sleep=self.sleep)
        if meta.get("size") is None:raise VerificationError("remote size evidence missing")
        if int(meta["size"])!=size:raise VerificationError("remote size mismatch")
        if meta.get("sha256") is not None and meta["sha256"].lower()!=digest:raise VerificationError("remote checksum mismatch")
        if meta.get('sha256') is None and not (meta.get('checksum_supported') is False and (meta.get('etag') or meta.get('version'))):raise VerificationError("remote checksum unavailable without strong alternative evidence")
        result={"local_path":str(path),"remote_id":rid,"size":size,"sha256":digest,"verified":True}
        target=Path(manifest_path or path.parent/"upload-manifest.json"); old=json.loads(target.read_text()) if target.exists() else []
        target.write_text(json.dumps([x for x in old if x.get("local_path")!=str(path)]+[result],indent=2),encoding="utf8");return result

class DiscordBridge:
    def __init__(self,pipeline,allowlist):self.pipeline,self.allowlist=pipeline,allowlist
    def dispatch(self,message_id,user_id,text,dry_run=False):
        from .adapters import parse_discord,authorize,CommandError
        from .service import now
        def audit(outcome,detail):
            self.pipeline.db.execute('insert into integration_attempts(source,external_id,user_id,outcome,detail,created_at) values(?,?,?,?,?,?)',('DISCORD',message_id,user_id,outcome,detail,now()))
        if user_id not in self.allowlist:
            audit('DENIED','unauthorized');return {"type":"TU_CHOI","ok":False,"message":"Bạn không có quyền sử dụng bot."}
        old=self.pipeline.db.one("select response_json from discord_messages where message_id=?",(message_id,))
        if old:
            audit('REPLAY','reserved');return json.loads(old[0]) if old[0] else {"type":"DANG_XU_LY","ok":False,"message":"Lệnh đang xử lý hoặc cần đối soát."}
        try:
            command=parse_discord(text)
            if not authorize(Role(self.allowlist[user_id]),command.name):
                audit('DENIED','rbac');return {"type":"TU_CHOI","ok":False,"message":"Vai trò không được phép thực hiện lệnh này."}
        except CommandError as exc:
            audit('INVALID',exc.code);return {"type":"LOI_LENH","ok":False,"message":str(exc)}
        old=self.pipeline.db.one("select response_json from discord_messages where message_id=?",(message_id,))
        if old:
            audit('REPLAY','reserved');return json.loads(old[0]) if old[0] else {"type":"DANG_XU_LY","ok":False,"message":"Lệnh đang xử lý hoặc cần đối soát."}
        if dry_run:
            audit('DRY_RUN','validated');return {"type":"DRY_RUN","ok":True,"message":"Lệnh hợp lệ và được phép; chưa thực thi."}
        try:self.pipeline.db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state) values(?,?,NULL,?,'PROCESSING')",(message_id,user_id,now()))
        except Exception:
            old=self.pipeline.db.one("select response_json from discord_messages where message_id=?",(message_id,));return json.loads(old[0]) if old and old[0] else {"type":"DANG_XU_LY","ok":False,"message":"Lệnh đang xử lý."}
        try:r={"type":"THANH_CONG","ok":True,"message":"Đã thực thi lệnh.","data":self.pipeline.dispatch_discord(text,Role(self.allowlist[user_id]))}
        except PermissionError:r={"type":"TU_CHOI","ok":False,"message":"Vai trò không được phép thực hiện lệnh này."}
        except Exception as exc:r={"type":"LOI_LENH","ok":False,"message":type(exc).__name__}
        with self.pipeline.db.transaction():
            self.pipeline.db.execute("update discord_messages set response_json=?,state=?,error=? where message_id=?",(json.dumps(r),'COMPLETED' if r['ok'] else 'FAILED',None if r['ok'] else r['type'],message_id))
            pid=(r.get("data") or {}).get("project_id")
            if pid:self.pipeline._event(pid,"DISCORD_COMMAND",{"message_id":message_id,"user_id":user_id})
        audit('COMPLETED' if r['ok'] else 'FAILED',r['type']);return r