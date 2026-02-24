"""Git worktree management — create, cleanup, list."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from po.config import worktrees_dir


class WorktreeProvider(Protocol):
    """Protocol for worktree management."""

    def create(self, task_id: str, project_root: Path) -> WorktreeInfo: ...
    def remove(self, task_id: str, project_root: Path) -> None: ...
    def list(self, project_root: Path) -> list[WorktreeInfo]: ...
    def exists(self, task_id: str, project_root: Path) -> bool: ...


@dataclass
class WorktreeInfo:
    """Info about a git worktree."""

    task_id: str
    path: Path
    branch: str


class GitWorktreeManager:
    """Manage git worktrees for task isolation."""

    def _branch_name(self, task_id: str) -> str:
        return f"po/{task_id}"

    def _worktree_path(self, task_id: str, project_root: Path) -> Path:
        return worktrees_dir(project_root) / task_id

    def create(self, task_id: str, project_root: Path) -> WorktreeInfo:
        """Create a new worktree branching from current main tip.

        Cleans up stale branches/worktrees from previous runs before creating.
        Raises subprocess.CalledProcessError on git failures.
        """
        branch = self._branch_name(task_id)
        wt_path = self._worktree_path(task_id, project_root)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Clean up stale worktree/branch from a previous run
        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            )
        # Prune worktree bookkeeping for paths that no longer exist
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        # Delete stale branch if it exists
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

        # Get current HEAD of main
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )
        base_commit = result.stdout.strip()

        # Create worktree with new branch from that commit
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(wt_path), base_commit],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )

        return WorktreeInfo(task_id=task_id, path=wt_path, branch=branch)

    def remove(self, task_id: str, project_root: Path) -> None:
        """Remove a worktree and its branch."""
        wt_path = self._worktree_path(task_id, project_root)
        branch = self._branch_name(task_id)

        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            )

        # Delete the branch
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def list(self, project_root: Path) -> list[WorktreeInfo]:
        """List all PO-managed worktrees."""
        wt_dir = worktrees_dir(project_root)
        if not wt_dir.exists():
            return []

        result: list[WorktreeInfo] = []
        for entry in sorted(wt_dir.iterdir()):
            if entry.is_dir():
                task_id = entry.name
                result.append(
                    WorktreeInfo(
                        task_id=task_id,
                        path=entry,
                        branch=self._branch_name(task_id),
                    )
                )
        return result

    def exists(self, task_id: str, project_root: Path) -> bool:
        """Check if a worktree exists for the given task."""
        return self._worktree_path(task_id, project_root).exists()
