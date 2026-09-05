# Phase 1 live integrations

All adapters are SDK-neutral and accept injected clients, so tests and local dry runs never
make network calls. SQLite remains the source of truth. No token content is read by validation
and row payloads are never logged.

## Activation

1. Put a **user OAuth** token outside this repository and set `DU_GOOGLE_OAUTH_TOKEN_PATH`.
   The file must exist and be readable. Authentication is never initiated automatically.
2. Optionally set `DU_GOOGLE_DRIVE_ROOT_ID` (the documented production root is the default).
3. Install a deployment plugin that supplies the Sheets/Drive client methods documented in
   `du_pipeline.integrations`; the core intentionally does not bind to a Google SDK.
4. Set `DU_DISCORD_ALLOWLIST_JSON` to a JSON object mapping user IDs to `OWNER`, `REVIEWER`,
   or `OPERATOR`. The bridge has no Discord SDK dependency and can be called by Hermes gateway.

Commands: `sync-sheet`, `ingest-sheet-commands`, `upload-drive`, `discord-dispatch`, and
`discord-status`. Mutation/network commands support `--dry-run`.

Sheets clients implement `find_spreadsheet`, `create_spreadsheet`, `get_tabs`, `add_tabs`,
`batch_upsert`, `read_rows`. Drive clients implement `ensure_folder`, `begin_upload` (returning
session and resume offset), `upload_chunk`, `finish_upload`, and `metadata`.