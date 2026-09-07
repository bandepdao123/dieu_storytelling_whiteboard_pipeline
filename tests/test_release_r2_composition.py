"""Bounded command composition: temporary DB/media only, independent connections."""
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
import pytest
from du_pipeline.db import Database, SCHEMA_VERSION
from du_pipeline.service import Pipeline


def fixture(tmp):
    db=Database(tmp/'test.db'); p=Pipeline(db)
    pid=p.init_project('R2',scene_range=(1,10))
    p.import_audio(pid,'metadata',6000,'a'*64)
    p.import_srt(pid,[(0,6000,'text')]); sid=p.plan_scenes(pid)[0]['id']
    source=tmp/'source.image'; source.write_bytes(b'image')
    return db,p,pid,sid,source,f'du-scene-replace {sid} {source}'


def call(p,cmd):
    return p.dispatch_discord(cmd,'OWNER',idempotency_key='review')


def test_concurrent_replay_and_request_binding(tmp_path,monkeypatch):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db, Database(db.path) as other:
        q=Pipeline(other); original=p._managed_copy
        @contextmanager
        def copy(*args):
            assert not db.conn.in_transaction
            with original(*args) as owned:
                with pytest.raises(PermissionError,match='in progress'): call(q,cmd)
                with pytest.raises(PermissionError,match='different request'): call(q,f'du-project-status {pid}')
                yield owned
        monkeypatch.setattr(p,'_managed_copy',copy)
        result=call(p,cmd)
        source.unlink()  # success replay must not read or copy source again
        assert call(q,cmd)==result
        with pytest.raises(PermissionError,match='different request'): call(q,cmd+'x')
        assert len(db.all('select * from artifacts'))==1


@pytest.mark.parametrize('change',['pause','replan','evidence','lease','attempt','unknown','substitute'])
def test_copy_first_revalidation(tmp_path,monkeypatch,change):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db, Database(db.path) as other:
        q=Pipeline(other); original=p._managed_copy; paths=[]
        @contextmanager
        def copy(*args):
            with original(*args) as owned:
                managed,fd,identity=owned; paths.append(managed)
                if change=='pause': q.pause(pid)
                elif change=='replan': q.import_srt(pid,[(0,6000,'new')]); q.plan_scenes(pid)
                elif change=='evidence': q.add_artifact(pid,'IMAGE',source,scene_id=sid)
                elif change in ('lease','attempt'):
                    with other.transaction():
                        if change=='lease': other.execute("update command_copy_requests set lease_expires_at='2000'")
                        else: other.execute("update command_copy_requests set attempt='new-worker'")
                elif change=='unknown':
                    managed.write_bytes(b'unknown'); q.pause(pid)
                else:
                    managed.unlink(); managed.write_bytes(b'unknown')
                yield owned
        monkeypatch.setattr(p,'_managed_copy',copy)
        with pytest.raises(PermissionError): call(p,cmd)
        assert not db.all('select * from command_receipts')
        assert len(db.all('select * from artifacts'))==(1 if change=='evidence' else 0)
        assert source.read_bytes()==b'image'
        if change in ('unknown','substitute'): assert paths[0].read_bytes()==b'unknown'
        if change=='attempt': assert db.one('select attempt from command_copy_requests')['attempt']=='new-worker'


def test_mutation_result_failure_rolls_back(tmp_path):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db:
        old=p.add_artifact(pid,'IMAGE',source,scene_id=sid)
        before=[dict(r) for r in db.all('select * from artifacts')]
        with db.transaction():
            db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON command_receipts BEGIN SELECT RAISE(ABORT,'result failure'); END")
        with pytest.raises(sqlite3.IntegrityError,match='result failure'): call(p,cmd)
        assert [dict(r) for r in db.all('select * from artifacts')]==before
        assert not db.all('select * from command_receipts')
        assert db.one('select state from command_copy_requests')['state']=='MANUAL_REVIEW'
        assert source.read_bytes()==b'image'


