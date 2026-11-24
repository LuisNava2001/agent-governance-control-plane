import random
import unittest

from agentgov.policy import Policy, matches, safe_relative_path

from .support import AUTH, DOCS, FEATURE, RELEASE, request


def steps(*items):
    return [dict(item) for item in items]


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.default()

    def level(self, req):
        return self.policy.classify(req).level

    def test_reference_levels(self) -> None:
        self.assertEqual(self.level(DOCS), "LOW")
        self.assertEqual(self.level(FEATURE), "MEDIUM")
        self.assertEqual(self.level(AUTH), "HIGH")
        self.assertEqual(self.level(RELEASE), "CRITICAL")

    def test_path_rules_raise_the_level_regardless_of_the_action(self) -> None:
        req = request(FEATURE, steps=steps({"action": "edit_source", "path": "src/auth/tokens.py", "content": "x"}))
        self.assertEqual(self.level(req), "HIGH")  # edit_source is MEDIUM but src/auth/** is HIGH
        req = request(FEATURE, steps=steps({"action": "edit_source", "path": "src/migrations/0002.sql", "content": "x"}))
        self.assertEqual(self.level(req), "HIGH")

    def test_declared_lower_level_is_ignored(self) -> None:
        result = self.policy.classify(request(AUTH, declared_risk="LOW"))
        self.assertEqual(result.level, "HIGH")
        self.assertIn("D-IGNORED", [f.rule for f in result.findings])

    def test_declared_higher_level_is_honoured(self) -> None:
        result = self.policy.classify(request(DOCS, declared_risk="HIGH"))
        self.assertEqual((result.computed_level, result.level), ("LOW", "HIGH"))

    def test_large_change_sets_escalate(self) -> None:
        many = [{"action": "edit_docs", "path": f"docs/page{i}.md", "content": "x"} for i in range(10)]
        self.assertEqual(self.level(request(DOCS, steps=many)), "HIGH")
        self.assertEqual(self.level(request(DOCS, steps=many[:9])), "LOW")

    def test_forbidden_and_unknown_actions_block(self) -> None:
        for action in ("run_shell", "deploy_production", "read_secrets", "call_external_api", "rm_everything"):
            result = self.policy.classify(request(DOCS, steps=steps({"action": action})))
            self.assertIsNone(result.level, action)
            self.assertTrue(result.forbidden)

    def test_action_may_only_write_its_own_paths(self) -> None:
        disguised = request(DOCS, steps=steps({"action": "edit_docs", "path": "src/notes/app.py", "content": "x"}))
        result = self.policy.classify(disguised)
        self.assertIsNone(result.level)
        self.assertIn("R-ACTION-SCOPE", [f.rule for f in result.findings])

    def test_path_escapes_and_secret_paths_block(self) -> None:
        for path in ("../other/README.md", "/etc/passwd", "C:/Windows/x.md", "docs\\..\\x.md", "docs/../../x.md",
                     "~/notes.md", "secrets/key.md", "docs/.env.md"):
            req = request(DOCS, steps=steps({"action": "edit_docs", "path": path, "content": "x"}))
            self.assertIsNone(self.level(req), path)

    def test_structural_errors_block(self) -> None:
        cases = [
            steps({"action": "edit_docs", "content": "x"}),  # write action without a path
            steps({"action": "run_tests", "path": "docs/x.md"}),  # non-write action with a path
            steps({"action": "edit_docs", "path": "docs/a.md", "content": "x"}, {"action": "edit_docs", "path": "docs/a.md", "content": "y"}),
        ]
        for case in cases:
            self.assertIsNone(self.level(request(DOCS, steps=case)), case)
        without_version = {key: value for key, value in RELEASE.items() if key != "release"}
        self.assertIsNone(self.level(without_version))  # release actions need release.version

    def test_unmatched_paths_are_treated_conservatively(self) -> None:
        result = self.policy.classify(request(FEATURE, steps=steps({"action": "edit_source", "path": "src/new_area/x.py", "content": "x"})))
        self.assertEqual(result.level, "MEDIUM")  # src/** still matches
        policy = Policy({**self.policy.data, "path_rules": []})
        self.assertEqual(policy.classify(DOCS).level, "HIGH")

    def test_classification_is_deterministic_and_order_independent_for_the_level(self) -> None:
        mixed = request(FEATURE, steps=steps(
            {"action": "edit_docs", "path": "docs/usage.md", "content": "x"},
            {"action": "edit_source", "path": "src/notes/app.py", "content": "y"},
            {"action": "change_auth", "path": "src/auth/session.py", "content": "z"},
            {"action": "run_tests"}))
        first = self.policy.classify(mixed)
        self.assertEqual(first, self.policy.classify(mixed))
        rng = random.Random(7)
        for _ in range(20):
            shuffled = request(mixed, steps=rng.sample(mixed["steps"], len(mixed["steps"])))
            self.assertEqual(self.policy.classify(shuffled).level, "HIGH")

    def test_requirements_differ_by_level_and_releases_need_a_plan(self) -> None:
        reqs = {level: self.policy.requirements(level, DOCS) for level in ("LOW", "MEDIUM", "HIGH", "CRITICAL")}
        self.assertEqual([r["approvals"] for r in reqs.values()], [0, 0, 1, 2])
        self.assertEqual(reqs["LOW"]["evidence"], ["plan"])
        self.assertIn("release_manager", reqs["CRITICAL"]["approver_roles"])
        self.assertIn("release_plan", self.policy.requirements("HIGH", RELEASE)["evidence"])

    def test_policy_digest_changes_with_the_rules(self) -> None:
        changed = Policy({**self.policy.data, "unmatched_path_level": "CRITICAL"})
        self.assertNotEqual(changed.digest, self.policy.digest)


class PathTests(unittest.TestCase):
    def test_glob_semantics(self) -> None:
        self.assertTrue(matches("docs/**", "docs/a/b.md"))
        self.assertTrue(matches("*.md", "README.md"))
        self.assertFalse(matches("*.md", "docs/README.md"))
        self.assertTrue(matches("**/.env*", ".env"))
        self.assertTrue(matches("**/.env*", "src/config/.env.local"))
        self.assertFalse(matches("src/auth/**", "src/authz.py"))

    def test_safe_relative_path(self) -> None:
        self.assertEqual(safe_relative_path("docs/./a.md"), "docs/a.md")
        for bad in ("", "/x", "../x", "a/../../x", "C:x", "a\\b", "~x", "a\x00b"):
            self.assertIsNone(safe_relative_path(bad), bad)


if __name__ == "__main__":
    unittest.main()
