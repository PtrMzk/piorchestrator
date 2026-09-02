"""Tests for RebaseMerger — rebase, ff-merge, conflict resolution, verification."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from po import procs
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
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
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
        branch = _make_clean_branch(git_repo, "po/task-vf", "vf.py", "x = 1")

        # Count commits before
        log_before = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()

        result = merger._merge_sync(
            branch,
            "task-vf",
            "python -c 'raise SystemExit(1)'",
            git_repo,
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
                success=True,
                needed_agent_resolution=True,
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

        # Up-front recovery abort, the rebase attempt, then the abort under test
        rebase_calls = [c for c in calls if c[0] == "rebase"]
        assert rebase_calls == [
            ["rebase", "--abort"],
            ["rebase", "main", "po/conflict-3"],
            ["rebase", "--abort"],
        ]


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
                "po/real-conflict",
                "real-conflict",
                git_repo,
                "",
            )
            assert result.success is False
            assert "could not resolve" in result.error_message.lower()
            mock_invoke.assert_called_once()

    def test_agent_success_with_verification(self, git_repo: Path) -> None:
        """Agent resolves conflicts, then verification passes."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/agent-ok", "agent.py", "ok = True")

        result = merger._try_agent_merge(
            branch,
            "agent-ok",
            git_repo,
            "python -c 'print(1)'",
        )
        assert result.success is True

    def test_agent_success_verification_fails_reverts(self, git_repo: Path) -> None:
        """Agent resolves conflicts but verification fails — revert."""
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/agent-vfail", "avf.py", "val = 1")

        log_before = _git(["rev-list", "--count", "HEAD"], git_repo).stdout.strip()

        result = merger._try_agent_merge(
            branch,
            "agent-vfail",
            git_repo,
            "python -c 'raise SystemExit(1)'",
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
                "po/abort-test",
                "abort-test",
                git_repo,
                "",
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

    def test_no_merge_in_progress_returns_false(self, git_repo: Path) -> None:
        """No conflicted files *and* no MERGE_HEAD means nothing was merged.

        This used to commit an empty commit and return True, which turned a
        refused merge into a "completed" task whose branch was then deleted.
        """
        merger = RebaseMerger()
        _make_clean_branch(git_repo, "po/refused", "r.py", "r = 1")
        before = _git(["rev-parse", "HEAD"], git_repo).stdout.strip()

        result = merger._invoke_merge_agent("refused", "po/refused", git_repo)

        assert result is False
        assert _git(["rev-parse", "HEAD"], git_repo).stdout.strip() == before

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


class TestBaseBranchDetection:
    """Base branch detection must reject HEAD readings that aren't branches."""

    @staticmethod
    def _leave_mid_rebase(repo: Path, branch: str) -> None:
        """Leave the repo with a conflicted rebase in progress, HEAD detached."""
        _make_conflicting_branch(repo, branch, "README.md", "branch side\n")
        _commit_file(repo, "README.md", "main side\n", "Main side")
        _git(["rebase", "main", branch], repo, check=False)
        assert (repo / ".git" / "rebase-merge").exists()

    def test_explicit_base_branch_wins(self, git_repo: Path) -> None:
        """An explicitly configured base branch skips detection entirely."""
        merger = RebaseMerger(base_branch="develop")
        assert merger._get_base_branch(git_repo) == "develop"

    def test_detached_head_does_not_become_base(self, git_repo: Path) -> None:
        """Mid-rebase, `rev-parse --abbrev-ref HEAD` prints "HEAD" — not a branch.

        Caching that merges every later task into a detached head, so the real
        branch never advances and the whole run is silently thrown away.
        """
        self._leave_mid_rebase(git_repo, "po/detached")

        assert RebaseMerger()._get_base_branch(git_repo) == "main"

    def test_task_branch_does_not_become_base(self, git_repo: Path) -> None:
        """A `po/` branch left checked out would make tasks merge into each other."""
        _git(["checkout", "-b", "po/left-over"], git_repo)

        assert RebaseMerger()._get_base_branch(git_repo) == "main"

    def test_falls_back_to_master(self, git_repo: Path) -> None:
        """Repos without `main` fall back to `master` before giving up."""
        _git(["branch", "-m", "main", "master"], git_repo)
        _git(["checkout", "--detach"], git_repo)

        assert RebaseMerger()._get_base_branch(git_repo) == "master"

    def test_stale_rebase_state_is_recovered(self, git_repo: Path) -> None:
        """A crashed run leaves the repo mid-rebase; the next merge must recover.

        Removing `.git/rebase-merge` is not enough — only `rebase --abort` puts
        HEAD back on a branch.
        """
        # The follow-up task's branch has to exist before the repo is wedged —
        # git refuses almost everything while a rebase is in progress.
        _make_clean_branch(git_repo, "po/next-task", "next.py", "x = 1")
        self._leave_mid_rebase(git_repo, "po/crashed")

        merger = RebaseMerger()
        result = merger._merge_sync("po/next-task", "next-task", "", git_repo)

        assert result.success is True
        assert (git_repo / "next.py").exists()
        # main itself advanced — not some detached head
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], git_repo).stdout.strip()
        assert head == "main"
        assert not (git_repo / ".git" / "rebase-merge").exists()


