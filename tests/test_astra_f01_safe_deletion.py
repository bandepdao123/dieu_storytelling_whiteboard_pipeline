"""F01: deterministic namespace races; all payloads are disposable tmp fixtures."""
from contextlib import contextmanager
from pathlib import Path
import os

import pytest

from du_pipeline.db import Database
from du_pipeline.service import Pipeline


@pytest.fixture
def owned(tmp_path):
    with Database(tmp_path / 'db.sqlite') as db:
        p = Pipeline(db)
        pid = p.init_project('F01 dummy', scene_range=(1, 1))
        root = Path(p.status(pid)['project']['artifact_root'])
        nested = root / 'nested'
        nested.mkdir()
        original = nested / 'victim'
        original.write_bytes(b'owned')
        aid = p.add_artifact(pid, 'IMAGE', original)
        db.execute("update artifacts set expires_at='2000-01-01' where id=?", (aid,))
        outside = tmp_path / 'outside'
        outside.mkdir()
        yield db, p, pid, root, original, aid, outside


def swap_parent(original, outside):
    parent = original.parent
    saved = parent.with_name('saved')
    parent.rename(saved)
    parent.symlink_to(outside, target_is_directory=True)
    return saved


@pytest.mark.parametrize('operation', ['cleanup', 'restore', 'discard'])
def test_parent_swap_at_lease_fence_never_mutates_outside(owned, monkeypatch, operation):
    db, p, pid, root, original, aid, outside = owned
    staged = original.with_name('victim.deleting-crash')
    if operation != 'cleanup':
        original.rename(staged)
        if operation == 'discard':
            db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    (outside / original.name).write_bytes(b'outside original')
    (outside / staged.name).write_bytes(b'outside staged')
    real = db.assert_reconciliation_lease
    swapped = False
    def fence(token):
        nonlocal swapped
        real(token)
        if not swapped:
            swap_parent(original, outside)
            swapped = True
    monkeypatch.setattr(db, 'assert_reconciliation_lease', fence)
    if operation == 'cleanup':
        p.cleanup()
    else:
        p.reconcile_deletions()
    assert swapped
    assert (outside / original.name).read_bytes() == b'outside original'
    assert (outside / staged.name).read_bytes() == b'outside staged'


def test_rollback_parent_swap_restores_only_pinned_owned_file(owned, monkeypatch):
    db, p, pid, root, original, aid, outside = owned
    real_tx = db.transaction
    injected = False
    @contextmanager
    def transaction():
        nonlocal injected
        with real_tx():
            yield db
            if not injected and db.one('select status from artifacts where id=?', (aid,))['status'] == 'DELETED':
                injected = True
                staged = next(original.parent.glob('victim.deleting-*'))
                (outside / staged.name).write_bytes(b'outside staged')
                swap_parent(original, outside)
                raise RuntimeError('rollback after rename')
    monkeypatch.setattr(db, 'transaction', transaction)
    with pytest.raises(RuntimeError, match='rollback after rename'):
        p.cleanup()
    assert (root / 'saved' / 'victim').read_bytes() == b'owned'
    assert not (outside / 'victim').exists()
    assert [x.read_bytes() for x in outside.iterdir()] == [b'outside staged']
    assert db.one('select status from artifacts where id=?', (aid,))['status'] == 'ACTIVE'


@pytest.mark.parametrize('operation', ['cleanup', 'reconcile'])
def test_unknown_bytes_are_retained_even_for_deleted_rows(owned, operation):
    db, p, pid, root, original, aid, outside = owned
    if operation == 'cleanup':
        candidate = original
    else:
        candidate = original.with_name('victim.deleting-unknown')
        original.rename(candidate)
        db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    candidate.write_bytes(b'not owned')
    if operation == 'cleanup':
        p.cleanup()
    else:
        p.reconcile_deletions()
    assert candidate.read_bytes() == b'not owned'


@pytest.mark.parametrize('which', ['root', 'parent', 'leaf'])
def test_cleanup_refuses_symlink_components(owned, which):
    db, p, pid, root, original, aid, outside = owned
    if which == 'root':
        root.rename(root.with_name(root.name + '-saved'))
        root.symlink_to(outside, target_is_directory=True)
        (outside / 'nested').mkdir()
        victim = outside / 'nested' / 'victim'
    elif which == 'parent':
        swap_parent(original, outside)
        victim = outside / 'victim'
    else:
        original.unlink()
        victim = outside / 'victim'
        original.symlink_to(victim)
    victim.write_bytes(b'owned')
    p.cleanup()
    assert victim.read_bytes() == b'owned'


