"""Agent launcher — runs Claude Code CLI as a subprocess.

This module is the single abstraction over how agents are invoked.
If the Agent SDK gains Max OAuth support, only this module changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from po import procs
from po.config import (
    DEFAULT_MAX_TURNS,
    FAILURE_FILE,
    SUBTASKS_FILE,
    agent_env,
    ensure_logs_dir,
)
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


def _rotate_log(log_file: Path) -> None:
    """Move a previous attempt's log aside instead of overwriting it.

    Retries would otherwise erase the only evidence of why the first attempt
    failed. The current attempt always writes ``<task>.jsonl``; earlier ones
    are kept as ``<task>.attempt1.jsonl``, ``<task>.attempt2.jsonl``, ...
    """
    if not log_file.exists():
        return
    n = 1
    while (rotated := log_file.with_name(f"{log_file.stem}.attempt{n}.jsonl")).exists():
        n += 1
    log_file.rename(rotated)


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
        log_dir = ensure_logs_dir(project_root)
        log_file = log_dir / f"{task_id}.jsonl"
        _rotate_log(log_file)

        # Drop nesting markers only — auth vars must survive (see agent_env)
        env = agent_env()

        cmd = [
            "claude",
            "-p",
            prompt,
            "--verbose",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            "bypassPermissions",
        ]

        if max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(max_budget_usd)])

        logger.debug("Running agent for %s: %s", task_id, " ".join(cmd[:6]))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(worktree_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=16 * 1024 * 1024,  # 16 MB — stream-json lines can be large
                # Own process group. The agent spawns its own children (every
                # Bash tool call), and signalling only `claude` leaves those
                # running — a `npm run dev` it started outlives the shutdown.
                start_new_session=True,
            )
        except FileNotFoundError:
            return AgentResult(
                task_id=task_id,
                success=False,
                error_message="Claude CLI not found. Ensure 'claude' is on PATH.",
                duration_ms=int((time.monotonic() - start_time) * 1000),
            )

        # Stream stdout to log file in real-time while parsing result fields
        cost_usd: float | None = None
        result_text: str | None = None
        session_id: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        num_turns: int | None = None
        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            async for chunk in proc.stderr:
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_drain_stderr())
        # Also reachable from the signal handler, in case a second Ctrl-C
        # force-exits before this task's cancellation is processed.
        procs.register_pid(proc.pid)

        try:
            assert proc.stdout is not None
            with open(log_file, "wb") as fh:
                async for raw_line in proc.stdout:
                    # Inject a timestamp into each JSON line
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        fh.write(raw_line)
                        fh.flush()
                        continue

                    msg["timestamp"] = datetime.now(UTC).isoformat()
                    fh.write(json.dumps(msg).encode())
                    fh.write(b"\n")
                    fh.flush()

                    # Parse for result metadata
                    if msg.get("type") == "result":
                        result_text = msg.get("result", "")
                        cost_usd = msg.get("total_cost_usd") or msg.get("cost_usd")
                        session_id = msg.get("session_id")
                        num_turns = msg.get("num_turns")
                        # Token usage from the aggregate usage block
                        usage = msg.get("usage", {})
                        input_tokens = (
                            usage.get("input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                        ) or None
                        output_tokens = usage.get("output_tokens") or None

            await stderr_task
            await proc.wait()
        except asyncio.CancelledError:
            # Shutdown requested — tear down the agent and everything it spawned
            logger.info("Terminating agent subprocess for task %s", task_id)
            if procs.signal_group(proc.pid, signal.SIGTERM):
                # Give it a moment to exit cleanly, then kill
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    procs.signal_group(proc.pid, signal.SIGKILL)
                    await proc.wait()
            stderr_task.cancel()
            raise
        finally:
            procs.unregister_pid(proc.pid)

        stderr_bytes = b"".join(stderr_chunks)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        success = proc.returncode == 0
        logger.debug(
            "Agent %s exited with code %d (%.1fs)",
            task_id,
            proc.returncode,
            duration_ms / 1000,
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
                error_message = "Agent reported failure (could not parse .po-failure.json)"
                success = False

        if not success and error_message is None:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
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
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            num_turns=num_turns,
        )
