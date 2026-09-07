"""Public Discord exception boundary; temporary databases, no integrations."""
import pytest
from du_pipeline.adapters import CommandError, parse_discord
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


@pytest.mark.parametrize('key', [None, 'status-fault'])
def test_public_dispatch_preserves_programming_typeerror(tmp_path, monkeypatch, key):
    with Database(tmp_path/'test.db') as db:
        p = Pipeline(db)
        fault = TypeError('programming fault')
        def broken(*args, **kwargs):
            raise fault
        monkeypatch.setattr(p, 'status', broken)
        before = list(db.conn.iterdump())
        with pytest.raises(TypeError) as caught:
            p.dispatch_discord('du-project-status arbitrary', 'OWNER', key)
        assert caught.value is fault
        assert list(db.conn.iterdump()) == before
        assert not db.conn.in_transaction


@pytest.mark.parametrize('text', [None, 123, b'du-status x', [], {}])
@pytest.mark.parametrize('key', [None, 'invalid-text'])
def test_public_dispatch_rejects_nontext_at_input_boundary(tmp_path, text, key):
    with Database(tmp_path/'test.db') as db:
        before = list(db.conn.iterdump())
        with pytest.raises(CommandError) as caught:
            Pipeline(db).dispatch_discord(text, 'OWNER', key)
        assert caught.value.code == 'INVALID_ARGUMENT'
        assert list(db.conn.iterdump()) == before


@pytest.mark.parametrize('text', [None, 123, b'du-status x', [], {}])
def test_parser_rejects_nontext_explicitly(text):
    with pytest.raises(CommandError) as caught:
        parse_discord(text)
    assert caught.value.code == 'INVALID_ARGUMENT'


@pytest.mark.parametrize('key', [None, 'matrix'])
@pytest.mark.parametrize('text,code', [
    ('', 'INVALID_COMMAND'), ('status x', 'INVALID_COMMAND'),
    ('du-unknown x', 'UNKNOWN_COMMAND'),
    ('du-project-create "broken', 'INVALID_ARGUMENTS'),
    ('du-status', 'INVALID_ARGUMENTS'),
    ('du-status x extra', 'INVALID_ARGUMENTS'),
    ('du-scene-retry nope', 'INVALID_ARGUMENT'),
    ('du-dependency-approve nope', 'INVALID_ARGUMENT'),
    ('du-dependency-apply nope', 'INVALID_ARGUMENT'),
    ('du-scene-replace nope path', 'INVALID_ARGUMENT'),
    ('du-scene-replace 1', 'INVALID_ARGUMENTS'),
    ('du-project-status unknown', 'INVALID_ARGUMENT'),
    ('du-scene-status unknown S001', 'INVALID_ARGUMENT'),
    ('du-scene-retry 99999', 'INVALID_ARGUMENT'),
    ('du-cost-report unknown', 'INVALID_ARGUMENT'),
    ('du-error-report unknown', 'INVALID_ARGUMENT'),
])
def test_dispatch_input_error_matrix(tmp_path, key, text, code):
    with Database(tmp_path/'test.db') as db:
        before = list(db.conn.iterdump())
        with pytest.raises(CommandError) as caught:
            Pipeline(db).dispatch_discord(text, 'OWNER', key)
        assert caught.value.code == code
        assert list(db.conn.iterdump()) == before
        assert not db.conn.in_transaction


@pytest.mark.parametrize('key', [None, 'valid'])
def test_dispatch_valid_aliases_scenes_and_roles(tmp_path, key):
    with Database(tmp_path/'test.db') as db:
        p = Pipeline(db)
        result = p.dispatch_discord('du-tao-du-an "Demo quoted" vi', 'OWNER', key)
        if key:
            assert p.dispatch_discord('du-tao-du-an "Demo quoted" vi', 'OWNER', key) == result
        pid = result['project_id']
        assert p.dispatch_discord(f'du-trang-thai {pid}', 'REVIEWER')['project']['name'] == 'Demo quoted'
        pid = p.init_project('scene fixture', scene_range=(1,10))
        p.import_audio(pid, 'metadata', 6000, 'a'*64)
        p.import_srt(pid, [(0,6000,'scene')])
        scene = p.plan_scenes(pid)[0]
        assert p.dispatch_discord(f'du-canh {pid} {scene["code"]}', 'REVIEWER')['id'] == scene['id']
        with pytest.raises(CommandError, match='scene not found'):
            p.dispatch_discord(f'du-canh {pid} missing', 'OWNER')
        before = list(db.conn.iterdump())
        with pytest.raises(PermissionError):
            p.dispatch_discord(f'du-project-pause {pid}', 'REVIEWER', 'denied' if key else None)
        assert list(db.conn.iterdump()) == before


def test_mutating_typeerror_rolls_back_and_does_not_publish_success(tmp_path, monkeypatch):
    with Database(tmp_path/'test.db') as db:
        p = Pipeline(db); pid = p.init_project('rollback')
        before = list(db.conn.iterdump()); fault = TypeError('event programming fault')
        original = p._event
        def broken(*args, **kwargs):
            original(*args, **kwargs)
            raise fault
        monkeypatch.setattr(p, '_event', broken)
        with pytest.raises(TypeError) as caught:
            p.dispatch_discord(f'du-project-pause {pid}', 'OWNER', 'mutation-fault')
        assert caught.value is fault
        assert list(db.conn.iterdump()) == before
        assert not db.conn.in_transaction
