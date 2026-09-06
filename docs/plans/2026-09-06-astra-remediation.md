# Astra Remediation Implementation Plan

> For Hermes: use the subagent-driven-development review sequence (specification then quality), executed directly in this session. User explicitly prohibits delegation, model switching, commits and pushes; those prohibitions override the skills' default delegation/commit steps.

Goal: remediate audit F01–F08 only, with proven RED/GREEN regressions and public-API real-media acceptance. This document is a plan, not a completion claim.

Architecture: separate managed-filesystem deletion authority, scene-local evidence, project execution fencing, assembly input identity and final output review identity. Maintain SQLite ownership constraints and atomic migrations. Share pure canonical projection builders between actual gates and batch-loaded summary; do not substitute dashboard-only hash fixes for workflow fixes.

Tech stack: Python 3.11+, SQLite, pytest, Linux O_NOFOLLOW/dirfd/renameat2(RENAME_NOREPLACE), local FFmpeg/ffprobe. No network/providers/uploads or production DB.

## Baseline and work controls

- Repository: /home/hermes/work/dieu_storytelling_whiteboard_pipeline.
- Audit and verified starting HEAD: 3bfe33dbf82e520fa3d30e73f55e000d37e3c051.
- Initial `git status --porcelain=v1` empty; `git diff --stat` empty.
- Full audit read: /home/hermes/work/astra_whiteboard_audit_20260906.md.
- Baseline: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`: 130 passed in 11.56s.
- Before every implementation group compare HEAD and status with own known writes. Stop on unexplained changes; never reset another actor's work.
- Runtime test data, benchmark cwd, build copy and installed-wheel virtualenv go in temporary directories outside repo. Disable bytecode and pytest cache.
- No license choice, credential reads, live provider calls, uploads, production DB access, settings changes, commits or pushes.

## Phase 1 (only authorized implementation)

### Task 1 — F03: canonical projections, no revision-policy change

Files: create src/du_pipeline/evidence.py and tests/test_astra_phase1_evidence.py; modify src/du_pipeline/service.py and affected documentation.

1. Write public-workflow fixture: init with one scene, import managed audio and SRT, plan, register nonempty IMAGE, record attempt, typed QA, sequential approval. Assert `_pilot_ready` and summary pilot current agree at equal project version. Add superseded image via replacement lineage and project-level CONTACT_SHEET (null scene_id); confirm unrelated fields never enter scene projection.
2. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider tests/test_astra_phase1_evidence.py` and capture expected false summary despite true actual pilot.
3. Extract exact existing project/scene/artifact field tuples into pure builders. `scene_evidence_hash(scene, artifacts)` selects only id/kind/sha256/version/parent_id/status from ACTIVE artifacts of that scene, sorted by id. `manifest_evidence_hash(project, scenes, artifacts)` preserves existing global payload semantics in this task. Both use one canonical digest function. No schema/hash envelope change; existing valid stored digests remain valid.
4. Route `_scene_evidence_hash`, `_manifest_hash` and summary through shared builders, preserving batch reads and SELECT ceiling. Do not remove global version comparisons in this task; F02 remains a separate explicit design/migration.
5. GREEN focused tests, full regression, diff check, spec and quality inspection. Document exactly the remaining F02 distinction.

### Task 2 — F01: deletion authority pinned before filesystem mutation

Files: src/du_pipeline/atomic_fs.py, src/du_pipeline/service.py; create tests/test_astra_phase1_deletion.py; existing cleanup/reconciliation tests.

1. Read all cleanup, reconciliation, rollback and publication tests before editing. Reproduce P11 with deterministic parent-swap hook at lease assertion; outside victim must survive.
2. Add RED tests for cleanup, rollback and recovery parent swaps, root/leaf symlinks, destination collision, FD closure and unrecognized staging bytes. Assert original tracked bytes are either safely retained/restored or journaled; never overwritten.
3. Pin root and traverse relative components using O_DIRECTORY|O_NOFOLLOW. Validate leaf names; use relative stat/open/hash/rename_noreplace_at/unlink only. Compare regular-file identity before mutation; fail closed on replacement/unknown identity. Recovery walks through pinned FDs rather than re-resolving paths from os.walk.
4. Preserve lease assertion within BEGIN IMMEDIATE and shared-reference retirement logic. Use journaled ownership, expected digest/identity and deterministic recovery states; no arbitrary suffix-based ownership inference for new deletion operations.
5. GREEN targeted adversarial and existing lease/cleanup tests; run full regression. F15 immutable list/TTL policy unchanged.

### Task 3 — F05: failed scratch ownership and journal

Files: src/du_pipeline/db.py, src/du_pipeline/service.py, deletion tests.

