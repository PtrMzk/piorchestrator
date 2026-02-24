"""Agent launcher — runs Claude Code CLI as a subprocess.

This module is the single abstraction over how agents are invoked.
If the Agent SDK gains Max OAuth support, only this module changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Protocol

from po.config import DEFAULT_MAX_TURNS, FAILURE_FILE, SUBTASKS_FILE, logs_dir
from po.db.queries import AgentResult
from po.spec.schema import TaskSpec

logger = logging.getLogger(__name__)


class AgentRunner(Protocol):
    """Protocol for running agents."""

    async def run(
        self,
        task_id: str,
        prompt: str,
        worktree_path: Path,
        model: str,
        max_turns: int,
        project_root: Path,
        max_budget_usd: float | None = None,
    ) -> AgentResult: ...


class ClaudeCodeRunner:
    """Run Claude Code CLI via subprocess."""

    async def run(
        self,
        task_id: str,
        prompt: str,
        worktree_path: Path,
        model: str,
        max_turns: int = DEFAULT_MAX_TURNS,
        project_root: Path = Path("."),
        max_budget_usd: float | None = None,
    ) -> AgentResult:
        """Run a Claude Code agent and return the result."""
        start_time = time.monotonic()

        # Prepare log file
        log_dir = logs_dir(project_root)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_id}.jsonl"

        # Build environment — strip CLAUDE_CODE vars to avoid nesting issues
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}

        cmd = [
            "claude",
            "-p", prompt,
            "--verbose",
            "--output-format", "stream-json",
            "--model", model,
            "--max-turns", str(max_turns),
        ]

        if max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(max_budget_usd)])

        logger.debug("Running agent for %s: %s", task_id, " ".join(cmd[:6]))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
        except FileNotFoundError:
            return AgentResult(
                task_id=task_id,
                success=False,
                error_message="Claude CLI not found. Ensure 'claude' is on PATH.",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Write raw log
        if stdout_bytes:
            log_file.write_bytes(stdout_bytes)

        # Parse the stream-json output (last line with result)
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        cost_usd: float | None = None
        result_text: str | None = None
        session_id: str | None = None

        # stream-json emits one JSON object per line
        for line in stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "result":
                result_text = msg.get("result", "")
                cost_usd = msg.get("cost_usd")
                session_id = msg.get("session_id")

        success = proc.returncode == 0
        logger.debug(
            "Agent %s exited with code %d (%.1fs)",
            task_id, proc.returncode, duration_ms / 1000,
        )

        # Check for subtasks file
        subtasks: list[TaskSpec] | None = None
        subtasks_path = worktree_path / SUBTASKS_FILE
        if subtasks_path.exists():
            try:
                subtask_data = json.loads(subtasks_path.read_text())
                subtasks = [TaskSpec.from_dict(s) for s in subtask_data]
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

        # Check for failure file
        error_message: str | None = None
        failure_path = worktree_path / FAILURE_FILE
        if failure_path.exists():
            try:
                failure_data = json.loads(failure_path.read_text())
                error_message = failure_data.get("reason", "Unknown failure")
                success = False
            except (json.JSONDecodeError, KeyError, TypeError):
                error_message = (
                    "Agent reported failure "
                    "(could not parse .po-failure.json)"
                )
                success = False

        if not success and error_message is None:
            stderr = stderr_bytes.decode("utf-8", errors="replace")
            error_message = stderr or f"Agent exited with code {proc.returncode}"

        return AgentResult(
            task_id=task_id,
            success=success,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            result_text=result_text,
            error_message=error_message,
            subtasks=subtasks,
            session_id=session_id,
        )
