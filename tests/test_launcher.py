"""Tests for agent launcher — ClaudeCodeRunner."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from po import procs
from po.agent.launcher import ClaudeCodeRunner


def _jsonl(*dicts: dict) -> bytes:
    """Encode dicts as newline-delimited JSON bytes."""
    return "\n".join(json.dumps(d) for d in dicts).encode()


class _AsyncLineIterator:
    """Simulate an async readline iterator over bytes, splitting on newlines."""

    def __init__(self, data: bytes) -> None:
        self._lines = [line + b"\n" for line in data.split(b"\n") if line]

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


def _make_mock_process(
    stdout: bytes = b"",
    stderr: bytes = b"",
    returncode: int = 0,
) -> AsyncMock:
    proc = AsyncMock()
    proc.stdout = _AsyncLineIterator(stdout)
    proc.stderr = _AsyncLineIterator(stderr)
    proc.wait = AsyncMock(return_value=returncode)
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
                project_root=project_root,
                max_budget_usd=1.50,
            )

        cmd_args = mock_exec.call_args[0]
        assert "--max-budget-usd" in cmd_args
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
                project_root=project_root,
                max_budget_usd=None,
            )

        cmd_args = mock_exec.call_args[0]
        assert "--max-budget-usd" not in cmd_args

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
        logged = json.loads(log_file.read_text().strip())
        assert logged["type"] == "result"
        assert logged["result"] == "ok"
        assert "timestamp" in logged

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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "do stuff",
                worktree,
                "sonnet",
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
                "t1",
                "my prompt",
                worktree,
                "opus",
                max_turns=25,
                project_root=project_root,
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


class TestAgentCancellation:
    """Cancelling a task must take the agent's whole process tree with it.

    These use a real subprocess rather than a mock, because the behaviour under
    test is process-group signalling.
    """

    @staticmethod
    def _stub_claude(bin_dir: Path, body: str) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "claude"
        script.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
        script.chmod(0o755)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @pytest.mark.asyncio
    async def test_cancel_kills_the_agents_children(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A tool call the agent started (`npm run dev`) must not outlive it."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        pid_file = tmp_path / "child.pid"
        self._stub_claude(
            tmp_path / "bin",
            "import subprocess, time\n"
            f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(120)'])\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(120)\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        task = asyncio.create_task(
            ClaudeCodeRunner().run(
                task_id="cancelled",
                prompt="p",
                worktree_path=worktree,
                model="sonnet",
                project_root=tmp_path,
            )
        )
        # Wait for the agent to spawn its child before cancelling.
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        grandchild = int(pid_file.read_text())

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        for _ in range(100):
            if not self._alive(grandchild):
                return
            await asyncio.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)
        raise AssertionError(f"agent's child {grandchild} survived cancellation")

    @pytest.mark.asyncio
    async def test_agent_pid_is_untracked_after_a_normal_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A finished agent must not linger in the registry as a stale pid."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        result_line = json.dumps({"type": "result", "result": "ok"})
        self._stub_claude(tmp_path / "bin", f"print({result_line!r})")
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        await ClaudeCodeRunner().run(
            task_id="clean",
            prompt="p",
            worktree_path=worktree,
            model="sonnet",
            project_root=tmp_path,
        )

        assert procs.shutdown() == 0


class TestLogRotation:
    @pytest.mark.asyncio
    async def test_retry_keeps_previous_attempts_log(self, tmp_path: Path) -> None:
        worktree = tmp_path / "wt"
        worktree.mkdir()
        project_root = tmp_path / "project"
        project_root.mkdir()
        runner = ClaudeCodeRunner()

        for text in ("first", "second", "third"):
            stdout = _jsonl({"type": "result", "result": text})
            mock_proc = _make_mock_process(stdout=stdout, returncode=1)
            with patch("po.agent.launcher.asyncio.create_subprocess_exec", return_value=mock_proc):
                await runner.run("t1", "do stuff", worktree, "sonnet", project_root=project_root)

        log_dir = project_root / ".po" / "logs"
        assert "third" in (log_dir / "t1.jsonl").read_text()
        assert "first" in (log_dir / "t1.attempt1.jsonl").read_text()
        assert "second" in (log_dir / "t1.attempt2.jsonl").read_text()
