# Architecture v1 (frozen)

`du_pipeline` is a ports-and-adapters Python 3.11 package.

- **Domain/policy**: enums and duration, validation, approvals, retry/RBAC policies.
- **Application service**: transactional orchestration and invariant enforcement.
- **SQLite repository**: sole source of truth, foreign keys and append-only audit tables.
- **Ports**: image/animation providers, input fetchers, Sheet and Drive contracts. Phase 1 adapters only describe/validate contracts.
- **CLI/Discord parser**: inbound adapters invoking the same service policy.

SQLite uses WAL, foreign keys, short transactions and indexes. Scheduler defaults to one image and one animation worker, two lightweight workers, bounded queue 8, memory budget 3072 MiB on a 4 CPU/3.8 GB host. Scene failures do not stop unrelated ready scenes. Artifact bytes live outside SQLite; metadata includes SHA-256, parent/version and retention timestamps.

Security: no credential discovery, storage, or live cloud calls. Commands are checked against role policy. Dependency modifications use proposed → approved/rejected lifecycle.
