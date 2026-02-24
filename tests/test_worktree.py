"""Tests for git worktree management."""

from __future__ import annotations

import subprocess
from pathlib import Path

from po.worktree.manager import GitWorktreeManager


class TestGitWorktreeManager:
    def test_create_worktree(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        info = mgr.create("test-task", git_repo)
        assert info.task_id == "test-task"
        assert info.branch == "po/test-task"
        assert info.path.exists()
        # Verify it's a valid git worktree
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=info.path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_create_branches_from_main_tip(self, git_repo: Path) -> None:
        # Get current HEAD
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        mgr = GitWorktreeManager()
        info = mgr.create("test-task", git_repo)

        # Worktree HEAD should match main HEAD
        wt_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=info.path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert wt_head == head

    def test_remove_worktree(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        info = mgr.create("test-task", git_repo)
        assert info.path.exists()

        mgr.remove("test-task", git_repo)
        assert not info.path.exists()

        # Branch should also be deleted
        result = subprocess.run(
            ["git", "branch", "--list", "po/test-task"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == ""

    def test_list_worktrees(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        mgr.create("task-1", git_repo)
        mgr.create("task-2", git_repo)

        worktrees = mgr.list(git_repo)
        ids = [w.task_id for w in worktrees]
        assert "task-1" in ids
        assert "task-2" in ids

    def test_exists(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        assert not mgr.exists("task-x", git_repo)
        mgr.create("task-x", git_repo)
        assert mgr.exists("task-x", git_repo)

    def test_remove_nonexistent(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        # Should not raise
        mgr.remove("nonexistent", git_repo)

    def test_multiple_worktrees_independent(self, git_repo: Path) -> None:
        mgr = GitWorktreeManager()
        info1 = mgr.create("task-1", git_repo)
        info2 = mgr.create("task-2", git_repo)

        # Write different files in each
        (info1.path / "file1.txt").write_text("from task 1")
        (info2.path / "file2.txt").write_text("from task 2")

        assert not (info1.path / "file2.txt").exists()
        assert not (info2.path / "file1.txt").exists()
