"""Stable error codes. Every refusal by the control plane carries one of these."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    NOT_FOUND = "NOT_FOUND"
    NOT_PERMITTED = "NOT_PERMITTED"  # the actor's role does not allow this command
    INVALID_TRANSITION = "INVALID_TRANSITION"
    GATE_FAILED = "GATE_FAILED"
    FORBIDDEN = "FORBIDDEN"  # the request or action is outside policy
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    STALE_AUTHORIZATION = "STALE_AUTHORIZATION"
    DUPLICATE_EXECUTION = "DUPLICATE_EXECUTION"
    CONCURRENT_UPDATE = "CONCURRENT_UPDATE"
    INTEGRITY_ERROR = "INTEGRITY_ERROR"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"


class GovernanceError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}
