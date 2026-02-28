# Piorchestrator Architecture

> **Auto-maintained:** This document is kept in sync with the codebase after every commit.

Piorchestrator (`po`) is a **multi-agent orchestrator** that coordinates parallel Claude Code agents to execute complex software development projects. It takes a JSON specification describing a project broken into tasks with dependencies, then runs multiple Claude agents concurrently — each in an isolated git worktree — to build the project in parallel.

---

## Major Entry Points (CLI Commands)

### 1. `po init <description>` — Spec Generation & Planning (combined)
Converts a plain-English project description into a structured JSON spec, then automatically validates, plans, scaffolds, and generates docs (i.e. runs `po plan` logic). In interactive terminals, uses a two-phase flow: first generates a human-readable outline for user review, then produces the full JSON spec after approval. Users can provide feedback to revise the outline before committing.

**Code walkthrough (interactive mode):**
1. `cli.py:cmd_init` → calls `init/generator.py:generate_outline(description, model, feedback=None, session_id=None)`
2. `generate_outline` builds a prompt via `_build_outline_prompt()` asking for a markdown outline (task names, descriptions, dependencies, layers) — NOT JSON
3. `_invoke_claude()` spawns Claude CLI with a `rich.status.Status` spinner (updates in-place with tool summaries from `display/tools.py:tool_summary()`), returns `(result_text, session_id)`
4. User reviews outline, types `y` to approve or provides feedback to revise — feedback revisions resume the same Claude session via `--resume <session_id>` to avoid re-reading the codebase
5. On approval: `generate_spec_from_outline(description, outline, output, model)` builds a prompt combining the approved outline with `_spec_schema_instructions()` (JSON schema, constraints, example spec)
6. `_invoke_claude()` generates JSON, `_extract_json()` strips fences, `ProjectSpec.from_dict()` validates
7. Auto-runs `cmd_plan` logic: validation, execution plan display, DB persistence, scaffold generation, doc tree creation

**Non-interactive mode** (piped stdin): skips the outline review loop and generates the full spec directly via `generate_spec()`.

### 2. `po plan <spec.json>` — Validation & Planning
Loads and validates a spec (checks for duplicate IDs, cycles, missing dependencies), persists it to a SQLite database (`.po/state.db`), and displays the execution plan as dependency layers. Scaffold and doc generation are on by default:
- `--no-scaffold` — skip generating stub files for all `output_files`
- `--no-generate-docs` — skip generating documentation tree (`CLAUDE.md`, `SYSTEM_DESIGN.md`, component docs)
- `--playground` — generates a self-testing calculator spec to demo the tool

**Code walkthrough:**
1. `cli.py:cmd_plan` — if `--playground`, calls `playground/generator.py:generate_playground()` to create a spec + seed files
2. `spec/loader.py:JsonSpecLoader.load()` parses JSON → `spec/schema.py:ProjectSpec.from_dict()` → `spec.validate()` (duplicate IDs, missing dep refs, cycle detection via `graph/resolver.py:topological_sort`)
3. `graph/resolver.py:get_execution_plan()` groups tasks into BFS layers where each layer's deps are satisfied by prior layers — displayed via `display/status.py:format_execution_plan()`
4. `db/connection.py:init_db()` opens SQLite in WAL mode, runs DDL from `db/models.py`
5. `db/queries.py:SqliteTaskStore.save_spec()` upserts project metadata + all tasks (preserves runtime state for re-plans via `ON CONFLICT DO UPDATE`)
6. Optionally: `scaffold/generator.py:generate_scaffolds()` creates stub files; `docs/generator.py:generate_doc_tree()` creates doc files

### 3. `po run [spec.json]` — The Orchestration Loop (core business logic)
This is the heart of the system. The async loop:

1. **Queries the DB** for tasks whose dependencies are all `completed`
2. **Filters for output file overlap** — prevents two tasks writing the same file concurrently
3. **Creates a git worktree** per task (branch: `po/{task_id}`, directory: `.po/worktrees/{task_id}/`)
4. **Launches Claude Code** as a subprocess in that worktree with a carefully built prompt containing the task description, global context, reference file contents, expected outputs, and verification command
5. **Streams agent output** to `.po/logs/{task_id}.jsonl`, parsing for cost/session data
6. **Processes results** when the agent finishes:
   - **Success**: detaches worktree, rebases branch onto main, fast-forward merges, runs verification. If verification fails, reverts.
   - **Subtasks created** (agent wrote `.po-subtasks.json`): namespaces subtask IDs under the parent, inherits dependencies, adds them to the DB, marks parent as `decomposed`
   - **Failure**: retries if attempts remain (keeps the branch for incremental progress), otherwise marks `failed` and cascades `cancelled` to all dependents
7. **Detects deadlock** — pending tasks with unsatisfiable dependencies
8. **Handles signals** — first SIGINT cancels running tasks and terminates agent subprocesses; second force-exits

Key design: tasks that write to the same files are serialized, while tasks with no file overlap run fully in parallel up to `--concurrency`.

**Code walkthrough — startup (`cli.py:cmd_run`):**
1. If a spec file is given, auto-runs `cmd_plan` first
2. Opens the DB, reads project metadata + checks for non-terminal tasks
3. If TTY, creates `display/live.py:LiveDisplay` (Rich Live at 4Hz); otherwise uses a simple `_live_event_printer` callback
4. Constructs `orchestrator/loop.py:OrchestratorLoop` with store, worktree manager, agent runner, merger (all protocol-based, defaulting to real implementations)
5. Spawns `caffeinate -i -s` to prevent macOS sleep, then `asyncio.run(orchestrator.run())`

**Code walkthrough — the async loop (`orchestrator/loop.py:OrchestratorLoop`):**
1. `run()` installs SIGINT/SIGTERM handlers → `_request_shutdown()` (first signal cancels asyncio tasks; second calls `os._exit`)
2. `_loop()` runs in a `while True`:
   - `_collect_completed()` — iterates `_running_tasks` dict, pops `.done()` asyncio Tasks, extracts `AgentResult` (catching `CancelledError` + exceptions)
   - `store.get_ready_task_ids()` — SQL query using `json_each()` to find pending tasks whose every dependency is completed (`db/queries.py:242-258`)
   - `_filter_output_overlap()` — computes the set of `output_files` for all running tasks, excludes ready tasks that intersect. Within a batch, tracks `batch_outputs` to prevent intra-iteration overlap too
   - For each task up to `slots` remaining: `asyncio.create_task(self._run_task(task_id))`
   - Deadlock check: if no running, no ready, but still non-terminal → `RuntimeError`
   - `asyncio.wait(..., timeout=5.0, return_when=FIRST_COMPLETED)` — wakes when any agent finishes

**Code walkthrough — running a single task (`_run_task`):**
1. `worktree/manager.py:GitWorktreeManager.create()` — prunes stale worktrees, ensures git repo, checks if branch `po/{task_id}` exists (retry reuses branch), otherwise `git worktree add -b po/{task_id} .po/worktrees/{task_id}/ HEAD`
2. `store.set_running()` — marks DB status=running, increments attempt counter, records branch/worktree path
3. Reads `context_files` (prefers worktree copy for retry continuity, falls back to project root) + `global_context_files`
4. `agent/prompt_builder.py:build_prompt()` — assembles markdown prompt: task description, previous error (for retries), global context, reference file contents, expected outputs, verification command, TDD rules, subtask/failure instructions
5. `agent/launcher.py:ClaudeCodeRunner.run()` — spawns `claude -p <prompt> --output-format stream-json --model <model> --max-turns <N> --permission-mode bypassPermissions` in the worktree dir. Streams stdout to `.po/logs/{task_id}.jsonl` (injecting timestamps). Drains stderr concurrently. On `CancelledError`, sends SIGTERM → wait 5s → SIGKILL. Checks for `.po-subtasks.json` and `.po-failure.json` in worktree after exit. Returns `AgentResult`

