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
## Atomic publication filesystem requirement

Final publication uses Linux `renameat2(..., RENAME_NOREPLACE)` so a destination
created concurrently can never be overwritten. The artifact root and requested
output must therefore be on the same local filesystem, and that filesystem and
kernel must support this operation. Cross-filesystem (`EXDEV`) and unsupported
(`ENOSYS`/`EINVAL`) operations fail closed; there is no copy, hard-link, or
check-then-rename fallback. Destination collisions are journaled as `ABORTED`, leave the destination untouched, retain the durable staged bytes, and do not register a `FINAL_VIDEO`.

Hệ thống không dùng fallback kiểu check-then-rename, copy hoặc hard-link vì các cách đó không duy trì hợp đồng publication bất biến.
