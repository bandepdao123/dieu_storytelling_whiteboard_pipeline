# Local final assembly

The public API is `Pipeline.assemble(project_id, output, dry_run=False)`. Output and
all inputs must resolve beneath the project's managed artifact root; symlink and
traversal escapes are rejected. Scenes are selected by database `ord`, never names.
ANIMATION/SCENE_VIDEO is preferred; an IMAGE is accepted only through a real ffmpeg
still render. Intermediate files live in a private `.assembly-*` directory and are
removed on every exit. Final bytes are atomically renamed and registered only after
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
ownership, F12 durability and F15 retention policy remain unresolved. No production,
provider, delivery or exact pen-tip whiteboard acceptance is implied.

## Atomic publication filesystem requirement

Final publication uses Linux `renameat2(..., RENAME_NOREPLACE)` so a destination
created concurrently can never be overwritten. The artifact root and requested
output must therefore be on the same local filesystem, and that filesystem and
kernel must support this operation. Cross-filesystem (`EXDEV`) and unsupported
(`ENOSYS`/`EINVAL`) operations fail closed; there is no copy, hard-link, or
check-then-rename fallback. Destination collisions are journaled as `ABORTED`, leave the destination untouched, retain the durable staged bytes, and do not register a `FINAL_VIDEO`.

Hệ thống không dùng fallback kiểu check-then-rename, copy hoặc hard-link vì các cách đó không duy trì hợp đồng publication bất biến.
