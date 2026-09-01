"""Tests for CLI argument parsing, subcommand dispatch, and error paths."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from po.cli import (
    _live_event_printer,
    cmd_clean,
    cmd_cost,
    cmd_logs,
    cmd_plan,
    cmd_reset,
    cmd_run,
    cmd_status,
    main,
)
from po.config import ensure_gitignore, state_db_path
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore
from po.spec.schema import ProjectSpec

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


def _make_namespace(**kwargs: Any) -> Any:
    """Create a mock argparse.Namespace."""
    import argparse

    ns = argparse.Namespace()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def _setup_planned_project(tmp_path: Path) -> tuple[Path, sqlite3.Connection, SqliteTaskStore]:
    """Create a project root with a saved plan in the DB."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    db_path = state_db_path(project_root)
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    spec = ProjectSpec.from_dict(SAMPLE_SPEC_DICT)
    store.save_spec(spec)
    return project_root, conn, store


# ──────────────── main() parser tests ────────────────


class TestMainParser:
    def test_no_command_exits(self) -> None:
        with pytest.raises(SystemExit), patch("sys.argv", ["po"]):
            main()

    def test_unknown_command_exits(self) -> None:
        with pytest.raises(SystemExit), patch("sys.argv", ["po", "nonexistent"]):
            main()

    def test_plan_dispatches(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT))

        with patch("sys.argv", ["po", "plan", str(spec_file), "--project-root", str(tmp_path)]):
            main()  # Should not raise

        assert state_db_path(tmp_path).exists()

    def test_status_dispatches(self, tmp_path: Path) -> None:
        project_root, conn, _ = _setup_planned_project(tmp_path)
        conn.close()

        with patch("sys.argv", ["po", "status", "--project-root", str(project_root)]):
            main()  # Should not raise


# ──────────────── cmd_plan tests ────────────────


