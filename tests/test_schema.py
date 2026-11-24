import json
import unittest

from agentgov.actors import default_actors_data
from agentgov.errors import ErrorCode, GovernanceError
from agentgov.policy import default_policy_data
from agentgov.scenarios import scenario_specs
from agentgov.schema import check, load_schema, schema_names, unsupported_keywords, validate

from .support import DOCS, request


class SchemaTests(unittest.TestCase):
    def test_every_schema_uses_only_supported_keywords(self) -> None:
        names = schema_names()
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            self.assertEqual(unsupported_keywords(load_schema(name)), set(), name)

    def test_shipped_data_is_valid(self) -> None:
        check(default_policy_data(), "policy")
        check(default_actors_data(), "actors")
        for spec in scenario_specs():
            check(spec["request"], "task_request")
            for step in spec["steps"]:
                if "request" in step:
                    check(step["request"], "task_request")

    def test_request_validation_rejects_bad_input(self) -> None:
        bad = [
            request(DOCS, title="x"),
            request(DOCS, project="Lumen Notes"),
            request(DOCS, steps=[]),
            request(DOCS, declared_risk="NONE"),
            request(DOCS, extra_field=True),
            request(DOCS, steps=[{"action": "edit_docs", "path": "docs/a.md", "shell": "ls"}]),
            request(DOCS, release={"version": "v1"}),
        ]
        for item in bad:
            with self.assertRaises(GovernanceError) as caught:
                check(item, "task_request")
            self.assertEqual(caught.exception.code, ErrorCode.SCHEMA_INVALID)

    def test_validator_keywords(self) -> None:
        schema = {"type": "object", "required": ["n", "s", "l"], "additionalProperties": False, "properties": {
            "n": {"type": "integer", "minimum": 0, "maximum": 5},
            "s": {"type": "string", "pattern": "^a+$", "minLength": 2, "maxLength": 3},
            "l": {"type": "array", "items": {"enum": [1, 2]}, "minItems": 1, "maxItems": 2, "uniqueItems": True},
            "t": {"type": "string", "format": "date-time"},
            "c": {"const": "fixed"}}}
        self.assertEqual(validate({"n": 3, "s": "aa", "l": [1, 2], "t": "2026-01-15T09:00:00Z", "c": "fixed"}, schema), [])
        errors = validate({"n": True, "s": "b", "l": [1, 1, 3], "t": "2026-13-40T00:00:00Z", "c": "other", "z": 1}, schema)
        joined = json.dumps(errors)
        for fragment in ("expected integer", "does not match", "shorter than", "items must be unique", "more than 2",
                         "must be one of", "date-time", "must equal", "unexpected property"):
            self.assertIn(fragment, joined)


if __name__ == "__main__":
    unittest.main()
