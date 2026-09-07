# Local final assembly

Current cross-cutting status: capabilities.md and cli-inspection.md supersede
historical remaining-scope lines within the individual milestones below. CLI
dry-run uses strict readonly opening of an offline rollback-journal current-schema
DB; live WAL is rejected, not silently checkpointed. Normal assemble uses an existing
current-schema writable DB without migration. `init`/`migrate` are explicit upgrade
boundaries. Current DB schema is v14; publication receipts were introduced in v13.
Typed image/QA/POST_BATCH/final-review public CLI commands remain omitted; the real
media E2E is public Python API, not a complete CLI quickstart. Copy-producing public
operations reject unsupported transaction nesting; Discord scene-replace therefore
fails closed. Crash/incomplete copies without durable ownership are retained for
manual review. Checkpoint v4 binds capture/plan identity; scene-bearing legacy v3
requires manual review, empty-scene v3 retains compatibility. No history is rewritten.

## F12 publication ownership and durability ordering (uncommitted review candidate)

Schema v13 adds `publication_intents`; the legacy 15-column publication journal is
not rebuilt or reinterpreted. A receipt records token, project, exact source path,
inode identity (device/inode/owner/mode/size/mtime), digest and complete journal
payload. Publication requires a top-level transaction boundary and SQLite
`synchronous=FULL` or `EXTRA`; a weaker connection setting is rejected, not changed.
Dry-run remains read-only and may run inside a caller transaction.

Ordering:
1. Pin no-follow parents; flush the proven regular source file and source directory.
   Persist created publication-directory entries via child/parent directory fsync.
2. Commit INTENT under the shared reconciliation lease before moving any bytes.
3. Assert lease and inode; `RENAME_NOREPLACE` source into the recorded staging leaf.
   Flush staged file, destination directory and source directory before inserting
   PREPARED and changing the receipt to PREPARED in one SQL transaction.
4. Recover only recorded INTENT/PREPARED rows. Validate inode/hash for v13 receipts;
   legacy journal recovery retains its historical hash/size ownership evidence.
   Recheck active version/evidence under the promotion transaction, then no-replace
   promote. Flush final file and both namespace parents before registration.
5. Assert lease, current evidence/version and final inode again; register artifact,
   manifest, event and COMMITTED together. Replay does not duplicate registration.

Each recovery call processes at most 100 INTENT rows and 100 PREPARED journal rows;
its returned historical journal snapshot is also capped at 100 (not a full listing
or a progress cursor). Repeat calls drain eligible rows. This bounds row count,
not file sizes, hashing time or wall-clock duration. There is no filesystem sweep.
Unknown pre-v13 `.publications` or `.assembly-*` files are not automatically adopted,
removed or treated as owned because of their name. A hard process exit before INTENT
can still leave unowned assembly work; receipt creation cannot recover bytes it never
recorded. Hard exit after INTENT can recover its exact original source or staging
inode; Python exception unwinding may already have removed unmoved assembly work,
which becomes ABORTED/missing bytes. Completed receipt state PREPARED means handoff
to the journal; consult the journal for final outcome.

Ambiguous paths, symlinks/nonregular leaves, replacement inodes, dual source/staging
or staging/final locations, malformed receipts and row-local filesystem failures
are preserved as MANUAL_REVIEW without deleting uncertain bytes. A bad row does not
block a subsequent valid row. Genuine SQLite errors and global lease errors propagate;
a failed attempt to persist MANUAL_REVIEW also propagates. Missing/stale owned bytes
may become ABORTED; terminal records and bytes are retained, not automatically retried
or cleaned. No manual-resolution/reset API is added in this slice.

On synchronous registration failure, rollback attempts a lease-fenced, inode/hash-
checked no-replace move back to staging and flushes both parents. It never rolls back
COMMITTED or a colliding/unproven destination. If ownership, rollback fsync, storage
or lease prevents safe rollback, bytes may remain exposed/unregistered and require
review; the exception is not a guarantee that the requested pathname is absent.
An fsync error never permits COMMITTED advancement. Recovery fsync errors are explicit
MANUAL_REVIEW; pre-PREPARED assembly errors leave a durable INTENT for a later replay.

Test evidence covers dummy-byte exceptions/EIO injection, hard subprocess exits,
SQLite trigger/DDL failures, replay, no-overwrite, leases and existing real-media E2E.
It does NOT demonstrate physical power-loss durability, device-cache correctness,
ENOSPC under actual disk exhaustion or a filesystem/kernel matrix. Correct syscall
ordering is conditional on SQLite and the local filesystem honoring fsync. No
production cleanup, retention, provider, credential or license changes are included.
Other F10 sites, F17/F18/F20 remain pending; this is not whole-Phase2 acceptance.

