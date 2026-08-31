"""Shared test fixtures for PO tests."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from po.db.connection import init_db
from po.db.queries import AgentResult, SqliteTaskStore
from po.spec.schema import ProjectSpec, TaskSpec
from po.worktree.manager import WorktreeInfo

# ──────────────────────────── Sample data ────────────────────────────


SAMPLE_SPEC_DICT: dict[str, Any] = {
    "project_name": "test-project",
    "description": "A test project",
    "default_model": "sonnet",
    "max_concurrency": 2,
    "global_context": "Use Python 3.12.",
    "global_context_files": [],
    "tasks": [
        {
            "id": "task-a",
            "description": "First task",
            "dependencies": [],
            "output_files": ["file_a.py"],
            "verification": "python -c 'print(1)'",
            "priority": 10,
            "tags": ["setup"],
        },
        {
            "id": "task-b",
            "description": "Second task, depends on A",
            "dependencies": ["task-a"],
            "output_files": ["file_b.py"],
            "priority": 5,
            "tags": ["core"],
        },
        {
            "id": "task-c",
            "description": "Third task, depends on A",
            "dependencies": ["task-a"],
            "output_files": ["file_c.py"],
            "priority": 8,
            "tags": ["core"],
        },
        {
            "id": "task-d",
            "description": "Fourth task, depends on B and C",
            "dependencies": ["task-b", "task-c"],
            "output_files": ["file_d.py"],
            "priority": 3,
        },
    ],
}


@pytest.fixture
def sample_spec() -> ProjectSpec:
    """Return a sample ProjectSpec for testing."""
    return ProjectSpec.from_dict(SAMPLE_SPEC_DICT)


@pytest.fixture
def sample_tasks(sample_spec: ProjectSpec) -> list[TaskSpec]:
    """Return the tasks from the sample spec."""
    return sample_spec.tasks


@pytest.fixture
def sample_spec_json(tmp_path: Path) -> Path:
    """Write the sample spec to a JSON file and return its path."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT), encoding="utf-8")
    return spec_file


# ──────────────────────────── Database ────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a temporary database path."""
    return tmp_path / ".po" / "state.db"


@pytest.fixture
def db_conn(db_path: Path) -> sqlite3.Connection:
    """Return an initialized database connection."""
    conn = init_db(db_path)
    yield conn  # type: ignore[misc]
    conn.close()


@pytest.fixture
def store(db_conn: sqlite3.Connection) -> SqliteTaskStore:
    """Return an SqliteTaskStore backed by a temp database."""
    return SqliteTaskStore(db_conn)


# ──────────────────────────── Git repo ────────────────────────────


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # -b main: the merger and several tests assume 'main'; without this the
    # branch name comes from the machine's init.defaultBranch (often 'master').
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo, capture_output=True, check=True,
    )
    # Create initial commit
    (repo / "README.md").write_text("# Test\n")
    subprocess.run(
        ["git", "add", "."], cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo, capture_output=True, check=True,
    )
    return repo


# ──────────────────────────── Mock implementations ────────────────────────────


class MockAgentRunner:
    """Mock agent runner that simulates configurable per-task outcomes.

    Configure outcomes via the `outcomes` dict mapping task_id -> AgentResult.
    Tasks not in `outcomes` succeed by default.
    """

    def __init__(self, outcomes: dict[str, AgentResult] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        task_id: str,
        prompt: str,
        worktree_path: Path,
        model: str,
        max_turns: int = 50,
        project_root: Path = Path("."),
        max_budget_usd: float | None = None,
    ) -> AgentResult:
        self.calls.append({
            "task_id": task_id,
            "prompt": prompt,
            "worktree_path": worktree_path,
            "model": model,
        })
        if task_id in self.outcomes:
            return self.outcomes[task_id]
        return AgentResult(
            task_id=task_id,
            success=True,
            cost_usd=0.01,
            duration_ms=100,
            result_text="Mock success",
        )


class MockWorktreeProvider:
    """Mock worktree provider that uses temp directories.

    No real git operations — just creates directories.
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base = base_dir or Path(tempfile.mkdtemp())
        self._worktrees: dict[str, WorktreeInfo] = {}

    def create(self, task_id: str, project_root: Path) -> WorktreeInfo:
        wt_path = self._base / task_id
        wt_path.mkdir(parents=True, exist_ok=True)
        info = WorktreeInfo(
            task_id=task_id,
            path=wt_path,
            branch=f"po/{task_id}",
        )
        self._worktrees[task_id] = info
        return info

    def detach(self, task_id: str, project_root: Path) -> None:
        pass  # No-op for mock — worktree dir doesn't matter

    def remove(self, task_id: str, project_root: Path) -> None:
        self._worktrees.pop(task_id, None)

    def list(self, project_root: Path) -> list[WorktreeInfo]:
        return list(self._worktrees.values())

    def exists(self, task_id: str, project_root: Path) -> bool:
        return task_id in self._worktrees


class MockMergeStrategy:
    """Mock merge strategy that always succeeds (configurable)."""

    def __init__(self, fail_tasks: set[str] | None = None) -> None:
        self.fail_tasks = fail_tasks or set()
        self.merged: list[str] = []

    async def merge(self, branch: str, task_id: str, verification: str, project_root: Path) -> Any:
        from po.orchestrator.merge import MergeResult

        if task_id in self.fail_tasks:
            return MergeResult(success=False, error_message=f"Mock merge failure for {task_id}")
        self.merged.append(task_id)
        return MergeResult(success=True)


@pytest.fixture
def mock_agent() -> MockAgentRunner:
    return MockAgentRunner()


@pytest.fixture
def mock_worktree(tmp_path: Path) -> MockWorktreeProvider:
    return MockWorktreeProvider(tmp_path / "worktrees")


@pytest.fixture
def mock_merger() -> MockMergeStrategy:
    return MockMergeStrategy()