**Code walkthrough — processing results (`_process_result`):**
- **Success path:**
  1. **Pre-merge verification** — if the task has a `verification` command, runs it in the worktree (`cwd=worktree_path`) *before* detaching or merging. If it fails, treats it as an agent failure (retry if attempts remain, otherwise fail). Logs to `.po/logs/preverify-{task_id}.log`. This catches issues early while the worktree still exists, so retry agents can fix them without wasting a merge attempt.
  2. `worktree_mgr.detach()` — `git worktree remove --force` + `git worktree prune` (frees the branch for checkout)
  3. `orchestrator/merge.py:RebaseMerger.merge()` — serialized by `asyncio.Lock`, runs in executor:
     - Cleans stale rebase/merge state, checks out main
     - `git rebase main po/{task_id}` → on success: `git checkout main` → `git merge --ff-only po/{task_id}`
     - If rebase fails: aborts, falls back to `_try_agent_merge()` → `git merge --no-ff --no-commit` → if conflicts, `_invoke_merge_agent()` spawns another Claude CLI to resolve conflict markers, stage files, commit
     - Runs post-merge verification command; on failure: `git reset --hard HEAD~1` (reverts merge)
  4. On merge success: `worktree_mgr.remove()` (deletes branch), `store.set_completed()`
  5. On merge failure with retries left: keeps branch, sets task back to pending
- **Subtask path:** namespaces IDs as `{parent}/{subtask}`, inherits parent deps, `store.add_runtime_task()`, marks parent `decomposed`
- **Failure path:** if retries left → `store.set_status(pending)`, cleans worktree; else → `store.set_failed()`, `store.cancel_dependents()` (BFS cascade)

### 4. `po status` / `po cost` / `po logs <id>` — Monitoring
- **status**: table of all tasks with their state, cost, and any error messages
- **cost**: per-task and total cost summary
- **logs**: streams the agent's JSONL log (supports `--raw` and `--tail`)

**Code walkthrough:**
- All three: `cli.py` → open DB → `SqliteTaskStore.get_all_tasks()` → formatter from `display/status.py`
- `cmd_logs`: reads `.po/logs/{task_id}.jsonl` directly, parses each JSON line by `type` field (`assistant` → text/tool_use blocks, `tool_result` → truncated output, `result` → cost/duration summary). Applies `--tail` slicing. `--raw` skips parsing and dumps lines verbatim

### 5. `po reset [--task ID]` — Recovery
Resets `failed`/`cancelled` tasks back to `pending`, cascading to dependents. Preserves the git branch so retry agents can build on previous work.

**Code walkthrough:**
1. `cli.py:cmd_reset` → `db/queries.py:SqliteTaskStore.reset_task(task_id)`
2. `_reset_single()` — sets status=pending, clears error/cost/timing/worktree fields (only for failed/cancelled/running tasks)
3. BFS cascade: walks all tasks, finds cancelled dependents of the reset task, resets them too
4. Git branches are *not* deleted — the next `po run` reuses the existing `po/{task_id}` branch via `git worktree add <path> <existing-branch>`

### 6. `po clean` — Cleanup
Removes orphaned git worktrees that weren't properly cleaned up.

**Code walkthrough:**
1. `cli.py:cmd_clean` → `worktree/manager.py:GitWorktreeManager.list()` — scans `.po/worktrees/` for directories
2. If DB exists, loads all tasks in terminal status (completed/failed/cancelled)
3. For each worktree whose `task_id` is terminal (or no DB): `GitWorktreeManager.remove()` → `git worktree remove --force` + `git worktree prune` + `git branch -D po/{task_id}`

---

## Key Business Logic Layers

