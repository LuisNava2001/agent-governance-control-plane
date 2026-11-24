"""The simulated agent. Deterministic, sandbox-only, and limited to a fixed action catalog.

This module deliberately imports nothing that can run processes, open sockets or read the environment; a test
enforces that. The worker only ever receives the steps the control plane authorized, and its report is treated
as a claim to be verified, never as proof or as an authorization.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .canonical import digest_text
from .errors import ErrorCode, GovernanceError
from .sandbox import Sandbox


@dataclass
class StepReport:
    action: str
    path: str | None
    status: str
    outputs: list[str]


@dataclass
class WorkerReport:
    worker: str
    execution_id: str
    steps: list[StepReport] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)  # path -> digest the worker says it produced
    claims: dict[str, Any] = field(default_factory=dict)  # free text from the agent; never trusted

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SimulatedWorker:
    name = "simulated-worker"

    def run(self, assignment: dict[str, Any], sandbox: Sandbox) -> WorkerReport:
        report = WorkerReport(worker=self.name, execution_id=assignment["execution_id"])
        for step in assignment["steps"]:
            handler = getattr(self, f"_do_{step['action']}", None)
            if handler is None:
                raise GovernanceError(ErrorCode.FORBIDDEN, f"worker has no handler for {step['action']}")
            written = handler(step, assignment, sandbox)
            for path in written:
                report.outputs[path] = digest_text(sandbox.read_text(path))
            report.steps.append(StepReport(step["action"], step.get("path"), "done", written))
        return report

    # -- write actions: put the authorized content at the authorized path ---------

    def _write(self, step: dict[str, Any], _: dict[str, Any], sandbox: Sandbox) -> list[str]:
        sandbox.write_text(step["path"], step["content"])
        return [step["path"]]

    _do_edit_docs = _do_add_tests = _do_edit_source = _do_update_dependency = _do_change_auth = _write

    # -- simulated actions ---------------------------------------------------------

    def _do_run_tests(self, _: dict[str, Any], assignment: dict[str, Any], sandbox: Sandbox) -> list[str]:
        files = sorted(path for path in sandbox.manifest() if path.startswith("tests/"))
        failing = [path for path in files if "SIMULATED_FAILURE" in sandbox.read_text(path)]
        result = {"execution_id": assignment["execution_id"], "simulated": True, "test_files": files,
                  "failing": failing, "result": "FAIL" if failing else "PASS"}
        sandbox.write_text(".agent/test-run.json", json.dumps(result, indent=2, sort_keys=True) + "\n")
        return [".agent/test-run.json"]

    def _do_prepare_release(self, _: dict[str, Any], assignment: dict[str, Any], sandbox: Sandbox) -> list[str]:
        version = assignment["release"]["version"]
        notes = assignment["release"].get("notes") or assignment["title"]
        sandbox.write_text("VERSION", version + "\n")
        changelog = sandbox.read_text("CHANGELOG.md")
        header, _, rest = changelog.partition("\n\n")
        sandbox.write_text("CHANGELOG.md", f"{header}\n\n## {version}\n\n- {notes}\n\n{rest}")
        return ["VERSION", "CHANGELOG.md"]

    def _do_publish_release(self, _: dict[str, Any], assignment: dict[str, Any], sandbox: Sandbox) -> list[str]:
        record = {"release_id": assignment["release"]["release_id"], "version": assignment["release"]["version"],
                  "status": "SIMULATED", "deployed": False,
                  "note": "Simulated publication inside the sandbox. Nothing was deployed anywhere."}
        sandbox.write_text(".agent/release-simulation.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
        return [".agent/release-simulation.json"]
