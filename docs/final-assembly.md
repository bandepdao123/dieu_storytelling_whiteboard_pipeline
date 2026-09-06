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
