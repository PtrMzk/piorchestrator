"""CLI entry point for PO — argparse-based, no external deps."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from po.config import (
    DEFAULT_MAX_TURNS,
    TERMINAL_STATUSES,
    ensure_gitignore,
    logs_dir,
    state_db_path,
)
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
from po.init.generator import (
    generate_outline,
    generate_spec,
    generate_spec_from_outline,
)
from po.orchestrator.loop import EventCallback, OrchestratorLoop
from po.playground.generator import generate_playground
from po.preflight import run_preflight
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
        "--fresh", action="store_true",
        help=(
            "Discard the existing plan in .po/state.db before saving this one. "
            "Required to plan a different project, or to redefine tasks that "
            "already finished, in the same project root"
        ),
    )
    plan_parser.add_argument(
        "--scaffold", action=argparse.BooleanOptionalAction, default=False,
        help=(
            "Generate stub files for all output_files in the spec (default: off). "
            "Commit them before 'po run': task branches only see committed files"
        ),
    )
    plan_parser.add_argument(
        "--generate-docs", action=argparse.BooleanOptionalAction, default=False,
        help="Generate documentation tree (default: off; commit it before 'po run')",
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
    run_parser.add_argument(
        "--allow-dirty", action="store_true",
        help=(
            "Run even if tracked files have uncommitted changes. "
            "The merge runs 'git checkout -f', which discards them"
        ),
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

    # po scan
    scan_parser = subparsers.add_parser(
        "scan", help="Scan codebase and generate documentation",
    )
    scan_parser.add_argument(
        "--model", type=str, default="opus",
        help="Claude model to use (default: opus)",
    )
    scan_parser.add_argument(
        "--output-dir", type=str, default="docs/codebase",
        help="Output directory for generated docs (default: docs/codebase)",
    )
    scan_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )

    # po init
    init_parser = subparsers.add_parser(
        "init", help="Generate a spec file from a plain English description",
    )
    init_parser.add_argument(
        "description", type=str,
        help="Plain English project description",
    )
    init_parser.add_argument(
        "-o", "--output", type=Path, default=Path("spec.json"),
        help="Output file path (default: spec.json)",
    )
    init_parser.add_argument(
        "--model", type=str, default="opus",
        help="Claude model to use (default: opus)",
    )
    init_parser.add_argument(
        "--project-root", type=Path, default=Path("."),
        help="Project root directory",
    )
    init_parser.add_argument(
        "--fresh", action="store_true",
        help="Discard the existing plan in .po/state.db (see 'po plan --fresh')",
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
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "init":
        cmd_init(args)


def cmd_plan(args: argparse.Namespace) -> None:
    """Load spec, validate, persist to DB, show execution plan."""
    project_root: Path = args.project_root.resolve()
    spec_file: Path | None = args.spec_file
    if spec_file is not None:
        spec_file = spec_file.resolve()

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

    # 5. Ensure .po/ is in .gitignore before creating the directory
    ensure_gitignore(project_root)

    # 6. Persist to database
    db_path = state_db_path(project_root)
    conn = init_db(db_path)
    store = SqliteTaskStore(conn)
    if getattr(args, "fresh", False):
        store.clear()
    else:
        conflicts = _plan_conflicts(store, spec)
        if conflicts:
            for line in conflicts:
                logger.error("%s", line)
            conn.close()
            sys.exit(1)
    store.save_spec(spec)
    conn.close()

    print(f"Plan saved to {db_path}")

    # 7. Generate scaffolds if requested
    generated_any = False
    if args.scaffold:
        created = generate_scaffolds(spec, project_root)
        if created:
            generated_any = True
            print(f"\nGenerated {len(created)} scaffold files:")
            for p in created:
                print(f"  {p.relative_to(project_root)}")
        else:
            print("\nNo scaffold files needed (all output files already exist).")

    # 8. Generate docs if requested
    if args.generate_docs:
        created = generate_doc_tree(spec, project_root)
        generated_any = True
        print(f"\nGenerated {len(created)} documentation files:")
        for p in created:
            print(f"  {p.relative_to(project_root)}")

    if generated_any:
        # Task branches are cut from HEAD. An uncommitted stub that matches an
        # agent's output file does not help the agent (it never sees it) and
        # blocks the merge (git refuses to overwrite untracked files).
        print(
            "\nCommit these files before 'po run' — task branches only see "
            "committed files, and untracked ones block merges."
        )


_TASK_DEFINITION_FIELDS = (
    "description", "dependencies", "context_files", "output_files", "verification",
)
_TASK_LIST_FIELDS = frozenset({"dependencies", "context_files", "output_files"})


def _plan_conflicts(store: SqliteTaskStore, spec: Any) -> list[str]:
    """Explain why saving `spec` over the existing plan would be silently wrong.

    `save_spec` upserts by task id and keeps runtime state, which is what a
    re-plan of the *same* spec wants: completed tasks stay completed. The same
    behaviour is a trap for a *new* spec in the same project root — a second
    feature whose tasks reuse ids like `setup` — because every id that already
    finished is skipped and `po run` reports "all tasks completed" having done
    nothing. Returns an empty list when the save is safe.
    """
    project = store.get_project()
    if project is None:
        return []
    existing = {str(t["id"]): t for t in store.get_all_tasks()}
    if not existing:
        return []

    if project["project_name"] != spec.project_name:
        finished = sum(1 for t in existing.values() if t["status"] in TERMINAL_STATUSES)
        return [
            f"This project root already holds a plan for '{project['project_name']}' "
            f"({len(existing)} tasks, {finished} finished); the spec is for "
            f"'{spec.project_name}'.",
            "Planning on top of it would silently skip every task id that already "
            "finished. Use 'po plan --fresh' to discard the old plan, or a different "
            "--project-root.",
        ]

    redefined: list[str] = []
    for task in spec.tasks:
        row = existing.get(task.id)
        if row is None or row["status"] not in TERMINAL_STATUSES:
            continue
        for field in _TASK_DEFINITION_FIELDS:
            stored = row[field]
            if field in _TASK_LIST_FIELDS and isinstance(stored, str):
                stored = json.loads(stored)
            if (stored or "") != (getattr(task, field) or ""):
                redefined.append(f"{task.id} ({row['status']}, {field} changed)")
                break
    if not redefined:
        return []
    shown = "\n".join(f"  {r}" for r in redefined)
    return [
        "The spec redefines tasks that already finished, and finished tasks never run "
        f"again:\n{shown}",
        "Give them new ids, or use 'po plan --fresh' to discard the old plan and start "
        "over.",
    ]


def _live_event_printer(event: str, task_id: str, detail: str) -> None:
    """Print live status updates during orchestration."""
    symbols = {
        "task_launched": "▶",
        "task_completed": "✓",
        "task_failed": "✗",
        "task_retrying": "↻",
        "task_decomposed": "◈",
        "model_escalated": "⬆",
        "dependents_cancelled": "⊘",
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
            spec_file=args.spec_file.resolve(),
            project_root=args.project_root,
            playground=False,
            scaffold=False,
            generate_docs=False,
        )
        cmd_plan(plan_args)
        print()

    # .gitignore is seeded and committed by OrchestratorLoop._prepare_repo(),
    # which has to run before the first task branch is cut.

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

    # Pre-flight: every one of these used to surface late and badly — as a
    # traceback from git, as every task failing on a missing `claude`, or as
    # uncommitted work discarded by the merge. Check before touching anything.
    pending_outputs: list[str] = []
    for t in non_terminal:
        raw = t["output_files"]
        pending_outputs.extend(json.loads(raw) if isinstance(raw, str) else raw)
    problems = run_preflight(
        project_root, pending_outputs,
        allow_dirty=getattr(args, "allow_dirty", False),
    )
    if problems:
        for problem in problems:
            logger.error("%s", problem)
        conn.close()
        sys.exit(1)

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

    setup = project.get("setup") or ""

    max_retries = args.max_retries
    model_override = args.model
    max_turns = args.max_turns or DEFAULT_MAX_TURNS

    # Choose display mode based on TTY
    live_display = None
    on_event: EventCallback
    if sys.stdout.isatty():
        from po.display.live import LiveDisplay

        live_display = LiveDisplay(store, project_root)
        on_event = live_display
    else:
        on_event = _live_event_printer

    orchestrator = OrchestratorLoop(
        store=store,
        project_root=project_root,
        max_concurrency=max_concurrency,
        global_context=global_context,
        global_context_files=global_context_files,
        setup=setup,
        max_retries=max_retries,
        on_event=on_event,
        model_override=model_override,
        max_turns=max_turns,
    )

    print(f"Starting orchestration (concurrency={max_concurrency})...")
    print(format_progress_summary(store.get_all_tasks()))
    print()

    # Prevent macOS from sleeping while orchestration is running
    caffeinate_proc = None
    try:
        caffeinate_proc = subprocess.Popen(
            ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.debug("caffeinate started (pid=%d)", caffeinate_proc.pid)
    except FileNotFoundError:
        logger.debug("caffeinate not available (non-macOS?), skipping")

    if live_display is not None:
        live_display.start()

    try:
        asyncio.run(orchestrator.run())
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nShutdown requested. Existing tasks will be preserved.")
    finally:
        if live_display is not None:
            live_display.stop()
        if caffeinate_proc is not None:
            caffeinate_proc.terminate()
            caffeinate_proc.wait()

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
        # Verify the task was actually reset
        task = store.get_task(args.task)
        if task and task["status"] == "pending":
            print(f"Reset task '{args.task}' to pending.")
        else:
            status = task["status"] if task else "not found"
            print(
                f"Warning: task '{args.task}' was not reset "
                f"(current status: {status})."
            )
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

        ts = msg.get("timestamp", "")
        if ts:
            # Show local time as HH:MM:SS
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(ts).astimezone()
                ts_prefix = dt.strftime("%H:%M:%S") + " "
            except (ValueError, TypeError):
                ts_prefix = ""
        else:
            ts_prefix = ""

        msg_type = msg.get("type", "")
        if msg_type == "assistant":
            text = msg.get("message", {}).get("content", "")
            if isinstance(text, list):
                for block in text:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        entries.append(f"{ts_prefix}[Assistant] {block['text']}")
                    elif btype == "tool_use":
                        name = block.get("name", "?")
                        inp = json.dumps(block.get("input", {}))
                        if len(inp) > 120:
                            inp = inp[:120] + "..."
                        entries.append(f"{ts_prefix}[Tool Call] {name}({inp})")
            elif isinstance(text, str) and text:
                entries.append(f"{ts_prefix}[Assistant] {text}")
        elif msg_type == "tool_result":
            content_val = msg.get("content", "")
            if isinstance(content_val, str):
                truncated = content_val[:200]
                if len(content_val) > 200:
                    truncated += "..."
                entries.append(f"{ts_prefix}[Tool Result] {truncated}")
        elif msg_type == "result":
            cost = msg.get("cost_usd", "?")
            duration = msg.get("duration_ms")
            result_text = msg.get("result", "")[:200]
            parts = [f"\n{ts_prefix}[Result] Cost: ${cost}"]
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

    # If we have a DB, only clean worktrees for terminal tasks
    terminal_ids: set[str] = set()
    if db_path.exists():
        conn = init_db(db_path)
        store = SqliteTaskStore(conn)
        for task in store.get_all_tasks():
            if task["status"] in TERMINAL_STATUSES:
                terminal_ids.add(str(task["id"]))
        conn.close()

    # A task that failed after a merge or verification keeps its branch with
    # no worktree directory, so it is not in the listing above; `remove()` is
    # idempotent, so reap those branches too.
    kept_branches = {
        tid for tid in terminal_ids if _branch_exists(project_root, f"po/{tid}")
    }
    if not worktrees and not kept_branches:
        print("No worktrees found.")
        return

    removed = 0
    for wt in worktrees:
        # Remove if task is terminal or if we have no DB
        if not db_path.exists() or wt.task_id in terminal_ids:
            wt_mgr.remove(wt.task_id, project_root)
            print(f"  Removed worktree: {wt.task_id}")
            removed += 1
            kept_branches.discard(wt.task_id)
    for task_id in sorted(kept_branches):
        wt_mgr.remove(task_id, project_root)
        print(f"  Removed branch: po/{task_id}")
        removed += 1

    print(f"Cleaned {removed} worktree(s)/branch(es).")


def _branch_exists(project_root: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        cwd=project_root, capture_output=True, check=False,
    ).returncode == 0


def cmd_scan(args: argparse.Namespace) -> None:
    """Scan codebase and generate documentation."""
    from po.scan.scanner import scan_codebase

    project_root: Path = args.project_root.resolve()
    model: str = args.model
    output_dir: str = args.output_dir

    print(f"Scanning codebase (model={model}, output={output_dir})...")
    try:
        docs_path = scan_codebase(
            model=model,
            output_dir=output_dir,
            project_root=project_root,
        )
    except RuntimeError as e:
        logger.error("%s", e)
        sys.exit(1)

    # List generated files
    files = sorted(docs_path.rglob("*"))
    doc_files = [f for f in files if f.is_file()]
    if doc_files:
        print(f"\nGenerated {len(doc_files)} documentation files:")
        for f in doc_files:
            print(f"  {f.relative_to(project_root)}")
    else:
        print("\nNo documentation files were generated.")

    print(f"\nDocs directory: {docs_path.relative_to(project_root)}")
    print("Tip: run 'po init' to generate a spec that references these docs.")


def cmd_init(args: argparse.Namespace) -> None:
    """Generate a spec file from a plain English description."""
    output: Path = args.output
    model: str = args.model
    description: str = args.description
    project_root: Path = args.project_root.resolve()
    interactive = sys.stdin.isatty()

    if not interactive:
        # Non-interactive: generate spec directly (no outline review)
        print(f"Generating spec from description (model={model})...")
        try:
            path = generate_spec(description, output, model, project_root=project_root)
        except (FileExistsError, ValueError, RuntimeError) as e:
            logger.error("%s", e)
            sys.exit(1)
    else:
        # Interactive: outline → review → full spec
        feedback: str | None = None
        session_id: str | None = None
        while True:
            if feedback:
                print(f"\nRevising outline with feedback (model={model})...")
            else:
                print(f"Generating outline (model={model})...")

            try:
                outline, session_id = generate_outline(
                    description, model, project_root=project_root,
                    feedback=feedback, session_id=session_id,
                )
            except RuntimeError as e:
                logger.error("%s", e)
                sys.exit(1)

            print()
            print(outline)
            print()

            response = input("Looks good? [Y/n] or type feedback: ").strip()
            if not response or response.lower() in ("y", "yes"):
                break
            if response.lower() in ("n", "no"):
                response = input("What should change? ").strip()
            feedback = response

        print(f"\nGenerating full spec from outline (model={model})...")
        try:
            path = generate_spec_from_outline(
                description, outline, output, model, project_root=project_root,
            )
        except (FileExistsError, ValueError, RuntimeError) as e:
            logger.error("%s", e)
            sys.exit(1)

    print(f"Spec written to {path}")
    print()

    # Auto-plan: validate spec, show execution plan, persist to DB
    plan_args = argparse.Namespace(
        spec_file=path.resolve(),
        project_root=args.project_root,
        playground=False,
        scaffold=False,
        generate_docs=False,
        fresh=getattr(args, "fresh", False),
    )
    cmd_plan(plan_args)
    print()
    print("Next step:")
    print("  po run                # run the orchestration")
