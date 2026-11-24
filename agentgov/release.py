"""Release plans. Four distinct stages, of which this MVP can reach only the first three:

1. GENERATED   - the control plane built a plan from the current facts and evidence.
2. AUTHORIZED  - the plan's fingerprint is covered by a valid authorization grant.
3. SIMULATED   - the worker ran the release steps in a sandbox and the control plane verified the result.
4. deployed    - never. `deployment` is always NOT_PERFORMED; there is no code path that deploys anything.
"""

from __future__ import annotations

from typing import Any

from .evidence import current_records

GENERATED, AUTHORIZED, SIMULATED, SUPERSEDED = "GENERATED", "AUTHORIZED", "SIMULATED", "SUPERSEDED"
NOT_PERFORMED = "NOT_PERFORMED"


def build_plan(task: dict[str, Any], release_id: str) -> dict[str, Any]:
    evidence = [{"evidence_id": r["evidence_id"], "type": r["type"], "digest": r["digest"]}
                for kind, r in sorted(current_records(task).items()) if kind != "release_plan"]
    return {
        "task_id": task["task_id"],
        "subject_fingerprint": task["facts_fingerprint"],
        "release_id": release_id,
        "version": task["request"]["release"]["version"],
        "changes": [{"action": s["action"], **({"path": s["path"]} if s.get("path") else {})}
                    for s in task["request"]["steps"]],
        "evidence": evidence,
        "rollback": "Restore VERSION and CHANGELOG.md from the previous release (simulated).",
        "deployment": NOT_PERFORMED,
    }


def new_release_record(release_id: str, version: str, plan_evidence_id: str, plan_digest: str) -> dict[str, Any]:
    return {"release_id": release_id, "version": version, "plan_evidence_id": plan_evidence_id,
            "plan_digest": plan_digest, "stage": GENERATED, "deployment": NOT_PERFORMED,
            "verification": {"status": "NOT_RUN"}}
