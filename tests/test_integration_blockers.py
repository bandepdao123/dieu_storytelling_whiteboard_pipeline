import hashlib, json, threading
from pathlib import Path
import pytest

from du_pipeline.adapters import SHEET_TABS
from du_pipeline.cli import integration_client
from du_pipeline.db import Database
from du_pipeline.integrations import (DriveAdapter, DiscordBridge, SheetsAdapter,
    SHEET_HEADERS, VerificationError, IntegrationConfigurationError)
from du_pipeline.service import Pipeline

class NoCalls:
    def __getattr__(self, name): raise AssertionError(f"client called: {name}")

def setup(tmp_path, name="Demo"):
    db=Database(tmp_path/f"{name}.db"); pid=Pipeline(db).init_project(name,scene_range=(1,2))
    db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,state,approval_state,qa_state) values(?,?,?,?,?,?,?,?,?)",(pid,'S001',1,0,1000,'x','PLANNED','REQUIRED','PENDING'))
    return db,pid

class Sheet:
    def __init__(self, rows=()): self.tabs=[]; self.rows=list(rows); self.writes=[]
    def find_spreadsheet(self,p): return None
    def create_spreadsheet(self,p): return "sid"
    def get_tabs(self,s): return self.tabs
    def add_tabs(self,s,t): self.tabs.extend(t)
    def batch_upsert(self,*args): self.writes.append(args)
    def read_rows(self,*args): return list(self.rows)

def test_sheet_all_eleven_headers_and_valid_empty_tables(tmp_path):
    db,pid=setup(tmp_path); fake=Sheet(); out=SheetsAdapter(fake,db).sync(pid)
    assert tuple(out["counts"]) == SHEET_TABS
    assert set(SHEET_HEADERS) == set(SHEET_TABS)
    assert all(SHEET_HEADERS[t] for t in SHEET_TABS)
    assert [x[1] for x in fake.writes] == [f"'{t}'" for t in SHEET_TABS]
    assert all(x[2] and list(x[2][0]) == list(SHEET_HEADERS[t]) for x,t in zip(fake.writes,SHEET_TABS))

def test_sheet_direct_dry_run_no_client_and_no_db_writes(tmp_path):
    db,pid=setup(tmp_path); before=db.conn.total_changes
    out=SheetsAdapter(NoCalls(),db).sync(pid,True)
    assert tuple(out["tabs"]) == SHEET_TABS and tuple(out["actions"]) == SHEET_TABS
    assert db.conn.total_changes == before

def test_sheet_malformed_then_valid_cross_project_and_outcomes(tmp_path):
    db,p1=setup(tmp_path,"a"); p2=Pipeline(db).init_project("b",scene_range=(1,2)); db.execute("insert into scenes(project_id,code,ord,start_ms,end_ms,text,state,approval_state,qa_state) values(?,?,?,?,?,?,?,?,?)",(p2,'S001',1,0,1000,'x','PLANNED','REQUIRED','PENDING'))
    rows=[{"command_id":"bad","command":"BOGUS"},{"command_id":"same","command":"APPROVE","scene_code":"S001"}]
    assert SheetsAdapter(Sheet(rows),db).ingest_commands(p1)["outcomes"] == ["QUARANTINED","COMPLETED"]
    assert SheetsAdapter(Sheet(rows[1:]),db).ingest_commands(p2)["outcomes"] == ["COMPLETED"]

