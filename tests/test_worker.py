import ast
import os
import tempfile
import unittest
from pathlib import Path

from agentgov.clock import SteppingClock
from agentgov.engine import ControlPlane
from agentgov.errors import ErrorCode, GovernanceError
from agentgov.sandbox import Sandbox
from agentgov.worker import SimulatedWorker

from .support import DOCS, FEATURE, REPO_ROOT, PlaneTestCase, request

FORBIDDEN_MODULES = {"subprocess", "socket", "urllib", "http", "requests", "shutil", "os", "ctypes",
                     "multiprocessing", "asyncio", "importlib", "pty", "signal", "ssl", "ftplib", "smtplib"}


class SandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.outside = Path(self._tmp.name)
        self.sandbox = Sandbox(self.outside / "box")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_paths_cannot_escape(self) -> None:
        for bad in ("../escape.txt", "/etc/passwd", "C:/x.txt", "a/../../escape.txt", "a\\b.txt", ""):
            with self.assertRaises(GovernanceError) as caught:
                self.sandbox.write_text(bad, "x")
            self.assertEqual(caught.exception.code, ErrorCode.SANDBOX_VIOLATION, bad)
        self.assertFalse((self.outside / "escape.txt").exists())

    def test_symlinks_cannot_be_used_to_escape(self) -> None:
        try:
            os.symlink(self.outside, self.sandbox.root / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("creating symlinks is not permitted on this machine")
        with self.assertRaises(GovernanceError):
            self.sandbox.write_text("link/escape.txt", "x")
        self.assertFalse((self.outside / "escape.txt").exists())


class WorkerModuleTests(unittest.TestCase):
    def test_worker_and_sandbox_import_nothing_that_reaches_outside(self) -> None:
        for name in ("worker.py", "sandbox.py"):
            tree = ast.parse((REPO_ROOT / "agentgov" / name).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, {"open", "eval", "exec", "__import__", "compile"}, name)
            self.assertEqual(imported & FORBIDDEN_MODULES, set(), name)


class RogueWorker(SimulatedWorker):
    """Misbehaving agents. Each mode breaks one rule; the control plane must catch it."""

    name = "rogue-worker"

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def run(self, assignment, sandbox):
        if self.mode == "crash":
            raise RuntimeError("agent fell over")
        if self.mode == "escape":
            sandbox.write_text("../../outside.txt", "pwned")
        report = super().run(assignment, sandbox)
        if self.mode == "extra_file":
            (sandbox.root / "src" / "backdoor.py").write_text("OPEN = True\n", encoding="utf-8")  # bypasses the API
        elif self.mode == "wrong_content":
            sandbox.write_text(assignment["steps"][0]["path"], "something else\n")
        elif self.mode == "extra_step":
            report.steps.append(report.steps[0])
        elif self.mode == "claims":
            report.claims = {"approved_by": "kai.morgan", "deployed": True, "authorization": "granted"}
        return report


class RogueWorkerTests(PlaneTestCase):
    def run_with(self, mode: str, base=DOCS):
        task_id = self.to_authorized(base)
        plane = ControlPlane(self.home, clock=SteppingClock("2026-03-01T00:00:00Z"), worker=RogueWorker(mode))
        return task_id, plane.execute(task_id, "agent.worker")

    def test_rule_breaking_workers_end_blocked(self) -> None:
        expectations = {
            "crash": "worker crashed: RuntimeError",
            "escape": "SANDBOX_VIOLATION",
            "extra_file": "files changed outside the authorized outputs: ['src/backdoor.py']",
            "wrong_content": "does not contain the authorized content",
            "extra_step": "differ from the authorized steps",
        }
        for mode, fragment in expectations.items():
            with self.subTest(mode=mode):
                task_id, result = self.run_with(mode)
                self.assertEqual(result["state"], "BLOCKED")
                self.assertIn(fragment, " ".join(result["execution"]["violations"]))
                self.assertEqual(self.task(task_id)["execution"]["status"], "FAILED")
        self.assertFalse((self.home.parent / "outside.txt").exists())

    def test_worker_claims_are_never_authorization(self) -> None:
        task_id, result = self.run_with("claims")
        self.assertEqual(result["state"], "VERIFIED")  # the work itself was fine
        self.assertIn("ignored worker claims", " ".join(result["execution"]["notes"]))
        task = self.task(task_id)
        self.assertEqual([a["approver"] for a in task["approvals"]], [])  # no approval was created from the claim
        self.assertIsNone(task["release"])

    def test_failing_simulated_tests_block_the_task(self) -> None:
        failing = request(FEATURE, steps=[{"action": "add_tests", "path": "tests/test_fail.txt", "content": "SIMULATED_FAILURE\n"},
                                          {"action": "run_tests"}])
        task_id = self.to_authorized(failing)
        result = self.plane.execute(task_id, "agent.worker")
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("simulated test run failed", " ".join(result["execution"]["violations"]))

    def test_execution_writes_only_inside_its_sandbox(self) -> None:
        task_id = self.to_authorized(DOCS)
        before = {p for p in self.home.rglob("*") if p.is_file()}
        self.plane.execute(task_id, "agent.worker")
        created = {p for p in self.home.rglob("*") if p.is_file()} - before
        sandbox = self.home / "workspaces" / task_id
        self.assertTrue(created)
        for path in created:
            self.assertTrue(path.is_relative_to(sandbox), path)
