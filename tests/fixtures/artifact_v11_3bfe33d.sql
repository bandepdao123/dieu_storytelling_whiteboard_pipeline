-- Historical schema: v11_3bfe33d; generated from actual Database constructor, not relabelled current schema.
BEGIN TRANSACTION;
CREATE TABLE approvals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,gate TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),actor TEXT NOT NULL,created_at TEXT NOT NULL, project_version INTEGER, evidence_sha256 TEXT, revoked_at TEXT, revoked_reason TEXT);
CREATE TABLE artifacts(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,scene_id INTEGER REFERENCES scenes(id) ON DELETE CASCADE,kind TEXT NOT NULL,uri TEXT NOT NULL,sha256 TEXT NOT NULL,version INTEGER NOT NULL,parent_id INTEGER REFERENCES artifacts(id),status TEXT NOT NULL CHECK(status IN ('ACTIVE','DELETED','SUPERSEDED')),created_at TEXT NOT NULL,expires_at TEXT NOT NULL,deleted_at TEXT,UNIQUE(project_id,scene_id,kind,version));
CREATE TABLE attempts(id INTEGER PRIMARY KEY,scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),provider TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),error TEXT,failed_path TEXT,failed_sha256 TEXT,failed_size INTEGER,created_at TEXT NOT NULL,UNIQUE(scene_id,number));
CREATE TABLE audio(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,uri TEXT NOT NULL,duration_ms INTEGER NOT NULL CHECK(duration_ms>0),sha256 TEXT NOT NULL CHECK(length(sha256)=64));
CREATE TABLE command_receipts(idempotency_key TEXT PRIMARY KEY,request_sha256 TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE cost_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),provider TEXT,currency TEXT,amount TEXT,created_at TEXT);
CREATE TABLE cues(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,idx INTEGER NOT NULL,start_ms INTEGER NOT NULL CHECK(start_ms>=0),end_ms INTEGER NOT NULL CHECK(end_ms>start_ms),text TEXT NOT NULL CHECK(length(trim(text))>0),UNIQUE(project_id,idx));
CREATE TABLE discord_messages(message_id TEXT PRIMARY KEY,user_id TEXT,response_json TEXT,created_at TEXT, state TEXT NOT NULL DEFAULT 'COMPLETED', error TEXT, lease_owner TEXT, lease_expires_at TEXT);
CREATE TABLE events(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,type TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE final_assemblies(artifact_id INTEGER PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,project_version INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL UNIQUE,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE impact_proposals(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),dependency TEXT,impact_json TEXT,state TEXT,proposer TEXT,decider TEXT,created_at TEXT,decided_at TEXT,applied_at TEXT);
CREATE TABLE integration_attempts(id INTEGER PRIMARY KEY,source TEXT NOT NULL,external_id TEXT,user_id TEXT,outcome TEXT NOT NULL,detail TEXT,created_at TEXT NOT NULL);
CREATE TABLE integration_commands(source TEXT,external_id TEXT,project_id TEXT REFERENCES projects(id),created_at TEXT,state TEXT NOT NULL DEFAULT 'COMPLETED',lease_owner TEXT,lease_expires_at TEXT,detail TEXT,PRIMARY KEY(source,project_id,external_id));
CREATE TABLE integration_projects(project_id TEXT PRIMARY KEY REFERENCES projects(id),spreadsheet_id TEXT);
CREATE TABLE jobs(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,kind TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','PAUSED','SUCCEEDED','FAILED','BLOCKED')),created_at TEXT NOT NULL,updated_at TEXT NOT NULL, project_version INTEGER, lease_owner TEXT, lease_expires_at TEXT, attempt_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE learning_metadata(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),key TEXT,value_json TEXT,created_at TEXT);
CREATE TABLE projects(
 id TEXT PRIMARY KEY,name TEXT NOT NULL,language TEXT NOT NULL CHECK(language IN ('vi','en')),
 state TEXT NOT NULL CHECK(state IN ('ACTIVE','PAUSED','BLOCKED','COMPLETED')),style_json TEXT NOT NULL,
 references_json TEXT NOT NULL,bible_json TEXT NOT NULL,image_provider TEXT NOT NULL,whiteboard_mode TEXT NOT NULL,
 seed INTEGER NOT NULL,transition TEXT NOT NULL,blocked_reason TEXT,created_at TEXT NOT NULL,
 min_scenes INTEGER NOT NULL DEFAULT 50 CHECK(min_scenes>0),max_scenes INTEGER NOT NULL DEFAULT 360 CHECK(max_scenes>=min_scenes),
 retention_days INTEGER NOT NULL DEFAULT 3 CHECK(retention_days>=0),output_json TEXT NOT NULL DEFAULT '{}',version INTEGER NOT NULL DEFAULT 1, artifact_root TEXT);
CREATE TABLE proposals(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,stage TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('PROPOSED','APPROVED','REJECTED','APPLIED')),actor TEXT,created_at TEXT NOT NULL);
CREATE TABLE publication_journal(token TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,project_version INTEGER NOT NULL,evidence_sha256 TEXT NOT NULL,current_evidence_sha256 TEXT NOT NULL,staging_path TEXT NOT NULL,final_path TEXT NOT NULL,output_sha256 TEXT NOT NULL,output_size INTEGER NOT NULL,manifest_json TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('PREPARED','COMMITTED','ABORTED','MANUAL_REVIEW')),artifact_id INTEGER REFERENCES artifacts(id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL,error TEXT,UNIQUE(final_path),UNIQUE(evidence_sha256));
CREATE TABLE reconciliation_leases(name TEXT PRIMARY KEY,owner_token TEXT NOT NULL,expires_at TEXT NOT NULL);
CREATE TABLE remote_uploads(project_id TEXT,local_path TEXT,remote_id TEXT,state TEXT,size INTEGER,sha256 TEXT,error TEXT,updated_at TEXT,PRIMARY KEY(project_id,local_path));
CREATE TABLE scenes(id INTEGER PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,code TEXT NOT NULL,ord INTEGER NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,special INTEGER NOT NULL DEFAULT 0 CHECK(special IN (0,1)),state TEXT NOT NULL CHECK(state IN ('PLANNED','RETRY_QUEUED','RETRYABLE','IMAGE_READY','BLOCKED','ANIMATED')),approval_state TEXT NOT NULL CHECK(approval_state IN ('REQUIRED','NOT_REQUIRED','APPROVED','REJECTED')),qa_state TEXT NOT NULL DEFAULT 'PENDING',qa_json TEXT NOT NULL DEFAULT '{}',duration_exception TEXT,continuity_json TEXT NOT NULL DEFAULT '{}',checkpoint_json TEXT NOT NULL DEFAULT '{}',UNIQUE(project_id,code));
CREATE TABLE stages(id INTEGER PRIMARY KEY,job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,name TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','BLOCKED','SKIPPED')),checkpoint_json TEXT NOT NULL DEFAULT '{}',UNIQUE(job_id,name));
CREATE TABLE time_observations(id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),stage TEXT,seconds REAL,created_at TEXT);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER cost_observations_no_update BEFORE UPDATE ON cost_observations BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER cost_observations_no_delete BEFORE DELETE ON cost_observations BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER time_observations_no_update BEFORE UPDATE ON time_observations BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER time_observations_no_delete BEFORE DELETE ON time_observations BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER learning_metadata_no_update BEFORE UPDATE ON learning_metadata BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE TRIGGER learning_metadata_no_delete BEFORE DELETE ON learning_metadata BEGIN SELECT RAISE(ABORT,'append only'); END;
CREATE INDEX idx_publication_state ON publication_journal(state,created_at);
CREATE UNIQUE INDEX idx_jobs_one_active ON jobs(project_id,kind,project_version) WHERE state IN ('QUEUED','RUNNING','PAUSED');
CREATE INDEX idx_scenes_project_ord ON scenes(project_id,ord);
CREATE INDEX idx_jobs_project_state_id ON jobs(project_id,state,id);
CREATE INDEX idx_events_project_type_id ON events(project_id,type,id);
CREATE INDEX idx_artifacts_cleanup ON artifacts(status,expires_at,project_id);
CREATE INDEX idx_attempts_scene_number ON attempts(scene_id,number);
CREATE TRIGGER artifact_scene_project_insert BEFORE INSERT ON artifacts WHEN NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project scene'); END;
CREATE TRIGGER artifact_scene_project_update BEFORE UPDATE ON artifacts WHEN NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project scene'); END;
CREATE TRIGGER artifact_parent_project_insert BEFORE INSERT ON artifacts WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.parent_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project parent'); END;
CREATE TRIGGER artifact_parent_project_update BEFORE UPDATE ON artifacts WHEN NEW.parent_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM artifacts WHERE id=NEW.parent_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project parent'); END;
CREATE TRIGGER approval_scene_project_insert BEFORE INSERT ON approvals WHEN NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project approval'); END;
CREATE TRIGGER approval_scene_project_update BEFORE UPDATE ON approvals WHEN NEW.scene_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM scenes WHERE id=NEW.scene_id AND project_id=NEW.project_id) BEGIN SELECT RAISE(ABORT,'cross-project approval'); END;
COMMIT;
PRAGMA user_version=11;
