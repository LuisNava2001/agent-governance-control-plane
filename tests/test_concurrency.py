import json
import subprocess
import sys
import threading

from agentgov.clock import SteppingClock
from agentgov.engine import ControlPlane
from agentgov.errors import ErrorCode, GovernanceError
from agentgov.store import file_lock

from .support import AUTH, DOCS, RELEASE, REPO_ROOT, PlaneTestCase


def race(workers: list) -> list:
    """Run callables at the same instant on separate threads; return their results or controlled refusals.

    Any other exception (or a thread that never finished) fails the calling test: concurrency must only ever
    produce a success or a GovernanceError, never a crash.
    """
    barrier = threading.Barrier(len(workers))
    results: list = [None] * len(workers)

    def run(index: int, fn) -> None:
        barrier.wait()
        try:
            results[index] = fn()
        except BaseException as err:  # noqa: BLE001 - recorded and asserted below
            results[index] = err

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    unexpected = [r for r in results if not isinstance(r, (dict, GovernanceError))]
    if unexpected:
        raise AssertionError(f"uncontrolled outcomes under concurrency: {unexpected!r}")
    return results


class ConcurrencyTests(PlaneTestCase):
    def other_plane(self) -> ControlPlane:
        """A second, independent control-plane instance on the same store (like another process)."""
        return ControlPlane(self.home, clock=SteppingClock("2026-02-01T00:00:00Z"))

    def test_stale_writer_gets_concurrent_update(self) -> None:
        task_id = self.submit(DOCS)
        stale = self.plane.store.load_task(task_id)
        self.plane.classify(task_id, "riley.chen")
        stale["state"] = "COMPLETED"
        with self.assertRaises(GovernanceError) as caught:
            self.plane.store.commit(stale, stale["version"], [])
        self.assertEqual(caught.exception.code, ErrorCode.CONCURRENT_UPDATE)
        self.assertEqual(self.task(task_id)["state"], "CLASSIFIED")

    def test_parallel_executions_run_exactly_once(self) -> None:
        task_id = self.to_authorized(DOCS)
        planes = [self.other_plane() for _ in range(6)]
        results = race([lambda p=p: p.execute(task_id, "agent.worker") for p in planes])
        successes = [r for r in results if isinstance(r, dict)]
        errors = {r.code for r in results if isinstance(r, GovernanceError)}
        self.assertEqual(len(successes), 1, results)
        self.assertLessEqual(errors, {ErrorCode.CONCURRENT_UPDATE, ErrorCode.DUPLICATE_EXECUTION, ErrorCode.INVALID_TRANSITION})
        task = self.task(task_id)
        self.assertEqual(task["state"], "VERIFIED")
        self.assertEqual([g["status"] for g in task["grants"]], ["CONSUMED"])
        self.assertEqual(len(list((self.home / "workspaces" / task_id).iterdir())), 1)  # one sandbox, one run

    def test_same_approver_racing_records_one_approval(self) -> None:
        task_id = self.to_ready(RELEASE)
        self.plane.request_authorization(task_id, "riley.chen")
        fingerprint = self.plane.review_fingerprint(self.task(task_id))
        planes = [self.other_plane() for _ in range(4)]
        race([lambda p=p: p.approve(task_id, "sam.ortiz", fingerprint) for p in planes])
        approvals = self.task(task_id)["approvals"]
        self.assertEqual([a["approver"] for a in approvals], ["sam.ortiz"])
        self.assertEqual(self.task(task_id)["state"], "APPROVAL_REQUIRED")

    def test_two_approvers_racing_reach_quorum_once(self) -> None:
        task_id = self.to_ready(RELEASE)
        self.plane.request_authorization(task_id, "riley.chen")
        fingerprint = self.plane.review_fingerprint(self.task(task_id))

        def approve_with_retry(approver: str):
            plane = self.other_plane()
            for _ in range(20):
                try:
                    return plane.approve(task_id, approver, fingerprint)
                except GovernanceError as err:
                    if err.code != ErrorCode.CONCURRENT_UPDATE:
                        raise
            raise AssertionError("never committed")

        race([lambda: approve_with_retry("sam.ortiz"), lambda: approve_with_retry("kai.morgan")])
        task = self.task(task_id)
        self.assertEqual(task["state"], "AUTHORIZED")
        self.assertEqual(sorted(a["approver"] for a in task["approvals"]), ["kai.morgan", "sam.ortiz"])
        self.assertEqual(len(task["grants"]), 1)

    def test_lock_times_out_instead_of_waiting_forever(self) -> None:
        lock = self.home / "test.lock"
        with file_lock(lock):
            with self.assertRaises(GovernanceError) as caught:
                with file_lock(lock, timeout=0.2):
                    pass
        self.assertEqual(caught.exception.code, ErrorCode.LOCK_TIMEOUT)
        self.assertFalse(lock.exists())

    def test_separate_processes_execute_once(self) -> None:
        task_id = self.to_authorized(AUTH)
        command = [sys.executable, "-B", "-m", "agentgov", "--home", str(self.home), "execute", task_id,
                   "--actor", "agent.worker", "--json"]
        processes = [subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(4)]
        outputs = [process.communicate(timeout=120) for process in processes]
        codes = [process.returncode for process in processes]
        self.assertEqual(codes.count(0), 1, outputs)
        self.assertEqual(codes.count(2), 3, outputs)
        winner = json.loads(outputs[codes.index(0)][0])
        self.assertEqual(winner["state"], "VERIFIED")
        self.assertEqual(self.plane.verify_integrity()["problems"], [])
