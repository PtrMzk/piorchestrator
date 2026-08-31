"""Integration test: mock Claude binary + full init → run flow.

Unlike the E2E tests that replace the AgentRunner protocol, these tests
exercise the **real** subprocess-spawning code paths in ``_invoke_claude()``
(sync, for ``po init``) and ``ClaudeCodeRunner.run()`` (async, for ``po run``)
by injecting a mock ``claude`` binary onto ``$PATH``.
"""

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from po.config import state_db_path, worktrees_dir
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore

# ──────────────────────────── Fixtures ────────────────────────────


@pytest.fixture
def integration_env(git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Set up an environment with a mock ``claude`` binary on PATH.

    Yields ``(project_root, mock_bin_dir)`` where *project_root* is a real
    git repo (via the shared ``git_repo`` fixture) and *mock_bin_dir* contains
    a ``claude`` shell wrapper that delegates to ``tests/mock_claude.py``.
    """
    # Ensure the default branch is named 'main' (required by RebaseMerger)
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    )

    # Build mock_bin/ directory with a `claude` wrapper script
    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir()

    mock_claude_py = Path(__file__).parent / "mock_claude.py"
    claude_wrapper = mock_bin / "claude"
    claude_wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{mock_claude_py}" "$@"\n'
    )
    claude_wrapper.chmod(claude_wrapper.stat().st_mode | stat.S_IEXEC)

    # Prepend mock_bin to PATH so both _invoke_claude() (sync Popen)
    # and ClaudeCodeRunner.run() (async create_subprocess_exec) find it.
    import os

    monkeypatch.setenv("PATH", f"{mock_bin}{os.pathsep}{os.environ['PATH']}")

    yield git_repo


# ──────────────────────────── Tests ────────────────────────────


def test_full_init_to_run_flow(integration_env: Path) -> None:
    """Exercise po init → po run through real subprocess invocations."""
    from po.cli import cmd_init, cmd_run

    project_root = integration_env

    # ── 1. po init (non-interactive, stdin is not a TTY in tests) ──
    init_args = argparse.Namespace(
        description="A mock project for integration testing",
        output=project_root / "spec.json",
        model="haiku",
        project_root=project_root,
    )
    cmd_init(init_args)

    # Assert: spec.json exists and is valid JSON with 3 tasks
    spec_file = project_root / "spec.json"
    assert spec_file.exists(), "spec.json should have been created by cmd_init"
    spec_data = json.loads(spec_file.read_text())
    assert spec_data["project_name"] == "mock-project"
    assert len(spec_data["tasks"]) == 3

    # Assert: DB was created (auto-plan) with 3 pending tasks
    db_path = state_db_path(project_root)
    assert db_path.exists(), "state.db should have been created by auto-plan"
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)

    all_tasks = store.get_all_tasks()
    assert len(all_tasks) == 3
    assert all(t["status"] == "pending" for t in all_tasks)

    conn.close()

    # Commit scaffolds, docs, and spec to main so they don't interfere
    # with git worktree creation and merge (untracked files block merges).
    subprocess.run(
        ["git", "add", "-A"], cwd=project_root, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Add scaffolds and spec"],
        cwd=project_root, capture_output=True, check=True,
    )

    # ── 2. po run (concurrency=1) ──
    run_args = argparse.Namespace(
        spec_file=None,  # already planned
        project_root=project_root,
        concurrency=1,
        max_retries=1,
        model=None,
        max_turns=10,
    )
    cmd_run(run_args)

    # ── 3. Assertions ──
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    all_tasks = store.get_all_tasks()

    # All 3 tasks should be completed
    for task in all_tasks:
        assert task["status"] == "completed", (
            f"Task {task['id']} should be completed, got {task['status']}"
        )

    # Output files should be tracked on main branch
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip().splitlines()
    for expected_file in ("setup.txt", "feature_a.txt", "feature_b.txt"):
        assert expected_file in tracked, (
            f"{expected_file} should be tracked on main"
        )

    # Worktrees directory should be cleaned up
    wt_dir = worktrees_dir(project_root)
    if wt_dir.exists():
        remaining = list(wt_dir.iterdir())
        assert remaining == [], (
            f"Worktrees should be cleaned up, found: {remaining}"
        )

    # Costs should be recorded (> 0)
    for task in all_tasks:
        assert task["cost_usd"] is not None and task["cost_usd"] > 0, (
            f"Task {task['id']} should have cost_usd > 0"
        )

    conn.close()

