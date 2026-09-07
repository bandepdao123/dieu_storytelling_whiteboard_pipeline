# Legacy checkpoint content revalidation — schema v15

This is an explicit OWNER opt-in, not backward-compatible historical restoration.
Ordinary restore-checkpoint is unchanged: v4 identity/inventory checks apply;
scene-bearing v3 fails closed by default. Original snapshot bytes are never edited.

```
du-pipeline --db BACKED_UP_CURRENT_DB --role OWNER recover-legacy-content PROJECT STAGE --expected-revision CURRENT_VERSION --checkpoint-sha256 ORIGINAL_CANONICAL_DIGEST --allow-legacy-content
```

Python: Pipeline.recover_legacy_checkpoint_content(project_id, stage,
expected_revision=version, checkpoint_sha256=digest, allow_legacy_content=True,
role=Role.OWNER). CLI role selection is a local assertion, not authentication.
Back up DB and managed media together before explicit migrate to schema v15.
Select the latest successful stage and inspect its original checkpoint_json and
snapshot_sha256 through read-only DB inspection on that backup; status exposes
current project.version. The digest argument is the canonical snapshot digest,
not SHA256 of the JSON file. The returned/persisted receipt also binds exact JSON
bytes, stage row/job identity, project, expected version and new generation.

## Supported, deliberately narrow case

The actual schema-v12 producer did not capture audio, cues or normalized-document
rows in v3. Recovery cannot recover those historical sources, nor certify that
current narration is the historical narration. It leaves all current source rows
and bytes untouched and hashes/pins the current managed narration. It validates
continuous scene timing against that current narration's declared duration; it
is not a decoder or historical measurement certification.

Only canonical scenes already matching the snapshot exactly (ID, ownership,
code, order, timing, text, special flag, duration exception) are revalidated.
Language, style, references and character bible are recovered from the canonical
snapshot. Provider/output/seed/transition/range/retention settings are not changed.
Replaced/missing/different scene sets, expanded or inconsistent artifact inventory,
detached provenance mismatches and unavailable or tampered required bytes reject.
No integer/text equality is treated as historical generation proof. A fresh
SCENES_PLANNED event assigns a genuinely new generation without an image epoch.
This does not import different historical scenes over a newer plan.

All active artifacts (including finals) become superseded; QA/continuity/checkpoint
execution state is cleared and scenes become PLANNED. Old approvals are revoked;
runnable jobs are blocked. Lifecycle is preserved, not resumed. Old attempts,
epochs, stages, final records and event history remain. Attempt budget is not
replenished. Fresh image registration/QA/reviews and current POST_BATCH/final gates
are required; an exhausted image budget needs the separate explicit OWNER rerun
lifecycle. Recovery itself neither queues execution nor invokes a provider.

## Failure and operational guidance

- Missing opt-in/OWNER: no recovery. Supply the explicit flag only after reviewing
  these limits; do not try another role as an authorization bypass.
- Stale revision/checkpoint or inventory: re-inspect current state and the latest
  original stage. Do not automatically retry with a newly fetched digest/version.
- Checksum/ownership/scene mismatch or missing bytes: use an intact verified backup
  or a separately reviewed new import/plan. Never relabel v3, rewrite its digest,
  alter IDs or normalize SQL rows to force acceptance.
- Replay: the same stage row cannot be recovered again, even with a new revision.
  Use a fresh v4 capture after successful revalidation.
- CLI errors retain fixed sanitized category messages (exit 2); service errors give
  the specific validation reason. This document supplies the actionable triage;
  CLI does not expose arbitrary snapshot contents or exception diagnostics.

Validation uses canonical digest, project/job ownership, exact immutable inventory,
managed-path byte checks and file identity pins. A writer-held full state recheck
rejects snapshot substitution, source/inventory/lifecycle races. Receipt and SQL
mutation share a transaction/savepoint, including caller outer rollback; no files
are created, copied, overwritten or deleted. This is not hostile-UID filesystem
locking or hardware power-loss certification.

## Compatibility decision boundary

This candidate supports matching-content revalidation only. If the requirement is
restoration of changed/missing historical audio/cues/documents or a different scene
set, a user decision and separately specified import mapping/provenance policy are
required. No such compatibility is claimed. Historical review C2 remains evidence
of ordinary v3 restore rejection, not a passing recovery acceptance record.

Tests: tests/test_scope_c_legacy_content.py extracts actual committed 929511e source
locally, produces and baseline-restores a schema-v12/v3 fixture, then upgrades it.
It never relabels a modern snapshot. Independent targeted re-review has completed;
the owner accepted this bounded recovery scope for Phase2 publication. See
[phase2-acceptance.md](phase2-acceptance.md). This does not add broader historical
restoration support.
