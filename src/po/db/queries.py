"""All SQL operations for task state management."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from po.config import (
    SOURCE_RUNTIME,
    SOURCE_SPEC,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
)
from po.spec.schema import ProjectSpec, TaskSpec


class TaskStore(Protocol):
    """Protocol for task state persistence."""

    def save_spec(self, spec: ProjectSpec) -> None: ...
    def upsert_task(self, task: TaskSpec, source: str) -> None: ...
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...
    def get_all_tasks(self) -> list[dict[str, Any]]: ...
    def set_status(self, task_id: str, status: str) -> None: ...
    def increment_attempt(self, task_id: str) -> None: ...
    def set_running(
        self, task_id: str, worktree_path: str, branch_name: str,
    ) -> None: ...
    def set_completed(
        self,
        task_id: str,
        cost_usd: float | None,
        duration_ms: int | None,
        agent_result: str | None,
        session_id: str | None = None,
    ) -> None: ...
    def set_failed(
        self,
        task_id: str,
        error_message: str,
        cost_usd: float | None,
        duration_ms: int | None,
        session_id: str | None = None,
    ) -> None: ...
    def get_ready_task_ids(self) -> list[str]: ...
    def get_running_task_ids(self) -> list[str]: ...
    def reset_task(self, task_id: str) -> None: ...
    def cancel_dependents(self, task_id: str) -> int: ...


@dataclass
class AgentResult:
    """Structured result from an agent run."""

    task_id: str
    success: bool
    cost_usd: float | None = None
    duration_ms: int | None = None
    result_text: str | None = None
    error_message: str | None = None
    subtasks: list[TaskSpec] | None = None
    session_id: str | None = None


class SqliteTaskStore:
    """SQLite-backed task store."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save_spec(self, spec: ProjectSpec) -> None:
        """Save the project spec metadata and all tasks."""
        self.conn.execute(
            """INSERT OR REPLACE INTO project
               (id, project_name, description, default_model,
                max_concurrency, global_context, global_context_files)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (
                spec.project_name,
                spec.description,
                spec.default_model,
                spec.max_concurrency,
                spec.global_context,
                json.dumps(spec.global_context_files),
            ),
        )
        for task in spec.tasks:
            self.upsert_task(task, SOURCE_SPEC)
        self.conn.commit()

    def upsert_task(self, task: TaskSpec, source: str = SOURCE_SPEC) -> None:
        """Insert or update a task.

        Uses ON CONFLICT to only update spec-defined columns, preserving
        runtime state (status, cost, timestamps, etc.) for non-pending tasks.
        """
        self.conn.execute(
            """INSERT INTO tasks
               (id, description, dependencies, context_files,
                output_files, verification, priority, model,
                max_budget_usd, tags, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   description = excluded.description,
                   dependencies = excluded.dependencies,
                   context_files = excluded.context_files,
                   output_files = excluded.output_files,
                   verification = excluded.verification,
                   priority = excluded.priority,
                   model = excluded.model,
                   max_budget_usd = excluded.max_budget_usd,
                   tags = excluded.tags,
                   source = excluded.source""",
            (
                task.id,
                task.description,
                json.dumps(task.dependencies),
                json.dumps(task.context_files),
                json.dumps(task.output_files),
                task.verification,
                task.priority,
                task.model,
                task.max_budget_usd,
                json.dumps(task.tags),
                source,
            ),
        )
        self.conn.commit()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """Get a single task by ID."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_all_tasks(self) -> list[dict[str, Any]]:
        """Get all tasks."""
        rows = self.conn.execute(
            "SELECT * FROM tasks ORDER BY priority DESC, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_project(self) -> dict[str, Any] | None:
        """Get project metadata."""
        row = self.conn.execute(
            "SELECT * FROM project WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def set_status(self, task_id: str, status: str) -> None:
        """Set the status of a task."""
        self.conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
        self.conn.commit()

    def increment_attempt(self, task_id: str) -> None:
        """Increment the attempt counter for a task."""
        self.conn.execute(
            "UPDATE tasks SET attempt = attempt + 1 WHERE id = ?",
            (task_id,),
        )
        self.conn.commit()

    def set_running(
        self, task_id: str, worktree_path: str, branch_name: str,
    ) -> None:
        """Mark a task as running with its worktree info."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE tasks
               SET status = ?, worktree_path = ?, branch_name = ?,
                   started_at = ?, attempt = attempt + 1
               WHERE id = ?""",
            (STATUS_RUNNING, worktree_path, branch_name, now, task_id),
        )
        self.conn.commit()

    def set_completed(
        self,
        task_id: str,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        agent_result: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Mark a task as completed."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE tasks
               SET status = ?, cost_usd = ?, duration_ms = ?,
                   agent_result = ?, session_id = ?, completed_at = ?
               WHERE id = ?""",
            (STATUS_COMPLETED, cost_usd, duration_ms,
             agent_result, session_id, now, task_id),
        )
        self.conn.commit()

    def set_failed(
        self,
        task_id: str,
        error_message: str,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        session_id: str | None = None,
    ) -> None:
        """Mark a task as failed."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE tasks
               SET status = ?, error_message = ?,
                   cost_usd = ?, duration_ms = ?,
                   session_id = ?, completed_at = ?
               WHERE id = ?""",
            (STATUS_FAILED, error_message, cost_usd,
             duration_ms, session_id, now, task_id),
        )
        self.conn.commit()

    def get_ready_task_ids(self) -> list[str]:
        """Get IDs of tasks whose dependencies are all completed.

        Uses json_each() to check that every dependency is in 'completed' status.
        """
        rows = self.conn.execute(
            """SELECT t.id, t.priority FROM tasks t
               WHERE t.status = ?
               AND NOT EXISTS (
                   SELECT 1 FROM json_each(t.dependencies) AS dep
                   WHERE dep.value NOT IN (
                       SELECT id FROM tasks WHERE status = ?
                   )
               )
               ORDER BY t.priority DESC, t.id""",
            (STATUS_PENDING, STATUS_COMPLETED),
        ).fetchall()
        return [row["id"] for row in rows]

    def get_running_task_ids(self) -> list[str]:
        """Get IDs of currently running tasks."""
        rows = self.conn.execute(
            "SELECT id FROM tasks WHERE status = ?", (STATUS_RUNNING,)
        ).fetchall()
        return [row["id"] for row in rows]

    def get_tasks_by_status(self, status: str) -> list[dict[str, Any]]:
        """Get all tasks with a given status."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, id", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def reset_task(self, task_id: str) -> None:
        """Reset a failed/cancelled task to pending, cascading to dependents."""
        self._reset_single(task_id)
        # Also reset dependents that were cascade-cancelled due to this task
        all_tasks = self.get_all_tasks()
        queue = [task_id]
        seen: set[str] = {task_id}
        while queue:
            current = queue.pop(0)
            for task in all_tasks:
                tid: str = task["id"]
                if tid in seen:
                    continue
                deps: list[str] = (
                    json.loads(task["dependencies"])
                    if isinstance(task["dependencies"], str)
                    else task["dependencies"]
                )
                if current in deps and task["status"] == STATUS_CANCELLED:
                    self._reset_single(tid)
                    seen.add(tid)
                    queue.append(tid)
        self.conn.commit()

    def _reset_single(self, task_id: str) -> None:
        """Reset a single task to pending (no commit)."""
        self.conn.execute(
            """UPDATE tasks
               SET status = ?, error_message = NULL,
                   agent_result = NULL, worktree_path = NULL,
                   branch_name = NULL, session_id = NULL,
                   cost_usd = NULL, duration_ms = NULL,
                   started_at = NULL, completed_at = NULL
               WHERE id = ? AND status IN (?, ?)""",
            (STATUS_PENDING, task_id, STATUS_FAILED, STATUS_CANCELLED),
        )

    def cancel_dependents(self, task_id: str) -> int:
        """Cancel all tasks that transitively depend on the given task. Returns count."""
        # Gather all transitive dependents
        to_cancel: set[str] = set()
        queue = [task_id]
        all_tasks = self.get_all_tasks()
        while queue:
            current = queue.pop(0)
            for task in all_tasks:
                deps: list[str] = (
                    json.loads(task["dependencies"])
                    if isinstance(task["dependencies"], str)
                    else task["dependencies"]
                )
                tid: str = task["id"]
                if (
                    current in deps
                    and tid not in to_cancel
                    and task["status"] not in TERMINAL_STATUSES
                ):
                    to_cancel.add(tid)
                    queue.append(tid)

        for tid in to_cancel:
            self.conn.execute(
                "UPDATE tasks SET status = ?, error_message = ? WHERE id = ?",
                (STATUS_CANCELLED,
                 f"Cancelled: dependency '{task_id}' failed",
                 tid),
            )
        self.conn.commit()
        return len(to_cancel)

    def add_runtime_task(self, task: TaskSpec, parent_task_id: str) -> None:
        """Add a runtime-generated subtask."""
        self.conn.execute(
            """INSERT INTO tasks
               (id, description, dependencies, context_files,
                output_files, verification, priority, model,
                max_budget_usd, tags, source, parent_task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task.id,
                task.description,
                json.dumps(task.dependencies),
                json.dumps(task.context_files),
                json.dumps(task.output_files),
                task.verification,
                task.priority,
                task.model,
                task.max_budget_usd,
                json.dumps(task.tags),
                SOURCE_RUNTIME,
                parent_task_id,
            ),
        )
        self.conn.commit()

    def get_total_cost(self) -> float:
        """Get total cost across all tasks."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) as total FROM tasks"
        ).fetchone()
        return float(row["total"])

    def get_status_counts(self) -> dict[str, int]:
        """Get count of tasks per status."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}
