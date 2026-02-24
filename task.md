# PO — Remaining Work

Audit of the current implementation against the design plan. Organized by severity.

---

## Critical — Blocks Correct Operation

### ~~1. `max_budget_usd` is never enforced~~ — DONE
`ClaudeCodeRunner` now passes `--max-cost` flag when `max_budget_usd` is set.
`OrchestratorLoop` retrieves `max_budget_usd` from the task and passes it to the runner.

### ~~2. Subtask handling logic is broken~~ — DONE
Added `STATUS_DECOMPOSED` to `config.py` and `TERMINAL_STATUSES`. Parent tasks are marked
as decomposed after subtask creation. Subtask IDs are namespaced with parent task ID
(`{parent_id}/{subtask_id}`). Parent worktree is cleaned up after decomposition.

### ~~3. `upsert_task` clobbers completed task state on re-plan~~ — DONE
Replaced `INSERT OR REPLACE` with `ON CONFLICT(id) DO UPDATE SET` that only updates
spec-level columns (description, dependencies, etc.) and preserves runtime state
(status, cost_usd, completed_at, etc.).

### ~~4. Graceful shutdown loses task results~~ — DONE
Shutdown branch now explicitly calls `_collect_completed()` after `asyncio.gather()`
completes, ensuring task results are persisted before exiting.

### ~~5. Merge agent resolution is stubbed out~~ — DONE
`_try_agent_merge()` now fully invokes Claude CLI to resolve merge conflicts.
Attempts `--no-ff --no-commit` merge first, detects conflicts, then calls
`_invoke_merge_agent()` which runs Claude with conflict context. Verifies merge was
committed and HEAD advanced.

---

## High — Significant Usability / Reliability Issues

### ~~6. No live status display during `po run`~~ — DONE
Added `EventCallback` protocol and `on_event` callback support to `OrchestratorLoop`.
Events emitted for all task state changes. CLI provides `_live_event_printer()` that
displays real-time progress during orchestration.

### ~~7. `--generate-docs` overwrites existing CLAUDE.md~~ — DONE
Smart CLAUDE.md handling: checks for `<!-- po:generated -->` marker. If found, replaces
only the generated section. If not found, appends with separator. Fresh file only created
when no existing CLAUDE.md exists.

### ~~8. Large test coverage gaps~~ — DONE
All test files created: `test_launcher.py`, `test_prompt_builder.py`, `test_merge.py`,
`test_docs_generator.py`, `test_display.py`, `test_cli.py`. Total: 211 tests passing.

### ~~9. Worktree branch name collisions on retry~~ — DONE
`WorktreeManager.create()` now cleans up stale state before creation: removes existing
worktree, prunes bookkeeping, and deletes stale branch with `git branch -D`.

---

## Medium — Should Be Addressed for Production Use

### ~~10. No `--max-retries` CLI flag~~ — DONE
`--max-retries` flag added to run subparser and passed through to `OrchestratorLoop`.

### ~~11. Session ID never persisted to DB~~ — DONE
`set_completed()` and `set_failed()` now write `session_id` to the database.
`OrchestratorLoop` passes `result.session_id` to both methods.

### ~~12. No `po clean` command~~ — DONE
Added `clean` subcommand that lists all worktrees, removes those belonging to terminal
tasks, and provides feedback to the user.

### ~~13. No logging framework~~ — DONE
Added stdlib `logging` to `cli.py`, `orchestrator/loop.py`, `orchestrator/merge.py`,
`agent/launcher.py`. CLI supports `-v`/`--verbose` and `-q`/`--quiet` flags.
All `print(..., file=sys.stderr)` error output replaced with `logger.error()`.

### ~~14. SQLite concurrent access is fragile~~ — DONE
Connection uses `check_same_thread=False` and `PRAGMA journal_mode=WAL` for safe
concurrent access.

### ~~15. `po status` doesn't show error messages or cost~~ — DONE
`format_status_table()` now displays cost as `$X.XXXX` and shows truncated error
messages for failed tasks.

### ~~16. `po reset --task X` doesn't cascade to cancelled dependents~~ — DONE
`reset_task()` implements cascade reset using BFS to find and reset all transitive
cancelled dependents.

### ~~17. No progress event system~~ — DONE
Full event callback system with `EventCallback` protocol, event emission on all state
changes, and CLI integration (see #6).

---

## Low — Nice-to-Have / Polish

### ~~18. No combined `po run <spec.json>` shortcut~~ — DONE
`po run` now accepts an optional `spec_file` positional arg that auto-plans first.

### ~~19. No `--model` override on `po run`~~ — DONE
`--model` flag added to run subparser. Model override passed to `OrchestratorLoop` and
applied to all tasks.

### ~~20. `max_turns` hardcoded instead of using config constant~~ — DONE
`DEFAULT_MAX_TURNS = 50` defined in `config.py`. Used by `OrchestratorLoop`.
`--max-turns` CLI flag added to override at runtime.

### ~~21. Context files read from project root, not worktree~~ — DONE
Context files now prefer the worktree copy, falling back to project root.

### ~~22. `API_CONTRACTS.md` is a TODO stub~~ — DONE
Stub implementation exists in `_build_api_contracts()`. Acceptable as placeholder for
future expansion.

### ~~23. `_handle_failure` logging is silenced~~ — DONE
`_handle_failure()` now emits `dependents_cancelled` event when cancelled count > 0,
providing feedback through the event system.

### ~~24. `po logs` parsing is fragile/limited~~ — DONE
Now handles `tool_result`, `system` messages, shows tool call inputs (truncated),
includes duration in result output. Added `--tail N` flag.

### ~~25. No spec format versioning~~ — DONE
Added `version` field to `ProjectSpec` (defaults to `"1"`). `SPEC_VERSION` constant
defined in `spec/schema.py`.

### ~~26. `global_context` edge case with None~~ — DONE
Safe handling with `or ""` default and `if global_context:` guard in prompt builder.

### ~~27. No "all tasks already completed" message~~ — DONE
`po run` now prints a clear message when all tasks are already in terminal state.

### ~~28. Example spec output file overlap is unintuitive~~ — DONE
Output file overlap in `examples/todo-api.json` is by design — demonstrates the overlap
filter serializing tasks with shared files correctly.