1. Reproduce P13: active managed IMAGE passed as failed_binary must not disappear. Add tests for narration, historical references, hardlink aliases, external paths, wrong scene/attempt receipt, replay and crash between attempt commit/deletion.
2. Define dedicated attempt scratch allocation/receipt API with exclusive names and durable owner project/scene/attempt identity. Caller path alone is not deletion authority. Existing arbitrary failed_binary calls preserve bytes without ownership proof; metadata can report retained unowned input honestly.
3. Add additive schema migration inside existing single BEGIN IMMEDIATE; no executescript, no resetting approvals or silently deleting artifacts. Test upgrade rollback on injected failure and reopen idempotence.
4. Journal approved scratch deletion with pinned FD operations from Task 2. Check all references inside write transaction before retiring, including audio/publication/input aliases; never treat ACTIVE artifacts as failed scratch.
5. GREEN and full regression. Update failed-binary contract; no claim that arbitrary failed_binary is safely auto-deleted.

### Task 4 — F02/F04: explicit scene-local revisions and QA subjects

Files: src/du_pipeline/db.py, src/du_pipeline/evidence.py, src/du_pipeline/service.py, tests/test_astra_phase1_evidence.py; new migration tests.

Design to implement and test before removing any gate version comparison:
- Project version remains monotonic execution/CAS fencing for jobs, publication preparation and project-level approvals.
- Scene evidence revision identifies that scene's relevant content/QA dependency set. Approval of a sibling changes project execution revision but not this scene's dependency revision. Scene decisions revoke earlier decisions for that subject; do not keep old APPROVED as a backdoor after rejection.
- Global input/config mutations invalidate all affected scene approvals/QA; scene-local changes invalidate only affected scene approvals plus downstream global approvals/jobs/outputs.
- Persist QA subject artifact ID/hash (and relevant dependency revision). Gates compare it with the current eligible subject, not only qa_state. Replacement resets qa_state=PENDING and qa_json, supersedes derived scene animation and final outputs, and rejects stale approvals.
- Missing legacy subject/revision fails closed, requiring fresh QA/approval; do not silently convert old QA into evidence for current assets.

Steps: RED public two-scene sequential QA/approval followed by post-batch/dry-run; replacement stale-subject tests; migration atomicity tests. Implement additive revisions and subject binding, then unify `_pilot_ready`, assembly and summary predicates. Keep batch loading without N+1. GREEN/full regression and document migration behavior.

### Task 5 — F06: separate input identity from output receipts

Files: src/du_pipeline/evidence.py, src/du_pipeline/service.py, tests/test_astra_phase1_assembly.py, docs/final-assembly.md.

1. RED same-input/same-destination assembly reuse, nonempty input histories, final publication not invalidating POST_BATCH; input alias/tamper/no-overwrite tests.
2. Define versioned input evidence: canonical source audio, selected current scene visuals/lineage, scene QA approvals, contact-sheet/batch QA provenance, relevant config/tool contract. Exclude FINAL_VIDEO/output history and FINAL review decisions. Keep requested destination and publication request identity distinct from reusable media identity; specify same-destination reuse first, do not imply arbitrary relocation reuse.
3. Verify an existing matching final before broad output-alias rejection, but require current authorized inputs and verified output digest. Any other existing destination, input alias or stale output must fail closed. Preserve publication version checks and registration transactions.
4. Persist typed POST_BATCH QA payload/subject rather than only passed boolean.
5. GREEN fake failure-injection tests and real-media tests, full suite.

### Task 6 — F07: typed rerun dependencies

Files: src/du_pipeline/service.py, src/du_pipeline/evidence.py (or focused dependency module), src/du_pipeline/db.py if attempt epoch needed; tests/test_astra_phase1_rerun.py.

1. RED stage/kind matrix: planning/image/qa/animation/assembly/upload against IMAGE, CONTACT_SHEET, ANIMATION, SCENE_VIDEO, FINAL_VIDEO and delivery receipts. Reproduce ACTIVE FINAL_VIDEO after assembly rerun.
2. Explicit DAG mapping, not kind.lower() comparison. Invalidate affected approvals/jobs/output revisions; retain independent upstream subjects. Rerun image resets execution readiness and QA, without deleting attempt audit. Model attempt budget by approved generation epoch if resetting budget is required; preserve unique identity/history and test legacy migration.
3. CAS proposal state within same transaction, preserve owner approval and no implicit execution. Unknown stage fails with no mutation.
4. GREEN matrix/replay/failure tests and full regression.

### Task 7 — F08: actual final review

Files: src/du_pipeline/contracts.py, src/du_pipeline/service.py, src/du_pipeline/db.py if receipt columns needed; tests/test_astra_phase1_final_review.py; README/contracts/final-assembly docs.

