"""Tests for RebaseMerger — rebase, ff-merge, conflict resolution, verification."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from po.orchestrator.merge import MergeResult, RebaseMerger


def _make_mock_popen(returncode: int = 0, stdout: bytes = b"") -> MagicMock:
    """Create a mock Popen that behaves like the real one for streaming."""
    proc = MagicMock()
    proc.stdout = io.BytesIO(stdout)
    proc.stderr = io.BytesIO(b"")
    proc.wait.return_value = returncode
    proc.returncode = returncode
    proc.__enter__ = lambda s: s
    proc.__exit__ = lambda s, *a: None
    return proc


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=check,
    )


def _commit_file(repo: Path, filename: str, content: str, msg: str) -> None:
    """Create/overwrite a file and commit it."""
    (repo / filename).write_text(content)
    _git(["add", filename], repo)
    _git(["commit", "-m", msg], repo)


# ──────────────── Helpers to set up branches ────────────────


def _make_clean_branch(repo: Path, branch: str, filename: str, content: str) -> str:
    """Create a branch off main with a single non-conflicting commit.

    Returns the branch name.
    """
    _git(["checkout", "-b", branch], repo)
    _commit_file(repo, filename, content, f"Add {filename}")
    _git(["checkout", "main"], repo)
    return branch


def _make_conflicting_branch(repo: Path, branch: str, filename: str, content: str) -> str:
    """Create a branch that modifies the same file as main, causing a conflict."""
    # Branch off current main
    _git(["checkout", "-b", branch], repo)
    _commit_file(repo, filename, content, f"Branch change to {filename}")
    _git(["checkout", "main"], repo)
    return branch


# ──────────────── Tests ────────────────


class TestMergeResult:
    def test_defaults(self) -> None:
        r = MergeResult(success=True)
        assert r.success is True
        assert r.error_message is None
        assert r.needed_agent_resolution is False

    def test_failure_with_message(self) -> None:
        r = MergeResult(success=False, error_message="oops")
        assert r.success is False
        assert r.error_message == "oops"


class TestRebaseMergerCleanMerge:
    """Cases where rebase + ff-merge succeeds without conflicts."""

    def test_simple_fast_forward(self, git_repo: Path) -> None:
        """A branch with one commit rebases and merges cleanly."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/task-1", "new_file.py", "print('hi')")

        result = merger._merge_sync(branch, "task-1", "", git_repo)
        assert result.success is True
        assert result.error_message is None

        # main should have the new file
        assert (git_repo / "new_file.py").exists()
        # Should be on main
        head_ref = _git(["rev-parse", "--abbrev-ref", "HEAD"], git_repo).stdout.strip()
        assert head_ref == "main"

    def test_merge_with_verification_passes(self, git_repo: Path) -> None:
        """Verification command succeeds after merge."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/task-v", "v.py", "x = 1")

        result = merger._merge_sync(branch, "task-v", "python -c 'print(1)'", git_repo)
        assert result.success is True

    def test_merge_with_verification_fails_reverts(self, git_repo: Path) -> None:
        """Failed verification reverts the merge commit."""
        merger = RebaseMerger()
        # Pre-seed .gitignore so _ensure_gitignore is a no-op during merge
        merger._ensure_gitignore(git_repo)
        branch = _make_clean_branch(git_repo, "po/task-vf", "vf.py", "x = 1")

        # Count commits before
        log_before = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()

        result = merger._merge_sync(
            branch, "task-vf", "python -c 'raise SystemExit(1)'", git_repo,
        )
        assert result.success is False
        assert "verification failed" in result.error_message.lower()

        # Commit count should be same as before (reverted)
        log_after = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()
        assert log_before == log_after

    def test_multiple_sequential_merges(self, git_repo: Path) -> None:
        """Multiple branches merge sequentially without issues."""
        merger = RebaseMerger()
        _make_clean_branch(git_repo, "po/seq-1", "seq1.py", "a = 1")
        _make_clean_branch(git_repo, "po/seq-2", "seq2.py", "b = 2")

        r1 = merger._merge_sync("po/seq-1", "seq-1", "", git_repo)
        r2 = merger._merge_sync("po/seq-2", "seq-2", "", git_repo)

        assert r1.success is True
        assert r2.success is True
        assert (git_repo / "seq1.py").exists()
        assert (git_repo / "seq2.py").exists()


class TestRebaseMergerConflict:
    """Cases where rebase fails and agent merge is attempted."""

    def test_conflict_triggers_agent_merge(self, git_repo: Path) -> None:
        """When rebase fails, _try_agent_merge is called."""
        merger = RebaseMerger()

        # Create conflict: both main and branch modify README.md
        _make_conflicting_branch(git_repo, "po/conflict-1", "README.md", "branch content\n")
        _commit_file(git_repo, "README.md", "main content\n", "Main changes README")

        with patch.object(merger, "_try_agent_merge") as mock_agent:
            mock_agent.return_value = MergeResult(
                success=True, needed_agent_resolution=True,
            )
            result = merger._merge_sync("po/conflict-1", "conflict-1", "", git_repo)
            assert result.success is True
            assert result.needed_agent_resolution is True
            mock_agent.assert_called_once()

    def test_conflict_agent_merge_fails(self, git_repo: Path) -> None:
        """When agent merge also fails, we get a failure result."""
        merger = RebaseMerger()

        _make_conflicting_branch(git_repo, "po/conflict-2", "README.md", "branch v2\n")
        _commit_file(git_repo, "README.md", "main v2\n", "Main changes README again")

        with patch.object(merger, "_try_agent_merge") as mock_agent:
            mock_agent.return_value = MergeResult(
                success=False,
                error_message="Agent could not resolve",
                needed_agent_resolution=True,
            )
            result = merger._merge_sync("po/conflict-2", "conflict-2", "", git_repo)
            assert result.success is False

    def test_rebase_abort_on_conflict(self, git_repo: Path) -> None:
        """After rebase conflict, rebase --abort is called before agent merge."""
        merger = RebaseMerger()

        _make_conflicting_branch(git_repo, "po/conflict-3", "README.md", "branch v3\n")
        _commit_file(git_repo, "README.md", "main v3\n", "Main changes README v3")

        calls: list[list[str]] = []
        original_run_git = merger._run_git

        def tracking_run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            return original_run_git(args, cwd)

        with (
            patch.object(merger, "_run_git", side_effect=tracking_run_git),
            patch.object(merger, "_try_agent_merge") as mock_agent,
        ):
            mock_agent.return_value = MergeResult(success=True, needed_agent_resolution=True)
            merger._merge_sync("po/conflict-3", "conflict-3", "", git_repo)

        # Should see rebase attempt, then rebase --abort
        rebase_calls = [c for c in calls if c[0] == "rebase"]
        assert len(rebase_calls) == 2
        assert rebase_calls[1] == ["rebase", "--abort"]


class TestTryAgentMerge:
    """Tests for _try_agent_merge specifically."""

    def test_no_conflict_merge_commits_directly(self, git_repo: Path) -> None:
        """If --no-ff --no-commit succeeds, just commit without agent."""
        merger = RebaseMerger()

        # Create a branch with non-conflicting changes
        _make_clean_branch(git_repo, "po/no-conflict", "extra.py", "z = 0")

        result = merger._try_agent_merge("po/no-conflict", "no-conflict", git_repo, "")
        assert result.success is True
        # needed_agent_resolution is True because _try_agent_merge always sets it
        assert result.needed_agent_resolution is True

    def test_agent_invoked_on_real_conflict(self, git_repo: Path) -> None:
        """When there are actual conflicts, _invoke_merge_agent is called."""
        merger = RebaseMerger()

        _make_conflicting_branch(git_repo, "po/real-conflict", "README.md", "branch\n")
        _commit_file(git_repo, "README.md", "main\n", "Main edit")

        with patch.object(merger, "_invoke_merge_agent") as mock_invoke:
            mock_invoke.return_value = False
            result = merger._try_agent_merge(
                "po/real-conflict", "real-conflict", git_repo, "",
            )
            assert result.success is False
            assert "could not resolve" in result.error_message.lower()
            mock_invoke.assert_called_once()

    def test_agent_success_with_verification(self, git_repo: Path) -> None:
        """Agent resolves conflicts, then verification passes."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/agent-ok", "agent.py", "ok = True")

        result = merger._try_agent_merge(
            branch, "agent-ok", git_repo, "python -c 'print(1)'",
        )
        assert result.success is True

    def test_agent_success_verification_fails_reverts(self, git_repo: Path) -> None:
        """Agent resolves conflicts but verification fails — revert."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/agent-vfail", "avf.py", "val = 1")

        log_before = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()

        result = merger._try_agent_merge(
            branch, "agent-vfail", git_repo, "python -c 'raise SystemExit(1)'",
        )
        assert result.success is False
        assert "verification failed" in result.error_message.lower()

        log_after = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()
        assert log_before == log_after

    def test_merge_abort_when_agent_fails(self, git_repo: Path) -> None:
        """When agent fails, merge --abort is called to clean up."""
        merger = RebaseMerger()

        _make_conflicting_branch(git_repo, "po/abort-test", "README.md", "abort branch\n")
        _commit_file(git_repo, "README.md", "abort main\n", "Main edit for abort")

        calls: list[list[str]] = []
        original_run_git = merger._run_git

        def tracking_run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
            calls.append(list(args))
            return original_run_git(args, cwd)

        with (
            patch.object(merger, "_run_git", side_effect=tracking_run_git),
            patch.object(merger, "_invoke_merge_agent", return_value=False),
        ):
            merger._try_agent_merge(
                "po/abort-test", "abort-test", git_repo, "",
            )

        abort_calls = [c for c in calls if c == ["merge", "--abort"]]
        assert len(abort_calls) == 1


class TestInvokeMergeAgent:
    """Tests for _invoke_merge_agent (mocking the Claude CLI call)."""

    def test_no_conflicts_commits_directly(self, git_repo: Path) -> None:
        """If no conflicted files, commit directly and return True."""
        merger = RebaseMerger()

        # Set up a merge state with no conflicts
        _make_clean_branch(git_repo, "po/no-conf-agent", "nc.py", "nc = 1")
        _git(["merge", "--no-ff", "--no-commit", "po/no-conf-agent"], git_repo)

        result = merger._invoke_merge_agent("no-conf-agent", "po/no-conf-agent", git_repo)
        assert result is True

    def test_claude_cli_failure_returns_false(self, git_repo: Path) -> None:
        """If Claude CLI returns non-zero, return False."""
        merger = RebaseMerger()

        # Create a real merge conflict
        _make_conflicting_branch(git_repo, "po/cli-fail", "README.md", "cli-fail branch\n")
        _commit_file(git_repo, "README.md", "cli-fail main\n", "Main for cli-fail")
        _git(["merge", "--no-ff", "--no-commit", "po/cli-fail"], git_repo, check=False)

        mock_proc = _make_mock_popen(returncode=1)
        real_popen = subprocess.Popen

        def selective_popen(cmd, **kwargs):
            if cmd[0] == "claude":
                return mock_proc
            return real_popen(cmd, **kwargs)

        with patch("po.orchestrator.merge.subprocess.Popen", side_effect=selective_popen):
            result = merger._invoke_merge_agent("cli-fail", "po/cli-fail", git_repo)
            assert result is False

    def test_prompt_contains_conflict_info(self, git_repo: Path) -> None:
        """The prompt sent to Claude should contain the conflicted file names."""
        merger = RebaseMerger()

        _make_conflicting_branch(git_repo, "po/prompt-check", "README.md", "prompt branch\n")
        _commit_file(git_repo, "README.md", "prompt main\n", "Main for prompt")
        _git(["merge", "--no-ff", "--no-commit", "po/prompt-check"], git_repo, check=False)

        mock_proc = _make_mock_popen(returncode=1)
        real_popen = subprocess.Popen
        captured_cmd: list[str] = []

        def selective_popen(cmd, **kwargs):
            if cmd[0] == "claude":
                captured_cmd.extend(cmd)
                return mock_proc
            return real_popen(cmd, **kwargs)

        with patch("po.orchestrator.merge.subprocess.Popen", side_effect=selective_popen):
            merger._invoke_merge_agent("prompt-check", "po/prompt-check", git_repo)
        # The -p flag should be followed by the prompt containing README.md
        assert "-p" in captured_cmd
        prompt_idx = captured_cmd.index("-p") + 1
        assert "README.md" in captured_cmd[prompt_idx]


class TestEnsureGitignore:
    """Tests for .gitignore creation to prevent untracked files blocking merges."""

    def test_untracked_files_dont_block_merge(self, git_repo: Path) -> None:
        """Untracked build artifacts (dist/, node_modules/) don't block merge."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/task-gi", "new_file.py", "x = 1")

        # Simulate build artifacts that verification commands would create
        (git_repo / "dist").mkdir()
        (git_repo / "dist" / "index.js").write_text("compiled output")
        (git_repo / "node_modules").mkdir()
        (git_repo / "node_modules" / ".vite").write_text("cache")

        result = merger._merge_sync(branch, "task-gi", "", git_repo)
        assert result.success is True

        # .gitignore should have been created with the expected patterns
        gitignore = git_repo / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "node_modules/" in content
        assert "dist/" in content
        assert "build/" in content

    def test_ensure_gitignore_idempotent(self, git_repo: Path) -> None:
        """Calling _ensure_gitignore twice doesn't duplicate patterns."""
        merger = RebaseMerger()

        merger._ensure_gitignore(git_repo)
        first = (git_repo / ".gitignore").read_text()

        merger._ensure_gitignore(git_repo)
        second = (git_repo / ".gitignore").read_text()

        assert first == second


