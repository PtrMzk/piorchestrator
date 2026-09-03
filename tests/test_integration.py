"""Integration tests: mock Claude binary + the real CLI commands.

Unlike the E2E tests that replace the AgentRunner protocol, these tests
exercise the **real** subprocess-spawning code paths in ``_invoke_claude()``
(sync, for ``po init``) and ``ClaudeCodeRunner.run()`` (async, for ``po run``)
by injecting a mock ``claude`` binary onto ``$PATH`` — and they drive the
commands exactly the way a user does, with nothing done by hand in between.
That last part is the point: the previous version of the init → run test
committed the scaffolds itself between the two commands, and so never saw
that a user who did not would have every task "complete" with nothing merged.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from po.cli import cmd_init, cmd_plan, cmd_run
from po.config import state_db_path, worktrees_dir
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore

# ──────────────────────────── Fixtures ────────────────────────────


@pytest.fixture
def integration_env(git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A real git repo (via ``git_repo``) with a mock ``claude`` on PATH."""
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    )

    mock_bin = tmp_path / "mock_bin"
    mock_bin.mkdir()
    mock_claude_py = Path(__file__).parent / "mock_claude.py"
    claude_wrapper = mock_bin / "claude"
    claude_wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{mock_claude_py}" "$@"\n')
    claude_wrapper.chmod(claude_wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{mock_bin}{os.pathsep}{os.environ['PATH']}")

    yield git_repo


@pytest.fixture
def fresh_dir(tmp_path: Path, integration_env: Path):
    """A directory that is not a git repository — the "new project" case.

    Reuses ``integration_env`` for the mock binary; the repo it made is unused.
    """
    d = tmp_path / "fresh"
    d.mkdir()
    return d


# ──────────────────────────── Helpers ────────────────────────────


def _init_args(project_root: Path, description: str = "A mock project", **kw) -> Any:
    return argparse.Namespace(
        description=description,
        output=project_root / "spec.json",
        model="haiku",
        project_root=project_root,
        **kw,
    )


def _run_args(project_root: Path, **kw) -> Any:
    base = dict(
        spec_file=None,
        project_root=project_root,
        concurrency=1,
        max_retries=1,
        model=None,
        max_turns=10,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _plan_args(project_root: Path, spec_file: Path, **kw) -> Any:
    base = dict(
        spec_file=spec_file,
        project_root=project_root,
        playground=False,
        scaffold=False,
        generate_docs=False,
        fresh=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _tasks(project_root: Path) -> dict[str, dict]:
    conn = init_db(state_db_path(project_root))
    try:
        return {str(t["id"]): t for t in SqliteTaskStore(conn).get_all_tasks()}
    finally:
        conn.close()


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _tracked(project_root: Path) -> list[str]:
    return _git(["ls-files"], project_root).strip().splitlines()


def _assert_no_worktrees(project_root: Path) -> None:
    wt_dir = worktrees_dir(project_root)
    remaining = list(wt_dir.iterdir()) if wt_dir.exists() else []
    assert remaining == [], f"Worktrees should be cleaned up, found: {remaining}"


# ──────────────────────────── Tests ────────────────────────────


class TestNewProject:
    """Workflow 1: a new project, with and without `git init` already run."""

    def test_init_then_run_in_existing_repo(self, integration_env: Path) -> None:
        """`po init` → `po run`, nothing done by hand in between."""
        project_root = integration_env

        cmd_init(_init_args(project_root))

        spec_data = json.loads((project_root / "spec.json").read_text())
        assert spec_data["project_name"] == "mock-project"
        assert len(spec_data["tasks"]) == 3
        assert all(t["status"] == "pending" for t in _tasks(project_root).values())

        cmd_run(_run_args(project_root))

        tasks = _tasks(project_root)
        assert {t["status"] for t in tasks.values()} == {"completed"}
        tracked = _tracked(project_root)
        for expected in ("setup.txt", "feature_a.txt", "feature_b.txt"):
            assert expected in tracked, f"{expected} should be on main"
        assert _git(["rev-parse", "--abbrev-ref", "HEAD"], project_root).strip() == "main"
        assert _git(["status", "--porcelain", "--untracked-files=no"], project_root) == ""
        _assert_no_worktrees(project_root)
        for task in tasks.values():
            assert task["cost_usd"] and task["cost_usd"] > 0

    def test_init_then_run_without_git_init(self, fresh_dir: Path) -> None:
        """A directory that is not a repo yet: po initialises it and still merges."""
        cmd_init(_init_args(fresh_dir))
        assert not (fresh_dir / ".git").exists()

        cmd_run(_run_args(fresh_dir))

        assert {t["status"] for t in _tasks(fresh_dir).values()} == {"completed"}
        tracked = _tracked(fresh_dir)
        for expected in (".gitignore", "setup.txt", "feature_a.txt", "feature_b.txt"):
            assert expected in tracked
        _assert_no_worktrees(fresh_dir)

    def test_run_with_spec_file_plans_first(self, fresh_dir: Path) -> None:
        cmd_init(_init_args(fresh_dir))
        cmd_run(_run_args(fresh_dir, spec_file=fresh_dir / "spec.json"))
        assert {t["status"] for t in _tasks(fresh_dir).values()} == {"completed"}

    def test_scaffolds_left_untracked_are_refused_before_any_agent_runs(
        self,
        integration_env: Path,
    ) -> None:
        """Opting into scaffolds and not committing them is caught up front.

        This is the exact state that used to end with every task "completed"
        and nothing on main.
        """
        project_root = integration_env
        cmd_init(_init_args(project_root))
        cmd_plan(_plan_args(project_root, project_root / "spec.json", scaffold=True))
        assert (project_root / "setup.txt").exists()

        with pytest.raises(SystemExit) as exc:
            cmd_run(_run_args(project_root))

        assert exc.value.code == 1
        assert all(t["status"] == "pending" for t in _tasks(project_root).values())
        assert "setup.txt" not in _tracked(project_root)

    def test_init_with_unusable_spec_exits_cleanly(self, integration_env: Path) -> None:
        """Claude returning something that is not a spec is an error, not a traceback."""
        project_root = integration_env
        with pytest.raises(SystemExit) as exc:
            cmd_init(_init_args(project_root, description="mock:bad-spec please"))
        assert exc.value.code == 1
        assert not (project_root / "spec.json").exists()
        assert not state_db_path(project_root).exists()


class TestFailurePaths:
    """Error handling through the real launcher, verifier and merger."""

    def test_verification_failure_retries_then_fails_and_cascades(
        self,
        integration_env: Path,
    ) -> None:
        project_root = integration_env
        spec = {
            "project_name": "failing",
            "max_concurrency": 1,
            "tasks": [
                {
                    "id": "setup",
                    "description": "Create setup",
                    "output_files": ["setup.txt"],
                    "verification": "echo BOOM >&2; false",
                },
                {
                    "id": "child",
                    "description": "Depends on setup",
                    "dependencies": ["setup"],
                    "output_files": ["child.txt"],
                },
            ],
        }
        spec_file = project_root / "spec.json"
        spec_file.write_text(json.dumps(spec))
        cmd_plan(_plan_args(project_root, spec_file))

        cmd_run(_run_args(project_root, max_retries=1))

        tasks = _tasks(project_root)
        assert tasks["setup"]["status"] == "failed"
        assert tasks["setup"]["attempt"] == 2
        assert "BOOM" in str(tasks["setup"]["error_message"])
        assert tasks["child"]["status"] == "cancelled"
        # Nothing landed on main, the tree is clean, and the agent's work is
        # kept on its branch for inspection.
        assert "setup.txt" not in _tracked(project_root)
        assert _git(["status", "--porcelain", "--untracked-files=no"], project_root) == ""
        assert "[branch po/setup kept]" in str(tasks["setup"]["error_message"])
        assert "po/setup" in _git(["branch", "--list", "po/*"], project_root)
        _assert_no_worktrees(project_root)

    def test_agent_reported_failure(self, integration_env: Path) -> None:
        """The agent writes .po-failure.json; the launcher must read it."""
        project_root = integration_env
        spec = {
            "project_name": "giving-up",
            "tasks": [
                {"id": "hard", "description": "mock:fail this one", "output_files": ["hard.txt"]}
            ],
        }
        spec_file = project_root / "spec.json"
        spec_file.write_text(json.dumps(spec))
        cmd_plan(_plan_args(project_root, spec_file))

        cmd_run(_run_args(project_root, max_retries=0))

        task = _tasks(project_root)["hard"]
        assert task["status"] == "failed"
        assert "mock agent gave up" in str(task["error_message"])
        assert "hard.txt" not in _tracked(project_root)
        # Kept, not deleted — `po reset` should be able to resume from it.
        assert (worktrees_dir(project_root) / "hard").exists()

    def test_uncommitted_changes_are_refused_and_survive(self, integration_env: Path) -> None:
        """The merge's `checkout -f` would discard these; po must not get that far."""
        project_root = integration_env
        cmd_init(_init_args(project_root))
        (project_root / "README.md").write_text("# IMPORTANT UNCOMMITTED WORK\n")

        with pytest.raises(SystemExit) as exc:
            cmd_run(_run_args(project_root))

        assert exc.value.code == 1
        assert (project_root / "README.md").read_text() == "# IMPORTANT UNCOMMITTED WORK\n"
        assert all(t["status"] == "pending" for t in _tasks(project_root).values())


class TestExistingProject:
    """Workflow 2: adding a feature to a project that already has history."""

    def test_second_spec_is_refused_then_runs_with_fresh(self, integration_env: Path) -> None:
        project_root = integration_env
        cmd_init(_init_args(project_root))
        cmd_run(_run_args(project_root))
        assert {t["status"] for t in _tasks(project_root).values()} == {"completed"}
        first_head = _git(["rev-parse", "HEAD"], project_root)

        # A second feature whose generated spec reuses the same task ids.
        second = json.loads((project_root / "spec.json").read_text())
        second["project_name"] = "feature-two"
        for task in second["tasks"]:
            task["output_files"] = [f"v2_{f}" for f in task["output_files"]]
        second_file = project_root / "two.json"
        second_file.write_text(json.dumps(second))

        with pytest.raises(SystemExit):
            cmd_plan(_plan_args(project_root, second_file))
        # Old plan untouched, nothing ran
        assert {t["status"] for t in _tasks(project_root).values()} == {"completed"}

        cmd_plan(_plan_args(project_root, second_file, fresh=True))
        assert {t["status"] for t in _tasks(project_root).values()} == {"pending"}
        cmd_run(_run_args(project_root))

        assert {t["status"] for t in _tasks(project_root).values()} == {"completed"}
        tracked = _tracked(project_root)
        for expected in ("v2_setup.txt", "v2_feature_a.txt", "v2_feature_b.txt"):
            assert expected in tracked
        assert _git(["rev-parse", "HEAD"], project_root) != first_head

    def test_tasks_that_modify_committed_files_merge(self, integration_env: Path) -> None:
        """Existing-project tasks rewrite files that are already on main."""
        project_root = integration_env
        spec = {
            "project_name": "brownfield",
            "tasks": [
                {
                    "id": "rewrite-readme",
                    "description": "Rewrite the README",
                    "output_files": ["README.md"],
                }
            ],
        }
        spec_file = project_root / "spec.json"
        spec_file.write_text(json.dumps(spec))
        cmd_plan(_plan_args(project_root, spec_file))

        cmd_run(_run_args(project_root))

        assert _tasks(project_root)["rewrite-readme"]["status"] == "completed"
        assert "generated by mock claude" in (project_root / "README.md").read_text()
