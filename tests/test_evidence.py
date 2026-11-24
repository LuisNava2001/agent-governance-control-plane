import json

from agentgov.errors import ErrorCode
from agentgov.evidence import check_record, current_records

from .support import AUTH, FEATURE, PlaneTestCase


class EvidenceTests(PlaneTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.task_id = self.submit(AUTH)
        self.plane.classify(self.task_id, "riley.chen")

    def problems(self, kind: str) -> list[str]:
        task = self.task(self.task_id)
        return check_record(self.plane.store, task, current_records(task)[kind])

    def test_valid_evidence_has_no_problems(self) -> None:
        for kind in ("plan", "test_report", "risk_assessment"):
            self.add(self.task_id, kind)
            self.assertEqual(self.problems(kind), [], kind)

    def test_record_carries_identity_type_status_source_date_and_digest(self) -> None:
        self.add(self.task_id, "plan")
        record = self.task(self.task_id)["evidence"][-1]
        self.assertEqual(set(record) >= {"evidence_id", "type", "status", "source", "submitted_by", "created_at", "digest", "content_ref"}, True)
        self.assertRegex(record["digest"], r"^sha256:[0-9a-f]{64}$")

    def test_submission_is_not_acceptance(self) -> None:
        result = self.add(self.task_id, "test_report", {"failed": 3, "passed": 39, "result": "FAIL"})
        self.assertIn("tests did not pass", " ".join(result["problems"]))
        self.assertEqual(self.task(self.task_id)["evidence"][-1]["status"], "SUBMITTED")

    def test_semantic_checks(self) -> None:
        cases = {
            "test_report": ({"passed": 10, "total": 42}, "inconsistent"),
            "plan": ({"steps": [{"action": "edit_docs", "path": "docs/usage.md"}]}, "plan does not match"),
            "risk_assessment": ({"assessed_level": "LOW"}, "below the computed level"),
        }
        for kind, (overrides, fragment) in cases.items():
            self.add(self.task_id, kind, overrides)
            self.assertIn(fragment, " ".join(self.problems(kind)), kind)

    def test_zero_tests_is_not_evidence(self) -> None:
        self.add(self.task_id, "test_report", {"total": 0, "passed": 0})
        self.assertIn("no tests were run", self.problems("test_report"))

    def test_evidence_bound_to_another_task_or_revision_is_rejected(self) -> None:
        self.add(self.task_id, "plan", {"task_id": "TASK-0999"})
        self.assertIn("bound to TASK-0999", " ".join(self.problems("plan")))
        self.add(self.task_id, "plan", {"subject_fingerprint": "sha256:" + "a" * 64})
        self.assertIn("stale", " ".join(self.problems("plan")))

    def test_a_file_that_exists_is_not_automatically_valid(self) -> None:
        self.add(self.task_id, "plan")
        task = self.task(self.task_id)
        record = current_records(task)["plan"]
        path = self.plane.store.evidence_path(self.task_id, record["evidence_id"])
        path.write_text(json.dumps({"task_id": self.task_id}), encoding="utf-8")
        self.assertEqual(self.problems("plan"), ["content changed after submission (digest mismatch)"])
        path.unlink()
        self.assertEqual(self.problems("plan"), ["content file is missing"])

    def test_schema_invalid_content_is_rejected_by_the_gate(self) -> None:
        self.add(self.task_id, "plan", {"summary": "short"})
        self.assertIn("shorter than", " ".join(self.problems("plan")))
        self.assertRejected(ErrorCode.GATE_FAILED, self.plane.plan, self.task_id, "riley.chen")

    def test_newer_submission_supersedes_older(self) -> None:
        self.add(self.task_id, "test_report", {"failed": 1, "passed": 41, "result": "FAIL"})
        self.add(self.task_id, "test_report")
        statuses = [r["status"] for r in self.task(self.task_id)["evidence"]]
        self.assertEqual(statuses, ["SUPERSEDED", "SUBMITTED"])
        self.assertEqual(self.problems("test_report"), [])

    def test_agents_cannot_submit_evidence_and_release_plans_cannot_be_submitted(self) -> None:
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.add, self.task_id, "plan", None, "agent.worker")
        self.assertRejected(ErrorCode.FORBIDDEN, self.plane.add_evidence, self.task_id, "release_plan", {}, "x", "riley.chen")
        self.assertRejected(ErrorCode.SCHEMA_INVALID, self.plane.add_evidence, self.task_id, "screenshot", {}, "x", "riley.chen")
        self.assertRejected(ErrorCode.SCHEMA_INVALID, self.plane.add_evidence, self.task_id, "plan", ["not", "an", "object"], "x", "riley.chen")

    def test_evidence_is_locked_after_review(self) -> None:
        task_id = self.to_ready(FEATURE)
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.add, task_id, "test_report")
