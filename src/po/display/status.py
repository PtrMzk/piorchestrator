"""Terminal output for status display — plain text, no dependencies."""

from __future__ import annotations

from typing import Any

STATUS_SYMBOLS = {
    "pending": "○",
    "running": "◉",
    "completed": "✓",
    "failed": "✗",
    "cancelled": "⊘",
    "decomposed": "◈",
}


def format_status_table(tasks: list[dict[str, Any]]) -> str:
    """Format tasks as a status table with error/cost details."""
    if not tasks:
        return "No tasks found."

    lines: list[str] = []
    header = f"{'Status':<12} {'ID':<25} {'Cost':<10} {'Description':<40}"
    lines.append(header)
    lines.append("─" * 87)

    for task in tasks:
        status = str(task["status"])
        symbol = STATUS_SYMBOLS.get(status, "?")
        task_id = str(task["id"])
        cost = task.get("cost_usd")
        cost_str = f"${cost:.4f}" if cost is not None else ""
        desc = str(task["description"])[:38]
        lines.append(f"{symbol} {status:<10} {task_id:<25} {cost_str:<10} {desc}")
        # Show error message for failed tasks
        error = task.get("error_message")
        if error and status == "failed":
            err_trunc = str(error)[:72]
            lines.append(f"{'':>14}└ {err_trunc}")

    return "\n".join(lines)


def format_execution_plan(layers: list[list[str]], tasks: list[dict[str, Any]]) -> str:
    """Format an execution plan showing layers."""
    task_map = {str(t["id"]): t for t in tasks}
    lines: list[str] = []
    lines.append("Execution Plan")
    lines.append("═" * 60)

    for i, layer in enumerate(layers):
        lines.append(f"\nLayer {i} (parallel):")
        for task_id in layer:
            task = task_map.get(task_id)
            desc = str(task["description"])[:50] if task else "?"
            lines.append(f"  ├── {task_id}: {desc}")

    return "\n".join(lines)


def format_cost_summary(tasks: list[dict[str, Any]]) -> str:
    """Format a cost/token summary."""
    lines: list[str] = []
    lines.append(
        f"{'ID':<25} {'Status':<12} {'In tokens':<12} "
        f"{'Out tokens':<12} {'Turns':<8} {'Duration':<12}"
    )
    lines.append("─" * 81)

    total_in = 0
    total_out = 0
    for task in tasks:
        task_id = str(task["id"])
        status = str(task["status"])
        in_tok = task.get("input_tokens")
        out_tok = task.get("output_tokens")
        turns = task.get("num_turns")
        in_str = _fmt_tokens(in_tok) if in_tok is not None else "—"
        out_str = _fmt_tokens(out_tok) if out_tok is not None else "—"
        turns_str = str(turns) if turns is not None else "—"
        duration = task.get("duration_ms")
        if duration is not None:
            secs = int(duration) / 1000
            dur_str = f"{secs:.1f}s"
        else:
            dur_str = "—"
        lines.append(
            f"{task_id:<25} {status:<12} {in_str:<12} {out_str:<12} {turns_str:<8} {dur_str}"
        )
        if in_tok is not None:
            total_in += int(in_tok)
        if out_tok is not None:
            total_out += int(out_tok)

    lines.append("─" * 81)
    lines.append(f"{'TOTAL':<37} {_fmt_tokens(total_in):<12} {_fmt_tokens(total_out)}")
    return "\n".join(lines)


def _fmt_tokens(n: int | None) -> str:
    """Format token count compactly."""
    if n is None:
        return "—"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_progress_summary(tasks: list[dict[str, Any]]) -> str:
    """Format a one-line progress summary."""
    counts: dict[str, int] = {}
    for task in tasks:
        s = str(task["status"])
        counts[s] = counts.get(s, 0) + 1

    total = len(tasks)
    completed = counts.get("completed", 0)
    failed = counts.get("failed", 0)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0)
    cancelled = counts.get("cancelled", 0)

    decomposed = counts.get("decomposed", 0)

    parts = [
        f"{completed}/{total} completed",
        f"{running} running",
        f"{pending} pending",
        f"{failed} failed",
        f"{cancelled} cancelled",
    ]
    if decomposed:
        parts.append(f"{decomposed} decomposed")

    return "Progress: " + ", ".join(parts)
