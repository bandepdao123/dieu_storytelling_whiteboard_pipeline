"""Historical attempts DDL (v1-v11), not relabelled modern attempts."""
import sqlite3
import pytest
from du_pipeline.db import Database, SCHEMA

LEGACY_ATTEMPTS="""CREATE TABLE attempts(id INTEGER PRIMARY KEY,scene_id INTEGER NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,number INTEGER NOT NULL CHECK(number BETWEEN 1 AND 3),provider TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RUNNING','SUCCEEDED','FAILED')),error TEXT,failed_path TEXT,failed_sha256 TEXT,failed_size INTEGER,created_at TEXT NOT NULL,UNIQUE(scene_id,number))"""


def historical(path):
    c=sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute('drop table attempts'); c.execute(LEGACY_ATTEMPTS)
    c.execute("insert into projects(id,name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,created_at) values('p','old','vi','ACTIVE','{}','[]','{}','codex-gpt-image-2','ask',0,'hard_cut','old')")
    c.execute("insert into scenes(id,project_id,code,ord,start_ms,end_ms,text,state,approval_state) values(7,'p','S001',1,0,6000,'text','BLOCKED','REQUIRED')")
    c.execute("insert into attempts values(9,7,1,'codex-gpt-image-2','FAILED','old failure','old-path',?,3,'old')",('a'*64,))
    c.execute('pragma user_version=1'); c.commit(); c.close()


def test_historical_attempt_migration_preserves_owner_and_detaches(tmp_path):
    path=tmp_path/'legacy.db'; historical(path)
    with Database(path) as db:
        row=dict(db.one('select * from attempts'))
        assert row.get('epoch')==0
        assert row.get('project_id')=='p'
        assert row['id']==9 and row['error']=='old failure'
        db.execute('delete from scenes where id=7')
        assert db.one('select scene_id from attempts')['scene_id'] is None
        assert not db.all('pragma foreign_key_check')
    with Database(path) as db:
        assert db.one('select count(*) n from attempts')['n']==1


def test_migration_failure_rolls_back_historical_table(tmp_path,monkeypatch):
    path=tmp_path/'legacy.db'; historical(path)
    original=sqlite3.connect
    class Failure(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql.startswith('PRAGMA user_version=12'):
                raise sqlite3.OperationalError('injected migration failure')
            return super().execute(sql,*args,**kwargs)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:original(*a,factory=Failure,**kw))
    with pytest.raises(sqlite3.OperationalError,match='injected'): Database(path)
    c=original(path)
    assert c.execute('pragma user_version').fetchone()[0]==1
    assert 'epoch' not in [r[1] for r in c.execute('pragma table_info(attempts)')]
    assert c.execute('select error from attempts').fetchone()[0]=='old failure'
    c.close()
