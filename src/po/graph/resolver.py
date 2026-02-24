"""Topological sort and ready-task detection."""

from __future__ import annotations

from po.spec.schema import TaskSpec


class CycleError(Exception):
    """Raised when a dependency cycle is detected."""


def topological_sort(tasks: list[TaskSpec]) -> list[str]:
    """Return task IDs in topological order (dependencies first).

    Raises CycleError if a cycle is detected.
    """
    task_map = {t.id: t for t in tasks}
    visited: set[str] = set()
    in_stack: set[str] = set()
    order: list[str] = []

    def visit(task_id: str) -> None:
        if task_id in in_stack:
            raise CycleError(f"Dependency cycle detected involving task '{task_id}'")
        if task_id in visited:
            return
        in_stack.add(task_id)
        task = task_map[task_id]
        for dep in task.dependencies:
            visit(dep)
        in_stack.remove(task_id)
        visited.add(task_id)
        order.append(task_id)

    for task_id in task_map:
        visit(task_id)

    return order


def get_ready_tasks(
    tasks: list[TaskSpec],
    completed: set[str],
    running: set[str],
    failed: set[str],
    cancelled: set[str],
) -> list[TaskSpec]:
    """Return tasks whose dependencies are all completed, sorted by priority (desc)."""
    not_ready = completed | running | failed | cancelled
    ready = []
    for task in tasks:
        if task.id in not_ready:
            continue
        if all(dep in completed for dep in task.dependencies):
            ready.append(task)
    ready.sort(key=lambda t: t.priority, reverse=True)
    return ready


def get_execution_plan(tasks: list[TaskSpec]) -> list[list[str]]:
    """Return tasks grouped into execution layers.

    Each layer contains tasks that can run concurrently (all deps satisfied by prior layers).
    """
    completed: set[str] = set()
    remaining = {t.id for t in tasks}
    task_map = {t.id: t for t in tasks}
    layers: list[list[str]] = []

    # Validate no cycles first
    topological_sort(tasks)

    while remaining:
        layer = []
        for task_id in sorted(remaining):
            task = task_map[task_id]
            if all(dep in completed for dep in task.dependencies):
                layer.append(task_id)
        if not layer:
            raise CycleError("Deadlock: remaining tasks have unsatisfiable dependencies")
        for task_id in layer:
            remaining.remove(task_id)
            completed.add(task_id)
        layers.append(layer)

    return layers
