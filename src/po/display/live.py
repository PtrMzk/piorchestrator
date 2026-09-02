"""Rich Live terminal display for orchestration progress."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.live import Live
from rich.text import Text
from rich.tree import Tree

from po.config import logs_dir
from po.db.queries import SqliteTaskStore
from po.display.tools import tool_summary

# Status → (symbol, style)
_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "pending": ("○", "dim"),
    "running": ("◉", "bold cyan"),
    "completed": ("✓", "green"),
    "failed": ("✗", "bold red"),
    "cancelled": ("⊘", "dim strike"),
    "decomposed": ("◈", "blue"),
}


def _attempt_summary(task: dict[str, Any]) -> str:
    """'(attempt 2, sonnet → opus)' — how many tries a task got and on what model.

    Without this a failed line looks like a single shot; the retry and the
    escalated model that ran are otherwise only visible in scrolled-off events.
    """
    attempt = int(task.get("attempt") or 0)
    parts = [f"attempt {attempt}" if attempt else "attempt ?"]
    model = task.get("model")
    if model:
        parts.append(str(model))
    return f"({', '.join(parts)})"


class LiveDisplay:
    """Rich Live display that acts as an EventCallback (event, task_id, detail) -> None."""

    def __init__(self, store: SqliteTaskStore, project_root: Path) -> None:
        self._store = store
        self._project_root = project_root
        self._log_dir = logs_dir(project_root)
        self._live: Live | None = None

        # Internal state: task_id → {status, cost, error, description}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._load_initial_state()

    def _load_initial_state(self) -> None:
        """Populate internal state from the store."""
        for task in self._store.get_all_tasks():
            tid = str(task["id"])
            deps_raw = task.get("dependencies", "[]")
            deps = json.loads(deps_raw) if isinstance(deps_raw, str) else deps_raw
            self._tasks[tid] = {
                "status": task["status"],
                "description": str(task["description"]),
                "cost_usd": task.get("cost_usd"),
                "error_message": task.get("error_message"),
                "dependencies": deps,
                "attempt": int(task.get("attempt") or 0),
            }

    def start(self) -> None:
        """Enter the Rich Live context."""
        self._live = Live(
            self._build_tree(),
            refresh_per_second=4,
            console=None,
        )
        self._live.start()

    def stop(self) -> None:
        """Exit the Rich Live context."""
        if self._live is not None:
            try:
                # Final render with latest state
                self._live.update(self._build_tree())
                self._live.stop()
            except Exception:
                # Rich can crash during stop (e.g. unicode data import
                # failures on Python 3.13). Since this is cleanup code,
                # swallow the error and ensure _live is cleared.
                pass
            self._live = None

    def __call__(self, event: str, task_id: str, detail: str) -> None:
        """Handle an orchestration event — implements EventCallback signature."""
        task_state = self._tasks.get(task_id)
        if task_state is None:
            task_state = {
                "status": "pending",
                "description": "",
                "cost_usd": None,
                "error_message": None,
                "dependencies": [],
            }
            self._tasks[task_id] = task_state

        if event == "task_launched":
            task_state["status"] = "running"
            task_state["attempt"] = int(task_state.get("attempt") or 0) + 1
        elif event == "model_escalated":
            task_state["model"] = detail
        elif event == "task_completed":
            task_state["status"] = "completed"
            if detail:
                task_state["token_summary"] = detail
        elif event == "task_failed":
            task_state["status"] = "failed"
            if detail:
                task_state["error_message"] = detail
        elif event == "task_retrying":
            task_state["status"] = "running"
        elif event == "task_decomposed":
            task_state["status"] = "decomposed"
        elif event == "task_cancelled":
            task_state["status"] = "cancelled"

        # Live auto-refresh handles screen updates via _build_tree
        if self._live is not None:
            self._live.update(self._build_tree())

    def _build_tree(self) -> Tree:
        """Build a Rich Tree representing all tasks grouped by dependency layer."""
        # Progress summary
        counts: dict[str, int] = {}
        for t in self._tasks.values():
            s = t["status"]
            counts[s] = counts.get(s, 0) + 1
        total = len(self._tasks)
        completed = counts.get("completed", 0)
        running = counts.get("running", 0)

        root_label = Text.assemble(
            ("PO", "bold magenta"),
            f"  {completed}/{total} done",
            f"  {running} running",
        )
        tree = Tree(root_label)

        # Separate top-level tasks from subtasks (subtasks have "/" in id)
        top_level: list[str] = []
        children: dict[str, list[str]] = {}  # parent_id → [child_ids]

        for tid in sorted(self._tasks.keys()):
            if "/" in tid:
                parent = tid.rsplit("/", 1)[0]
                children.setdefault(parent, []).append(tid)
            else:
                top_level.append(tid)

        # Compute BFS layers for top-level tasks
        top_set = set(top_level)
        remaining = set(top_level)
        layer_done: set[str] = set()
        layers: list[list[str]] = []
        fallback: list[str] = []

        while remaining:
            layer: list[str] = []
            for tid in sorted(remaining):
                deps = self._tasks[tid].get("dependencies", [])
                # Only consider deps that are in top_set (ignore unknown)
                relevant_deps = [d for d in deps if d in top_set]
                if all(d in layer_done for d in relevant_deps):
                    layer.append(tid)
            if not layer:
                # Remaining tasks have unresolvable deps — put in fallback
                fallback = sorted(remaining)
                break
            for tid in layer:
                remaining.remove(tid)
                layer_done.add(tid)
            layers.append(layer)

        # Render each layer as a branch
        for i, layer_tasks in enumerate(layers):
            layer_branch = tree.add(Text(f"Layer {i}", style="bold"))
            for tid in layer_tasks:
                node = self._add_task_node(layer_branch, tid)
                for child_id in children.get(tid, []):
                    self._add_task_node(node, child_id)

        # Fallback group for unresolvable tasks
        if fallback:
            fallback_branch = tree.add(Text("Unresolved", style="bold dim"))
            for tid in fallback:
                node = self._add_task_node(fallback_branch, tid)
                for child_id in children.get(tid, []):
                    self._add_task_node(node, child_id)

        # Orphan subtasks whose parent isn't in top_level
        for parent_id, child_ids in children.items():
            if parent_id not in self._tasks or "/" in parent_id:
                for child_id in child_ids:
                    self._add_task_node(tree, child_id)

        return tree

    def _add_task_node(self, parent: Tree, task_id: str) -> Tree:
        """Add a single task node to the tree."""
        task = self._tasks[task_id]
        status = task["status"]
        symbol, style = _STATUS_STYLES.get(status, ("?", ""))

        # Build the label
        label = Text()
        label.append(f"{symbol} ", style=style)

        # Show short display id (just the leaf part for subtasks)
        display_id = task_id.rsplit("/", 1)[-1] if "/" in task_id else task_id
        label.append(display_id, style=style)

        # Extra info based on status
        if status == "running":
            action, ts, tokens = self._read_last_action(task_id)
            label.append(f"  {action}", style="dim")
            if tokens:
                label.append(f"  [{tokens}]", style="dim yellow")
            if ts:
                label.append(f"  ({ts})", style="dim italic")
        elif status == "completed":
            token_summary = task.get("token_summary")
            if token_summary:
                label.append(f"  {token_summary}", style="dim green")
            else:
                cost = task.get("cost_usd")
                if cost is not None:
                    label.append(f"  ${float(cost):.4f}", style="dim green")
        elif status == "failed":
            error = task.get("error_message")
            if error:
                truncated = str(error)[:60]
                label.append(f"  {truncated}", style="dim red")
            label.append(f"  {_attempt_summary(task)}", style="dim")

        return parent.add(label)

    def _read_last_action(self, task_id: str) -> tuple[str, str, str]:
        """Read the last agent action and token usage from the task's JSONL log.

        Returns (action_text, relative_time, token_info) where:
        - relative_time is e.g. "5s ago"
        - token_info is e.g. "1.2k out" (output tokens so far)
        """
        log_file = self._log_dir / f"{task_id}.jsonl"
        if not log_file.exists():
            return "starting...", "", ""

        try:
            # Read last 32KB of the file (tool_result blocks can be large)
            size = log_file.stat().st_size
            read_size = min(size, 32768)
            with open(log_file, "rb") as f:
                if size > read_size:
                    f.seek(size - read_size)
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            return "starting...", "", ""

        # Parse lines backwards looking for the most recent assistant message
        # and accumulate token usage from the chunk
        lines = chunk.strip().splitlines()
        total_output_tokens = 0
        action: str | None = None
        action_ts = ""

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "assistant":
                # Accumulate output tokens from all assistant messages in chunk
                usage = msg.get("message", {}).get("usage", {})
                total_output_tokens += usage.get("output_tokens", 0)

                # Only capture the action from the first (most recent) one
                if action is None:
                    action_ts = _format_relative_time(msg.get("timestamp"))
                    content = msg.get("message", {}).get("content")
                    if isinstance(content, list):
                        for block in reversed(content):
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                action = tool_summary(block)
                                break
                        if action is None:
                            for block in reversed(content):
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text = block.get("text", "")
                                    action = text[:60] if text else "..."
                                    break
                    elif isinstance(content, str) and content:
                        action = content[:60]

        token_info = ""
        if total_output_tokens > 0:
            if total_output_tokens >= 1000:
                token_info = f"{total_output_tokens / 1000:.1f}k out"
            else:
                token_info = f"{total_output_tokens} out"

        return action or "working...", action_ts, token_info


def _format_relative_time(timestamp: str | None) -> str:
    """Format an ISO timestamp as a relative time string like '5s ago'."""
    if not timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(timestamp)
        delta = datetime.now(UTC) - dt
        secs = int(delta.total_seconds())
        if secs < 0:
            return ""
        if secs < 60:
            return f"{secs}s ago"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        return f"{hours}h ago"
    except (ValueError, TypeError):
        return ""
