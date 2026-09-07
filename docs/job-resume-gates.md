# Current job resume contract (schema v15)

Job records are local scheduling metadata, not a production worker or provider
execution guarantee. `resume_job` and project `resume` validate under the same
SQLite writer transaction as requeue. A historical admission is not a grant to
run against changed current evidence.

All jobs require PAUSED, exact current project version and both lease fields
NULL. Individual resume also requires an ACTIVE project. Project resume requires
PAUSED lifecycle and preflights every current-version PAUSED job before changing
any row or emitting RESUMED. One invalid candidate rejects the whole operation;
old-version jobs remain untouched, never upgraded. Other projects are untouched.
Individual pause retains its existing compare-and-set and does not steal leases.
Project pause retains its existing lease-clearing cancellation fence. There is
no automatic expired-worker-lease takeover API.

Stage-specific prerequisites:

| Job kind | Current gate |
| --- | --- |
| PIPELINE, RERUN:planning | Orchestration; source import may not yet exist |
| RERUN:image | Valid persisted audio/SRT and complete plan; fresh pilot production does not require old pilot approval |
| BATCH_IMAGE | Valid plan and current required pilot/special-scene approvals |
| RETRY_IMAGE:id | Valid plan, matching project scene in RETRY_QUEUED, remaining current-epoch attempt budget; nonpilot/nonspecial scene also requires pilot approval |
| RERUN:qa | Valid plan and every scene IMAGE_READY with exactly one active image subject |
| ANIMATION, RERUN:animation, RERUN:assembly | Valid plan and nonrevoked approved POST_BATCH bound to exact current version and canonical manifest |
| RERUN:upload | Valid plan and current subject-bound final review as reported by status-summary |
| Other kinds | Unsupported; fail closed |

OWNER rerun admission still authorizes invalidation/orchestration, not bypass of
subsequent execution prerequisites. Existing decision provenance, epochs and
historical attempts are not rewritten by resume. Current valid POST_BATCH
re-review can authorize an old same-version ANIMATION job; metadata alone does
not pin an obsolete admission manifest. A generic scene artifact mutation that
makes new animation admission reject also makes resume reject. Generic mutation
does not proactively change QUEUED to BLOCKED; no worker is implemented here.

Regression coverage: tests/test_scope_c_resume.py and
 tests/test_astra_phase2_jobs.py. Fixtures assert external QA explicitly, not
creative quality or valid decoded video. Lease faults use synthetic job lease
fields because no public worker claim API exists; approvals use public APIs.

Limits: all-or-nothing bulk resume may leave a stale-evidence project PAUSED.
No new paused-project evidence-repair or individual job retirement use-case is
provided by this patch. Do not repair by direct SQL or ignore the gate. This
patch does not complete legacy v3 checkpoint recovery or reconcile older
operator documentation; those remain separately tracked Scope C blockers.
