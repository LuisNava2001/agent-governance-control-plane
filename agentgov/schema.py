"""A small JSON Schema validator (draft 2020-12 subset) built on the standard library.

The schemas in agentgov/schemas are ordinary JSON Schema documents; this module implements only the keywords
they use. `unsupported_keywords` lets the tests prove no schema relies on a keyword that would be ignored.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from functools import cache
from importlib import resources
from typing import Any

from .canonical import canonical_json
from .errors import ErrorCode, GovernanceError

SUPPORTED = {
    "$schema", "$id", "title", "description", "type", "properties", "required", "additionalProperties", "enum",
    "const", "pattern", "minLength", "maxLength", "minimum", "maximum", "items", "minItems", "maxItems",
    "uniqueItems", "format",
}
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    raise ValueError(f"unknown JSON type {name}")


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of human-readable errors; empty means valid."""
    errors: list[str] = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, t) for t in types):
            return [f"{path}: expected {' or '.join(types)}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match {schema['pattern']}")
        if schema.get("format") == "date-time":
            try:
                ok = bool(_DATE_TIME.match(instance)) and bool(datetime.strptime(instance, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                ok = False
            if not ok:
                errors.append(f"{path}: not a UTC date-time (YYYY-MM-DDThh:mm:ssZ)")
    if _is_type(instance, "number"):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for key in sorted(set(instance) - set(properties)):
                errors.append(f"{path}: unexpected property {key!r}")
        for key, subschema in properties.items():
            if key in instance:
                errors.extend(validate(instance[key], subschema, f"{path}.{key}"))
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems") and len({canonical_json(item) for item in instance}) != len(instance):
            errors.append(f"{path}: items must be unique")
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
    return errors


def unsupported_keywords(schema: Any) -> set[str]:
    """Keywords this validator would silently ignore. Property names are not keywords."""
    found: set[str] = set()
    if isinstance(schema, dict):
        found |= set(schema) - SUPPORTED
        for name, value in schema.items():
            if name == "properties":
                for subschema in value.values():
                    found |= unsupported_keywords(subschema)
            elif name == "items":
                found |= unsupported_keywords(value)
    return found


def schema_names() -> list[str]:
    folder = resources.files(__package__).joinpath("schemas")
    return sorted(entry.name.removesuffix(".schema.json") for entry in folder.iterdir() if entry.name.endswith(".schema.json"))


@cache
def load_schema(name: str) -> dict[str, Any]:
    text = resources.files(__package__).joinpath("schemas", f"{name}.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def check(instance: Any, name: str) -> None:
    errors = validate(instance, load_schema(name))
    if errors:
        shown = "; ".join(errors[:6]) + (f"; and {len(errors) - 6} more" if len(errors) > 6 else "")
        raise GovernanceError(ErrorCode.SCHEMA_INVALID, f"{name}: {shown}", {"errors": errors})
