import hashlib
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.adapters import SHEET_TABS
from du_pipeline.integrations import SheetsAdapter,DriveAdapter,DiscordBridge,VerificationError,quote_a1,retry_call

class SheetFake:
 def __init__(self):self.tabs=[];self.created=0;self.upserts=[];self.rows=[]
 def find_spreadsheet(self,p):return None
 def create_spreadsheet(self,p):self.created+=1;return 'sheet'
 def get_tabs(self,s):return self.tabs
 def add_tabs(self,s,t):self.tabs+=t
 def batch_upsert(self,*a):self.upserts.append(a)
 def read_rows(self,*a):return self.rows

def project(tmp_path):
 db=Database(tmp_path/'x.db');pid=Pipeline(db).init_project('Demo',scene_range=(1,2));return db,pid

def test_sheet_exact_tabs_idempotent_batch_and_a1(tmp_path):
 db,pid=project(tmp_path);fake=SheetFake();adapter=SheetsAdapter(fake,db,batch_size=1)
 assert adapter.ensure(pid)=='sheet';assert tuple(fake.tabs)==SHEET_TABS
 assert adapter.ensure(pid)=='sheet' and fake.created==1
 adapter.sync(pid);assert fake.upserts[0][1]=="'PROJECT'";assert quote_a1("O'Brien")=="'O''Brien'"

def test_retry_backoff():
 class E(Exception):retryable=True
 calls=[]
 def work():
  calls.append(1)
  if len(calls)<3:raise E()
  return 7
 sleeps=[];assert retry_call(work,sleep=sleeps.append)==7;assert sleeps==[.25,.5]

class DriveFake:
 def __init__(self,bad=False):self.data=b'';self.bad=bad;self.offsets=[]
 def ensure_folder(self,parent,name):return parent+'/'+name
 def begin_upload(self,*a):self.data=b'ab';return 'session',2
 def upload_chunk(self,s,o,data):self.offsets.append(o);self.data+=data;return o+len(data)
 def finish_upload(self,s):return 'remote'
 def metadata(self,r):return {'size':999 if self.bad else len(self.data),'sha256':hashlib.sha256(self.data).hexdigest()}

def test_drive_resume_manifest_and_verification_keeps_local(tmp_path):
 path=tmp_path/'a.bin';path.write_bytes(b'abcdef');fake=DriveFake();result=DriveAdapter(fake,chunk_size=2).upload('p',path)
 assert fake.offsets[0]==2 and result['verified'] and path.exists() and (tmp_path/'upload-manifest.json').exists()
 bad=DriveFake(True)
 with pytest.raises(VerificationError):DriveAdapter(bad).upload('p',path)
 assert path.exists()

def test_discord_allowlist_rbac_and_message_idempotency(tmp_path):
 db,pid=project(tmp_path);bridge=DiscordBridge(Pipeline(db),{'owner':'OWNER','review':'REVIEWER'})
 denied=bridge.dispatch('1','nobody',f'du-trang-thai {pid}');assert denied['type']=='TU_CHOI'
 forbidden=bridge.dispatch('2','review',f'du-tam-dung {pid}');assert forbidden['type']=='TU_CHOI'
 first=bridge.dispatch('3','owner',f'du-trang-thai {pid}');second=bridge.dispatch('3','owner','du-unknown')
 assert first==second and first['type']=='THANH_CONG'