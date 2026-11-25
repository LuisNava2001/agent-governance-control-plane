# Execution boundaries and release plans

## The simulated worker

`SimulatedWorker` stands in for an AI coding agent. It is deterministic and limited to the action catalog:

| Action | What the worker does |
| --- | --- |
| `edit_docs`, `add_tests`, `edit_source`, `update_dependency`, `change_auth` | Writes the authorized content to the authorized path |
| `run_tests` | Simulated: inspects `tests/` in the sandbox and writes `.agent/test-run.json`; executes nothing |
| `prepare_release` | Writes `VERSION` and a `CHANGELOG.md` entry |
| `publish_release` | Writes `.agent/release-simulation.json` with `"deployed": false`; deploys nothing |

None of these actions runs commands, opens sockets, reads environment variables or secrets, calls APIs or
touches a real project. `tests/test_worker.py` parses `worker.py` and `sandbox.py` and fails if they import
modules such as `subprocess`, `socket`, `os`, `shutil`, `urllib` or call `open`, `eval` or `exec`.

## The sandbox

Each execution gets `workspaces/TASK-xxxx/EX-xxxx/`, filled with the synthetic Lumen Notes files. Every path is
checked lexically (relative, no `..`, no absolute or drive paths, no backslashes) and after resolution (must stay
under the sandbox root; symlinks in the path are refused).

"Sandbox" here means a directory plus these path checks, applied by the `Sandbox` class that the worker writes
through. It is not process or OS isolation: the worker runs in the control plane's own Python process, with the
same user and file-system rights. The worker stays inside the directory because it is written to use the
`Sandbox` API (and a static test checks its imports), not because anything stops it. Code that bypassed that API
could write anywhere the process can, and the before/after manifest only covers the sandbox directory, so such a
write would go undetected. A real agent would need an OS-level boundary (container, virtual machine or restricted
user) with the same verification on top.

## Verification after the worker returns

The control plane does not believe the worker's report. It compares a manifest of the sandbox taken before and
after the run and requires:

- The reported steps equal the authorized steps, in order.
- Each reported output digest matches the file actually in the sandbox.
- No file changed outside the authorized outputs (the step paths plus each action's fixed outputs).
- Each written file contains exactly the authorized content.
- A simulated test run, if requested, passed.

Free-form claims in the report (for example "approved by kai.morgan" or "deployed: true") are recorded as
ignored and never change authorization. A crash, a sandbox violation (an escape attempted through the `Sandbox`
API) or any failed check ends in BLOCKED.

## Release plans

Four stages are kept distinct:

| Stage | Meaning | Reachable here |
| --- | --- | --- |
| GENERATED | The control plane built the plan from the current facts and evidence digests | yes |
| AUTHORIZED | A grant covers the review fingerprint that includes the plan's digest | yes |
| SIMULATED | The worker ran the release steps in the sandbox and verification passed | yes |
| deployed | Software actually released somewhere | **no** |

The plan records an id, version, proposed changes, the evidence it relies on (ids and digests), a rollback note,
its digest, the authorization stage and the verification result. Its `deployment` field is the constant
`NOT_PERFORMED`, enforced by its JSON Schema. If evidence changes after the plan was generated, the plan no longer
validates and must be regenerated.
