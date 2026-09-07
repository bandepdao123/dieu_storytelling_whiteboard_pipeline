# Strict CLI inspection contract — bounded F18 candidate

## Local workflow continuation (supersedes deferred inventory below)
Explicit global `--operational-wal` now opts inspection into mode=ro/query_only WAL
reading. SQLite may create WAL/SHM sidecars or write shared-memory reader state:
this mode is NOT zero-filesystem-mutation inspection. No immutable=1, automatic
checkpoint, journal conversion, creation of a missing DB or migration is added.
Default strict offline behavior below is unchanged. Old/future schemas still fail.
The flag affects inspection only, not write-command semantics. Stable path/format
preconditions remain. A zero-write live WAL backend remains deferred.
Typed media commands and real-media CLI acceptance: [cli-media-workflow.md](cli-media-workflow.md).
Measured PCM narration ingest, checkpoint/restore/rerun and durable keyed Discord
copy are implemented; see cli-media-workflow.md and r2-command-copy.md. Explicit
OWNER v3 content revalidation limits: legacy-content-recovery.md.

Inspection: status, status-summary, report, discord-status and all current
--dry-run routes. Uses Database.open_existing(path, readonly=True), SQLite URI
mode=ro, query_only, and current schema version checks. No constructor migration,
DDL, journal-mode changes, integrity repair, mkdir, or BEGIN IMMEDIATE. Service
status/status_summary retain their existing deferred snapshot and role forwarding.
Database is context-managed on dispatch success, expected errors and BaseException.
Partial open failures close their connection. Writable legacy Database(path) remains
an explicit initialization/migration API for Python callers; Pipeline(db) is pure.

## Important limitation

Standard-library SQLite mode=ro is not enough to promise zero filesystem writes
for WAL: it may create WAL/SHM or alter shared-memory reader state. This candidate
rejects a WAL-format database by reading its header before SQLite open. It does
NOT use immutable=1 on a live database (which would ignore WAL/locking semantics),
checkpoint automatically, convert journal mode or copy state during inspection.
Prepare an offline rollback-journal backup in an explicitly authorized maintenance
step. README contains an executable disposable example. Strict CLI live WAL polling
is omitted. The database path/format must remain stable during open; arbitrary
hostile same-UID replacement or concurrent journal-mode conversion is not certified.
Do not run journal conversion concurrently with inspection.

Missing paths, old/future schema and malformed databases reject. Version validation
is not exhaustive historical schema-signature certification. No migration or repair
occurs in inspection. The current version is 15; use `du-pipeline --db BACKED_UP_DB
migrate` explicitly for upgrades. Invalid legacy identity/checksum/ownership requires
manual review on a backup; do not delete or normalize rows to make a test pass.
`init` is the other explicit initialization/migration boundary. Other writable CLI
commands use mode=rw existing-current-schema open without migration or journal change.

## Error contract

Success JSON goes to stdout, exit 0. Expected parser/input/domain/storage failures
produce one JSON object on stderr and exit 2:

```json
{"error":{"code":"INVALID_REQUEST","message":"..."}}
```

Stable codes: USAGE, DATABASE_UNAVAILABLE, SCHEMA_UNSUPPORTED, NOT_ALLOWED,
INVALID_REQUEST, ASSEMBLY_REJECTED, INTEGRATION_ERROR, BUSY, DATABASE_ERROR, IO_ERROR.
Messages are fixed category guidance, never interpolated exception text, input
content, factory specification, credential path or FFmpeg/provider diagnostics.
DATABASE_UNAVAILABLE also covers missing local input files. --help remains standard
argparse text/exit 0. Unexpected programming exceptions are not relabelled successful
or swallowed; this is an expected-failure contract, not a plugin sandbox.

Roles are forwarded unchanged to existing service methods. Local role selection
is not identity authentication. Existing integration authorization/delivery gaps
are not repaired by the CLI error/open boundary.

## Deferred usecases

Broader narration codecs, zero-write live WAL backend, worker/provider invocation
and automatic manual-review resolution remain deferred. Typed media commands, scene-range
flags and actual real-media CLI E2E are documented in cli-media-workflow.md.
