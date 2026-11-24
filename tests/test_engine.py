import copy

from agentgov.errors import ErrorCode

from .support import AUTH, DOCS, FEATURE, RELEASE, PlaneTestCase, request


class LifecycleTests(PlaneTestCase):
    def test_low_risk_task_runs_end_to_end(self) -> None:
        task_id = self.to_ready(DOCS)
        self.assertEqual(self.plane.request_authorization(task_id, "riley.chen")["state"], "AUTHORIZED")
        self.assertEqual(self.plane.execute(task_id, "agent.worker")["state"], "VERIFIED")
        self.assertEqual(self.plane.complete(task_id, "riley.chen")["state"], "COMPLETED")
        path = [h["to"] for h in self.task(task_id)["history"]]
        self.assertEqual(path, ["CREATED", "CLASSIFIED", "PLANNED", "READY_FOR_REVIEW", "AUTHORIZED", "EXECUTING",
                                "VERIFIED", "COMPLETED"])

    def test_steps_cannot_be_skipped(self) -> None:
        task_id = self.submit(FEATURE)
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.request_authorization, task_id, "riley.chen")
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.execute, task_id, "agent.worker")
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.complete, task_id, "riley.chen")
        self.plane.classify(task_id, "riley.chen")
        self.assertRejected(ErrorCode.GATE_FAILED, self.plane.plan, task_id, "riley.chen")  # no plan evidence
        self.assertEqual(self.task(task_id)["state"], "CLASSIFIED")

    def test_missing_evidence_fails_safe_into_evidence_pending(self) -> None:
        task_id = self.submit(FEATURE)
        self.plane.classify(task_id, "riley.chen")
        self.add(task_id, "plan")
        self.plane.plan(task_id, "riley.chen")
        result = self.plane.review(task_id, "riley.chen")
        self.assertEqual(result["state"], "EVIDENCE_PENDING")
        self.assertEqual(result["gate"]["passed"], False)
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.request_authorization, task_id, "riley.chen")

    def test_forbidden_request_is_blocked_and_terminal(self) -> None:
        task_id = self.submit(request(DOCS, steps=[{"action": "run_shell"}]))
        self.assertEqual(self.plane.classify(task_id, "riley.chen")["state"], "BLOCKED")
        for call in (lambda: self.add(task_id, "plan"), lambda: self.plane.amend(task_id, copy.deepcopy(DOCS), "riley.chen"),
                     lambda: self.plane.block(task_id, "riley.chen", "again")):
            self.assertRejected(ErrorCode.INVALID_TRANSITION, call)

    def test_submit_checks_identity_and_schema(self) -> None:
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.submit, copy.deepcopy(DOCS), "noor.haddad")  # not the requester
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.submit, request(DOCS, requester="agent.worker"), "agent.worker")
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.submit, request(DOCS, requester="kai.morgan"), "kai.morgan")
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.submit, request(DOCS, requester="mallory.x"), "mallory.x")
        self.assertRejected(ErrorCode.SCHEMA_INVALID, self.plane.submit, request(DOCS, steps=[]), "riley.chen")

    def test_unknown_task(self) -> None:
        self.assertRejected(ErrorCode.NOT_FOUND, self.plane.classify, "TASK-0042", "riley.chen")


