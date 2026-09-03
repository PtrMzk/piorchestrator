"""Git worktree management — create, cleanup, list."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from po.config import worktrees_dir


class WorktreeProvider(Protocol):
    """Protocol for worktree management."""

    def create(self, task_id: str, project_root: Path) -> WorktreeInfo: ...
    def detach(self, task_id: str, project_root: Path) -> None: ...
    def remove(self, task_id: str, project_root: Path) -> None: ...
    def list(self, project_root: Path) -> list[WorktreeInfo]: ...
    def exists(self, task_id: str, project_root: Path) -> bool: ...


@dataclass
class WorktreeInfo:
    """Info about a git worktree."""

    task_id: str
    path: Path
    branch: str


def ensure_git_repo(project_root: Path) -> None:
    """Ensure project_root is a git repo with at least one commit.

    Module-level because the orchestrator needs it before the first worktree
    exists, to commit a baseline .gitignore (see OrchestratorLoop._prepare_repo).
    """
    # Check if inside a git work tree
    check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        subprocess.run(
            ["git", "init"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )

    # Check if HEAD exists (repo may be initialized but have no commits)
    head_check = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if head_check.returncode != 0:
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "Initial commit (po)"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=True,
        )


def _git(args: list[str], cwd: Path, check: bool = True) -> int:
    """Run a git command; on failure raise RuntimeError carrying git's stderr.

    subprocess.CalledProcessError only says "exit status 128", which for
    `worktree add` could be any of a dozen things. The message is what the
    user needs to see.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (
            result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        )
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.returncode


class GitWorktreeManager:
    """Manage git worktrees for task isolation."""

    _ensure_git_repo = staticmethod(ensure_git_repo)

    def _branch_name(self, task_id: str) -> str:
        return f"po/{task_id}"

    def _worktree_path(self, task_id: str, project_root: Path) -> Path:
        return worktrees_dir(project_root) / task_id

    def create(self, task_id: str, project_root: Path) -> WorktreeInfo:
        """Return a worktree for the task, reusing a previous attempt's if it is intact.

        A failed attempt leaves its worktree and branch in place so a retry, or
        `po reset` + `po run`, continues from that work instead of starting
        over. Three cases, in order:

        1. The directory is still a registered worktree on `po/<task>`: reuse
           it as-is, uncommitted changes included.
        2. Only the branch survives (the worktree was detached for a merge, or
           the directory is stale): re-attach a fresh worktree to the branch.
        3. Neither exists: cut a new branch from HEAD.

        A stale directory is one git no longer recognises — typically left by
        an agent's orphaned children writing into it after the worktree was
        removed. It is deleted; anything of value is on the branch.

        Raises RuntimeError with git's stderr on failure.
        """
        branch = self._branch_name(task_id)
        wt_path = self._worktree_path(task_id, project_root)
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        # Ensure a git repo exists with at least one commit (needed for worktrees)
        self._ensure_git_repo(project_root)

        if self._attached_branch(wt_path, project_root) == branch:
            return WorktreeInfo(task_id=task_id, path=wt_path, branch=branch)

        if wt_path.exists():
            _git(["worktree", "remove", "--force", str(wt_path)], project_root, check=False)
            if wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
        # Prune worktree bookkeeping for paths that no longer exist
        _git(["worktree", "prune"], project_root, check=False)

        if _git(["rev-parse", "--verify", "--quiet", branch], project_root, check=False) == 0:
            # Reuse existing branch — reattach worktree to it
            _git(["worktree", "add", str(wt_path), branch], project_root)
        else:
            # Fresh branch from current HEAD
            _git(["worktree", "add", "-b", branch, str(wt_path), "HEAD"], project_root)

        return WorktreeInfo(task_id=task_id, path=wt_path, branch=branch)

    @staticmethod
    def _attached_branch(wt_path: Path, project_root: Path) -> str | None:
        """The branch checked out at `wt_path` if git lists it as a worktree, else None."""
        if not wt_path.is_dir():
            return None
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        target = wt_path.resolve()
        current: Path | None = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current = Path(line[len("worktree ") :]).resolve()
            elif line.startswith("branch ") and current == target:
                return line[len("branch ") :].removeprefix("refs/heads/")
        return None

    def detach(self, task_id: str, project_root: Path) -> None:
        """Remove the worktree directory but keep the branch.

        Use this before merging so that ``git rebase`` / ``git checkout``
        can access the branch (git refuses to check out a branch that is
        already checked out in another worktree).
        """
        wt_path = self._worktree_path(task_id, project_root)
        if wt_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=project_root,
                capture_output=True,
                text=True,
                check=False,
            )
        # Prune so git no longer considers the branch checked out elsewhere
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )

    def remove(self, task_id: str, project_root: Path) -> None:
        """Remove a worktree and its branch."""
        self.detach(task_id, project_root)

        # Delete the branch
        branch = self._branch_name(task_id)
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
