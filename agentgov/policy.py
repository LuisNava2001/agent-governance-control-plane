"""Deterministic risk classification and requirements.

The level of a task is the maximum over every rule it triggers: the actions it requests, the paths those actions
write, and size escalations. A requester may declare a higher level, never a lower one. Anything the policy does
not recognise (an unknown action, an unclassified path) is treated conservatively or forbidden: the engine fails
closed. No language model is involved anywhere in this module.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from .canonical import digest
from .schema import check

LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


def rank(level: str) -> int:
    return LEVELS.index(level)


@cache
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob: `**/` any directories, `**` anything, `*` within one segment, `?` one character."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(pattern: str, path: str) -> bool:
    return bool(glob_regex(pattern).match(path))


def safe_relative_path(path: str) -> str | None:
    """Normalized project-relative POSIX path, or None if it could escape the project root."""
    if not path or "\\" in path or "\x00" in path or path.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", path):
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


@dataclass(frozen=True)
class Finding:
    rule: str
    detail: str
    level: str | None = None
    forbidden: bool = False


@dataclass(frozen=True)
class Classification:
    level: str | None  # None when the request is forbidden
    computed_level: str | None
    declared_level: str | None
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    policy_id: str = ""
    policy_digest: str = ""

    @property
    def forbidden(self) -> list[str]:
        return [f.detail for f in self.findings if f.forbidden]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["findings"] = [asdict(f) for f in self.findings]
        data["forbidden"] = self.forbidden
        return data


class Policy:
    def __init__(self, data: dict[str, Any]) -> None:
        check(data, "policy")
        self.data = data
        self.digest = digest(data)
        self.actions = {a["name"]: a for a in data["actions"]}
        self.forbidden_actions = {a["name"]: a["reason"] for a in data["forbidden_actions"]}
        self._requirements = {r["level"]: r for r in data["requirements"]}
        if set(self._requirements) != set(LEVELS):
            raise ValueError("policy must define requirements for every level")

    @classmethod
    def from_file(cls, path: Path) -> Policy:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def default(cls) -> Policy:
        return cls(default_policy_data())

    def needs_release(self, request: dict[str, Any]) -> bool:
        return any(self.actions.get(s["action"], {}).get("requires_release") for s in request["steps"])

    def requirements(self, level: str, request: dict[str, Any]) -> dict[str, Any]:
        base = self._requirements[level]
        evidence = list(base["evidence"])
        if self.needs_release(request) and "release_plan" not in evidence:
            evidence.append("release_plan")  # any release, whatever its level, carries a release plan
        return {"level": level, "evidence": evidence, "approvals": base["approvals"],
                "approver_roles": list(base["approver_roles"]), "summary": base["summary"]}

    def allowed_outputs(self, request: dict[str, Any]) -> set[str]:
        """Every sandbox path an execution of this request may create or change."""
        outputs: set[str] = set()
        for step in request["steps"]:
            spec = self.actions.get(step["action"], {})
            if step.get("path"):
                outputs.add(step["path"])
            outputs.update(spec.get("fixed_outputs", []))
        return outputs

    def classify(self, request: dict[str, Any]) -> Classification:
        findings: list[Finding] = []
        levels: list[str] = []
        seen: set[tuple[str, str | None]] = set()
        for index, step in enumerate(request["steps"]):
            action, path = step["action"], step.get("path")
            where = f"step {index + 1} ({action}{' ' + path if path else ''})"
            if (action, path) in seen:
                findings.append(Finding("R-DUPLICATE", f"{where}: duplicate step", forbidden=True))
                continue
            seen.add((action, path))
            if action in self.forbidden_actions:
                findings.append(Finding(f"F-{action}", f"{where}: {self.forbidden_actions[action]}", forbidden=True))
                continue
            spec = self.actions.get(action)
            if spec is None:
                findings.append(Finding("R-UNKNOWN-ACTION", f"{where}: action is not in the catalog", forbidden=True))
                continue
            levels.append(spec["level"])
            findings.append(Finding(f"A-{action}", f"{where}: {spec['description']}", spec["level"]))
            if spec["requires_release"] and not request.get("release"):
                findings.append(Finding("R-RELEASE-VERSION", f"{where}: needs release.version", forbidden=True))
            if not spec["writes"]:
                if path is not None:
                    findings.append(Finding("R-NO-PATH", f"{where}: this action takes no path", forbidden=True))
                continue
            safe = safe_relative_path(path) if path is not None else None
            if path is None:
                findings.append(Finding("R-PATH-REQUIRED", f"{where}: this action needs a path", forbidden=True))
            elif safe is None or safe != path:
                findings.append(Finding("R-PATH-ESCAPE", f"{where}: path is not a plain project-relative path", forbidden=True))
            elif not any(matches(glob, path) for glob in spec["writes"]):
                findings.append(Finding("R-ACTION-SCOPE", f"{where}: {action} may only write {spec['writes']}", forbidden=True))
            else:
                matched = [rule for rule in self.data["path_rules"] if matches(rule["glob"], path)]
                for rule in matched:
                    if rule.get("forbidden"):
                        findings.append(Finding(rule["id"], f"{where}: {rule['reason']}", forbidden=True))
                    else:
                        levels.append(rule["level"])
                        findings.append(Finding(rule["id"], f"{where}: {rule['reason']}", rule["level"]))
                if not matched:
                    levels.append(self.data["unmatched_path_level"])
                    findings.append(Finding("R-UNMATCHED-PATH", f"{where}: no path rule matches; treated conservatively",
                                            self.data["unmatched_path_level"]))
        for rule in self.data["escalations"]:
            if len(request["steps"]) >= rule["min_steps"]:
                levels.append(rule["level"])
                findings.append(Finding(rule["id"], rule["reason"], rule["level"]))

        computed = max(levels, key=rank) if levels else "LOW"
        declared = request.get("declared_risk")
        level = computed
        if declared and rank(declared) > rank(computed):
            level = declared
            findings.append(Finding("D-RAISED", f"declared {declared} raises the computed {computed}", declared))
        elif declared and rank(declared) < rank(computed):
            findings.append(Finding("D-IGNORED", f"declared {declared} ignored: computed level is {computed}"))
        forbidden = any(f.forbidden for f in findings)
        return Classification(level=None if forbidden else level, computed_level=computed, declared_level=declared,
                              findings=tuple(findings), policy_id=self.data["policy_id"], policy_digest=self.digest)


@cache
def _default_text() -> str:
    return resources.files(__package__).joinpath("data", "policy.json").read_text(encoding="utf-8")


def default_policy_data() -> dict[str, Any]:
    return json.loads(_default_text())
