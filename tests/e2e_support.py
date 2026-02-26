"""Support module for e2e tests — ScriptedAgentRunner and AgentScript."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from po.config import logs_dir
from po.db.queries import AgentResult
from po.spec.schema import TaskSpec


@dataclass
class AgentScript:
    """Defines one agent invocation (one attempt) for scripted testing.

    Each script describes what the agent "does" in its worktree: which files
    to write, whether to commit them, and what result to return.
    """

    files: dict[str, str] = field(default_factory=dict)
    commit: bool = True
    commit_message: str = "Agent work"
    subtasks: list[dict[str, Any]] | None = None
    failure_reason: str | None = None
    cost_usd: float = 0.01
    result_text: str = "Done"
    session_id: str = "mock-session"
    log_entries: list[dict[str, Any]] | None = None


class ScriptedAgentRunner:
    """AgentRunner that writes real files and makes real git commits.

    Uses pre-defined scripts to simulate agent behavior without
    invoking the Claude CLI subprocess.  Scripts are consumed in order
    per task_id; if none remain, a default success script is used.
    """

    def __init__(self, scripts: dict[str, list[AgentScript]] | None = None) -> None:
        self.scripts: dict[str, list[AgentScript]] = scripts or {}
        self.calls: list[dict[str, Any]] = []

    async def run(
        self,
        task_id: str,
        prompt: str,
        worktree_path: Path,
        model: str,
        max_turns: int = 50,
        project_root: Path = Path("."),
        max_budget_usd: float | None = None,
    ) -> AgentResult:
        """Execute the next script for this task_id."""
        self.calls.append({
            "task_id": task_id,
            "prompt": prompt,
            "worktree_path": worktree_path,
            "model": model,
        })

        script = self._pop_script(task_id)

        # Write files to worktree
        for rel_path, content in script.files.items():
            file_path = worktree_path / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        # Git add + commit if requested and there are files
        if script.commit and script.files:
            subprocess.run(
                ["git", "add", "."],
                cwd=worktree_path,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-m", script.commit_message],
                cwd=worktree_path,
                capture_output=True,
                check=True,
            )

        # Write JSONL log
        self._write_log(task_id, script, project_root)

        # Return appropriate result
        if script.failure_reason:
            return AgentResult(
                task_id=task_id,
                success=False,
                cost_usd=script.cost_usd,
                duration_ms=100,
                error_message=script.failure_reason,
                session_id=script.session_id,
            )

        if script.subtasks is not None:
            subtask_specs = [TaskSpec.from_dict(s) for s in script.subtasks]
            return AgentResult(
                task_id=task_id,
                success=False,
                cost_usd=script.cost_usd,
                duration_ms=100,
                result_text=script.result_text,
                subtasks=subtask_specs,
                session_id=script.session_id,
            )

        return AgentResult(
            task_id=task_id,
            success=True,
            cost_usd=script.cost_usd,
            duration_ms=100,
            result_text=script.result_text,
            session_id=script.session_id,
        )

    def _pop_script(self, task_id: str) -> AgentScript:
        """Get the next script for a task, falling back to a default."""
        if task_id in self.scripts and self.scripts[task_id]:
            return self.scripts[task_id].pop(0)
        return AgentScript()

    def _write_log(
        self, task_id: str, script: AgentScript, project_root: Path,
    ) -> None:
        """Write JSONL log entries for this task."""
        log_file = logs_dir(project_root) / f"{task_id}.jsonl"
        log_file.parent.mkdir(parents=True, exist_ok=True)

        entries = script.log_entries or [
            {"type": "assistant", "message": f"Working on {task_id}"},
            {"type": "result", "result": script.result_text, "cost_usd": script.cost_usd},
        ]

        with open(log_file, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
