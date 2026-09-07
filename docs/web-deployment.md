# Approved production cutover — 2026-09-07

Live URL: https://audiobooks.io.vn

## Isolation and paths

- Runtime snapshot: `/opt/audiobooks-studio/app` (root-owned, copied `src`, `web/dist`, `pyproject.toml`; not editable install).
- Dedicated Python 3.12 environment: `/opt/audiobooks-studio/venv`.
- Unit: `/etc/systemd/system/audiobooks-studio.service`; enabled and active.
- Unix account: `audiobooks-studio`, no login shell.
- Environment: `/etc/audiobooks-studio/runtime.env`, root-only. No provider secrets.
- Data: `/var/lib/audiobooks-studio`, mode 0700, dedicated service owner. New `web-auth.sqlite3` and `web-pipeline.sqlite3`; no existing DB adopted.
- Bootstrap credential file: `/etc/audiobooks-studio/bootstrap-credentials.json`, root-owned 0600 in a 0700 directory. Private handoff only; never paste content into group chat. Bootstrap generated a random password, not a default.
- Listener: `127.0.0.1:4340`, single worker, concurrency 8, keepalive 5s, no access logging.
- Hardening: strict read-only filesystem except dedicated data, protected home/kernel/control groups, private tmp/devices, no capabilities/new privileges, restricted address families, 1 GiB memory and 64-task limits, umask 0077.
- Nginx edited only `/etc/nginx/sites-available/audiobooks.io.vn`; original enabled symlink remains.
- Exact prior file: `/etc/audiobooks-studio/nginx.before`; original symlink copy: `/etc/audiobooks-studio/nginx-enabled.before`.
- Existing Let's Encrypt certificate/key retained unchanged. Edge: 64 MiB body limit, body timeout 30s, upstream connect 5s/read 120s/send 60s, 10 requests/s with burst 40 and 12 connections per client, HSTS.
- Old Flow Prompt Studio code/data untouched at `/home/hermes/.hermes/profiles/trumpcreative/workspace/flow-prompt-studio`; original Node PID 2015698 still listening on port 4177 after cutover.

## Verification evidence

- Development web tests: **8 passed in 2.70s**.
- Actual deployment Python environment web tests: **8 passed in 3.05s**, two upstream Starlette deprecation warnings. Full suite is parent's separate final gate.
- TypeScript/Vite build passed, 29 modules, JS 212.77 kB and CSS 8.84 kB.
- `nginx -t` passed before reload; unit active; loopback 4340 and preserved old 4177 listener verified.
- HTTPS root 200; deployed asset bodies exactly equal runtime build bytes:
  - `/assets/index-nIkjs8Nq.js`: SHA-256 `0e168a0c756f497765eecff04718b53d01ff3762b942df9544d87b4d07fa528f`
  - `/assets/index-t8JL1M_3.css`: SHA-256 `1da4bff0f63260a140637769441e62f743fce1f778043193724cc5043e9a7860`
- Anonymous `/api/projects`, `/api/session`, `/api/artifacts/1`: **401** each.
- Private credential login: **200**, Secure/HttpOnly/SameSite=Strict cookie. Authenticated session owner matched credential file, projects **200**, zero projects (no test production project created).
- Mutation without CSRF: **403**. Logout succeeded; media after logout **401**.
- Report: `/tmp/audiobooks-production-verification.json`.
- Playwright Chromium live HTTPS login, dashboard and populated unsaved creation form at 1366×900 and 390×900: zero page errors; document widths exactly 1366 and 390; dashboard clipped-important-label count zero.
- Browser report: `/tmp/audiobooks-browser-qa.json`.
- Screenshots: `/tmp/audiobooks-{login,dashboard,create}-{1366,390}.png`.

## UI improvements

Natural Vietnamese workspace/owner/status labels, clearer capability boundaries and next-step guidance, robust Vietnamese network and non-JSON edge error handling, global loading notice, artifact status translation, wrapping and shrink-safe controls, readable stacked mobile metrics. No AI, assembly, reveal or worker capability fabricated.

## Maintenance and rollback

Build and test the working checkout, copy source and built frontend together to runtime, restart only `audiobooks-studio`, and repeat authenticated plus HTTPS asset checks. Do not assume editing the checkout changes deployment. Freeze deployed packages in `/etc/audiobooks-studio/requirements.freeze.txt`.

Rollback domain routing: copy `nginx.before` back onto `/etc/nginx/sites-available/audiobooks.io.vn`, run `sudo nginx -t`, then reload nginx. The original port-4177 process remains available. Preserve new data even during rollback.

## Limitations

Chromium screenshots captured and DOM checked, not visually inspected with an image tool; Safari, assistive technology and complete production browser upload/playback not tested. Actual TXT/WAV/SRT planning, media, pause/resume/checkpoint and review refusal are exercised by isolated automated tests. No project/scene smoke data was added to production. No scheduled backups/disk quota yet. New production dependencies were resolved from optional version ranges; deployed-environment tests pass but upstream deprecation warnings remain. System Python lacked ensurepip: used `uv pip --python` to populate its isolated venv, without modifying global packages. No commit or push performed.
