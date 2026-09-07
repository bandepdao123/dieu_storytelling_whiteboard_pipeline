"""Single-owner web adapter. Core mutations remain exclusively service-owned."""
from __future__ import annotations
import argparse
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .db import Database
from .service import Pipeline
from .adapters import Role
from .cli import srt
from .atomic_fs import open_dir_beneath


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    origin: str
    session_seconds: int = 28800
    upload_limit: int = 64 * 1024 * 1024

    def __post_init__(self):
        p = Path(self.data_dir)
        u = urlsplit(self.origin)
        if not p.is_absolute() or p.is_symlink() or p.resolve() != p:
            raise ValueError('WEB_DATA_DIR must be an absolute non-symlink path')
        repo = Path(__file__).resolve().parents[2]
        if p == repo or repo in p.parents:
            raise ValueError('WEB_DATA_DIR must be outside the repository')
        if u.scheme not in ('https', 'http') or not u.hostname or u.path or u.query or u.fragment or u.username or '*' in self.origin:
            raise ValueError('WEB_ORIGIN must be an exact origin')
        if u.scheme != 'https' and u.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('HTTPS required outside loopback')
        if not 1 <= self.session_seconds <= 86400 or not 1024 <= self.upload_limit <= 128*1024*1024:
            raise ValueError('Invalid web limits')

    @classmethod
    def env(cls):
        return cls(Path(os.environ['WEB_DATA_DIR']), os.environ.get('WEB_ORIGIN','https://audiobooks.io.vn'),
                   int(os.environ.get('WEB_SESSION_SECONDS','28800')), int(os.environ.get('WEB_UPLOAD_LIMIT','67108864')))


@contextmanager
def auth_db(settings):
    c = sqlite3.connect(settings.data_dir/'web-auth.sqlite3', timeout=10)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def bootstrap(settings, username, password):
    if not username.strip() or len(username)>80 or len(password)<16 or len(password)>256:
        raise ValueError('Use a username and a password of 16–256 characters')
    root = settings.data_dir
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_mode & 0o077:
        raise ValueError('WEB_DATA_DIR must have mode 0700')
    for name in ('web-instance.json', 'web-pipeline.sqlite3', 'web-auth.sqlite3', 'uploads'):
        candidate = root / name
        if candidate.is_symlink() or (candidate.is_file() and candidate.stat().st_nlink != 1):
            raise ValueError('Links forbidden in web data')
    marker = root/'web-instance.json'
    dbpath = root/'web-pipeline.sqlite3'
    if marker.exists() and json.loads(marker.read_text()) != {'format':'du-web-v1'}:
        raise ValueError('Invalid web instance marker')
    if not marker.exists() and (dbpath.exists() or (root/'web-auth.sqlite3').exists()):
        raise ValueError('Refusing to adopt existing databases')
    if not marker.exists():
        with marker.open('x') as f:
            json.dump({'format':'du-web-v1'}, f)
        db = Database(dbpath); db.close()
    (root/'uploads').mkdir(mode=0o700, exist_ok=True)
    with auth_db(settings) as c:
        c.executescript('CREATE TABLE IF NOT EXISTS owner(username TEXT PRIMARY KEY,salt TEXT NOT NULL,hash TEXT NOT NULL); CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL); CREATE TABLE IF NOT EXISTS throttle(key TEXT PRIMARY KEY,failures INTEGER NOT NULL,until REAL NOT NULL);')
        salt = secrets.token_hex(16)
        c.execute('DELETE FROM owner')
        c.execute('INSERT INTO owner VALUES(?,?,?)',(username,salt,password_hash(password,salt)))
        c.execute('DELETE FROM sessions')
        c.execute('DELETE FROM throttle')
    for p in (marker, dbpath, root/'web-auth.sqlite3'):
        p.chmod(0o600)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

class Login(StrictModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=256)

class ProjectInput(StrictModel):
    name: str = Field(min_length=1,max_length=160)
    min_scenes: int = Field(default=50,ge=1,le=360)
    max_scenes: int = Field(default=360,ge=1,le=360)

class Review(StrictModel):
    decision: str

class Note(StrictModel):
    note: str = Field(default='',max_length=2000)


