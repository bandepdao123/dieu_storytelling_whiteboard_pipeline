# Local media CLI candidate — external QA, no generation

Executable acceptance from repository root (Python, pytest, ffmpeg and ffprobe required):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_cli_media_workflow.py::test_real_cli_media_workflow
```

This test invokes actual CLI subprocesses against a disposable WAL database. It creates a real two-second PCM WAV and binary PPM, then runs init -> audio/SRT import -> two-scene plan -> IMAGE registration -> successful attempt -> externally supplied scene QA -> human approval -> persisted contact-sheet POST_BATCH -> real MP4 assembly -> reuse -> final QA -> final review. No direct SQL approval mutation, provider call, automatic evaluator or image generation. Fixture passing visual judgments are caller assertions, not measured AI assessments. Assembly performs its inherited media/evidence validation.

Command syntax (global --db and --role precede the command):

```text
du-pipeline --db DB init NAME --min-scenes 2 --max-scenes 2
du-pipeline --db DB --operational-wal status PROJECT
du-pipeline --db DB import-audio PROJECT MANAGED_WAV DURATION_MS SHA256
du-pipeline --db DB import-srt PROJECT INPUT.srt
du-pipeline --db DB plan PROJECT
du-pipeline --db DB register-artifact PROJECT IMAGE INPUT.ppm --scene-id SCENE_ID
du-pipeline --db DB image-attempt SCENE_ID succeeded
du-pipeline --db DB scene-qa SCENE_ID QA.json
du-pipeline --db DB approve PROJECT SCENE_CODE --actor HUMAN
du-pipeline --db DB post-batch PROJECT CONTACT.ppm QA.json --actor HUMAN
du-pipeline --db DB assemble PROJECT OUTPUT.mp4
du-pipeline --db DB assemble PROJECT OUTPUT.mp4
du-pipeline --db DB final-qa PROJECT FINAL_ARTIFACT_ID QA.json
du-pipeline --db DB final-review PROJECT FINAL_ARTIFACT_ID APPROVED --actor HUMAN
```

Use IDs returned by init/plan/register/assemble. The test is a complete executable example, not a requirement to guess placeholders. Scene range defaults remain 50..360. For batches beyond five scenes, approve pilot scenes before later image attempts as required by service.

QA.json is exactly an object with checks, score and evaluator:

```json
{"checks":{"visual":true},"score":1,"evaluator":"human-reviewer-id"}
```

Checks must be a nonempty object with nonblank names and actual boolean values. Score must be a finite number in [0,1], not a boolean. Evaluator must be nonblank text. Extra fields (including caller-forged subject) reject. The persisted evaluator is `external:` plus the supplied evaluator, preserving its text. Service selects, verifies and persists the current subject/hash/dependency provenance under its existing transaction; this CLI does not add an expected-subject compare-and-swap for reviews prepared earlier. Operators must review the current identified image/contact/final bytes. No claim of authenticated evaluator identity or automatic AI evaluation.

register-artifact forwards optional --parent-id, --scene-id and kind to add_artifact. Existing ownership, copy, role and active restrictions remain authoritative. Generic kind registration does not create final assembly provenance. image-attempt accepts failed or succeeded, optional --error and an existing --scratch-receipt; it does not allocate scratch, invoke a provider or select a fallback. Final review accepts APPROVED or REJECTED, requires an artifact ID and current persisted final QA. Output no-overwrite and evidence reuse remain service-owned.

Measured narration candidate: `du-pipeline --db DB import-narration PROJECT /absolute/external.wav`. Supports complete uncompressed PCM RIFF/WAV only, using actual sample frames/sample rate (nearest millisecond) and SHA256 of the exact owned snapshot consumed by the WAV parser. Other codecs/containers, truncated chunks/frames, empty and no-audio files fail closed; no conversion, generation or original-audio rewrite. JSON returns artifact_id, owned uri, duration_ms, sha256 and measurement=pcm-wav-owned-v1; NARRATION_MEASURED audit event and NARRATION artifact registration commit with the existing audio import/invalidation invariants. The original `import-audio` remains explicitly legacy metadata-only in help/output.

The source is opened no-follow through pinned parent directories; symlinks, hardlinks and same-project managed input paths reject. Destination is random exclusive-create, never a caller-selected overwrite target. Measurement/hash run outside the writer on owned bytes, then namespace/content identity and expected project/source/evidence are fenced under the import writer. Caller-owned transaction nesting rejects before copying; SQL-only internal composition uses existing savepoints. Ordinary failure removes only proven unchanged unreferenced owned copies; substituted/in-place unknown or referenced bytes remain. Source bytes are not modified; OS read access times may change. Linux /proc/self/fd is required. Parser materializes the WAV in memory; no large-file performance guarantee. Continuous hostile filesystem mutation is not locked out.

Executable measured external WAV -> scenes/typed external QA -> real assembly/review -> reopened status:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_cli_measured_narration.py tests/test_measured_narration.py
```

