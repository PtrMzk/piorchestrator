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

---

## Phase 2 — Feature Work

### ~~29. Enforce TDD in spec creation~~ — DONE
Added TDD constraints to init prompt (test files in output_files, "write failing tests
first" descriptions, pytest verification commands). Updated example spec to demonstrate
TDD pattern. Added TDD rule to agent prompt builder. Tests updated and passing.

**Files:** `src/po/init/generator.py`, `src/po/agent/prompt_builder.py`

### ~~30. Tailor spec generation for TypeScript/Bun UI apps~~ — DONE
Rewrote `_EXAMPLE_SPEC` and `examples/todo-api.json` to target TypeScript on Bun with
built-in APIs (Bun.serve, bun:sqlite). Added Bun/TypeScript constraints to the init
prompt. Updated test assertions for new keywords.

**Files:** `src/po/init/generator.py`, `examples/todo-api.json`, `tests/test_init.py`

### ~~31. User stories in specs with Playwright verification agents~~ — DONE
Added `user_stories` field to `ProjectSpec` schema. Updated `_EXAMPLE_SPEC` and init prompt
to generate Playwright e2e tasks for each user story. Updated `examples/todo-api.json` with
2 user stories and 2 e2e tasks. All tests passing.

**Files:** `src/po/spec/schema.py`, `src/po/init/generator.py`, `examples/todo-api.json`

### ~~32. Rich terminal UI with agent tree and live status~~ — DONE
Added `rich>=13.0` as first runtime dependency. Created `src/po/display/live.py` with
`LiveDisplay` class that renders a Rich `Tree` with styled task nodes (status symbols,
colors, spinners), reads last agent action from JSONL log tails, and uses `rich.live.Live`
for 4Hz screen refresh. CLI detects TTY and uses LiveDisplay for terminals, falling back
to `_live_event_printer` for piped output. 18 new tests covering tree building, event
handling, log tail parsing, status styles, and TTY detection.

**Files:** `pyproject.toml`, `src/po/display/live.py`, `src/po/cli.py`, `tests/test_display.py`, `tests/test_cli.py`

### ~~34. Dependency-layered tree in LiveDisplay~~ — DONE
`_build_tree()` now computes BFS layers from task dependencies and renders each layer as
a labeled branch (`Layer 0`, `Layer 1`, ...). Tasks with no deps appear in Layer 0,
dependent tasks in subsequent layers. Subtasks still nest under their parent within the
parent's layer. Dynamic tasks with unknown deps fall into a fallback group. Three new
tests added for layer grouping, labels, and updated node counts.

**Files:** `src/po/display/live.py`, `tests/test_display.py`

### ~~33. Auto-generate documentation tasks in specs~~ — DONE
Added doc companion task constraints to init prompt and `_EXAMPLE_SPEC` (doc-models,
doc-db, doc-routes). Doc tasks use sonnet model, 0.50 budget, ["docs"] tag, and write to
`docs/<feature>.md`. Downstream implementation and e2e tasks include relevant doc files in
`context_files`. Updated `examples/todo-api.json` with matching doc tasks. Added prompt
assertion test.

**Files:** `src/po/init/generator.py`, `examples/todo-api.json`, `tests/test_init.py`

### ~~35. Level 3 e2e tests with scripted agent~~ — DONE
Created `ScriptedAgentRunner` that writes real files and makes real git commits in real
worktrees, replacing only the Claude subprocess. Six e2e test scenarios cover linear chains,
diamond DAGs, failure cascading, subtask decomposition, agent failure retry with error
context, and event callback ordering. Fixed two bugs found during testing: worktree cleanup
on agent failure without retries, and error message propagation for agent failure retries.

**Files:** `tests/e2e_support.py`, `tests/test_e2e.py`, `src/po/orchestrator/loop.py`

### ~~36. Combine po init + po plan into one step~~ — DONE
`po init` now auto-runs `cmd_plan` after spec generation (validation, execution plan display,
DB persistence, scaffold, docs). Interactive terminals get a two-phase flow: outline review
with user feedback loop, then full spec generation from the approved outline. Non-interactive
mode falls back to direct spec generation. `po plan` now defaults `--scaffold` and
`--generate-docs` to on (use `--no-scaffold`/`--no-generate-docs` to opt out). Added
`generate_outline()`, `generate_spec_from_outline()`, and `_build_outline_prompt()` to the
generator. 12 new tests for outline generation and spec-from-outline flow.

**Files:** `src/po/cli.py`, `src/po/init/generator.py`, `tests/test_init.py`

### ~~37. Improve `po init` streaming display and session reuse~~ — DONE
`_invoke_claude()` now uses a `rich.status.Status` spinner that updates in-place with
human-readable tool summaries (via new `display/tools.py:tool_summary()` helper) instead
of printing a new line per tool call. Session reuse: `_invoke_claude()` captures and returns
`session_id` from the result message; `generate_outline()` accepts/returns `session_id`;
CLI feedback loop passes `session_id` back so revisions resume the same Claude session via
`--resume`. `LiveDisplay._read_last_action()` also uses `tool_summary()` for richer labels.
14 new tests for `tool_summary`, updated existing tests for new tuple return signatures.

**Files:** `src/po/display/tools.py` (new), `src/po/init/generator.py`, `src/po/cli.py`, `src/po/display/live.py`, `tests/test_display.py`, `tests/test_init.py`

### ~~38. Model escalation on retries~~ — DONE
Added `MODEL_LADDER` constant and `escalate_model(base_model, attempt)` pure function to
`config.py`. When tasks retry (attempt > 1) and no `--model` override is set, the
orchestrator escalates the model up the ladder (haiku → sonnet → opus). Emits a
`model_escalated` event when the model changes. Added `⬆` and `⊘` symbols to the CLI
event printer. 13 new tests covering the ladder function and integration scenarios.

**Files:** `src/po/config.py`, `src/po/orchestrator/loop.py`, `src/po/cli.py`, `tests/test_escalation.py` (new)

### ~~40. Docker-based agent sandboxing~~ — DONE
Added `SandboxProvider` protocol with `NoSandbox` (passthrough, default) and `DockerSandbox`
implementations. `DockerSandbox` wraps agent commands in `docker run` with: project root
mounted at the same absolute path (preserving worktree references), iptables firewall allowing
only `api.anthropic.com:443`, tmpfs for `/tmp` and `/home/agent`, IPv6 disabled. Entrypoint
runs as root for iptables setup then drops to non-root `agent` user via `su-exec`. Opt-in via
`po run --sandbox`. 15 new tests covering protocol, DNS resolution, Docker checks, image
building, and command wrapping.

**Files:** `src/po/sandbox/__init__.py` (new), `src/po/sandbox/provider.py` (new),
`src/po/sandbox/docker.py` (new), `src/po/sandbox/Dockerfile` (new),
`src/po/sandbox/entrypoint.sh` (new), `src/po/agent/launcher.py`, `src/po/orchestrator/loop.py`,
`src/po/cli.py`, `src/po/config.py`, `pyproject.toml`, `tests/test_sandbox.py` (new)

**Follow-up fix:** Docker sandbox onboarding bypass, Python runtime, and package registry access.
Added Python 3 + uv and ripgrep + musl compat libs to Dockerfile. Pre-seeded `~/.claude.json`
with `hasCompletedOnboarding: true` to prevent interactive onboarding hang. Generalized
`_resolve_api_ips()` → `_resolve_hosts()` to resolve multiple hostnames (API + package registries).
Entrypoint now allows DNS (port 53) and matches registry hosts in `/etc/hosts` for iptables rules.
Added `SANDBOX_REGISTRY_HOSTS` config constant and `CLAUDE_CONFIG_DIR`/`NODE_OPTIONS`/`USE_BUILTIN_RIPGREP`
env vars in entrypoint.

### ~~39. Codebase documentation scan~~ — DONE
Added `po scan` command that invokes Claude to analyze an existing codebase and generate
nested documentation under `docs/codebase/` (configurable via `--output-dir`). Created
`src/po/scan/` module with `scan_codebase()`, `_build_scan_prompt()`, and
`_invoke_scan_agent()`. Integrated codebase docs into spec generation: `_detect_codebase_docs()`
checks for pre-scanned docs and appends instructions to outline, spec-from-outline, and
init prompts telling Claude to wire docs into `global_context_files` and per-task
`context_files`. 16 new tests covering prompt building and codebase doc detection/integration.

**Files:** `src/po/scan/__init__.py` (new), `src/po/scan/scanner.py` (new), `src/po/cli.py`, `src/po/init/generator.py`, `tests/test_scan.py` (new), `tests/test_init.py`

### ~~41. Replace Docker sandbox with macOS sandbox-exec~~ — DONE
Added `SeatbeltSandbox` using macOS's `sandbox-exec` as an alternative sandbox provider.
Kept as a lighter option for when Docker isn't available. 16 new seatbelt tests.

**Files:** `src/po/sandbox/seatbelt.py` (new), `src/po/sandbox/__init__.py`,
`src/po/cli.py`, `tests/test_sandbox.py`

### ~~42. Fix Docker sandbox auth with named volume + one-time login~~ — DONE
Replaced host-mount auth approach (which couldn't access macOS Keychain OAuth tokens) with
a named Docker volume (`po-claude-auth`). On first `po run`, if the volume has no credentials,
launches an interactive `docker run -it` container for `claude /login`. The user completes
the OAuth flow once; credentials persist in the volume across all subsequent container runs.
Removed all `.claude-host` staging logic from `docker.py` and `entrypoint.sh`. Removed
`--tmpfs /home/agent` (conflicted with volume mount). Docker sandbox restored as default.
6 new auth volume tests, 377 total tests passing.

**Files:** `src/po/sandbox/docker.py`, `src/po/sandbox/entrypoint.sh`, `src/po/cli.py`,
`src/po/config.py`, `tests/test_sandbox.py`

### ~~43. Harden Docker sandbox firewall~~ — DONE
Applied lessons from Claude Code's official devcontainer. Entrypoint now uses `set -euo pipefail`,
fails hard if no allowed IPs found (prevents running without isolation), validates each IP from
`/etc/hosts` against a regex, sets iptables default policy to DROP (not trailing REJECT),
adds `ip6tables -P DROP` on all chains as belt-and-suspenders alongside sysctl disable, and
verifies the firewall by testing that `example.com` is blocked and `api.anthropic.com` is
reachable before starting the agent. Dockerfile adds `curl` (for verification) and `ip6tables`.
`docker.py` adds `--cap-add=NET_RAW` (needed for curl in entrypoint) and validates DNS-resolved
IPs against a regex before passing them to `--add-host`. 377 tests passing.

**Files:** `src/po/sandbox/entrypoint.sh`, `src/po/sandbox/Dockerfile`,
`src/po/sandbox/docker.py`, `tests/test_sandbox.py`

---

## Phase 3 — Security Hardening & Cleanup

### ~~44. Remove macOS seatbelt sandbox code~~ — DONE

Deleted `src/po/sandbox/seatbelt.py`, removed `SeatbeltSandbox` export from
`src/po/sandbox/__init__.py`, removed 16 seatbelt tests from `tests/test_sandbox.py`,
and cleaned up all references in `ARCHITECTURE.md`.

### ~~45. Remove unused/dead code across the codebase~~ — DONE
Audited all source files. Removed 3 unused methods from `SqliteTaskStore`:
`get_tasks_by_status()`, `get_total_cost()`, `get_status_counts()`. Removed
corresponding dead tests. Updated e2e test to compute cost inline.

### ~~46. Fix shell injection in verification command execution~~ — DONE
Replaced `shell=True` with `shlex.split()` in both `merge.py:_run_verification()` and
`loop.py:_run_verification()`. Verification commands are now parsed safely without shell
interpretation.

### ~~47. Prevent prompt injection via context files~~ — DONE
Added `_escape_backticks()` helper that escapes triple-backtick sequences in context file
content and previous error messages before embedding in prompts. 3 new tests.

### ~~48. Restrict database and log file permissions~~ — DONE
`get_connection()` now sets `0o600` on the DB file after creation. Added `ensure_logs_dir()`
helper in `config.py` that creates `.po/logs/` with `0o700`. All callers updated to use it.

### ~~49. Narrow git safe.directory in Docker entrypoint~~ — DONE
`docker.py` now passes `PO_PROJECT_ROOT` env var to the container. `entrypoint.sh` scopes
`safe.directory` to the project root and working directory instead of wildcard `*`.

### 50. Remove redundant ANTHROPIC_API_KEY pass-through in Docker sandbox

`docker.py` passes `ANTHROPIC_API_KEY` as an environment variable to the container, but auth is
already handled via the named Docker volume with OAuth credentials. The env var is redundant and
exposes the key via `docker inspect` / `/proc/*/environ`. Remove it unless it serves as a
required fallback.

### 51. Add spec size limits

`spec/schema.py` has no upper bounds on the number of tasks, dependencies, context files, or
output files. A malicious or accidental spec could cause OOM. Add reasonable limits (e.g., max
1,000 tasks, max 50 dependencies per task).

### 52. Add noexec to Docker tmpfs mount

`docker.py` mounts `--tmpfs /tmp:size=1G` without `noexec`. Adding `noexec` prevents agents
from compiling and executing arbitrary binaries from `/tmp` inside the container.
