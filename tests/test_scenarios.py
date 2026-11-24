import re
import unittest

from agentgov.scenarios import render_demo_data, run_all, scenario_specs

from .support import REPO_ROOT

TRACES = None


def traces():
    global TRACES
    if TRACES is None:
        TRACES = run_all()
    return TRACES


class ScenarioTests(unittest.TestCase):
    def test_at_least_five_scenarios_cover_the_required_stories(self) -> None:
        ids = [spec["id"] for spec in scenario_specs()]
        self.assertGreaterEqual(len(ids), 5)
        for required in ("S1-low-risk-docs", "S2-missing-evidence", "S3-human-approval", "S4-stale-approval",
                         "S5-forbidden-operation"):
            self.assertIn(required, ids)

    def test_every_scenario_behaves_as_specified(self) -> None:
        for trace in traces():
            failed = [s for s in trace["steps"] if not s["passed"]]
            self.assertEqual(failed, [], trace["id"])
            self.assertTrue(trace["integrity"]["audit_chain_intact"], trace["id"])

    def test_scenario_outcomes(self) -> None:
        by_id = {t["id"]: t for t in traces()}
        self.assertEqual(by_id["S1-low-risk-docs"]["final"]["state"], "COMPLETED")
        self.assertEqual(by_id["S5-forbidden-operation"]["final"]["state"], "BLOCKED")
        self.assertIsNone(by_id["S5-forbidden-operation"]["final"]["execution"])  # the worker never ran
        self.assertEqual(by_id["S6-critical-release"]["final"]["release"]["stage"], "SIMULATED")
        self.assertEqual(by_id["S6-critical-release"]["final"]["release"]["deployment"], "NOT_PERFORMED")
        stale = by_id["S4-stale-approval"]["final"]
        self.assertEqual([g["status"] for g in stale["grants"]], ["SUPERSEDED", "CONSUMED"])
        self.assertNotEqual(stale["grants"][0]["fingerprint"], stale["grants"][1]["fingerprint"])
        self.assertIn("content changed after submission", " ".join(by_id["S7-tampered-evidence"]["integrity"]["problems"]))

    def test_runs_are_deterministic(self) -> None:
        self.assertEqual(render_demo_data(run_all()), render_demo_data(traces()))

    def test_committed_demo_data_matches_a_fresh_run(self) -> None:
        committed = (REPO_ROOT / "demo" / "data.js").read_text(encoding="utf-8")
        self.assertEqual(committed, render_demo_data(traces()), "run `python -m agentgov demo --out demo/data.js`")

    def test_demo_page_is_static_and_offline(self) -> None:
        demo = REPO_ROOT / "demo"
        script = (demo / "app.js").read_text(encoding="utf-8")
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "fetch(", "XMLHttpRequest"):
            self.assertNotIn(forbidden, script)
        for name in ("index.html", "app.js", "styles.css", "data.js"):
            self.assertIsNone(re.search(r"https?://", (demo / name).read_text(encoding="utf-8")), name)


if __name__ == "__main__":
    unittest.main()
