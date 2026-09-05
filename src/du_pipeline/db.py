import sqlite3
from pathlib import Path
SCHEMA='''
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,language TEXT CHECK(language IN ('vi','en')),state TEXT,style_json TEXT,references_json TEXT,bible_json TEXT,image_provider TEXT,whiteboard_mode TEXT,seed INTEGER,transition TEXT,blocked_reason TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,project_id TEXT,kind TEXT,state TEXT);
CREATE TABLE IF NOT EXISTS stages(id INTEGER PRIMARY KEY,job_id INTEGER,name TEXT,state TEXT);
CREATE TABLE IF NOT EXISTS audio(project_id TEXT PRIMARY KEY,uri TEXT,duration_ms INTEGER,sha256 TEXT);
CREATE TABLE IF NOT EXISTS cues(id INTEGER PRIMARY KEY,project_id TEXT,idx INTEGER,start_ms INTEGER,end_ms INTEGER,text TEXT);
CREATE TABLE IF NOT EXISTS scenes(id INTEGER PRIMARY KEY,project_id TEXT,code TEXT,ord INTEGER,start_ms INTEGER,end_ms INTEGER,text TEXT,special INTEGER DEFAULT 0,state TEXT,approval_state TEXT,UNIQUE(project_id,code));
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,scene_id INTEGER,number INTEGER,provider TEXT,state TEXT,error TEXT,created_at TEXT,UNIQUE(scene_id,number));
CREATE TABLE IF NOT EXISTS artifacts(id INTEGER PRIMARY KEY,project_id TEXT,scene_id INTEGER,kind TEXT,uri TEXT,sha256 TEXT,version INTEGER,parent_id INTEGER,status TEXT,created_at TEXT,expires_at TEXT,deleted_at TEXT);
CREATE TABLE IF NOT EXISTS approvals(id INTEGER PRIMARY KEY,project_id TEXT,scene_id INTEGER,gate TEXT,decision TEXT,actor TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS impact_proposals(id INTEGER PRIMARY KEY,project_id TEXT,dependency TEXT,impact_json TEXT,state TEXT,proposer TEXT,decider TEXT,created_at TEXT,decided_at TEXT,applied_at TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,project_id TEXT,type TEXT,data_json TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS cost_observations(id INTEGER PRIMARY KEY,project_id TEXT,provider TEXT,currency TEXT,amount TEXT,created_at TEXT);
CREATE TABLE IF NOT EXISTS time_observations(id INTEGER PRIMARY KEY,project_id TEXT,stage TEXT,seconds REAL,created_at TEXT);
CREATE TABLE IF NOT EXISTS learning_metadata(id INTEGER PRIMARY KEY,project_id TEXT,key TEXT,value_json TEXT,created_at TEXT);
'''
APPEND=("events","cost_observations","time_observations","learning_metadata")
class Database:
 def __init__(self,path):
  self.path=Path(path); self.conn=sqlite3.connect(self.path); self.conn.row_factory=sqlite3.Row; self.conn.execute('PRAGMA foreign_keys=ON'); self.conn.execute('PRAGMA journal_mode=WAL'); self.conn.executescript(SCHEMA)
  for t in APPEND:
   self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
   self.conn.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} BEGIN SELECT RAISE(ABORT,'append only'); END")
  self.conn.commit()
 def execute(self,sql,args=()):
  cur=self.conn.execute(sql,args); self.conn.commit(); return cur
 def one(self,sql,args=()): return self.conn.execute(sql,args).fetchone()
 def all(self,sql,args=()): return self.conn.execute(sql,args).fetchall()
