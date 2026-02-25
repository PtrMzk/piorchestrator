"""Main async orchestration loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from collections.abc import Callable
from pathlib import Path

from po.agent.launcher import AgentRunner, ClaudeCodeRunner
from po.agent.prompt_builder import build_prompt
from po.config import (
    DEFAULT_MAX_TURNS,
    STATUS_DECOMPOSED,
    STATUS_PENDING,
    TERMINAL_STATUSES,
)
from po.db.queries import AgentResult, SqliteTaskStore
from po.orchestrator.merge import MergeResult, MergeStrategy, RebaseMerger
from po.worktree.manager import GitWorktreeManager, WorktreeProvider

logger = logging.getLogger(__name__)

# Event callback type: (event_name, task_id, extra_info)
EventCallback = Callable[[str, str, str], None]


class OrchestratorLoop:
    """Main orchestration loop that fans out tasks to agents."""

    def __init__(
        self,
        store: SqliteTaskStore,
        project_root: Path,
        max_concurrency: int = 3,
        worktree_manager: WorktreeProvider | None = None,
        agent_runner: AgentRunner | None = None,
        merger: MergeStrategy | None = None,
        global_context: str = "",
        global_context_files: list[str] | None = None,
        max_retries: int = 1,
        on_event: EventCallback | None = None,
        model_override: str | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.store = store
        self.project_root = project_root
        self.max_concurrency = max_concurrency
        self.worktree_mgr = worktree_manager or GitWorktreeManager()
        self.agent_runner = agent_runner or ClaudeCodeRunner()
        self.merger = merger or RebaseMerger()
        self.global_context = global_context
        self.global_context_files = global_context_files or []
        self.max_retries = max_retries
        self._on_event = on_event
        self.model_override = model_override
        self.max_turns = max_turns

        self._running_tasks: dict[str, asyncio.Task[AgentResult]] = {}
        self._shutting_down = False

    def _emit(self, event: str, task_id: str, detail: str = "") -> None:
        """Fire an event callback if one is registered."""
        if self._on_event is not None:
            self._on_event(event, task_id, detail)

    async def run(self) -> None:
        """Run the orchestration loop until all tasks are terminal."""
        # Install signal handlers for graceful shutdown
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_shutdown)

        try:
            await self._loop()
        finally:
            # Restore default signal handlers
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)

    def _request_shutdown(self) -> None:
        """Handle shutdown signal — let current tasks finish but don't launch new ones."""
        logger.info("Shutdown requested, finishing %d running task(s)", len(self._running_tasks))
        self._shutting_down = True

    async def _loop(self) -> None:
        """Core loop: find ready tasks, launch agents, collect results, merge."""
        while True:
            if self._shutting_down:
                # Wait for running tasks to finish and collect their results
                if self._running_tasks:
                    await asyncio.gather(*self._running_tasks.values(), return_exceptions=True)
                    await self._collect_completed()
                break

            # Collect completed agents
            await self._collect_completed()

            # Check if all tasks are terminal
            all_tasks = self.store.get_all_tasks()
            non_terminal = [t for t in all_tasks if t["status"] not in TERMINAL_STATUSES]
            if not non_terminal:
                break

            # Get ready tasks
            ready_ids = self.store.get_ready_task_ids()

            # Filter out tasks with output file overlap with running tasks
            ready_ids = self._filter_output_overlap(ready_ids)

            # Launch agents up to concurrency limit, tracking output files
            # claimed in this batch to prevent overlap within the same iteration
            slots = self.max_concurrency - len(self._running_tasks)
            batch_outputs: set[str] = set()
            launched = 0
            for task_id in ready_ids:
                if launched >= slots:
                    break
                if task_id in self._running_tasks:
                    continue
                task = self.store.get_task(task_id)
                if task:
                    files: list[str] = (
                        json.loads(task["output_files"])
                        if isinstance(task["output_files"], str)
                        else task["output_files"]
                    )
                    if batch_outputs.intersection(files):
                        continue  # Skip — overlaps with this batch
                    batch_outputs.update(files)
                self._running_tasks[task_id] = asyncio.create_task(
                    self._run_task(task_id)
                )
                logger.debug(
                    "Launched task %s (slot %d/%d)",
                    task_id, len(self._running_tasks), self.max_concurrency,
                )
                self._emit("task_launched", task_id)
                launched += 1

            # Deadlock detection
            running_ids = self.store.get_running_task_ids()
            if not running_ids and not ready_ids and non_terminal:
                pending = [t for t in non_terminal if t["status"] == STATUS_PENDING]
                if pending:
                    raise RuntimeError(
                        f"Deadlock: {len(pending)} pending tasks have unsatisfiable dependencies. "
                        f"IDs: {[t['id'] for t in pending]}"
                    )
                break

            # Wait for at least one task to complete or timeout
            if self._running_tasks:
                done, _ = await asyncio.wait(
                    self._running_tasks.values(),
                    timeout=5.0,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(1)

    def _filter_output_overlap(self, ready_ids: list[str]) -> list[str]:
        """Remove tasks whose output_files overlap with running tasks."""
        running_outputs: set[str] = set()
        for task_id in self._running_tasks:
            task = self.store.get_task(task_id)
            if task:
                files: list[str] = (
                    json.loads(task["output_files"])
                    if isinstance(task["output_files"], str)
                    else task["output_files"]
                )
                running_outputs.update(files)

        if not running_outputs:
            return ready_ids

        filtered = []
        for task_id in ready_ids:
            task = self.store.get_task(task_id)
            if task:
                candidate_files: list[str] = (
                    json.loads(task["output_files"])
                    if isinstance(task["output_files"], str)
                    else task["output_files"]
                )
                if not running_outputs.intersection(candidate_files):
                    filtered.append(task_id)
        return filtered

    async def _collect_completed(self) -> None:
        """Check for completed agent tasks and process results."""
        completed_ids = []
        for task_id, async_task in self._running_tasks.items():
            if async_task.done():
                completed_ids.append(task_id)

        for task_id in completed_ids:
            async_task = self._running_tasks.pop(task_id)
            try:
                result = async_task.result()
            except Exception as e:
                logger.warning("Task %s raised exception: %s", task_id, e)
                result = AgentResult(
                    task_id=task_id,
                    success=False,
                    error_message=str(e),
                )
            await self._process_result(result)

    async def _run_task(self, task_id: str) -> AgentResult:
        """Set up worktree, build prompt, run agent for a task."""
        task = self.store.get_task(task_id)
        if task is None:
            return AgentResult(task_id=task_id, success=False, error_message="Task not found")

        # Create worktree
        logger.debug("Creating worktree for task %s", task_id)
        wt_info = self.worktree_mgr.create(task_id, self.project_root)

        # Mark as running
        self.store.set_running(task_id, str(wt_info.path), wt_info.branch)

        # Read context files — prefer worktree copy (may have partial
        # work from a previous attempt), fall back to project root.
        context_content: dict[str, str] = {}
        context_files: list[str] = (
            json.loads(task["context_files"])
            if isinstance(task["context_files"], str)
            else task["context_files"]
        )
        for cf in context_files:
            wt_filepath = wt_info.path / cf
            root_filepath = self.project_root / cf
            filepath = wt_filepath if wt_filepath.exists() else root_filepath
            if filepath.exists():
                with contextlib.suppress(Exception):
                    context_content[cf] = filepath.read_text(encoding="utf-8")
        for cf in self.global_context_files:
            if cf not in context_content:
                wt_filepath = wt_info.path / cf
                root_filepath = self.project_root / cf
                filepath = wt_filepath if wt_filepath.exists() else root_filepath
                if filepath.exists():
                    with contextlib.suppress(Exception):
                        context_content[cf] = filepath.read_text(encoding="utf-8")

        output_files: list[str] = (
            json.loads(task["output_files"])
            if isinstance(task["output_files"], str)
            else task["output_files"]
        )

        # Build prompt
        previous_error = str(task["error_message"]) if task["error_message"] else ""
        prompt = build_prompt(
            task_id=task_id,
            description=str(task["description"]),
            global_context=self.global_context,
            context_files_content=context_content,
            verification=str(task["verification"]) if task["verification"] else "",
            output_files=output_files,
            previous_error=previous_error,
        )

        # Run agent
        max_budget = task["max_budget_usd"]
        model = self.model_override or str(task["model"])
        result = await self.agent_runner.run(
            task_id=task_id,
            prompt=prompt,
            worktree_path=wt_info.path,
            model=model,
            max_turns=self.max_turns,
            project_root=self.project_root,
            max_budget_usd=float(max_budget) if max_budget is not None else None,
        )

        return result

    async def _process_result(self, result: AgentResult) -> None:
        """Process an agent result: handle subtasks, merge, or fail."""
        task = self.store.get_task(result.task_id)
        if task is None:
            return

        if result.success:
            # Detach worktree before merge so rebase can check out the branch
            # (git refuses to check out a branch in another worktree).
            # Keep the branch — it's needed for the merge.
            self.worktree_mgr.detach(result.task_id, self.project_root)

            # Try to merge
            branch = str(task["branch_name"])
            verification = (
                str(task["verification"]) if task["verification"] else ""
            )
            merge_result: MergeResult = await self.merger.merge(
                branch=branch,
                task_id=result.task_id,
                verification=verification,
                project_root=self.project_root,
            )

            if merge_result.success:
                # Branch is merged — clean up worktree and branch
                self.worktree_mgr.remove(result.task_id, self.project_root)
                self.store.set_completed(
                    result.task_id,
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                    agent_result=result.result_text,
                    session_id=result.session_id,
                )
                cost = f"${result.cost_usd:.4f}" if result.cost_usd else ""
                self._emit("task_completed", result.task_id, cost)
            else:
                err = merge_result.error_message or "Merge failed"
                # Check if we can retry — attempt was already incremented
                # by set_running, so read it directly.
                attempt = int(task["attempt"])
                if attempt <= self.max_retries:
                    # Keep the branch so retry agent builds on previous work
                    # (detach already removed the worktree dir)
                    self.store.set_error_message(result.task_id, err)
                    self.store.set_status(result.task_id, STATUS_PENDING)
                    self._emit(
                        "task_retrying", result.task_id,
                        f"merge failed, attempt {attempt}/{self.max_retries}",
                    )
                else:
                    # No retries left — clean up branch
                    self.worktree_mgr.remove(result.task_id, self.project_root)
                    self.store.set_failed(
                        result.task_id,
                        error_message=err,
                        cost_usd=result.cost_usd,
                        duration_ms=result.duration_ms,
                        session_id=result.session_id,
                    )
                    self._handle_failure(result.task_id)
                    self._emit("task_failed", result.task_id, err)
        else:
            # Handle subtasks if the agent created them
            if result.subtasks:
                parent_task = self.store.get_task(result.task_id)
                parent_deps: list[str] = (
                    json.loads(parent_task["dependencies"])
                    if parent_task
                    and isinstance(parent_task["dependencies"], str)
                    else []
                )
                for subtask in result.subtasks:
                    # Namespace subtask IDs under parent to avoid collisions
                    if not subtask.id.startswith(f"{result.task_id}/"):
                        subtask.id = f"{result.task_id}/{subtask.id}"
                    # Subtasks inherit parent's dependencies
                    subtask.dependencies = parent_deps
                    self.store.add_runtime_task(
                        subtask, parent_task_id=result.task_id,
                    )
                # Mark parent as decomposed — don't retry it
                self.store.set_status(result.task_id, STATUS_DECOMPOSED)
                self.worktree_mgr.remove(result.task_id, self.project_root)
                self._emit(
                    "task_decomposed", result.task_id,
                    f"{len(result.subtasks)} subtasks",
                )
                return

            # Check retry — if failure happened before set_running (e.g.
            # worktree creation failed), the attempt counter was never
            # incremented.  Detect this by checking if status is still pending.
            if task["status"] == STATUS_PENDING:
                self.store.increment_attempt(result.task_id)
                task = self.store.get_task(result.task_id) or task
            attempt = int(task["attempt"])
            if attempt <= self.max_retries:
                self.store.set_status(result.task_id, STATUS_PENDING)
                # Clean up worktree for retry
                self.worktree_mgr.remove(result.task_id, self.project_root)
                self._emit(
                    "task_retrying", result.task_id,
                    f"attempt {attempt}/{self.max_retries}",
                )
            else:
                err = result.error_message or "Agent failed"
                self.store.set_failed(
                    result.task_id,
                    error_message=err,
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                    session_id=result.session_id,
                )
                self._handle_failure(result.task_id)
                self._emit("task_failed", result.task_id, err)

    def _handle_failure(self, task_id: str) -> None:
        """Cancel dependents of a failed task."""
        cancelled_count = self.store.cancel_dependents(task_id)
        if cancelled_count > 0:
            self._emit(
                "dependents_cancelled", task_id,
                f"{cancelled_count} task(s)",
            )
