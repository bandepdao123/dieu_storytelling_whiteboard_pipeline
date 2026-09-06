# Scene evidence v2 — F02/F04 subset

This is not full Phase 1 or production acceptance. F06 assembly reuse and F08
final review now have an uncommitted implementation described in final-assembly.md.
F07 typed rerun DAG/attempt epochs and full F05 remain incomplete.

## Two revisions, two purposes

- `projects.version` remains the monotonic global execution fence. Scene decisions,
  QA changes and IMAGE registration advance it and block old work. Jobs,
  POST_BATCH approvals and publication CAS still require the exact global version.
- The scene's input dependency revision is a content-addressed SHA-256, persisted
  as `qa_json.dependency_revision`. It covers a versioned projection of canonical
  project configuration (language/style/references/bible/provider/mode/seed/
  transition/scene range/output) and scene identity/content/timing/special/
  continuity/checkpoint/duration exception. It excludes global execution version,
  human decision, QA results and execution state.
- The approval revision remains `approvals.evidence_sha256`: the shared canonical
  scene digest of scene content/state, QA envelope and ACTIVE scene artifacts
  including their IDs, hashes, versions, parent IDs and statuses. No sibling scene
  or global contact sheet is inserted into this local projection.
- A sibling approval or QA update can advance global version without invalidating
  an unchanged scene. A SCENE approval is current only if its non-null positive
  issuance version is not in the future, it is unrevoked APPROVED, the scene is
  approved/ready, the exact local evidence matches, and its QA subject/dependencies
  still match. Reject/reapprove revokes earlier decisions for the same scene.

`scene_qa_current` and `scene_approval_current` in `evidence.py` are the shared
predicates used by pilot, summary and assembly. Summary keeps batch reads, no
per-scene SQL, and its existing eight-SELECT ceiling. Pilot `evidence` is now
`SCENE_DEPENDENCY_CURRENT`; project gates retain `CURRENT_VERSION_ACTIVE`.
`active` counts unrevoked scene approval records; `current` validates evidence.
Polling validates database evidence, not media-file contents; recording QA and
new human approval verify image bytes, and assembly verifies narration/selected
visual bytes and snapshots. Polling is not a disk integrity audit.

## Subject-bound QA and reset rules

`record_scene_qa` keeps its public signature and typed `QAEvidence` argument.
It requires exactly one ACTIVE IMAGE for the scene, verifies the managed bytes,
and binds the new evaluation to that image inside the write transaction:

```json
{
  "schema_version": 2,
  "subject": {"artifact_id": 123, "sha256": "<sha256>"},
  "dependency_revision": "<sha256>",
  "checks": {"visual": true},
  "score": 1,
  "evaluator": "reviewer"
}
```

This records trusted reviewer evidence; it does not invoke or authenticate an AI
provider. The caller must evaluate the current image before submitting QA.

- IMAGE registration (including replacement) resets `qa_state=PENDING`, clears
  `qa_json` to `{}`, revokes the affected scene and project approvals, fences jobs,
  and supersedes that scene's ANIMATION/SCENE_VIDEO and project CONTACT_SHEET/
  FINAL_VIDEO. ANIMATED scenes return to IMAGE_READY for re-QA.
- Replacement preserves immutable old image lineage. Even identical bytes under
  a new artifact ID require fresh QA. Generic IMAGE registration does not guess
  which duplicate is intended: multiple active images fail QA closed.
- QA mutation revokes affected approval/downstream global approvals and supersedes
  derived scene visual/contact/final artifacts; sibling image QA is retained.
- Global input/config invalidation clears QA and revokes approvals. Audio/SRT/
  document changes and replanning already retire the old scene plan. First document
  import after a plan now invalidates it too. Identical imports/config are no-ops.
- Approved planning/image/QA reruns clear QA/revoke approvals as a strictly required
  F04 dependency. This is not a F07 stage/kind mapping or attempt-counter fix.
- Artifact/checkpoint restore retains the existing conservative invalidation and
  checksum rules. Checkpoint QA is only usable when the restored subject and
  dependency projection match; historical human approvals remain revoked.

No TTL, retention class, binary deletion policy, license, provider default or
fallback behavior changes accompany these rules.

## Migration rationale and compatibility

No database schema migration is needed: existing `qa_json` and
`approvals.evidence_sha256` can represent the complete immutable binding without
parallel integer counters that can drift from the dependency payload. The v1
canonical projection field sets remain shared with F03; their QA field now
contains an explicitly versioned envelope. Existing tables, IDs, artifacts and
history are preserved, and opening a database does not invent or rewrite QA.

Missing/legacy/malformed v2 QA is fail-closed even when `qa_state` says PASS and
an old approval hash matches. Operators must record fresh subject-bound QA and
then human approval. Legacy ANIMATED-only data without an eligible IMAGE must
re-ingest/replace its image before re-QA; no automatic conversion of clip review
into image review. API call shapes and database schema are backward compatible;
unsafe evidence-free approval behavior is intentionally not compatible.

Tests include API-only sequential two-scene approvals, real WAV/PPM/FFmpeg
assembly, sibling preservation, replacement/config/input/rerun invalidation,
wrong/legacy subjects, corrupted dependencies, revoked/future approvals and
reopen. SQL writes in corruption tests deliberately damage evidence, never
manufacture acceptance approvals. Existing media/lease/collision safety fixtures
now supply subject-bound prerequisites rather than fabricate PASS.