@pytest.mark.parametrize('boundary',['copy','prepared','mutation'])
def test_process_crash_reopen_manual_recovery(tmp_path,boundary):
    db,p,pid,sid,source,cmd=fixture(tmp_path); root=p._root_lookup(pid); db.close()
    code='''
import os,sys
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
with Database(sys.argv[1]) as db:
 p=Pipeline(db)
 if sys.argv[3]=='copy':
  from contextlib import contextmanager
  original=p._managed_copy
  @contextmanager
  def copy(*a):
   with original(*a) as owned:
    os._exit(73)
    yield owned
  p._managed_copy=copy
 elif sys.argv[3]=='prepared':
  original=p._command_copy_fence
  count=[0]
  def fence(*a):
   count[0]+=1
   if count[0]==2: os._exit(73)
   return original(*a)
  p._command_copy_fence=fence
 else:
  original=p._event
  def event(pid,kind,data):
   original(pid,kind,data)
   if kind=='ARTIFACT_ADDED': os._exit(73)
  p._event=event
 p.dispatch_discord(sys.argv[2],'OWNER',idempotency_key='review')
'''
    proc=subprocess.run([sys.executable,'-c',code,str(tmp_path/'test.db'),cmd,boundary],env=os.environ.copy())
    assert proc.returncode==73
    files={str(f):f.read_bytes() for f in root.iterdir() if f.is_file()}
    assert b'image' in files.values()
    with Database(tmp_path/'test.db') as db:
        p=Pipeline(db)
        assert not db.all('select * from artifacts')
        assert not db.all('select * from command_receipts')
        preparation=db.one('select preparation_json from command_copy_requests')['preparation_json']
        assert bool(preparation)==(boundary!='copy')
        with db.transaction(): db.execute("update command_copy_requests set lease_expires_at='2000'")
        with pytest.raises(PermissionError,match='manual review'): call(p,cmd)
        assert db.one('select state from command_copy_requests')['state']=='MANUAL_REVIEW'
    assert {str(f):f.read_bytes() for f in root.iterdir() if f.is_file()}==files
    assert source.read_bytes()==b'image'


def test_nested_command_rejects_before_claim(tmp_path):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db, db.transaction():
        with pytest.raises(PermissionError,match='top-level'): call(p,cmd)
        assert not db.all('select * from command_copy_requests')
        assert source.read_bytes()==b'image'


def test_v14_additive_migration_rollback(tmp_path,monkeypatch):
    # Reconstructed pre-R2 v14 constructor: remove only the additive v15 DDL
    # and version increment. This is NOT an independently frozen historical
    # source fixture and does not certify a historical schema signature matrix.
    import du_pipeline.db as module
    path=tmp_path/'old.db'
    source=Path(module.__file__).read_text()
    source=source.replace('SCHEMA_VERSION = 15','SCHEMA_VERSION = 14')
    source='\n'.join(line for line in source.splitlines() if 'CREATE TABLE IF NOT EXISTS command_copy_requests(' not in line)
    namespace={}; exec(compile(source,'v14-constructor','exec'),namespace)
    with namespace['Database'](path) as old:
        before=list(old.conn.iterdump())
        assert old.conn.execute('pragma user_version').fetchone()[0]==14
    connect=sqlite3.connect
    def denied(*args,**kwargs):
        conn=connect(*args,**kwargs)
        def auth(action,a,b,c,d):
            if action==sqlite3.SQLITE_PRAGMA and a=='user_version' and b==str(SCHEMA_VERSION): return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(auth); return conn
    monkeypatch.setattr(module.sqlite3,'connect',denied)
    with pytest.raises(sqlite3.DatabaseError): Database(path)
    monkeypatch.setattr(module.sqlite3,'connect',connect)
    with connect(path) as raw:
        assert list(raw.iterdump())==before
        assert raw.execute('pragma user_version').fetchone()[0]==14
    for _ in range(2):
        with Database(path) as upgraded:
            assert upgraded.one('pragma user_version')[0]==SCHEMA_VERSION
            assert not upgraded.all('select * from command_copy_requests')
