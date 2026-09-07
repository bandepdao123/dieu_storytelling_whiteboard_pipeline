# Capability matrix — current uncommitted Phase2 candidate

This matrix supersedes historical completion wording, not historical test evidence.
No full-production or exact pen-tip acceptance is implied.
Executable local CLI workflow and command/QA contract: cli-media-workflow.md.
init supports --min-scenes/--max-scenes (defaults unchanged). Explicit global
--operational-wal enables WAL inspection with possible SQLite sidecar writes;
strict offline inspection remains default. No checkpoint/journal conversion.

| Capability | Implemented boundary | Public CLI / limitation |
|---|---|---|
| SQLite orchestration | metadata, gates, lineage, epochs, job admission | init/import/plan/pause/resume/retry |
| Strict read-only inspection | current offline rollback-journal DB, mode=ro | status/summary/report/Discord status/dry-runs; live WAL rejected |
| Migration | explicit constructor migration, current v15, inherited real v11/v13 fixture coverage | migrate existing DB, init; additive command-copy claim; not universal schema parity |
| Text/DOCX/XLSX import | actual local extraction (optional libraries for document formats) | import-script; not prompt/semantic planning |
| GDocs/URL import | explicit adapter contract | no CLI adapter injection; not a live fetch implementation |
| Audio import | measured managed PCM WAV ingestion; legacy supplied metadata route | import-narration measures/copies/hashes PCM; import-audio is metadata-only; no broader codecs |
| Cue planning | duration subdivision, exact continuous visual readiness | plan; no gap/outro visual timeline or semantic segmentation |
| Image results | add_artifact, record_image_attempt public APIs | register-artifact, image-attempt; no provider invocation |
| Scene QA/review | subject-bound QAEvidence + decide_scene | scene-qa validated external JSON, approve/reject |
| POST_BATCH | approve_post_batch typed QA/contact subject | post-batch persisted contact + external QA; no synthesis/evaluator |
| Local final assembly | real FFmpeg normalize/concat/mux/probe, same-version evidence reuse | assemble/assemble-final; positional output; real WAV/PPM multi-scene CLI E2E |
| Final QA/review | record_final_qa and evidence-bound final decision service paths | final-qa, final-review; explicit artifact subject required |
| Checkpoints | v4 capture/plan identity, strict inventory restore | checkpoint-create, restore-checkpoint, restore-artifact; OWNER recover-legacy-content opt-in matching-content NEW generation only; historical sources absent, see legacy-content-recovery.md |
| Rerun | OWNER-reviewed dependency invalidation and epoch semantics | rerun-propose, rerun-decide, rerun-reapprove, rerun-apply; no execution worker |
| Copy ownership | top-level exclusive-copy registration/compensation | nesting rejected; crash/incomplete unknown copies retained/manual review |
| Publication recovery | bounded intent/journal recovery, dirfd/no-replace/fsync ordering | recover-publications; not unknown-file sweeping or physical power-loss proof |
| Discord | local parser/RBAC/idempotent receipt; bounded durable scene-copy claim | scene replacement supported at top level; attempt/lease fence, atomic mutation/result; expired/failed attempts manual review; see r2-command-copy.md; no live gateway |
| Sheets/Drive | local adapter contracts and configured client factory entry points | dry-run available; live clients/authorization/retry/delivery ledger gaps remain |
| Worker | job metadata/admission and deterministic fake outcomes | no production claim/heartbeat execution worker |
| Image provider | request default Codex OAuth/gpt-image-2/high/no fallback | no live generation command implemented or invoked |
| Precision whiteboard | absent | IMAGE still rendering is not hand/pen-tip synchronization |
| Token accounting | serialization/query-count benchmark only | no measured token savings or provider-cost telemetry claim |
| Retention | inherited TTL semantics | FINAL_VIDEO vs FINAL unresolved; no policy changes |

Output: fixed H.264 MP4, 1920x1080, 30fps, yuv420p, AAC, hard cuts. No custom
transition/hold/reveal. Default hold 1.5 is metadata only. Timing and publication
limits: final-assembly.md. Exact remaining engineering inventory: phase2 plan.
Sheet tab names/headers are authoritative in adapters.py and integrations.py;
frozen v1 documentation is not current schema authority.