class TestRebaseMergerAsync:
    """Test the async merge() entry point."""

    @pytest.mark.asyncio
    async def test_async_merge_delegates_to_sync(self, git_repo: Path) -> None:
        """async merge() calls _merge_sync under the lock."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/async-1", "async.py", "a = 1")

        result = await merger.merge(branch, "async-1", "", git_repo)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_lock_serializes_merges(self, git_repo: Path) -> None:
        """Two concurrent merges are serialized by the lock."""
        import asyncio

        merger = RebaseMerger()
        _make_clean_branch(git_repo, "po/lock-1", "lock1.py", "l1 = 1")
        _make_clean_branch(git_repo, "po/lock-2", "lock2.py", "l2 = 2")

        order: list[str] = []
        original_merge = merger._merge_sync

        def tracking_merge(branch, task_id, verification, project_root):
            order.append(f"start-{task_id}")
            result = original_merge(branch, task_id, verification, project_root)
            order.append(f"end-{task_id}")
            return result

        with patch.object(merger, "_merge_sync", side_effect=tracking_merge):
            await asyncio.gather(
                merger.merge("po/lock-1", "lock-1", "", git_repo),
                merger.merge("po/lock-2", "lock-2", "", git_repo),
            )

        # Merges should be fully serialized (no interleaving)
        assert order[0].startswith("start-")
        assert order[1].startswith("end-")
        assert order[2].startswith("start-")
        assert order[3].startswith("end-")
