# R2 bounded idempotent scene replacement

This candidate supersedes earlier statements that idempotent Discord scene replacement always fails at the nesting guard. Other Phase2 limitations remain unchanged.

`dispatch_discord('du-scene-replace SID PATH', role, idempotency_key=KEY)` uses an additive schema-v15 command-copy claim. Existing successful command_receipts remain compatible. Writable existing v14 databases require explicit migration using the existing migration boundary; inspection does not migrate them.

Protocol:
- Reject caller-owned transaction nesting, validate role/arguments, serialize request identity claim with a unique attempt token and five-minute deadline.
- Capture coherent project/lifecycle, source, plan-event generation, scene, artifact and approval dependencies in the claim transaction.
- Copy with the existing exclusive pinned-dirfd ownership primitive outside the writer. Snapshot/hash no-follow regular copied bytes, fsync file and parent, and persist preparation identity under the current attempt fence.
- Revalidate current lifecycle/dependencies, pinned identity/content-change metadata and request/attempt/deadline in the apply writer. Replace/register via SQL-only primitives, write success receipt and set SUCCEEDED in that SAME transaction. No separate canonical mutation commit.
- A successful replay returns the original receipt even if the source is gone. Same key with different text/role rejects, including pending claims. Concurrent same-key work fails with in-progress rather than waiting; retry after success reuses receipt.

Recovery policy is deliberately conservative: no automatic takeover or lease renewal. Work exceeding five minutes cannot apply. Replaying an expired PREPARING request durably marks MANUAL_REVIEW. Failed attempts are terminal MANUAL_REVIEW as well. The original key cannot be automatically retried after uncertain work; independent review is required. This is recovery classification, not a recovery CLI or automatic prepared-copy resumption.

Before preparation persistence, a hard crash can leave an unknown file with only a request claim. Never infer ownership from filenames or sweep it. After preparation persistence, the recorded identity aids review but is not authorization to unlink/reuse on reopen. Existing live-process compensation may remove only unchanged provably owned unreferenced completed copies; substituted, modified, referenced and incomplete bytes are retained. Crash leftovers are retained in all cases.

No filesystem lock against continuously hostile same-UID namespace/content mutation, arbitrary database modification, hardware power-loss durability, or automatic crash cleanup is certified. Conservative whole-project dependency snapshots may reject unrelated changes. No provider/settings/retention/license changes; measured narration and operator recovery CLI remain separate work.
