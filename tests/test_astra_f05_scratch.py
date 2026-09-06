"""Explicit sealed attempt scratch, only temporary dummy bytes."""
import json
import os
from pathlib import Path
import pytest
from test_astra_f07 import setup,rerun
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def allocate(p,sid):
    assert callable(getattr(p,'allocate_attempt_scratch',None)), 'explicit scratch allocation API missing'
    return p.allocate_attempt_scratch(sid,b'failed dummy bytes')


def test_owned_failed_scratch_retired_and_journal_replay(setup):
    p,pid,root,sid=setup; r=allocate(p,sid); path=Path(r['path'])
    assert path.read_bytes()==b'failed dummy bytes'
    p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    assert not path.exists()
    attempt=p.db.one('select * from attempts')
    assert attempt['failed_path']==str(path) and attempt['failed_size']==18
    events=[e['type'] for e in p.db.all('select type from events')]
    assert {'SCRATCH_ALLOCATED','SCRATCH_CONSUMED','SCRATCH_RETIRE_PREPARED','SCRATCH_RETIRED'}<=set(events)
    with Database(p.db.path) as db:
        Pipeline(db).reconcile_deletions(); Pipeline(db).reconcile_deletions()
        assert db.one("select count(*) n from events where type='SCRATCH_RETIRED'")['n']==1


@pytest.mark.parametrize('protection',['artifact','narration','hardlink','unknown','symlink','shared','publication'])
def test_owned_scratch_protection_retains_bytes(setup,protection):
    p,pid,root,sid=setup; r=allocate(p,sid); path=Path(r['path'])
    outside=root/'protected'; outside.write_bytes(b'outside protected')
    if protection=='artifact': p.add_artifact(pid,'IMAGE',path,sid)
    elif protection=='narration': p.import_audio(pid,str(path),6000,'a'*64)
    elif protection=='hardlink': os.link(path,root/'alias')
    elif protection=='unknown': path.write_bytes(b'unknown bytes')
    elif protection=='symlink': path.unlink(); path.symlink_to(outside)
    elif protection=='shared':
        other=p.init_project('other',scene_range=(1,1))
        p.db.execute("insert into audio values(?,?,?,?)",(other,str(path),6000,'a'*64))
    elif protection=='publication':
        p.db.execute("insert into remote_uploads(project_id,local_path,state) values(?,?,'PENDING')",(pid,str(path)))
    # Mutations can fence allocations: rejecting consumption is safe too.
    try: p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    except (PermissionError,ValueError): pass
    p.reconcile_deletions()
    assert path.exists() or path.is_symlink()
    assert outside.read_bytes()==b'outside protected'


@pytest.mark.parametrize('point',['after_attempt_commit','after_retire_prepared','after_scratch_unlink'])
def test_crash_reopen_replay_retires_only_consumed_receipt(setup,point,monkeypatch):
    p,pid,root,sid=setup; r=allocate(p,sid)
    def crash(where):
        if where==point: raise RuntimeError('crash '+point)
    monkeypatch.setattr(p,'_scratch_crash',crash)
    with pytest.raises(RuntimeError,match='crash'): p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    with Database(p.db.path) as db:
        q=Pipeline(db); q.reconcile_deletions(); q.reconcile_deletions()
        assert not Path(r['path']).exists()
        assert db.one('select count(*) n from attempts')['n']==1
        assert db.one("select count(*) n from events where type='SCRATCH_RETIRED'")['n']==1


def test_unconsumed_or_forged_wrong_epoch_receipts_never_delete(setup):
    p,pid,root,sid=setup; r=allocate(p,sid)
    with pytest.raises(PermissionError): p.record_image_attempt(sid,False,scratch_receipt='forged')
    p.reconcile_deletions(); assert Path(r['path']).exists()
    rerun(p,pid,'image')
    with pytest.raises(PermissionError): p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    assert not p.db.all('select * from attempts')
    assert Path(r['path']).exists()


