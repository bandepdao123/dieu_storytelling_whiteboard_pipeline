# Phase 1 rerun and scratch contracts (review candidate)

This is local orchestration, not a provider/worker/delivery activation. Image default
remains Codex OAuth gpt-image-2 high, no fallback. No retention change.

## Rerun impact

An OWNER-approved proposal is consumed once inside the mutation transaction.
Unknown stages or inactive projects fail without consuming approval. `plan` aliases
`planning`. No provider or media execution occurs merely by creating RERUN jobs.

| Stage | Superseded ACTIVE artifact kinds | Image QA / budget |
|---|---|---|
| planning / image | IMAGE, CONTACT_SHEET, ANIMATION, SCENE_VIDEO, FINAL_VIDEO | PENDING; PLANNED; new approved epoch |
| qa | CONTACT_SHEET, ANIMATION, SCENE_VIDEO, FINAL_VIDEO | PENDING; no new budget |
| animation | ANIMATION, SCENE_VIDEO, FINAL_VIDEO | retain image QA; return ANIMATED to IMAGE_READY |
| assembly | FINAL_VIDEO | retain image QA and scene approvals |
| upload | none | unchanged |

INPUT/BIBLE and other independent upstream assets remain untouched. Files are not
deleted by rerun. Animation invalidates SCENE decisions only where clips/state
changed, since the existing scene digest includes those fields. Upstream reruns
revoke SCENE/PILOT and dependent project approvals. Every media rerun revokes
POST_BATCH/BATCH/FINAL/UPLOAD and increments global execution version; all
old-version active jobs are blocked with lease ownership cleared. The typed map
also records which jobs are dependency descendants, separately from conservative
whole-version fencing. Historical job versions are never rewritten as current.
Upload-only rerun blocks upload jobs/revokes UPLOAD only, preserving final review.
Remote receipt history is not deleted or falsely marked undelivered. There is no
live delivery gate/worker integration in this phase.

Assembly rerun requires fresh POST_BATCH authorization under the existing exact
version gate. It retains contact-sheet bytes, but the dedicated approval use case
registers a new receipt/artifact. Existing destination bytes are never overwritten;
use a new output name. Old finals become SUPERSEDED and cannot satisfy reuse.
No cross-version cache equivalence is promised.

## OWNER decision provenance (B1)

`decide_rerun` approval AND rejection are OWNER-only; generic REVIEWER scene
approval permissions do not authorize rerun decisions. OWNER `apply_rerun` is a
separate consumption action, never an implicit OWNER approval.

Each decision transaction stores a `rerun-decision-v1` RERUN_DECIDED receipt in the
existing append-only event ledger. It binds the proposal ID, complete decided
proposal snapshot (project, exact stage, creation identity, actor and state), OWNER
role, and exact project execution version at decision time. Apply validates the
latest matching receipt and current version inside the same write transaction as
consumption, invalidation, job creation and epoch allocation. A changed revision,
proposal, missing/invalid receipt, or non-OWNER receipt fails closed; no fallback to
an older approval. IMAGE_EPOCH_STARTED and RERUN_APPLIED link `decision_event_id`.
An unrelated project does not affect this revision fence.

No schema change or startup rewrite is required: both actual v11 pending approvals
and earlier v12 candidate approvals lack these receipts and therefore cannot grant
new epochs, even if the historical actor string says "owner". Historical proposal
rows and events remain untouched on open/migration. After reviewing current scope,
an OWNER can explicitly make a fresh decision through the public API:

```python
pipeline.decide_rerun(proposal_id, True, 'owner-identity',
                     role=Role.OWNER, reapprove=True)
pipeline.apply_rerun(proposal_id, role=Role.OWNER)
```

`reapprove=True` is only for pending APPROVED proposals (legacy, unproven or stale,
or an explicit refreshed decision); omission does not overwrite an approval.
The receipt preserves the complete previous proposal row, including historical
actor/state, and links any previous decision event. False explicitly rejects that
pending proposal. REJECTED/APPLIED proposals cannot be reopened by this flag.
Old receipts are never updated/deleted; migration does not infer OWNER from actor
names or application roles. Decision/audit failures roll back together. This is
verifiable provenance at the trusted local service/append-only DB boundary, not a
cryptographic signature or protection against arbitrary DB administrator forgery.
Malformed unidentifiable ledger records fail closed and require separate diagnosis;
this API does not repair corrupted audit records or retroactively revoke already
applied historical epochs.

