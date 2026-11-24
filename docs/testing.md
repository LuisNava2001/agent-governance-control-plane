# Testing

Standard library only; nothing to install.

```bash
python -m unittest discover -s tests -t . -v        # 91 tests
python -m agentgov demo --out demo/data.js --check    # 7 scenarios + demo-data drift check
```

## Inventory

| File | Tests | Covers |
| --- | --- | --- |
| `test_states.py` | 9 | Every listed edge accepted; every unlisted (state, event, target) refused; terminal states; reachability; execution only from AUTHORIZED; completion only from VERIFIED |
| `test_policy.py` | 15 | Reference levels, path rules over actions, declared risk ignored or honoured, escalation, forbidden and unknown actions, action scope, path escapes, structural errors, unmatched paths, determinism under reordering, requirements, policy digest, glob and path-safety semantics |
| `test_schema.py` | 4 | No schema uses an unsupported keyword, shipped data validates, bad requests are refused, validator keywords |
| `test_evidence.py` | 11 | Record fields, submission is not acceptance, semantic checks, zero tests, cross-task and stale binding, edited and deleted files, schema-invalid content, supersession, agents cannot submit, evidence locked after review |
| `test_engine.py` | 22 | Full lifecycles, no skipped steps, fail-safe EVIDENCE_PENDING, forbidden requests, identity on submit, separation of duties, fingerprint quoting, CRITICAL quorum and roles, no double authorization, rejection, invalidation by amendment, old fingerprint and old grant refused, tampering after authorization, single execution, authorized files only, release stages, stale release plans |
| `test_concurrency.py` | 6 | Compare-and-swap, six threads racing to execute, racing approvals, quorum reached once, lock timeout, four OS processes racing to execute |
| `test_worker.py` | 7 | Sandbox escapes, symlink escape, no outward-reaching imports, rogue workers (crash, escape, extra file, wrong content, extra step), claims ignored, failing simulated tests, writes confined to the sandbox |
| `test_audit.py` | 7 | Every transition audited, refusals audited, chain linkage, edit / delete / reorder detected, full rewrite not detectable (documented limit), task files edited outside the control plane, policy changed after classification |
| `test_scenarios.py` | 6 | All scenarios behave as specified, outcomes, determinism, committed demo data equals a fresh run, demo page is static and offline |
| `test_cli.py` | 4 | Full flow through the CLI, exit code 2 with a reason on refusal, explain and demo, missing home and bad input |

## Local results (2026-11-23, Windows, Python 3.11.9)

- `python -m unittest discover -s tests -t . -v`: 91 tests run, 90 passed, 1 skipped, 0 failed.
- `python -m agentgov demo`: 7/7 scenarios behaved as specified.

The skipped test is `test_symlinks_cannot_be_used_to_escape`. Creating a symlink needs extra privileges on that
Windows machine, so the test skips itself there with an explicit reason; a skip is reported as a skip, never as a
pass. The test runs wherever symlinks can be created (for example Linux), and the lexical path checks it
complements are covered by other tests on every platform.

## Continuous integration

`.github/workflows/ci.yml` runs the suite, the scenario check and a CLI smoke test on Ubuntu and Windows with
Python 3.11, 3.12 and 3.13. Results are in the repository's Actions tab once the workflow has run.

## Coverage limits

- No coverage tool is used (standard library only); the inventory above is the coverage statement.
- Concurrency is tested on one machine with threads and local processes. Distributed or networked storage is out
  of scope.
- Identity is not tested beyond role checks, because no authentication exists (see `human-authorization.md`).
