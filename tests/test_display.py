"""Tests for display/status formatting functions."""

from __future__ import annotations

from typing import Any

from po.display.status import (
    format_cost_summary,
    format_execution_plan,
    format_progress_summary,
    format_status_table,
)


def _make_task(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "id": "task-1",
        "status": "pending",
        "description": "A task",
        "cost_usd": None,
        "duration_ms": None,
        "error_message": None,
    }
    defaults.update(overrides)
    return defaults


class TestFormatStatusTable:
    def test_empty_list(self) -> None:
        assert format_status_table([]) == "No tasks found."

    def test_single_pending_task(self) -> None:
        result = format_status_table([_make_task()])
        assert "○" in result
        assert "pending" in result
        assert "task-1" in result

    def test_multiple_statuses(self) -> None:
        tasks = [
            _make_task(id="a", status="completed"),
            _make_task(id="b", status="failed", error_message="Boom"),
            _make_task(id="c", status="running"),
        ]
        result = format_status_table(tasks)
        assert "✓" in result
        assert "✗" in result
        assert "◉" in result

    def test_failed_task_shows_error(self) -> None:
        task = _make_task(status="failed", error_message="Something broke")
        result = format_status_table([task])
        assert "└ Something broke" in result

    def test_failed_task_no_error_no_extra_line(self) -> None:
        task = _make_task(status="failed")
        result = format_status_table([task])
        assert "└" not in result

    def test_cost_display(self) -> None:
        task = _make_task(cost_usd=0.1234)
        result = format_status_table([task])
        assert "$0.1234" in result

    def test_no_cost_empty(self) -> None:
        task = _make_task(cost_usd=None)
        result = format_status_table([task])
        # No dollar sign for None cost
        assert "$" not in result.split("\n")[-1]

    def test_decomposed_status(self) -> None:
        task = _make_task(status="decomposed")
        result = format_status_table([task])
        assert "◈" in result
        assert "decomposed" in result

    def test_unknown_status_uses_question_mark(self) -> None:
        task = _make_task(status="weird")
        result = format_status_table([task])
        assert "?" in result

    def test_long_description_truncated(self) -> None:
        task = _make_task(description="A" * 100)
        result = format_status_table([task])
        # Description column is truncated at 38 chars
        lines = result.split("\n")
        data_line = lines[2]  # skip header + separator
        assert "A" * 38 in data_line
        assert "A" * 39 not in data_line

    def test_long_error_truncated(self) -> None:
        task = _make_task(status="failed", error_message="E" * 200)
        result = format_status_table([task])
        error_line = [x for x in result.split("\n") if "└" in x][0]
        # Error after └ is truncated at 72 chars
        assert "E" * 72 in error_line
        assert "E" * 73 not in error_line

    def test_header_present(self) -> None:
        result = format_status_table([_make_task()])
        assert "Status" in result
        assert "ID" in result
        assert "Cost" in result
        assert "─" in result


class TestFormatExecutionPlan:
    def test_single_layer(self) -> None:
        layers = [["t1", "t2"]]
        tasks = [
            {"id": "t1", "description": "First", "status": "pending"},
            {"id": "t2", "description": "Second", "status": "pending"},
        ]
        result = format_execution_plan(layers, tasks)
        assert "Layer 0" in result
        assert "t1" in result
        assert "t2" in result

    def test_multiple_layers(self) -> None:
        layers = [["t1"], ["t2"]]
        tasks = [
            {"id": "t1", "description": "First", "status": "pending"},
            {"id": "t2", "description": "Second", "status": "pending"},
        ]
        result = format_execution_plan(layers, tasks)
        assert "Layer 0" in result
        assert "Layer 1" in result

    def test_header_present(self) -> None:
        result = format_execution_plan([], [])
        assert "Execution Plan" in result
        assert "═" in result


class TestFormatCostSummary:
    def test_no_costs(self) -> None:
        tasks = [_make_task(cost_usd=None, duration_ms=None)]
        result = format_cost_summary(tasks)
        assert "—" in result
        assert "$0.0000" in result  # total

    def test_with_costs(self) -> None:
        tasks = [_make_task(cost_usd=0.1234, duration_ms=1500)]
        result = format_cost_summary(tasks)
        assert "$0.1234" in result
        assert "1.5s" in result

    def test_total_calculation(self) -> None:
        tasks = [
            _make_task(id="a", cost_usd=0.10),
            _make_task(id="b", cost_usd=0.25),
        ]
        result = format_cost_summary(tasks)
        assert "$0.3500" in result

    def test_header_and_separator(self) -> None:
        result = format_cost_summary([_make_task()])
        assert "Cost" in result
        assert "Duration" in result
        assert "TOTAL" in result


class TestFormatProgressSummary:
    def test_all_pending(self) -> None:
        tasks = [_make_task(), _make_task(id="t2")]
        result = format_progress_summary(tasks)
        assert "0/2 completed" in result
        assert "2 pending" in result

    def test_mixed_statuses(self) -> None:
        tasks = [
            _make_task(id="a", status="completed"),
            _make_task(id="b", status="failed"),
            _make_task(id="c", status="running"),
        ]
        result = format_progress_summary(tasks)
        assert "1/3 completed" in result
        assert "1 failed" in result
        assert "1 running" in result

    def test_with_decomposed(self) -> None:
        tasks = [_make_task(status="decomposed")]
        result = format_progress_summary(tasks)
        assert "1 decomposed" in result

    def test_without_decomposed(self) -> None:
        tasks = [_make_task(status="pending")]
        result = format_progress_summary(tasks)
        assert "decomposed" not in result

    def test_empty_list(self) -> None:
        result = format_progress_summary([])
        assert "0/0 completed" in result
