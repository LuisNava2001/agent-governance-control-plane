"""Evidence records, fingerprints and the evidence gate.

A record says that some content was submitted; it never says the content is good. Every gate re-reads the
content file and checks, in order: it exists, it still has the digest registered at submission, it matches its
JSON Schema, it is bound to this task and to the current revision of the facts, and it passes type-specific
semantic checks (tests actually passed, the plan covers exactly the requested steps, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .canonical import digest, digest_bytes
from .policy import rank
from .schema import load_schema, validate
from .store import Store

EVIDENCE_TYPES = ("plan", "test_report", "risk_assessment", "release_plan")
SUBMITTED, SUPERSEDED, STALE = "SUBMITTED", "SUPERSEDED", "STALE"


def change_fingerprint(task_id: str, revision: int, request: dict[str, Any]) -> str:
    """Fingerprint of the facts an agent would act on. Evidence binds to it."""
    return digest({"task_id": task_id, "revision": revision, "request": request})


def current_records(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The latest submitted (not superseded, not stale) record of each evidence type."""
    latest: dict[str, dict[str, Any]] = {}
    for record in task["evidence"]:
        if record["status"] == SUBMITTED:
            latest[record["type"]] = record
    return latest


def _steps(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted((item["action"], item.get("path") or "") for item in items)


def check_record(store: Store, task: dict[str, Any], record: dict[str, Any]) -> list[str]:
    """Problems with one evidence record; empty means it is valid right now."""
    path = store.evidence_path(task["task_id"], record["evidence_id"])
    if not path.is_file():
        return ["content file is missing"]
    data = path.read_bytes()
    if digest_bytes(data) != record["digest"]:
        return ["content changed after submission (digest mismatch)"]
    try:
        content = json.loads(data)
    except json.JSONDecodeError:
        return ["content is not valid JSON"]
    problems = validate(content, load_schema(f"evidence_{record['type']}"))
    if problems:
        return problems[:6]
    if content["task_id"] != task["task_id"]:
        problems.append(f"bound to {content['task_id']}, not to this task")
    if content["subject_fingerprint"] != task["facts_fingerprint"]:
        problems.append("covers an earlier revision of the facts (stale)")
    kind = record["type"]
    if kind == "plan":
        planned, requested = _steps(content["steps"]), _steps(task["request"]["steps"])
        if planned != requested:
            missing = sorted(set(requested) - set(planned))
            extra = sorted(set(planned) - set(requested))
            problems.append(f"plan does not match the requested steps (missing {missing}, extra {extra})")
    elif kind == "test_report":
        if content["passed"] + content["failed"] + content["skipped"] != content["total"]:
            problems.append("counts are inconsistent: passed + failed + skipped != total")
        if content["total"] == 0:
            problems.append("no tests were run")
        if content["failed"] > 0 or content["result"] != "PASS":
            problems.append(f"tests did not pass ({content['failed']} failed, result {content['result']})")
    elif kind == "risk_assessment":
        level = (task.get("classification") or {}).get("level")
        if level and rank(content["assessed_level"]) < rank(level):
            problems.append(f"assessed level {content['assessed_level']} is below the computed level {level}")
    elif kind == "release_plan":
        release = task.get("release") or {}
        if content["version"] != task["request"].get("release", {}).get("version"):
            problems.append("release version does not match the request")
        if content["release_id"] != release.get("release_id"):
            problems.append("release id does not match the task's release")
        expected = sorted((r["evidence_id"], r["type"], r["digest"])
                          for t, r in current_records(task).items() if t != "release_plan")
        listed = sorted((e["evidence_id"], e["type"], e["digest"]) for e in content["evidence"])
        if listed != expected:
            problems.append("release plan lists evidence that is no longer current; regenerate it")
        if _steps(content["changes"]) != _steps(task["request"]["steps"]):
            problems.append("release plan changes do not match the requested steps")
    return problems


@dataclass
class GateResult:
    gate: str
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(item["valid"] for item in self.items)

    @property
    def missing(self) -> list[str]:
        return [item["type"] for item in self.items if item["evidence_id"] is None]

    @property
    def invalid(self) -> list[str]:
        return [item["type"] for item in self.items if item["evidence_id"] is not None and not item["valid"]]

    def summary(self) -> str:
        if self.passed:
            return f"{self.gate}: all required evidence is valid"
        parts = []
        if self.missing:
            parts.append(f"missing {', '.join(self.missing)}")
        for item in self.items:
            if item["evidence_id"] and not item["valid"]:
                parts.append(f"{item['type']} {item['evidence_id']} invalid: {'; '.join(item['problems'])}")
        return f"{self.gate}: " + " | ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "passed": self.passed, "items": self.items}


def evaluate(store: Store, task: dict[str, Any], required: list[str], gate: str) -> GateResult:
    records = current_records(task)
    result = GateResult(gate)
    for kind in required:
        record = records.get(kind)
        if record is None:
            result.items.append({"type": kind, "evidence_id": None, "valid": False, "problems": ["not submitted"]})
            continue
        problems = check_record(store, task, record)
        result.items.append({"type": kind, "evidence_id": record["evidence_id"], "valid": not problems,
                             "problems": problems})
    return result
