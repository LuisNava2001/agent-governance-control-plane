"""A per-execution directory the worker may write to, and nothing else.

Every path is checked twice: lexically (project-relative, no `..`, no absolute or drive paths) and after
resolution (the real path must stay under the sandbox root, so symlinks cannot be used to escape).
"""

from __future__ import annotations

from pathlib import Path

from .canonical import digest_bytes
from .errors import ErrorCode, GovernanceError
from .policy import safe_relative_path


class Sandbox:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._real_root = self.root.resolve()

    def resolve(self, relative: str) -> Path:
        safe = safe_relative_path(relative)
        if safe is None or safe != relative:
            raise GovernanceError(ErrorCode.SANDBOX_VIOLATION, f"path {relative!r} is not a plain sandbox-relative path")
        target = self.root / safe
        current = self.root
        for part in safe.split("/"):
            current = current / part
            if current.is_symlink():
                raise GovernanceError(ErrorCode.SANDBOX_VIOLATION, f"path {relative!r} goes through a symlink")
        if not target.resolve().is_relative_to(self._real_root):
            raise GovernanceError(ErrorCode.SANDBOX_VIOLATION, f"path {relative!r} resolves outside the sandbox")
        return target

    def write_text(self, relative: str, text: str) -> None:
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def read_text(self, relative: str) -> str:
        return self.resolve(relative).read_bytes().decode("utf-8")

    def materialize(self, files: dict[str, str]) -> None:
        for relative, text in sorted(files.items()):
            self.write_text(relative, text)

    def manifest(self) -> dict[str, str]:
        """Digest of every regular file in the sandbox, keyed by POSIX relative path."""
        entries: dict[str, str] = {}
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink():
                entries[path.relative_to(self.root).as_posix()] = "symlink"
            elif path.is_file():
                entries[path.relative_to(self.root).as_posix()] = digest_bytes(path.read_bytes())
        return entries
