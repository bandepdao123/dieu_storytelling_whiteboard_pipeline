"""Final writer boundaries, no external services or persistent fixture data."""
import sqlite3
from pathlib import Path
import pytest
from du_pipeline.db import Database
from du_pipeline.service import Pipeline
from test_release_r2_composition import fixture, call


def test_writer_first_blocks_stop(tmp_path,monkeypatch):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db, Database(db.path) as other:
        other.conn.execute('pragma busy_timeout=0')
        q=Pipeline(other); original=p._register_artifact
        def register(*args,**kwargs):
            with pytest.raises(sqlite3.OperationalError,match='locked'): q.pause(pid)
            return original(*args,**kwargs)
        monkeypatch.setattr(p,'_register_artifact',register)
        result=call(p,cmd)
        assert result=={'artifact_id':1}
        q.pause(pid)
        assert call(q,cmd)==result
        assert source.read_bytes()==b'image'


@pytest.mark.parametrize('fault',['expire','attempt','bytes'])
def test_final_fence_rejects_late_worker_or_unknown_bytes(tmp_path,monkeypatch,fault):
    db,p,pid,sid,source,cmd=fixture(tmp_path)
    with db:
        original=p._command_copy_fence; count=0; changed=[]
        def fence(*args):
            nonlocal count
            count+=1
            # Third fence is after SQL mutation, immediately before result.
            if fault in ('expire','attempt') and count==3:
                if fault=='expire': db.execute("update command_copy_requests set lease_expires_at='2000'")
                else: db.execute("update command_copy_requests set attempt='late'")
            # Second fence is after durable preparation but before registration.
            if fault=='bytes' and count==2:
                import json
                prepared=json.loads(db.one('select preparation_json from command_copy_requests')['preparation_json'])
                path=Path(prepared['uri']); path.write_bytes(b'unknown'); changed.append(path)
            return original(*args)
        monkeypatch.setattr(p,'_command_copy_fence',fence)
        with pytest.raises(PermissionError): call(p,cmd)
        assert not db.all('select * from artifacts')
        assert not db.all('select * from command_receipts')
        assert db.one('select state from command_copy_requests')['state']=='MANUAL_REVIEW'
        assert source.read_bytes()==b'image'
        for path in changed: assert path.read_bytes()==b'unknown'
