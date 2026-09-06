import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from du_pipeline.integrations import SheetsAdapter, DiscordBridge
from du_pipeline.adapters import Role


def test_discord_lease_takeover_does_not_repeat_canonical_effect(tmp_path, monkeypatch):
    db=Database(tmp_path/'discord.db'); p=Pipeline(db)
    bridge=DiscordBridge(p,{'owner':Role.OWNER},lease_seconds=300)
    real=p.dispatch_discord
    def expire_after_effect(*args,**kwargs):
        result=real(*args,**kwargs)
        db.execute("update discord_messages set lease_expires_at='2000-01-01' where message_id='m1'")
        return result
    monkeypatch.setattr(p,'dispatch_discord',expire_after_effect)
    # Lose the transport lease after the atomic core effect but before response persistence.
    assert bridge.dispatch('m1','owner','du-tao-du-an once')['type']=='DANG_XU_LY'
    monkeypatch.setattr(p,'dispatch_discord',real)
    result=DiscordBridge(p,{'owner':Role.OWNER}).dispatch('m1','owner','du-tao-du-an once')
    assert result['ok'] and result['idempotency_key']=='discord:m1'
    assert db.one('select count(*) n from projects')['n']==1
    assert db.one("select count(*) n from events where type='PROJECT_CREATED'")['n']==1
    assert db.one('select count(*) n from command_receipts')['n']==1


def test_checkpoint_restore_preserves_stopped_lifecycle_and_reason(tmp_path):
    for state,reason in [('PAUSED',None),('BLOCKED','needs operator'),('COMPLETED','terminal')]:
        db=Database(tmp_path/f'{state}.db'); p=Pipeline(db)
        pid=p.init_project(state,scene_range=(1,1)); p.checkpoint(pid,'content',{'marker':state})
        db.execute('update projects set state=?,blocked_reason=?,language=? where id=?',(state,reason,'en',pid))
        before=db.one('select version from projects where id=?',(pid,))['version']
        p.restore_latest_checkpoint(pid,'content')
        restored=db.one('select state,blocked_reason,language,version from projects where id=?',(pid,))
        assert (restored['state'],restored['blocked_reason'])==(state,reason)
        assert restored['language']=='vi' and restored['version']==before+1


def test_artifact_restore_preserves_paused_lifecycle(tmp_path):
    db,p,pid=pipeline(tmp_path); source=tmp_path/'restore.png'; source.write_bytes(b'x')
    artifact=p.add_artifact(pid,'IMAGE',source); db.execute("update artifacts set status='SUPERSEDED' where id=?",(artifact,))
    p.pause(pid); p.restore_artifact(artifact)
    assert db.one('select state from projects where id=?',(pid,))['state']=='PAUSED'
from du_pipeline.contracts import QAEvidence


def pipeline(tmp_path):
    db=Database(tmp_path/'p.db'); p=Pipeline(db); pid=p.init_project('x',scene_range=(1,10))
    p.import_audio(pid,'a',6000,'a'*64); p.import_srt(pid,[(0,6000,'OLD')]); return db,p,pid