## F09/F16 local readiness contract (uncommitted review candidate)

Subtitle intervals describe when words are spoken; they are not automatically a
continuous visual-coverage timeline. `import_srt` retains its nonassembly validation:
positive, ordered/nonoverlapping cues within narration, nonempty text, and terminal
drift at most 500 ms. Import success does NOT promise local assembly readiness.
`plan_scenes` now rejects nonzero starts, any gap (even 1 ms), inexact terminal
coverage (even 1 ms), or any resulting scene that rounds to zero frames, before
changing the plan, project version/state, evidence, jobs or artifacts. It checks
both source intervals and DurationPolicy subdivisions. Existing import invalidation
rules still apply when a caller explicitly replaces imported sources.

Supported cue-based planning requires exact integer-millisecond coverage from 0
through the narration duration. This does not require frame-aligned milliseconds:
each boundary uses `round(Fraction(ms * 30, 1000))`, nearest frame, ties to even.
Each scene receives the difference of its cumulative boundary frame indices, not
an independently rounded duration. Counts telescope to the rounded audio endpoint;
every scene must have at least one frame. Maximum endpoint quantization is half a
frame. The separate final ffprobe tolerance remains 1/30 second + 20 ms, including
encoded AAC timing; neither that tolerance nor the 500 ms import allowance licenses
closing a visual gap. Original narration bytes/duration and subtitle text/timing
are not rewritten by planning. Audio is still encoded to AAC at mux as before.

Gap/intro/outro footage planning and a separate visual timeline import API are
UNSUPPORTED here. Recovery: retain original SRT/audio and arrange a separately
approved continuous visual timeline with explicit gap/outro footage in an external
editor. Do not pad text, remove silence, stretch a still over missing coverage, or
change subtitle times merely to pass validation. Retry this planner only with
independently valid contiguous source timing. There is no automatic repair or
resume command that makes unsupported coverage valid.

Local output is fixed: H264 in MP4, 1920x1080, 16:9, 30fps, yuv420p, AAC narration,
hard-cut concat; no music, burned subtitles, generated SRT or logo. A shared
validator checks OutputConfig construction, project initialization, transition
configuration, planning and both assembly modes. It accepts flat stored config or
the known youtube/presentation preset envelope; omitted legacy fields use defaults,
unknown fields/unsupported values reject. Stored config and project.transition
are both checked. Legacy unsupported assembly config fails before reconciliation,
directory creation or encoding, not after expensive work. Recovery is explicit:
`select_preset(pid, 'youtube')` and, when needed,
`configure_project(pid, 'transition', 'hard_cut')`. These authorized mutations retain
normal invalidation/approval rules; no automatic legacy migration occurs.

`final_hold_seconds=1.5` is retained solely as legacy default metadata, NOT a
rendered reveal/hold promise. Nondefault hold requests reject; no hold, transition,
gap renderer or pen-tip engine was added. IMAGE remains a whole-scene still render;
existing clip normalization/padding behavior is unchanged. These checks prove only
timing/config readiness, not media validity, creative QA or approval readiness.
Other F10 race sites, F17 and broader F18/F20 remain pending; the F12 candidate and its durability limits are specified above.

The public API is `Pipeline.assemble(project_id, output, dry_run=False)`. Output and
all inputs must resolve beneath the project's managed artifact root; symlink and
traversal escapes are rejected. Scenes are selected by database `ord`, never names.
ANIMATION/SCENE_VIDEO is preferred; an IMAGE is accepted only through a real ffmpeg
still render. Intermediate files live in a private `.assembly-*` directory and are
removed on normal Python unwinding (not hard process exit). Final bytes are atomically renamed and registered only after
ffprobe confirms H.264 1920x1080 30 fps yuv420p video, AAC audio, and duration within
1/30 second + 20 ms of narration. The persisted manifest records ordered scene and
artifact identity/checksums/durations, narration checksum, configuration, commands,
probe evidence, project version and lineage evidence hash.

CLI: `du-pipeline --db pipeline.db assemble-final PROJECT_ID OUTPUT_PATH
[--dry-run]` (`assemble` remains an alias). `OUTPUT_PATH` is positional and must be
inside the managed project root. Canonical parent lineage is narration first, then
exactly one selected visual per scene in `ord` order.

