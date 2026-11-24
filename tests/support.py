"""Shared fixtures: a fresh control-plane home per test and helpers to drive a task through its lifecycle."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agentgov.clock import SteppingClock
from agentgov.engine import ControlPlane
from agentgov.errors import ErrorCode, GovernanceError
from agentgov.scenarios import build_evidence

REPO_ROOT = Path(__file__).resolve().parents[1]

DOCS = {"title": "Document tag search", "project": "lumen-notes", "requester": "riley.chen",
        "steps": [{"action": "edit_docs", "path": "docs/usage.md", "content": "# Usage\n\nSearch by tag.\n"}]}
FEATURE = {"title": "Case-insensitive search", "project": "lumen-notes", "requester": "riley.chen",
           "steps": [{"action": "edit_source", "path": "src/notes/search.py", "content": "def search():\n    return []\n"},
                     {"action": "run_tests"}]}
AUTH = {"title": "Shorter sessions", "project": "lumen-notes", "requester": "riley.chen",
        "steps": [{"action": "change_auth", "path": "src/auth/session.py", "content": "SESSION_MINUTES = 30\n"}]}
RELEASE = {"title": "Release 1.4.0", "project": "lumen-notes", "requester": "riley.chen",
           "release": {"version": "1.4.0"}, "steps": [{"action": "prepare_release"}, {"action": "publish_release"}]}


def request(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(base)
    data.update(changes)
    return data


class PlaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="agentgov-test-")
        self.home = Path(self._tmp.name) / "home"
        ControlPlane.init_home(self.home)
        self.plane = ControlPlane(self.home, clock=SteppingClock())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def task(self, task_id: str) -> dict[str, Any]:
        return self.plane.store.load_task(task_id)

    def assertRejected(self, code: ErrorCode, fn: Any, *args: Any, **kwargs: Any) -> GovernanceError:
        with self.assertRaises(GovernanceError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def submit(self, base: dict[str, Any] = DOCS, actor: str | None = None) -> str:
        return self.plane.submit(copy.deepcopy(base), actor or base["requester"])["task_id"]

    def add(self, task_id: str, kind: str, overrides: dict[str, Any] | None = None, actor: str = "riley.chen") -> dict[str, Any]:
        return self.plane.add_evidence(task_id, kind, build_evidence(kind, self.task(task_id), overrides), "test", actor)

    def to_ready(self, base: dict[str, Any] = DOCS) -> str:
        """Submit and drive a task to READY_FOR_REVIEW with valid evidence for its level."""
        task_id = self.submit(base)
        actor = base["requester"]
        self.plane.classify(task_id, actor)
        self.add(task_id, "plan", actor=actor)
        self.plane.plan(task_id, actor)
        for kind in self.task(task_id)["requirements"]["evidence"]:
            if kind in ("test_report", "risk_assessment"):
                self.add(task_id, kind, actor=actor)
        if "release_plan" in self.task(task_id)["requirements"]["evidence"]:
            self.plane.generate_release_plan(task_id, actor)
        result = self.plane.review(task_id, actor)
        self.assertEqual(result["state"], "READY_FOR_REVIEW", result["reason"])
        return task_id

    def to_authorized(self, base: dict[str, Any] = DOCS, approvers: tuple[str, ...] = ("sam.ortiz", "kai.morgan")) -> str:
        task_id = self.to_ready(base)
        result = self.plane.request_authorization(task_id, base["requester"])
        for approver in approvers:
            if result["state"] == "AUTHORIZED":
                break
            result = self.plane.approve(task_id, approver, self.plane.review_fingerprint(self.task(task_id)))
        self.assertEqual(result["state"], "AUTHORIZED")
        return task_id