def test_source_change_invalidates_and_same_import_is_idempotent(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    p.decide_scene(pid,scene['code'],'APPROVED','r')
    version=db.one('select version from projects where id=?',(pid,))['version']
    p.import_srt(pid,[(0,6000,'OLD')])
    assert db.one('select version from projects where id=?',(pid,))['version']==version
    p.import_srt(pid,[(0,6000,'NEW')])
    assert not p._pilot_ready(pid)
    approval=db.one('select * from approvals where project_id=?',(pid,))
    assert approval['revoked_at'] and approval['revoked_reason']=='srt changed'


def test_audio_change_deletes_derived_scenes_and_cannot_authorize_batch(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    db.execute("update scenes set state='IMAGE_READY',qa_state='PASS',approval_state='APPROVED' where id=?",(scene['id'],))
    db.execute("insert into approvals(project_id,scene_id,gate,decision,actor,created_at,evidence_sha256) values(?,?,?,?,?,?,?)",(pid,scene['id'],'SCENE','APPROVED','r','now',p._scene_evidence_hash(scene['id'])))
    assert p._pilot_ready(pid)
    p.import_audio(pid,'new',6000,'b'*64)
    assert db.one('select 1 from scenes where project_id=?',(pid,)) is None
    with pytest.raises(PermissionError): p.start_batch(pid)


def test_revoked_scene_approval_cannot_open_batch(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    db.execute("update scenes set state='IMAGE_READY',qa_state='PASS' where id=?",(scene['id'],))
    p.decide_scene(pid,scene['code'],'APPROVED','r')
    assert p._pilot_ready(pid)
    db.execute("update approvals set revoked_at='now' where scene_id=?",(scene['id'],))
    with pytest.raises(PermissionError): p.start_batch(pid)


def test_superseded_expired_artifact_is_cleaned(tmp_path):
    db,p,pid=pipeline(tmp_path); src=tmp_path/'old.png'; src.write_bytes(b'x')
    aid=p.add_artifact(pid,'IMAGE',src)
    uri=db.one('select uri from artifacts where id=?',(aid,))['uri']
    db.execute("update artifacts set status='SUPERSEDED',expires_at='2000-01-01' where id=?",(aid,))
    assert p.cleanup()==1
    assert db.one('select status from artifacts where id=?',(aid,))['status']=='DELETED'
    assert not __import__('pathlib').Path(uri).exists()


def test_pause_and_cancel_fence_jobs(tmp_path):
    db,p,pid=pipeline(tmp_path); jid=p._create_job(pid,'X'); p.pause(pid)
    assert db.one('select state from jobs where id=?',(jid,))['state']=='PAUSED'
    p.resume(pid); p.cancel_project(pid)
    assert db.one('select state from jobs where id=?',(jid,))['state']=='BLOCKED'


def test_document_change_is_idempotent_then_invalidates(tmp_path):
    db,p,pid=pipeline(tmp_path); p.import_document(pid,'text','one')
    version=db.one('select version from projects where id=?',(pid,))['version']
    p.import_document(pid,'text','one')
    assert db.one('select version from projects where id=?',(pid,))['version']==version
    p.plan_scenes(pid); p.import_document(pid,'text','two')
    assert db.one('select version from projects where id=?',(pid,))['version']==version+1
    assert db.one('select 1 from scenes where project_id=?',(pid,)) is None


def test_manifest_covers_authorization_relevant_scene_fields(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    fields={'ord':2,'start_ms':1,'end_ms':5999,'text':'changed','special':1,
            'state':'IMAGE_READY','approval_state':'APPROVED','qa_state':'PASS',
            'qa_json':'{"x":1}','duration_exception':'x',
            'continuity_json':'{"x":1}','checkpoint_json':'{"x":1}'}
    for field,value in fields.items():
        before=p._manifest_hash(pid); db.execute(f'update scenes set {field}=? where id=?',(value,scene['id']))
        assert p._manifest_hash(pid)!=before, field


def test_v5_duplicate_active_jobs_are_audited_and_reconciled(tmp_path):
    db,p,pid=pipeline(tmp_path); db.close(); source=tmp_path/'p.db'; path=tmp_path/'legacy.db'; source.rename(path)
    conn=sqlite3.connect(path); conn.execute('drop index idx_jobs_one_active'); conn.execute('pragma user_version=5')
    conn.executemany("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) values(?,?,?,?,?,?)",[(pid,'X','QUEUED','x','x',1),(pid,'X','RUNNING','x','x',1)])
    conn.commit(); conn.close(); migrated=Database(path)
    assert [r['state'] for r in migrated.all("select state from jobs where kind='X' order by id")]==['BLOCKED','RUNNING']
    event=migrated.one("select data_json from events where type='MIGRATION_ACTIVE_JOBS_RECONCILED'")
    assert __import__('json').loads(event['data_json'])['retained_state']=='RUNNING'


def test_replan_versions_and_fences_jobs(tmp_path):
    db,p,pid=pipeline(tmp_path); p.plan_scenes(pid); jid=p._create_job(pid,'BATCH_IMAGE')
    version=db.one('select version from projects where id=?',(pid,))['version']
    p.plan_scenes(pid)
    assert db.one('select version from projects where id=?',(pid,))['version']==version+1
    assert db.one('select state from jobs where id=?',(jid,))['state']=='BLOCKED'


def test_srt_rejects_inactive_project_even_for_idempotent_import(tmp_path):
    db,p,pid=pipeline(tmp_path); db.execute("update projects set state='PAUSED' where id=?",(pid,))
    with pytest.raises(PermissionError):p.import_srt(pid,[(0,6000,'OLD')])


def test_constructor_failure_closes_connection(tmp_path,monkeypatch):
    real=sqlite3.connect; opened=[]
    class Tracking:
        def __init__(self,conn): self.conn=conn; self.closed=False
        def __getattr__(self,name): return getattr(self.conn,name)
        def close(self): self.closed=True; return self.conn.close()
    def connect(*args,**kwargs):
        tracked=Tracking(real(*args,**kwargs)); opened.append(tracked); return tracked
    path=tmp_path/'future.db'; conn=real(path); conn.execute('pragma user_version=999'); conn.close()
    monkeypatch.setattr(sqlite3,'connect',connect)
    with pytest.raises(Exception): Database(path)
    assert opened[0].closed


def test_replan_preserves_artifact_lineage(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    src=tmp_path/'x.png'; src.write_bytes(b'x'); aid=p.add_artifact(pid,'IMAGE',src,scene['id'])
    managed=db.one('select uri from artifacts where id=?',(aid,))['uri']; p.plan_scenes(pid)
    artifact=db.one('select * from artifacts where id=?',(aid,))
    assert artifact['status']=='SUPERSEDED' and artifact['scene_id'] is None
    assert __import__('pathlib').Path(managed).exists()


def test_active_job_is_idempotent(tmp_path):
    db,p,pid=pipeline(tmp_path)
    first=p._create_job(pid,'BATCH_IMAGE'); assert p._create_job(pid,'BATCH_IMAGE')==first
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("insert into jobs(project_id,kind,state,created_at,updated_at,project_version) select id,'BATCH_IMAGE','QUEUED','x','x',version from projects where id=?",(pid,))


def test_database_context_manager_closes(tmp_path):
    with Database(tmp_path/'x.db') as db: assert db.one('select 1')[0]==1
    with pytest.raises(Exception): db.one('select 1')


class Client:
    def find_spreadsheet(self,pid): return 's'
    def get_tabs(self,sid): return ['PROJECT','CHARACTERS','ENVIRONMENTS','PROPS','SCENES','ASSETS','GENERATIONS','QA','FEEDBACK','COST_TIME','LEARNINGS']
    def read_rows(self,*args): return [{'command_id':'c','command':'APPROVE','scene_code':'S001'}]


def test_expired_sheet_lease_is_reclaimed(tmp_path):
    db,p,pid=pipeline(tmp_path); p.plan_scenes(pid)
    old=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
    db.execute("insert into integration_commands values('SHEET','c',?,?,'PROCESSING','old',?,NULL)",(pid,old,old))
    result=SheetsAdapter(Client(),db).ingest_commands(pid)
    assert result['processed']==1
    assert db.one("select state from integration_commands where external_id='c'")['state']=='COMPLETED'


@pytest.mark.parametrize('mutation',[
    lambda p,pid:p.select_provider(pid,'other-provider'),
    lambda p,pid:p.select_preset(pid,'presentation'),
    lambda p,pid:p.configure_project(pid,'transition','crossfade'),
])
def test_generation_config_mutations_version_fence_and_enter_evidence(tmp_path,mutation):
    db,p,pid=pipeline(tmp_path); p.plan_scenes(pid); jid=p._create_job(pid,'BATCH_IMAGE')
    version=db.one('select version from projects where id=?',(pid,))['version']; evidence=p._manifest_hash(pid)
    mutation(p,pid)
    assert db.one('select version from projects where id=?',(pid,))['version']==version+1
    assert db.one('select state from jobs where id=?',(jid,))['state']=='BLOCKED'
    assert p._manifest_hash(pid)!=evidence


def test_artifact_parent_lineage_changes_manifest(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    src=tmp_path/'x.png'; src.write_bytes(b'x')
    first=p.add_artifact(pid,'SOURCE',src,scene['id']); child=p.add_artifact(pid,'IMAGE',src,scene['id'],first)
    before=p._manifest_hash(pid); db.execute('update artifacts set parent_id=NULL where id=?',(child,))
    assert p._manifest_hash(pid)!=before


def test_restore_artifact_fences_old_jobs_and_downstream_state(tmp_path):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    src=tmp_path/'x.png'; src.write_bytes(b'x'); old=p.add_artifact(pid,'IMAGE',src,scene['id'])
    src.write_bytes(b'y'); new=p.add_artifact(pid,'IMAGE',src,scene['id'],old)
    db.execute("update artifacts set status='SUPERSEDED' where id=?",(old,)); jid=p._create_job(pid,'BATCH_IMAGE')
    version=db.one('select version from projects where id=?',(pid,))['version']; p.restore_artifact(old)
    assert db.one('select version from projects where id=?',(pid,))['version']==version+1
    assert db.one('select state from jobs where id=?',(jid,))['state']=='BLOCKED'
    assert db.one('select status from artifacts where id=?',(old,))['status']=='ACTIVE'
    assert db.one('select status from artifacts where id=?',(new,))['status']=='SUPERSEDED'


@pytest.mark.parametrize('mutation', ['qa', 'decision', 'replacement'])
def test_evidence_mutations_atomically_fence_active_work(tmp_path, mutation):
    db,p,pid=pipeline(tmp_path); scene=p.plan_scenes(pid)[0]
    db.execute("update scenes set state='IMAGE_READY' where id=?",(scene['id'],))
    old_approval=db.execute("insert into approvals(project_id,gate,decision,actor,created_at) values(?,?,?,?,?)",
                            (pid,'POST_BATCH','APPROVED','old','now')).lastrowid
    jid=p._create_job(pid,'ANIMATION')
    db.execute("update jobs set state='RUNNING',lease_owner='worker',lease_expires_at='2999-01-01' where id=?",(jid,))
    version=db.one('select version from projects where id=?',(pid,))['version']
    if mutation == 'qa':
        p.record_scene_qa(scene['id'],QAEvidence({'image':True},1.0,'reviewer'))
    elif mutation == 'decision':
        p.decide_scene(pid,scene['code'],'APPROVED','reviewer')
    else:
        image=tmp_path/'replacement.png'; image.write_bytes(b'replacement')
        p.replace_scene_artifact(scene['id'],image)
    job=db.one('select * from jobs where id=?',(jid,))
    assert (job['state'],job['lease_owner'],job['lease_expires_at']) == ('BLOCKED',None,None)
    assert db.one('select revoked_at from approvals where id=?',(old_approval,))['revoked_at']
    assert db.one('select version from projects where id=?',(pid,))['version'] == version+1


def test_checkpoint_after_replan_never_binds_stale_blocked_job(tmp_path):
    db,p,pid=pipeline(tmp_path); p.plan_scenes(pid)
    stale=p._create_job(pid,'BATCH_IMAGE')
    p.plan_scenes(pid)
    assert db.one('select state from jobs where id=?',(stale,))['state']=='BLOCKED'
    p.checkpoint(pid,'after-replan',{'ok':True})
    bound=db.one("select jobs.* from stages join jobs on jobs.id=stages.job_id where stages.name='after-replan'")
    current=db.one('select version from projects where id=?',(pid,))['version']
    assert bound['id'] != stale
    assert bound['project_version'] == current
    assert bound['state'] in ('QUEUED','RUNNING','PAUSED')
