from datetime import datetime, timezone, timedelta
import threading
import pytest

from du_pipeline.db import Database, LeaseUnavailable
from du_pipeline.service import Pipeline


def test_separate_connections_mutual_exclusion_and_non_owner_release(tmp_path):
    path=tmp_path/'db.sqlite'; a=Database(path); b=Database(path)
    token=a.acquire_reconciliation_lease()
    assert b.release_reconciliation_lease('not-owner') is False
    with pytest.raises(LeaseUnavailable): b.acquire_reconciliation_lease()
    errors=[]
    def contender():
        with Database(path) as connection:
            try: connection.acquire_reconciliation_lease()
            except Exception as exc: errors.append(exc)
    t=threading.Thread(target=contender)
    t.start(); t.join()
    assert len(errors)==1 and isinstance(errors[0],LeaseUnavailable)
    assert a.release_reconciliation_lease(token)
    b.close(); a.close()


def test_stale_reclaim_and_old_owner_is_fenced(tmp_path):
    path=tmp_path/'db.sqlite'; a=Database(path); b=Database(path)
    old=a.acquire_reconciliation_lease()
    a.execute("update reconciliation_leases set expires_at=? where name='artifact-deletion'",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    new=b.acquire_reconciliation_lease()
    assert new != old
    assert a.release_reconciliation_lease(old) is False
    with pytest.raises(LeaseUnavailable): a.renew_reconciliation_lease(old)
    assert b.release_reconciliation_lease(new)
    b.close(); a.close()


def test_crash_recovery_reconcile_is_idempotent(tmp_path):
    path=tmp_path/'db.sqlite'; a=Database(path); p=Pipeline(a)
    pid=p.init_project('x',scene_range=(1,1)); source=tmp_path/'source'; source.write_bytes(b'x')
    aid=p.add_artifact(pid,'IMAGE',source)
    row=a.one('select uri from artifacts where id=?',(aid,)); original=__import__('pathlib').Path(row['uri'])
    staged=original.with_name(original.name+'.deleting-crash'); original.replace(staged)
    expired=a.acquire_reconciliation_lease()
    a.execute("update reconciliation_leases set expires_at=?",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),))
    b=Database(path); Pipeline(b).reconcile_deletions(); Pipeline(b).reconcile_deletions()
    assert original.read_bytes()==b'x' and not staged.exists()
    assert a.release_reconciliation_lease(expired) is False
    b.close(); a.close()
