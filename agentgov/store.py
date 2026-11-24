"""Local persistence: one JSON document per task, evidence files, sandboxes and a hash-chained audit log.

Concurrency: every read-modify-write goes through an exclusive lock file and an optimistic version check
(compare-and-swap on `version`). A writer holding a stale copy gets CONCURRENT_UPDATE instead of overwriting
another writer's change. Files are replaced atomically (write to a temporary file, then `os.replace`).

Integrity: the audit log is hash-chained, so editing, deleting or reordering an entry is detected by
`verify_audit`, and every applied entry records the digest of the task document it produced, so a task file
edited outside the control plane no longer matches its seal. Hashes detect tampering by someone who does not
also rewrite the whole chain; they are not signatures and do not replace authentication or access control on
the store directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import GENESIS, canonical_json, digest, digest_bytes
from .errors import ErrorCode, GovernanceError


@contextmanager
def file_lock(path: Path, timeout: float = 10.0, poll: float = 0.01) -> Iterator[None]:
    """Exclusive, cross-process lock using O_CREAT | O_EXCL. Works on Windows and POSIX."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            # Windows reports PermissionError while a just-released lock file is still pending deletion.
            if time.monotonic() > deadline:
                raise GovernanceError(ErrorCode.LOCK_TIMEOUT, f"could not acquire {path.name} within {timeout}s") from None
            time.sleep(poll)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        os.unlink(path)


def _replace(source: str, target: Path) -> None:
    # On Windows a reader in another process can briefly hold the target open; retry instead of failing.
    for attempt in range(50):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 49:
                raise
            time.sleep(0.01)


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.remove(temp)


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


class Store:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.tasks_dir = self.home / "tasks"
        self.evidence_dir = self.home / "evidence"
        self.workspaces_dir = self.home / "workspaces"
        self.audit_path = self.home / "audit.jsonl"
        self.counters_path = self.home / "counters.json"
        self.policy_path = self.home / "policy.json"
        self.actors_path = self.home / "actors.json"
        self.lock_path = self.home / ".lock"

    def exists(self) -> bool:
        return self.policy_path.is_file() and self.actors_path.is_file()

    def lock(self) -> Any:
        return file_lock(self.lock_path)

    # -- ids ----------------------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        with self.lock():
            counters = json.loads(self.counters_path.read_text(encoding="utf-8")) if self.counters_path.exists() else {}
            counters[prefix] = counters.get(prefix, 0) + 1
            write_atomic(self.counters_path, json_bytes(counters))
        return f"{prefix}-{counters[prefix]:04d}"

    # -- tasks --------------------------------------------------------------------

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _read_task(self, task_id: str) -> dict[str, Any]:
        path = self.task_path(task_id)
        if not path.is_file():
            raise GovernanceError(ErrorCode.NOT_FOUND, f"task {task_id} does not exist")
        return json.loads(path.read_text(encoding="utf-8"))

    def load_task(self, task_id: str) -> dict[str, Any]:
        with self.lock():
            return self._read_task(task_id)

    def load_task_sealed(self, task_id: str) -> tuple[dict[str, Any], str | None]:
        """The task and the digest recorded by its last applied audit entry, read under one lock."""
        with self.lock():
            task = self._read_task(task_id)
            seal = None
            for entry in reversed(self.read_audit()):
                if entry.get("task_id") == task_id and entry.get("outcome") == "applied":
                    seal = entry.get("task_digest")
                    break
        return task, seal

    def task_ids(self) -> list[str]:
        return sorted(p.stem for p in self.tasks_dir.glob("TASK-*.json")) if self.tasks_dir.exists() else []

    def create_task(self, task: dict[str, Any], audit: list[dict[str, Any]]) -> None:
        with self.lock():
            if self.task_path(task["task_id"]).exists():
                raise GovernanceError(ErrorCode.CONCURRENT_UPDATE, f"task {task['task_id']} already exists")
            for entry in audit:
                entry["task_digest"] = digest(task)
            write_atomic(self.task_path(task["task_id"]), json_bytes(task))
            self._append_audit(audit)

    def commit(self, task: dict[str, Any], expected_version: int, audit: list[dict[str, Any]]) -> dict[str, Any]:
        """Compare-and-swap: write only if nobody else changed the task since it was read."""
        with self.lock():
            current = self._read_task(task["task_id"])
            if current["version"] != expected_version:
                raise GovernanceError(ErrorCode.CONCURRENT_UPDATE,
                                      f"task {task['task_id']} changed (version {current['version']}, expected {expected_version}); reload and retry")
            task["version"] = expected_version + 1
            for entry in audit:
                entry["task_version"] = task["version"]
                entry["task_digest"] = digest(task)  # seals the document: edits outside the control plane are detectable
            write_atomic(self.task_path(task["task_id"]), json_bytes(task))
            self._append_audit(audit)
        return task

    # -- evidence -----------------------------------------------------------------

    def evidence_path(self, task_id: str, evidence_id: str) -> Path:
        return self.evidence_dir / task_id / f"{evidence_id}.json"

    def write_evidence(self, task_id: str, evidence_id: str, content: dict[str, Any]) -> str:
        data = json_bytes(content)
        write_atomic(self.evidence_path(task_id, evidence_id), data)
        return digest_bytes(data)

    # -- audit --------------------------------------------------------------------

    def append_audit(self, entries: list[dict[str, Any]]) -> None:
        with self.lock():
            self._append_audit(entries)

    def _last_entry(self) -> dict[str, Any] | None:
        if not self.audit_path.exists():
            return None
        lines = [line for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None

    def _append_audit(self, entries: list[dict[str, Any]]) -> None:
        last = self._last_entry()
        seq = last["seq"] if last else 0
        prev = last["entry_hash"] if last else GENESIS
        lines = []
        for entry in entries:
            seq += 1
            entry["seq"] = seq
            entry["prev_hash"] = prev
            entry.pop("entry_hash", None)
            entry["entry_hash"] = digest(entry)
            prev = entry["entry_hash"]
            lines.append(canonical_json(entry))
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(line + "\n" for line in lines))

    def read_audit(self) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def verify_audit(self) -> list[str]:
        """Problems found in the hash chain; empty means the chain is intact."""
        problems: list[str] = []
        prev = GENESIS
        if not self.audit_path.exists():
            return problems
        for number, line in enumerate(self.audit_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {number}: not valid JSON")
                continue
            body = {key: value for key, value in entry.items() if key != "entry_hash"}
            if entry.get("seq") != number:
                problems.append(f"line {number}: sequence {entry.get('seq')} out of order")
            if entry.get("prev_hash") != prev:
                problems.append(f"line {number}: previous-hash link broken")
            if digest(body) != entry.get("entry_hash"):
                problems.append(f"line {number}: entry content does not match its hash")
            prev = entry.get("entry_hash", "")
        return problems