This test reuses the existing real-media subprocess workflow, replacing only legacy metadata import with a separately created external WAV and measured CLI import; it does not yet exercise recovery/rerun continuation. No narration idempotency key or automatic crash recovery: repeat success is a new import and may invalidate downstream evidence. After uncertain interruption, inspect the audio/artifact rows and NARRATION_MEASURED event before retrying. Unknown residual files are manual-review only; never sweep/adopt/delete them by filename.

Inspection: strict offline mode is unchanged by default. Explicit global --operational-wal permits mode=ro/query_only WAL reads and possible SQLite WAL/SHM creation or shared-memory writes. It is NOT zero-filesystem-mutation inspection. No immutable=1, automatic checkpoint or journal-mode conversion. Missing/old schema still fail without migration. The flag only affects inspection opening; it does not make a mutation command read-only.

Recovery CLI candidate (global --db DB and --role OWNER precede commands):

```text
checkpoint-create PROJECT STAGE DATA.json
restore-artifact ARTIFACT_ID
restore-checkpoint PROJECT STAGE
rerun-propose PROJECT image
rerun-decide PROPOSAL_ID APPROVED --actor OWNER_ID
rerun-reapprove PROPOSAL_ID --actor OWNER_ID
rerun-apply PROPOSAL_ID
```

These route respectively to checkpoint, restore_artifact, restore_latest_checkpoint, propose_rerun, decide_rerun, decide_rerun(reapprove=True), apply_rerun. Checkpoint stages are nonblank labels; rerun stages are planning (alias plan), image, qa, animation, assembly, upload. DATA.json must be a UTF-8 JSON object: duplicate keys at any depth, nonfinite numbers, nonobjects and reserved top-level snapshot fields (snapshot_version, data, project, job, scenes, artifacts, snapshot_sha256, identity, plan_identity) reject. Example: {"note":"reviewed local fixture"}. This is user metadata, never an imported snapshot or authorization receipt.

Decide accepts APPROVED or REJECTED and requires a nonblank actor. Only OWNER can decide/reapprove/apply; role and actor are local caller assertions, not authenticated identity. Apply never approves. Pending stale APPROVED proposals require explicit reapprove; ordinary decide cannot replace that step. Applied/rejected proposals cannot be reapplied/reapproved. Decision/apply/epoch receipts remain service-owned; image rerun starts a new budget epoch while retaining historical attempts. No worker/provider is invoked. Output JSON returns proposal_id and, on apply, job_id; authoritative provenance is persisted in RERUN_DECIDED/RERUN_APPLIED events.

Checkpoint v4 capture identity, plan generation and strict complete artifact inventory remain authoritative. Capture requires ACTIVE; restore is content-only, never resumes a stopped project or restores approval authorization. Restore is not idempotent: repeated supported restore increments the live version and revokes approvals. New artifacts after capture cause inventory rejection; missing/deleted/tampered bytes are never reconstructed. Fresh current QA and approvals, POST_BATCH and final review are required by existing evidence gates. Retention is unchanged.

Executable full measured-media recovery lifecycle (actual subprocesses and FFmpeg; SQL inspection only):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_cli_recovery.py
```

Includes measured external WAV -> two scenes/external QA/assembly/review -> checkpoint -> artifact/checkpoint restore -> stale OWNER decision rejection -> explicit reapproval -> image rerun -> fresh QA/POST_BATCH/new output/review, receipts/history/replay and invalid-role checks. Fixture visual QA remains explicitly human-supplied, not an invented evaluator. Existing recover-publications is bounded to inherited publication receipts, not narration-copy recovery. R2 Discord/copy remains separate; see r2-command-copy.md.

Legacy scene-bearing checkpoint v3: ordinary restore still fails closed. Schema v15
adds explicit OWNER `recover-legacy-content PROJECT STAGE --expected-revision VERSION
--checkpoint-sha256 DIGEST --allow-legacy-content`. This revalidates only matching
canonical scenes and creative configuration as a genuinely NEW generation, preserves
source rows/checkpoint bytes/attempt budget and invalidates old evidence/jobs/finals.
Historical audio/cues/documents were absent from v3 and cannot be reconstructed.
Exact inventory and managed byte identity checks apply. See
[legacy-content-recovery.md](legacy-content-recovery.md) for authority, receipts,
rollback, actionable failures and the changed/missing-content decision boundary.
Historical plans/reviews describing manual-only recovery remain historical evidence,
not current command availability or a claim that ordinary v3 compatibility is restored.

Still deferred: broader narration formats, zero-filesystem-write live WAL backend and broader schema signature certification. Recovery CLI independent full re-review remains required. This candidate is for independent review, not whole Phase2 or production acceptance.
