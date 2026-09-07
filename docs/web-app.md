# Audiobooks Studio — single-owner web MVP

This is a source-checkout web adapter over the existing core; it does not replace the CLI or adopt its databases. The approved deployment now serves https://audiobooks.io.vn from an isolated copy at `/opt/audiobooks-studio/app`; see `web-deployment.md` for production paths and verification.

## Implemented scope

Vietnamese responsive React UI: owner login/logout, project list/create/detail, TXT script upload, measured PCM WAV narration, SRT import, real scene planning, pause/resume, checkpoint, core-governed scene review, event/job status and authenticated artifact downloads/audio playback. All mutations call `Pipeline` with OWNER authorization. Approval still requires current core QA/evidence; it is not a bypass.

No AI provider calls, background worker, image generation, TTS, video assembly, precision reveal, project deletion, multi-user accounts or durable job orchestration. Resume changes core state only. Only one owner is supported. Do not describe this as an end-to-end video generator.

## Install and build

Python 3.11+ and Node/npm are required. Run from this checkout (the frontend build is not bundled into a Python wheel):

```bash
cd /home/hermes/work/dieu_storytelling_whiteboard_pipeline
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev,web]'
npm --prefix web ci
npm --prefix web run build
```

## Bootstrap / password rotation

Use a NEW dedicated directory outside the checkout. Do not point this at a CLI database or another application's data. Parent directories must not be symlinks; data root must have mode 0700. Run as the same unprivileged Unix user that will run the server.

```bash
export WEB_DATA_DIR="$HOME/.local/share/audiobooks-studio"
export WEB_ORIGIN='https://audiobooks.io.vn'
umask 077
mkdir -p "$WEB_DATA_DIR"
chmod 700 "$WEB_DATA_DIR"
.venv/bin/python -m du_pipeline.web bootstrap --username owner
```

Password is prompted twice, never placed in arguments, files or documentation. Minimum 16, maximum 256 characters. No default credentials. Rerunning bootstrap against this existing web instance rotates the owner/password and revokes every session; stop the server first. Back up the dedicated directory before rotation. Bootstrap refuses to adopt existing unmarked databases.

## Start

```bash
.venv/bin/python -m uvicorn du_pipeline.web:create_app --factory \
  --host 127.0.0.1 --port 8765 --workers 1 \
  --limit-concurrency 8 --timeout-keep-alive 5 --no-proxy-headers
```

`WEB_DATA_DIR` and `WEB_ORIGIN` must be exported in this process. Nginx/systemd/TLS/real credentials are separate operator work. Preserve `Host: audiobooks.io.vn` and the browser Origin, proxy `/` and `/api/` to this loopback listener, and never alias the data directory. Configure proxy request body limits, body/read timeouts, connection/rate limits and HTTPS/HSTS. Do not expose uvicorn directly. Build `web/dist` before starting/restarting; `/` and static assets are only mounted if the build exists at app creation.

For local-only browser testing use `WEB_ORIGIN=http://127.0.0.1:8765`, bootstrap a separate disposable directory, then visit that exact URL. HTTP is refused for non-loopback origins. Vite's standalone dev server is not an authenticated API proxy; use the built UI served by uvicorn.

## Security and operating limits

- All `/api/` data and media routes require a server-side session except login. Public `/` and `/assets` contain only application/login code, never project data.
- Scrypt salted password hashes; random session secrets stored hashed in SQLite; HttpOnly, SameSite=Strict cookies, Secure under HTTPS; default absolute expiry eight hours (no sliding refresh).
- Exact Origin required on every mutation including login. Authenticated mutations additionally require `X-CSRF-Token` returned by login/session. Exact configured hostname validation; no CORS.
- Shared owner login throttle: five failures block login for five minutes (an attacker can temporarily lock out the owner; add edge rate limits).
- JSON bodies capped at 16 KiB. Default upload ceiling 64 MiB, adjustable by `WEB_UPLOAD_LIMIT` from 1024 to 134217728 bytes. TXT/SRT additionally capped at 1 MiB. Body bounds also apply without Content-Length. Bodies are buffered within the cap; concurrency must remain limited. Files are temporary random server names, never supplied paths, and removed after processing.
- Data/media error responses are sanitized. CSP, no-store, nosniff and no-referrer are applied. Downloads only serve ACTIVE managed artifacts via no-follow directory traversal, regular-file/single-link checks and SHA-256 verification. Unknown formats download as `.bin`, not inline HTML/SVG. Media is buffered, without streaming/range optimization.
- This is a synchronous, single-owner MVP, not a scalable worker service. Per-request size bounds do not provide a total disk quota; configure filesystem quota/monitoring and backup retention. History is capped at 100, artifacts 1000, projects 200; no pagination yet. Large core status queries may still be expensive after prolonged use.
- Dedicated data directory is a trusted OS boundary: no untrusted local writer may share the service account. Stop server before filesystem maintenance; back up the entire data root while stopped, restore to the same absolute path because artifact references are absolute.

## Verification

```bash
cd /home/hermes/work/dieu_storytelling_whiteboard_pipeline
PYTHONPATH=src .venv/bin/python -m pytest tests/test_web_app.py -q
PYTHONPATH=src .venv/bin/python -m pytest -q
npm --prefix web run build
```

Explicit `PYTHONPATH=src` is important in environments with another installed `du_pipeline`: core tests spawn subprocesses that otherwise may import that unrelated version. Optional web tests skip when web dependencies are not installed, keeping core-only installs supported. Tests use disposable temp directories and nonproduction credentials.

Manual acceptance: login; create a short project with scene range 1–2; import matching TXT/WAV/SRT; plan; inspect scenes, media and history; pause/resume/checkpoint; logout and confirm direct artifact URL returns 401. Check desktop and mobile widths, errors, empty states and expired sessions. Unsupported AI/video capabilities must stay explicitly labeled unavailable.
