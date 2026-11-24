# Human authorization

This is a **local demonstration control**. Actors are entries in `actors.json` and the CLI takes `--actor` at
face value; there is no login, password, token or signature. It shows where authorization decisions belong and
how they bind to content, not how to authenticate people.

## What an approver approves

Two fingerprints, both SHA-256 over canonical JSON:

| Fingerprint | Covers | Bound to it |
| --- | --- | --- |
| facts | task id, revision, the full request (steps, paths, content, release version) | every evidence item |
| review | facts fingerprint, level, policy digest, requirements, and the id and digest of each required evidence item | every approval and grant |

`agentgov show TASK` prints the review fingerprint. `agentgov approve TASK --fingerprint <fp>` must quote it
exactly; if anything relevant changed since the approver looked, the command fails with `STALE_AUTHORIZATION`.

## Rules enforced in code

- The requester can never approve their own task, even with the approver role.
- Agents can never approve, reject, submit evidence or generate plans.
- Only actors with the `approver` role approve; CRITICAL also needs a `release_manager` among the approvers.
- One approval per approver per fingerprint; repeats get `APPROVAL_REJECTED`.
- When the quorum is reached, the control plane issues one **grant** bound to the review fingerprint and moves
  the task to AUTHORIZED. LOW and MEDIUM tasks get their grant from policy, recorded with actor `control-plane`.
- `execute` consumes the grant once. A second execution gets `DUPLICATE_EXECUTION`.
- Before execution the grant's fingerprint is recomputed from current data, and the evidence gate runs again.

## Invalidation

`amend` (requester only, before execution) increments the revision. The facts fingerprint changes, and with it:

- evidence becomes STALE and must be resubmitted for the new revision;
- approvals and grants become SUPERSEDED;
- a release plan becomes SUPERSEDED.

The task returns to CLASSIFIED. The old fingerprint and the old grant are refused afterwards: `approve` with the
old fingerprint and `execute --grant <old>` both fail with `STALE_AUTHORIZATION`. The revision is part of the
fingerprint, so even an amendment back to identical facts needs a new approval. Scenario S4 shows the whole
sequence.

## What this does not provide

No authentication, no signatures, no non-repudiation. Anyone who can write the store directory can impersonate
any actor. A real deployment would put an identity provider in front of the CLI or API, sign approvals with keys
held by the approvers, and keep the audit log in storage the control plane cannot rewrite.