class AuthorizationTests(PlaneTestCase):
    def ready_for_approval(self, base=AUTH) -> str:
        task_id = self.to_ready(base)
        self.assertEqual(self.plane.request_authorization(task_id, base["requester"])["state"], "APPROVAL_REQUIRED")
        return task_id

    def fp(self, task_id: str) -> str:
        return self.plane.review_fingerprint(self.task(task_id))

    def test_high_risk_needs_one_independent_approver(self) -> None:
        task_id = self.ready_for_approval()
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.execute, task_id, "agent.worker")
        self.assertRejected(ErrorCode.APPROVAL_REJECTED, self.plane.approve, task_id, "riley.chen", self.fp(task_id))
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.approve, task_id, "agent.worker", self.fp(task_id))
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.approve, task_id, "noor.haddad", self.fp(task_id))
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.approve, task_id, "nobody.here", self.fp(task_id))
        result = self.plane.approve(task_id, "sam.ortiz", self.fp(task_id))
        self.assertEqual(result["state"], "AUTHORIZED")
        self.assertEqual(result["grant"]["review_fingerprint"], self.fp(task_id))

    def test_approver_who_is_also_the_requester_is_refused(self) -> None:
        own = request(AUTH, requester="sam.ortiz")
        task_id = self.ready_for_approval(own)
        self.assertRejected(ErrorCode.APPROVAL_REJECTED, self.plane.approve, task_id, "sam.ortiz", self.fp(task_id))

    def test_approval_must_quote_the_current_fingerprint(self) -> None:
        task_id = self.ready_for_approval()
        self.assertRejected(ErrorCode.STALE_AUTHORIZATION, self.plane.approve, task_id, "sam.ortiz", "sha256:" + "0" * 64)
        self.assertEqual(self.task(task_id)["approvals"], [])

    def test_critical_needs_two_approvers_including_a_release_manager(self) -> None:
        task_id = self.ready_for_approval(RELEASE)
        self.assertEqual(self.plane.approve(task_id, "sam.ortiz", self.fp(task_id))["state"], "APPROVAL_REQUIRED")
        self.assertRejected(ErrorCode.APPROVAL_REJECTED, self.plane.approve, task_id, "sam.ortiz", self.fp(task_id))
        result = self.plane.approve(task_id, "kai.morgan", self.fp(task_id))
        self.assertEqual(result["state"], "AUTHORIZED")
        self.assertEqual(len(result["grant"]["approvals"]), 2)

    def test_two_approvers_without_a_release_manager_are_not_enough(self) -> None:
        actors = self.plane.actors.by_id
        actors["noor.haddad"]["roles"] = ["engineer", "approver"]  # in-memory only: a second plain approver
        task_id = self.ready_for_approval(RELEASE)
        self.plane.approve(task_id, "sam.ortiz", self.fp(task_id))
        result = self.plane.approve(task_id, "noor.haddad", self.fp(task_id))
        self.assertEqual(result["state"], "APPROVAL_REQUIRED")
        self.assertIn("release_manager", result["reason"])

    def test_no_double_authorization(self) -> None:
        task_id = self.ready_for_approval()
        fingerprint = self.fp(task_id)
        self.plane.approve(task_id, "sam.ortiz", fingerprint)
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.approve, task_id, "kai.morgan", fingerprint)
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.request_authorization, task_id, "riley.chen")
        self.assertEqual(len(self.task(task_id)["grants"]), 1)

    def test_rejection_blocks(self) -> None:
        task_id = self.ready_for_approval()
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.reject, task_id, "riley.chen", "self")
        self.assertEqual(self.plane.reject(task_id, "sam.ortiz", "too risky")["state"], "BLOCKED")


class InvalidationTests(PlaneTestCase):
    def amended(self) -> dict:
        return request(AUTH, steps=[{"action": "change_auth", "path": "src/auth/session.py", "content": "SESSION_MINUTES = 15\n"}])

    def test_amending_after_approval_invalidates_everything_bound_to_the_old_facts(self) -> None:
        task_id = self.to_authorized(AUTH)
        old = self.task(task_id)
        old_fp = self.plane.review_fingerprint(old)
        result = self.plane.amend(task_id, self.amended(), "riley.chen")
        task = self.task(task_id)
        self.assertEqual(result["state"], "CLASSIFIED")
        self.assertEqual(task["revision"], 2)
        self.assertNotEqual(task["facts_fingerprint"], old["facts_fingerprint"])
        self.assertTrue(all(a["status"] == "SUPERSEDED" for a in task["approvals"]))
        self.assertTrue(all(g["status"] == "SUPERSEDED" for g in task["grants"]))
        self.assertTrue(all(r["status"] == "STALE" for r in task["evidence"]))
        self.assertRejected(ErrorCode.INVALID_TRANSITION, self.plane.execute, task_id, "agent.worker")
        # Old evidence cannot be reused either: the plan gate needs evidence for revision 2.
        self.assertRejected(ErrorCode.GATE_FAILED, self.plane.plan, task_id, "riley.chen")
        # Drive it to approval again; the old fingerprint and the old grant are refused.
        self.add(task_id, "plan")
        self.plane.plan(task_id, "riley.chen")
        self.add(task_id, "test_report")
        self.add(task_id, "risk_assessment")
        self.plane.review(task_id, "riley.chen")
        self.plane.request_authorization(task_id, "riley.chen")
        self.assertRejected(ErrorCode.STALE_AUTHORIZATION, self.plane.approve, task_id, "sam.ortiz", old_fp)
        self.plane.approve(task_id, "sam.ortiz", self.plane.review_fingerprint(self.task(task_id)))
        old_grant = self.task(task_id)["grants"][0]["grant_id"]
        self.assertRejected(ErrorCode.STALE_AUTHORIZATION, self.plane.execute, task_id, "agent.worker", old_grant)
        self.assertEqual(self.plane.execute(task_id, "agent.worker")["state"], "VERIFIED")

    def test_same_facts_after_amend_still_need_a_new_approval(self) -> None:
        task_id = self.to_authorized(AUTH)
        self.plane.amend(task_id, copy.deepcopy(AUTH), "riley.chen")  # identical facts, new revision
        self.assertEqual(self.task(task_id)["grants"][0]["status"], "SUPERSEDED")

    def test_only_the_requester_can_amend_and_amendments_are_reclassified(self) -> None:
        task_id = self.to_authorized(DOCS)
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.amend, task_id, copy.deepcopy(DOCS), "sam.ortiz")
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.amend, task_id, request(DOCS, requester="noor.haddad"), "riley.chen")
        upgraded = request(DOCS, steps=DOCS["steps"] + AUTH["steps"])
        self.plane.amend(task_id, upgraded, "riley.chen")
        self.assertEqual(self.task(task_id)["classification"]["level"], "HIGH")
        self.assertEqual(self.task(task_id)["requirements"]["approvals"], 1)
        blocked = self.plane.amend(task_id, request(DOCS, steps=[{"action": "deploy_production"}]), "riley.chen")
        self.assertEqual(blocked["state"], "BLOCKED")

    def test_evidence_tampered_after_authorization_stops_execution(self) -> None:
        task_id = self.to_authorized(FEATURE)
        record = self.task(task_id)["evidence"][-1]
        path = self.plane.store.evidence_path(task_id, record["evidence_id"])
        path.write_bytes(path.read_bytes().replace(b"PASS", b"PASS "))
        self.assertRejected(ErrorCode.GATE_FAILED, self.plane.execute, task_id, "agent.worker")
        self.assertEqual(self.task(task_id)["state"], "AUTHORIZED")
        self.assertEqual(self.task(task_id)["grants"][0]["status"], "ACTIVE")  # nothing consumed


