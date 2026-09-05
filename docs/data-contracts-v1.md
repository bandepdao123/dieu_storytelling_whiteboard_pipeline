# Data Contracts v1 (frozen)

## Core records
Project(id, name, language vi|en, state, style_json, references_json, bible_json, image_provider, whiteboard_mode, seed, transition, created_at); Job(project_id, kind, state); Stage(job_id, name, state); Scene(project_id, code SNNN, order, start/end ms, text, special, state, approval_state); Attempt(scene_id, number 1..3, provider, state, error); Artifact(project_id, scene_id?, kind, uri, sha256, version, parent_id?, status, created/expires/deleted timestamps).

Append-only tables: Event, CostObservation(currency/amount/provider), TimeObservation(seconds), LearningMetadata(key/value JSON). Updates/deletes are rejected by SQLite triggers.

AudioMetadata(duration_ms, sha256, uri) and SubtitleCue(index,start_ms,end_ms,text). Validation requires monotonic, non-overlapping, positive cues within audio and terminal drift <= configured tolerance (default 500 ms); failure blocks project.

ImpactProposal(id, project_id, dependency, impact_json, state, proposer/decider timestamps). Approval(id, project_id, scene_id?, gate, decision, actor).

## Sheet tabs (exactly 11)
Projects, Jobs, Stages, Scenes, Attempts, Artifacts, Approvals, Events, Costs, Timings, Learnings.

## Drive folders
`{project_id}/00_inputs`, `01_references`, `02_audio_srt`, `03_scenes`, `04_images`, `05_animation`, `06_qa`, `07_exports`, `99_failed_metadata`.

Provider methods return metadata and paths; they must not return hidden credentials. Binary payloads are implementation-specific. Input kinds: text/docx/gdocs/excel/srt/audio/url.
