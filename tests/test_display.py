"""Tests for display/status formatting functions and LiveDisplay."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from rich.tree import Tree

from po.display.live import _STATUS_STYLES, LiveDisplay, _format_relative_time
from po.display.status import (
    format_cost_summary,
    format_execution_plan,
    format_progress_summary,
    format_status_table,
)
from po.display.tools import tool_summary


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

    def test_failed_task_shows_attempt_count(self) -> None:
        task = _make_task(status="failed", error_message="Something broke", attempt=2)
        result = format_status_table([task])
        assert "└ Something broke  (attempt 2)" in result

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
    def test_no_tokens(self) -> None:
        tasks = [_make_task(input_tokens=None, output_tokens=None, duration_ms=None)]
        result = format_cost_summary(tasks)
        assert "—" in result
        assert "TOTAL" in result

    def test_with_tokens(self) -> None:
        tasks = [_make_task(input_tokens=50000, output_tokens=3000, num_turns=10, duration_ms=1500)]
        result = format_cost_summary(tasks)
        assert "50.0k" in result
        assert "3.0k" in result
        assert "1.5s" in result

    def test_total_calculation(self) -> None:
        tasks = [
            _make_task(id="a", input_tokens=10000, output_tokens=500),
            _make_task(id="b", input_tokens=20000, output_tokens=1500),
        ]
        result = format_cost_summary(tasks)
        assert "30.0k" in result  # total input
        assert "2.0k" in result  # total output

    def test_header_and_separator(self) -> None:
        result = format_cost_summary([_make_task()])
        assert "In tokens" in result
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


# ──────────────── LiveDisplay tests ────────────────


def _render(tree: Any) -> str:
    """Render a Rich renderable to plain text."""
    from io import StringIO

    from rich.console import Console

    console = Console(width=200, record=True, file=StringIO())
    console.print(tree)
    return console.export_text()


def _mock_store(tasks: list[dict[str, Any]] | None = None) -> MagicMock:
    """Create a mock SqliteTaskStore with get_all_tasks returning given tasks."""
    store = MagicMock()
    store.get_all_tasks.return_value = tasks or []
    return store


class TestLiveDisplayBuildTree:
    def test_build_tree_returns_tree(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="task-a", status="pending"),
                _make_task(id="task-b", status="running"),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        assert isinstance(tree, Tree)

    def test_build_tree_correct_node_count(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="task-a", status="pending", dependencies="[]"),
                _make_task(id="task-b", status="completed", dependencies="[]"),
                _make_task(id="task-c", status="failed", dependencies="[]"),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        # All 3 tasks have no deps → 1 layer branch with 3 task nodes
        assert len(tree.children) == 1
        assert len(tree.children[0].children) == 3

    def test_build_tree_nests_subtasks(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="parent", status="decomposed", dependencies="[]"),
                _make_task(id="parent/sub-1", status="running"),
                _make_task(id="parent/sub-2", status="pending"),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        # 1 layer branch → 1 parent node → 2 subtask children
        assert len(tree.children) == 1  # Layer 0
        layer_branch = tree.children[0]
        assert len(layer_branch.children) == 1  # parent
        assert len(layer_branch.children[0].children) == 2  # sub-1, sub-2

    def test_build_tree_progress_in_label(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="a", status="completed", dependencies="[]"),
                _make_task(id="b", status="pending", dependencies="[]"),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        label_text = tree.label.plain  # type: ignore[union-attr]
        assert "1/2 done" in label_text

    def test_build_tree_groups_by_dependency_layer(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="init", status="completed", dependencies="[]"),
                _make_task(id="models", status="pending", dependencies='["init"]'),
                _make_task(id="db", status="pending", dependencies='["init"]'),
                _make_task(id="routes", status="pending", dependencies='["models", "db"]'),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        # 3 layers: [init], [models, db], [routes]
        assert len(tree.children) == 3
        # Layer 0: init
        assert len(tree.children[0].children) == 1
        # Layer 1: db, models (sorted alphabetically)
        assert len(tree.children[1].children) == 2
        # Layer 2: routes
        assert len(tree.children[2].children) == 1

    def test_build_tree_layer_labels(self, tmp_path: Path) -> None:
        store = _mock_store(
            [
                _make_task(id="a", status="pending", dependencies="[]"),
                _make_task(id="b", status="pending", dependencies='["a"]'),
            ]
        )
        display = LiveDisplay(store, tmp_path)
        tree = display._build_tree()
        for i, child in enumerate(tree.children):
            assert f"Layer {i}" in child.label.plain  # type: ignore[union-attr]


class TestLiveDisplayEventUpdatesState:
    def test_launched_sets_running(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="pending")])
        display = LiveDisplay(store, tmp_path)
        display("task_launched", "task-a", "")
        assert display._tasks["task-a"]["status"] == "running"

    def test_completed_sets_completed_with_tokens(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        display("task_completed", "task-a", "50.0k in / 3.2k out / 25 turns")
        assert display._tasks["task-a"]["status"] == "completed"
        assert display._tasks["task-a"]["token_summary"] == "50.0k in / 3.2k out / 25 turns"

    def test_failed_sets_failed_with_error(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        display("task_failed", "task-a", "Boom")
        assert display._tasks["task-a"]["status"] == "failed"
        assert display._tasks["task-a"]["error_message"] == "Boom"

    def test_failed_line_shows_attempts_and_escalated_model(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="pending", attempt=0)])
        display = LiveDisplay(store, tmp_path)
        display("task_launched", "task-a", "")
        display("task_retrying", "task-a", "attempt 1/1")
        display("task_launched", "task-a", "")
        display("model_escalated", "task-a", "sonnet → opus")
        display("task_failed", "task-a", "Agent exited with code -9")

        rendered = _render(display._build_tree())
        assert "Agent exited with code -9" in rendered
        assert "(attempt 2, sonnet → opus)" in rendered

    def test_failed_line_without_escalation_shows_attempt_only(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="pending", attempt=0)])
        display = LiveDisplay(store, tmp_path)
        display("task_launched", "task-a", "")
        display("task_failed", "task-a", "Boom")

        rendered = _render(display._build_tree())
        assert "Boom  (attempt 1)" in rendered

    def test_decomposed_event(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        display("task_decomposed", "task-a", "")
        assert display._tasks["task-a"]["status"] == "decomposed"

    def test_event_for_unknown_task_creates_entry(self, tmp_path: Path) -> None:
        store = _mock_store([])
        display = LiveDisplay(store, tmp_path)
        display("task_launched", "new-task", "")
        assert "new-task" in display._tasks
        assert display._tasks["new-task"]["status"] == "running"


class TestLiveDisplayReadLastAction:
    def test_read_last_action_tool_use(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-a.jsonl"
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will write a file"},
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/foo.ts"}},
                ],
            },
        }
        log_file.write_text(json.dumps(msg) + "\n")

        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert "Write foo.ts" in action

    def test_read_last_action_text_fallback(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-a.jsonl"
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Analyzing the codebase"},
                ],
            },
        }
        log_file.write_text(json.dumps(msg) + "\n")

        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert "Analyzing the codebase" in action

    def test_read_last_action_no_log(self, tmp_path: Path) -> None:
        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert action == "starting..."
        assert ts == ""

    def test_read_last_action_empty_log(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "task-a.jsonl").write_text("")

        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert action == "working..."
        assert ts == ""

    def test_read_last_action_string_content(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-a.jsonl"
        msg = {
            "type": "assistant",
            "message": {"content": "Simple string content"},
        }
        log_file.write_text(json.dumps(msg) + "\n")

        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert "Simple string content" in action

    def test_read_last_action_with_timestamp(self, tmp_path: Path) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-a.jsonl"
        recent = datetime.now(UTC) - timedelta(seconds=30)
        msg = {
            "type": "assistant",
            "timestamp": recent.isoformat(),
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                ],
            },
        }
        log_file.write_text(json.dumps(msg) + "\n")

        store = _mock_store([_make_task(id="task-a", status="running")])
        display = LiveDisplay(store, tmp_path)
        action, ts, _tokens = display._read_last_action("task-a")
        assert "Bash" in action
        assert "s ago" in ts


class TestFormatRelativeTime:
    def test_none_returns_empty(self) -> None:
        assert _format_relative_time(None) == ""

    def test_empty_string_returns_empty(self) -> None:
        assert _format_relative_time("") == ""

    def test_seconds_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(seconds=15)).isoformat()
        result = _format_relative_time(ts)
        assert "s ago" in result

    def test_minutes_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
        result = _format_relative_time(ts)
        assert result == "3m ago"

    def test_hours_ago(self) -> None:
        ts = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        result = _format_relative_time(ts)
        assert result == "2h ago"

    def test_invalid_timestamp(self) -> None:
        assert _format_relative_time("not-a-date") == ""


class TestLiveDisplayStatusStyles:
    def test_all_statuses_have_styles(self) -> None:
        expected = {"pending", "running", "completed", "failed", "cancelled", "decomposed"}
        assert set(_STATUS_STYLES.keys()) == expected

    def test_each_status_has_symbol_and_style(self) -> None:
        for status, (symbol, style) in _STATUS_STYLES.items():
            assert len(symbol) > 0, f"{status} has empty symbol"
            assert len(style) > 0, f"{status} has empty style"

    def test_specific_symbols(self) -> None:
        assert _STATUS_STYLES["pending"][0] == "○"
        assert _STATUS_STYLES["running"][0] == "◉"
        assert _STATUS_STYLES["completed"][0] == "✓"
        assert _STATUS_STYLES["failed"][0] == "✗"
        assert _STATUS_STYLES["cancelled"][0] == "⊘"
        assert _STATUS_STYLES["decomposed"][0] == "◈"


# ──────────────── tool_summary tests ────────────────


class TestToolSummary:
    def test_read_with_path(self) -> None:
        block = {"name": "Read", "input": {"file_path": "/src/po/cli.py"}}
        assert tool_summary(block) == "Read cli.py"

    def test_read_without_path(self) -> None:
        block = {"name": "Read", "input": {}}
        assert tool_summary(block) == "Read"

    def test_glob_with_pattern(self) -> None:
        block = {"name": "Glob", "input": {"pattern": "**/*.ts"}}
        assert tool_summary(block) == "Glob **/*.ts"

    def test_grep_with_pattern(self) -> None:
        block = {"name": "Grep", "input": {"pattern": "tool_use"}}
        assert tool_summary(block) == "Grep 'tool_use'"

    def test_bash_with_description(self) -> None:
        block = {"name": "Bash", "input": {"description": "Run tests", "command": "pytest"}}
        assert tool_summary(block) == "Bash: Run tests"

    def test_bash_with_command_only(self) -> None:
        block = {"name": "Bash", "input": {"command": "npm install"}}
        assert tool_summary(block) == "Bash: npm install"

    def test_bash_empty(self) -> None:
        block = {"name": "Bash", "input": {}}
        assert tool_summary(block) == "Bash"

    def test_edit_with_path(self) -> None:
        block = {"name": "Edit", "input": {"file_path": "/src/po/display/live.py"}}
        assert tool_summary(block) == "Edit live.py"

    def test_write_with_path(self) -> None:
        block = {"name": "Write", "input": {"file_path": "/tmp/foo.ts"}}
        assert tool_summary(block) == "Write foo.ts"

    def test_write_file_variant(self) -> None:
        block = {"name": "write_file", "input": {"file_path": "/tmp/bar.py"}}
        assert tool_summary(block) == "Write bar.py"

    def test_task_with_description(self) -> None:
        block = {"name": "Task", "input": {"description": "Explore codebase"}}
        assert tool_summary(block) == "Task: Explore codebase"

    def test_unknown_tool_returns_name(self) -> None:
        block = {"name": "SomeCustomTool", "input": {}}
        assert tool_summary(block) == "SomeCustomTool"

    def test_missing_name_returns_question_mark(self) -> None:
        block = {"input": {}}
        assert tool_summary(block) == "?"

    def test_long_bash_command_truncated(self) -> None:
        block = {"name": "Bash", "input": {"command": "x" * 100}}
        result = tool_summary(block)
        assert len(result) <= 66  # "Bash: " (6) + 60 chars
