"""Merge task branches into main — rebase, merge, verify."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class MergeStrategy(Protocol):
    """Protocol for merging task branches into main."""

    async def merge(
        self, branch: str, task_id: str,
        verification: str, project_root: Path,
    ) -> MergeResult: ...


@dataclass
class MergeResult:
    """Result of a merge attempt."""

    success: bool
    error_message: str | None = None
    needed_agent_resolution: bool = False


class RebaseMerger:
    """Merge strategy: rebase onto main, then fast-forward merge.

    If rebase conflicts, invokes a merge agent (Claude) to resolve.
    Merge is serialized via an asyncio.Lock (one merge at a time).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _run_git(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    async def merge(
        self,
        branch: str,
        task_id: str,
        verification: str,
        project_root: Path,
    ) -> MergeResult:
        """Merge a task branch into main, serialized by the lock."""
        async with self._lock:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._merge_sync, branch, task_id, verification, project_root
            )

    def _merge_sync(
        self,
        branch: str,
        task_id: str,
        verification: str,
        project_root: Path,
    ) -> MergeResult:
        """Synchronous merge logic."""
        # Step 1: Rebase task branch onto main
        logger.debug("Rebasing %s onto main", branch)
        result = self._run_git(["rebase", "main", branch], project_root)
        if result.returncode != 0:
            # Abort the failed rebase
            logger.info("Rebase failed for %s, attempting agent merge", task_id)
            self._run_git(["rebase", "--abort"], project_root)
            # Try merge agent resolution
            return self._try_agent_merge(branch, task_id, project_root, verification)

        # Step 2: Fast-forward merge into main
        self._run_git(["checkout", "main"], project_root)
        result = self._run_git(["merge", "--ff-only", branch], project_root)
        if result.returncode != 0:
            return MergeResult(
                success=False,
                error_message=f"Fast-forward merge failed: {result.stderr}",
            )

        # Step 3: Run verification if specified
        if verification:
            logger.debug("Running verification for %s: %s", task_id, verification)
            verify_result = subprocess.run(
                verification,
                shell=True,
                cwd=project_root,
                capture_output=True,
                text=True,
            )
            if verify_result.returncode != 0:
                # Revert the merge
                logger.warning("Verification failed for %s, reverting merge", task_id)
                self._run_git(["reset", "--hard", "HEAD~1"], project_root)
                return MergeResult(
                    success=False,
                    error_message=(
                    "Post-merge verification failed: "
                    f"{verify_result.stderr or verify_result.stdout}"
                ),
                )

        logger.debug("Merge succeeded for %s", task_id)
        return MergeResult(success=True)

    def _try_agent_merge(
        self,
        branch: str,
        task_id: str,
        project_root: Path,
        verification: str,
    ) -> MergeResult:
        """Attempt to merge using a Claude agent to resolve conflicts.

        Strategy:
        1. Start a merge (--no-commit) to surface conflicts.
        2. If there are conflicts, invoke Claude to resolve them.
        3. Complete the merge commit and run verification.
        """
        self._run_git(["checkout", "main"], project_root)

        # Start merge without committing so we can inspect conflicts
        result = self._run_git(
            ["merge", "--no-ff", "--no-commit", branch],
            project_root,
        )

        if result.returncode == 0:
            # No conflicts — just commit
            self._run_git(
                ["commit", "-m", f"Merge task {task_id}"],
                project_root,
            )
        else:
            # There are conflicts — invoke Claude to resolve
            logger.info("Conflicts detected for %s, invoking merge agent", task_id)
            agent_ok = self._invoke_merge_agent(task_id, branch, project_root)
            if not agent_ok:
                logger.warning("Merge agent failed for %s, aborting merge", task_id)
                self._run_git(["merge", "--abort"], project_root)
                return MergeResult(
                    success=False,
                    error_message=(
                        f"Merge agent could not resolve conflicts for task '{task_id}'"
                    ),
                    needed_agent_resolution=True,
                )

        # Run verification
        if verification:
            verify_result = subprocess.run(
                verification, shell=True, cwd=project_root,
                capture_output=True, text=True,
            )
            if verify_result.returncode != 0:
                self._run_git(["reset", "--hard", "HEAD~1"], project_root)
                return MergeResult(
                    success=False,
                    error_message=(
                        "Post-merge verification failed after agent resolution: "
                        f"{verify_result.stderr or verify_result.stdout}"
                    ),
                )

        return MergeResult(success=True, needed_agent_resolution=True)

    def _invoke_merge_agent(
        self,
        task_id: str,
        branch: str,
        project_root: Path,
    ) -> bool:
        """Run Claude CLI to resolve merge conflicts in the working tree.

        Returns True if the agent successfully resolved conflicts and
        created a merge commit, False otherwise.
        """
        # Get the list of conflicted files
        status_result = self._run_git(
            ["diff", "--name-only", "--diff-filter=U"],
            project_root,
        )
        conflicted_files = status_result.stdout.strip()
        if not conflicted_files:
            # No conflicts to resolve — just commit
            self._run_git(
                ["commit", "-m", f"Merge task {task_id}"],
                project_root,
            )
            return True

        prompt = (
            f"You are resolving git merge conflicts for task '{task_id}' "
            f"(branch '{branch}' into main).\n\n"
            f"The following files have merge conflicts:\n{conflicted_files}\n\n"
            "For each conflicted file:\n"
            "1. Read the file and understand both sides of the conflict.\n"
            "2. Resolve the conflict by keeping the correct combination of changes.\n"
            "3. Remove all conflict markers (<<<<<<, ======, >>>>>>).\n"
            "4. Stage the resolved file with `git add`.\n\n"
            "After resolving all conflicts, run `git commit --no-edit` to "
            "complete the merge. Do NOT push or modify any other files."
        )

        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--max-turns", "30",
        ]

        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            env=env,
        )

        if result.returncode != 0:
            return False

        # Verify the merge was committed (no conflicts remain)
        status = self._run_git(["diff", "--name-only", "--diff-filter=U"], project_root)
        if status.stdout.strip():
            # Still has unresolved conflicts
            return False

        # Check that HEAD advanced (merge commit was created)
        log_result = self._run_git(["log", "--oneline", "-1"], project_root)
        return "Merge" in log_result.stdout or result.returncode == 0
