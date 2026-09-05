import sqlite3
from pathlib import Path

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
CREATE TABLE IF NOT EXISTS scenes(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,code TEXT NOT NULL,ord INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,special INTEGER NOT NULL DEFAULT 0 CHECK(special IN (0,1)),state TEXT NOT NULL CHECK(state IN ('PLANNED','RETRY_QUEUED','RETRYABLE','IMAGE_READY','BLOCKED','ANIMATED')),approval_state TEXT NOT NULL CHECK(approval_state IN ('REQUIRED','NOT_REQUIRED','APPROVED','REJECTED')),qa_state TEXT NOT NULL DEFAULT 'PENDING',duration_exception TEXT,continuity_json TEXT NOT NULL DEFAULT '{}',checkpoint_json TEXT NOT NULL DEFAULT '{}',UNIQUE(project_id,code));
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),provider TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),error TEXT,failed_path TEXT,failed_sha256 TEXT,failed_size INTEGER,created_at TEXT NOT NULL,UNIQUE(scene_id,number));
CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,kind TEXT NOT NULL,uri TEXT NOT NULL,sha256 TEXT NOT NULL,version INTEGER NOT NULL,parent_id INTEGER REFERENCES artifacts(id),status TEXT NOT NULL CHECK(status IN ('ACTIVE','DELETED','SUPERSEDED')),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,deleted_at TEXT,UNIQUE(project_id,scene_id,kind,version));
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,gate TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),actor TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,stage TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('PROPOSED','APPROVED','REJECTED','APPLIED')),actor TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS impact_proposals(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),dependency TEXT,impact_json TEXT,state TEXT,proposer TEXT,decider TEXT,created_at TEXT,decided_at TEXT,applied_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,type TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cost_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),provider TEXT,currency TEXT,amount TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS time_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),stage TEXT,seconds REAL,created_at TEXT);
CREATE TABLE IF NOT EXISTS learning_metadata(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),key TEXT,value_json TEXT,created_at TEXT);
'''
APPEND=("events","cost_observations","time_observations","learning_metadata")
class Database:
 def __init__(self,path):
  self.path=Path(path); self.conn=sqlite3.connect(self.path); self.conn.row_factory=sqlite3.Row
  self.conn.execute('PRAGMA foreign_keys=ON'); self.conn.execute('PRAGMA journal_mode=WAL'); self.conn.executescript(SCHEMA)
  # Additive migration for databases produced by v1.
  cols={r[1] for r in self.conn.execute('pragma table_info(projects)')}
  for name,ddl in [('min_scenes','INTEGER NOT NULL DEFAULT 50'),('max_scenes','INTEGER NOT NULL DEFAULT 360'),('retention_days','INTEGER NOT NULL DEFAULT 3'),('output_json',"TEXT NOT NULL DEFAULT '{}'") ,('version','INTEGER NOT NULL DEFAULT 1')]:
   if name not in cols:self.conn.execute(f'ALTER TABLE projects ADD COLUMN {name} {ddl}')
  scene_cols={r[1] for r in self.conn.execute('pragma table_info(scenes)')}
  for name,ddl in [('qa_state',"TEXT NOT NULL DEFAULT 'PENDING'"),('duration_exception','TEXT')]:
   if name not in scene_cols:self.conn.execute(f'ALTER TABLE scenes ADD COLUMN {name} {ddl}')
  for t in APPEND:
   self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
   self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
  self.conn.commit()
 def execute(self,sql,args=()): cur=self.conn.execute(sql,args); self.conn.commit(); return cur
 def one(self,sql,args=()): return self.conn.execute(sql,args).fetchone()
 def all(self,sql,args=()): return self.conn.execute(sql,args).fetchall()