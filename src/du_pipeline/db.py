import sqlite3,uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime,timezone,timedelta

SCHEMA_VERSION = 12

class SchemaVersionError(RuntimeError): pass
class LeaseUnavailable(RuntimeError): pass

SCHEMA = '''
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,language TEXT NOT NULL CHECK(language IN ('vi','en')),
 state TEXT NOT NULL CHECK(state IN ('ACTIVE','PAUSED','BLOCKED','COMPLETED')),style_json TEXT NOT NULL,
 references_json TEXT NOT NULL,bible_json TEXT NOT NULL,image_provider TEXT NOT NULL,whiteboard_mode TEXT NOT NULL,
 seed INTEGER NOT NULL,transition TEXT NOT NULL,blocked_reason TEXT,created_at TEXT NOT NULL,
 min_scenes INTEGER NOT NULL DEFAULT 50 CHECK(min_scenes>0),max_scenes INTEGER NOT NULL DEFAULT 360 CHECK(max_scenes>=min_scenes),
 retention_days INTEGER NOT NULL DEFAULT 3 CHECK(retention_days>=0),output_json TEXT NOT NULL DEFAULT '{}',version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,kind TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','PAUSED','SUCCEEDED','FAILED','BLOCKED')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stages(id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,name TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','BLOCKED','SKIPPED')),checkpoint_json TEXT NOT NULL DEFAULT '{}',UNIQUE(job_id,name));
CREATE TABLE IF NOT EXISTS audio(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,uri TEXT NOT NULL,duration_ms INTEGER NOT NULL CHECK(duration_ms>0),sha256 TEXT NOT NULL CHECK(length(sha256)=64));
CREATE TABLE IF NOT EXISTS cues(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,idx INTEGER NOT NULL,start_ms INTEGER NOT NULL CHECK(start_ms>=0),end_ms INTEGER NOT NULL CHECK(end_ms>start_ms),text TEXT NOT NULL CHECK(length(trim(text))>0),UNIQUE(project_id,idx));
CREATE TABLE IF NOT EXISTS scenes(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,code TEXT NOT NULL,ord INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,special INTEGER NOT NULL DEFAULT 0 CHECK(special IN (0,1)),state TEXT NOT NULL CHECK(state IN ('PLANNED','RETRY_QUEUED','RETRYABLE','IMAGE_READY','BLOCKED','ANIMATED')),approval_state TEXT NOT NULL CHECK(approval_state IN ('REQUIRED','NOT_REQUIRED','APPROVED','REJECTED')),qa_state TEXT NOT NULL DEFAULT 'PENDING',qa_json TEXT NOT NULL DEFAULT '{}',duration_exception TEXT,continuity_json TEXT NOT NULL DEFAULT '{}',checkpoint_json TEXT NOT NULL DEFAULT '{}',UNIQUE(project_id,code));
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),provider TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),error TEXT,failed_path TEXT,failed_sha256 TEXT,failed_size INTEGER,created_at TEXT NOT NULL,UNIQUE(scene_id,number));
CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,kind TEXT NOT NULL,uri TEXT NOT NULL,sha256 TEXT NOT NULL,version INTEGER NOT NULL,parent_id INTEGER REFERENCES artifacts(id),status TEXT NOT NULL CHECK(status IN ('ACTIVE','DELETED','SUPERSEDED')),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,deleted_at TEXT,UNIQUE(project_id,scene_id,kind,version));
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,gate TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),actor TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,stage TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('PROPOSED','APPROVED','REJECTED','APPLIED')),actor TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS impact_proposals(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),dependency TEXT,impact_json TEXT,state TEXT,proposer TEXT,decider TEXT,created_at TEXT,decided_at TEXT,applied_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,type TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cost_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),provider TEXT,currency TEXT,amount TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS time_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),stage TEXT,seconds REAL,created_at TEXT);
CREATE TABLE IF NOT EXISTS learning_metadata(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),key TEXT,value_json TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS integration_projects(project_id TEXT PRIMARY KEY REFERENCES projects(id),spreadsheet_id TEXT);
CREATE TABLE IF NOT EXISTS integration_commands(source TEXT,external_id TEXT,project_id TEXT REFERENCES projects(id),created_at TEXT,PRIMARY KEY(source,external_id));
CREATE TABLE IF NOT EXISTS discord_messages(message_id TEXT PRIMARY KEY,user_id TEXT,response_json TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS reconciliation_leases(name TEXT PRIMARY KEY,owner_token TEXT NOT NULL,expires_at TEXT NOT NULL);
'''
APPEND=("events","cost_observations","time_observations","learning_metadata")
class Database:
 def __init__(self,path):
  self.path=Path(path); self.conn=sqlite3.connect(self.path); self._tx_depth=0
  try:
   self.conn.row_factory=sqlite3.Row
   self.conn.execute('PRAGMA foreign_keys=ON'); self.conn.execute('PRAGMA journal_mode=WAL')
   version=self.conn.execute('PRAGMA user_version').fetchone()[0]
   if version>SCHEMA_VERSION: raise SchemaVersionError(f'unsupported schema version {version}')
   self.conn.execute('BEGIN IMMEDIATE')
   # executescript commits implicitly; execute individual DDL so migration is atomic.
   if version == 0:
    for statement in SCHEMA.split(';'):
     if statement.strip(): self.conn.execute(statement)
    self.conn.execute('PRAGMA user_version=1'); version=1
   # Explicit 1 -> 2 additive upgrade from the original schema; 2 is verified below.
   if version not in (1,2,3,4,5,6,7,8,9,10,11,12): raise SchemaVersionError(f'unsupported schema version {version}')
   cols={r[1] for r in self.conn.execute('pragma table_info(projects)')}
   if version == 2 and 'artifact_root' not in cols: raise SchemaVersionError('schema version 2 does not match structure')
   additions=[('min_scenes','INTEGER NOT NULL DEFAULT 50'),('max_scenes','INTEGER NOT NULL DEFAULT 360'),('retention_days','INTEGER NOT NULL DEFAULT 3'),('output_json',"TEXT NOT NULL DEFAULT '{}'") ,('version','INTEGER NOT NULL DEFAULT 1'),('artifact_root','TEXT')]
   for name,ddl in additions:
    if name not in cols:self.conn.execute(f'ALTER TABLE projects ADD COLUMN {name} {ddl}')
   scene_cols={r[1] for r in self.conn.execute('pragma table_info(scenes)')}
   for name,ddl in [('qa_state',"TEXT NOT NULL DEFAULT 'PENDING'"),('qa_json',"TEXT NOT NULL DEFAULT '{}'"),('duration_exception','TEXT')]:
    if name not in scene_cols:self.conn.execute(f'ALTER TABLE scenes ADD COLUMN {name} {ddl}')
   for t in APPEND:
    self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
    self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
   self.conn.execute('CREATE TABLE IF NOT EXISTS integration_projects(project_id TEXT PRIMARY KEY REFERENCES projects(id),spreadsheet_id TEXT)')
   self.conn.execute('CREATE TABLE IF NOT EXISTS integration_commands(source TEXT,external_id TEXT,project_id TEXT REFERENCES projects(id),created_at TEXT,PRIMARY KEY(source,external_id))')
   self.conn.execute('CREATE TABLE IF NOT EXISTS discord_messages(message_id TEXT PRIMARY KEY,user_id TEXT,response_json TEXT,created_at TEXT)')
   self.conn.execute('CREATE TABLE IF NOT EXISTS reconciliation_leases(name TEXT PRIMARY KEY,owner_token TEXT NOT NULL,expires_at TEXT NOT NULL)')
   dcols={r[1] for r in self.conn.execute('pragma table_info(discord_messages)')}
   if 'state' not in dcols:self.conn.execute("ALTER TABLE discord_messages ADD COLUMN state TEXT NOT NULL DEFAULT 'COMPLETED'")
   if 'error' not in dcols:self.conn.execute('ALTER TABLE discord_messages ADD COLUMN error TEXT')
   self.conn.execute('CREATE TABLE IF NOT EXISTS integration_attempts(id INTEGER PRIMARY KEY,source TEXT NOT NULL,external_id TEXT,user_id TEXT,outcome TEXT NOT NULL,detail TEXT,created_at TEXT NOT NULL)')
   icols={r[1] for r in self.conn.execute('pragma table_info(integration_commands)')}
   if 'state' not in icols:
    self.conn.execute('ALTER TABLE integration_commands RENAME TO integration_commands_old')
    self.conn.execute("CREATE TABLE integration_commands(source TEXT,external_id TEXT,project_id TEXT REFERENCES projects(id),created_at TEXT,state TEXT NOT NULL DEFAULT 'COMPLETED',lease_owner TEXT,lease_expires_at TEXT,detail TEXT,PRIMARY KEY(source,project_id,external_id))")
    self.conn.execute("INSERT INTO integration_commands(source,external_id,project_id,created_at) SELECT source,external_id,project_id,created_at FROM integration_commands_old")
    self.conn.execute('DROP TABLE integration_commands_old')
   dcols={r[1] for r in self.conn.execute('pragma table_info(discord_messages)')}
   if 'lease_owner' not in dcols:self.conn.execute('ALTER TABLE discord_messages ADD COLUMN lease_owner TEXT')
   if 'lease_expires_at' not in dcols:self.conn.execute('ALTER TABLE discord_messages ADD COLUMN lease_expires_at TEXT')
   # Before v7 Discord PROCESSING meant only that the bridge had started.  There
   # is no durable receipt from which to infer whether its side effect happened.
   # Retrying such rows could repeat an approval or other canonical mutation, so
   # migration deliberately fails closed and leaves an auditable resolution job.
   if version < 7:
    self.conn.execute("UPDATE discord_messages SET state='MANUAL_REVIEW',error='V7_LEGACY_PROCESSING_SIDE_EFFECT_UNKNOWN',lease_owner=NULL,lease_expires_at=NULL WHERE state='PROCESSING'")
   # Core command effects are committed atomically with this receipt.  A bridge
   # lease may be taken over, but the stable external key cannot mutate core twice.
   self.conn.execute('CREATE TABLE IF NOT EXISTS command_receipts(idempotency_key TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL)')
   self.conn.execute('CREATE TABLE IF NOT EXISTS final_assemblies(artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,project_version INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL UNIQUE,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL)')
   self.conn.execute("""CREATE TABLE IF NOT EXISTS publication_journal(token TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,project_version INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL,current_evidence_sha256 TEXT NOT NULL,staging_path TEXT NOT NULL,final_path TEXT NOT NULL,output_sha256 TEXT NOT NULL,output_size INTEGER NOT NULL,manifest_json TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('PREPARED','COMMITTED','ABORTED','MANUAL_REVIEW')),artifact_id INTEGER REFERENCES artifacts(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT,UNIQUE(final_path),UNIQUE(evidence_sha256))""")
   self.conn.execute("CREATE INDEX IF NOT EXISTS idx_publication_state ON publication_journal(state,created_at)")
   fks=self.conn.execute('PRAGMA foreign_key_list(final_assemblies)').fetchall()
   if fks and any(r[6].upper()!='CASCADE' for r in fks):
    self.conn.execute('ALTER TABLE final_assemblies RENAME TO final_assemblies_legacy')
    self.conn.execute('CREATE TABLE final_assemblies(artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,project_version INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL UNIQUE,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL)')
    self.conn.execute('INSERT INTO final_assemblies SELECT * FROM final_assemblies_legacy')
    self.conn.execute('DROP TABLE final_assemblies_legacy')
   acols={r[1] for r in self.conn.execute('pragma table_info(approvals)')}
   for name,ddl in [('project_version','INTEGER'),('evidence_sha256','TEXT'),('revoked_at','TEXT'),('revoked_reason','TEXT')]:
    if name not in acols:self.conn.execute(f'ALTER TABLE approvals ADD COLUMN {name} {ddl}')
   jcols={r[1] for r in self.conn.execute('pragma table_info(jobs)')}
   for name,ddl in [('project_version','INTEGER'),('lease_owner','TEXT'),('lease_expires_at','TEXT'),('attempt_count','INTEGER NOT NULL DEFAULT 0')]:
    if name not in jcols:self.conn.execute(f'ALTER TABLE jobs ADD COLUMN {name} {ddl}')
   self.conn.execute("UPDATE jobs SET project_version=(SELECT version FROM projects WHERE projects.id=jobs.project_id) WHERE project_version IS NULL")
   groups=self.conn.execute("SELECT project_id,kind,project_version FROM jobs WHERE state IN ('QUEUED','RUNNING','PAUSED') GROUP BY project_id,kind,project_version HAVING count(*)>1").fetchall()
   for group in groups:
    active=self.conn.execute("SELECT id,state FROM jobs WHERE project_id=? AND kind=? AND project_version=? AND state IN ('QUEUED','RUNNING','PAUSED') ORDER BY CASE state WHEN 'RUNNING' THEN 3 WHEN 'PAUSED' THEN 2 ELSE 1 END DESC, updated_at DESC, id DESC",(group['project_id'],group['kind'],group['project_version'])).fetchall()
    keep_id=active[0]['id']; duplicate_ids=[r['id'] for r in active[1:]]
    previous_states={str(r['id']):r['state'] for r in active[1:]}
    self.conn.executemany("UPDATE jobs SET state='BLOCKED',lease_owner=NULL,lease_expires_at=NULL WHERE id=?",[(i,) for i in duplicate_ids])
    import json,datetime
    detail=json.dumps({'kind':group['kind'],'project_version':group['project_version'],'retained_job_id':keep_id,'retained_state':active[0]['state'],'blocked_job_ids':duplicate_ids,'blocked_previous_states':previous_states,'selection_policy':'RUNNING>PAUSED>QUEUED, updated_at DESC, id DESC'},sort_keys=True)
    self.conn.execute("INSERT INTO events(project_id,type,data_json,created_at) VALUES(?,?,?,?)",(group['project_id'],'MIGRATION_ACTIVE_JOBS_RECONCILED',detail,datetime.datetime.now(datetime.timezone.utc).isoformat()))
   self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active ON jobs(project_id,kind,project_version) WHERE state IN ('QUEUED','RUNNING','PAUSED')")
   self.conn.execute('CREATE INDEX IF NOT EXISTS idx_scenes_project_ord ON scenes(project_id,ord)')
   self.conn.execute('CREATE INDEX IF NOT EXISTS idx_jobs_project_state_id ON jobs(project_id,state,id)')
   self.conn.execute('CREATE INDEX IF NOT EXISTS idx_events_project_type_id ON events(project_id,type,id)')
   self.conn.execute('CREATE INDEX IF NOT EXISTS idx_artifacts_cleanup ON artifacts(status,expires_at,project_id)')
   # v12 preserves attempts when a plan replaces scene rows. Epoch zero is
   # exactly the historical 1..3 budget; approved reruns allocate later epochs.
   attempt_cols={r[1] for r in self.conn.execute('pragma table_info(attempts)')}
   if 'epoch' not in attempt_cols:
    self.conn.execute('ALTER TABLE attempts RENAME TO attempts_legacy_v11')
    self.conn.execute("CREATE TABLE attempts(id INTEGER PRIMARY KEY,scene_id INTEGER REFERENCES scenes(id) ON DELETE SET NULL,number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),provider TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),error TEXT,failed_path TEXT,failed_sha256 TEXT,failed_size INTEGER,created_at TEXT NOT NULL,project_id TEXT REFERENCES projects(id),scene_code TEXT,epoch INTEGER NOT NULL DEFAULT 0 CHECK(epoch>=0),UNIQUE(scene_id,epoch,number))")
    self.conn.execute('INSERT INTO attempts(id,scene_id,number,provider,state,error,failed_path,failed_sha256,failed_size,created_at,project_id,scene_code,epoch) SELECT a.id,a.scene_id,a.number,a.provider,a.state,a.error,a.failed_path,a.failed_sha256,a.failed_size,a.created_at,s.project_id,s.code,0 FROM attempts_legacy_v11 a JOIN scenes s ON s.id=a.scene_id')
    if self.conn.execute('SELECT count(*) FROM attempts').fetchone()[0]!=self.conn.execute('SELECT count(*) FROM attempts_legacy_v11').fetchone()[0]: raise SchemaVersionError('attempt owner missing during migration')
    self.conn.execute('DROP TABLE attempts_legacy_v11')
   self.conn.execute('CREATE INDEX IF NOT EXISTS idx_attempts_scene_number ON attempts(scene_id,epoch,number)')
   self.conn.execute('CREATE TABLE IF NOT EXISTS remote_uploads(project_id TEXT,local_path TEXT,remote_id TEXT,state TEXT,size INTEGER,sha256 TEXT,error TEXT,updated_at TEXT,PRIMARY KEY(project_id,local_path))')
   # Composite ownership safeguards without rebuilding legacy tables.
   ownership=(
    ('artifact_scene_project','artifacts',"NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id)",'cross-project scene'),
    ('artifact_parent_project','artifacts',"NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.parent_id AND project_id=NEW.project_id)",'cross-project parent'),
    ('approval_scene_project','approvals',"NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id)",'cross-project approval'))
   for name,table,condition,message in ownership:
    for operation in ('INSERT','UPDATE'):
     self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name}_{operation.lower()} BEFORE {operation} ON {table} WHEN {condition} BEGIN SELECT RAISE(ABORT,'{message}'); END")
   self.conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
   if self.conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise sqlite3.DatabaseError('integrity check failed')
   if self.conn.execute('PRAGMA foreign_key_check').fetchone(): raise sqlite3.IntegrityError('foreign key check failed')
   self.conn.commit()
  except Exception:
   self.conn.rollback(); self.conn.close(); self.conn=None; raise
 def close(self):
  if self.conn is not None:self.conn.close();self.conn=None
 def __enter__(self): return self
 def __exit__(self,*exc): self.close()
 @contextmanager
 def transaction(self):
  outer=self._tx_depth==0
  if outer:self.conn.execute('BEGIN IMMEDIATE')
  self._tx_depth+=1
  try:
   yield self
   self._tx_depth-=1
   if outer:self.conn.commit()
  except Exception:
   self._tx_depth-=1
   if outer:self.conn.rollback()
   raise
 def execute(self,sql,args=()):
  cur=self.conn.execute(sql,args)
  if not self._tx_depth:self.conn.commit()
  return cur
 def one(self,sql,args=()): return self.conn.execute(sql,args).fetchone()
 def all(self,sql,args=()): return self.conn.execute(sql,args).fetchall()
 def acquire_reconciliation_lease(self,ttl_seconds=300):
  """Atomically acquire/reclaim the host-local reconciler lease."""
  token=uuid.uuid4().hex; current=datetime.now(timezone.utc); expires=current+timedelta(seconds=ttl_seconds)
  with self.transaction():
   changed=self.conn.execute("INSERT INTO reconciliation_leases(name,owner_token,expires_at) VALUES('artifact-deletion',?,?) ON CONFLICT(name) DO UPDATE SET owner_token=excluded.owner_token,expires_at=excluded.expires_at WHERE reconciliation_leases.expires_at<=?",(token,expires.isoformat(),current.isoformat())).rowcount
   if changed!=1: raise LeaseUnavailable('artifact deletion reconciliation lease unavailable')
  return token
 def renew_reconciliation_lease(self,token,ttl_seconds=300):
  current=datetime.now(timezone.utc); expires=current+timedelta(seconds=ttl_seconds)
  with self.transaction():
   changed=self.conn.execute("UPDATE reconciliation_leases SET expires_at=? WHERE name='artifact-deletion' AND owner_token=? AND expires_at>?",(expires.isoformat(),token,current.isoformat())).rowcount
   if changed!=1: raise LeaseUnavailable('artifact deletion reconciliation lease lost')
 def assert_reconciliation_lease(self,token):
  """Fence one filesystem operation; caller must hold a write transaction."""
  current=datetime.now(timezone.utc).isoformat()
  if not self.one("SELECT 1 FROM reconciliation_leases WHERE name='artifact-deletion' AND owner_token=? AND expires_at>?",(token,current)):
   raise LeaseUnavailable('artifact deletion reconciliation lease lost')
 def release_reconciliation_lease(self,token):
  with self.transaction():
   return self.conn.execute("DELETE FROM reconciliation_leases WHERE name='artifact-deletion' AND owner_token=?",(token,)).rowcount==1