"""Main async orchestration loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from po import procs
from po.agent.launcher import AgentRunner, ClaudeCodeRunner
from po.agent.prompt_builder import build_prompt
from po.config import (
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_TURNS,
    STATUS_DECOMPOSED,
    STATUS_PENDING,
    TERMINAL_STATUSES,
    ensure_gitignore,
    ensure_logs_dir,
    escalate_model,
)
from po.db.queries import AgentResult, SqliteTaskStore
from po.orchestrator.merge import MergeResult, MergeStrategy, RebaseMerger
from po.verify import run_verification
from po.worktree.manager import GitWorktreeManager, WorktreeProvider, ensure_git_repo

logger = logging.getLogger(__name__)

# Event callback type: (event_name, task_id, extra_info)
EventCallback = Callable[[str, str, str], None]


class OrchestratorLoop:
    """Main orchestration loop that fans out tasks to agents."""

    def __init__(
        self,
        store: SqliteTaskStore,
        project_root: Path,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        worktree_manager: WorktreeProvider | None = None,
        agent_runner: AgentRunner | None = None,
        merger: MergeStrategy | None = None,
        global_context: str = "",
        global_context_files: list[str] | None = None,
        setup: str = "",
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
        self.setup = setup
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

    def _prepare_repo(self) -> None:
        """Seed and commit .gitignore before the first task branch is cut.

        Ordering is the whole point. A commit that lands on the base branch
        *after* a task branch was created, touching a file the agent also
        creates, is an add/add conflict at merge time — and `.gitignore` is
        exactly that file for any scaffolding task. Doing it here, before any
        `git worktree add`, means task branches inherit the file instead of
        racing to invent it.
        """
        ensure_git_repo(self.project_root)
        ensure_gitignore(self.project_root)
        # Check git rather than whether we just edited the file: `po plan` may
        # have written the patterns already, and an uncommitted .gitignore is
        # invisible to task branches, which is the case that bites.
        status = self._git(["status", "--porcelain", "--", ".gitignore"])
        if not status.stdout.strip():
            return
        self._git(["add", "--", ".gitignore"])
        # Pathspec form: commit only .gitignore, never whatever else is staged.
        self._git(
            [
                "commit",
                "-m",
                "Add .gitignore for po state and build artifacts",
                "--",
                ".gitignore",
            ]
        )

    def _git(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.project_root,
            capture_output=True,
            text=True,
        )

    async def run(self) -> None:
        """Run the orchestration loop until all tasks are terminal."""
        self._prepare_repo()

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
        """Handle shutdown signal — cancel running tasks and shut down.

        First signal: cancel all running tasks and kill every tracked child
        process. Cancelling the asyncio tasks is enough for the agents, whose
        subprocesses are awaited by those tasks — but not for a merge or a
        verification command, which block an executor thread that cancellation
        cannot reach. `procs.shutdown()` kills those directly; without it the
        first Ctrl-C looks ignored and the interpreter later hangs joining the
        executor on the way out of `asyncio.run()`.

        Second signal: kill whatever registered since (the merge's own cleanup
        commands), then force exit.
        """
        if self._shutting_down:
            # Second signal — force exit
            logger.info("Force shutdown requested")
            procs.shutdown()
            import os

            os._exit(1)
        else:
            logger.info(
                "Shutdown requested, cancelling %d running task(s)",
                len(self._running_tasks),
            )
            self._shutting_down = True
            for task in self._running_tasks.values():
                task.cancel()
            killed = procs.shutdown()
            if killed:
                logger.info("Killed %d in-flight subprocess group(s)", killed)
            self._emit("shutdown", "", f"{len(self._running_tasks)} task(s) cancelled")

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
                self._running_tasks[task_id] = asyncio.create_task(self._run_task(task_id))
                logger.debug(
                    "Launched task %s (slot %d/%d)",
                    task_id,
                    len(self._running_tasks),
                    self.max_concurrency,
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
            except asyncio.CancelledError:
                logger.info("Task %s was cancelled", task_id)
                if self._shutting_down:
                    continue  # Skip processing — we're shutting down
                result = AgentResult(
                    task_id=task_id,
                    success=False,
                    error_message="Task cancelled",
                )
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

        await self._run_setup(task_id, wt_info.path)

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

        # Run agent — escalate model on retries unless user overrode
        max_budget = task["max_budget_usd"]
        if self.model_override:
            model = self.model_override
        else:
            base_model = str(task["model"])
            # Re-read task to get current attempt (set_running incremented it)
            current_task = self.store.get_task(task_id) or task
            current_attempt = int(current_task["attempt"])
            model = escalate_model(base_model, current_attempt)
            if model != base_model:
                self._emit(
                    "model_escalated",
                    task_id,
                    f"{base_model} → {model}",
                )
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

    def _abandon_for_shutdown(self, task_id: str) -> None:
        """Park an interrupted task as pending so the next run picks it up.

        A shutdown is not the task's fault, so it must not be recorded as a
        failure or burn the merge's retry budget.
        """
        logger.info("Shutdown interrupted task %s, leaving it pending", task_id)
        self.store.set_status(task_id, STATUS_PENDING)

    async def _process_result(self, result: AgentResult) -> None:
        """Process an agent result: handle subtasks, merge, or fail."""
        task = self.store.get_task(result.task_id)
        if task is None:
            return

        if self._shutting_down:
            # Don't start a merge we are about to interrupt.
            self._abandon_for_shutdown(result.task_id)
            return

        if result.success:
            # Run verification in the worktree before merging so the agent
            # can retry with a clear error if verification fails.
            verification = str(task["verification"]) if task["verification"] else ""
            worktree_path = str(task["worktree_path"]) if task["worktree_path"] else ""
            if verification and worktree_path:
                preverify_fail = await self._run_preverify(
                    verification,
                    result.task_id,
                    Path(worktree_path),
                )
                if self._shutting_down:
                    self._abandon_for_shutdown(result.task_id)
                    return
                if preverify_fail is not None:
                    attempt = int(task["attempt"])
                    if attempt <= self.max_retries:
                        self.store.set_error_message(result.task_id, preverify_fail)
                        self.store.set_status(result.task_id, STATUS_PENDING)
                        self._emit(
                            "task_retrying",
                            result.task_id,
                            f"pre-merge verification failed, attempt {attempt}/{self.max_retries}",
                        )
                    else:
                        # The agent's commits are real work; keep the branch
                        # for inspection and for `po reset` to build on.
                        self.worktree_mgr.detach(result.task_id, self.project_root)
                        preverify_fail = _kept_branch(preverify_fail, task)
                        self.store.set_failed(
                            result.task_id,
                            error_message=preverify_fail,
                            cost_usd=result.cost_usd,
                            duration_ms=result.duration_ms,
                            session_id=result.session_id,
                            input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens,
                            num_turns=result.num_turns,
                        )
                        self._handle_failure(result.task_id)
                        self._emit("task_failed", result.task_id, preverify_fail)
                    return

            # Detach worktree before merge so rebase can check out the branch
            # (git refuses to check out a branch in another worktree).
            # Keep the branch — it's needed for the merge.
            self.worktree_mgr.detach(result.task_id, self.project_root)

            # Try to merge
            branch = str(task["branch_name"])
            verification = str(task["verification"]) if task["verification"] else ""
            merge_result: MergeResult = await self.merger.merge(
                branch=branch,
                task_id=result.task_id,
                verification=verification,
                project_root=self.project_root,
            )

            if self._shutting_down:
                # The merge was interrupted, not attempted and found wanting.
                self._abandon_for_shutdown(result.task_id)
                return

            if merge_result.success:
                # Branch is merged — clean up worktree and branch
                self.worktree_mgr.remove(result.task_id, self.project_root)
                self.store.set_completed(
                    result.task_id,
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                    agent_result=result.result_text,
                    session_id=result.session_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    num_turns=result.num_turns,
                )
                self._emit(
                    "task_completed",
                    result.task_id,
                    _format_result_detail(result),
                )
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
                        "task_retrying",
                        result.task_id,
                        f"merge failed, attempt {attempt}/{self.max_retries}",
                    )
                else:
                    # No retries left. The branch holds work that passed
                    # pre-merge verification, so it is kept, never deleted:
                    # `po clean` reaps it, `po reset` builds on it.
                    err = _kept_branch(err, task)
                    self.store.set_failed(
                        result.task_id,
                        error_message=err,
                        cost_usd=result.cost_usd,
                        duration_ms=result.duration_ms,
                        session_id=result.session_id,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        num_turns=result.num_turns,
                    )
                    self._handle_failure(result.task_id)
                    self._emit("task_failed", result.task_id, err)
        else:
            # Handle subtasks if the agent created them
            if result.subtasks:
                parent_task = self.store.get_task(result.task_id)
                parent_deps: list[str] = (
                    json.loads(parent_task["dependencies"])
                    if parent_task and isinstance(parent_task["dependencies"], str)
                    else []
                )
                for subtask in result.subtasks:
                    # Namespace subtask IDs under parent to avoid collisions
                    if not subtask.id.startswith(f"{result.task_id}/"):
                        subtask.id = f"{result.task_id}/{subtask.id}"
                    # Subtasks inherit parent's dependencies
                    subtask.dependencies = parent_deps
                    self.store.add_runtime_task(
                        subtask,
                        parent_task_id=result.task_id,
                    )
                # Mark parent as decomposed — don't retry it
                self.store.set_status(result.task_id, STATUS_DECOMPOSED)
                self.worktree_mgr.remove(result.task_id, self.project_root)
                self._emit(
                    "task_decomposed",
                    result.task_id,
                    f"{len(result.subtasks)} subtasks",
                )
                return

            # Check retry — if failure happened before set_running (e.g.
            # worktree creation failed), the attempt counter was never
            # incremented.  Detect this by checking if status is still pending.
            if task["status"] == STATUS_PENDING:
                self.store.increment_attempt(result.task_id)
                task = self.store.get_task(result.task_id) or task
            # The worktree and branch stay in place either way. An agent that
            # ran out of turns, memory or budget usually left real progress
            # behind, committed or not; the retry — or `po reset` + `po run`
            # — resumes from it, and `po clean` is the explicit way to discard.
            attempt = int(task["attempt"])
            if attempt <= self.max_retries:
                err = result.error_message or "Agent failed"
                self.store.set_error_message(result.task_id, err)
                self.store.set_status(result.task_id, STATUS_PENDING)
                self._emit(
                    "task_retrying",
                    result.task_id,
                    f"attempt {attempt}/{self.max_retries}",
                )
            else:
                err = _kept_branch(result.error_message or "Agent failed", task)
                self.store.set_failed(
                    result.task_id,
                    error_message=err,
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                    session_id=result.session_id,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    num_turns=result.num_turns,
                )
                self._handle_failure(result.task_id)
                self._emit("task_failed", result.task_id, err)

    async def _run_setup(self, task_id: str, worktree_path: Path) -> None:
        """Install the project's dependencies in a freshly created worktree.

        A worktree is a clean checkout, and dependency directories are gitignored
        (`node_modules/`, `.venv/`), so nothing installed in the project root is
        present here — every task starts with no dependencies at all. `npx tsc`
        in that state does not fail usefully: it downloads an unrelated `tsc`
        package from the registry and prints "This is not the tsc command you are
        looking for", which reaches the agent as a mystery about its own code.

        Best-effort by design: a failure is logged and reported but does not fail
        the task. The bootstrap task of any project — the one whose job is to
        write package.json — runs before a manifest exists, so `npm ci` there
        fails for the most ordinary reason there is, and hard-failing it would
        deadlock layer 0 of every from-scratch run. The gate that catches a
        genuinely broken toolchain is the task's own verification command, which
        runs against real work rather than against an empty checkout.

        Logs to .po/logs/setup-{task_id}.log.
        """
        if not self.setup:
            return

        logger.debug("Running setup for %s: %s", task_id, self.setup)
        log_file = ensure_logs_dir(self.project_root) / f"setup-{task_id}.log"
        outcome = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: run_verification(self.setup, worktree_path, log_file),
        )
        if outcome.ok or outcome.cancelled:
            return

        logger.warning(
            "Setup command failed for %s (continuing anyway): %s",
            task_id,
            outcome.detail,
        )
        self._emit("task_setup_failed", task_id, _first_line(outcome.detail))

    async def _run_preverify(
        self,
        verification: str,
        task_id: str,
        worktree_path: Path,
    ) -> str | None:
        """Run verification command in the worktree before merging.

        Returns None on success, or an error message string on failure.
        Logs output to .po/logs/preverify-{task_id}.log.
        """
        logger.debug(
            "Running pre-merge verification for %s: %s",
            task_id,
            verification,
        )
        log_file = ensure_logs_dir(self.project_root) / f"preverify-{task_id}.log"
        outcome = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: run_verification(verification, worktree_path, log_file),
        )
        if outcome.ok:
            return None

        logger.warning("Pre-merge verification failed for %s", task_id)
        return f"Pre-merge verification failed (cmd: {verification}): {outcome.detail}"

    def _handle_failure(self, task_id: str) -> None:
        """Cancel dependents of a failed task."""
        cancelled_count = self.store.cancel_dependents(task_id)
        if cancelled_count > 0:
            self._emit(
                "dependents_cancelled",
                task_id,
                f"{cancelled_count} task(s)",
            )


def _kept_branch(err: str, task: dict[str, object]) -> str:
    """Append the kept branch name to a final-failure message."""
    branch = task.get("branch_name")
    return f"{err} [branch {branch} kept]" if branch else err


def _first_line(text: str) -> str:
    """First non-empty line, for a one-line event detail."""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _format_result_detail(result: AgentResult) -> str:
    """Format agent result as a compact detail string for event callbacks.

    Example: "12.5k in / 3.2k out / 25 turns"
    """
    parts: list[str] = []
    if result.input_tokens:
        parts.append(_format_tokens(result.input_tokens) + " in")
    if result.output_tokens:
        parts.append(_format_tokens(result.output_tokens) + " out")
    if result.num_turns:
        parts.append(f"{result.num_turns} turns")
    return " / ".join(parts)


def _format_tokens(n: int) -> str:
    """Format token count compactly: 1234 → '1.2k', 1234567 → '1.2M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
