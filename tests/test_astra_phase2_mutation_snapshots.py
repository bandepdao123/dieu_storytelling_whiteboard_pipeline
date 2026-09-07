"""Public API races using two real connections; no fabricated approvals."""

import pytest

from test_astra_phase2_jobs import pair, before_transaction, approve


def mutate(p, pid, entry):
    if entry == 'audio':
        return p.import_audio(pid, 'replacement.wav', 6000, 'b' * 64)
    if entry == 'srt':
        return p.import_srt(pid, [(0, 6000, 'replacement')])
    if entry == 'document':
        return p.import_document(pid, 'text', 'replacement document')
    if entry == 'config':
        return p.configure_project(pid, 'language', 'en')
    if entry == 'preset':
        return p.select_preset(pid, 'presentation')
    raise AssertionError(entry)


@pytest.mark.parametrize('entry', ['audio', 'srt', 'document', 'config', 'preset'])
@pytest.mark.parametrize('stop', ['pause', 'cancel'])
def test_stop_before_mutation_rejects_without_content_changes(pair, monkeypatch, entry, stop):
    p, other, pid, scene, root = pair
    expected = []

    def stop_first():
        (other.pause if stop == 'pause' else other.cancel_project)(pid)
        expected.append(list(other.db.conn.iterdump()))

    fired = before_transaction(monkeypatch, p.db, stop_first)
    with pytest.raises(PermissionError):
        mutate(p, pid, entry)
    assert fired
    assert list(p.db.conn.iterdump()) == expected[0]


def test_srt_validates_current_audio_not_pretransaction_duration(pair, monkeypatch):
    p, other, pid, scene, root = pair
    expected = []

    def change_audio():
        other.import_audio(pid, 'longer.wav', 12000, 'c' * 64)
        expected.append(list(other.db.conn.iterdump()))

    fired = before_transaction(monkeypatch, p.db, change_audio)
    with pytest.raises(ValueError):
        p.import_srt(pid, [(0, 6000, 'new text')])
    assert fired
    assert list(p.db.conn.iterdump()) == expected[0]


@pytest.mark.parametrize('view', ['status', 'status_summary'])
def test_polling_reads_one_snapshot_while_other_connection_commits(pair, monkeypatch, view):
    p, other, pid, scene, root = pair
    approve(p, pid, scene, root)
    expected = getattr(p, view)(pid)
    original = p.db.one
    fired = []

    def after_project(sql, args=()):
        result = original(sql, args)
        if not fired and sql.lower().startswith('select * from projects where id='):
            fired.append(True)
            other.configure_project(pid, 'language', 'en')
        return result

    monkeypatch.setattr(p.db, 'one', after_project)
    actual = getattr(p, view)(pid)
    assert fired
    assert actual == expected
    assert getattr(p, view)(pid)['project']['language'] == 'en'
    assert not p.db.conn.in_transaction
    assert p.db._tx_depth == 0


@pytest.mark.parametrize('entry', ['audio', 'srt', 'document', 'config', 'preset'])
def test_mutation_authoritative_active_check_holds_writer(pair, monkeypatch, entry):
    import sqlite3

    p, other, pid, scene, root = pair
    other.db.conn.execute('PRAGMA busy_timeout=0')
    original = p._active
    checked = []

    def check(pid):
        result = original(pid)
        if p.db._tx_depth:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.pause(pid)
            checked.append(True)
        return result

    monkeypatch.setattr(p, '_active', check)
    mutate(p, pid, entry)
    assert checked
    other.pause(pid)
    assert other.status(pid)['project']['state'] == 'PAUSED'


@pytest.mark.parametrize('view', ['status', 'status_summary'])
def test_snapshot_failure_releases_transaction_and_nested_scope_keeps_outer(pair, monkeypatch, view):
    p, other, pid, scene, root = pair
    original = p.db.all

    class Interrupted(BaseException):
        pass

    def fail(*args, **kwargs):
        raise Interrupted()

    monkeypatch.setattr(p.db, 'all', fail)
    with pytest.raises(Interrupted):
        getattr(p, view)(pid)
    assert p.db._tx_depth == 0
    assert not p.db.conn.in_transaction
    with p.db.transaction():
        p.configure_project(pid, 'whiteboard_mode', 'test-mode')
        with pytest.raises(Interrupted):
            getattr(p, view)(pid)
        assert p.db._tx_depth == 1
        assert p.db.conn.in_transaction
    monkeypatch.setattr(p.db, 'all', original)
    assert p.status(pid)['project']['whiteboard_mode'] == 'test-mode'


@pytest.mark.parametrize('entry', ['audio', 'srt', 'document', 'config', 'preset'])
def test_mutation_event_failure_caught_in_outer_rolls_back_only_mutation(pair, monkeypatch, entry):
    p, other, pid, scene, root = pair
    before = list(p.db.conn.iterdump())

    def fail(*args, **kwargs):
        raise RuntimeError('event failure')

    monkeypatch.setattr(p, '_event', fail)
    with p.db.transaction():
        with pytest.raises(RuntimeError, match='event failure'):
            mutate(p, pid, entry)
        assert list(p.db.conn.iterdump()) == before
        assert p.db._tx_depth == 1
    assert list(p.db.conn.iterdump()) == before


def test_document_normalization_does_not_hold_sql_writer(pair):
    p, other, pid, scene, root = pair
    called = []

    class LocalAdapter:
        def fetch(self, kind, source):
            assert not p.db.conn.in_transaction
            other.pause(pid)
            called.append(True)
            return {'kind': 'text', 'text': 'adapter result'}

    with pytest.raises(PermissionError):
        p.import_document(pid, 'url', 'local-test-only', remote=LocalAdapter())
    assert called
    assert not p.db.one("select 1 from learning_metadata where key='normalized_input'")
