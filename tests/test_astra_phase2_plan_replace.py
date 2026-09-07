"""Bounded F10: real WAL connections, deterministic hooks, public mutations."""
from contextlib import contextmanager
from pathlib import Path
import pytest

from test_astra_phase2_jobs import pair, before_transaction, approve


@pytest.mark.parametrize('change', ['audio', 'srt', 'config', 'replan', 'pause', 'cancel'])
@pytest.mark.parametrize('initial', [False, True])
def test_plan_rejects_changed_snapshot(pair, monkeypatch, change, initial):
    p, other, pid, scene, root = pair
    if initial:
        p.import_srt(pid, [(0, 6000, 'unplanned source')])
    expected = []

    def race():
        if change == 'audio':
            other.import_audio(pid, 'new.wav', 6000, 'b' * 64)
        elif change == 'srt':
            other.import_srt(pid, [(0, 6000, 'new source')])
        elif change == 'config':
            other.configure_project(pid, 'language', 'en')
        elif change == 'replan':
            other.plan_scenes(pid, special_codes=('S001',))
        elif change == 'pause':
            other.pause(pid)
        else:
            other.cancel_project(pid)
        expected.append(list(other.db.conn.iterdump()))

    fired = before_transaction(monkeypatch, p.db, race)
    with pytest.raises(PermissionError):
        p.plan_scenes(pid)
    assert fired
    assert list(p.db.conn.iterdump()) == expected[0]


def test_plan_returns_own_committed_rows_not_later_plan(pair, monkeypatch):
    p, other, pid, scene, root = pair
    original = p.db.transaction
    fired = []

    @contextmanager
    def after_commit(**kwargs):
        outer = not p.db.conn.in_transaction
        with original(**kwargs):
            yield p.db
        if outer and kwargs.get('write', True) and not fired:
            fired.append(True)
            other.plan_scenes(pid, special_codes=('S001',))

    monkeypatch.setattr(p.db, 'transaction', after_commit)
    returned = p.plan_scenes(pid)
    assert fired
    assert returned[0]['special'] == 0
    assert other.scene(scene['id'])['special'] == 1


@pytest.mark.parametrize('change', ['pause', 'cancel', 'replan', 'config', 'image'])
def test_replace_rechecks_identity_revision_and_active(pair, monkeypatch, tmp_path, change):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'replacement.png'
    source.write_bytes(b'replacement untouched')
    original_files = {x.name: x.read_bytes() for x in root.iterdir() if x.is_file()}
    expected = []

    def race():
        if change == 'pause':
            other.pause(pid)
        elif change == 'cancel':
            other.cancel_project(pid)
        elif change == 'replan':
            assert other.plan_scenes(pid)[0]['id'] == scene['id']
        elif change == 'config':
            other.configure_project(pid, 'language', 'en')
        else:
            other.add_artifact(pid, 'IMAGE', root / 'image.png', scene['id'])
        expected.append(list(other.db.conn.iterdump()))

    fired = before_transaction(monkeypatch, p.db, race)
    with pytest.raises(PermissionError):
        p.replace_scene_artifact(scene['id'], source)
    assert fired
    assert list(p.db.conn.iterdump()) == expected[0]
    assert source.read_bytes() == b'replacement untouched'
    for name, data in original_files.items():
        assert (root / name).read_bytes() == data
    registered = {Path(a['uri']).name for a in other.db.all('select uri from artifacts')}
    assert {x.name for x in root.iterdir() if x.is_file()} <= set(original_files) | registered


