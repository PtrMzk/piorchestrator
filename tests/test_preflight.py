"""Tests for the `po run` pre-flight checks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from po.preflight import (
    check_claude_on_path,
    check_clean_worktree,
    check_git_identity,
    check_output_collisions,
    run_preflight,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


class TestClaudeOnPath:
    def test_missing(self) -> None:
        with patch("po.preflight.shutil.which", return_value=None):
            problem = check_claude_on_path()
        assert problem is not None
        assert "not on PATH" in problem

    def test_present(self) -> None:
        with patch("po.preflight.shutil.which", return_value="/usr/bin/claude"):
            assert check_claude_on_path() is None


class TestGitIdentity:
    def test_configured_in_repo(self, git_repo: Path) -> None:
        # The fixture sets user.name/user.email locally
        assert check_git_identity(git_repo) is None

    def test_missing_identity(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fresh directory on a machine with no git identity at all."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "EMAIL"):
            monkeypatch.delenv(var, raising=False)
        problem = check_git_identity(tmp_path)
        assert problem is not None
        assert "git config --global user.email" in problem

    def test_identity_from_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        monkeypatch.setenv("GIT_AUTHOR_NAME", "CI")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ci@example.com")
        assert check_git_identity(tmp_path) is None


class TestCleanWorktree:
    def test_clean(self, git_repo: Path) -> None:
        assert check_clean_worktree(git_repo) is None

    def test_modified_tracked_file(self, git_repo: Path) -> None:
        """The exact case that lost work: the merge's `checkout -f` discards this."""
        (git_repo / "README.md").write_text("# uncommitted\n")
        problem = check_clean_worktree(git_repo)
        assert problem is not None
        assert "README.md" in problem
        assert "checkout -f" in problem

    def test_staged_change(self, git_repo: Path) -> None:
        (git_repo / "README.md").write_text("# staged\n")
        _git(["add", "README.md"], git_repo)
        assert check_clean_worktree(git_repo) is not None

    def test_untracked_files_are_not_dirty(self, git_repo: Path) -> None:
        """spec.json and friends live untracked in the root; that's fine."""
        (git_repo / "spec.json").write_text("{}")
        assert check_clean_worktree(git_repo) is None

    def test_not_a_repo(self, tmp_path: Path) -> None:
        assert check_clean_worktree(tmp_path) is None

    def test_modified_gitignore_is_pos_own_business(self, git_repo: Path) -> None:
        """`po plan` appends to a tracked .gitignore; `po run` commits it next."""
        (git_repo / ".gitignore").write_text("build/\n")
        _git(["add", ".gitignore"], git_repo)
        _git(["commit", "-m", "ignore"], git_repo)
        (git_repo / ".gitignore").write_text("build/\n.po/\n")
        assert check_clean_worktree(git_repo) is None


class TestOutputCollisions:
    def test_untracked_output_file(self, git_repo: Path) -> None:
        """A scaffold stub at an output path makes git refuse the merge later."""
        (git_repo / "setup.txt").write_text("stub")
        problem = check_output_collisions(git_repo, ["setup.txt", "other.txt"])
        assert problem is not None
        assert "setup.txt" in problem
        assert "other.txt" not in problem

    def test_tracked_output_file_is_fine(self, git_repo: Path) -> None:
        """Existing-project tasks modify committed files; that merges cleanly."""
        assert check_output_collisions(git_repo, ["README.md"]) is None

    def test_ignored_files_are_fine(self, git_repo: Path) -> None:
        (git_repo / ".gitignore").write_text("build/\n")
        _git(["add", ".gitignore"], git_repo)
        _git(["commit", "-m", "ignore"], git_repo)
        (git_repo / "build").mkdir()
        (git_repo / "build" / "out.js").write_text("x")
        assert check_output_collisions(git_repo, ["build/out.js"]) is None

    def test_untracked_gitignore_written_by_po_is_fine(self, git_repo: Path) -> None:
        """The exact false positive: a spec whose init task lists .gitignore as an
        output, in a repo where `po plan` just wrote one. `_prepare_repo` commits
        it before any branch exists, so it never collides with anything."""
        (git_repo / ".gitignore").write_text(".po/\n")
        assert check_output_collisions(git_repo, [".gitignore", "package.json"]) is None

    def test_no_outputs(self, git_repo: Path) -> None:
        (git_repo / "anything.txt").write_text("x")
        assert check_output_collisions(git_repo, []) is None

    def test_not_a_repo(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        assert check_output_collisions(tmp_path, ["a.txt"]) is None


class TestRunPreflight:
    def test_all_clear(self, git_repo: Path) -> None:
        with patch("po.preflight.shutil.which", return_value="/usr/bin/claude"):
            assert run_preflight(git_repo, ["new.py"]) == []

    def test_collects_every_problem(self, git_repo: Path) -> None:
        (git_repo / "README.md").write_text("dirty")
        (git_repo / "new.py").write_text("stub")
        with patch("po.preflight.shutil.which", return_value=None):
            problems = run_preflight(git_repo, ["new.py"])
        assert len(problems) == 3
        assert "not on PATH" in problems[0]
        assert "uncommitted" in problems[1]
        assert "new.py" in problems[2]

    def test_allow_dirty_skips_only_the_dirty_check(self, git_repo: Path) -> None:
        (git_repo / "README.md").write_text("dirty")
        (git_repo / "new.py").write_text("stub")
        with patch("po.preflight.shutil.which", return_value="/usr/bin/claude"):
            problems = run_preflight(git_repo, ["new.py"], allow_dirty=True)
        assert len(problems) == 1
        assert "new.py" in problems[0]

    def test_fresh_directory_only_needs_claude_and_identity(self, tmp_path: Path) -> None:
        with patch("po.preflight.shutil.which", return_value="/usr/bin/claude"):
            assert run_preflight(tmp_path, ["a.txt"]) == []
