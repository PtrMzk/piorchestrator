# Piorchestrator Architecture

> **Auto-maintained:** This document is kept in sync with the codebase after every commit.

Piorchestrator (`po`) is a **multi-agent orchestrator** that coordinates parallel Claude Code agents to execute complex software development projects. It takes a JSON specification describing a project broken into tasks with dependencies, then runs multiple Claude agents concurrently — each in an isolated git worktree — to build the project in parallel.

---

## Major Entry Points (CLI Commands)

### 1. `po init <description>` — Spec Generation
Converts a plain-English project description into a structured JSON spec by prompting Claude. The generated spec includes tasks, dependencies, output files, and verification commands. It enforces conventions like TDD (tests in output files), doc companion tasks, and a DAG structure.

### 2. `po plan <spec.json>` — Validation & Planning
Loads and validates a spec (checks for duplicate IDs, cycles, missing dependencies), persists it to a SQLite database (`.po/state.db`), and displays the execution plan as dependency layers. Optional flags:
- `--scaffold` — generates stub files for all `output_files` with language-aware placeholder comments
- `--generate-docs` — creates a documentation tree (`CLAUDE.md`, `SYSTEM_DESIGN.md`, component docs)
- `--playground` — generates a self-testing calculator spec to demo the tool

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

### 4. `po status` / `po cost` / `po logs <id>` — Monitoring
- **status**: table of all tasks with their state, cost, and any error messages
- **cost**: per-task and total cost summary
- **logs**: streams the agent's JSONL log (supports `--raw` and `--tail`)

### 5. `po reset [--task ID]` — Recovery
Resets `failed`/`cancelled` tasks back to `pending`, cascading to dependents. Preserves the git branch so retry agents can build on previous work.

### 6. `po clean` — Cleanup
Removes orphaned git worktrees that weren't properly cleaned up.

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
| **Display** | `display/live.py`, `display/status.py` | Rich terminal UI with 4Hz refresh, dependency-layered tree, live action tailing |

---

## Module Map

| Module | Purpose |
|--------|---------|
| `cli.py` | CLI argument parsing, command routing |
| `config.py` | Constants and path helpers |
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
- **macOS sleep prevention**: `caffeinate` is spawned during orchestration to prevent the machine from sleeping
- **SQLite WAL mode**: enables safe concurrent read/write access to the task database
- **Event callback system**: decouples orchestration from display (can emit events to different handlers)
- **Namespace subtask IDs**: `{parent_id}/{subtask_id}` prevents ID collisions between parent and child tasks
