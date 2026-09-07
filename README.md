# Dieu Storytelling Whiteboard Pipeline

Local SQLite orchestration and real FFmpeg final assembly for storytelling media.
This is an uncommitted Phase2 review candidate, NOT full production acceptance or
an autonomous whiteboard generator. See [capabilities](docs/capabilities.md),
[inspection](docs/cli-inspection.md), and [assembly contract](docs/final-assembly.md).

## Local media CLI continuation

See [executable real WAV/PPM CLI workflow](docs/cli-media-workflow.md) for typed
artifact/attempt/scene-QA/POST_BATCH/final-QA/final-review commands and scene-range
flags. QA is caller-supplied external evidence, not an invented AI evaluation.
Strict inspection remains default; explicit --operational-wal allows SQLite
sidecar writes without checkpoint/journal conversion. The historical omitted-CLI
inventory below is superseded by that workflow and the capability matrix.

## Installation

From the repository root, Python >=3.11:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m pip install pytest
python -m pytest -q
```

Core runtime is standard-library Python. Real assembly additionally needs local
FFmpeg/ffprobe and Linux no-replace filesystem support. Optional local DOCX/XLSX
extraction needs python-docx/openpyxl. Network document inputs need an explicitly
supplied adapter; the CLI does not supply one. No provider is invoked by planning.
Default image request remains Codex OAuth, gpt-image-2, high, without fallback.

## Executable temporary inspection quickstart

This example deliberately creates only a disposable metadata project and an
OFFLINE inspection copy. It does not generate media, measure model usage, or
pretend missing QA/approvals exist. Run in an installed environment:

```sh
python - <<'PY'
import json, pathlib, sqlite3, subprocess, tempfile
root = pathlib.Path(tempfile.mkdtemp(prefix='du-cli-demo-'))
db = root / 'demo.db'
def cli(path, *args):
    result = subprocess.run(['du-pipeline', '--db', str(path), *args],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)
pid = cli(db, 'init', 'Demo', '--language', 'vi')['project_id']
copy = root / 'inspection.db'
# Explicit backup creation is writable setup, NOT part of inspection.
source = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
target = sqlite3.connect(copy)
try:
    source.backup(target)
    target.execute('PRAGMA journal_mode=DELETE')
finally:
    target.close()
    source.close()
print(cli(copy, 'status-summary', pid))
print(cli(copy, 'report', pid))
print(root)
PY
```

The backup step may access WAL shared memory; use it only as an explicitly
writable maintenance/setup operation, never as a claim of zero-write live polling.
Strict inspection itself never initializes/migrates, changes journal mode, creates
artifact directories or reserves a database writer. WAL databases are currently
rejected, including closed WAL databases; use an offline rollback-journal backup.
This is an explicit live-monitoring limitation, not a transparent snapshot feature.

## Local workflow commands and limits

`--db` and `--role OWNER|REVIEWER|OPERATOR` precede the command. This is a trusted
local OS/DB-access boundary; choosing a role is not remote authentication.

- `init NAME [--language vi|en] [--seed N] [--style JSON] [--references JSON]`
  initializes/migrates and creates a project. Default scene range is 50–360;
  custom scene range is currently a Python API option, not a CLI flag.
- `migrate` explicitly upgrades an existing DB. Back up first. Normal writable
  commands require existing current schema and do not auto-migrate.
- `import-audio PROJECT_ID URI DURATION_MS SHA256` records supplied metadata.
  It does not copy narration, probe duration, or calculate its checksum. Put real
  narration under the managed project root and measure its actual bytes/duration
  externally; do not use placeholder hashes. Managed root is in project status.
- `import-srt PROJECT_ID PATH` parses timing; `import-script PROJECT_ID KIND SOURCE`
  normalizes text/docx/excel inputs or requires an adapter for gdocs/url. Document
  import is not semantic scene planning or prompt generation.
- `plan PROJECT_ID` uses audio/SRT cues and duration policy. It is not a semantic
  script segmenter; subdivisions can repeat cue text. Identical plan replay is
  not promised to be a no-op: replan invalidates/version-fences derived evidence.
- `approve PROJECT_ID SCENE_CODE --actor NAME` and `reject ...` submit human
  scene decisions. Approval requires current subject-bound QA; not an AI invocation.
- `pause PROJECT_ID`, `resume PROJECT_ID`, `retry SCENE_DATABASE_ID` manage metadata
  lifecycle/jobs; queued does not mean an actual production worker is running.
- `status`, `status-summary`, `report` take PROJECT_ID. `discord-status` takes
  MESSAGE_ID. All inspection and `--dry-run` routes use the strict open above.
- `assemble PROJECT_ID OUTPUT_PATH [--dry-run]` (alias `assemble-final`) requires
  managed, verified inputs and current scene/POST_BATCH evidence. OUTPUT_PATH is
  positional, not `--output`. Real assembly normalizes images/clips and muxes audio.
- `recover-publications` mutates recorded publication recovery state. Unknown
  files/manual-review rows are not automatically cleaned or repaired.

There is NOT yet a pure CLI init-to-reviewed-real-MP4 workflow. Missing explicit
CLI paths: safe measured narration ingestion, scene-range configuration, image
result registration, scene typed QA import, POST_BATCH contact/QA review, final
QA import/final review, checkpoint restore and rerun proposal lifecycle. These
have public Python service paths where noted in the capability matrix; do not
insert approvals via SQL to bypass the missing commands. Public-API real-media
E2E tests exercise assembly without SQL approval shortcuts. No new CLI media E2E
is claimed by the metadata quickstart.

## Safety and compatibility

Local output is fixed 1920×1080/30fps H.264/yuv420p MP4, AAC narration, hard cuts.
IMAGE renders as a whole-scene still, NOT an exact pen-tip reveal. No synthesized
hand trajectory, AI QA, contact-sheet synthesis, music/subtitle/logo generation or
custom hold is implemented. Default hold=1.5 is legacy metadata only.

SRT import tolerance is not assembly readiness. Planning requires exact continuous
0-to-narration coverage and positive cumulative rounded 30fps scene allocation.
Gaps/tails reject rather than stretch stills or alter narration/subtitles. See the
assembly document for quantization, output verification and recovery details.

Copy-producing artifact registration/contact review reject unsupported caller-owned
transaction nesting. Keyed Discord scene replacement is supported through the
durable claim/copy/finalize path, with attempt/lease fencing and atomic result.
Expired/failed claims and unknown crash residuals require manual review, not blind
retry or sweeping. See docs/r2-command-copy.md for exact support and limits.

Schema is v15 (artifact identity/checksum preflight, detached history provenance,
durable command-copy claims);
invalid/ambiguous legacy rows require manual review on a backup, not normalization.
Checkpoint snapshot v4 binds capture/plan identity. Existing scene-bearing v3 fails
closed by default; explicit OWNER recover-legacy-content supports only matching
canonical content revalidation as a NEW generation, not historical source recovery.
See docs/legacy-content-recovery.md for opt-in, receipts and decision boundaries.
Empty-scene v3 retains ordinary compatibility. Historical snapshots
are not rewritten. Pre-v7 Discord PROCESSING becomes MANUAL_REVIEW, not automatic
retry. See the phase2 plan for limits and historical migration fixture coverage.

F15 retention remains unresolved: FINAL_VIDEO TTL differs from protected legacy
FINAL. No retention, license, provider settings or delivery policy is changed.
Historical audits/plans retain their original evidence and are not current
acceptance statements. No measured token savings, live provider usage, full
production readiness or hardware power-loss certification is claimed.
