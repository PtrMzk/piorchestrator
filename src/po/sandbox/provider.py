"""Sandbox provider protocol and base implementations."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SandboxError(RuntimeError):
    """Raised when sandbox setup or execution fails."""


class SandboxProvider(Protocol):
    """Protocol for wrapping agent commands in a sandbox."""

    async def prepare(self) -> None:
        """One-time setup (e.g. build container image).

        Called once before the orchestration loop starts.
        Raises SandboxError on failure.
        """
        ...

    def wrap_command(
        self,
        cmd: list[str],
        *,
        worktree_path: Path,
        project_root: Path,
        env: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        """Transform a command to run inside the sandbox.

        Returns (new_cmd, new_env).  The caller uses these in place of
        the original cmd/env when spawning the subprocess.
        """
        ...


class NoSandbox:
    """Passthrough — runs commands directly on the host (default)."""

    async def prepare(self) -> None:
        pass

    def wrap_command(
        self,
        cmd: list[str],
        *,
        worktree_path: Path,
        project_root: Path,
        env: dict[str, str],
    ) -> tuple[list[str], dict[str, str]]:
        return cmd, env
