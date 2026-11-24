# Audit and concurrency

## Audit log

`audit.jsonl` holds one JSON object per line. Each entry contains:

- `seq`, `timestamp`, `task_id`, `event`, `actor`;
- `outcome` (`applied` or `rejected`), `from_state`, `to_state`, `reason`;
- `evidence_refs`, `facts_fingerprint`, `task_version`, `task_digest`;
- `prev_hash` and `entry_hash`.

Refused commands are logged too, with the error code in the reason, so attempts are as visible as successes.

### Integrity mechanisms

| Mechanism | Detects |
| --- | --- |
| Hash chain (`prev_hash`, `entry_hash`) | An entry edited, deleted, inserted or reordered |
| Task seal (`task_digest` of the last applied entry) | A task file edited outside the control plane: a forged state, a fake grant, a lowered level |
| Facts fingerprint | Request content changed without an amendment |
| Classification re-check | A stored level that no longer matches the facts, or a policy file changed after classification |
| Evidence digests | Evidence content edited after submission |

`agentgov verify` runs all of them and also checks that each task's state equals the last state its audit trail
records.

**These are hashes, not signatures.** Anyone with write access to the store can rewrite the whole chain and
recompute every hash; `tests/test_audit.py` demonstrates this limit on purpose. Hashes make accidental or naive
tampering evident; they do not replace authentication, signed entries, access control or write-once storage.

## Concurrency

| Risk | Control |
| --- | --- |
| Two writers interleave | Exclusive lock file (`O_CREAT \| O_EXCL`), held only for each read or compare-and-swap write |
| Lost update from a stale copy | Optimistic versioning: commit succeeds only if `version` is unchanged, otherwise `CONCURRENT_UPDATE` |
| Torn files | Writes go to a temporary file in the same directory, then `os.replace` |
| Double authorization | One approval per approver per fingerprint; the transition to AUTHORIZED happens once |
| Duplicate execution | Starting execution consumes the grant inside the same compare-and-swap; later attempts get `DUPLICATE_EXECUTION` |
| Stale authorization | Grants are bound to the review fingerprint and superseded by amendment |
| Stuck lock | Acquisition times out with `LOCK_TIMEOUT` instead of waiting forever |

`tests/test_concurrency.py` races six control-plane instances on threads and four separate OS processes against
the same authorized task: exactly one execution happens and one sandbox is created. Racing approvers record one
approval each and reach quorum once.

### Limits

- The lock and the store are local files: one machine, one directory. There is no distributed coordination.
- If the process dies after starting execution, the task stays EXECUTING with a consumed grant. That is
  deliberate (no automatic retry, so no duplicate run); an operator resolves it with `block`.
- A lock left behind by a killed process must be removed by hand (`.agentgov/.lock`).
