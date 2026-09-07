"""F12 temporary dummy-byte fault injection; not physical power-loss tests."""
import json
import os
import sqlite3
from pathlib import Path

import pytest
import du_pipeline.service as service_module
from du_pipeline.db import Database, LeaseUnavailable
from du_pipeline.service import Pipeline
from test_final_assembly import fixture, SuccessfulFakeAssembler


@pytest.mark.parametrize('field,value', [
    ('final_path', None), ('final_path', ''), ('final_path', 'bad\x00path'),
    ('staging_path', []), ('project_version', True), ('project_version', 0),
    ('output_size', True), ('output_size', -1), ('output_sha256', 'bad'),
    ('evidence_sha256', None), ('current_evidence_sha256', 'bad'),
    ('token', '../bad'), ('project_id', 'other'), ('manifest_json', '[]'),
    ('artifact_id', 123), ('created_at', None),
    ('identity', None), ('identity', [True]*7), ('identity', [-1]*7),
    ('source_path', ''), ('source_path', 'bad\x00path'),
])
@pytest.mark.parametrize('prepared', [False, True])
def test_malformed_receipt_isolated_before_any_filesystem_mutation(tmp_path, monkeypatch, field, value, prepared):
    db,p,pid,root,*_=fixture(tmp_path)
    point='after_prepared_commit' if prepared else 'after_staging_move'
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at==point else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    receipt=dict(db.one('select * from publication_intents'))
    payload=json.loads(receipt['journal_json']); staged=Path(payload['staging_path'])
    if field=='identity': db.execute('update publication_intents set identity_json=?',(json.dumps(value),))
    elif field=='source_path': db.execute('update publication_intents set source_path=?',(value,))
    else:
        payload[field]=value
        db.execute('update publication_intents set journal_json=?',(json.dumps(payload),))
    # Unrelated project/row must still finish, after the malformed row.
    other=tmp_path/'other'; other.mkdir()
    db2,q,pid2,root2,*_=fixture(other)
    # Recovery of the malformed row must not even open/create/fsync a parent.
    def forbidden(*a,**k): raise AssertionError('filesystem reached before receipt validation')
    monkeypatch.setattr(p,'_publication_parent_fd',forbidden)
    p._publication_crash=lambda at: None
    p.reconcile_publications(); p.reconcile_publications()
    table='publication_journal' if prepared else 'publication_intents'
    assert db.one('select state from '+table)['state']=='MANUAL_REVIEW'
    assert staged.read_bytes()==b'generated' and not (root/'final.mp4').exists()
    assert not db.one('select 1 from final_assemblies')
    q.assemble(pid2,root2/'final.mp4',assembler=SuccessfulFakeAssembler(root2))
    assert (root2/'final.mp4').read_bytes()==b'generated'