## Attempt epoch and schema v12

Legacy UNIQUE(scene_id,number), CHECK number 1..3 and ON DELETE CASCADE cannot
represent renewed budgets while retaining attempts. Atomic migration rebuilds only
attempts: keeps IDs/all original fields, adds epoch=0, project_id and scene_code;
scene deletion detaches scene_id instead of deleting history. Migration remains
inside the constructor's existing BEGIN IMMEDIATE, with integrity/FK verification.
Historical attempts DDL fixture and injected rollback/reopen tests cover this
change, not every historical schema variant in the broader audit.

Each applied planning/image rerun appends IMAGE_EPOCH_STARTED. Its event ID is the
epoch; three attempts maximum per project/scene-code/epoch. Normal replan,
checkpoint restore, QA rerun or retry cannot reset that budget. Detached historical
attempts still count for their scene code in the current epoch. Count/insert/state
transition now share a write transaction. Epoch is project-wide because proposals
have project-wide scope. There is no cap on independently OWNER-approved epochs;
there is no automatic epoch renewal. No attempt rows are deleted to reset limits.

## Explicit attempt-owned scratch

```
receipt = pipeline.allocate_attempt_scratch(scene_id, b'failed payload')
pipeline.record_image_attempt(scene_id, False,
    scratch_receipt=receipt['token'], error='local failure')
```

The API accepts bytes and creates/seals its own exclusive `.attempt-scratch/<token>/payload`
name with mode 0600. It does NOT accept a caller-selected path for deletion or
provide a writable provider workspace. Receipt binds project, scene ID/code, epoch,
next number, execution version, inode identity, digest and size. Returned fields
are informational: consumption fetches the canonical receipt by token from events.
Version/epoch/owner/number mismatches and reused tokens reject. Several allocations
may target the same next number, but only one can be consumed; unused allocations
are retained, never swept automatically.

Append-only journal sequence:
1. SCRATCH_ALLOCATION_PREPARED committed before exclusive creation.
2. File flush/fsync, parent fsync, safe FD snapshot, SCRATCH_ALLOCATED committed.
3. Attempt row/state plus SCRATCH_CONSUMED committed atomically.
4. Under the F01 SQLite reconciliation lease: SCRATCH_RETIRE_PREPARED committed.
5. Revalidate exact identity/hash, references and link count; pinned unlink + parent
   fsync; append SCRATCH_RETIRED. Crash after unlink is reconciled as absent after
   committed intent. Repeated recovery does not duplicate terminal receipt.

Only consumed FAILED scratch can be retired. Successful, unconsumed, unsealed,
wrong-inode, unknown bytes, symlinks, multiply-linked payloads and all artifact
history/narration/publication/delivery references are protected. Global URI and
inode alias observations are protection-only; no alias pathname supplies bytes
for reading or unlinking. Any ambiguous namespace or reference error retains bytes.
F01 no-follow traversal/snapshot/unlink and lease primitives are reused unchanged.
`reconcile_deletions` runs scratch recovery before existing artifact recovery;
it does not apply TTL. Scratch operations require independent transactions so an
outer rollback cannot undo ownership intent after filesystem deletion.

Caller-supplied `failed_binary` remains nondeleting. Diagnostic metadata uses pinned
no-follow regular-file FD reads; external paths, symlinks and unsupported leaves
produce no digest rather than reading arbitrary targets. Cannot combine it with
`scratch_receipt`.

## Limits and acceptance holds

- Local trusted DB/service boundary, not hostile same-UID/kernel isolation. Final
  identity check and unlink are not a kernel-atomic compare-and-delete; OS directory
  permissions remain necessary. Parent swaps are pinned; ambiguous replay retains.
- File and leaf-parent fsync do not establish complete directory-tree/power-loss
  durability. Tests inject deterministic exceptions, not physical power failure.
- Unsealed/unused/protected scratch and empty directories accumulate. No arbitrary
  orphan sweep, retention policy, production cleanup or worker cancellation policy.
- Alias protection scans registered references; no production-scale latency claim.
- F10/F11/F12 and other later-phase findings remain outside this remediation.
- Independent Phase 1 review and final release verification are still required;
  green tests alone are not phase or production acceptance.