@pytest.mark.parametrize('operation', ['cleanup', 'restore'])
def test_atomic_destination_collision_preserves_both_files(owned, monkeypatch, operation):
    import du_pipeline.service as service
    db, p, pid, root, original, aid, outside = owned
    if operation == 'restore':
        original.rename(original.with_name('victim.deleting-crash'))
    real = service.rename_noreplace_at
    collided = []
    def collide(sfd, source, dfd, destination):
        if not collided:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
            os.write(fd, b'concurrent owner')
            os.close(fd)
            collided.append(destination)
        return real(sfd, source, dfd, destination)
    monkeypatch.setattr(service, 'rename_noreplace_at', collide)
    try:
        p.cleanup() if operation == 'cleanup' else p.reconcile_deletions()
    except FileExistsError:
        pass
    assert collided, 'all deletion moves must use atomic no-replace rename'
    assert (original.parent / collided[0]).read_bytes() == b'concurrent owner'
    assert b'owned' in [f.read_bytes() for f in original.parent.iterdir()]


def test_cleanup_persists_ownership_receipt_before_rename(owned, monkeypatch):
    import du_pipeline.service as service
    db, p, pid, root, original, aid, outside = owned
    real = service.rename_noreplace_at
    observed = []
    def rename(sfd, source, dfd, destination):
        if '.deleting-' in destination:
            import sqlite3
            with sqlite3.connect(f'file:{db.path}?mode=ro', uri=True) as other:
                receipt = other.execute("select data_json from events where type='DELETION_PREPARED'").fetchone()
                observed.append(receipt is not None)
        return real(sfd, source, dfd, destination)
    monkeypatch.setattr(service, 'rename_noreplace_at', rename)
    p.cleanup()
    assert observed == [True]


@pytest.mark.parametrize('operation', ['cleanup', 'reconcile'])
def test_leaf_replacement_after_snapshot_is_not_deleted(owned, monkeypatch, operation):
    db, p, pid, root, original, aid, outside = owned
    candidate = original
    if operation == 'reconcile':
        candidate = original.with_name('victim.deleting-crash')
        original.rename(candidate)
        db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    real = p._deletion_snapshot
    replaced = False
    def snapshot(fd, name):
        nonlocal replaced
        result = real(fd, name)
        if result and not replaced:
            candidate.rename(candidate.with_name('retained-owned'))
            candidate.write_bytes(b'replacement')
            replaced = True
        return result
    monkeypatch.setattr(p, '_deletion_snapshot', snapshot)
    try:
        p.cleanup() if operation == 'cleanup' else p.reconcile_deletions()
    except PermissionError:
        pass
    assert replaced
    assert candidate.read_bytes() == b'replacement'
    assert candidate.with_name('retained-owned').read_bytes() == b'owned'


