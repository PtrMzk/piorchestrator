"""CLI entry point for PO — argparse-based, no external deps."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from po.config import DEFAULT_MAX_TURNS, TERMINAL_STATUSES, logs_dir, state_db_path
from po.db.connection import init_db
from po.db.queries import SqliteTaskStore
from po.display.status import (
    format_cost_summary,
    format_execution_plan,
    format_progress_summary,
    format_status_table,
)
from po.docs.generator import generate_doc_tree
from po.graph.resolver import get_execution_plan
from po.orchestrator.loop import OrchestratorLoop
from po.playground.generator import generate_playground
from po.scaffold.generator import generate_scaffolds
from po.spec.loader import JsonSpecLoader

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False, quiet: bool = False) -> None:
    """Configure the root logger based on CLI flags."""
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    logging.basicConfig(
        level=level,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="po",
        description="PO — An orchestrator built on top of Claude Code CLI",
    )

    # Global flags
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress info-level logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # po plan
    plan_parser = subparsers.add_parser("plan", help="Load spec, validate, show execution plan")
    plan_parser.add_argument(
        "spec_file", type=Path, nargs="?", default=None,
        help="Path to the spec JSON file",
    )
    plan_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )
    plan_parser.add_argument(
        "--playground", action="store_true",
        help="Generate a self-testing playground spec for quick verification",
    )
    plan_parser.add_argument(
        "--scaffold", action="store_true",
        help="Generate stub files for all output_files in the spec",
    )
    plan_parser.add_argument(
        "--generate-docs", action="store_true",
        help="Generate documentation tree",
    )

    # po run
    run_parser = subparsers.add_parser(
        "run", help="Start the orchestration loop",
    )
    run_parser.add_argument(
        "spec_file", type=Path, nargs="?", default=None,
        help="Optional spec file — auto-runs 'po plan' first",
    )
    run_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )
    run_parser.add_argument(
        "--concurrency", type=int, help="Override max concurrency",
    )
    run_parser.add_argument(
        "--max-retries", type=int, default=1,
        help="Max retries per task (default: 1)",
    )
    run_parser.add_argument(
        "--model", type=str, help="Override model for all tasks",
    )
    run_parser.add_argument(
        "--max-turns", type=int, help="Max agent turns per task",
    )

    # po status
    status_parser = subparsers.add_parser("status", help="Show task states and progress")
    status_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    # po reset
    reset_parser = subparsers.add_parser("reset", help="Reset failed/cancelled tasks to pending")
    reset_parser.add_argument(
        "--task", type=str,
        help="Specific task ID to reset (default: all failed/cancelled)",
    )
    reset_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    # po cost
    cost_parser = subparsers.add_parser("cost", help="Show cost summary")
    cost_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    # po logs
    logs_parser = subparsers.add_parser("logs", help="Show agent logs for a task")
    logs_parser.add_argument("task_id", type=str, help="Task ID")
    logs_parser.add_argument("--raw", action="store_true", help="Show raw JSONL")
    logs_parser.add_argument(
        "--tail", type=int, default=0, metavar="N",
        help="Show only the last N log entries",
    )
    logs_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    # po clean
    clean_parser = subparsers.add_parser(
        "clean", help="Remove orphaned worktrees",
    )
    clean_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    args = parser.parse_args()
    _configure_logging(
        verbose=getattr(args, "verbose", False),
        quiet=getattr(args, "quiet", False),
    )

    if args.command == "plan":
        cmd_plan(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "reset":
        cmd_reset(args)
    elif args.command == "cost":
        cmd_cost(args)
    elif args.command == "logs":
        cmd_logs(args)
    elif args.command == "clean":
        cmd_clean(args)


def cmd_plan(args: argparse.Namespace) -> None:
    """Load spec, validate, persist to DB, show execution plan."""
    project_root: Path = args.project_root.resolve()
    spec_file: Path | None = args.spec_file

    # 1. If --playground: generate spec + seed files
    if args.playground:
        if spec_file is not None:
            logger.warning("--playground takes precedence over spec_file argument")
        try:
            generated_path, seed_files = generate_playground(project_root)
        except FileExistsError as e:
            logger.error("%s", e)
            sys.exit(1)
        spec_file = generated_path
        print(f"Generated playground spec: {generated_path}")
        if seed_files:
            print(f"Created seed files: {', '.join(str(s) for s in seed_files)}")
        print()

    # 2. Require a spec file
    if spec_file is None:
        logger.error("spec_file is required (or use --playground)")
        sys.exit(1)

    # 3. Load and validate spec
    loader = JsonSpecLoader()
    try:
        spec = loader.load(spec_file)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.error("%s", e)
        sys.exit(1)

    # 4. Show execution plan
    print(f"Project: {spec.project_name}")
    print(f"Tasks: {len(spec.tasks)}")
    print(f"Max concurrency: {spec.max_concurrency}")
    print()

    layers = get_execution_plan(spec.tasks)
    task_dicts: list[dict[str, Any]] = [
        {"id": t.id, "description": t.description, "status": "pending"}
        for t in spec.tasks
    ]
    print(format_execution_plan(layers, task_dicts))
    print()

    # 5. Persist to database
    db_path = state_db_path(project_root)
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    store.save_spec(spec)
    conn.close()

    print(f"Plan saved to {db_path}")

    # 6. Generate scaffolds if requested
    if args.scaffold:
        created = generate_scaffolds(spec, project_root)
        if created:
            print(f"\nGenerated {len(created)} scaffold files:")
            for p in created:
                print(f"  {p.relative_to(project_root)}")
        else:
            print("\nNo scaffold files needed (all output files already exist).")

    # 7. Generate docs if requested
    if args.generate_docs:
        created = generate_doc_tree(spec, project_root)
        print(f"\nGenerated {len(created)} documentation files:")
        for p in created:
            print(f"  {p.relative_to(project_root)}")


def _live_event_printer(event: str, task_id: str, detail: str) -> None:
    """Print live status updates during orchestration."""
    symbols = {
        "task_launched": "▶",
        "task_completed": "✓",
        "task_failed": "✗",
        "task_retrying": "↻",
        "task_decomposed": "◈",
    }
    symbol = symbols.get(event, "·")
    parts = [f"  {symbol} {task_id} {event.replace('task_', '')}"]
    if detail:
        parts.append(f"({detail})")
    print(" ".join(parts))


def cmd_run(args: argparse.Namespace) -> None:
    """Start the orchestration loop."""
    project_root: Path = args.project_root.resolve()

    # If a spec file was provided, auto-plan first
    if args.spec_file is not None:
        plan_args = argparse.Namespace(
            spec_file=args.spec_file,
            project_root=args.project_root,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        cmd_plan(plan_args)
        print()

    db_path = state_db_path(project_root)

    if not db_path.exists():
        logger.error("No plan found. Run 'po plan <spec.json>' first.")
        sys.exit(1)

    conn = init_db(db_path)
    store = SqliteTaskStore(conn)

    project = store.get_project()
    if project is None:
        logger.error("No project data found in database.")
        sys.exit(1)

    # Check if all tasks are already terminal
    all_tasks = store.get_all_tasks()
    non_terminal = [t for t in all_tasks if t["status"] not in TERMINAL_STATUSES]
    if not non_terminal:
        print("All tasks are already completed (or failed/cancelled).")
        print(format_progress_summary(all_tasks))
        conn.close()
        return

    max_concurrency = (
        args.concurrency or int(project["max_concurrency"])
    )
    global_context = project.get("global_context") or ""
    global_context_files_raw = project.get("global_context_files", "[]")
    global_context_files: list[str] = (
        json.loads(global_context_files_raw)
        if isinstance(global_context_files_raw, str)
        else global_context_files_raw
    )

    max_retries = args.max_retries
    model_override = args.model
    max_turns = args.max_turns or DEFAULT_MAX_TURNS

    orchestrator = OrchestratorLoop(
        store=store,
        project_root=project_root,
        max_concurrency=max_concurrency,
        global_context=global_context,
        global_context_files=global_context_files,
        max_retries=max_retries,
        on_event=_live_event_printer,
        model_override=model_override,
        max_turns=max_turns,
    )

    print(f"Starting orchestration (concurrency={max_concurrency})...")
    print(format_progress_summary(store.get_all_tasks()))
    print()

    try:
        asyncio.run(orchestrator.run())
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutdown requested. Existing tasks will be preserved.")

    print()
    print(format_progress_summary(store.get_all_tasks()))
    conn.close()


def cmd_status(args: argparse.Namespace) -> None:
    """Show task states and progress."""
    project_root: Path = args.project_root.resolve()
    db_path = state_db_path(project_root)

    if not db_path.exists():
        logger.error("No plan found. Run 'po plan <spec.json>' first.")
        sys.exit(1)

    conn = init_db(db_path)
    store = SqliteTaskStore(conn)

    tasks = store.get_all_tasks()
    print(format_status_table(tasks))
    print()
    print(format_progress_summary(tasks))
    conn.close()


def cmd_reset(args: argparse.Namespace) -> None:
    """Reset failed/cancelled tasks to pending."""
    project_root: Path = args.project_root.resolve()
    db_path = state_db_path(project_root)

    if not db_path.exists():
        logger.error("No plan found.")
        sys.exit(1)

    conn = init_db(db_path)
    store = SqliteTaskStore(conn)

    if args.task:
        store.reset_task(args.task)
        print(f"Reset task '{args.task}' to pending.")
    else:
        tasks = store.get_all_tasks()
        count = 0
        for task in tasks:
            if task["status"] in ("failed", "cancelled"):
                store.reset_task(str(task["id"]))
                count += 1
        print(f"Reset {count} tasks to pending.")

    conn.close()


def cmd_cost(args: argparse.Namespace) -> None:
    """Show cost summary."""
    project_root: Path = args.project_root.resolve()
    db_path = state_db_path(project_root)

    if not db_path.exists():
        logger.error("No plan found.")
        sys.exit(1)

    conn = init_db(db_path)
    store = SqliteTaskStore(conn)

    tasks = store.get_all_tasks()
    print(format_cost_summary(tasks))
    conn.close()


def cmd_logs(args: argparse.Namespace) -> None:
    """Show agent logs for a task."""
    project_root: Path = args.project_root.resolve()
    log_dir = logs_dir(project_root)
    log_file = log_dir / f"{args.task_id}.jsonl"

    if not log_file.exists():
        logger.error("No logs found for task '%s'.", args.task_id)
        sys.exit(1)

    content = log_file.read_text(encoding="utf-8")

    if args.raw:
        lines = content.strip().splitlines()
        if args.tail > 0:
            lines = lines[-args.tail :]
        print("\n".join(lines))
        return

    # Parse all lines into structured entries
    entries: list[str] = []
    for line in content.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            entries.append(line)
            continue

        msg_type = msg.get("type", "")
        if msg_type == "assistant":
            text = msg.get("message", {}).get("content", "")
            if isinstance(text, list):
                for block in text:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        entries.append(f"[Assistant] {block['text']}")
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))
                        if len(inp) > 120:
                            inp = inp[:120] + "..."
                        entries.append(f"[Tool Call] {name}({inp})")
            elif isinstance(text, str) and text:
                entries.append(f"[Assistant] {text}")
        elif msg_type == "tool_result":
            content_val = msg.get("content", "")
            if isinstance(content_val, str):
                truncated = content_val[:200]
                if len(content_val) > 200:
                    truncated += "..."
                entries.append(f"[Tool Result] {truncated}")
        elif msg_type == "result":
            cost = msg.get("cost_usd", "?")
            duration = msg.get("duration_ms")
            result_text = msg.get("result", "")[:200]
            parts = [f"\n[Result] Cost: ${cost}"]
            if duration:
                parts[0] += f" | Duration: {duration}ms"
            if result_text:
                parts.append(f"  {result_text}")
            entries.extend(parts)
        elif msg_type == "system":
            entries.append(f"[System] {msg.get('message', '')}")

    # Apply --tail
    if args.tail > 0:
        entries = entries[-args.tail :]

    for entry in entries:
        print(entry)


def cmd_clean(args: argparse.Namespace) -> None:
    """Remove orphaned worktrees from failed/cancelled tasks."""
    from po.worktree.manager import GitWorktreeManager

    project_root: Path = args.project_root.resolve()
    db_path = state_db_path(project_root)

    wt_mgr = GitWorktreeManager()
    worktrees = wt_mgr.list(project_root)

    if not worktrees:
        print("No worktrees found.")
        return

    # If we have a DB, only clean worktrees for terminal tasks
    terminal_ids: set[str] = set()
    if db_path.exists():
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        for task in store.get_all_tasks():
            if task["status"] in TERMINAL_STATUSES:
                terminal_ids.add(str(task["id"]))
        conn.close()

    removed = 0
    for wt in worktrees:
        # Remove if task is terminal or if we have no DB
        if not db_path.exists() or wt.task_id in terminal_ids:
            wt_mgr.remove(wt.task_id, project_root)
            print(f"  Removed worktree: {wt.task_id}")
            removed += 1

    print(f"Cleaned {removed} worktree(s).")