def test_bad_intent_does_not_abort_unrelated_valid_row(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at=='after_staging_move' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    good=dict(db.one('select * from publication_intents'))
    bad=dict(good,token='bad',journal_json='null',created_at='0000')
    db.execute('insert into publication_intents values('+','.join('?' for _ in bad)+')',tuple(bad.values()))
    p._publication_crash=lambda at: None
    p.reconcile_publications(); p.reconcile_publications()
    assert db.one("select state from publication_intents where token='bad'")['state']=='MANUAL_REVIEW'
    assert db.one('select state from publication_journal where token=?',(good['token'],))['state']=='COMMITTED'
    assert (root/'final.mp4').read_bytes()==b'generated'


class Crash(BaseException):
    pass


def test_prepared_insert_failure_has_durable_owner_and_replays(tmp_path):
    db, p, pid, root, *_ = fixture(tmp_path)
    db.execute("create trigger reject_prepared before insert on publication_journal begin select raise(abort,'injected PREPARED failure'); end")
    with pytest.raises(sqlite3.IntegrityError, match='injected PREPARED'):
        p.assemble(pid, root/'final.mp4', assembler=SuccessfulFakeAssembler(root))
    staged = list((root/'.publications').glob('*.staged.mp4'))
    assert len(staged) == 1 and staged[0].read_bytes() == b'generated'
    assert not db.one('select 1 from publication_journal')
    assert db.one("select count(*) n from publication_intents where state='INTENT'")['n'] == 1
    db.execute('drop trigger reject_prepared')
    path = db.path
    db.close()
    with Database(path) as reopened:
        recovery = Pipeline(reopened)
        recovery.reconcile_publications(); recovery.reconcile_publications()
        assert (root/'final.mp4').read_bytes() == b'generated'
        assert reopened.one('select count(*) n from final_assemblies')['n'] == 1


@pytest.mark.parametrize('point', [
    'before_intent_commit', 'after_intent_commit', 'before_staging_move',
    'after_staging_move', 'before_staging_fsync', 'after_staging_fsync',
    'before_prepared_insert', 'after_prepared_insert', 'after_prepared_commit',
    'before_promotion', 'after_rename_before_registration',
    'before_final_fsync', 'after_final_fsync', 'before_registration',
    'after_registration_before_cleanup',
])
def test_crash_reopen_replay_boundaries(tmp_path, point):
    db, p, pid, root, *_ = fixture(tmp_path)
    def crash(at):
        if at == point:
            raise Crash(at)
    p._publication_crash = crash
    with pytest.raises(Crash):
        p.assemble(pid, root/'final.mp4', assembler=SuccessfulFakeAssembler(root))
    path = db.path
    db.close()
    with Database(path) as reopened:
        q = Pipeline(reopened)
        q.reconcile_publications(); q.reconcile_publications()
        count = reopened.one('select count(*) n from final_assemblies')['n']
        # Exception unwinding cleans assembly work before a move; no bytes invented.
        if point in ('before_intent_commit', 'after_intent_commit', 'before_staging_move'):
            assert count == 0 and not (root/'final.mp4').exists()
        else:
            assert count == 1 and (root/'final.mp4').read_bytes() == b'generated'
        assert not reopened.one("select 1 from publication_intents where state='INTENT'")


def test_publication_fsync_order(tmp_path, monkeypatch):
    db, p, pid, root, *_ = fixture(tmp_path)
    calls = []
    real_fsync = os.fsync
    def fsync(fd):
        calls.append(('sync', os.readlink(f'/proc/self/fd/{fd}')))
        return real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    p._publication_crash = lambda point: calls.append(('point', point))
    p.assemble(pid, root/'exports'/'nested'/'final.mp4', assembler=SuccessfulFakeAssembler(root))
    def index(point): return calls.index(('point', point))
    assert any(k == 'sync' and v.endswith('/final.mp4') for k,v in calls[:index('before_intent_commit')])
    between = calls[index('after_staging_move'):index('before_prepared_insert')]
    assert any(k == 'sync' and v.endswith('/.publications') for k,v in between)
    assert any(k == 'sync' and '/.assembly-' in v for k,v in between)
    between = calls[index('after_rename_before_registration'):index('before_registration')]
    for suffix in ('/.publications', '/exports/nested', '/exports/nested/final.mp4'):
        assert any(k == 'sync' and v.endswith(suffix) for k,v in between)


def test_unknown_staging_is_never_swept(tmp_path):
    db,p,pid,root,*_ = fixture(tmp_path)
    pub = root/'.publications'; pub.mkdir()
    unknown = pub/'unknown.staged.mp4'; unknown.write_bytes(b'unknown')
    p.reconcile_publications(); p.reconcile_publications()
    assert unknown.read_bytes() == b'unknown'


def test_bad_legacy_parent_isolated_while_valid_row_recovers(tmp_path):
    db,p,pid,root,*_ = fixture(tmp_path)
    p._publication_crash = lambda point: (_ for _ in ()).throw(Crash()) if point == 'after_prepared_commit' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    good = dict(db.one('select * from publication_journal'))
    outside = tmp_path/'outside'; outside.mkdir(); victim=outside/'victim'; victim.write_bytes(b'generated')
    bad = dict(good, token='bad', evidence_sha256='bad', staging_path=str(victim), final_path=str(root/'bad.mp4'), created_at='0000')
    db.execute('insert into publication_journal values('+','.join('?' for _ in bad)+')',tuple(bad.values()))
    p._publication_crash = lambda point: None
    p.reconcile_publications(); p.reconcile_publications()
    assert db.one("select state from publication_journal where token='bad'")['state'] == 'MANUAL_REVIEW'
    assert db.one('select state from publication_journal where token=?',(good['token'],))['state'] == 'COMMITTED'
    assert victim.read_bytes() == b'generated'


@pytest.mark.parametrize('failure', [sqlite3.OperationalError('database I/O error'), LeaseUnavailable('lost lease')])
def test_global_errors_not_isolated(tmp_path, monkeypatch, failure):
    db,p,pid,root,*_ = fixture(tmp_path)
    def fail(*a, **k): raise failure
    monkeypatch.setattr(db, 'renew_reconciliation_lease', fail)
    # Use a valid prepared row so the renew boundary is reached.
    p._publication_crash = lambda point: (_ for _ in ()).throw(Crash()) if point == 'after_prepared_commit' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    with pytest.raises(type(failure), match=str(failure)): p.reconcile_publications()


@pytest.mark.parametrize('phase', ['source_file','staging_file','staging_dir','source_dir_after_move','final_file','final_dir'])
def test_fsync_failure_never_registers_or_loses_ownership(tmp_path, monkeypatch, phase):
    db,p,pid,root,*_ = fixture(tmp_path)
    real = os.fsync; failed = False; moved = False
    def point(at):
        nonlocal moved
        if at == 'after_staging_move': moved = True
    p._publication_crash = point
    def sync(fd):
        nonlocal failed
        path = os.readlink(f'/proc/self/fd/{fd}')
        match = {
            'source_file': '/.assembly-' in path and path.endswith('/final.mp4'),
            'staging_file': path.endswith('.staged.mp4'),
            'staging_dir': moved and path.endswith('/.publications'),
            'source_dir_after_move': moved and Path(path).name.startswith('.assembly-'),
            'final_file': path == str(root/'final.mp4'),
            'final_dir': path == str(root) and (root/'final.mp4').exists(),
        }[phase]
        if match and not failed:
            failed = True
            raise OSError(5, 'injected fsync failure')
        return real(fd)
    monkeypatch.setattr(os,'fsync',sync)
    with pytest.raises(Exception, match='fsync failure'):
        p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert failed and not db.one('select 1 from final_assemblies')
    assert not (root/'final.mp4').exists()
    monkeypatch.setattr(os,'fsync',real)
    path=db.path; db.close()
    with Database(path) as reopened:
        q=Pipeline(reopened); q.reconcile_publications(); q.reconcile_publications()
        for staged in (root/'.publications').glob('*.staged.mp4'):
            assert reopened.one('select 1 from publication_intents where token=?',(staged.name.split('.')[0],))


@pytest.mark.parametrize('kind', ['same_bytes_new_inode','symlink','fifo','both','malformed','bad_final_type','bad_identity_type'])
def test_uncertain_intent_preserved_for_manual_review(tmp_path, kind):
    db,p,pid,root,*_ = fixture(tmp_path)
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at=='after_staging_move' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    intent=dict(db.one('select * from publication_intents'))
    staged=Path(json.loads(intent['journal_json'])['staging_path'])
    original=staged.read_bytes()
    outside=tmp_path/'outside'; outside.write_bytes(original)
    if kind=='same_bytes_new_inode':
        held=staged.with_suffix('.held'); staged.rename(held); staged.write_bytes(original)
    elif kind=='symlink': staged.unlink(); staged.symlink_to(outside)
    elif kind=='fifo': staged.unlink(); os.mkfifo(staged)
    elif kind=='both':
        source=Path(intent['source_path']); source.parent.mkdir(); source.write_bytes(original)
    elif kind=='bad_final_type':
        payload=json.loads(intent['journal_json']); payload['final_path']=None
        db.execute('update publication_intents set journal_json=?',(json.dumps(payload),))
    elif kind=='bad_identity_type': db.execute("update publication_intents set identity_json='[null,null,null,null,null,null,null]'")
    else: db.execute("update publication_intents set journal_json='{}'")
    p._publication_crash=lambda at: None
    p.reconcile_publications(); p.reconcile_publications()
    assert db.one('select state from publication_intents')['state']=='MANUAL_REVIEW'
    assert os.path.lexists(staged) and outside.read_bytes()==original
    assert not db.one('select 1 from final_assemblies')


@pytest.mark.parametrize('point', ['after_intent_commit','after_staging_move','after_prepared_insert','after_prepared_commit','after_rename_before_registration','after_final_fsync','after_registration_before_cleanup'])
def test_hard_process_exit_recovery(tmp_path, point):
    import subprocess, sys
    db,p,pid,root,*_=fixture(tmp_path); path=db.path; db.close()
    script = """
import os, sys
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from test_final_assembly import SuccessfulFakeAssembler
from pathlib import Path
path,pid,root,point=sys.argv[1:]
with Database(path) as db:
    p=Pipeline(db)
    def crash(at):
        if at==point: os._exit(73)
    p._publication_crash=crash
    p.assemble(pid,Path(root)/'final.mp4',assembler=SuccessfulFakeAssembler(root))
"""
    env=dict(os.environ, PYTHONPATH='src:tests', PYTHONDONTWRITEBYTECODE='1')
    result=subprocess.run([sys.executable,'-c',script,str(path),pid,str(root),point],env=env,capture_output=True,timeout=20)
    assert result.returncode==73, result.stderr.decode()
    with Database(path) as reopened:
        # Deterministic expired-crashed-owner fixture, not a production lease override.
        reopened.execute("update reconciliation_leases set expires_at='2000-01-01T00:00:00+00:00'")
        q=Pipeline(reopened); q.reconcile_publications(); q.reconcile_publications()
        assert (root/'final.mp4').read_bytes()==b'generated'
        assert reopened.one('select count(*) n from final_assemblies')['n']==1


@pytest.mark.parametrize('point', ['before_promotion','before_registration'])
def test_evidence_mutation_at_action_boundary_never_registers(tmp_path, point):
    db,p,pid,root,*_=fixture(tmp_path)
    p._publication_crash=lambda at: p.pause(pid) if at==point else None
    with pytest.raises(Exception): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert not db.one('select 1 from final_assemblies')
    assert not (root/'final.mp4').exists()


def test_stale_before_promotion_does_not_expose_requested_name(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at=='after_prepared_commit' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    exposed=[]
    def mutate(at):
        if at=='before_promotion': p.pause(pid)
        if at=='after_rename_before_registration': exposed.append((root/'final.mp4').exists())
    p._publication_crash=mutate
    p.reconcile_publications()
    assert not exposed and not (root/'final.mp4').exists()
    assert not db.one('select 1 from final_assemblies')


def test_two_legacy_locations_require_manual_review_without_moving_either(tmp_path):
    db,p,pid,root,*_=fixture(tmp_path)
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at=='after_prepared_commit' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    db.execute('delete from publication_intents')
    row=dict(db.one('select * from publication_journal'))
    Path(row['final_path']).write_bytes(b'generated')
    p._publication_crash=lambda at: None
    p.reconcile_publications()
    assert db.one('select state from publication_journal')['state']=='MANUAL_REVIEW'
    assert Path(row['staging_path']).read_bytes()==Path(row['final_path']).read_bytes()==b'generated'


def test_recovery_batch_bound_and_next_pass_progress(tmp_path):
    from du_pipeline.service import now
    db,p,pid,root,*_=fixture(tmp_path)
    (root/'.publications').mkdir()
    p.PUBLICATION_RECOVERY_LIMIT=2
    for i in range(3):
        db.execute('insert into publication_journal values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (str(i),pid,db.one('select version from projects')['version'],str(i),p._manifest_hash(pid),str(root/'.publications'/str(i)),str(root/f'{i}.mp4'),'0'*64,1,'{}','PREPARED',None,now(),now(),None))
    assert len(p.reconcile_publications())==2
    assert db.one("select count(*) n from publication_journal where state='PREPARED'")['n']==1
    p.reconcile_publications()
    assert not db.one("select 1 from publication_journal where state='PREPARED'")


@pytest.mark.parametrize('boundary',['after_intent_commit','before_promotion','before_registration'])
def test_lost_lease_fences_next_filesystem_or_registration_action(tmp_path,boundary):
    db,p,pid,root,*_=fixture(tmp_path)
    def lose(at):
        if at==boundary: db.execute("update reconciliation_leases set owner_token='other'")
    p._publication_crash=lose
    with pytest.raises(LeaseUnavailable):
        p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert not db.one('select 1 from final_assemblies')
    # A global lease failure is not disguised as an isolated bad row.
    assert not db.one("select 1 from publication_journal where state='MANUAL_REVIEW'")


def test_publication_rejects_outer_transaction_before_render(tmp_path):
    from du_pipeline.media import AssemblyError
    db,p,pid,root,*_=fixture(tmp_path)
    with db.transaction():
        with pytest.raises(AssemblyError,match='top-level'):
            p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert not list(root.glob('.assembly-*'))


def test_publication_rejects_weak_sqlite_sync_without_changing_setting(tmp_path):
    from du_pipeline.media import AssemblyError
    db,p,pid,root,*_=fixture(tmp_path)
    db.execute('pragma synchronous=NORMAL')
    with pytest.raises(AssemblyError,match='synchronous FULL'):
        p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    assert db.one('pragma synchronous')[0]==1


def test_v13_additive_migration_failure_is_atomic_and_reopen_preserves_legacy(tmp_path, monkeypatch):
    import du_pipeline.db as db_module
    db,p,pid,root,*_=fixture(tmp_path)
    p._publication_crash=lambda at: (_ for _ in ()).throw(Crash()) if at=='after_prepared_commit' else None
    with pytest.raises(Crash): p.assemble(pid,root/'final.mp4',assembler=SuccessfulFakeAssembler(root))
    legacy=tuple(db.one('select * from publication_journal'))
    db.execute('drop table publication_intents'); db.execute('pragma user_version=12')
    path=db.path; db.close()
    connect=sqlite3.connect
    def denied(*a,**k):
        conn=connect(*a,**k)
        def authorize(action,arg1,arg2,*rest):
            if action==sqlite3.SQLITE_CREATE_INDEX and arg1=='idx_publication_intent_state': return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize); return conn
    monkeypatch.setattr(db_module.sqlite3,'connect',denied)
    with pytest.raises(sqlite3.DatabaseError): Database(path)
    monkeypatch.setattr(db_module.sqlite3,'connect',connect)
    with connect(path) as raw:
        assert raw.execute('pragma user_version').fetchone()[0]==12
        assert not raw.execute("select 1 from sqlite_master where name='publication_intents'").fetchone()
        assert raw.execute('select * from publication_journal').fetchone()==legacy
    for _ in range(2):
        with Database(path) as reopened:
            assert reopened.one('pragma user_version')[0]==db_module.SCHEMA_VERSION
            assert len(reopened.all('pragma table_info(publication_journal)'))==15
            q=Pipeline(reopened); q.reconcile_publications()
            assert reopened.one('select count(*) n from final_assemblies')['n']==1
