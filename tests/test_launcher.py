"""Tests for agent launcher — ClaudeCodeRunner."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from po.agent.launcher import ClaudeCodeRunner


def _jsonl(*dicts: dict) -> bytes:
    """Encode dicts as newline-delimited JSON bytes."""
    return "\n".join(json.dumps(d) for d in dicts).encode()


def _make_mock_process(
    stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0,
) -> AsyncMock:
    proc = AsyncMock()
    proc.communicate.return_value = (stdout, stderr)
    proc.returncode = returncode
    return proc


class TestClaudeCodeRunner:
    @pytest.mark.asyncio
    async def test_successful_run_parses_result(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        stdout = _jsonl(
            {"type": "assistant", "message": "working..."},
            {"type": "result", "result": "Done!", "cost_usd": 0.05, "session_id": "sess-1"},
        )
        mock_proc = _make_mock_process(stdout=stdout, returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is True
        assert result.result_text == "Done!"
        assert result.cost_usd == 0.05
        assert result.session_id == "sess-1"
        assert result.error_message is None
        assert result.duration_ms is not None and result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_failed_run_captures_stderr(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        mock_proc = _make_mock_process(stderr=b"something broke", returncode=1)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is False
        assert result.error_message == "something broke"

    @pytest.mark.asyncio
    async def test_cli_not_found(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        runner = ClaudeCodeRunner()
        with patch(
            "po.agent.launcher.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError,
        ):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is False
        assert "Claude CLI not found" in (result.error_message or "")
        assert result.duration_ms is not None and result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_subtasks_file_detected(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        subtasks = [
            {"id": "sub1", "description": "Subtask 1"},
            {"id": "sub2", "description": "Subtask 2"},
        ]
        (worktree / ".po-subtasks.json").write_text(json.dumps(subtasks))

        stdout = _jsonl({"type": "result", "result": "ok"})
        mock_proc = _make_mock_process(stdout=stdout, returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.subtasks is not None
        assert len(result.subtasks) == 2
        assert result.subtasks[0].id == "sub1"

    @pytest.mark.asyncio
    async def test_failure_file_detected(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        (worktree / ".po-failure.json").write_text(json.dumps({"reason": "Too complex"}))

        mock_proc = _make_mock_process(returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is False
        assert result.error_message == "Too complex"

    @pytest.mark.asyncio
    async def test_malformed_failure_file(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        (worktree / ".po-failure.json").write_text("not json {{{")

        mock_proc = _make_mock_process(returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is False
        assert "could not parse" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_max_cost_flag_included(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        mock_proc = _make_mock_process(returncode=0)
        mock_exec = AsyncMock(return_value=mock_proc)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", mock_exec):
            await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root, max_budget_usd=1.50,
            )

        cmd_args = mock_exec.call_args[0]
        assert "--max-cost" in cmd_args
        assert "1.5" in cmd_args

    @pytest.mark.asyncio
    async def test_max_cost_flag_not_included_when_none(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        mock_proc = _make_mock_process(returncode=0)
        mock_exec = AsyncMock(return_value=mock_proc)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", mock_exec):
            await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root, max_budget_usd=None,
            )

        cmd_args = mock_exec.call_args[0]
        assert "--max-cost" not in cmd_args

    @pytest.mark.asyncio
    async def test_log_file_written(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        stdout = b'{"type":"result","result":"ok"}\n'
        mock_proc = _make_mock_process(stdout=stdout, returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            await runner.run("t1", "do stuff", worktree, "sonnet", project_root=project_root)

        log_file = project_root / ".po" / "logs" / "t1.jsonl"
        assert log_file.exists()
        assert log_file.read_bytes() == stdout

    @pytest.mark.asyncio
    async def test_empty_stderr_uses_exit_code(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        mock_proc = _make_mock_process(stderr=b"", returncode=42)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is False
        assert "42" in (result.error_message or "")

    @pytest.mark.asyncio
    async def test_malformed_jsonl_lines_skipped(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        stdout = b'not json at all\n{"type":"result","result":"ok","cost_usd":0.01}\n'
        mock_proc = _make_mock_process(stdout=stdout, returncode=0)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await runner.run(
                "t1", "do stuff", worktree, "sonnet",
                project_root=project_root,
            )

        assert result.success is True
        assert result.result_text == "ok"
        assert result.cost_usd == 0.01

    @pytest.mark.asyncio
    async def test_command_structure(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()

        mock_proc = _make_mock_process(returncode=0)
        mock_exec = AsyncMock(return_value=mock_proc)

        runner = ClaudeCodeRunner()
        with patch("po.agent.launcher.asyncio.create_subprocess_exec", mock_exec):
            await runner.run(
                "t1", "my prompt", worktree, "opus",
                max_turns=25, project_root=project_root,
            )

        cmd_args = mock_exec.call_args[0]
        assert cmd_args[0] == "claude"
        assert "-p" in cmd_args
        assert "my prompt" in cmd_args
        assert "--output-format" in cmd_args
        assert "stream-json" in cmd_args
        assert "--model" in cmd_args
        assert "opus" in cmd_args
        assert "--max-turns" in cmd_args
        assert "25" in cmd_args
        assert mock_exec.call_args[1]["cwd"] == str(worktree)