@pytest.mark.parametrize('point', ['before_rename', 'after_rename', 'before_unlink'])
def test_durable_receipt_recovers_crash_and_replay(owned, monkeypatch, point):
    import du_pipeline.service as service
    db, p, pid, root, original, aid, outside = owned
    real = service.rename_noreplace_at
    def crash(sfd, source, dfd, destination):
        if point == 'before_rename':
            raise RuntimeError('crash')
        real(sfd, source, dfd, destination)
        raise RuntimeError('crash')
    if point == 'before_unlink':
        monkeypatch.setattr(p, '_deletion_unlink', lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
    else:
        monkeypatch.setattr(service, 'rename_noreplace_at', crash)
    with pytest.raises(RuntimeError, match='crash'):
        p.cleanup()
    monkeypatch.undo()
    assert db.one("select 1 from events where type='DELETION_PREPARED'")
    p.reconcile_deletions()
    p.reconcile_deletions()
    assert not list(original.parent.glob('*.deleting-*'))
    if point == 'before_unlink':
        assert not original.exists()
        assert db.one('select status from artifacts where id=?', (aid,))['status'] == 'DELETED'
    else:
        assert original.read_bytes() == b'owned'
        assert db.one('select status from artifacts where id=?', (aid,))['status'] == 'ACTIVE'


def test_receipted_staging_rejects_same_bytes_wrong_inode(owned, monkeypatch):
    db, p, pid, root, original, aid, outside = owned
    monkeypatch.setattr(p, '_deletion_unlink', lambda *args: (_ for _ in ()).throw(RuntimeError('crash')))
    with pytest.raises(RuntimeError):
        p.cleanup()
    monkeypatch.undo()
    staged = next(original.parent.glob('*.deleting-*'))
    staged.rename(staged.with_name('saved-owned'))
    staged.write_bytes(b'owned')
    p.reconcile_deletions()
    assert staged.read_bytes() == b'owned'


@pytest.mark.parametrize('operation', ['cleanup', 'reconcile'])
def test_lost_lease_closes_fds_and_preserves_payload(owned, monkeypatch, operation):
    from du_pipeline.db import LeaseUnavailable
    db, p, pid, root, original, aid, outside = owned
    candidate = original
    if operation == 'reconcile':
        candidate = original.with_name('victim.deleting-crash')
        original.rename(candidate)
    before = len(os.listdir('/proc/self/fd'))
    monkeypatch.setattr(db, 'assert_reconciliation_lease', lambda token: (_ for _ in ()).throw(LeaseUnavailable('lost')))
    with pytest.raises(LeaseUnavailable):
        p.cleanup() if operation == 'cleanup' else p.reconcile_deletions()
    assert candidate.read_bytes() == b'owned'
    assert len(os.listdir('/proc/self/fd')) <= before


def test_rollback_destination_collision_leaves_receipted_staging(owned, monkeypatch):
    db, p, pid, root, original, aid, outside = owned
    real_tx = db.transaction
    injected = False
    @contextmanager
    def transaction():
        nonlocal injected
        with real_tx():
            yield db
            if not injected and db.one('select status from artifacts where id=?', (aid,))['status'] == 'DELETED':
                injected = True
                original.write_bytes(b'concurrent owner')
                raise RuntimeError('rollback collision')
    monkeypatch.setattr(db, 'transaction', transaction)
    with pytest.raises(RuntimeError, match='rollback collision'):
        p.cleanup()
    monkeypatch.undo()
    p.reconcile_deletions()
    assert original.read_bytes() == b'concurrent owner'
    assert next(original.parent.glob('*.deleting-*')).read_bytes() == b'owned'
    assert db.one('select status from artifacts where id=?', (aid,))['status'] == 'ACTIVE'


def test_new_format_staging_without_receipt_is_not_owned(owned):
    db, p, pid, root, original, aid, outside = owned
    candidate = original.with_name('victim.deleting-f01-' + 'a' * 32)
    original.rename(candidate)
    db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    p.reconcile_deletions()
    assert candidate.read_bytes() == b'owned'


def test_recovery_rechecks_lease_after_hashing(owned, monkeypatch):
    from du_pipeline.db import LeaseUnavailable
    db, p, pid, root, original, aid, outside = owned
    staged = original.with_name('victim.deleting-crash')
    original.rename(staged)
    db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    real = p._deletion_snapshot
    def snapshot(fd, name):
        result = real(fd, name)
        db.execute("update reconciliation_leases set expires_at='2000-01-01'")
        return result
    monkeypatch.setattr(p, '_deletion_snapshot', snapshot)
    with pytest.raises(LeaseUnavailable):
        p.reconcile_deletions()
    assert staged.read_bytes() == b'owned'


@pytest.mark.parametrize('operation', ['cleanup', 'reconcile'])
def test_root_ancestor_symlink_is_rejected(owned, operation):
    db, p, pid, root, original, aid, outside = owned
    staged = original.with_name('victim.deleting-crash')
    if operation == 'reconcile':
        original.rename(staged)
        db.execute("update artifacts set status='DELETED' where id=?", (aid,))
    ancestor = root.parent
    saved = ancestor.with_name('saved-artifacts')
    ancestor.rename(saved)
    ancestor.symlink_to(saved, target_is_directory=True)
    p.cleanup() if operation == 'cleanup' else p.reconcile_deletions()
    assert (staged if operation == 'reconcile' else original).read_bytes() == b'owned'


@pytest.mark.parametrize('reference', ['narration', 'other_project'])
def test_cleanup_preserves_shared_uri_owners(owned, reference):
    db, p, pid, root, original, aid, outside = owned
    if reference == 'narration':
        p.import_audio(pid, str(original), 6000, __import__('hashlib').sha256(b'owned').hexdigest())
    else:
        other = p.init_project('other owner', scene_range=(1, 1))
        row = db.one('select * from artifacts where id=?', (aid,))
        db.execute("insert into artifacts(project_id,kind,uri,sha256,version,status,created_at,expires_at) values(?,?,?,?,1,'ACTIVE',?,'2999-01-01')",
                   (other, 'IMAGE', str(original), row['sha256'], row['created_at']))
    assert p.cleanup() == 1
    assert original.read_bytes() == b'owned'
    original.rename(original.with_name('victim.deleting-crash'))
    p.reconcile_deletions()
    assert original.read_bytes() == b'owned'


def test_cleanup_does_not_leak_descriptors(owned):
    db, p, pid, root, original, aid, outside = owned
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(20):
        p.reconcile_deletions()
    p.cleanup()
    assert len(os.listdir('/proc/self/fd')) <= before
