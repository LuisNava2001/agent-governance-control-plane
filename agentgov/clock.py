"""Clocks. The demo uses a stepping clock so every trace is reproducible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Clock(Protocol):
    def now(self) -> str: ...


class SystemClock:
    def now(self) -> str:
        return datetime.now(UTC).strftime(FORMAT)


class SteppingClock:
    """Returns start, start + step, start + 2*step, ... on successive calls."""

    def __init__(self, start: str = "2026-01-15T09:00:00Z", step_seconds: int = 60) -> None:
        self._next = datetime.strptime(start, FORMAT).replace(tzinfo=UTC)
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> str:
        value = self._next
        self._next += self._step
        return value.strftime(FORMAT)
