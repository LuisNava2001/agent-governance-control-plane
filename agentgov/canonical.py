"""Canonical JSON and SHA-256 digests.

Fingerprints are digests of a canonical serialization (sorted keys, no insignificant whitespace, UTF-8).
The facts that get fingerprinted contain only strings, integers, booleans, lists and objects; floats are
deliberately avoided, so this simple canonical form is stable across platforms and Python versions.
A digest detects change. It is not a signature and proves nothing about who produced the data.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

GENESIS = "sha256:" + "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def digest(value: Any) -> str:
    return digest_text(canonical_json(value))


def short(fingerprint: str | None) -> str:
    """First 12 hex characters, for display only."""
    return fingerprint.split(":", 1)[-1][:12] if fingerprint else "-"
