"""Web boundaries exercised against a disposable core DB, never production data."""
import io
import wave
import importlib.util
import pytest
pytest.importorskip('fastapi', reason='Install the optional web extra to test the web adapter')
pytest.importorskip('httpx')
from fastapi.testclient import TestClient


def test_web_module_exists():
    assert importlib.util.find_spec('du_pipeline.web') is not None


@pytest.fixture
def client(tmp_path):
    from du_pipeline.web import Settings, bootstrap, create_app
    settings = Settings(tmp_path / 'web', 'https://testserver', session_seconds=60, upload_limit=100000)
    bootstrap(settings, 'owner', 'test-only-password-123!')
    with TestClient(create_app(settings), base_url='https://testserver') as c:
        yield c


def login(c):
    r = c.post('/api/login', json={'username':'owner','password':'test-only-password-123!'}, headers={'origin':'https://testserver'})
    assert r.status_code == 200, r.text
    assert 'httponly' in r.headers['set-cookie'].lower()
    assert 'secure' in r.headers['set-cookie'].lower()
    return {'origin':'https://testserver','x-csrf-token':r.json()['csrf']}


def test_auth_csrf_host_throttle(client):
    assert client.get('/api/projects').status_code == 401
    assert client.get('/api/artifacts/1').status_code == 401
    assert client.get('/api/projects', headers={'host':'evil.example'}).status_code == 400
    assert client.post('/api/login', json={}).status_code == 403
    h = login(client)
    assert client.post('/api/projects', json={'name':'Test'}).status_code == 403
    assert client.post('/api/projects', json={'name':'Test'}, headers={**h,'origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/projects', json={'name':'Test','artifact_root':'/tmp'}, headers=h).status_code == 422
    assert client.post('/api/logout', headers=h).status_code == 200
    assert client.get('/api/projects').status_code == 401
    for _ in range(5):
        r = client.post('/api/login', json={'username':'owner','password':'wrong'}, headers={'origin':'https://testserver'})
    assert r.status_code == 429


def test_real_inputs_plan_history_pause_media(client):
    h = login(client)
    r = client.post('/api/projects', json={'name':'Câu chuyện thật','min_scenes':2,'max_scenes':2}, headers=h)
    assert r.status_code == 200, r.text
    pid = r.json()['id']; base = '/api/projects/' + pid
    assert len(client.get('/api/projects').json()['projects']) == 1
    assert client.post(base+'/uploads/script', content='Kịch bản tiếng Việt'.encode(), headers=h).status_code == 200
    audio = io.BytesIO()
    with wave.open(audio,'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(b'\0\0'*16000)
    r = client.post(base+'/uploads/audio', content=audio.getvalue(), headers=h)
    assert r.status_code == 200, r.text
    aid = r.json()['artifact_id']
    assert client.get('/api/artifacts/'+str(aid)).content == audio.getvalue()
    srt = b'1\n00:00:00,000 --> 00:00:01,000\nXin chao\n\n2\n00:00:01,000 --> 00:00:02,000\nTam biet\n'
    assert client.post(base+'/uploads/subtitle', content=srt, headers=h).status_code == 200
    assert client.post(base+'/actions/plan', headers=h).status_code == 200
    detail = client.get(base).json()
    assert len(detail['scenes']) == 2
    assert detail['audio']['duration_ms'] == 2000
    assert any(e['type'] == 'SCENES_PLANNED' for e in detail['history'])
    assert 'uri' not in detail['artifacts'][0]
    assert client.post(base+'/actions/checkpoint', json={'note':'Điểm kiểm tra'}, headers=h).status_code == 200
    assert client.post(base+'/actions/pause', headers=h).status_code == 200
    assert client.post(base+'/actions/plan', headers=h).status_code == 409
    assert client.post(base+'/actions/resume', headers=h).status_code == 200
    sid = detail['scenes'][0]['id']
    assert client.post(base+f'/scenes/{sid}/review', json={'decision':'APPROVED'}, headers=h).status_code == 409
    assert client.post(base+'/uploads/audio', content=b'not wav', headers=h).status_code == 409
    assert client.post(base+'/uploads/script', content=b'a'*100001, headers=h).status_code == 413
    assert client.get('/api/artifacts/999999').status_code == 404
    assert client.get('/api/artifacts/1?path=/etc/passwd').status_code == 422


def test_unicode_username_and_csrf_are_safe(client):
    h = login(client)
    response = client.post('/api/projects', json={'name':'Test'}, headers={**h, 'x-csrf-token':'invalid'})
    assert response.status_code == 403
    response = client.post('/api/login', json={'username':'chủ sở hữu','password':'test-only-password-123!'}, headers={'origin':'https://testserver'})
    assert response.status_code == 401


def test_bootstrap_rejects_symlink_before_writing(tmp_path):
    from du_pipeline.web import Settings, bootstrap
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    victim = tmp_path / 'victim'
    victim.write_text('untouched')
    (root / 'web-instance.json').symlink_to(victim)
    with pytest.raises(ValueError):
        bootstrap(Settings(root, 'https://testserver'), 'owner', 'test-only-password-123!')
    assert victim.read_text() == 'untouched'


def test_streamed_limits_and_sanitized_errors(client):
    h = login(client)
    pid = client.post('/api/projects', json={'name':'Limits'}, headers=h).json()['id']
    response = client.post(f'/api/projects/{pid}/uploads/script', content=iter([b'x'*60000, b'y'*60000]), headers=h)
    assert response.status_code == 413
    response = client.post('/api/projects', content=iter([b' '*20000]), headers=h)
    assert response.status_code == 413
    response = client.post(f'/api/projects/{pid}/uploads/script', content=b'\xff', headers=h)
    assert response.status_code == 409
    assert '/tmp/' not in response.text
    assert not list((client.app.state.settings.data_dir/'uploads').iterdir())


def test_expired_session_blocks_media(client):
    import time
    from du_pipeline.web import auth_db
    h = login(client)
    with auth_db(client.app.state.settings) as c:
        c.execute('UPDATE sessions SET expires=?', (time.time()-1,))
    assert client.get('/api/session').status_code == 401
    assert client.get('/api/artifacts/1').status_code == 401


def test_frontend_source_exists():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert (root/'web/src/App.tsx').is_file()
    assert (root/'web/package.json').is_file()
