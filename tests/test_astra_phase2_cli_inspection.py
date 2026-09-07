import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
import pytest
from du_pipeline.db import Database, SchemaVersionError
from du_pipeline.service import Pipeline


def run(*args):
    return subprocess.run([sys.executable, '-m', 'du_pipeline.cli', *map(str,args)], capture_output=True, text=True)


@pytest.mark.parametrize('command', ['status','status-summary','report','discord-status'])
def test_missing_inspection_never_creates(tmp_path, command):
    path=tmp_path/'missing.db'
    result=run('--db',path,command,'missing')
    assert not path.exists()
    assert result.returncode == 2
    assert json.loads(result.stderr)['error']['code']=='DATABASE_UNAVAILABLE'
    assert 'Traceback' not in result.stderr


def test_readonly_no_constructor_writes(tmp_path):
    path=tmp_path/'db.sqlite'
    with Database(path) as db:
        pid=Pipeline(db).init_project('test')
    # Offline fixture uses rollback journal to verify pure inspection without WAL sidecars.
    with sqlite3.connect(path) as c: c.execute('PRAGMA journal_mode=DELETE')
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with Database.open_existing(path, readonly=True) as db:
        assert Pipeline(db).status_summary(pid)['project']['id']==pid
        with pytest.raises(sqlite3.DatabaseError): db.conn.execute("DELETE FROM projects")
        with pytest.raises(PermissionError):
            with db.transaction(): pass
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_old_schema_inspect_preserves_bytes(tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as c: c.execute('PRAGMA user_version=11')
    before=path.read_bytes()
    with pytest.raises(SchemaVersionError): Database.open_existing(path, readonly=True)
    assert path.read_bytes()==before


def test_expected_failure_is_sanitized_and_closed(tmp_path, monkeypatch, capsys):
    from du_pipeline.cli import main
    path=tmp_path/'db'
    with Database(path): pass
    seen=[]
    original=Database.close
    def close(self): seen.append(self); original(self)
    monkeypatch.setattr(Database,'close',close)
    def fail(*args): raise ValueError('secret-sensitive-user-input')
    monkeypatch.setattr(Pipeline,'pause',fail)
    assert main(['--db',str(path),'pause','missing'])==2
    output=capsys.readouterr()
    assert 'secret-sensitive' not in output.err
    assert json.loads(output.err)['error']['code']=='INVALID_REQUEST'
    assert seen and all(x.conn is None for x in seen)


def test_bad_assemble_syntax_structured(tmp_path):
    result=run('--db',tmp_path/'no.db','assemble','p','--output','out.mp4')
    assert result.returncode==2
    assert json.loads(result.stderr)['error']['code']=='USAGE'
    assert not (tmp_path/'no.db').exists()