@pytest.mark.parametrize('kind',['artifact','audio'])
def test_alias_reference_added_after_consumption_protects_scratch(setup,monkeypatch,kind):
    p,pid,root,sid=setup; r=allocate(p,sid); path=Path(r['path'])
    def crash(point):
        if point=='after_attempt_commit': raise RuntimeError('crash')
    monkeypatch.setattr(p,'_scratch_crash',crash)
    with pytest.raises(RuntimeError): p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    alias=path.parent/'alias'; alias.symlink_to(path)
    if kind=='audio': p.db.execute('update audio set uri=? where project_id=?',(str(alias),pid))
    else:
        aid=p.add_artifact(pid,'INPUT',root/'audio')
        p.db.execute('update artifacts set uri=? where id=?',(str(alias),aid))
    monkeypatch.setattr(p,'_scratch_crash',lambda point:None)
    p.reconcile_deletions()
    assert path.exists(), 'referenced inode via alias must not be retired'


@pytest.mark.parametrize('point',['consume','seal'])
def test_journal_failure_retains_unowned_bytes_and_rolls_back(setup,monkeypatch,point):
    p,pid,root,sid=setup
    original=p._event
    def fail(pid,kind,data=None):
        if kind==('SCRATCH_CONSUMED' if point=='consume' else 'SCRATCH_ALLOCATED'): raise RuntimeError('journal failure')
        return original(pid,kind,data)
    if point=='consume': r=allocate(p,sid)
    monkeypatch.setattr(p,'_event',fail)
    with pytest.raises(RuntimeError,match='journal failure'):
        if point=='consume': p.record_image_attempt(sid,False,scratch_receipt=r['token'])
        else: allocate(p,sid)
    monkeypatch.setattr(p,'_event',original)
    p.reconcile_deletions()
    intent=json.loads(p.db.one("select data_json from events where type='SCRATCH_ALLOCATION_PREPARED'")['data_json'])
    assert Path(intent['path']).read_bytes()==b'failed dummy bytes'
    assert not p.db.all('select * from attempts')


@pytest.mark.parametrize('change',['parent','leaf','hardlink','lease'])
def test_late_retirement_change_is_fail_closed(setup,monkeypatch,change):
    from du_pipeline.db import LeaseUnavailable
    p,pid,root,sid=setup; r=allocate(p,sid); path=Path(r['path'])
    outside=root/'outside'; outside.mkdir(); victim=outside/'payload'; victim.write_bytes(b'unknown')
    def mutate(point):
        if point!='after_retire_prepared': return
        if change=='parent':
            path.parent.rename(root/'saved'); path.parent.symlink_to(outside,target_is_directory=True)
        elif change=='leaf': path.unlink(); path.write_bytes(b'unknown')
        elif change=='hardlink': os.link(path,root/'hardlink')
        else: p.db.execute("update reconciliation_leases set expires_at='2000'")
    monkeypatch.setattr(p,'_scratch_crash',mutate)
    try: p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    except LeaseUnavailable: assert change=='lease'
    assert victim.read_bytes()==b'unknown'
    if change=='parent': assert not (root/'saved'/'payload').exists()
    else: assert path.exists()
    monkeypatch.setattr(p,'_scratch_crash',lambda point:None)
    p.reconcile_deletions()
    if change=='parent': assert path.read_bytes()==b'unknown'


def test_success_wrong_scene_and_repeated_consumption(setup):
    p,pid,root,sid=setup; r=allocate(p,sid)
    other=p.init_project('other',scene_range=(1,1))
    p.import_audio(other,str(root/'audio'),6000,'a'*64); p.import_srt(other,[(0,6000,'other')]); other_sid=p.plan_scenes(other)[0]['id']
    with pytest.raises(PermissionError): p.record_image_attempt(other_sid,False,scratch_receipt=r['token'])
    p.record_image_attempt(sid,True,scratch_receipt=r['token'])
    p.reconcile_deletions(); assert Path(r['path']).exists()
    with pytest.raises(PermissionError): p.record_image_attempt(sid,False,scratch_receipt=r['token'])
    assert len(p.db.all('select * from attempts'))==1


def test_diagnostic_symlink_not_read(setup):
    p,pid,root,sid=setup
    target=root/'private'; target.write_bytes(b'not diagnostic')
    alias=root/'alias'; alias.symlink_to(target)
    p.record_image_attempt(sid,False,failed_binary=alias)
    assert p.db.one('select failed_sha256 from attempts')['failed_sha256'] is None
    assert target.exists()