def test_replace_copy_allows_stop_then_compensates_owned_file(pair, monkeypatch, tmp_path):
    import shutil
    p, other, pid, scene, root = pair
    source = tmp_path / 'external.png'
    source.write_bytes(b'copy source')
    original = shutil.copyfileobj
    fired = []
    expected = []

    def copying(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        assert not p.db.conn.in_transaction
        other.pause(pid)
        fired.append(True)
        expected.append(list(other.db.conn.iterdump()))
        return result

    monkeypatch.setattr(shutil, 'copyfileobj', copying)
    before = set(root.iterdir())
    with pytest.raises(PermissionError):
        p.replace_scene_artifact(scene['id'], source)
    assert fired
    assert set(root.iterdir()) == before
    assert source.read_bytes() == b'copy source'
    assert list(p.db.conn.iterdump()) == expected[0]


def test_replace_compensation_preserves_substituted_leaf(pair, monkeypatch, tmp_path):
    p, other, pid, scene, root = pair
    source = tmp_path / 'external.png'
    source.write_bytes(b'source')
    original = p._event
    substituted = []

    def fail(pid, typ, data=None):
        if typ == 'SCENE_ARTIFACT_REPLACED':
            artifact = p.db.one('select uri from artifacts where id=?', (data['artifact'],))
            path = Path(artifact['uri'])
            path.rename(tmp_path / 'saved-owned-copy')
            path.write_bytes(b'not owned')
            substituted.append(path)
            raise RuntimeError('substitution')
        return original(pid, typ, data)

    monkeypatch.setattr(p, '_event', fail)
    with pytest.raises(RuntimeError, match='substitution'):
        p.replace_scene_artifact(scene['id'], source)
    assert substituted[0].read_bytes() == b'not owned'
    assert (tmp_path / 'saved-owned-copy').read_bytes() == b'source'
    assert not p.db.one('select 1 from artifacts')


@pytest.mark.parametrize('change', ['replan', 'cancel'])
def test_replace_copy_race_compensates_after_scene_identity_change(pair, monkeypatch, tmp_path, change):
    import shutil
    p, other, pid, scene, root = pair
    source = tmp_path / 'external.png'
    source.write_bytes(b'source')
    original = shutil.copyfileobj
    before = set(root.iterdir())
    expected = []

    def copying(src, dst, *args, **kwargs):
        result = original(src, dst, *args, **kwargs)
        assert not p.db.conn.in_transaction
        if change == 'replan':
            assert other.plan_scenes(pid)[0]['id'] == scene['id']
        else:
            other.cancel_project(pid)
        expected.append(list(other.db.conn.iterdump()))
        return result

    monkeypatch.setattr(shutil, 'copyfileobj', copying)
    with pytest.raises(PermissionError):
        p.replace_scene_artifact(scene['id'], source)
    assert list(p.db.conn.iterdump()) == expected[0]
    assert set(root.iterdir()) == before
    assert source.read_bytes() == b'source'


def test_plan_snapshot_does_not_mix_project_and_new_sources(pair, monkeypatch):
    p, other, pid, scene, root = pair
    original = p.db.one
    fired = []
    expected = []

    def after_project(sql, args=()):
        result = original(sql, args)
        if not fired and sql.startswith('select * from projects where id='):
            fired.append(True)
            other.import_srt(pid, [(0, 6000, 'concurrent source')])
            expected.append(list(other.db.conn.iterdump()))
        return result

    monkeypatch.setattr(p.db, 'one', after_project)
    with pytest.raises(PermissionError):
        p.plan_scenes(pid)
    assert fired
    assert list(p.db.conn.iterdump()) == expected[0]


@pytest.mark.parametrize('entry', ['plan', 'replace'])
def test_writer_recheck_blocks_second_connection_stop(pair, monkeypatch, tmp_path, entry):
    import sqlite3
    p, other, pid, scene, root = pair
    source = tmp_path / 'external.png'
    source.write_bytes(b'source')
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._planning_snapshot
    calls = []

    def snapshot(pid):
        result = original(pid)
        calls.append(True)
        if len(calls) == 2:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.pause(pid)
        return result

    monkeypatch.setattr(p, '_planning_snapshot', snapshot)
    if entry == 'plan':
        p.plan_scenes(pid)
    else:
        p.replace_scene_artifact(scene['id'], source)
    assert len(calls) == 2
    other.pause(pid)


@pytest.mark.parametrize('base_exception', [False, True])
def test_replace_event_failure_compensates_and_rolls_back(pair, monkeypatch, tmp_path, base_exception):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    source = tmp_path / 'external.png'
    source.write_bytes(b'copy source')
    before = list(p.db.conn.iterdump())
    files = set(root.iterdir())
    original = p._event
    error = KeyboardInterrupt if base_exception else RuntimeError

    def fail(pid, typ, data=None):
        if typ == 'SCENE_ARTIFACT_REPLACED':
            raise error('replacement failure')
        return original(pid, typ, data)

    monkeypatch.setattr(p, '_event', fail)
    with pytest.raises(error, match='replacement failure'):
        p.replace_scene_artifact(scene['id'], source)
    with p.db.transaction():
        with pytest.raises(PermissionError, match='top-level'):
            p.replace_scene_artifact(scene['id'], source)
        assert list(p.db.conn.iterdump()) == before
        assert set(root.iterdir()) == files
    assert source.read_bytes() == b'copy source'
