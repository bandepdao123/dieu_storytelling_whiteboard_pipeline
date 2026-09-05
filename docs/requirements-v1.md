# Phase 1 Requirements (frozen v1)

Status: frozen 2026-09-05. Changes require a new version and approved impact proposal.

## Scope
A runnable orchestration foundation, not a precision whiteboard/render engine. Vietnamese and English projects; 50–360 scenes; one human narrator; supplied audio and SRT, with audio as canonical clock. Strict mismatch blocks planning (never silently realigns).

Inputs: text, DOCX, Google Docs, Excel, SRT, audio, URL. Phase 1 local adapters validate/normalize metadata; remote retrieval is an interface without credentials or network side effects. Research/script generation is optional.

Per project, ask/record style, references, continuity bible, image provider (default `codex-gpt-image-2`), whiteboard mode (default `ask`), transition (default hard cut), and deterministic random seed. Reference/bible continuity is strict.

## Workflow and safety
- SQLite is authoritative. Sheet, Discord and Drive are adapters.
- States cover project, job, stage, scene and generation attempts.
- Artifacts are checksummed, versioned, and linked to parents.
- Events, observed costs, elapsed time and learning metadata are append-only. Cost is observation only, never billing truth.
- Scene duration: timeline 0–180 s targets 5–8 s; thereafter 10–20 s. Semantic boundaries are soft, audio timing remains canonical.
- Approval gates: S001–S005 and configured representative/special scenes; after a batch produce contact-sheet/AI-QA metadata and require human approval.
- Image generation has at most three attempts per scene; then that scene is BLOCKED while independent scenes continue.
- Dependency changes create impact proposals and cannot apply before approval.
- Failed binary payloads are deleted immediately while metadata remains. Other local binaries expire after 3 days.
- Pause/resume and role-controlled Vietnamese no-accent `du-*` commands are supported.

## Output contract
Target metadata: 1920×1080, 30 fps, 16:9, H.264 MP4; no music, burned subtitles, or logo by default. Final hold is 1–2 seconds inside scene duration. Transition is project-selectable, default hard cut. Phase 1 does not claim final rendering or ultimate drawing precision.

## Integrations and roles
RBAC roles: OWNER, REVIEWER, OPERATOR. Google Sheet has exactly 11 prescribed tabs; Drive has prescribed folders. Adapters have no live API side effects or secrets in Phase 1. Image and animation providers are plugins.

## Acceptance
Python 3.11, minimal dependencies, CLI and automated unit/integration tests. Commands initialize, import metadata, plan, inspect, pause/resume, retry, approve/reject, and report cost/errors. No OpenMontage code is copied.
# Frozen source-requirement coverage

This frozen v1 contract requires: semantic 5–8 second scenes before minute 3 and
10–20 second scenes thereafter; normally 50–360 scenes (explicit per-project
fixture override only); deterministic project seed; S001–S005 and designated
representatives approved before batch generation; AI QA, contact sheet, and
human approval before animation. Retry is queued without inventing an attempt.
Jobs, stages, scenes and attempts are checkpointed; reruns require proposals and
approval; artifacts/project versions support restore/resume.

Inputs are normalized text, local audio/SRT metadata, optional DOCX/XLSX readers,
or Google Docs/URL adapters. Exactly one human narrator is metadata, while
research/script providers are optional protocols. Strict style, character,
environment, prop and time-state bibles and references attach to generation.
Output is 1920x1080, 30 fps, 16:9 H264 MP4 yuv420p, no music/subtitles/SRT/logo,
with a 1–2 second final hold inside narration duration and hard-cut default.
Retention defaults to three days per project; cleanup preserves metadata and
deletes binaries. SQLite enforces FK/check/state constraints. Discord `du-*`, CLI
and service all enforce OWNER/REVIEWER/OPERATOR. Scheduling targets 4 CPU/3.8 GB
using bounded image/animation/light semaphores. Integrations remain adapters and
perform no provider/network side effects.