| Layer | Module | Responsibility |
|-------|--------|---------------|
| **Spec** | `spec/schema.py`, `spec/loader.py` | Data model, validation, cycle detection |
| **Graph** | `graph/resolver.py` | Topological sort, ready-task detection, execution plan layers |
| **Database** | `db/queries.py` | Task state machine (pending→running→completed/failed/cancelled/decomposed) |
| **Orchestrator** | `orchestrator/loop.py` | Async coordination loop, concurrency, output-file overlap filtering, retries, deadlock detection |
| **Agent** | `agent/launcher.py`, `agent/prompt_builder.py` | Claude CLI subprocess management, prompt construction, cost/session parsing |
| **Merge** | `orchestrator/merge.py` | Rebase + fast-forward merge; spawns a "merge agent" Claude to resolve conflicts |
| **Worktree** | `worktree/manager.py` | Git worktree lifecycle (create, detach, remove), branch management |
| **Display** | `display/live.py`, `display/status.py`, `display/tools.py` | Rich terminal UI with 4Hz refresh, dependency-layered tree, live action tailing, tool summaries |

---

## Module Map

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI argument parsing, command routing |
| `config.py` | Constants, path helpers, model escalation ladder |
| `spec/schema.py` | `ProjectSpec` and `TaskSpec` dataclasses |
| `spec/loader.py` | JSON spec loading and validation |
| `db/connection.py` | SQLite connection management |
| `db/models.py` | Schema DDL |
| `db/queries.py` | `SqliteTaskStore` (all DB operations) |
| `orchestrator/loop.py` | Main async orchestration loop |
| `orchestrator/merge.py` | Rebase merge with conflict resolution |
| `agent/launcher.py` | Claude CLI invocation |
| `agent/prompt_builder.py` | Prompt construction |
| `worktree/manager.py` | Git worktree lifecycle |
| `graph/resolver.py` | Dependency resolution and planning |
| `display/status.py` | Text-based status output |
| `display/live.py` | Rich terminal UI |
| `display/tools.py` | Human-readable tool_use block summaries |
| `init/generator.py` | Spec generation from English |
| `scaffold/generator.py` | Stub file generation |
| `docs/generator.py` | Documentation tree generation |
| `playground/generator.py` | Self-testing playground spec |

---

## Data Model

### ProjectSpec
```
project_name, description, tasks[], default_model, max_concurrency,
global_context, global_context_files[], user_stories[], version
```

### TaskSpec
```
id, description, dependencies[], context_files[], output_files[],
verification, priority, model, max_budget_usd, tags[]
```

### Task Statuses
`pending` → `running` → `completed` | `failed` | `cancelled` | `decomposed`

---

## Critical Design Decisions

- **Protocol-based architecture**: `TaskStore`, `AgentRunner`, `WorktreeProvider`, `MergeStrategy` are all protocols — enabling clean testing with mocks and future swappability
- **Git worktree isolation**: each task gets its own checkout, preventing interference between concurrent agents
- **Output file overlap filtering**: serializes tasks that touch the same files while parallelizing everything else
- **Dynamic decomposition**: agents can break tasks into subtasks at runtime by writing `.po-subtasks.json`
- **Merge conflict auto-resolution**: a separate Claude agent resolves rebase conflicts if they occur
- **Retry with state preservation**: failed task branches are kept so retry attempts can build on partial progress
- **Model escalation on retries**: when tasks retry, the model is automatically escalated up the ladder (haiku → sonnet → opus) to increase success probability; `--model` override disables escalation
- **macOS sleep prevention**: `caffeinate` is spawned during orchestration to prevent the machine from sleeping
- **SQLite WAL mode**: enables safe concurrent read/write access to the task database
- **Event callback system**: decouples orchestration from display (can emit events to different handlers)
- **Namespace subtask IDs**: `{parent_id}/{subtask_id}` prevents ID collisions between parent and child tasks
