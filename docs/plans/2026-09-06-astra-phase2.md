# Astra Phase2 Implementation Plan

> For Hermes: execute directly with specification then quality review. No subagents, model switching, commits or pushes: explicit user restrictions override skill defaults.

Goal: complete a bounded F11/F10 transaction foundation, and sequence the remaining Phase2 findings without implementing them now.

Architecture: SQLite BEGIN IMMEDIATE owns authoritative state/version/evidence reads and mutation. Nested calls use SAVEPOINT isolation, never an inner commit. Existing Phase1 scene evidence, rerun OWNER provenance, epochs, pinned filesystem safety and migration atomicity remain unchanged.

Tech stack: Python, sqlite3, pytest, local temporary file databases; no providers/network/paid calls.

## Verified baseline

HEAD 929511ebcf42c68f2530e40e5b81fa1f58b543d8, clean tracked/untracked status. Baseline command `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`: 315 passed in 42.29s. Prior plan is historical; user supplied independent Phase1 PASS supersedes its stale progress ledger. Audit: /home/hermes/work/astra_whiteboard_audit_20260906.md.

## Task 1 — F11 RED nested isolation and depth recovery

Create tests/test_astra_phase2_transactions.py. Use temporary Database and a small transactional test table, not approval SQL. Cover caught nested exception retaining outer writes only; successful inner scope rolled back by outer failure; three-level recovery; BaseException; real deferred foreign-key failure on commit with depth restored and connection reusable; failure entering BEGIN/SAVEPOINT where feasible.

Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_astra_phase2_transactions.py`. Record expected failures before changing db.py.

## Task 2 — F11 GREEN

Modify only Database.transaction in src/du_pipeline/db.py. Capture entry depth; BEGIN IMMEDIATE at zero, generated internal SAVEPOINT otherwise. On success commit outer or RELEASE inner. On BaseException rollback outer or ROLLBACK TO then RELEASE inner. Restore captured depth exactly once in finally, including commit/release failures. Failed entry never increments depth. Do not change constructor DDL or executescript behavior. Verify focused tests and existing migration suites, then full suite. Save external milestone.

## Task 3 — F10 RED bounded job admission

Create tests/test_astra_phase2_jobs.py. Fixture must use public init/import/plan/artifact/attempt/QA/scene approval/post-batch methods; no approval SQL shortcuts. Open second Database before hooks (constructor migration needs a write lock). Hook immediately before first transaction acquisition to commit pause/cancel or evidence mutation through connection B; assert no stale/new queued job. Exercise start_batch, start_animation, retry and direct private insertion boundary as a primitive regression, plus run_stage routing. Test stale gate mutation between public entry and transaction, current-version idempotency, paused/stale resume CAS, and caught retry failure atomicity. Verify inverse order job then pause yields PAUSED. No scheduler sleeps; explicit deterministic hook ordering.

## Task 4 — F10 GREEN bounded lifecycle

Modify src/du_pipeline/service.py: _create_job active/version/idempotency reads inside write transaction; start_batch/start_animation gates and insertion in one transaction; queue_retry scene/state/budget reads and mutation inside transaction. Project resume reads current state/version inside transaction and uses state/version precondition for its lifecycle update. Existing pause/cancel and resume_job conditional updates remain fencing; test them rather than claiming a worker CAS exists. Preserve private _create_job API unless explicit expected revision is needed; serialization under the same write lock suffices for gated creation. Do not fix unrelated import/planning/artifact/restore/snapshot races in this slice.

## Task 5 — verify and handoff

Focused: new Phase2 files plus tests/test_astra_attempt_migration.py, tests/test_adversarial_regressions.py, tests/test_final_blockers.py, tests/test_reconciliation_lease.py, Phase1 provenance/epochs/public E2E tests.
Full: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
Run `git diff --check`, inspect complete intended diff and status, confirm unchanged HEAD. Save exact RED/GREEN counts, covered sites and residuals outside repo at /home/hermes/work/astra_phase2_implementation_20260906.md. Leave all changes uncommitted for independent review.

## F10 mutation/snapshot continuation — bounded candidate, verification underway

Authorized continuation inspected imports, planning, configuration, replace/restore/checkpoint, dependency application and status views. New tests/test_astra_phase2_mutation_snapshots.py reproduces 13 failures before runtime edits. Candidate fixes import_audio/import_srt/import_document/configure_project/select_preset authoritative state/dependency reads under existing BEGIN IMMEDIATE; document normalization remains outside SQL locking. status/status_summary use Database.transaction(write=False), deferred BEGIN with inherited SAVEPOINT/BaseException cleanup, preserving query ceilings and allowing a second WAL writer to commit during the snapshot. This is not CLI readonly opening or a SQL write-prevention sandbox. No schema addition; inherited v14 semantics unchanged.

Initial new 13 GREEN; expanded focused 138 passed in 8.55s. Initial full 522 passed; expanded final full 535 passed in 67.18s, no skips reported. git diff --check PASS; unchanged HEAD 929511e; bounded candidate ready for independent review, all uncommitted. Inherited jobs interleaving helper now forwards transaction keyword arguments and fires only on writer acquisition; all safety assertions retained. External report contains per-path inventory and exact logs.

### Authorized plan_scenes / replace_scene_artifact continuation — bounded candidate

RED 16 failed / 5 passed; initial GREEN 21 passed. Final focused 216 passed in 27.85s; final full 562 passed in 67.89s, no skips reported. New tests/test_astra_phase2_plan_replace.py contains 27 tests, including six post-GREEN verification cases (not 27 newly reproduced bugs). Inherited runtime/test changes preserved; incremental runtime scope only these methods plus private _planning_snapshot; no schema change. Uncommitted, independent review required.

1. FIXED/TESTED plan_scenes: coherent deferred snapshot of full project/audio/cues/scenes plus latest append-only SCENES_PLANNED event ID; compute outside own writer, compare exact expected snapshot and active state inside BEGIN IMMEDIATE before mutation. Full source rows, not project version alone, bind import dependencies. Reproduced changed audio/SRT/config and competing plan with existing or empty scene set; empty fixture is cleared by public SRT replacement, not a special SQL fixture. Verify concurrent source commit during snapshot and own returned rows captured before commit, not a later plan. Existing stop rejection retained. Source timing/text and pure planning algorithm unchanged; no silent repair.
2. FIXED/TESTED replace_scene_artifact: same coherent expected project/source/scene/plan identity and writer recheck reject pause/cancel, scene-ID reuse after replan, config/image revision changes. Always make a private exclusive copy before own writer (also for managed source); pin parent FD and created inode; reject pre-registration namespace substitution and compensate only matching unreferenced leaf after rollback, including BaseException. Real second connection pause/cancel/replan during copy verified; inverse writer order rejects concurrent stop. Public QA/approval fixtures retained, old artifacts/detached provenance/source bytes preserved. No modifications to add_artifact or F12 protocol.

Scope limits: snapshot equality is conservative and may require retry after unrelated scene/project changes; plan event identity is not a new global scene UUID. Copy may execute inside a caller-owned outer transaction; this method does not release that lock. Caught replacement failure inside outer transaction compensates correctly; successful nested replacement followed by later caller rollback/crash is NOT given a durable filesystem rollback receipt in this slice. No hard-crash/power-loss copy recovery or guarantee against continuously hostile namespace/in-place byte mutation; unknown/substituted files are retained, never swept. Original invalid-timing diagnostic branch is unchanged; coverage tests enforce inherited early local rejection. Full suite is not a universal filesystem/concurrency proof.

### Authorized restore/checkpoint continuation — candidate policy and verification

Incremental runtime only service.py checkpoint/restore and private helpers; no schema migration. RED 23 failed / 2 passed; initial GREEN 25 passed. Initial focused 83 passed; initial full 611 passed in 73.35s. Final focused 294 passed in 49.46s; final full 619 passed in 97.02s, no skips reported; git diff --check PASS. Final logs /tmp/f10-restore-focused-final.log and /tmp/f10-restore-full-final.log. All changes uncommitted at unchanged HEAD 929511e for independent review; no whole-Phase2 acceptance. New tests/test_astra_phase2_restore.py and tests/test_astra_phase2_restore_verification.py use independent connections and public approval setup; deletion TTL/status and historical-format SQL are explicit disposable fault fixtures, never approval fabrication.

Policy:
- checkpoint creation requires ACTIVE under writer even if a current paused job exists. Restore remains content-only for already PAUSED/BLOCKED/COMPLETED projects; concurrent lifecycle change rejects rather than implicitly resuming or applying stale evidence. Version increments from the writer-validated expected revision, jobs fenced and approvals revoked as before.
- New JSON snapshot v4 adds random immutable-per-capture identity and latest append-only SCENES_PLANNED event ID. Existing (job_id,name) UPSERT and latest-by-stage-ID selection remain unchanged; this is NOT append-only checkpoint history or a new scene UUID. Replacing an identical payload still changes identity. Replan generation mismatch rejects even for identical reused integer scene IDs. Epoch policy/attempt history unchanged.
- Existing v3 history is NOT rewritten/deleted/backfilled. Scene-bearing v3 cannot establish generation and now fails closed with explicit manual-review error; empty-scene v3 retains existing checksum/inventory validation. No implicit reconstruction/repair route. Hash/identity is not a signature against arbitrary DB writers.
- Strict artifact inventory equality remains; v14 detached_scene_id must match, is never rebound. DELETED ownership state must agree in both directions: restore cannot revive leftover deletion bytes or convert live bytes into a historical deletion decision. New artifacts since checkpoint still cause rejection. Retention TTL unchanged; restore does not pin files against future cleanup.
- Coherent deferred DB snapshot includes project/source/scene/plan identity, full artifact/approval/job/stage rows. Same snapshot equality is revalidated under BEGIN IMMEDIATE/savepoint with mutation. Conservative conflicts (including unrelated stage/job change) require retry.
- Byte hashing uses pinned no-follow regular-file descriptors outside the method's own writer; metadata and public parent/leaf identity are checked inside writer. Only bounded namespace/stat work under writer, not bulk hashing/copying. Cooperative cleanup commits DELETED and moves under its own writer; restore validates that status and cannot race the deletion decision. No additional lease is taken because restore never moves/unlinks bytes or writes deletion receipts. Reconciler compensation for live files only restores absent locations; unknown/substituted bytes remain untouched.
- No filesystem mutation by restore, hence SQL/savepoint rollback is sufficient compensation, including successful nested restore followed by outer rollback. Existing outer writer is NOT released for nested hashing; callers should avoid broad outer transactions. No hard-exit/power-loss or continuously hostile namespace/in-place mutation guarantee; identity metadata checks are not a filesystem lock. Pins scale with nondeleted artifact inventory; no new unbounded-history performance certification.

Remaining EXACT scope: approve_post_batch lifecycle/readiness checks and contact-copy ownership/rollback remain unimplemented/unproven here; apply_dependency double-apply/rejection-before-existing APPROVED->APPLIED CAS remains untested in this slice, not declared broken. add_artifact/contact-copy/replacement successful nested copy followed by outer rollback/crash still has no new durable filesystem compensation receipt. F18/F20 CLI and wider migration matrix remain excluded. Provider/default Codex OAuth gpt-image-2 high/no fallback, retention/license/settings unchanged. No whole-F10 or Phase2 acceptance.

Historical next-slice inventory below is superseded for items 3–5 by this continuation; items 6–7 remain deferred:
Remaining coherent slices (original inventory):
3. restore_artifact: reproduce cleanup/delete or detach/replan after status/file read. Coordinate actual deletion lease/dirfd ownership rather than assuming SQL rolls files back. Preserve intentional content-only restoration without lifecycle resurrection, including paused/completed semantics.
4. restore_latest_checkpoint: reproduce current version advancing after validation (risk of setting version to old+1), changed inventory and checkpoint replacement; bind current revision, canonical scene/artifact inventory and snapshot identity inside mutation. Include detached_scene_id historical provenance in analysis without inventing a live scene FK. Files are validated but not mutated here; do not hold expensive hash work under a writer without explicit design.
5. checkpoint: test concurrent pause with existing job; payload reads already transactional, but existing-job path bypasses _create_job active check. Establish supported paused content-snapshot semantics before changing policy.
6. approve_post_batch: recheck paused/cancelled state and IMAGE_READY under mutation; reproduce first and design compensation for newly copied contact sheet on outer event failure.
7. apply_dependency: test double apply/rejection-before-CAS; preliminary read exists but conditional APPROVED->APPLIED protects single-effect admission. No new provider/settings mutations. select_provider remains inspected-only and excluded from edits/tests by current restriction.

Do not claim all F10 races fixed. F18/F20 CLI remains unstarted. No changes to providers/defaults, production DB/settings, retention/license or Phase1/F12 filesystem machinery; leave uncommitted for independent review.

## Post-batch continuation — partial lifecycle candidate only

New tests/test_astra_phase2_post_batch.py: RED 2 failed / 4 passed; pause/cancel were admitted. Runtime adds ACTIVE check inside existing post-batch writer only. Readiness/revision cases already rejected by inherited evidence; no claim new snapshot protection. Dependency existing CAS passed double apply/replay and explicit temporary rejection fault (public API prohibits reversing APPROVED); no dependency runtime change. Writer-first post-batch lock ordering and stopped replay verified. Focused 35 passed; full 626 passed in 74.23s, logs /tmp/f10-post-batch-{red,focused,full}.log. Compatibility: already-stopped projects now reject post-batch approval; content-only restore unchanged.

Requested copy slice remains incomplete: no new durable ownership receipts or nesting prohibition, no nested successful-copy outer rollback/crash reproduction. Existing unsafe add_artifact compensation and nested add/contact/replacement residuals are NOT resolved by this lifecycle check. Next mandatory work: deterministic copy outer rollback/inner failure/process-exit tests; explicit exclusive pinned ownership/alias/unknown-byte compensation with durable receipt or fail early for unsupported nesting; expected contact revision/evidence snapshots rechecked with mutation, without broad external-copy writer locks. No schema change or whole-F10 acceptance. F18/F20 readonly CLI, error/close behavior and executable truthful docs plus wider historical migration matrix remain deferred. Independent review required, inherited uncommitted candidate preserved.

## Managed-copy ownership / contact expected evidence — bounded compatibility policy

This continuation supersedes historical statements above that public successful nested copies are supported without rollback ownership. No schema or publication-journal change. Runtime scope is add_artifact, approve_post_batch, replace_scene_artifact and private copy/registration/contact helpers only.

- Public replace_scene_artifact and approve_post_batch require top-level entry (also for managed source). Public add_artifact rejects caller-owned transactions when a copy is required: external input or an already registered managed URI. Rejection is PermissionError with top-level/caller-owned nesting explanation, before copy or logical mutation. Copy-free registration of an unregistered managed caller file remains savepoint-composable; rollback never deletes that caller file. No public bypass keyword/context flag. Removed unused private _new_copies argument; it was not a durable ownership receipt.
- Internal contact/replacement composition uses _register_artifact, a SQL-only primitive requiring an existing transaction, with no filesystem copy or commit. The top-level method owns one exclusive managed copy through SQL commit/rollback. This is an explicit supported composition boundary, not a nesting bypass on a public copy operation. Idempotent Discord wrappers that enclose scene replacement in a caller transaction now fail closed; no independently committed filesystem work under that wrapper. A transaction-aware durable command/copy receipt is separate work, not silently implemented here.
- Copy and hashing run outside the method's own writer. O_EXCL/O_NOFOLLOW creation, pinned parent dirfd and an open inode descriptor prevent overwriting existing leaves and inode-reuse compensation. Registration checks pinned/public leaf identity under writer. Failure after completed copy, including BaseException, compensates only matching singly-linked inode with unchanged size/mtime/ctime and no artifact URI/inode reference, under a reference-check writer and via pinned dirfd. Caller sources, substituted leaves/parents, hardlink aliases, referenced bytes and observed in-place changes are preserved. Conservative reference scan currently scales with artifact inventory; no constant-time performance guarantee.
- No durable copy receipt/recovery is added. Hard process exit after copy/SQL registration but before commit leaves unknown bytes; reopening retains them and SQL rolls back. No orphan sweep, journal forgery or claim of crash cleanup. Incomplete copy before completion metadata is likewise conservatively retained because exclusive inode alone cannot prove subsequent byte ownership. Manual review is required for these residuals. These rules are not hardware power-loss durability, nor protection against continuously hostile namespace/in-place mutation between validation and use; stat checks are not a filesystem lock.
- Contact review captures coherent deferred full project/audio/cue/scene/plan-generation identity plus artifact and approval rows before copying; equality is revalidated with ACTIVE/current QA/pilot readiness inside approval writer after copying. Replan ID reuse, changed valid QA/reapproval, stop and revisions conflict rather than approving a contact sheet against newer evidence. Conservative conflicts require fresh review/retry. Returned contact result is captured in the same writer. Managed contact inputs now also receive a private copy instead of adopting caller bytes.
- Tests record original nested outer-rollback and substituted-leaf RED, then explicit no-copy-on-reject compatibility assertions, standalone Exception/BaseException compensation, source/copy replacement, alias/reference/parent/in-place faults, writer-first interleavings, copy-first contact changes and subprocess os._exit retention. Existing real-media public E2E/assembly and Phase1/F12/v14/checkpoint regressions remain mandatory. No test safety assertion removed: inherited fault hooks follow SQL registration, and old nested-success fixtures additionally test standalone event failures plus nested rejection.

F18/F20, general F10 inventory, providers/defaults, settings/retention/license remain outside scope. Uncommitted candidate, independent review required; not whole Phase2 acceptance.

## Remaining Phase2 — plan only

### F09/F16 timing and capabilities — authorized continuation implemented for review
Continuation authorization supersedes the original plan-only status for this finding pair only. Public RED reproduced audit timing mismatch and unsupported config acceptance. Candidate implements fail-early local readiness, not a broader visual planner. Shared validation checks exact millisecond coverage and cumulative 30fps nearest-frame/ties-to-even allocation, including generated subdivisions, and fixed H264/MP4/1920x1080/30fps/yuv420p/AAC hard-cut capability. Raw source imports remain nonassembly inputs; no silence closure, footage invention, still stretching or text edits. Flat/preset legacy unsupported configs reject before planning mutation or assembly reconciliation/encode. Default hold remains explicitly legacy metadata; nondefault hold is unsupported. See docs/final-assembly.md for recovery and precise tolerances. External report records exact verification outcomes. Inherited F11/bounded F10 retained; other F10/F17/F12/F18/F20 pending; no whole-Phase2 completion.

Original work specification:
Files: src/du_pipeline/policies.py, contracts.py, media.py, service.py; tests/test_policies.py, test_final_assembly.py and new timing/capability regressions; docs/final-assembly.md.
RED audit gap/start-offset/tail fixture and unsupported width/fps/transition. Separate subtitle intervals from exact visual coverage; choose explicit gap/outro planning or actionable early rejection, never extend one still silently. Reject unsupported options through an explicit capability contract before expensive work. Preserve narration bytes; test exact frame allocation and actual FFmpeg output. Precision hand/pen reveal remains a separate unimplemented engine.

### F10/F17 authorized bounded continuation — candidate complete, independent review pending
Verified 199 focused tests / 509 full tests; git diff --check PASS. v14 additive partial project identity index, checksum/identity INSERT+UPDATE triggers and fail-closed historical ownership/duplicate/checksum preflight. add_artifact URI ownership/copy/hash/max-version/registration/event serialize in one write transaction; publication allocator remains transactional. Real v11 (3bfe33d) and inherited v13 frozen SQL fixtures preserve original fields/checks and verify failed migration rollback/reopen. No artifact rebuild or historical normalization.
Full regression exposed scene-detachment collisions: additive detached_scene_id records provenance only during actual invalidation, without renumbering/deleting old artifacts; true project-level index/allocation excludes explicitly detached history. Legacy NULL duplicates remain ambiguous and fail closed. Current storage still permits multiple ACTIVE versions; assembly rejects ambiguous eligible visuals. No global detached-generation uniqueness claim (scene IDs may be reused), closed kind enum, all-fields immutability, universal schema parity or whole-F10 completion.
Next bounded tasks: deterministic restore/replace/checkpoint/config races; imports/planning stale dependency reads; post-batch readiness and coherent snapshots; earliest historical layout/schema-signature matrix. Keep attempts/scratch, F12 publication and other inherited fixes intact. No unrelated CLI work. Full milestone/evidence/residual inventory: /home/hermes/work/astra_phase2_implementation_20260906.md.

Original slice specification:
Current slice: reproduce NULL project-artifact duplicates and public max-version races with independent connections; test first, then explicit partial uniqueness and serialized allocation/registration. Audit actual historical DDL and stored digest fixtures; fail closed with actionable diagnostics for invalid/ambiguous rows, without silent historical repair. Prefer additive constraints if equivalent enforcement avoids hazardous table rebuild. Verify rollback/reopen, nullable ownership, multiple ACTIVE policy, scratch/publication compatibility; inventory other F10 mutations separately. No unrelated CLI work.

### Remaining F10/F17 constraints and migrations
Files: db.py/service.py and new historical-schema fixtures/tests. Inventory remaining reads outside transactions: imports, plan_scenes, add_artifact version allocation, restore_artifact, replace_scene_artifact, restore_latest_checkpoint, configuration, publication and status snapshots. Reproduce each separately before fixing. Add project-level NULL-scene partial uniqueness only after duplicate inventory/fail-closed upgrade policy; validate digest/kind contracts without inventing a destructive normalization. Test genuine historical layouts, schema parity, failure rollback and reopen idempotence. No universal migration/race claim.

### F12 publication recovery — bounded candidate complete, independent review pending
Continuation reproduced `bad_final_type`: 51 passed / 1 failed; expanded typed-receipt RED: 36 failed / 6 passed. Recovery now validates required paths, exact integer identity/size/version (rejecting booleans), regular-file mode, SHA256 syntax, prepared payload and receipt/journal ownership before opening/creating/fsyncing publication parents. Invalid recorded rows become MANUAL_REVIEW, retain bytes, and do not abort unrelated valid rows. Reservation collision is checked before source movement. No broad exception suppression added; SQLite/lease errors propagate.
Final focused publication/assembly/blockers/lease/migration: 121 passed in 21.30s. Full: 474 passed in 63.72s; git diff --check PASS and full runtime diff inspected. One intermediate full failure (473 passed) was a descriptor-leak fixture using non-SHA256 evidence; only fixture input changed to a real unique SHA256, all assertions preserved. Additive v13 migration reviewed; index-DDL failure rollback and repeated reopen/legacy journal recovery pass. Historical migration injection tracks SCHEMA_VERSION. Existing Phase2 changes preserved; all uncommitted.
Limitations: bounded 100 intents and 100 prepared journal rows per invocation; manual review is not automatic repair, unknown files are not swept. Legacy rows without receipts retain hash/size ownership semantics, not retroactive inode provenance. Tests cover process exits and injected failures, not physical power loss or arbitrary persistent storage corruption. Manifest validation checks JSON object shape, not a new cryptographic authenticity protocol. No whole-Phase2/production acceptance.

Files: atomic_fs.py/service.py; test_final_assembly.py/test_final_blockers.py and new recovery tests. RED pre-PREPARED journal rejection/orphan, per-row fault isolation and fsync ordering. Design provable ownership receipts before staging, bounded quarantine/reconciliation and file/directory fsync order. Preserve no-replace, dirfd identity, leases and stale evidence fencing. Process-kill/disk-failure tests do not claim physical power-loss certification. No live cleanup.

### F18/F20 authorized continuation plan
1. Add tests/test_astra_phase2_cli_inspection.py: subprocess missing DB/error/parser reproduction; direct readonly SQLite authorization, schema/version, writer contention and byte/directory snapshots. Run RED before runtime changes.
2. db.py: separate current-schema open from legacy writable constructor initialization/migration; readonly URI with explicit WAL sidecar prerequisites (never immutable on a live database), no repair/DDL/writer reservation. Close partial opens and use existing context manager. CLI init/migrate explicitly own migrations; normal writes require current existing DB.
3. cli.py: context-managed dispatch; sanitized fixed-code JSON expected errors and parser errors; preserve role forwarding. Inspection status/summary/report/Discord status and eligible dry-runs use readonly opening. Tests must detect unintended filesystem writes.
4. README/capability documentation: executable temporary quickstart, positional assemble syntax, local renderer limits and remaining CLI usecases. Historical evidence stays intact with historical banners.
5. Focused/full tests, isolated wheel install/CLI smoke and diff-of-diffs review; milestone report. No commit, delegation or providers. Additional typed media commands may be deferred under authorized fallback; no SQL approvals for E2E.

### F18/F20 bounded fallback candidate / remaining inventory
Initial RED 8 failed; boundary RED 3 failed / 4 passed; focused verification 49 passed including service snapshot and real-media public API E2E. Initial full 688 passed; final full result recorded externally. README metadata quickstart executed through source console wrapper and isolated installed wheel; sdist/wheel build and no-index/no-deps installation passed. Not a real-media CLI E2E.

Implemented: Database.open_existing current-schema mode=rw without migration; strict mode=ro/query_only inspection; early missing DB rejection; context close; fixed-code JSON expected errors; explicit migrate/init boundary; positional assemble docs; capability matrix and historical banners. Existing service runtime/snapshot/role forwarding preserved.

Compatibility limit: strict inspection rejects WAL-format DB before SQLite open, including closed WAL. Stdlib mode=ro alone may create/mutate WAL shared memory; no immutable=1/live WAL or automatic journal conversion. Offline rollback-journal backup must be prepared separately as an explicitly writable operation. Stable path/journal format required during inspection; no hostile same-UID replacement guarantee. This limitation means F18 live monitoring usability is NOT completed.

Explicitly omitted under authorized fallback: live WAL zero-write backend; typed image-result/scene QA/POST_BATCH/final QA/final review CLI; automatic safe narration import/probe/hash; init scene-range flags; checkpoint/restore/rerun public CLI lifecycle; real-media CLI E2E; durable Discord command/copy integration/manual-review resolution. Do not use SQL approval shortcuts or bypass rejected Discord scene replacement. Wider historical-schema signature/migration matrix, provider/worker/delivery, retention/license remain separate. Current v14 constraints and v4/legacy v3 checkpoint behavior unchanged.

### F18/F20 CLI and documentation truth (original specification)
Files: cli.py/db.py/README.md/docs/final-assembly.md/docs/data-contracts-v1.md and executable CLI tests. RED nonexistent inspect path must not create DB, structured errors/close-on-failure, accepted quickstart syntax. Explicit readonly inspection and migration ownership; expose only evidence-bound supported use cases. Publish capability matrix distinguishing local assembly, fake deterministic executor, absent worker/provider/precise reveal. Test documented commands with temporary artifacts and public approvals.

## Local media CLI continuation — bounded candidate

Supersedes historical omitted-CLI inventory only. Added thin register-artifact,
image-attempt, scene-qa, post-batch, final-qa, final-review commands and init
--min-scenes/--max-scenes. Validated JSON becomes QAEvidence with evaluator
`external:` plus original caller text; service retains subject/hash/dependency
binding. No automated evaluator or expected-subject CAS added. Actor and artifact
subject remain mandatory for final review; existing approval/role/active/ownership
and no-overwrite gates are not bypassed. Only service runtime change is explicit
unknown-scene ValueError in record_scene_qa, reproduced as CLI traceback first.

RED: eight missing-command/validation failures. Initial focused GREEN: 24 passed
including inherited strict inspection/boundary tests and actual subprocess WAV/PPM
multi-scene final encode/reuse/review. Boundary run: two failed/five passed; one
was a test's raw failed DML leaving an implicit transaction (fixture rollback),
one was unknown-scene traceback (service guard). Initial full: 706 passed in
92.16s. Expanded provenance assertions and final full verification recorded in
external report. No existing tests edited by this continuation.

Operational WAL inspection: explicit global --operational-wal bypasses only the
strict header rejection, retaining mode=ro/query_only, current-schema and missing
DB rejection. SQLite can create/change sidecars; NOT zero-filesystem-mutation.
No immutable=1, checkpoint, migration or journal-mode conversion. Strict offline
snapshot tests remain unchanged. Live writer commit visibility and reader DML/
writer-scope rejection tested. Zero-write live WAL backend remains deferred.

Executable workflow/QA syntax: docs/cli-media-workflow.md and
tests/test_cli_media_workflow.py. All canonical workflow mutations use CLI public
service routing; test SQL is read-only provenance inspection, not approval edits.
Measured narration ingest deferred because import_audio is metadata-only and no
safe public managed narration ingestion API exists; fixture creates WAV in managed
root and supplies its known duration/hash. Remaining checkpoint/restore/rerun CLI,
durable Discord copy integration, wider historical-schema signature matrix,
worker/provider/precision reveal are not closed by this slice. Candidate remains
uncommitted at HEAD 929511e for independent review; not full Phase2 acceptance.
Reusable procedure retained in this executable workflow/plan; separate skill save
requires Sếp approval, not an unrelated repository change.

## Recovery CLI continuation — bounded candidate

Supersedes historical omitted recovery-command inventory only. Seven thin routes in cli.py call existing public checkpoint/restore/rerun APIs; service.py, schema v15, R1/R2/R3 and copy nesting boundaries unchanged. RED 16 missing-command failures, initial GREEN 16; expanded measured external WAV subprocess recovery lifecycle and strict inputs/unknown IDs/replay/OWNER/stale decision verification in tests/test_cli_recovery.py. Focused 166 passed in 42.07s; full evidence recorded in external report. Executable command/data/role and v3 manual-review policy: docs/cli-media-workflow.md. No snapshot provenance backfill or SQL approval fabrication. Applied image rerun retains historical attempts and starts a fresh epoch; fresh scene QA/POST_BATCH/new final/review exercised. Independent full re-review required; all uncommitted. Wider codecs, zero-write live WAL, historical schema matrix, crash/power-loss certification and worker/provider/precision reveal remain out of scope, not acceptance claims.

## Non-goals / controls

F13/F14 integrations, G01 worker, G02 engine/provider and G03 delivery are not authorized. F15 retention and F19 license/settings remain unchanged. No production DB, secrets, live cleanup, provider invocation, fallback, paid call, delegation, model switch, commit or push. Default Codex OAuth gpt-image-2 high/no fallback unchanged. This plan is not a completion claim.
