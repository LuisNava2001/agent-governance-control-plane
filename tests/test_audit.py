import json

from agentgov.canonical import GENESIS, digest
from agentgov.errors import ErrorCode

from .support import AUTH, DOCS, PlaneTestCase


class AuditTests(PlaneTestCase):
    def lines(self) -> list[str]:
        return self.plane.store.audit_path.read_text(encoding="utf-8").splitlines()

    def write_lines(self, lines: list[str]) -> None:
        self.plane.store.audit_path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_every_transition_is_audited_with_actor_reason_and_time(self) -> None:
        task_id = self.to_authorized(AUTH)
        self.plane.execute(task_id, "agent.worker")
        self.plane.complete(task_id, "riley.chen")
        applied = [e for e in self.plane.store.read_audit() if e["task_id"] == task_id and e["outcome"] == "applied"]
        transitions = [(e["from_state"], e["to_state"]) for e in applied if e["from_state"] != e["to_state"]]
        history = [(h["from"], h["to"]) for h in self.task(task_id)["history"]]
        self.assertEqual(transitions, history)
        for entry in applied:
            for key in ("event", "actor", "from_state", "to_state", "reason", "timestamp", "evidence_refs", "task_digest"):
                self.assertIn(key, entry)
            self.assertTrue(entry["reason"])
        evidence_events = [e for e in applied if e["event"] == "add_evidence"]
        self.assertTrue(all(e["evidence_refs"] for e in evidence_events))

    def test_refusals_are_audited_and_change_nothing(self) -> None:
        task_id = self.submit(DOCS)
        version = self.task(task_id)["version"]
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.execute, task_id, "agent.worker")
        last = self.plane.store.read_audit()[-1]
        self.assertEqual((last["outcome"], last["event"], last["actor"]), ("rejected", "start_execution", "agent.worker"))
        self.assertTrue(last["reason"].startswith("INVALID_TRANSITION"))
        self.assertEqual(self.task(task_id)["version"], version)

    def test_chain_is_intact_and_linked(self) -> None:
        self.to_authorized(AUTH)
        entries = self.plane.store.read_audit()
        self.assertEqual(self.plane.store.verify_audit(), [])
        self.assertEqual(entries[0]["prev_hash"], GENESIS)
        for previous, entry in zip(entries, entries[1:]):
            self.assertEqual(entry["prev_hash"], previous["entry_hash"])
            self.assertEqual(entry["seq"], previous["seq"] + 1)

    def test_edits_deletions_and_reordering_are_detected(self) -> None:
        self.to_authorized(AUTH)
        original = self.lines()
        edited = original.copy()
        entry = json.loads(edited[3])
        entry["actor"] = "kai.morgan"
        edited[3] = json.dumps(entry)
        cases = {"edit": edited, "delete": original[:3] + original[4:], "reorder": [original[1], original[0], *original[2:]]}
        for name, lines in cases.items():
            self.write_lines(lines)
            self.assertNotEqual(self.plane.store.verify_audit(), [], name)
            self.assertFalse(self.plane.verify_integrity()["ok"], name)
        self.write_lines(original)
        self.assertEqual(self.plane.store.verify_audit(), [])

    def test_full_chain_rewrite_is_not_detectable_by_hashes_alone(self) -> None:
        """Documented limit: without a secret or signature, someone who recomputes every hash goes unnoticed."""
        self.to_authorized(AUTH)
        entries = self.plane.store.read_audit()
        entries[2]["reason"] = "rewritten history"
        prev = GENESIS
        rewritten = []
        for entry in entries:
            entry["prev_hash"] = prev
            body = {k: v for k, v in entry.items() if k != "entry_hash"}
            entry["entry_hash"] = digest(body)
            prev = entry["entry_hash"]
            rewritten.append(json.dumps(entry))
        self.write_lines(rewritten)
        self.assertEqual(self.plane.store.verify_audit(), [])

    def test_task_file_edited_outside_the_control_plane_is_refused(self) -> None:
        task_id = self.to_ready(AUTH)
        path = self.plane.store.task_path(task_id)
        original = path.read_text(encoding="utf-8")
        edits = {
            "skip approval": lambda t: t.update(state="AUTHORIZED", grants=[{"grant_id": "GR-9999", "status": "ACTIVE",
                                                                               "review_fingerprint": "x", "basis": "forged",
                                                                               "approvals": [], "issued_at": "2026-01-01T00:00:00Z"}]),
            "downgrade level": lambda t: t["classification"].update(level="LOW"),
            "change facts": lambda t: t["request"]["steps"][0].update(content="SESSION_MINUTES = 999\n"),
        }
        for name, edit in edits.items():
            task = json.loads(original)
            edit(task)
            path.write_text(json.dumps(task), encoding="utf-8")
            self.assertRejected(ErrorCode.INTEGRITY_ERROR, self.plane.request_authorization, task_id, "riley.chen")
            self.assertTrue(self.plane.status(task_id)["integrity_problems"], name)
            self.assertFalse(self.plane.verify_integrity()["ok"], name)
        path.write_text(original, encoding="utf-8")
        self.assertEqual(self.plane.request_authorization(task_id, "riley.chen")["state"], "APPROVAL_REQUIRED")

    def test_policy_change_after_classification_is_detected(self) -> None:
        task_id = self.to_ready(DOCS)
        policy_path = self.plane.store.policy_path
        data = json.loads(policy_path.read_text(encoding="utf-8"))
        data["unmatched_path_level"] = "CRITICAL"
        policy_path.write_text(json.dumps(data), encoding="utf-8")
        plane = type(self.plane)(self.home)
        self.assertRejected(ErrorCode.INTEGRITY_ERROR, plane.request_authorization, task_id, "riley.chen")
        self.assertEqual(plane.amend(task_id, json.loads(json.dumps(DOCS)), "riley.chen")["state"], "CLASSIFIED")
