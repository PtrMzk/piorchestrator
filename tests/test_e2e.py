"""End-to-end tests with real git, real DB, and scripted agent.

These tests exercise the full pipeline — SqliteTaskStore, GitWorktreeManager,
RebaseMerger — with only the Claude subprocess replaced by a ScriptedAgentRunner
that writes real files and makes real git commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from po.config import worktrees_dir
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore
from po.orchestrator.loop import OrchestratorLoop
from po.orchestrator.merge import RebaseMerger
from po.spec.schema import ProjectSpec
from po.worktree.manager import GitWorktreeManager

from .e2e_support import AgentScript, ScriptedAgentRunner

# ──────────────────────────── Fixtures ────────────────────────────


@pytest.fixture
def e2e_env(git_repo: Path):
    """Set up e2e environment with real git repo, DB, and worktree manager."""
    # Ensure the default branch is named 'main' (required by RebaseMerger)
    subprocess.run(
        ["git", "branch", "-M", "main"],
        cwd=git_repo,
        capture_output=True,
        check=False,
    )
    db_path = git_repo / ".po" / "state.db"
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    yield git_repo, store, conn
    conn.close()


# ──────────────────────────── Helpers ────────────────────────────


def _git_ls_files(repo: Path) -> list[str]:
    """List files tracked on the current branch."""
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip().split("\n") if result.stdout.strip() else []


async def run_orchestrator(
    store: SqliteTaskStore,
    project_root: Path,
    scripts: dict[str, list[AgentScript]],
    spec_dict: dict[str, Any],
    max_concurrency: int = 2,
    max_retries: int = 0,
    on_event: Any = None,
) -> tuple[SqliteTaskStore, list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Create spec, wire up real components with scripted agent, and run."""
    spec = ProjectSpec.from_dict(spec_dict)
    store.save_spec(spec)

    agent = ScriptedAgentRunner(scripts)
    events: list[tuple[str, str, str]] = []

    def capture_event(event: str, task_id: str, detail: str = "") -> None:
        events.append((event, task_id, detail))

    orchestrator = OrchestratorLoop(
        store=store,
        project_root=project_root,
        max_concurrency=max_concurrency,
        worktree_manager=GitWorktreeManager(),
        agent_runner=agent,
        merger=RebaseMerger(),
        max_retries=max_retries,
        on_event=on_event or capture_event,
    )

    await orchestrator.run()
    return store, agent.calls, events


# ──────────────────────────── Tests ────────────────────────────