1. RED approve FINAL with no final output; wrong project, stale input version, tampered output/manifest, rejected QA and replay tests.
2. Dedicated final review accepts explicit artifact subject and typed review evidence; verify ACTIVE FINAL_VIDEO ownership, stored manifest integrity, actual output checksum and current input evidence/version. Persist reviewer, decision, subject ID/hash and manifest hash, QA payload, revision.
3. FINAL summary reads same final-evidence builder. Generic PILOT/BATCH commands either delegate to complete dedicated workflow with required evidence or reject actionably; never return success for evidence-free approvals. Update dispatcher command contract honestly.
4. GREEN and full suite; no delivery or upload activation.

### Task 8 — required integration exit test

Create tests/test_astra_phase1_public_e2e.py.

Use ONLY public Pipeline methods for project/scene/QA/approval fixtures (no SQL writes and no private helper for approval fabrication). Obtain managed root through public status. Generate narration and at least two actual image/clip assets with local FFmpeg in tmp_path; import exact continuous timing.

Sequence: sequential scene import/attempt/typed QA/human approval -> verify summary -> POST_BATCH typed QA/contact sheet -> dry-run (no mutation) -> real assembly -> same-input same-output reuse -> explicit final approval. Assert output ffprobe contract and IDs/lineage/checksums. Replace one scene asset; assert QA PENDING, stale derived outputs not ACTIVE, final review/reuse rejected until fresh evidence is supplied. This test must actually run when accepting Phase 1, not pass via skipped media tools.

### Task 9 — release verification and handoff

1. Full regression: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest -q -p no:cacheprovider`.
2. Benchmark from mkdtemp cwd: `PYTHONDONTWRITEBYTECODE=1 python /home/hermes/work/dieu_storytelling_whiteboard_pipeline/scripts/benchmark_status_summary.py`; call it serialization/query count only, not latency/token savings.
3. Copy only tracked working-tree files plus intended untracked source/tests/docs into a temporary source tree (git archive HEAD alone would omit fixes). `python -m build --no-isolation` there. Install resulting wheel into fresh temp venv with `pip --no-index --no-deps`; execute installed CLI --help outside checkout and verify installed module path.
4. `git diff --check`, HEAD/status review. Leave changes uncommitted. Update README/contracts only to describe implemented behavior, clearly mark partial completion. No production-ready claim.
5. External report: /home/hermes/work/astra_phase1_implementation_20260906.md with exact files, RED/GREEN outputs, commands, baseline/final HEAD, migration rationale, risks, outstanding tasks. If budget ends, finish current coherent task and explicitly mark the rest unimplemented.

## Later phases — planning only; do not execute

- Phase 2: F09/F16 timing/capabilities; F10/F11/F17 transactional preconditions/savepoints/schema parity; F12 publication orphan/durability; F18 CLI lifecycle. Separate approval required.
- Phase 3: F13/F14 and G01/G03 worker/integration/delivery contracts; no live activation under this task.
- Phase 4: G02 precision whiteboard engine/creative production and F19 release governance, broader F20 documentation cleanup. License is owner's decision.

## F15 decision pending — explicit hold

Do not silently add FINAL_VIDEO to immutable retention or otherwise change its TTL. Existing FINAL vs FINAL_VIDEO inconsistency remains a documented risk until owner chooses pin-until-delivery, explicit TTL or another retention class. Phase 1 acceptance does not authorize automatic production cleanup or certify safe final delivery retention.

## Progress ledger

- Baseline/full audit and primary runtime code read: complete.
- Tasks 1/2/4/5/7 and initial Task 8 are inherited reviewed test candidates; see external report milestones 1–14, not a production claim.
- Task 6 first GREEN: explicit typed map, budget 3 per owner-approved epoch, v12 historical attempts migration preserves audit on replan. 10 focused / 235 full tests pass; expanded rerun/E2E acceptance pending.
- Task 3 first GREEN: sealed bytes allocation, ownership/consumption/retirement receipts in append-only events, F01 FD primitives, crash/replay integration. 13 focused / 248 full tests pass; expanded safety verification pending. No scratch table migration. Original Task 3 additive-DDL step is superseded by this existing-ledger design.
- Tasks 3/6 expanded verification: 41 focused passed; full 267 passed in 39.01s; public real-media rerun/epoch/scratch E2E ran without skips. Review candidates only.
- Task 9 current-tree benchmark, temporary sdist/wheel build, isolated install/CLI smoke and diff check passed; see report milestone 17. Independent specification/security/migration/ownership review and acceptance remain outstanding. Phase 1 is NOT complete.
