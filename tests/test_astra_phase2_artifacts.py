"""Bounded F17 identity and F10 public allocation regressions."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import pytest
from du_pipeline.db import Database, SchemaVersionError, SCHEMA_VERSION
from du_pipeline.service import Pipeline


@pytest.fixture
def setup(tmp_path):
    with Database(tmp_path/'p.db') as db:
        p=Pipeline(db); pid=p.init_project('artifact tests',scene_range=(1,10))
        source=p._root(pid)/'input'; source.write_bytes(b'artifact')
        yield db,p,pid,source


def insert(c,pid,scene=None,digest='a'*64,version=1,kind='CUSTOM'):
    return c.execute("insert into artifacts(project_id,scene_id,kind,uri,sha256,version,status,created_at,expires_at) values(?,?,?,'dummy',?,?,'ACTIVE','old','later')",(pid,scene,kind,digest,version))


def test_null_scene_identity_unique(setup):
    db,p,pid,source=setup
    with db.transaction():
        insert(db.conn,pid)
        with pytest.raises(sqlite3.IntegrityError): insert(db.conn,pid)


@pytest.mark.parametrize('digest',['not-a-hash','g'*64,'a'*63,'a'*65,'a'*63+'\x00',b'a'*64,None])
@pytest.mark.parametrize('operation',['insert','update'])
def test_artifact_checksum_db_enforced(setup,digest,operation):
    db,p,pid,source=setup
    with db.transaction():
        if operation=='update': insert(db.conn,pid)
        with pytest.raises(sqlite3.IntegrityError):
            if operation=='insert': insert(db.conn,pid,digest=digest)
            else: db.conn.execute('update artifacts set sha256=?',(digest,))


@pytest.mark.parametrize('scene_scope',[False,True])
def test_public_allocation_competing_connection_before_begin(setup,monkeypatch,scene_scope):
    db,p,pid,source=setup
    sid=None
    if scene_scope:
        p.import_audio(pid,str(source),6000,'a'*64)
        p.import_srt(pid,[(0,6000,'text')]); sid=p.plan_scenes(pid)[0]['id']
    with Database(db.path) as other:
        q=Pipeline(other); transaction=db.transaction; fired=False
        @contextmanager
        def interleave(*args,**kwargs):
            nonlocal fired
            if not fired:
                fired=True; q.add_artifact(pid,'CUSTOM',source,sid)
            with transaction(*args,**kwargs): yield db
        monkeypatch.setattr(db,'transaction',interleave)
        p.add_artifact(pid,'CUSTOM',source,sid)
        rows=db.all('select version,uri from artifacts order by version')
        assert [r['version'] for r in rows]==[1,2]
        assert len({r['uri'] for r in rows})==2
        assert all(Path(r['uri']).read_bytes()==b'artifact' for r in rows)


def historical(tmp_path,label,digest='A'*64,duplicate=False):
    path=tmp_path/'legacy.db'; c=sqlite3.connect(path)
    c.executescript((Path(__file__).parent/'fixtures'/f'artifact_{label}.sql').read_text())
    c.execute("insert into projects(id,name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,created_at) values('p','old','vi','ACTIVE','{}','[]','{}','codex-gpt-image-2','ask',0,'hard_cut','old')")
    insert(c,'p',digest=digest)
    if duplicate: insert(c,'p',digest=digest)
    c.commit(); c.close(); return path


def snapshot(path):
    with sqlite3.connect(path) as c:
        return c.execute('pragma user_version').fetchone()[0],list(c.iterdump())


@pytest.mark.parametrize('label',['v11_3bfe33d','v13_inherited'])
@pytest.mark.parametrize('invalid',['checksum','duplicate'])
def test_historical_preflight_fails_closed_unchanged(tmp_path,label,invalid):
    path=historical(tmp_path,label,digest='bad' if invalid=='checksum' else 'a'*64,duplicate=invalid=='duplicate')
    before=snapshot(path)
    for _ in range(2):
        with pytest.raises(SchemaVersionError,match='artifact.*preflight.*(id|identity).*manual'):
            Database(path)
        assert snapshot(path)==before


@pytest.mark.parametrize('label',['v11_3bfe33d','v13_inherited'])
def test_actual_history_preserved_and_reopen(tmp_path,label):
    path=historical(tmp_path,label)
    with sqlite3.connect(path) as c:
        table=c.execute("select sql from sqlite_master where name='artifacts'").fetchone()[0]
        row=c.execute('select * from artifacts').fetchone()
    for _ in range(2):
        with Database(path) as db:
            assert tuple(db.one('select * from artifacts'))==(*row,None)
            # Additive provenance column only; original constraints remain intact.
            current=db.one("select sql from sqlite_master where name='artifacts'")[0]
            assert current.replace(', detached_scene_id INTEGER','')==table
            assert db.one('pragma user_version')[0]==SCHEMA_VERSION
            assert not db.all('pragma foreign_key_check')
            with db.transaction():
                with pytest.raises(sqlite3.IntegrityError): insert(db.conn,'p')
                with pytest.raises(sqlite3.IntegrityError): db.conn.execute('update projects set min_scenes=0')


@pytest.mark.parametrize('label',['v11_3bfe33d','v13_inherited'])
def test_failed_migration_ddl_rollback_and_reopen(tmp_path,label,monkeypatch):
    path=historical(tmp_path,label); before=snapshot(path); original=sqlite3.connect
    class Deny(sqlite3.Connection):
        def execute(self,sql,*args,**kw):
            if sql.startswith(f'PRAGMA user_version={SCHEMA_VERSION}'):
                raise sqlite3.OperationalError('injected migration failure')
            return super().execute(sql,*args,**kw)
    with monkeypatch.context() as m:
        m.setattr(sqlite3,'connect',lambda *a,**kw:original(*a,factory=Deny,**kw))
        with pytest.raises(sqlite3.OperationalError,match='injected'): Database(path)
    assert snapshot(path)==before
    with Database(path) as db:
        assert db.one('pragma user_version')[0]==SCHEMA_VERSION


@pytest.mark.parametrize('field,value',[('version',0),('version',1.5),('kind',''),('kind','   ')])
def test_identity_fields_reject_invalid(setup,field,value):
    db,p,pid,source=setup
    with db.transaction():
        insert(db.conn,pid)
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(f'update artifacts set {field}=?',(value,))


def test_historical_cross_project_owner_fails_closed(tmp_path):
    path=historical(tmp_path,'v11_3bfe33d')
    with sqlite3.connect(path) as c:
        c.execute('drop trigger artifact_scene_project_update')
        c.execute("insert into projects select 'q',name,language,state,style_json,references_json,bible_json,image_provider,whiteboard_mode,seed,transition,blocked_reason,created_at,min_scenes,max_scenes,retention_days,output_json,version,artifact_root from projects where id='p'")
        c.execute("insert into scenes(id,project_id,code,ord,start_ms,end_ms,text,state,approval_state) values(1,'q','S001',1,0,6000,'text','PLANNED','REQUIRED')")
        c.execute('update artifacts set scene_id=1')
    before=snapshot(path)
    with pytest.raises(SchemaVersionError,match='artifact.*preflight.*manual'): Database(path)
    assert snapshot(path)==before


def test_replan_detachment_preserves_original_identity(setup):
    db,p,pid,source=setup
    p.import_audio(pid,str(source),12000,'a'*64)
    p.import_srt(pid,[(0,6000,'one'),(6000,12000,'two')])
    scenes=p.plan_scenes(pid)
    ids=[p.add_artifact(pid,'IMAGE',source,s['id']) for s in scenes]
    p.import_document(pid,'text','new script')
    rows=db.all('select * from artifacts order by id')
    assert [r['id'] for r in rows]==ids
    assert [r['version'] for r in rows]==[1,1]
    assert all(r['scene_id'] is None for r in rows)
    assert [r['detached_scene_id'] for r in rows]==[s['id'] for s in scenes]
    project_id=p.add_artifact(pid,'IMAGE',source)
    assert db.one('select version from artifacts where id=?',(project_id,))[0]==1
    with Database(db.path) as reopened:
        assert len(reopened.all('select * from artifacts'))==3


def test_allocation_holds_write_lock_at_max_read(setup,monkeypatch):
    db,p,pid,source=setup
    with Database(db.path) as other:
        other.conn.execute('pragma busy_timeout=0'); original=db.one; checked=[]
        def read(sql,args=()):
            result=original(sql,args)
            if 'max(version)' in sql:
                with pytest.raises(sqlite3.OperationalError,match='locked'):
                    Pipeline(other).add_artifact(pid,'CUSTOM',source)
                checked.append(True)
            return result
        monkeypatch.setattr(db,'one',read)
        p.add_artifact(pid,'CUSTOM',source)
        assert checked==[True]
        Pipeline(other).add_artifact(pid,'CUSTOM',source)
        assert [r[0] for r in db.all('select version from artifacts order by version')]==[1,2]


def test_registration_event_failure_rolls_back_copy_and_allocation(setup,monkeypatch,tmp_path):
    db,p,pid,source=setup
    external=tmp_path/'external'; external.write_bytes(b'outside')
    before=set(p._root(pid).iterdir())
    original=p._event
    def fail(pid,event,*args,**kw):
        if event=='ARTIFACT_ADDED': raise RuntimeError('event failure')
        return original(pid,event,*args,**kw)
    with monkeypatch.context() as m:
        m.setattr(p,'_event',fail)
        with pytest.raises(RuntimeError,match='event failure'): p.add_artifact(pid,'CUSTOM',external)
        with db.transaction():
            with pytest.raises(PermissionError,match='top-level'): p.add_artifact(pid,'CUSTOM',external)
            assert not db.all('select * from artifacts')
    assert set(p._root(pid).iterdir())==before
    assert external.read_bytes()==b'outside'
    aid=p.add_artifact(pid,'CUSTOM',external)
    assert db.one('select version from artifacts where id=?',(aid,))[0]==1


def test_raw_connection_cannot_forge_detached_namespace(setup):
    db,p,pid,source=setup
    aid=p.add_artifact(pid,'CUSTOM',source)
    with sqlite3.connect(db.path) as c:
        with pytest.raises(sqlite3.IntegrityError,match='provenance'):
            c.execute('update artifacts set detached_scene_id=99 where id=?',(aid,))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("update artifacts set sha256=? where id=?",('g'*64,aid))


def test_multiple_active_versions_and_nullable_parent_remain_supported(setup):
    db,p,pid,source=setup
    first=p.add_artifact(pid,'IMAGE',source)
    second=p.add_artifact(pid,'IMAGE',source,parent_id=first)
    assert [r['status'] for r in db.all('select status from artifacts')]==['ACTIVE','ACTIVE']
    assert db.one('select parent_id from artifacts where id=?',(second,))[0]==first
