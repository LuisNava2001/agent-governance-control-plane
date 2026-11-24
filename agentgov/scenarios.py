"""Reproducible demo scenarios.

Each scenario runs in a fresh, temporary control-plane home with a stepping clock, so ids, timestamps and
fingerprints are identical on every run and every platform. Each step declares the outcome it expects; a
scenario passes only if every step behaves as declared.
"""

from __future__ import annotations

import copy
import json
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from .canonical import short
from .clock import SteppingClock
from .engine import ControlPlane
from .errors import GovernanceError
from .evidence import current_records
from .schema import check


def scenario_specs() -> list[dict[str, Any]]:
    folder = resources.files(__package__).joinpath("scenarios")
    specs = [json.loads(entry.read_text(encoding="utf-8")) for entry in folder.iterdir() if entry.name.endswith(".json")]
    for spec in specs:
        check(spec, "scenario")
    return sorted(specs, key=lambda s: s["id"])


def build_evidence(kind: str, task: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Evidence a fictional engineer or CI system would submit, bound to the task's current facts."""
    base: dict[str, Any] = {"task_id": task["task_id"], "subject_fingerprint": task["facts_fingerprint"]}
    if kind == "plan":
        base.update(summary=f"Carry out: {task['request']['title']}",
                    steps=[{"action": s["action"], **({"path": s["path"]} if s.get("path") else {})} for s in task["request"]["steps"]],
                    rollback="Revert the change set in version control (simulated).")
    elif kind == "test_report":
        base.update(suite="lumen-notes unit tests (simulated)", total=42, passed=42, failed=0, skipped=0, result="PASS")
    elif kind == "risk_assessment":
        base.update(assessed_level=task["classification"]["level"],
                    risks=[{"id": "R1", "description": "The change could alter behaviour beyond the edited files.",
                            "mitigation": "Run the full simulated suite and review the diff before execution."}])
    base.update(overrides or {})
    return base


class ScenarioRunner:
    def __init__(self, spec: dict[str, Any], home: Path) -> None:
        self.spec = spec
        ControlPlane.init_home(home)
        self.plane = ControlPlane(home, clock=SteppingClock())
        self.task_id: str | None = None
        self.approved_fingerprints: list[str] = []

    def _task(self) -> dict[str, Any]:
        assert self.task_id is not None
        return self.plane.store.load_task(self.task_id)

    def _dispatch(self, step: dict[str, Any], actor: str) -> dict[str, Any]:
        plane, do = self.plane, step["do"]
        if do == "submit":
            result = plane.submit(copy.deepcopy(self.spec["request"]), actor)
            self.task_id = result["task_id"]
            return result
        task_id = self.task_id
        if do == "classify":
            return plane.classify(task_id, actor)
        if do == "evidence":
            content = build_evidence(step["type"], self._task(), step.get("with"))
            return plane.add_evidence(task_id, step["type"], content, step.get("source", "manual"), actor)
        if do == "plan":
            return plane.plan(task_id, actor)
        if do == "review":
            return plane.review(task_id, actor)
        if do == "release_plan":
            return plane.generate_release_plan(task_id, actor)
        if do == "authorize":
            return plane.request_authorization(task_id, actor)
        if do == "approve":
            if step.get("fingerprint") == "previous":
                fingerprint = self.approved_fingerprints[-1]
            else:
                fingerprint = plane.review_fingerprint(self._task())
            result = plane.approve(task_id, actor, fingerprint)
            self.approved_fingerprints.append(fingerprint)
            return result
        if do == "reject":
            return plane.reject(task_id, actor, step.get("reason", "not acceptable"))
        if do == "amend":
            return plane.amend(task_id, copy.deepcopy(step["request"]), actor)
        if do == "execute":
            grant = None
            if step.get("grant") == "previous":
                grant = self._task()["grants"][-2]["grant_id"]
            return plane.execute(task_id, actor, grant)
        if do == "complete":
            return plane.complete(task_id, actor)
        if do == "block":
            return plane.block(task_id, actor, step.get("reason", "stopped by an operator"))
        if do == "tamper_evidence":
            # Simulates someone editing an evidence file directly on disk, bypassing the control plane.
            record = current_records(self._task())[step["type"]]
            path = plane.store.evidence_path(task_id, record["evidence_id"])
            content = json.loads(path.read_text(encoding="utf-8"))
            content.update(step.get("with", {}))
            path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return {"state": self._task()["state"], "reason": f"edited {record['evidence_id']} on disk outside the control plane"}
        raise ValueError(f"unknown scenario step {do}")

    def run(self) -> dict[str, Any]:
        steps = []
        for number, step in enumerate(self.spec["steps"], start=1):
            actor = step.get("actor", self.spec["request"]["requester"])
            before = self._task()["state"] if self.task_id else None
            try:
                result = self._dispatch(step, actor)
                outcome, code, message = "ok", None, result.get("reason", "")
            except GovernanceError as err:
                outcome, code, message = "rejected", err.code.value, err.message
            task = self._task() if self.task_id else None
            after = task["state"] if task else None
            expected = step.get("expect", "ok")
            matched = (outcome == "ok") if expected == "ok" else (code == expected)
            state_ok = step.get("state") in (None, after)
            fingerprint = self.plane.review_fingerprint(task) if task and task.get("requirements") else None
            steps.append({"n": number, "do": step["do"], "actor": actor, "note": step.get("note", ""), "expect": expected,
                          "outcome": outcome, "error_code": code, "message": message, "state_before": before,
                          "state_after": after, "review_fingerprint": short(fingerprint), "passed": matched and state_ok})
        status = self.plane.status(self.task_id) if self.task_id else None
        audit = [{k: e[k] for k in ("seq", "timestamp", "event", "actor", "outcome", "from_state", "to_state", "reason",
                                    "evidence_refs", "entry_hash")}
                 for e in self.plane.store.read_audit() if e["task_id"] == self.task_id]
        integrity = self.plane.verify_integrity()
        return {
            "id": self.spec["id"], "title": self.spec["title"], "description": self.spec["description"],
            "request": self.spec["request"], "passed": all(s["passed"] for s in steps), "steps": steps,
            "final": _final_view(status), "audit": audit,
            "integrity": {"audit_chain_intact": not any(p.startswith("audit:") for p in integrity["problems"]),
                          "problems": integrity["problems"]},
        }


def _final_view(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    execution = status.get("execution") or {}
    return {
        "state": status["state"], "level": status["level"], "revision": status["revision"],
        "requirements": status["requirements"], "facts_fingerprint": short(status["facts_fingerprint"]),
        "review_fingerprint": short(status["review_fingerprint"]),
        "approvals": [{k: a[k] for k in ("approval_id", "approver", "status")} | {"fingerprint": short(a["review_fingerprint"])}
                      for a in status["approvals"]],
        "grants": [{k: g[k] for k in ("grant_id", "basis", "status")} | {"fingerprint": short(g["review_fingerprint"])}
                   for g in status["grants"]],
        "release": status["release"],
        "execution": {k: execution.get(k) for k in ("execution_id", "status", "changed_files", "violations", "notes")}
        if execution else None,
        "path": [h["to"] for h in status["history"]],
    }


def run_scenario(spec: dict[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agentgov-demo-") as home:
        return ScenarioRunner(spec, Path(home)).run()


def run_all(only: str | None = None) -> list[dict[str, Any]]:
    return [run_scenario(spec) for spec in scenario_specs() if only in (None, spec["id"])]


def render_demo_data(traces: list[dict[str, Any]]) -> str:
    payload = {"generated_by": "agentgov demo", "llm_used": False, "scenarios": traces}
    return ("// Generated by `python -m agentgov demo --out demo/data.js` from real scenario runs. Do not edit.\n"
            "// Deterministic: fixed clock, fresh store per scenario, no LLM, no network.\n"
            "window.GOV_DEMO = " + json.dumps(payload, indent=1, ensure_ascii=True) + ";\n")
