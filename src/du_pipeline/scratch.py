"""Sealed scratch receipts in the existing append-only event ledger.

Only bytes copied by this API into an exclusive name acquire ownership. No
caller-selected deletion paths, directory sweeps, or unsealed orphan deletion.
"""
import json
import os
import uuid
from pathlib import Path
from .atomic_fs import snapshot_at,assert_identity_at


def event_receipt(p,kind,token):
    row=p.db.one("select data_json from events where type=? and json_extract(data_json,'$.token')=? order by id desc limit 1",(kind,token))
    return json.loads(row['data_json']) if row else None


def allocate(p,sid,payload):
    if p.db._tx_depth: raise RuntimeError('scratch requires an independent journal transaction')
    if not isinstance(payload,bytes): raise TypeError('sealed scratch payload must be bytes')
    with p.db.transaction():
        s=p.scene(sid); pid=s['project_id']; p._active(pid)
        n=p._attempt_count(sid)+1
        if n>3: raise ValueError('maximum 3 attempts')
        if s['state'] not in ('PLANNED','RETRY_QUEUED','RETRYABLE'): raise PermissionError('illegal image attempt transition')
        token=uuid.uuid4().hex
        path=p._root_lookup(pid)/'.attempt-scratch'/token/'payload'
        r={'schema_version':1,'token':token,'project_id':pid,'scene_id':sid,'scene_code':s['code'],'epoch':p._attempt_epoch(pid),'number':n,'project_version':p.db.one('select version from projects where id=?',(pid,))['version'],'path':str(path)}
        p._event(pid,'SCRATCH_ALLOCATION_PREPARED',r)
    # Intent survives any creation/write/receipt failure. Unsealed bytes remain
    # manual-review only; intent/path alone can never authorize their deletion.
    fd,name=p._publication_parent_fd(pid,path,create=True)
    try:
        leaf=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(leaf,'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.fsync(fd)
        identity,digest=snapshot_at(fd,name)
        r.update(identity=identity,sha256=digest,size=identity[5])
        with p.db.transaction(): p._event(pid,'SCRATCH_ALLOCATED',r)
        return r
    finally: os.close(fd)


def consume(p,s,n,token,success):
    r=event_receipt(p,'SCRATCH_ALLOCATED',token)
    if not r or any(r[k]!=v for k,v in {'project_id':s['project_id'],'scene_id':s['id'],'scene_code':s['code'],'epoch':p._attempt_epoch(s['project_id']),'number':n,'project_version':p.db.one('select version from projects where id=?',(s['project_id'],))['version']}.items()):
        raise PermissionError('wrong or stale scratch owner receipt')
    if event_receipt(p,'SCRATCH_CONSUMED',token): raise PermissionError('scratch receipt already consumed')
    return r


def protected(p,r,fd,name):
    uri=r['path']
    # All artifact history is protected, not just ACTIVE; global URI ownership.
    if p.db.one('select 1 from artifacts where uri=? limit 1',(uri,)) or p.db.one('select 1 from audio where uri=? limit 1',(uri,)):
        return True
    if p.db.one('select 1 from publication_journal where staging_path=? or final_path=?',(uri,uri)) or p.db.one('select 1 from remote_uploads where local_path=?',(uri,)):
        return True
    target=os.stat(name,dir_fd=fd,follow_symlinks=False)
    if target.st_nlink!=1: return True
    # Alias lookups are protection-only metadata observations, never sources for
    # reading/unlinking. A symlink reference must not lose its target either.
    refs=p.db.all('select uri from artifacts union select uri from audio union select staging_path from publication_journal union select final_path from publication_journal union select local_path from remote_uploads')
    for ref in refs:
        try: st=os.stat(ref[0])
        except FileNotFoundError: continue
        except (OSError,TypeError): return True
        if (st.st_dev,st.st_ino)==(target.st_dev,target.st_ino): return True
    return False


def reconcile(p,lease):
    if p.db._tx_depth: raise RuntimeError('scratch recovery requires an independent journal transaction')
    for row in p.db.all("select data_json from events where type='SCRATCH_CONSUMED' order by id"):
        consumed=json.loads(row['data_json']); token=consumed['token']
        if consumed['success'] or event_receipt(p,'SCRATCH_RETIRED',token): continue
        r=event_receipt(p,'SCRATCH_ALLOCATED',token)
        if not r: continue
        attempt=p.db.one('select * from attempts where id=?',(consumed['attempt_id'],))
        if not attempt or attempt['state']!='FAILED' or any(attempt[k]!=r[v] for k,v in [('project_id','project_id'),('scene_code','scene_code'),('epoch','epoch'),('number','number'),('failed_path','path'),('failed_sha256','sha256')]): continue
        p.db.renew_reconciliation_lease(lease)
        try: fd,name=p._publication_parent_fd(r['project_id'],r['path'])
        except (OSError,ValueError): continue
        try:
            with p.db.transaction():
                p.db.assert_reconciliation_lease(lease)
                prepared=event_receipt(p,'SCRATCH_RETIRE_PREPARED',token)
                try: candidate=snapshot_at(fd,name)
                except FileNotFoundError:
                    if prepared: p._event(r['project_id'],'SCRATCH_RETIRED',{'token':token,'outcome':'absent_after_intent'})
                    continue
                if candidate!=(tuple(r['identity']),r['sha256']) or protected(p,r,fd,name): continue
                p.db.assert_reconciliation_lease(lease)
                if not prepared: p._event(r['project_id'],'SCRATCH_RETIRE_PREPARED',r)
            p._scratch_crash('after_retire_prepared')
            with p.db.transaction():
                # Re-read after durable intent; never trust previous namespace.
                if snapshot_at(fd,name)!=(tuple(r['identity']),r['sha256']) or protected(p,r,fd,name): continue
                p.db.assert_reconciliation_lease(lease)
                p._deletion_unlink(fd,name,r['identity']); os.fsync(fd)
                p._scratch_crash('after_scratch_unlink')
                p._event(r['project_id'],'SCRATCH_RETIRED',{'token':token,'outcome':'unlinked'})
        except OSError:
            continue
        finally: os.close(fd)
