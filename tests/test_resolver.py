"""Tests for graph resolution — topological sort and ready-task detection."""

from __future__ import annotations

import pytest

from po.graph.resolver import CycleError, get_execution_plan, get_ready_tasks, topological_sort
from po.spec.schema import TaskSpec


class TestTopologicalSort:
    def test_no_deps(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A"),
            TaskSpec(id="b", description="B"),
        ]
        order = topological_sort(tasks)
        assert set(order) == {"a", "b"}

    def test_linear_chain(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A"),
            TaskSpec(id="b", description="B", dependencies=["a"]),
            TaskSpec(id="c", description="C", dependencies=["b"]),
        ]
        order = topological_sort(tasks)
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond(self, sample_tasks: list[TaskSpec]) -> None:
        order = topological_sort(sample_tasks)
        assert order.index("task-a") < order.index("task-b")
        assert order.index("task-a") < order.index("task-c")
        assert order.index("task-b") < order.index("task-d")
        assert order.index("task-c") < order.index("task-d")

    def test_cycle_detection(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A", dependencies=["b"]),
            TaskSpec(id="b", description="B", dependencies=["a"]),
        ]
        with pytest.raises(CycleError):
            topological_sort(tasks)

    def test_self_cycle(self) -> None:
        tasks = [TaskSpec(id="a", description="A", dependencies=["a"])]
        with pytest.raises(CycleError):
            topological_sort(tasks)

    def test_three_node_cycle(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A", dependencies=["c"]),
            TaskSpec(id="b", description="B", dependencies=["a"]),
            TaskSpec(id="c", description="C", dependencies=["b"]),
        ]
        with pytest.raises(CycleError):
            topological_sort(tasks)


class TestGetReadyTasks:
    def test_initial_state(self, sample_tasks: list[TaskSpec]) -> None:
        ready = get_ready_tasks(sample_tasks, set(), set(), set(), set())
        assert [t.id for t in ready] == ["task-a"]

    def test_after_first_complete(self, sample_tasks: list[TaskSpec]) -> None:
        ready = get_ready_tasks(sample_tasks, {"task-a"}, set(), set(), set())
        ids = [t.id for t in ready]
        # task-c has priority 8, task-b has priority 5
        assert ids == ["task-c", "task-b"]

    def test_after_two_complete(self, sample_tasks: list[TaskSpec]) -> None:
        ready = get_ready_tasks(sample_tasks, {"task-a", "task-b", "task-c"}, set(), set(), set())
        assert [t.id for t in ready] == ["task-d"]

    def test_running_excluded(self, sample_tasks: list[TaskSpec]) -> None:
        ready = get_ready_tasks(sample_tasks, {"task-a"}, {"task-b"}, set(), set())
        assert [t.id for t in ready] == ["task-c"]

    def test_failed_excluded(self, sample_tasks: list[TaskSpec]) -> None:
        ready = get_ready_tasks(sample_tasks, {"task-a"}, set(), {"task-b"}, set())
        assert [t.id for t in ready] == ["task-c"]

    def test_all_terminal(self, sample_tasks: list[TaskSpec]) -> None:
        all_ids = {t.id for t in sample_tasks}
        ready = get_ready_tasks(sample_tasks, all_ids, set(), set(), set())
        assert ready == []

    def test_priority_ordering(self) -> None:
        tasks = [
            TaskSpec(id="low", description="L", priority=1),
            TaskSpec(id="high", description="H", priority=10),
            TaskSpec(id="mid", description="M", priority=5),
        ]
        ready = get_ready_tasks(tasks, set(), set(), set(), set())
        assert [t.id for t in ready] == ["high", "mid", "low"]


class TestGetExecutionPlan:
    def test_single_layer(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A"),
            TaskSpec(id="b", description="B"),
        ]
        layers = get_execution_plan(tasks)
        assert len(layers) == 1
        assert set(layers[0]) == {"a", "b"}

    def test_linear_layers(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A"),
            TaskSpec(id="b", description="B", dependencies=["a"]),
            TaskSpec(id="c", description="C", dependencies=["b"]),
        ]
        layers = get_execution_plan(tasks)
        assert layers == [["a"], ["b"], ["c"]]

    def test_diamond_layers(self, sample_tasks: list[TaskSpec]) -> None:
        layers = get_execution_plan(sample_tasks)
        assert len(layers) == 3
        assert layers[0] == ["task-a"]
        assert set(layers[1]) == {"task-b", "task-c"}
        assert layers[2] == ["task-d"]

    def test_cycle_raises(self) -> None:
        tasks = [
            TaskSpec(id="a", description="A", dependencies=["b"]),
            TaskSpec(id="b", description="B", dependencies=["a"]),
        ]
        with pytest.raises(CycleError):
            get_execution_plan(tasks)