class TestCmdPlan:
    def test_load_spec_and_save(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT))

        args = _make_namespace(
            spec_file=spec_file,
            project_root=tmp_path,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        cmd_plan(args)

        # DB should exist and contain 4 tasks
        db_path = state_db_path(tmp_path)
        assert db_path.exists()
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        tasks = store.get_all_tasks()
        assert len(tasks) == 4
        conn.close()

    def test_missing_spec_file_exits(self) -> None:
        args = _make_namespace(
            spec_file=None,
            project_root=Path("/tmp"),
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        with pytest.raises(SystemExit):
            cmd_plan(args)

    def test_invalid_json_exits(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "bad.json"
        spec_file.write_text("not json at all")

        args = _make_namespace(
            spec_file=spec_file,
            project_root=tmp_path,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        with pytest.raises(SystemExit):
            cmd_plan(args)

    def test_nonexistent_spec_file_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(
            spec_file=tmp_path / "no-such-file.json",
            project_root=tmp_path,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        with pytest.raises(SystemExit):
            cmd_plan(args)

    def test_playground_flag(self, tmp_path: Path) -> None:
        args = _make_namespace(
            spec_file=None,
            project_root=tmp_path,
            playground=True,
            scaffold=False,
            generate_docs=False,
        )
        cmd_plan(args)

        # Should have created the playground spec and saved to DB
        assert state_db_path(tmp_path).exists()

    def test_scaffold_flag(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT))

        args = _make_namespace(
            spec_file=spec_file,
            project_root=tmp_path,
            playground=False,
            scaffold=True,
            generate_docs=False,
        )
        cmd_plan(args)

        # Scaffold should have created output files
        assert (tmp_path / "file_a.py").exists()

    def test_generate_docs_flag(self, tmp_path: Path) -> None:
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT))

        args = _make_namespace(
            spec_file=spec_file,
            project_root=tmp_path,
            playground=False,
            scaffold=False,
            generate_docs=True,
        )
        cmd_plan(args)

        # Docs go into project_root/docs/ (not .po/docs/)
        docs_dir = tmp_path / "docs"
        assert docs_dir.exists()
        assert (docs_dir / "SYSTEM_DESIGN.md").exists()

    def test_plan_rerun_preserves_state(self, tmp_path: Path) -> None:
        """Running plan twice should not clobber completed task state."""
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(SAMPLE_SPEC_DICT))

        args = _make_namespace(
            spec_file=spec_file,
            project_root=tmp_path,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        cmd_plan(args)

        # Mark task-a as completed
        conn = init_db(state_db_path(tmp_path))
        store = SqliteTaskStore(conn)
        store.set_completed("task-a", cost_usd=0.05, duration_ms=500, agent_result="done")
        conn.close()

        # Re-run plan
        cmd_plan(args)

        # task-a should still be completed
        conn = init_db(state_db_path(tmp_path))
        store = SqliteTaskStore(conn)
        task_a = store.get_task("task-a")
        assert task_a["status"] == "completed"
        conn.close()


# ──────────────── cmd_run tests ────────────────


class TestCmdRun:
    def test_no_plan_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(
            spec_file=None,
            project_root=tmp_path,
            concurrency=None,
            max_retries=1,
            model=None,
            max_turns=None,
        )
        with pytest.raises(SystemExit):
            cmd_run(args)

    def test_all_tasks_completed_exits_early(self, tmp_path: Path) -> None:
        project_root, conn, store = _setup_planned_project(tmp_path)
        for t in store.get_all_tasks():
            store.set_completed(t["id"])
        conn.close()

        args = _make_namespace(
            spec_file=None,
            project_root=project_root,
            concurrency=None,
            max_retries=1,
            model=None,
            max_turns=None,
        )
        # Should not raise, just exit early
        cmd_run(args)

    def test_run_invokes_orchestrator(self, tmp_path: Path) -> None:
        project_root, conn, _ = _setup_planned_project(tmp_path)
        conn.close()

        args = _make_namespace(
            spec_file=None,
            project_root=project_root,
            concurrency=2,
            max_retries=3,
            model="opus",
            max_turns=20,
        )

        with patch("po.cli.OrchestratorLoop") as mock_loop_cls:
            mock_instance = MagicMock()
            mock_instance.run = MagicMock(return_value=None)
            mock_loop_cls.return_value = mock_instance

            # asyncio.run with a non-coroutine — mock it
            with patch("po.cli.asyncio.run"):
                cmd_run(args)

            # Verify OrchestratorLoop was constructed with the right args
            call_kwargs = mock_loop_cls.call_args[1]
            assert call_kwargs["max_concurrency"] == 2
            assert call_kwargs["max_retries"] == 3
            assert call_kwargs["model_override"] == "opus"
            assert call_kwargs["max_turns"] == 20


# ──────────────── cmd_status tests ────────────────


class TestCmdStatus:
    def test_no_plan_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(project_root=tmp_path)
        with pytest.raises(SystemExit):
            cmd_status(args)

    def test_shows_status(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        project_root, conn, _ = _setup_planned_project(tmp_path)
        conn.close()

        args = _make_namespace(project_root=project_root)
        cmd_status(args)

        captured = capsys.readouterr()
        assert "task-a" in captured.out
        assert "pending" in captured.out


# ──────────────── cmd_reset tests ────────────────


class TestCmdReset:
    def test_no_plan_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(project_root=tmp_path, task=None)
        with pytest.raises(SystemExit):
            cmd_reset(args)

    def test_reset_all(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        project_root, conn, store = _setup_planned_project(tmp_path)
        store.set_status("task-a", "failed")
        store.set_status("task-b", "cancelled")
        conn.close()

        args = _make_namespace(project_root=project_root, task=None)
        cmd_reset(args)

        captured = capsys.readouterr()
        assert "Reset 2 tasks" in captured.out

    def test_reset_single(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        project_root, conn, store = _setup_planned_project(tmp_path)
        store.set_status("task-a", "failed")
        conn.close()

        args = _make_namespace(project_root=project_root, task="task-a")
        cmd_reset(args)

        captured = capsys.readouterr()
        assert "task-a" in captured.out


# ──────────────── cmd_cost tests ────────────────


class TestCmdCost:
    def test_no_plan_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(project_root=tmp_path)
        with pytest.raises(SystemExit):
            cmd_cost(args)

    def test_shows_cost(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        project_root, conn, store = _setup_planned_project(tmp_path)
        store.set_completed(
            "task-a", cost_usd=0.15,
            input_tokens=50000, output_tokens=3000, num_turns=10,
        )
        conn.close()

        args = _make_namespace(project_root=project_root)
        cmd_cost(args)

        captured = capsys.readouterr()
        assert "50.0k" in captured.out
        assert "3.0k" in captured.out
        assert "10" in captured.out


# ──────────────── cmd_logs tests ────────────────


class TestCmdLogs:
    def test_no_logs_exits(self, tmp_path: Path) -> None:
        args = _make_namespace(
            project_root=tmp_path,
            task_id="nonexistent",
            raw=False,
            tail=0,
        )
        with pytest.raises(SystemExit):
            cmd_logs(args)

    def test_raw_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "my-task.jsonl"
        log_file.write_text('{"type":"result","cost_usd":0.1}\n')

        args = _make_namespace(
            project_root=tmp_path,
            task_id="my-task",
            raw=True,
            tail=0,
        )
        cmd_logs(args)

        captured = capsys.readouterr()
        assert '"type":"result"' in captured.out

    def test_parsed_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-x.jsonl"
        lines = [
            json.dumps({"type": "assistant", "message": {"content": "Hello from agent"}}),
            json.dumps({"type": "result", "cost_usd": 0.05, "result": "done"}),
        ]
        log_file.write_text("\n".join(lines) + "\n")

        args = _make_namespace(
            project_root=tmp_path,
            task_id="task-x",
            raw=False,
            tail=0,
        )
        cmd_logs(args)

        captured = capsys.readouterr()
        assert "[Assistant]" in captured.out
        assert "[Result]" in captured.out
        assert "0.05" in captured.out

    def test_parsed_output_with_blocks(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-blocks.jsonl"
        msg = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I will help"},
                    {"type": "tool_use", "name": "write_file"},
                ],
            },
        }
        log_file.write_text(json.dumps(msg) + "\n")

        args = _make_namespace(
            project_root=tmp_path,
            task_id="task-blocks",
            raw=False,
            tail=0,
        )
        cmd_logs(args)

        captured = capsys.readouterr()
        assert "[Assistant] I will help" in captured.out
        assert "[Tool Call] write_file" in captured.out

    def test_malformed_json_printed_as_is(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        log_dir = tmp_path / ".po" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "task-bad.jsonl"
        log_file.write_text("not json\n")

        args = _make_namespace(
            project_root=tmp_path,
            task_id="task-bad",
            raw=False,
            tail=0,
        )
        cmd_logs(args)

        captured = capsys.readouterr()
        assert "not json" in captured.out


# ──────────────── cmd_clean tests ────────────────


class TestCmdClean:
    def test_no_worktrees(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _make_namespace(project_root=tmp_path)

        with patch("po.worktree.manager.GitWorktreeManager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.list.return_value = []
            mock_mgr.return_value = mock_instance
            cmd_clean(args)

        captured = capsys.readouterr()
        assert "No worktrees" in captured.out

    def test_cleans_terminal_task_worktrees(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        from po.worktree.manager import WorktreeInfo

        project_root, conn, store = _setup_planned_project(tmp_path)
        store.set_completed("task-a")
        store.set_status("task-b", "failed")
        conn.close()

        args = _make_namespace(project_root=project_root)

        with patch("po.worktree.manager.GitWorktreeManager") as mock_mgr:
            mock_instance = MagicMock()
            mock_instance.list.return_value = [
                WorktreeInfo(task_id="task-a", path=Path("/tmp/a"), branch="po/task-a"),
                WorktreeInfo(task_id="task-b", path=Path("/tmp/b"), branch="po/task-b"),
                WorktreeInfo(task_id="task-c", path=Path("/tmp/c"), branch="po/task-c"),
            ]
            mock_mgr.return_value = mock_instance
            cmd_clean(args)

        captured = capsys.readouterr()
        # task-a (completed) and task-b (failed) are terminal, task-c (pending) is not
        assert "task-a" in captured.out
        assert "task-b" in captured.out
        assert "task-c" not in captured.out
        assert "Cleaned 2" in captured.out


# ──────────────── _live_event_printer tests ────────────────


class TestCmdRunDisplayMode:
    def test_cmd_run_uses_live_display_on_tty(self, tmp_path: Path) -> None:
        project_root, conn, _ = _setup_planned_project(tmp_path)
        conn.close()

        args = _make_namespace(
            spec_file=None,
            project_root=project_root,
            concurrency=1,
            max_retries=1,
            model=None,
            max_turns=None,
        )

        with (
            patch("po.cli.OrchestratorLoop") as mock_loop_cls,
            patch("po.cli.asyncio.run"),
            patch("po.cli.sys.stdout") as mock_stdout,
            patch("po.display.live.LiveDisplay.start"),
            patch("po.display.live.LiveDisplay.stop"),
        ):
            mock_stdout.isatty.return_value = True
            mock_loop_cls.return_value = MagicMock()
            cmd_run(args)

            # LiveDisplay should have been used as on_event callback
            call_kwargs = mock_loop_cls.call_args[1]
            from po.display.live import LiveDisplay
            assert isinstance(call_kwargs["on_event"], LiveDisplay)

    def test_cmd_run_uses_simple_printer_non_tty(self, tmp_path: Path) -> None:
        project_root, conn, _ = _setup_planned_project(tmp_path)
        conn.close()

        args = _make_namespace(
            spec_file=None,
            project_root=project_root,
            concurrency=1,
            max_retries=1,
            model=None,
            max_turns=None,
        )

        with (
            patch("po.cli.OrchestratorLoop") as mock_loop_cls,
            patch("po.cli.asyncio.run"),
            patch("po.cli.sys.stdout") as mock_stdout,
        ):
            mock_stdout.isatty.return_value = False
            mock_loop_cls.return_value = MagicMock()
            cmd_run(args)

            # _live_event_printer should have been used
            call_kwargs = mock_loop_cls.call_args[1]
            assert call_kwargs["on_event"] is _live_event_printer


class TestLiveEventPrinter:
    def test_known_events(self, capsys: pytest.CaptureFixture[str]) -> None:
        _live_event_printer("task_launched", "my-task", "starting")
        captured = capsys.readouterr()
        assert "▶" in captured.out
        assert "my-task" in captured.out
        assert "launched" in captured.out

    def test_completed_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        _live_event_printer("task_completed", "task-1", "$0.05")
        captured = capsys.readouterr()
        assert "✓" in captured.out
        assert "$0.05" in captured.out

    def test_unknown_event(self, capsys: pytest.CaptureFixture[str]) -> None:
        _live_event_printer("unknown_event", "task-1", "")
        captured = capsys.readouterr()
        assert "·" in captured.out

    def test_empty_detail(self, capsys: pytest.CaptureFixture[str]) -> None:
        _live_event_printer("task_failed", "task-1", "")
        captured = capsys.readouterr()
        assert "✗" in captured.out
        # No trailing parens for empty detail
        assert "()" not in captured.out


class TestEnsurePoGitignore:
    """Tests for ensure_gitignore."""

    def test_creates_gitignore_with_po(self, tmp_path: Path) -> None:
        ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert ".po/" in content

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        (tmp_path / ".gitignore").write_text("node_modules/\n")
        ensure_gitignore(tmp_path)
        content = (tmp_path / ".gitignore").read_text()
        assert "node_modules/" in content
        assert ".po/" in content

    def test_idempotent(self, tmp_path: Path) -> None:
        ensure_gitignore(tmp_path)
        first = (tmp_path / ".gitignore").read_text()
        ensure_gitignore(tmp_path)
        second = (tmp_path / ".gitignore").read_text()
        assert first == second

    def test_noop_when_already_present(self, tmp_path: Path) -> None:
        original = "stuff\n.po/\nnode_modules/\ndist/\nbuild/\nmore\n"
        (tmp_path / ".gitignore").write_text(original)
        assert ensure_gitignore(tmp_path) is False
        assert (tmp_path / ".gitignore").read_text() == original

    def test_adds_build_artifact_patterns(self, tmp_path: Path) -> None:
        """Build artifacts are covered too, so verification output stays untracked."""
        assert ensure_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        for pattern in (".po/", "node_modules/", "dist/", "build/"):
            assert pattern in content

    def test_partial_match_still_appends(self, tmp_path: Path) -> None:
        """A file with some patterns gets only the missing ones."""
        (tmp_path / ".gitignore").write_text(".po/\n")
        assert ensure_gitignore(tmp_path) is True
        content = (tmp_path / ".gitignore").read_text()
        assert content.count(".po/") == 1
        assert "build/" in content