class ExecutionTests(PlaneTestCase):
    def test_only_agents_execute_and_execution_happens_once(self) -> None:
        task_id = self.to_authorized(DOCS)
        self.assertRejected(ErrorCode.NOT_PERMITTED, self.plane.execute, task_id, "riley.chen")
        self.plane.execute(task_id, "agent.worker")
        self.assertRejected(ErrorCode.DUPLICATE_EXECUTION, self.plane.execute, task_id, "agent.worker")
        grant = self.task(task_id)["grants"][0]
        self.assertEqual((grant["status"], grant["consumed_by"]), ("CONSUMED", "EX-0001"))

    def test_execution_changes_only_the_authorized_files(self) -> None:
        task_id = self.to_authorized(FEATURE)
        result = self.plane.execute(task_id, "agent.worker")
        self.assertEqual(result["execution"]["changed_files"], [".agent/test-run.json", "src/notes/search.py"])

    def test_release_reaches_simulated_and_never_deployed(self) -> None:
        task_id = self.to_ready(RELEASE)
        release = self.task(task_id)["release"]
        self.assertEqual((release["stage"], release["deployment"]), ("GENERATED", "NOT_PERFORMED"))
        self.plane.request_authorization(task_id, "riley.chen")
        for approver in ("sam.ortiz", "kai.morgan"):
            self.plane.approve(task_id, approver, self.plane.review_fingerprint(self.task(task_id)))
        self.assertEqual(self.task(task_id)["release"]["stage"], "AUTHORIZED")
        self.plane.execute(task_id, "agent.worker")
        self.plane.complete(task_id, "riley.chen")
        release = self.task(task_id)["release"]
        self.assertEqual((release["stage"], release["deployment"], release["verification"]["status"]),
                         ("SIMULATED", "NOT_PERFORMED", "PASSED"))
        sandbox = self.home / self.task(task_id)["execution"]["sandbox"]
        self.assertEqual((sandbox / "VERSION").read_text(encoding="utf-8"), "1.4.0\n")
        self.assertIn('"deployed": false', (sandbox / ".agent/release-simulation.json").read_text(encoding="utf-8"))

    def test_release_plan_goes_stale_when_evidence_changes(self) -> None:
        task_id = self.submit(RELEASE)
        self.plane.classify(task_id, "riley.chen")
        self.add(task_id, "plan")
        self.plane.plan(task_id, "riley.chen")
        self.assertRejected(ErrorCode.GATE_FAILED, self.plane.generate_release_plan, task_id, "riley.chen")
        self.add(task_id, "test_report")
        self.add(task_id, "risk_assessment")
        self.plane.generate_release_plan(task_id, "riley.chen")
        self.add(task_id, "test_report", {"suite": "re-run"})
        result = self.plane.review(task_id, "riley.chen")
        self.assertEqual(result["state"], "EVIDENCE_PENDING")
        self.assertIn("regenerate", result["reason"])
        self.plane.generate_release_plan(task_id, "riley.chen")
        self.assertEqual(self.plane.review(task_id, "riley.chen")["state"], "READY_FOR_REVIEW")

    def test_non_release_task_has_no_release_plan(self) -> None:
        task_id = self.submit(FEATURE)
        self.plane.classify(task_id, "riley.chen")
        self.add(task_id, "plan")
        self.plane.plan(task_id, "riley.chen")
        self.assertRejected(ErrorCode.FORBIDDEN, self.plane.generate_release_plan, task_id, "riley.chen")
