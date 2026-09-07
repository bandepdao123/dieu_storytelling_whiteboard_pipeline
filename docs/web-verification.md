# Web MVP verification record

Performed against the source checkout, isolated temporary databases, no deployment or real credentials.

- Initial inherited web tests: 4 passed.
- Red/green regressions: non-ASCII usernames returned 500; bootstrap followed a linked instance marker; chunked oversized uploads returned 500 due to middleware exception grouping. Each reproduced before fixing.
- Final targeted command: `PYTHONPATH=src python -m pytest tests/test_web_app.py -q` — 8 passed in 2.76s. Covers real WAV/SRT planning, checkpoints, pause/resume, review evidence refusal, authenticated media, host/origin/CSRF, throttling, logout/expiry, upload bounds with and without Content-Length, temporary cleanup, sanitized malformed input and linked bootstrap rejection.
- Full core plus web command: `PYTHONPATH=src python -m pytest -q` — 946 passed in 161.01s. This run collected six web tests before the last two boundary tests were added; those final two are included in the separate eight-test passing run above. Original 940 core tests retained, no core modifications.
- A first full run without PYTHONPATH failed 57 tests because spawned processes loaded an unrelated installed core. Inspection confirmed its `Pipeline` lacked `status_summary`; explicit checkout PYTHONPATH resolved all failures.
- `npm install` generated lockfile: audit reported 0 vulnerabilities. `npm run build` passed TypeScript and Vite after completing the missing stylesheet; generated frontend bundle approximately 212 kB JS and 8.4 kB CSS before gzip.
- Actual isolated loopback uvicorn smoke: root HTTP 200, browser login, empty dashboard, creation of a Vietnamese-named project, real detail/input screen.
- Browser DOM checks at 1366×900 and 390×844 showed no horizontal document overflow; project input detail also checked at mobile width. Chromium automation only, not Safari. Screenshots saved outside checkout at `/tmp/du-web-mvp-desktop.png` and `/tmp/du-web-mvp-mobile.png`; captured but not visually inspected with an image tool.
- Browser closed and disposable smoke server stopped after verification. No nginx/systemd edits, no git commit/push, no deployment.

Remaining manual QA: full browser file upload/media playback (API equivalents pass), long-lived session expiry UI, Safari, keyboard-only tabs and assistive technology. Backend endpoints are synchronous and bounded but not production worker infrastructure. See `web-app.md` for operational constraints and exact startup commands.