class TestE2E:
    async def test_linear_chain_all_succeed(self, e2e_env: tuple) -> None:
        """Three tasks in a linear chain — all complete, all files on main."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "linear-chain",
            "tasks": [
                {
                    "id": "init",
                    "description": "Setup project",
                    "dependencies": [],
                    "output_files": ["setup.py"],
                },
                {
                    "id": "models",
                    "description": "Create models",
                    "dependencies": ["init"],
                    "output_files": ["models.py"],
                },
                {
                    "id": "routes",
                    "description": "Create routes",
                    "dependencies": ["models"],
                    "output_files": ["routes.py"],
                },
            ],
        }

        scripts = {
            "init": [
                AgentScript(
                    files={"setup.py": "# setup\n"},
                    commit_message="Add setup",
                )
            ],
            "models": [
                AgentScript(
                    files={"models.py": "# models\n"},
                    commit_message="Add models",
                )
            ],
            "routes": [
                AgentScript(
                    files={"routes.py": "# routes\n"},
                    commit_message="Add routes",
                )
            ],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
        )

        # All 3 tasks completed
        for task_id in ["init", "models", "routes"]:
            task = store.get_task(task_id)
            assert task is not None
            assert task["status"] == "completed", f"{task_id} is {task['status']}"

        # All 3 files present on main
        files = _git_ls_files(project_root)
        for expected in ["setup.py", "models.py", "routes.py"]:
            assert expected in files, f"{expected} not on main"

        # Worktrees cleaned up
        wt_dir = worktrees_dir(project_root)
        remaining = list(wt_dir.iterdir()) if wt_dir.exists() else []
        assert remaining == [], f"Leftover worktrees: {remaining}"

        # Costs recorded
        all_tasks = store.get_all_tasks()
        total_cost = sum(t["cost_usd"] or 0 for t in all_tasks)
        assert total_cost == pytest.approx(0.03, abs=0.001)

    async def test_diamond_dag_concurrent_execution(self, e2e_env: tuple) -> None:
        """Diamond DAG: a → (b, c) → d with max_concurrency=2."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "diamond",
            "tasks": [
                {"id": "a", "description": "Root", "dependencies": [], "output_files": ["a.txt"]},
                {
                    "id": "b",
                    "description": "Left",
                    "dependencies": ["a"],
                    "output_files": ["b.txt"],
                },
                {
                    "id": "c",
                    "description": "Right",
                    "dependencies": ["a"],
                    "output_files": ["c.txt"],
                },
                {
                    "id": "d",
                    "description": "Join",
                    "dependencies": ["b", "c"],
                    "output_files": ["d.txt"],
                },
            ],
        }

        scripts = {
            "a": [AgentScript(files={"a.txt": "a\n"})],
            "b": [AgentScript(files={"b.txt": "b\n"})],
            "c": [AgentScript(files={"c.txt": "c\n"})],
            "d": [AgentScript(files={"d.txt": "d\n"})],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
            max_concurrency=2,
        )

        # All completed
        for task_id in ["a", "b", "c", "d"]:
            task = store.get_task(task_id)
            assert task is not None
            assert task["status"] == "completed"

        # b and c were launched before d
        call_ids = [c["task_id"] for c in calls]
        assert call_ids.index("d") > call_ids.index("b")
        assert call_ids.index("d") > call_ids.index("c")

        # All files on main
        files = _git_ls_files(project_root)
        for expected in ["a.txt", "b.txt", "c.txt", "d.txt"]:
            assert expected in files

    async def test_failure_cancels_dependents(self, e2e_env: tuple) -> None:
        """When a task fails, all transitive dependents are cancelled."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "failure-cascade",
            "tasks": [
                {"id": "a", "description": "Root", "dependencies": [], "output_files": ["a.txt"]},
                {
                    "id": "b",
                    "description": "Left",
                    "dependencies": ["a"],
                    "output_files": ["b.txt"],
                },
                {
                    "id": "c",
                    "description": "Right",
                    "dependencies": ["a"],
                    "output_files": ["c.txt"],
                },
                {
                    "id": "d",
                    "description": "Join",
                    "dependencies": ["b", "c"],
                    "output_files": ["d.txt"],
                },
            ],
        }

        scripts = {
            "a": [
                AgentScript(
                    files={},
                    commit=False,
                    failure_reason="broken",
                )
            ],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
            max_retries=0,
        )

        # a failed
        task_a = store.get_task("a")
        assert task_a is not None
        assert task_a["status"] == "failed"

        # b, c, d cancelled
        for task_id in ["b", "c", "d"]:
            task = store.get_task(task_id)
            assert task is not None
            assert task["status"] == "cancelled", f"{task_id} is {task['status']}"

        # a's worktree is kept, not deleted — `po reset` should resume from it.
        wt_dir = worktrees_dir(project_root)
        assert (wt_dir / "a").exists()

    async def test_subtask_decomposition(self, e2e_env: tuple) -> None:
        """Agent decomposes a task into subtasks; subtasks run and complete."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "subtask-test",
            "tasks": [
                {"id": "big-task", "description": "A big task", "output_files": []},
            ],
        }

        scripts = {
            "big-task": [
                AgentScript(
                    files={},
                    commit=False,
                    subtasks=[
                        {"id": "sub-a", "description": "Sub A", "output_files": ["sub_a.txt"]},
                        {"id": "sub-b", "description": "Sub B", "output_files": ["sub_b.txt"]},
                    ],
                )
            ],
            "big-task/sub-a": [
                AgentScript(
                    files={"sub_a.txt": "sub-a output\n"},
                    commit_message="Add sub_a",
                )
            ],
            "big-task/sub-b": [
                AgentScript(
                    files={"sub_b.txt": "sub-b output\n"},
                    commit_message="Add sub_b",
                )
            ],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
            max_concurrency=2,
        )

        # Parent is decomposed
        parent = store.get_task("big-task")
        assert parent is not None
        assert parent["status"] == "decomposed"

        # Subtasks completed
        for sub_id in ["big-task/sub-a", "big-task/sub-b"]:
            task = store.get_task(sub_id)
            assert task is not None
            assert task["status"] == "completed", f"{sub_id} is {task['status']}"

        # Subtask files on main
        files = _git_ls_files(project_root)
        assert "sub_a.txt" in files
        assert "sub_b.txt" in files

    async def test_retry_on_agent_failure(self, e2e_env: tuple) -> None:
        """First attempt fails; second attempt succeeds with error context in prompt."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "retry-test",
            "tasks": [
                {"id": "flaky", "description": "A flaky task", "output_files": ["result.txt"]},
            ],
        }

        scripts = {
            "flaky": [
                # First attempt fails
                AgentScript(
                    files={},
                    commit=False,
                    failure_reason="timeout",
                ),
                # Second attempt succeeds
                AgentScript(
                    files={"result.txt": "success\n"},
                    commit_message="Add result",
                ),
            ],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
            max_retries=1,
        )

        # Task completed
        task = store.get_task("flaky")
        assert task is not None
        assert task["status"] == "completed"

        # Agent was called twice
        flaky_calls = [c for c in calls if c["task_id"] == "flaky"]
        assert len(flaky_calls) == 2

        # Second call's prompt contains the error from the first attempt
        assert "timeout" in flaky_calls[1]["prompt"]
        assert "Previous Attempt Failed" in flaky_calls[1]["prompt"]

        # File is on main
        files = _git_ls_files(project_root)
        assert "result.txt" in files

    async def test_event_callbacks_fired(self, e2e_env: tuple) -> None:
        """Events are emitted in valid order for a two-task chain."""
        project_root, store, _conn = e2e_env

        spec_dict = {
            "project_name": "events-test",
            "tasks": [
                {"id": "a", "description": "First", "dependencies": [], "output_files": ["a.txt"]},
                {
                    "id": "b",
                    "description": "Second",
                    "dependencies": ["a"],
                    "output_files": ["b.txt"],
                },
            ],
        }

        scripts = {
            "a": [AgentScript(files={"a.txt": "a\n"})],
            "b": [AgentScript(files={"b.txt": "b\n"})],
        }

        store, calls, events = await run_orchestrator(
            store,
            project_root,
            scripts,
            spec_dict,
        )

        event_tuples = [(e[0], e[1]) for e in events]

        # Both tasks were launched and completed
        assert ("task_launched", "a") in event_tuples
        assert ("task_completed", "a") in event_tuples
        assert ("task_launched", "b") in event_tuples
        assert ("task_completed", "b") in event_tuples

        # a launched before b launched (dependency ordering)
        a_launched = event_tuples.index(("task_launched", "a"))
        b_launched = event_tuples.index(("task_launched", "b"))
        assert a_launched < b_launched