Assembly requires a current, non-revoked `POST_BATCH` approval whose project version
and evidence digest match the exact assembly inputs. Applicable scene approvals are
also revalidated. `FINAL` is intentionally not a pre-assembly gate: it is the downstream
human QA decision on the completed final artifact, so requiring it here would be circular.
The input/request evidence digest is distinct from `manifest_sha256`; the latter is
computed only after commands, ffprobe output, approval identities, narration metadata,
and canonical lineage have been added to the persisted manifest.

Dry-run performs no reconciliation, database writes, or directory creation. Publication
rejects symlink traversal (including `.publications` and output parents), uses atomic
no-replace promotion, and quarantines a promoted destination if evidence becomes stale
before registration.

Publication is a recoverable state machine rather than an impossible cross-resource
transaction. Before the immutable destination is exposed, a committed `PREPARED`
journal row records token, project/version/evidence, staging/final paths, output
checksum/size, manifest and timestamps. A leased reconciler revalidates evidence,
renames without overwrite, then atomically registers `FINAL_VIDEO`,
`final_assemblies`, and `COMMITTED`. It safely resumes either staging-only or
already-promoted crash states. Stale output is quarantined; missing/tampered bytes
and cleanup failures fail closed as auditable `ABORTED`/`MANUAL_REVIEW` states.
Recovery runs before assembly and is callable with `recover-publications`.
## F06/F08 evidence and review (uncommitted review candidate)

The v2 input inventory includes ACTIVE scene dependencies, contact sheets and
referenced ancestors; generated project finals and unrelated retired history are
excluded. Global execution version remains an exact fence. Assembly input identity
additionally binds narration URI/hash/duration, ordered selected visuals/frame
allocation, fixed encoding contract and persisted batch QA/contact-sheet subject.
Destination, command paths, tool-version observations and FINAL decisions are not
media input identity. This is conservative same-version reuse, not cross-version
cache reuse. Tool versions remain recorded as output provenance.

`approve_post_batch` now persists typed QA with its contact-sheet ID/hash and
approval binding in the append-only event ledger. Missing legacy receipts fail
assembly closed. Existing approvals are not silently upgraded.

Same-destination real assembly may reuse only a registered ACTIVE final with matching
input identity and intact manifest. It rehashes actual narration, active scene
inputs (including the QA image when a clip is selected), contact sheets and output,
runs fresh final media QA and rechecks gates/input bytes before returning. It never
allows narration/scene aliases, hardlink aliases or overwriting a stale destination.
Dry-run remains inspection only; an already-existing destination is rejected rather
than returning a verified reuse result. Relocation is a new publication, not reuse.

Final review uses two explicit public calls after assembly:

```python
pipeline.record_final_qa(project_id, artifact_id, QAEvidence(checks, score, evaluator))
pipeline.review_final(project_id, artifact_id, 'APPROVED', actor)
```

QA and human review receipts bind current registered FINAL_VIDEO ID/hash, full
manifest hash, input identity and exact project version. Both calls verify actual
output media and current input gates/bytes. Recording new QA revokes old FINAL
approval. The latest persisted passing QA is required for human approval; missing,
legacy, failed or stale evidence is not inferred. Trusted reviewer submission is
not proof of an AI-provider invocation. No schema migration is used: receipts live
in existing append-only events, with approvals retaining their evidence digest.
Generic PILOT/BATCH/FINAL command decisions now reject as unsupported, including
`du-final-approve`; they cannot report evidence-free success. Use scene review,
`approve_post_batch`, and these dedicated final APIs instead.

Summary FINAL validates stored receipt/subject metadata, not live disk bytes.
Execution/review performs the byte checks. Summary receipt lookup currently uses a
correlated event subquery; the SELECT-count ceiling is preserved, not a history-size
or latency guarantee. Further query-shape optimization and broader corruption/race
hardening require independent review. Existing F07 rerun DAG, full F05 scratch
ownership and F15 retention policy were outside that historical slice. F12's current
candidate and untested power-loss limits are specified above. No production,
provider, delivery or exact pen-tip whiteboard acceptance is implied.

## Atomic publication filesystem requirement

Final publication uses Linux `renameat2(..., RENAME_NOREPLACE)` so a destination
created concurrently can never be overwritten. The artifact root and requested
output must therefore be on the same local filesystem, and that filesystem and
kernel must support this operation. Cross-filesystem (`EXDEV`) and unsupported
(`ENOSYS`/`EINVAL`) operations fail closed; there is no copy, hard-link, or
check-then-rename fallback. Destination collisions are journaled as `ABORTED`, leave the destination untouched, retain the durable staged bytes, and do not register a `FINAL_VIDEO`.

Hệ thống không dùng fallback kiểu check-then-rename, copy hoặc hard-link vì các cách đó không duy trì hợp đồng publication bất biến.
