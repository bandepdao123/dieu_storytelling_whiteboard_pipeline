# Artifact historical fixtures

artifact_v11_3bfe33d.sql is sqlite3 iterdump of an empty database created by the actual Database constructor read with `git show 3bfe33d:src/du_pipeline/db.py` (SCHEMA_VERSION 11).

artifact_v13_inherited.sql is the equivalent dump from inherited uncommitted db.py at the beginning of this authorized slice, BEFORE changing runtime code (SCHEMA_VERSION 13, including publication_intents and v12 attempts). It is not claimed to be a committed v13 release.

Tests execute these frozen SQL files directly through sqlite3, seed rows under their real original constraints, then open current Database. They do not create current tables and merely change user_version. Runtime migration must not import these fixtures.

Audit: both original artifact tables have NOT NULL checksum but no hex/length constraint; scene-scoped UNIQUE permits NULL duplicates. v11 attempts has required scene owner/CASCADE and 1..3 budget; v13 has nullable detached attempts/SET NULL and epoch constraints. Original project CHECKs and artifact status/foreign keys remain present. Tests assert original artifact fields and DDL constraints remain unchanged apart from additive detached_scene_id, plus repeated reopen and complete logical dump equality after injected failed migration. No historical row is silently normalized.

Only artifact.sha256 is newly validated: 64 ASCII hexadecimal text characters, upper/lowercase preserved, no blob/NUL. Audio, attempts.failed_sha256, publication manual-review payloads, arbitrary event/learning JSON and evidence digests are NOT swept into this contract. Existing direct artifact SQL fixtures were audited; no dummy artifact checksum fixtures required rewriting. Historical uppercase digests remain byte-for-byte unchanged; syntax validation does not certify file availability/content or case-insensitive downstream comparisons.

The repository's initial unversioned 2355790 foundation was inspected: jobs lacks timestamps and attempts lacks failed_path/failed_sha256/failed_size; it is NOT structurally equivalent to supported v11/v13 and is not certified migratable by these tests. No universal historical-version/schema-parity claim.