class TestMergeAgentSubprocess:
    """Exercise the real subprocess handling in `_invoke_merge_agent`.

    These tests put a stub `claude` on PATH rather than patching Popen, because
    the behaviour under test *is* the pipe/env handling.
    """

    @staticmethod
    def _stub_claude(bin_dir: Path, body: str) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "claude"
        # Absolute interpreter path so the stub does not depend on PATH order.
        script.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
        script.chmod(0o755)

    @staticmethod
    def _conflicted_repo(repo: Path, branch: str) -> None:
        """Leave the repo mid-merge with a conflict in README.md."""
        _make_conflicting_branch(repo, branch, "README.md", "branch side\n")
        _commit_file(repo, "README.md", "main side\n", "Main side")
        _git(["merge", "--no-ff", "--no-commit", branch], repo, check=False)

    def test_large_stderr_does_not_deadlock(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: >64KB on stderr used to fill the pipe and hang forever.

        The child blocks writing to stderr while the parent is blocked reading
        stdout, so neither side ever advances — the run stalls with no output.
        """
        self._conflicted_repo(git_repo, "po/big-stderr")
        self._stub_claude(
            tmp_path / "bin",
            # 1 MB of stderr — far past the ~64KB pipe buffer — written before
            # anything reaches stdout, so the deadlock would trigger first.
            "sys.stderr.write('x' * 1_000_000)\n"
            "sys.stderr.flush()\n"
            'print(\'{"type": "result", "result": "done"}\')\n',
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        merger = RebaseMerger()
        # Conflicts are left unresolved, so this reports failure — the point is
        # that it returns at all.
        assert merger._invoke_merge_agent("big-stderr", "po/big-stderr", git_repo) is False

    def test_stdin_is_closed(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stdin must be /dev/null so a prompting child cannot hang the merge."""
        self._conflicted_repo(git_repo, "po/stdin-check")
        self._stub_claude(
            tmp_path / "bin",
            "assert sys.stdin.read() == '', 'stdin was not empty'\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        merger = RebaseMerger()
        assert merger._invoke_merge_agent("stdin-check", "po/stdin-check", git_repo) is False

    def test_auth_env_survives(
        self, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Auth vars must reach the merge agent; only nesting markers are dropped."""
        self._conflicted_repo(git_repo, "po/env-check")
        self._stub_claude(
            tmp_path / "bin",
            "import json\n"
            "print(json.dumps({'type': 'result', 'result': '', 'env': {\n"
            "    'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''),\n"
            "    'nested': os.environ.get('CLAUDECODE', ''),\n"
            "}}))\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
        monkeypatch.setenv("CLAUDECODE", "1")

        merger = RebaseMerger()
        merger._invoke_merge_agent("env-check", "po/env-check", git_repo)

        log_file = git_repo / ".po" / "logs" / "merge-env-check.jsonl"
        seen = json.loads(log_file.read_text().splitlines()[0])["env"]
        assert seen["token"] == "tok-123"  # auth reaches the agent
        assert seen["nested"] == ""  # nesting marker stripped


class TestUntrackedArtifacts:
    """Build artifacts left in the working tree must not block a merge.

    Seeding `.gitignore` is the orchestrator's job now, done once before any
    branch is cut (`OrchestratorLoop._prepare_repo`) — the merge used to commit
    it mid-run, which made it conflict with every task that wrote one.
    """

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
        assert (git_repo / "new_file.py").exists()

    def test_merge_adds_no_commit_of_its_own(self, git_repo: Path) -> None:
        """The merge must not commit to the base branch behind the task's back.

        Any such commit lands *after* every in-flight branch was cut, so a task
        touching the same file gets an add/add conflict it cannot avoid.
        """
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/no-extra", "extra.py", "x = 1")
        before = _git(["rev-list", "--count", "main"], git_repo).stdout.strip()

        assert merger._merge_sync(branch, "no-extra", "", git_repo).success is True

        after = _git(["rev-list", "--count", "main"], git_repo).stdout.strip()
        assert int(after) == int(before) + 1  # the task's commit, and nothing else


class TestMergeIsHonest:
    """A merge may only report success if the base branch contains the tip."""

    def test_untracked_collision_fails_and_keeps_branch(self, git_repo: Path) -> None:
        """An untracked file the branch would overwrite refuses the checkout.

        The exact shape of the bug: scaffold stubs left in the project root
        matched the agent's output files, git refused the rebase and the merge,
        and the fallback reported success with nothing merged.
        """
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/collide", "setup.txt", "real work\n")
        (git_repo / "setup.txt").write_text("stub")  # untracked, same path

        result = merger._merge_sync(branch, "collide", "", git_repo)

        assert result.success is False
        assert "refused" in (result.error_message or "")
        # Base did not move, branch still exists, working tree untouched
        assert "setup.txt" not in _git(["ls-files"], git_repo).stdout
        assert _git(["rev-parse", "--verify", branch], git_repo).returncode == 0
        assert (git_repo / "setup.txt").read_text() == "stub"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], git_repo).stdout.strip() == "main"

    def test_confirm_merged_rejects_unmerged_branch(self, git_repo: Path) -> None:
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/unmerged", "u.py", "u = 1")
        assert merger._confirm_merged(branch, "main", git_repo).success is False

    def test_confirm_merged_accepts_merged_branch(self, git_repo: Path) -> None:
        merger = RebaseMerger()
        branch = _make_clean_branch(git_repo, "po/merged", "m.py", "m = 1")
        _git(["merge", "--ff-only", branch], git_repo)
        result = merger._confirm_merged(branch, "main", git_repo, needed_agent_resolution=True)
        assert result.success is True
        assert result.needed_agent_resolution is True


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


class TestMergeInterruption:
    """Ctrl-C during a merge must stop it and leave the repo usable."""

    def test_cancelled_merge_unwinds_the_repo(self, git_repo: Path) -> None:
        """The interrupted merge aborts its own rebase instead of orphaning it.

        Being killed mid-rebase is exactly how a repo ends up stuck at
        `REBASE 2/2` on a detached HEAD.
        """
        _make_conflicting_branch(git_repo, "po/interrupted", "README.md", "branch\n")
        _commit_file(git_repo, "README.md", "main\n", "Main side")
        merger = RebaseMerger()

        procs.shutdown()
        result = merger._merge_sync("po/interrupted", "interrupted", "", git_repo)

        assert result.success is False
        assert "cancelled" in result.error_message.lower()
        assert not (git_repo / ".git" / "rebase-merge").exists()
        head = _git(["rev-parse", "--abbrev-ref", "HEAD"], git_repo).stdout.strip()
        assert head == "main"

    def test_cancelled_merge_does_not_invoke_the_merge_agent(self, git_repo: Path) -> None:
        """Shutdown must not spawn a fresh Claude agent on the way out."""
        _make_conflicting_branch(git_repo, "po/no-agent", "README.md", "branch\n")
        _commit_file(git_repo, "README.md", "main\n", "Main side")
        merger = RebaseMerger()

        procs.shutdown()
        with patch.object(merger, "_invoke_merge_agent") as mock_agent:
            merger._merge_sync("po/no-agent", "no-agent", "", git_repo)

        mock_agent.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_interrupts_a_merge_in_flight(self, git_repo: Path) -> None:
        """The real complaint: a merge already running must not ignore Ctrl-C.

        The merge blocks an executor thread, which asyncio cancellation cannot
        reach — killing the tracked subprocess is what actually stops it.
        """
        _make_clean_branch(git_repo, "po/slow", "slow.py", "x = 1")
        merger = RebaseMerger()

        async def interrupt_once_running() -> None:
            # Let the merge reach its verification command, then "press Ctrl-C".
            await asyncio.sleep(1.0)
            procs.shutdown()

        start = time.monotonic()
        merge_task = asyncio.create_task(
            merger.merge(
                branch="po/slow",
                task_id="slow",
                verification=f"{sys.executable} -c 'import time; time.sleep(120)'",
                project_root=git_repo,
            )
        )
        await interrupt_once_running()
        result = await asyncio.wait_for(merge_task, timeout=30)
        elapsed = time.monotonic() - start

        assert result.success is False
        assert elapsed < 30, f"merge ran {elapsed:.0f}s after shutdown"
