"""The control plane. Every command follows the same shape:

    load (integrity-checked) -> guards -> state-machine transition -> audit entry -> compare-and-swap commit

A refused command changes nothing except the audit log, which records the refusal and its reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from .actors import CONTROL_PLANE, Actors, default_actors_data
from .canonical import digest, digest_text
from .clock import Clock, SystemClock
from .errors import ErrorCode, GovernanceError
from .evidence import EVIDENCE_TYPES, STALE, SUBMITTED, SUPERSEDED, change_fingerprint, check_record, current_records, evaluate
from .policy import Policy, default_policy_data
from .release import AUTHORIZED as RELEASE_AUTHORIZED
from .release import GENERATED, SIMULATED, build_plan, new_release_record
from .release import SUPERSEDED as RELEASE_SUPERSEDED
from .sandbox import Sandbox
from .schema import check
from .states import TRANSITIONS, State, allowed_events, transition
from .store import Store, json_bytes, write_atomic
from .worker import SimulatedWorker, WorkerReport

ACTIVE, CONSUMED = "ACTIVE", "CONSUMED"


@cache
def _sample_app_text() -> str:
    return resources.files(__package__).joinpath("data", "sample_app.json").read_text(encoding="utf-8")


def sample_app_files() -> dict[str, str]:
    return json.loads(_sample_app_text())["files"]


@dataclass
class Outcome:
    reason: str
    evidence_refs: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


class ControlPlane:
    def __init__(self, home: Path | str, clock: Clock | None = None, worker: Any = None) -> None:
        self.store = Store(Path(home))
        if not self.store.exists():
            raise GovernanceError(ErrorCode.NOT_FOUND, f"no control-plane home at {home}; run `agentgov init` first")
        self.clock = clock or SystemClock()
        self.worker = worker or SimulatedWorker()
        self.policy = Policy.from_file(self.store.policy_path)
        self.actors = Actors.from_file(self.store.actors_path)

    @staticmethod
    def init_home(home: Path | str, policy: dict[str, Any] | None = None, actors: dict[str, Any] | None = None) -> Path:
        store = Store(Path(home))
        for folder in (store.home, store.tasks_dir, store.evidence_dir, store.workspaces_dir):
            folder.mkdir(parents=True, exist_ok=True)
        if not store.policy_path.exists():
            data = policy or default_policy_data()
            Policy(data)  # validate before writing
            write_atomic(store.policy_path, json_bytes(data))
        if not store.actors_path.exists():
            data = actors or default_actors_data()
            Actors(data)
            write_atomic(store.actors_path, json_bytes(data))
        return store.home

    # -- plumbing -------------------------------------------------------------------

    def _entry(self, task: dict[str, Any] | None, event: str, actor: str, outcome: str, before: str | None,
               after: str | None, reason: str, evidence_refs: list[str] | None = None) -> dict[str, Any]:
        return {"timestamp": self.clock.now(), "task_id": task["task_id"] if task else None, "event": event,
                "actor": actor, "outcome": outcome, "from_state": before, "to_state": after, "reason": reason,
                "evidence_refs": list(evidence_refs or []),
                "facts_fingerprint": task.get("facts_fingerprint") if task else None}

    def _check_integrity(self, task: dict[str, Any], seal: str | None, allow_policy_change: bool = False) -> None:
        if seal != digest(task):
            raise GovernanceError(ErrorCode.INTEGRITY_ERROR,
                                  "task document does not match its last audited state (modified outside the control plane)")
        if change_fingerprint(task["task_id"], task["revision"], task["request"]) != task["facts_fingerprint"]:
            raise GovernanceError(ErrorCode.INTEGRITY_ERROR, "task facts were modified outside the control plane")
        stored = task.get("classification")
        if stored is None:
            return
        if stored["policy_digest"] != self.policy.digest:
            if allow_policy_change:
                return
            raise GovernanceError(ErrorCode.INTEGRITY_ERROR, "policy changed since classification; amend the task to re-classify")
        fresh = self.policy.classify(task["request"]).to_dict()
        if fresh["level"] != stored["level"] or fresh["forbidden"] != stored["forbidden"]:
            raise GovernanceError(ErrorCode.INTEGRITY_ERROR, "stored classification does not match the facts")

    def _move(self, task: dict[str, Any], event: str, target: State, actor: str) -> None:
        source = State(task["state"])
        transition(source, event, target)
        now = self.clock.now()
        task["state"] = target.value
        task["updated_at"] = now
        task["history"].append({"event": event, "from": source.value, "to": target.value, "at": now, "actor": actor})

    @staticmethod
    def _require_event(task: dict[str, Any], event: str) -> None:
        state = State(task["state"])
        if (state, event) not in TRANSITIONS:
            raise GovernanceError(ErrorCode.INVALID_TRANSITION, f"'{event}' is not allowed in state {state}",
                                  {"allowed_events": allowed_events(state)})

    def _command(self, task_id: str, event: str, actor: str, mutate: Callable[[dict[str, Any]], Outcome],
                 allow_policy_change: bool = False) -> dict[str, Any]:
        task, seal = self.store.load_task_sealed(task_id)
        before, expected = task["state"], task["version"]
        try:
            self._check_integrity(task, seal, allow_policy_change)
            outcome = mutate(task)
        except GovernanceError as err:
            self.store.append_audit([self._entry(task, event, actor, "rejected", before, before, f"{err.code}: {err.message}")])
            raise
        entry = self._entry(task, event, actor, "applied", before, task["state"], outcome.reason, outcome.evidence_refs)
        try:
            self.store.commit(task, expected, [entry])
        except GovernanceError as err:
            self.store.append_audit([self._entry(task, event, actor, "rejected", before, before, f"{err.code}: {err.message}")])
            raise
        return {"task_id": task_id, "event": event, "state": task["state"], "reason": outcome.reason, **outcome.result}

    def review_fingerprint(self, task: dict[str, Any]) -> str | None:
        """What a human approves: facts, level, policy, requirements and the exact evidence digests."""
        classification = task.get("classification") or {}
        if not classification.get("level"):
            return None
        records = current_records(task)
        evidence = sorted((kind, records[kind]["evidence_id"], records[kind]["digest"])
                          for kind in task["requirements"]["evidence"] if kind in records)
        return digest({"facts": task["facts_fingerprint"], "level": classification["level"],
                       "policy": classification["policy_digest"], "requirements": task["requirements"],
                       "evidence": evidence})

    def _gate(self, task: dict[str, Any], gate: str, types: list[str] | None = None):
        return evaluate(self.store, task, types if types is not None else task["requirements"]["evidence"], gate)

    def _register_evidence(self, task: dict[str, Any], kind: str, content: dict[str, Any], source: str,
                           submitted_by: str) -> dict[str, Any]:
        evidence_id = self.store.next_id("EV")
        evidence_digest = self.store.write_evidence(task["task_id"], evidence_id, content)
        for record in task["evidence"]:
            if record["type"] == kind and record["status"] == SUBMITTED:
                record["status"] = SUPERSEDED
        record = {"evidence_id": evidence_id, "type": kind, "status": SUBMITTED, "source": source,
                  "submitted_by": submitted_by, "created_at": self.clock.now(), "revision": task["revision"],
                  "content_ref": f"evidence/{task['task_id']}/{evidence_id}.json", "digest": evidence_digest}
        task["evidence"].append(record)
        return record

    def _issue_grant(self, task: dict[str, Any], fingerprint: str, basis: str, approvals: list[str]) -> dict[str, Any]:
        grant = {"grant_id": self.store.next_id("GR"), "review_fingerprint": fingerprint, "basis": basis,
                 "approvals": approvals, "issued_at": self.clock.now(), "status": ACTIVE}
        task["grants"].append(grant)
        if task.get("release") and task["release"]["stage"] == GENERATED:
            task["release"]["stage"] = RELEASE_AUTHORIZED
        return grant

    # -- commands -------------------------------------------------------------------

    def submit(self, request: dict[str, Any], actor: str) -> dict[str, Any]:
        try:
            check(request, "task_request")
            self.actors.require_human(actor, "submit tasks")
            if not self.actors.has(actor, "engineer"):
                raise GovernanceError(ErrorCode.NOT_PERMITTED, f"{actor} is not an engineer and cannot submit tasks")
            if request["requester"] != actor:
                raise GovernanceError(ErrorCode.NOT_PERMITTED, "the requester must be the actor submitting the task")
        except GovernanceError as err:
            self.store.append_audit([self._entry(None, "submit", actor, "rejected", None, None, f"{err.code}: {err.message}")])
            raise
        task_id = self.store.next_id("TASK")
        now = self.clock.now()
        task = {
            "task_id": task_id, "version": 1, "state": State.CREATED.value, "revision": 1, "request": request,
            "facts_fingerprint": change_fingerprint(task_id, 1, request), "classification": None,
            "requirements": None, "evidence": [], "approvals": [], "grants": [], "authorization_request": None,
            "release": None, "execution": None, "created_at": now, "updated_at": now,
            "history": [{"event": "submit", "from": None, "to": State.CREATED.value, "at": now, "actor": actor}],
        }
        entry = self._entry(task, "submit", actor, "applied", None, State.CREATED.value, f"request '{request['title']}' recorded")
        entry["task_version"] = 1
        self.store.create_task(task, [entry])
        return {"task_id": task_id, "event": "submit", "state": task["state"], "reason": entry["reason"]}

    def classify(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "classify tasks")
            self._require_event(task, "classify")
            classification = self.policy.classify(task["request"])
            task["classification"] = classification.to_dict()
            if classification.level is None:
                self._move(task, "classify", State.BLOCKED, actor)
                return Outcome("forbidden by policy: " + "; ".join(classification.forbidden),
                               result={"classification": task["classification"]})
            task["requirements"] = self.policy.requirements(classification.level, task["request"])
            self._move(task, "classify", State.CLASSIFIED, actor)
            reqs = task["requirements"]
            ignored = [f.detail for f in classification.findings if f.rule == "D-IGNORED"]
            return Outcome(f"level {classification.level}: needs {', '.join(reqs['evidence'])}; "
                           f"{reqs['approvals']} human approval(s)" + (f" ({ignored[0]})" if ignored else ""),
                           result={"classification": task["classification"]})

        return self._command(task_id, "classify", actor, mutate)

    def add_evidence(self, task_id: str, kind: str, content: Any, source: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "submit evidence about their own work")
            if kind not in EVIDENCE_TYPES:
                raise GovernanceError(ErrorCode.SCHEMA_INVALID, f"unknown evidence type {kind!r}")
            if kind == "release_plan":
                raise GovernanceError(ErrorCode.FORBIDDEN, "release plans are generated by the control plane, not submitted")
            if not isinstance(content, dict):
                raise GovernanceError(ErrorCode.SCHEMA_INVALID, "evidence content must be a JSON object")
            if task["state"] not in (State.CLASSIFIED, State.PLANNED, State.EVIDENCE_PENDING):
                raise GovernanceError(ErrorCode.INVALID_TRANSITION,
                                      f"evidence can only be added while planning or collecting evidence (state {task['state']})")
            record = self._register_evidence(task, kind, content, source, actor)
            problems = check_record(self.store, task, record)
            note = f"; does not pass checks yet: {'; '.join(problems)}" if problems else ""
            return Outcome(f"{kind} {record['evidence_id']} submitted{note}", [record["evidence_id"]],
                           {"evidence_id": record["evidence_id"], "problems": problems})

        return self._command(task_id, "add_evidence", actor, mutate)

    def plan(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "accept plans")
            self._require_event(task, "plan")
            gate = self._gate(task, "plan gate", ["plan"])
            if not gate.passed:
                raise GovernanceError(ErrorCode.GATE_FAILED, gate.summary(), gate.to_dict())
            self._move(task, "plan", State.PLANNED, actor)
            return Outcome(gate.summary(), [i["evidence_id"] for i in gate.items], {"gate": gate.to_dict()})

        return self._command(task_id, "plan", actor, mutate)

    def review(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "submit tasks for review")
            self._require_event(task, "review")
            gate = self._gate(task, "evidence gate")
            target = State.READY_FOR_REVIEW if gate.passed else State.EVIDENCE_PENDING
            self._move(task, "review", target, actor)
            refs = [i["evidence_id"] for i in gate.items if i["evidence_id"]]
            return Outcome(gate.summary(), refs, {"gate": gate.to_dict()})

        return self._command(task_id, "review", actor, mutate)

    def generate_release_plan(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "generate release plans")
            if task["state"] not in (State.PLANNED, State.EVIDENCE_PENDING):
                raise GovernanceError(ErrorCode.INVALID_TRANSITION,
                                      f"release plans are generated after planning and before review (state {task['state']})")
            if not self.policy.needs_release(task["request"]):
                raise GovernanceError(ErrorCode.FORBIDDEN, "this task has no release step")
            others = [kind for kind in task["requirements"]["evidence"] if kind != "release_plan"]
            gate = self._gate(task, "release-plan gate", others)
            if not gate.passed:
                raise GovernanceError(ErrorCode.GATE_FAILED, gate.summary(), gate.to_dict())
            release = task.get("release")
            release_id = release["release_id"] if release and release["stage"] != RELEASE_SUPERSEDED else self.store.next_id("RP")
            content = build_plan(task, release_id)
            record = self._register_evidence(task, "release_plan", content, "control-plane", CONTROL_PLANE)
            task["release"] = new_release_record(release_id, content["version"], record["evidence_id"], record["digest"])
            return Outcome(f"release plan {release_id} for {content['version']} generated ({record['evidence_id']})",
                           [record["evidence_id"]], {"release": task["release"]})

        return self._command(task_id, "release_plan", actor, mutate)

    def request_authorization(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "request authorization")
            self._require_event(task, "request_authorization")
            gate = self._gate(task, "authorization gate")
            if not gate.passed:
                raise GovernanceError(ErrorCode.GATE_FAILED, gate.summary(), gate.to_dict())
            fingerprint = self.review_fingerprint(task)
            reqs = task["requirements"]
            if reqs["approvals"] == 0:
                grant = self._issue_grant(task, fingerprint, f"policy: {reqs['level']} needs no human approval", [])
                self._move(task, "request_authorization", State.AUTHORIZED, CONTROL_PLANE)
                return Outcome(f"authorized by policy ({grant['grant_id']})", result={"grant": grant, "review_fingerprint": fingerprint})
            task["authorization_request"] = {"review_fingerprint": fingerprint, "approvals_required": reqs["approvals"],
                                             "approver_roles": reqs["approver_roles"], "opened_at": self.clock.now()}
            self._move(task, "request_authorization", State.APPROVAL_REQUIRED, actor)
            return Outcome(f"waiting for {reqs['approvals']} approval(s) of review fingerprint {fingerprint}",
                           result={"review_fingerprint": fingerprint})

        return self._command(task_id, "request_authorization", actor, mutate)

    def approve(self, task_id: str, actor: str, fingerprint: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self._require_event(task, "approve")
            if actor == task["request"]["requester"]:  # separation of duties comes before any role check
                raise GovernanceError(ErrorCode.APPROVAL_REJECTED, "the requester cannot approve their own task")
            self.actors.require_human(actor, "approve work")
            if not self.actors.has(actor, "approver"):
                raise GovernanceError(ErrorCode.NOT_PERMITTED, f"{actor} does not hold the approver role")
            current = self.review_fingerprint(task)
            if fingerprint != current:
                raise GovernanceError(ErrorCode.STALE_AUTHORIZATION,
                                      "the reviewed fingerprint does not match the current facts and evidence; review again")
            gate = self._gate(task, "approval gate")
            if not gate.passed:
                raise GovernanceError(ErrorCode.GATE_FAILED, gate.summary(), gate.to_dict())
            active = [a for a in task["approvals"] if a["status"] == ACTIVE and a["review_fingerprint"] == current]
            if any(a["approver"] == actor for a in active):
                raise GovernanceError(ErrorCode.APPROVAL_REJECTED, f"{actor} already approved this fingerprint")
            approval = {"approval_id": self.store.next_id("AP"), "approver": actor, "roles": self.actors.get(actor)["roles"],
                        "review_fingerprint": current, "approved_at": self.clock.now(), "status": ACTIVE}
            task["approvals"].append(approval)
            active.append(approval)
            reqs = task["requirements"]
            approvers = [a["approver"] for a in active]
            missing_roles = [role for role in reqs["approver_roles"] if not any(self.actors.has(a, role) for a in approvers)]
            if len(set(approvers)) >= reqs["approvals"] and not missing_roles:
                grant = self._issue_grant(task, current, f"{len(approvers)} human approval(s)", [a["approval_id"] for a in active])
                self._move(task, "approve", State.AUTHORIZED, actor)
                return Outcome(f"approval {approval['approval_id']} recorded; quorum reached, grant {grant['grant_id']} issued",
                               result={"approval": approval, "grant": grant})
            self._move(task, "approve", State.APPROVAL_REQUIRED, actor)
            needs = f"{reqs['approvals'] - len(set(approvers))} more approval(s)" if len(set(approvers)) < reqs["approvals"] else ""
            if missing_roles:
                needs = (needs + " and " if needs else "") + f"an approver with role {', '.join(missing_roles)}"
            return Outcome(f"approval {approval['approval_id']} recorded; still needs {needs}", result={"approval": approval})

        return self._command(task_id, "approve", actor, mutate)

    def reject(self, task_id: str, actor: str, reason: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self._require_event(task, "reject")
            self.actors.require_human(actor, "reject work")
            if not self.actors.has(actor, "approver") or actor == task["request"]["requester"]:
                raise GovernanceError(ErrorCode.NOT_PERMITTED, f"{actor} cannot reject this task")
            self._move(task, "reject", State.BLOCKED, actor)
            return Outcome(f"rejected by {actor}: {reason}")

        return self._command(task_id, "reject", actor, mutate)

    def amend(self, task_id: str, request: dict[str, Any], actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            check(request, "task_request")
            self._require_event(task, "amend")
            if actor != task["request"]["requester"] or request["requester"] != actor:
                raise GovernanceError(ErrorCode.NOT_PERMITTED, "only the original requester can amend, and the requester cannot change")
            task["revision"] += 1
            task["request"] = request
            task["facts_fingerprint"] = change_fingerprint(task_id, task["revision"], request)
            stale = [r["evidence_id"] for r in task["evidence"] if r["status"] == SUBMITTED]
            for record in task["evidence"]:
                if record["status"] == SUBMITTED:
                    record["status"] = STALE
            superseded = [a["approval_id"] for a in task["approvals"] if a["status"] == ACTIVE]
            for item in task["approvals"] + task["grants"]:
                if item["status"] == ACTIVE:
                    item["status"] = SUPERSEDED
            grants = [g["grant_id"] for g in task["grants"] if g["status"] == SUPERSEDED]
            task["authorization_request"] = None
            if task.get("release"):
                task["release"]["stage"] = RELEASE_SUPERSEDED
            classification = self.policy.classify(request)
            task["classification"] = classification.to_dict()
            task["requirements"] = self.policy.requirements(classification.level, request) if classification.level else None
            target = State.CLASSIFIED if classification.level else State.BLOCKED
            self._move(task, "amend", target, actor)
            return Outcome(f"revision {task['revision']}: facts changed; invalidated approvals {superseded or 'none'}, "
                           f"grants {grants or 'none'}, evidence {stale or 'none'}; re-classified as "
                           f"{classification.level or 'forbidden'}", stale)

        return self._command(task_id, "amend", actor, mutate, allow_policy_change=True)

    def execute(self, task_id: str, actor: str, grant_id: str | None = None) -> dict[str, Any]:
        """Start (consumes the grant, at most once), run the worker in a sandbox, then verify its result."""
        started: dict[str, Any] = {}

        def start(task: dict[str, Any]) -> Outcome:
            if not self.actors.has(actor, "agent"):
                raise GovernanceError(ErrorCode.NOT_PERMITTED, "only an agent identity executes work")
            if task.get("execution") and task["state"] != State.AUTHORIZED:
                raise GovernanceError(ErrorCode.DUPLICATE_EXECUTION,
                                      f"task was already executed ({task['execution']['execution_id']})")
            self._require_event(task, "start_execution")
            if grant_id:
                named = next((g for g in task["grants"] if g["grant_id"] == grant_id), None)
                if named is None or named["status"] == SUPERSEDED:
                    raise GovernanceError(ErrorCode.STALE_AUTHORIZATION, f"grant {grant_id} is not valid for the current facts")
                if named["status"] == CONSUMED:
                    raise GovernanceError(ErrorCode.DUPLICATE_EXECUTION, f"grant {grant_id} was already used")
            active = [g for g in task["grants"] if g["status"] == ACTIVE]
            if not active or (grant_id and active[-1]["grant_id"] != grant_id):
                raise GovernanceError(ErrorCode.STALE_AUTHORIZATION, "no valid authorization grant for the current facts")
            grant = active[-1]
            if grant["review_fingerprint"] != self.review_fingerprint(task):
                raise GovernanceError(ErrorCode.STALE_AUTHORIZATION, "facts or evidence changed after authorization")
            gate = self._gate(task, "execution gate")
            if not gate.passed:
                raise GovernanceError(ErrorCode.GATE_FAILED, "evidence changed after authorization: " + gate.summary(), gate.to_dict())
            execution_id = self.store.next_id("EX")
            grant["status"] = CONSUMED
            grant["consumed_by"] = execution_id
            task["execution"] = {"execution_id": execution_id, "grant_id": grant["grant_id"], "actor": actor,
                                 "worker": getattr(self.worker, "name", type(self.worker).__name__),
                                 "started_at": self.clock.now(), "status": "RUNNING",
                                 "sandbox": f"workspaces/{task['task_id']}/{execution_id}"}
            self._move(task, "start_execution", State.EXECUTING, actor)
            started.update(task=json.loads(json.dumps(task)))
            return Outcome(f"execution {execution_id} started with grant {grant['grant_id']} (grant consumed)")

        self._command(task_id, "start_execution", actor, start)
        task = started["task"]
        execution = task["execution"]
        sandbox = Sandbox(self.store.home / execution["sandbox"])
        sandbox.materialize(sample_app_files())
        before = sandbox.manifest()
        release = task.get("release")
        assignment = {"task_id": task_id, "execution_id": execution["execution_id"], "title": task["request"]["title"],
                      "steps": task["request"]["steps"],
                      "release": {**task["request"]["release"], "release_id": release["release_id"]} if release else None}
        report: WorkerReport | None = None
        failure = None
        try:
            report = self.worker.run(assignment, sandbox)
        except GovernanceError as err:
            failure = f"worker stopped: {err.code}: {err.message}"
        except Exception as err:  # noqa: BLE001 - any worker crash must end in BLOCKED, not in a hung EXECUTING
            failure = f"worker crashed: {type(err).__name__}"
        after = sandbox.manifest()
        violations, notes = self._verify_execution(task, report, failure, before, after)

        def verify(current: dict[str, Any]) -> Outcome:
            passed = not violations
            current["execution"].update(status="VERIFIED" if passed else "FAILED", finished_at=self.clock.now(),
                                        report=report.to_dict() if report else None, violations=violations, notes=notes,
                                        changed_files=sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p)))
            if current.get("release"):
                current["release"]["verification"] = {"status": "PASSED" if passed else "FAILED"}
                if passed:
                    current["release"]["stage"] = SIMULATED
            self._move(current, "verify", State.VERIFIED if passed else State.BLOCKED, CONTROL_PLANE)
            summary = "worker result verified" if passed else "verification failed: " + "; ".join(violations)
            return Outcome(summary + (f" ({'; '.join(notes)})" if notes else ""),
                           result={"execution": current["execution"]})

        return self._command(task_id, "verify", CONTROL_PLANE, verify)

    def _verify_execution(self, task: dict[str, Any], report: WorkerReport | None, failure: str | None,
                          before: dict[str, str], after: dict[str, str]) -> tuple[list[str], list[str]]:
        violations: list[str] = [failure] if failure else []
        notes: list[str] = []
        steps = task["request"]["steps"]
        if report is None and not failure:
            violations.append("worker produced no report")
        if report is not None:
            if [(s.action, s.path) for s in report.steps] != [(s["action"], s.get("path")) for s in steps]:
                violations.append("worker reported steps that differ from the authorized steps")
            for path, claimed in sorted(report.outputs.items()):
                if after.get(path) != claimed:
                    violations.append(f"reported output {path} does not match the sandbox")
            if report.claims:
                notes.append(f"ignored worker claims {sorted(report.claims)}: agent output is never an authorization")
        changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
        unexpected = [p for p in changed if p not in self.policy.allowed_outputs(task["request"])]
        if unexpected:
            violations.append(f"files changed outside the authorized outputs: {unexpected}")
        for step in steps:
            if "content" in step and step.get("path") and after.get(step["path"]) != digest_text(step["content"]):
                violations.append(f"{step['path']} does not contain the authorized content")
        if ".agent/test-run.json" in after:
            run = json.loads((self.store.home / task["execution"]["sandbox"] / ".agent/test-run.json").read_text(encoding="utf-8"))
            if run.get("result") != "PASS":
                violations.append(f"simulated test run failed: {run.get('failing')}")
        return violations, notes

    def complete(self, task_id: str, actor: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "complete tasks")
            self._require_event(task, "complete")
            self._move(task, "complete", State.COMPLETED, actor)
            release = task.get("release")
            tail = f"; release {release['release_id']} stage {release['stage']}, deployment {release['deployment']}" if release else ""
            return Outcome(f"completed{tail}")

        return self._command(task_id, "complete", actor, mutate)

    def block(self, task_id: str, actor: str, reason: str) -> dict[str, Any]:
        def mutate(task: dict[str, Any]) -> Outcome:
            self.actors.require_human(actor, "block tasks")
            self._require_event(task, "block")
            self._move(task, "block", State.BLOCKED, actor)
            return Outcome(f"blocked by {actor}: {reason}")

        return self._command(task_id, "block", actor, mutate, allow_policy_change=True)

    # -- read side ------------------------------------------------------------------

    def status(self, task_id: str) -> dict[str, Any]:
        task, seal = self.store.load_task_sealed(task_id)
        integrity: list[str] = []
        try:
            self._check_integrity(task, seal)
        except GovernanceError as err:
            integrity.append(err.message)
        requirements = task.get("requirements")
        gate = self._gate(task, "evidence gate").to_dict() if requirements else None
        return {
            "task_id": task_id, "state": task["state"], "revision": task["revision"], "title": task["request"]["title"],
            "requester": task["request"]["requester"], "level": (task.get("classification") or {}).get("level"),
            "requirements": requirements, "evidence_gate": gate, "facts_fingerprint": task["facts_fingerprint"],
            "review_fingerprint": self.review_fingerprint(task) if requirements else None,
            "approvals": task["approvals"], "grants": task["grants"], "release": task.get("release"),
            "execution": task.get("execution"), "allowed_events": allowed_events(State(task["state"])),
            "integrity_problems": integrity, "history": task["history"],
        }

    def verify_integrity(self) -> dict[str, Any]:
        problems = [f"audit: {p}" for p in self.store.verify_audit()]
        last_state: dict[str, str] = {}
        for entry in self.store.read_audit():
            if entry["outcome"] == "applied" and entry["task_id"]:
                last_state[entry["task_id"]] = entry["to_state"]
        for task_id in self.store.task_ids():
            task, seal = self.store.load_task_sealed(task_id)
            try:
                self._check_integrity(task, seal)
            except GovernanceError as err:
                problems.append(f"{task_id}: {err.message}")
            if last_state.get(task_id) != task["state"]:
                problems.append(f"{task_id}: state {task['state']} does not match the audit log ({last_state.get(task_id)})")
            for record in task["evidence"]:
                if record["status"] == SUBMITTED and check_record(self.store, task, record)[:1] == ["content changed after submission (digest mismatch)"]:
                    problems.append(f"{task_id}: evidence {record['evidence_id']} content changed after submission")
        return {"ok": not problems, "problems": problems, "tasks": len(self.store.task_ids())}
