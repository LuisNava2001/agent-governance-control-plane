"""Local actor directory. A demo control: it names who may do what, it does not authenticate anyone."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from .errors import ErrorCode, GovernanceError
from .schema import check

CONTROL_PLANE = "control-plane"  # actor id used for decisions taken by policy, never by a person or agent


class Actors:
    def __init__(self, data: dict[str, Any]) -> None:
        check(data, "actors")
        self.by_id = {actor["id"]: actor for actor in data["actors"]}

    @classmethod
    def from_file(cls, path: Path) -> Actors:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def get(self, actor_id: str) -> dict[str, Any]:
        actor = self.by_id.get(actor_id)
        if actor is None:
            raise GovernanceError(ErrorCode.NOT_PERMITTED, f"unknown actor {actor_id!r}")
        return actor

    def has(self, actor_id: str, role: str) -> bool:
        return role in self.get(actor_id)["roles"]

    def require_human(self, actor_id: str, command: str) -> dict[str, Any]:
        actor = self.get(actor_id)
        if "agent" in actor["roles"]:
            raise GovernanceError(ErrorCode.NOT_PERMITTED, f"agents may not {command}")
        return actor


@cache
def _default_text() -> str:
    return resources.files(__package__).joinpath("data", "actors.json").read_text(encoding="utf-8")


def default_actors_data() -> dict[str, Any]:
    return json.loads(_default_text())
