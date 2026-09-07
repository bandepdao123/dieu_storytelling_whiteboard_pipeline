"""R1/R3 public two-connection and actual CLI regressions; temporary data only."""
from contextlib import contextmanager
from pathlib import Path
import json
import os
import sqlite3
import subprocess
import sys
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.contracts import QAEvidence

@pytest.fixture
def fixture(tmp_path):
    with Database(tmp_path/'test.db') as db:
        p=Pipeline(db); pid=p.init_project('release',scene_range=(1,10))
        p.import_audio(pid,'metadata',6000,'a'*64)
        p.import_srt(pid,[(0,6000,'old')]); sid=p.plan_scenes(pid)[0]['id']
        yield db,p,pid,sid

@pytest.mark.parametrize('route',['external','managed'])
@pytest.mark.parametrize('change',['pause','cancel','same','different','srt','evidence'])
def test_registration_rejects_intervening_public_change(fixture,tmp_path,monkeypatch,route,change):
    db,p,pid,sid=fixture
    if change=='evidence':
        image=p._root(pid)/'image'; image.write_bytes(b'image')
        p.add_artifact(pid,'IMAGE',image,sid); p.record_image_attempt(sid,True)
        p.record_scene_qa(sid,QAEvidence({'ok':True},1,'first'))
    source=(tmp_path if route=='external' else p._root(pid))/'caller'
    source.write_bytes(b'old-scene-image'); copied=[]; after=[]
    with Database(db.path) as other:
        q=Pipeline(other)
        def mutate():
            if change=='pause': q.pause(pid)
            elif change=='cancel': q.cancel_project(pid)
            elif change=='srt': q.import_srt(pid,[(0,6000,'changed')])
            elif change=='evidence': q.record_scene_qa(sid,QAEvidence({'ok':True},1,'second'))
            else:
                if change=='different': q.import_srt(pid,[(0,6000,'new')])
                assert q.plan_scenes(pid)[0]['id']==sid
            after.extend(other.conn.iterdump())
        if route=='external':
            original=p._managed_copy
            @contextmanager
            def copy(*a,**kw):
                with original(*a,**kw) as value:
                    copied.append(value[0]); mutate(); yield value
            monkeypatch.setattr(p,'_managed_copy',copy)
        else:
            original=db.transaction; fired=False
            @contextmanager
            def transaction(*a,**kw):
                nonlocal fired
                if kw.get('write',True) and not fired:
                    fired=True; mutate()
                with original(*a,**kw) as value: yield value
            monkeypatch.setattr(db,'transaction',transaction)
        with pytest.raises((PermissionError,ValueError)):
            p.add_artifact(pid,'IMAGE',source,sid)
        assert list(db.conn.iterdump())==after
    assert source.read_bytes()==b'old-scene-image'
    assert all(not path.exists() for path in copied)

@pytest.mark.parametrize('identity',['project','scene','foreign_scene','parent'])
def test_registration_missing_or_foreign_identity(fixture,tmp_path,identity):
    db,p,pid,sid=fixture; source=tmp_path/'caller'; source.write_bytes(b'caller')
    if identity=='foreign_scene':
        other=p.init_project('other',scene_range=(1,10))
        p.import_audio(other,'metadata',6000,'b'*64); p.import_srt(other,[(0,6000,'other')])
        sid=p.plan_scenes(other)[0]['id']
    before=list(db.conn.iterdump()); files=set(tmp_path.rglob('*'))
    with pytest.raises(ValueError):
        p.add_artifact('missing' if identity=='project' else pid,'IMAGE',source,
                       99999 if identity=='scene' else sid,parent_id=99999 if identity=='parent' else None)
    assert list(db.conn.iterdump())==before and set(tmp_path.rglob('*'))==files
    assert source.read_bytes()==b'caller'

@pytest.mark.parametrize('route',['external','managed'])
def test_registration_writer_blocks_stop(fixture,tmp_path,monkeypatch,route):
    db,p,pid,sid=fixture
    source=(tmp_path if route=='external' else p._root(pid))/'caller'; source.write_bytes(b'caller')
    original=p._register_artifact; checked=[]
    with Database(db.path) as other:
        other.conn.execute('pragma busy_timeout=0')
        def register(*a,**kw):
            with pytest.raises(sqlite3.OperationalError,match='locked'): Pipeline(other).pause(pid)
            checked.append(True); return original(*a,**kw)
        monkeypatch.setattr(p,'_register_artifact',register)
        aid=p.add_artifact(pid,'IMAGE',source,sid)
        Pipeline(other).pause(pid)
    assert checked and db.one('select scene_id from artifacts where id=?',(aid,))[0]==sid

