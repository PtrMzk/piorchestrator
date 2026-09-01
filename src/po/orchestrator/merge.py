"""Merge task branches into main — rebase, merge, verify."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from po.config import agent_env, ensure_logs_dir

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
    """Merge strategy: rebase onto base branch, then fast-forward merge.

    If rebase conflicts, invokes a merge agent (Claude) to resolve.
    Merge is serialized via an asyncio.Lock (one merge at a time).
    """

    def __init__(self, base_branch: str | None = None) -> None:
        self._lock = asyncio.Lock()
        self._base_branch = base_branch

    def _get_base_branch(self, project_root: Path) -> str:
        """Return the base branch name, auto-detecting from HEAD if needed.

        The answer is cached for the process lifetime, so detection has to
        reject two readings of HEAD that look valid but are not:

        - **Detached HEAD** — mid-rebase, mid-bisect, or left that way by a
          crashed run. ``git rev-parse --abbrev-ref HEAD`` prints the literal
          string ``HEAD`` there. Caching that merges every task into a detached
          head for the rest of the run: the real branch silently never advances.
        - **A ``po/`` branch** — a task branch left checked out, which would
          make tasks merge into each other instead of into the base.

        Either way, fall back to the conventional default branch.
        """
        if self._base_branch is not None:
            return self._base_branch

        result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], project_root)
        candidate = result.stdout.strip() if result.returncode == 0 else ""
        if candidate and candidate != "HEAD" and not candidate.startswith("po/"):
            self._base_branch = candidate
            return self._base_branch

        for fallback in ("main", "master"):
            verify = self._run_git(
                ["rev-parse", "--verify", f"refs/heads/{fallback}"], project_root,
            )
            if verify.returncode == 0:
                logger.warning(
                    "HEAD is %s, not a usable base branch; falling back to '%s'",
                    f"'{candidate}'" if candidate else "unreadable", fallback,
                )
                self._base_branch = fallback
                return self._base_branch

        # Nothing usable. Return the conventional name so the caller's git
        # command fails with a real message instead of merging into nowhere.
        logger.error(
            "Could not determine a base branch (HEAD is %s, no main/master)",
            f"'{candidate}'" if candidate else "unreadable",
        )
        self._base_branch = "main"
        return self._base_branch

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

    def _ensure_gitignore(self, project_root: Path) -> None:
        """Ensure .gitignore contains common build artifact patterns.

        Idempotent: no-ops if all patterns are already present.
        Stages and commits the change to main so it persists.
        """
        gitignore = project_root / ".gitignore"
        existing = gitignore.read_text() if gitignore.exists() else ""

        patterns = ["node_modules/", "dist/", "build/"]
        missing = [p for p in patterns if p not in existing]
        if not missing:
            return

        lines = existing.rstrip("\n")
        if lines:
            lines += "\n"
        lines += "\n".join(missing) + "\n"
        gitignore.write_text(lines)

        self._run_git(["add", ".gitignore"], project_root)
        self._run_git(
            ["commit", "-m", "Add .gitignore for build artifacts"],
            project_root,
        )

    def _run_verification(
        self,
        verification: str,
        task_id: str,
        project_root: Path,
        after_agent: bool = False,
    ) -> MergeResult | None:
        """Run a verification command and return a failure MergeResult, or None on success.

        Logs full stdout/stderr to .po/logs/verify-<task_id>.log for debugging.
        """
        if not verification:
            return None

        logger.debug("Running verification for %s: %s", task_id, verification)
        verify_result = subprocess.run(
            shlex.split(verification),
            cwd=project_root,
            capture_output=True,
            text=True,
        )

        # Always write verification output to a log file
        log_dir = ensure_logs_dir(project_root)
        verify_log = log_dir / f"verify-{task_id}.log"
        with open(verify_log, "w") as fh:
            fh.write(f"Command: {verification}\n")
            fh.write(f"Exit code: {verify_result.returncode}\n")
            fh.write(f"--- stdout ---\n{verify_result.stdout}\n")
            fh.write(f"--- stderr ---\n{verify_result.stderr}\n")

        if verify_result.returncode == 0:
            return None

        logger.warning("Verification failed for %s, reverting merge", task_id)
        self._run_git(["reset", "--hard", "HEAD~1"], project_root)

        detail = verify_result.stderr or verify_result.stdout or "(no output)"
        # Truncate to last 500 chars to keep error messages readable
        if len(detail) > 500:
            detail = "..." + detail[-500:]
        prefix = (
            "Post-merge verification failed after agent resolution"
            if after_agent
            else "Post-merge verification failed"
        )
        return MergeResult(
            success=False,
            error_message=f"{prefix} (cmd: {verification}): {detail}",
            needed_agent_resolution=after_agent,
        )

    def _merge_sync(
        self,
        branch: str,
        task_id: str,
        verification: str,
        project_root: Path,
    ) -> MergeResult:
        """Synchronous merge logic."""
        # Clean up stale rebase/merge state from previous failed attempts.
        # `git rebase --abort` comes first and does the real work: it is what
        # puts HEAD back on a branch. Deleting the state directory alone leaves
        # HEAD detached, which then defeats base branch detection below — so
        # the rmtree is only a fallback for state git itself won't clear.
        self._run_git(["rebase", "--abort"], project_root)
        git_dir = project_root / ".git"
        for stale_dir in ("rebase-merge", "rebase-apply"):
            stale_path = git_dir / stale_dir
            if stale_path.exists():
                shutil.rmtree(stale_path)
        merge_head = self._run_git(
            ["rev-parse", "--verify", "MERGE_HEAD"], project_root
        )
        if merge_head.returncode == 0:
            self._run_git(["merge", "--abort"], project_root)

        # Detect only after recovery, so HEAD is on a branch again.
        base = self._get_base_branch(project_root)
        self._run_git(["checkout", "-f", base], project_root)

        # Ensure .gitignore covers build artifacts before merging
        self._ensure_gitignore(project_root)

        # Step 1: Rebase task branch onto base branch
        logger.debug("Rebasing %s onto %s", branch, base)
        result = self._run_git(["rebase", base, branch], project_root)
        if result.returncode != 0:
            # Abort the failed rebase
            logger.info(
                "Rebase failed for %s (stderr: %s), attempting agent merge",
                task_id, result.stderr.strip(),
            )
            self._run_git(["rebase", "--abort"], project_root)
            # Try merge agent resolution
            return self._try_agent_merge(branch, task_id, project_root, verification)

        # Step 2: Fast-forward merge into base branch
        self._run_git(["checkout", base], project_root)
        result = self._run_git(["merge", "--ff-only", branch], project_root)
        if result.returncode != 0:
            return MergeResult(
                success=False,
                error_message=f"Fast-forward merge failed: {result.stderr}",
            )

        # Step 3: Run verification if specified
        fail = self._run_verification(verification, task_id, project_root)
        if fail:
            return fail

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
        base = self._get_base_branch(project_root)
        self._run_git(["checkout", "-f", base], project_root)

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
        fail = self._run_verification(
            verification, task_id, project_root, after_agent=True,
        )
        if fail:
            return fail

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

        base = self._get_base_branch(project_root)
        prompt = (
            f"You are resolving git merge conflicts for task '{task_id}' "
            f"(branch '{branch}' into {base}).\n\n"
            f"The following files have merge conflicts:\n{conflicted_files}\n\n"
            "For each conflicted file:\n"
            "1. Read the file and understand both sides of the conflict.\n"
            "2. Resolve the conflict by keeping the correct combination of changes.\n"
            "3. Remove all conflict markers (<<<<<<, ======, >>>>>>).\n"
            "4. Stage the resolved file with `git add`.\n\n"
            "After resolving all conflicts, run `git commit --no-edit` to "
            "complete the merge. Do NOT push or modify any other files."
        )

        env = agent_env()

        cmd = [
            "claude",
            "-p", prompt,
            "--verbose",
            "--output-format", "stream-json",
            "--max-turns", "30",
            "--permission-mode", "bypassPermissions",
        ]

        log_dir = ensure_logs_dir(project_root)
        log_file = log_dir / f"merge-{task_id}.jsonl"

        proc = subprocess.Popen(
            cmd,
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Drain stderr on a thread. Reading it only after the process exits would
        # deadlock: once the OS pipe buffer fills, the child blocks writing to
        # stderr while we are still blocked reading stdout.
        stderr_chunks: list[bytes] = []

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        assert proc.stdout is not None
        try:
            with open(log_file, "wb") as fh:
                for raw_line in proc.stdout:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        fh.write(raw_line)
                        fh.flush()
                        continue
                    msg["timestamp"] = datetime.now(UTC).isoformat()
                    fh.write(json.dumps(msg).encode())
                    fh.write(b"\n")
                    fh.flush()
        except KeyboardInterrupt:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise

        proc.wait()
        stderr_thread.join(timeout=5)

        if proc.returncode != 0:
            stderr_text = (
                b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
            )
            logger.warning(
                "Merge agent for %s exited with code %d: %s",
                task_id, proc.returncode, stderr_text or "(no stderr)",
            )
            return False

        # Verify the merge was committed (no conflicts remain)
        status = self._run_git(["diff", "--name-only", "--diff-filter=U"], project_root)
        if status.stdout.strip():
            # Still has unresolved conflicts
            return False

        # Check that HEAD advanced (merge commit was created)
        log_result = self._run_git(["log", "--oneline", "-1"], project_root)
        return "Merge" in log_result.stdout or proc.returncode == 0