def create_app(settings=None):
    settings = settings or Settings.env()
    root = settings.data_dir
    if not (root/'web-instance.json').is_file() or json.loads((root/'web-instance.json').read_text()) != {'format':'du-web-v1'}:
        raise RuntimeError('Bootstrap the dedicated web directory first')
    if root.stat().st_mode & 0o077:
        raise RuntimeError('WEB_DATA_DIR must have mode 0700')
    for name in ('web-auth.sqlite3','web-pipeline.sqlite3','uploads'):
        if (root/name).is_symlink():
            raise RuntimeError('Symlinks forbidden in web data')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    cookie = 'du_session'

    @contextmanager
    def pipeline():
        db = Database.open_existing(root/'web-pipeline.sqlite3')
        try:
            yield Pipeline(db)
        finally:
            db.close()

    def session(request):
        token = request.cookies.get(cookie,'')
        with auth_db(settings) as c:
            r = c.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            owner = c.execute('SELECT username FROM owner').fetchone()
        if not r or not owner:
            raise HTTPException(401,'Vui lòng đăng nhập lại.')
        return dict(r), owner['username']

    @app.middleware('http')
    async def security(request, call_next):
        try:
            if request.url.path.startswith('/api/'):
                if request.query_params:
                    raise HTTPException(422,'Tham số URL không được hỗ trợ.')
                if request.url.path != '/api/login':
                    current,_ = session(request)
                if request.method not in ('GET','HEAD','OPTIONS'):
                    if request.headers.get('origin') != settings.origin:
                        raise HTTPException(403,'Nguồn yêu cầu không hợp lệ.')
                    if request.url.path != '/api/login' and not hmac.compare_digest(request.headers.get('x-csrf-token',''),current['csrf']):
                        raise HTTPException(403,'Phiên xác thực yêu cầu không hợp lệ.')
                    limit = settings.upload_limit if '/uploads/' in request.url.path else 16384
                    try:
                        if int(request.headers.get('content-length','0')) > limit:
                            raise HTTPException(413,'Tệp hoặc yêu cầu vượt giới hạn dung lượng.')
                    except ValueError:
                        raise HTTPException(400,'Yêu cầu không hợp lệ.')
                    # Read before BaseHTTPMiddleware's task boundary: exceptions
                    # from a wrapped receive otherwise become an ExceptionGroup.
                    received = 0
                    chunks = []
                    async for chunk in request.stream():
                        received += len(chunk)
                        if received > limit:
                            raise HTTPException(413,'Tệp hoặc yêu cầu vượt giới hạn dung lượng.')
                        chunks.append(chunk)
                    request._body = b''.join(chunks)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse({'detail':exc.detail},status_code=exc.status_code)
        except Exception:
            response = JSONResponse({'detail':'Không thể xử lý yêu cầu. Vui lòng thử lại hoặc kiểm tra trạng thái dự án.'},status_code=500)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.origin).hostname], www_redirect=False)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail':'Dữ liệu không hợp lệ. Kiểm tra trường bắt buộc, định dạng và giới hạn.'},status_code=422)

    async def domain_error(request, exc):
        return JSONResponse({'detail':'Thao tác bị từ chối: kiểm tra trạng thái hoạt động, đầu vào WAV/SRT, phạm vi cảnh và bằng chứng duyệt hiện tại.'},status_code=409)
    for exception in (ValueError, PermissionError, RuntimeError, OSError):
        app.add_exception_handler(exception, domain_error)

    @app.post('/api/login')
    async def login(body: Login, request: Request):
        now = time.time()
        with auth_db(settings) as c:
            c.execute('BEGIN IMMEDIATE')
            t = c.execute("SELECT * FROM throttle WHERE key='owner'").fetchone()
            if t and t['until']>now and t['failures']>=5:
                return JSONResponse({'detail':'Quá nhiều lần đăng nhập. Thử lại sau 5 phút.'},status_code=429)
            owner = c.execute('SELECT * FROM owner').fetchone()
            correct = hmac.compare_digest(password_hash(body.password,owner['salt']),owner['hash'])
            if not correct or not hmac.compare_digest(body.username.encode('utf-8'),owner['username'].encode('utf-8')):
                failures = t['failures']+1 if t and t['until']>now else 1
                c.execute("INSERT OR REPLACE INTO throttle VALUES('owner',?,?)",(failures,now+300))
                return JSONResponse({'detail':'Thông tin đăng nhập không đúng.'},status_code=429 if failures>=5 else 401)
            c.execute('DELETE FROM throttle')
            c.execute('DELETE FROM sessions WHERE expires<=?',(now,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),csrf,now+settings.session_seconds))
        response = JSONResponse({'username':owner['username'],'role':'OWNER','csrf':csrf})
        response.set_cookie(cookie,token,httponly=True,secure=settings.origin.startswith('https:'),samesite='strict',max_age=settings.session_seconds,path='/')
        return response

    @app.get('/api/session')
    async def me(request: Request):
        current, owner = session(request)
        return {'username':owner,'role':'OWNER','csrf':current['csrf']}

    @app.post('/api/logout')
    async def logout(request: Request):
        current,_ = session(request)
        with auth_db(settings) as c:
            c.execute('DELETE FROM sessions WHERE token=?',(current['token'],))
        response = JSONResponse({'ok':True}); response.delete_cookie(cookie,path='/')
        return response

    @app.get('/api/projects')
    async def projects():
        with pipeline() as p:
            return {'projects':[dict(r) for r in p.db.all('SELECT id,name,state,created_at,version FROM projects ORDER BY created_at DESC LIMIT 200')]}

    @app.post('/api/projects')
    async def create(body: ProjectInput):
        with pipeline() as p:
            return {'id':p.init_project(body.name,scene_range=(body.min_scenes,body.max_scenes),role=Role.OWNER)}

    def require_project(p,pid):
        if not p.db.one('SELECT id FROM projects WHERE id=?',(pid,)):
            raise HTTPException(404,'Không tìm thấy dự án.')

    @app.get('/api/projects/{pid}')
    async def detail(pid: str):
        with pipeline() as p:
            require_project(p,pid)
            summary = p.status_summary(pid,role=Role.OWNER)
            # No filesystem paths, raw provider errors or untrusted snapshot payloads.
            scenes = [dict(r) for r in p.db.all('SELECT id,code,ord,start_ms,end_ms,text,state,approval_state,qa_state FROM scenes WHERE project_id=? ORDER BY ord',(pid,))]
            artifacts = [dict(r) for r in p.db.all('SELECT id,scene_id,kind,sha256,version,status,created_at FROM artifacts WHERE project_id=? ORDER BY id DESC LIMIT 1000',(pid,))]
            audio = p.db.one('SELECT duration_ms,sha256 FROM audio WHERE project_id=?',(pid,))
            history = [dict(r) for r in p.db.all('SELECT id,type,created_at FROM events WHERE project_id=? ORDER BY id DESC LIMIT 100',(pid,))]
            script = p.db.one("SELECT value_json FROM learning_metadata WHERE project_id=? AND key='normalized_input' ORDER BY id DESC LIMIT 1",(pid,))
            jobs = [dict(r) for r in p.db.all('SELECT id,kind,state,updated_at FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 100',(pid,))]
            summary['project']['blocked_reason'] = 'Đầu vào hoặc bằng chứng chưa đáp ứng điều kiện của pipeline.' if summary['project']['blocked_reason'] else None
            actions = ['resume'] if summary['project']['state']=='PAUSED' else ['pause','checkpoint'] if summary['project']['state']=='ACTIVE' else []
            if summary['project']['state']=='ACTIVE' and audio and p.db.one('SELECT id FROM cues WHERE project_id=? LIMIT 1',(pid,)):
                actions.append('plan')
            return {**summary,'scenes':scenes,'artifacts':artifacts,'audio':dict(audio) if audio else None,'history':history,'jobs':jobs,'script':json.loads(script['value_json'])['document']['text'] if script else None,'next_actions':actions,'limits':{'ai_generation':False,'precision_reveal':False,'assembly':False,'upload_bytes':settings.upload_limit}}

    @app.post('/api/projects/{pid}/uploads/{kind}')
    async def upload(pid: str, kind: str, request: Request):
        if kind not in ('script','audio','subtitle'):
            raise HTTPException(404,'Loại tệp chưa được hỗ trợ.')
        with pipeline() as p:
            require_project(p,pid)
            suffix = {'script':'.txt','audio':'.wav','subtitle':'.srt'}[kind]
            fd, name = tempfile.mkstemp(dir=root/'uploads',suffix=suffix)
            try:
                count = 0
                with os.fdopen(fd,'wb') as f:
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > (settings.upload_limit if kind=='audio' else min(settings.upload_limit,1024*1024)):
                            raise HTTPException(413,'Tệp vượt giới hạn dung lượng.')
                        f.write(chunk)
                if not count:
                    raise ValueError('empty upload')
                if kind == 'audio':
                    result = p.import_narration(pid,Path(name),role=Role.OWNER)
                    return {k:v for k,v in result.items() if k!='uri'}
                if kind == 'script':
                    p.import_document(pid,'text',Path(name).read_text(encoding='utf-8-sig'),role=Role.OWNER)
                else:
                    p.import_srt(pid,srt(name),role=Role.OWNER)
                return {'ok':True}
            finally:
                Path(name).unlink(missing_ok=True)

    @app.post('/api/projects/{pid}/actions/{action}')
    async def action(pid: str, action: str, body: Note = Note()):
        with pipeline() as p:
            require_project(p,pid)
            if action == 'plan': p.plan_scenes(pid,role=Role.OWNER)
            elif action == 'pause': p.pause(pid,role=Role.OWNER)
            elif action == 'resume': p.resume(pid,role=Role.OWNER)
            elif action == 'checkpoint': p.checkpoint(pid,'web',{'note':body.note},role=Role.OWNER)
            else: raise HTTPException(404,'Thao tác chưa được hỗ trợ trên web.')
            return {'ok':True}

    @app.post('/api/projects/{pid}/scenes/{sid}/review')
    async def review(pid: str, sid: int, body: Review, request: Request):
        if body.decision not in ('APPROVED','REJECTED'):
            raise HTTPException(422,'Quyết định không hợp lệ.')
        with pipeline() as p:
            require_project(p,pid)
            scene = p.db.one('SELECT code FROM scenes WHERE id=? AND project_id=?',(sid,pid))
            if not scene: raise HTTPException(404,'Không tìm thấy cảnh.')
            _,owner = session(request)
            p.decide_scene(pid,scene['code'],body.decision,'web:'+owner,role=Role.OWNER)
            return {'ok':True}

    @app.get('/api/artifacts/{aid}')
    async def media(aid: int):
        with pipeline() as p:
            row = p.db.one("SELECT * FROM artifacts WHERE id=? AND status='ACTIVE'",(aid,))
            if not row: raise HTTPException(404,'Không tìm thấy tệp đang hoạt động.')
            managed = root/'artifacts'/row['project_id']
            path = Path(row['uri'])
            try:
                rel = path.relative_to(managed)
                dfd = open_dir_beneath(managed,rel.parent)
                try: fd = os.open(rel.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=dfd)
                finally: os.close(dfd)
                with os.fdopen(fd,'rb') as f:
                    st = os.fstat(f.fileno())
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_size>settings.upload_limit:
                        raise ValueError('unsafe media')
                    data = f.read(settings.upload_limit+1)
                if hashlib.sha256(data).hexdigest()!=row['sha256']:
                    raise ValueError('changed artifact')
            except (ValueError,OSError):
                raise HTTPException(409,'Tệp thiếu, bị thay đổi hoặc không thuộc vùng dữ liệu quản lý.')
            ext = path.suffix.lower()
            mime = {'.wav':'audio/wav','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp','.mp4':'video/mp4'}.get(ext,'application/octet-stream')
            # Other types are attachment-only; never render uploaded HTML/SVG.
            disposition = 'inline' if mime!='application/octet-stream' else 'attachment'
            safe_ext = ext if ext in ('.wav','.png','.jpg','.jpeg','.webp','.mp4') else '.bin'
            return Response(data,media_type=mime,headers={'Content-Disposition':f'{disposition}; filename="artifact-{aid}{safe_ext}"'})

    dist = Path(__file__).resolve().parents[2]/'web'/'dist'
    if dist.is_dir():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
        @app.get('/')
        async def index():
            return FileResponse(dist/'index.html')
    return app


def main():
    parser = argparse.ArgumentParser(description='Dedicated single-owner web bootstrap; never opens CLI production DB.')
    parser.add_argument('command',choices=['bootstrap'])
    parser.add_argument('--username',required=True)
    args = parser.parse_args()
    password = getpass.getpass('Mật khẩu OWNER (ít nhất 16 ký tự): ')
    if password != getpass.getpass('Nhập lại mật khẩu: '):
        raise SystemExit('Mật khẩu không khớp.')
    bootstrap(Settings.env(),args.username,password)
    print('Đã lưu mật khẩu băm và thu hồi phiên cũ. Không có mật khẩu mặc định.')

if __name__ == '__main__':
    main()