@pytest.mark.parametrize('replacement',['leaf','inplace','none'])
def test_stale_copy_compensation_preserves_unknown(fixture,tmp_path,monkeypatch,replacement):
    db,p,pid,sid=fixture; source=tmp_path/'source'; source.write_bytes(b'caller')
    paths=[]; original=p._managed_copy
    with Database(db.path) as other:
        @contextmanager
        def copy(*a,**kw):
            with original(*a,**kw) as value:
                path=value[0]; paths.append(path)
                if replacement=='leaf': path.unlink(); path.write_bytes(b'unknown')
                elif replacement=='inplace': path.write_bytes(b'unknown')
                Pipeline(other).plan_scenes(pid)
                yield value
        monkeypatch.setattr(p,'_managed_copy',copy)
        with pytest.raises(PermissionError): p.add_artifact(pid,'IMAGE',source,sid)
    assert source.read_bytes()==b'caller'
    assert not db.all('select * from artifacts')
    if replacement=='none': assert not paths[0].exists()
    else: assert paths[0].read_bytes()==b'unknown'

def test_cli_does_not_swallow_programming_typeerror(fixture,monkeypatch):
    from du_pipeline import cli
    db,p,pid,_=fixture
    def broken(*args,**kwargs): raise TypeError('programming fault')
    monkeypatch.setattr(Pipeline,'status',broken)
    with pytest.raises(TypeError,match='programming fault'):
        cli.main(['--db',str(db.path),'--operational-wal','status',pid])

@pytest.mark.parametrize('method,args',[
    ('status',('unknown',)),('report',('unknown',)),('scene',(99999,)),
    ('queue_retry',(99999,)),('record_scene_qa',(99999,QAEvidence({'ok':True},1,'test'))),
])
def test_service_missing_identity_is_explicit(fixture,method,args):
    db,p,_,_=fixture; before=list(db.conn.iterdump())
    with pytest.raises(ValueError,match='(project|scene) not found'):
        getattr(p,method)(*args)
    assert list(db.conn.iterdump())==before

@pytest.mark.parametrize('wal',[False,True])
@pytest.mark.parametrize('command',['status','status-summary','report','retry','scene-qa','image-attempt','final-qa','final-review','plan','approve'])
def test_cli_unknown_ids_structured_and_unchanged(tmp_path,wal,command):
    path=tmp_path/'known.db'
    with Database(path) as db: Pipeline(db).init_project('known')
    if not wal:
        with sqlite3.connect(path) as conn: conn.execute('pragma journal_mode=delete')
    qa=tmp_path/'qa.json'; qa.write_text(json.dumps({'checks':{'ok':True},'score':1,'evaluator':'test'}))
    args={'retry':['99999'],'scene-qa':['99999',str(qa)],'image-attempt':['99999','succeeded'],
          'final-qa':['unknown','99999',str(qa)],'final-review':['unknown','99999','APPROVED','--actor','test'],
          'approve':['unknown','S001']}.get(command,['unknown'])
    before={p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with sqlite3.connect(path) as conn: logical=list(conn.iterdump())
    result=subprocess.run([sys.executable,'-m','du_pipeline.cli','--db',str(path),*(['--operational-wal'] if wal else []),command,*args],capture_output=True,text=True,env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),'PYTHONDONTWRITEBYTECODE':'1'},cwd=tmp_path)
    assert result.returncode==2,result.stderr
    assert not result.stdout and 'Traceback' not in result.stderr
    payload=json.loads(result.stderr)
    assert payload['error']['code'] in ('INVALID_REQUEST','NOT_ALLOWED')
    assert 'unknown' not in result.stderr and str(path) not in result.stderr
    if not wal: assert {p.name:p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}==before
    with sqlite3.connect(path) as conn: assert list(conn.iterdump())==logical
