import sqlite3
import pytest
from du_pipeline.db import Database, SchemaVersionError, SCHEMA_VERSION
from du_pipeline.service import Pipeline
from test_cli_media_workflow import cli


def test_operational_live_wal_sees_commits_and_rejects_writes(tmp_path):
    path=tmp_path/'live.db'
    with Database(path) as writer:
        p=Pipeline(writer); pid=p.init_project('before')
        with Database.open_existing(path,readonly=True,operational_wal=True) as reader:
            assert reader.one('pragma journal_mode')[0]=='wal'
            assert reader.one('pragma query_only')[0]==1
            with pytest.raises(sqlite3.DatabaseError): reader.conn.execute("update projects set name='bad'")
            reader.conn.rollback()  # failed raw DML opened an implicit SQLite scope
            with pytest.raises(PermissionError):
                with reader.transaction(): pass
            p.pause(pid)
            assert Pipeline(reader).status(pid)['project']['state']=='PAUSED'
        assert cli(path,'--operational-wal','status',pid)['project']['state']=='PAUSED'
        assert cli(path,'status',pid,ok=False)['error']['code']=='SCHEMA_UNSUPPORTED'
        assert writer.one('pragma journal_mode')[0]=='wal'


@pytest.mark.parametrize('version',[0,13,SCHEMA_VERSION+1])
def test_operational_old_schema_no_migration(tmp_path,version):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as db:
        db.execute('create table sentinel(value)'); db.execute(f'pragma user_version={version}')
    before=path.read_bytes()
    assert cli(path,'--operational-wal','status','missing',ok=False)['error']['code']=='SCHEMA_UNSUPPORTED'
    assert path.read_bytes()==before


def test_operational_missing_no_creation(tmp_path):
    path=tmp_path/'missing.db'
    cli(path,'--operational-wal','status','missing',ok=False)
    assert not path.exists()


def test_valid_qa_unknown_scene_structured_error(tmp_path):
    path=tmp_path/'db'; cli(path,'init','test')
    qa=tmp_path/'qa.json'; qa.write_text('{"checks":{"visual":true},"score":1,"evaluator":"human"}')
    assert cli(path,'scene-qa',999,qa,ok=False)['error']['code']=='INVALID_REQUEST'


def test_operator_cannot_submit_qa(tmp_path):
    path=tmp_path/'db'; cli(path,'init','test')
    qa=tmp_path/'qa.json'; qa.write_text('{"checks":{"visual":true},"score":1,"evaluator":"human"}')
    assert cli(path,'--role','OPERATOR','scene-qa',999,qa,ok=False)['error']['code']=='NOT_ALLOWED'
