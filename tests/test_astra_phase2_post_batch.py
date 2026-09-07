"""Public post-batch and dependency interleavings; disposable two-connection DBs."""
import pytest
from du_pipeline.contracts import QAEvidence
from test_astra_phase2_jobs import pair, approve, before_transaction


@pytest.mark.parametrize('mutation', ['pause', 'cancel', 'readiness', 'revision'])
def test_post_batch_rechecks_before_writer(pair, monkeypatch, mutation):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    qa = QAEvidence({'ok': True}, 1, 'test')
    contact = root / 'new-contact'
    contact.write_bytes(b'new contact')
    before = p.db.one("select count(*) from approvals where gate='POST_BATCH'")[0]
    def change():
        if mutation == 'pause': other.pause(pid)
        elif mutation == 'cancel': other.cancel_project(pid)
        elif mutation == 'revision': other.configure_project(pid, 'language', 'en')
        else:
            # Animation state is a disposable readiness fault; no fabricated approvals.
            other.db.execute("update scenes set state='ANIMATED' where id=?", (scene['id'],))
    fired = before_transaction(monkeypatch, p.db, change)
    with pytest.raises(PermissionError):
        p.approve_post_batch(pid, qa, contact, 'reviewer')
    assert fired
    assert p.db.one("select count(*) from approvals where gate='POST_BATCH'")[0] == before
    assert contact.read_bytes() == b'new contact'


def test_dependency_double_apply_before_cas_and_replay(pair, monkeypatch):
    p, other, pid, scene, root = pair
    proposal = p.propose_dependency(pid, 'local-test', {})
    p.decide_dependency(proposal, True, 'owner')
    fired = before_transaction(monkeypatch, p.db, lambda: other.apply_dependency(proposal))
    with pytest.raises(PermissionError): p.apply_dependency(proposal)
    assert fired
    with pytest.raises(PermissionError): p.apply_dependency(proposal)
    assert p.db.one("select count(*) from events where type='DEPENDENCY_APPLIED'")[0] == 1


def test_post_batch_writer_first_and_replay(pair, monkeypatch):
    import sqlite3
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._pilot_ready
    checked = []
    def probe(project):
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            other.pause(project)
        checked.append(True)
        return original(project)
    monkeypatch.setattr(p, '_pilot_ready', probe)
    p.approve_post_batch(pid, QAEvidence({'ok': True}, 1, 'test'), root / 'contact.png', 'reviewer')
    assert checked
    other.pause(pid)
    with pytest.raises(PermissionError):
        p.approve_post_batch(pid, QAEvidence({'ok': True}, 1, 'test'), root / 'contact.png', 'reviewer')


def test_dependency_rejection_before_cas(pair, monkeypatch):
    p, other, pid, scene, root = pair
    proposal = p.propose_dependency(pid, 'local-test', {})
    p.decide_dependency(proposal, True, 'owner')
    # Public API deliberately prohibits reversing APPROVED. Exercise the existing
    # CAS against an explicit disposable storage fault, not a supported decision.
    before_transaction(monkeypatch, p.db, lambda: other.db.execute(
        "update impact_proposals set state='REJECTED' where id=?", (proposal,)))
    with pytest.raises(PermissionError): p.apply_dependency(proposal)
    assert p.db.one("select count(*) from events where type='DEPENDENCY_APPLIED'")[0] == 0
