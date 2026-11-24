import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agentgov.cli import main
from agentgov.scenarios import build_evidence

from .support import AUTH, DOCS


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.home = str(self.dir / "home")
        self.assertEqual(self.call("init")[0], 0)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--home", self.home, *args])
        return code, out.getvalue(), err.getvalue()

    def file(self, name: str, data) -> str:
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return str(path)

    def evidence(self, task_id: str, kind: str) -> str:
        from agentgov.engine import ControlPlane

        task = ControlPlane(self.home).store.load_task(task_id)
        return self.file(f"{kind}.json", build_evidence(kind, task))

    def test_full_low_risk_flow_through_the_cli(self) -> None:
        code, out, _ = self.call("submit", self.file("req.json", DOCS), "--actor", "riley.chen", "--json")
        self.assertEqual(code, 0)
        task_id = json.loads(out)["task_id"]
        for args in (["classify", task_id, "--actor", "riley.chen"],
                     ["evidence", task_id, "--type", "plan", "--file", self.evidence(task_id, "plan"), "--actor", "riley.chen"],
                     ["plan", task_id, "--actor", "riley.chen"],
                     ["review", task_id, "--actor", "riley.chen"],
                     ["authorize", task_id, "--actor", "riley.chen"],
                     ["execute", task_id, "--actor", "agent.worker"],
                     ["complete", task_id, "--actor", "riley.chen"]):
            code, out, err = self.call(*args)
            self.assertEqual(code, 0, (args, out, err))
        code, out, _ = self.call("show", task_id, "--json")
        self.assertEqual(json.loads(out)["state"], "COMPLETED")
        self.assertIn("COMPLETED", self.call("list")[1])
        self.assertIn("complete", self.call("audit", "--task", task_id)[1])
        self.assertEqual(self.call("verify"), (0, "integrity OK\n", ""))

    def test_refusals_exit_with_code_2_and_a_reason(self) -> None:
        _, out, _ = self.call("submit", self.file("req.json", AUTH), "--actor", "riley.chen", "--json")
        task_id = json.loads(out)["task_id"]
        code, _, err = self.call("execute", task_id, "--actor", "agent.worker")
        self.assertEqual(code, 2)
        self.assertIn("REJECTED INVALID_TRANSITION", err)
        code, _, err = self.call("approve", task_id, "--actor", "riley.chen", "--fingerprint", "sha256:x")
        self.assertEqual(code, 2)

    def test_explain_and_demo_commands(self) -> None:
        code, out, _ = self.call("explain", self.file("req.json", dict(AUTH, declared_risk="LOW")))
        self.assertEqual(code, 0)
        self.assertIn("level: HIGH", out)
        self.assertIn("D-IGNORED", out)
        code, out, _ = self.call("demo", "--scenario", "S5-forbidden-operation")
        self.assertEqual(code, 0)
        self.assertIn("PASS  S5-forbidden-operation", out)
        self.assertEqual(self.call("demo", "--list")[0], 0)

    def test_missing_home_and_bad_input(self) -> None:
        code, _, err = main_with_home(str(self.dir / "nowhere"), "list")
        self.assertEqual(code, 2)
        self.assertIn("agentgov init", err)
        self.assertEqual(self.call("submit", str(self.dir / "missing.json"), "--actor", "riley.chen")[0], 1)


def main_with_home(home: str, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--home", home, *args])
    return code, out.getvalue(), err.getvalue()


if __name__ == "__main__":
    unittest.main()