def test_sheet_concurrent_claim_single_execution(tmp_path):
    db,pid=setup(tmp_path); path=db.path; rows=[{"command_id":"x","command":"APPROVE","scene_code":"S001"}]
    barrier=threading.Barrier(2); results=[]
    def run():
        d=Database(path); barrier.wait(); results.append(SheetsAdapter(Sheet(rows),d).ingest_commands(pid)); d.conn.close()
    ts=[threading.Thread(target=run) for _ in range(2)]; [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(r["processed"] for r in results)==1

class Drive:
    def __init__(self, offsets=None, meta=None): self.offsets=iter(offsets or [3]); self.meta=meta or {}
    def ensure_folder(self,p,n): return n
    def begin_upload(self,*a): return "s",0
    def upload_chunk(self,*a): return next(self.offsets)
    def finish_upload(self,s): return "r"
    def metadata(self,r): return self.meta

@pytest.mark.parametrize("offsets", [[0],[99],[-1]])
def test_drive_rejects_no_progress_overflow_negative(tmp_path, offsets):
    p=tmp_path/'x'; p.write_bytes(b'abc')
    with pytest.raises(VerificationError): DriveAdapter(Drive(offsets, {"size":3,"sha256":hashlib.sha256(b'abc').hexdigest()}),chunk_size=3).upload('p',p)

def test_drive_requires_real_checksum_not_etag(tmp_path):
    p=tmp_path/'x'; p.write_bytes(b'abc')
    with pytest.raises(VerificationError): DriveAdapter(Drive([3],{"size":3,"etag":"opaque"})).upload('p',p)

@pytest.mark.parametrize("returned", [1, 3])
def test_drive_rejects_partial_or_skipped_chunk_offset(tmp_path, returned):
    p=tmp_path/'x'; p.write_bytes(b'abcd')
    digest=hashlib.sha256(b'abcd').hexdigest()
    with pytest.raises(VerificationError, match='offset'):
        DriveAdapter(Drive([returned], {"size":4,"sha256":digest}),chunk_size=2).upload('p',p)

def test_drive_atomic_manifest_and_retry_exhaustion(tmp_path, monkeypatch):
    p=tmp_path/'x'; p.write_bytes(b'abc'); digest=hashlib.sha256(b'abc').hexdigest(); m=tmp_path/'m.json'
    DriveAdapter(Drive([3],{"size":3,"sha256":digest})).upload('p',p,manifest_path=m)
    assert json.loads(m.read_text())[0]["verified"] and not list(tmp_path.glob('.m.json.*'))
    class Boom(Drive):
        def ensure_folder(self,p,n):
            e=RuntimeError(); e.retryable=True; raise e
    with pytest.raises(RuntimeError): DriveAdapter(Boom(),sleep=lambda _:None).upload('p',p)

def test_factory_rejects_placeholder_protocol(monkeypatch,tmp_path):
    token=tmp_path/'token'; token.write_text('{}'); monkeypatch.setenv('DU_GOOGLE_OAUTH_TOKEN_PATH',str(token))
    monkeypatch.setenv('DU_INTEGRATION_CLIENT_FACTORY','du_pipeline.example_clients:placeholder_factory')
    with pytest.raises(IntegrationConfigurationError,match='missing required methods'): integration_client('sheets')

def test_discord_dry_run_zero_db_and_processing_status(tmp_path):
    db,pid=setup(tmp_path); b=DiscordBridge(Pipeline(db),{'u':'OWNER'}); before=db.conn.total_changes
    assert b.dispatch('m','u',f'du-trang-thai {pid}',True)['type']=='DRY_RUN'
    assert db.conn.total_changes==before
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state) values('z','u',NULL,'x','PROCESSING')")
    assert b.status('z')['type']=='DANG_XU_LY'

def test_discord_stale_takeover_and_reconciliation(tmp_path):
    db,pid=setup(tmp_path); b=DiscordBridge(Pipeline(db),{'u':'OWNER'},lease_seconds=1,owner='new')
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state,lease_owner,lease_expires_at) values('m','u',NULL,'x','PROCESSING','old','2000-01-01T00:00:00+00:00')")
    assert b.dispatch('m','u',f'du-trang-thai {pid}')['ok']
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state,lease_owner,lease_expires_at) values('crash','u',?, 'x','PROCESSING','old','2000-01-01T00:00:00+00:00')",(json.dumps({'type':'THANH_CONG','ok':True}),))
    assert b.status('crash')['ok']

def test_discord_unauthorized_user_cannot_retrieve_cached_response(tmp_path):
    db,pid=setup(tmp_path); b=DiscordBridge(Pipeline(db),{'u':'OWNER'})
    secret={'type':'THANH_CONG','ok':True,'data':{'secret':'do-not-leak'}}
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state) values('m','u',?,'x','COMPLETED')",(json.dumps(secret),))
    out=b.dispatch('m','intruder',f'du-trang-thai {pid}')
    assert out['type']=='TU_CHOI' and 'secret' not in json.dumps(out)
    assert tuple(db.one("select outcome,detail from integration_attempts where external_id='m'"))==('DENIED','unauthorized')

def test_discord_audits_replay_and_active_lease_without_response_data(tmp_path):
    db,pid=setup(tmp_path); b=DiscordBridge(Pipeline(db),{'u':'OWNER'})
    command=f'du-trang-thai {pid}'
    secret={'type':'THANH_CONG','ok':True,'data':{'secret':'do-not-audit'}}
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state) values('replay','u',?,'x','COMPLETED')",(json.dumps(secret),))
    db.execute("insert into discord_messages(message_id,user_id,response_json,created_at,state,lease_owner,lease_expires_at) values('leased','u',NULL,'x','PROCESSING','other','2999-01-01T00:00:00+00:00')")
    assert b.dispatch('replay','u',command)==secret
    assert b.dispatch('leased','u',command)['type']=='DANG_XU_LY'
    rows=[tuple(r) for r in db.all("select external_id,outcome,detail from integration_attempts where external_id in ('replay','leased') order by external_id")]
    assert rows==[('leased','ACTIVE_LEASE','processing'),('replay','REPLAY','cached')]
    assert 'secret' not in json.dumps(rows)