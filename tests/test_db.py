"""Tests for database operations."""

from __future__ import annotations

import pytest

from po.db.queries import SqliteTaskStore
from po.spec.schema import ProjectSpec, TaskSpec


class TestSqliteTaskStore:
    def test_save_spec_and_get_project(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        project = store.get_project()
        assert project is not None
        assert project["project_name"] == "test-project"
        assert project["max_concurrency"] == 2

    def test_save_spec_persists_tasks(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        tasks = store.get_all_tasks()
        assert len(tasks) == 4
        ids = [t["id"] for t in tasks]
        assert "task-a" in ids
        assert "task-d" in ids

    def test_get_task(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        task = store.get_task("task-a")
        assert task is not None
        assert task["description"] == "First task"
        assert task["status"] == "pending"

    def test_get_task_not_found(self, store: SqliteTaskStore) -> None:
        assert store.get_task("nonexistent") is None

    def test_set_running(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp/wt/task-a", "po/task-a")
        task = store.get_task("task-a")
        assert task is not None
        assert task["status"] == "running"
        assert task["worktree_path"] == "/tmp/wt/task-a"
        assert task["branch_name"] == "po/task-a"
        assert task["attempt"] == 1
        assert task["started_at"] is not None

    def test_set_completed(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp/wt", "po/task-a")
        store.set_completed("task-a", cost_usd=0.05, duration_ms=500)
        task = store.get_task("task-a")
        assert task is not None
        assert task["status"] == "completed"
        assert task["cost_usd"] == 0.05
        assert task["duration_ms"] == 500
        assert task["completed_at"] is not None

    def test_set_failed(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp/wt", "po/task-a")
        store.set_failed("task-a", error_message="Boom")
        task = store.get_task("task-a")
        assert task is not None
        assert task["status"] == "failed"
        assert task["error_message"] == "Boom"

    def test_get_ready_task_ids_initial(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        ready = store.get_ready_task_ids()
        assert ready == ["task-a"]

    def test_get_ready_task_ids_after_completion(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp", "po/task-a")
        store.set_completed("task-a")
        ready = store.get_ready_task_ids()
        # task-c (priority 8) before task-b (priority 5)
        assert ready == ["task-c", "task-b"]

    def test_get_ready_task_ids_after_all_deps(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        for tid in ["task-a", "task-b", "task-c"]:
            store.set_running(tid, "/tmp", f"po/{tid}")
            store.set_completed(tid)
        ready = store.get_ready_task_ids()
        assert ready == ["task-d"]

    def test_get_running_task_ids(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp", "po/task-a")
        running = store.get_running_task_ids()
        assert running == ["task-a"]

    def test_reset_task(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp", "po/task-a")
        store.set_failed("task-a", "err")
        store.reset_task("task-a")
        task = store.get_task("task-a")
        assert task is not None
        assert task["status"] == "pending"
        assert task["error_message"] is None
        assert task["worktree_path"] is None

    def test_reset_only_failed_or_cancelled(
        self, store: SqliteTaskStore, sample_spec: ProjectSpec,
    ) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp", "po/task-a")
        store.set_completed("task-a")
        store.reset_task("task-a")  # Should not reset completed
        task = store.get_task("task-a")
        assert task is not None
        assert task["status"] == "completed"

    def test_cancel_dependents(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        store.set_running("task-a", "/tmp", "po/task-a")
        store.set_failed("task-a", "err")
        count = store.cancel_dependents("task-a")
        assert count == 3  # task-b, task-c, task-d all depend transitively
        for tid in ["task-b", "task-c", "task-d"]:
            task = store.get_task(tid)
            assert task is not None
            assert task["status"] == "cancelled"

    def test_add_runtime_task(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        subtask = TaskSpec(id="task-a-sub1", description="Subtask of A")
        store.add_runtime_task(subtask, parent_task_id="task-a")
        task = store.get_task("task-a-sub1")
        assert task is not None
        assert task["source"] == "runtime"
        assert task["parent_task_id"] == "task-a"

    def test_upsert_replaces(self, store: SqliteTaskStore, sample_spec: ProjectSpec) -> None:
        store.save_spec(sample_spec)
        updated = TaskSpec(id="task-a", description="Updated description")
        store.upsert_task(updated)
        task = store.get_task("task-a")
        assert task is not None
        assert task["description"] == "Updated description"
