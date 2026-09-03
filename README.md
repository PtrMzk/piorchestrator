# piorchestrator

Piorchestrator (`po`) orchestrates parallel [Claude Code](https://claude.com/claude-code)
agents to build a whole project from a spec — a plain-English description, or a JSON file
listing tasks and their dependencies. Each task runs in its own isolated git worktree; tasks
whose dependencies are satisfied and whose output files don't overlap run concurrently, then
get rebased and merged onto `main` as they finish.

Point it at an empty directory with a description, and it plans a dependency graph, runs every
layer of it with real Claude Code agents, and leaves you a project with every task's work
merged in and verified — retrying failures, escalating to a stronger model on retry, and
resuming cleanly if you stop and re-run it.

## Install

```bash
uv tool install -e /path/to/piorchestrator
```

This makes `po` available globally from any directory. The `-e` flag means changes to the
source take effect immediately without reinstalling. Requires the [Claude Code CLI](https://claude.com/claude-code)
(`claude`) to be installed and authenticated.

## Usage

```bash
# 1. Generate a spec from a plain-English description (or write JSON by hand — see examples/todo-api.json)
po init "REST API for a todo list using TypeScript on Bun"

# 2. Run it — spins up agents in parallel git worktrees, merges as tasks complete
po run

# 3. Monitor
po status
po cost
po logs <task-id>
```

If a task fails, `po run` retries it (escalating the model up the ladder on each retry) and
cancels anything that depended on it. Once you've fixed the underlying issue — or just want
another shot — `po reset` puts failed/cancelled tasks back to `pending`, and `po run` picks up
each one from its existing worktree and branch instead of starting over.

Other commands:

```bash
po scan                    # generate documentation for an existing codebase
po clean                   # remove worktrees left behind by finished/abandoned tasks
po reset [--task ID]       # reset failed/cancelled tasks (all, or one) back to pending
```

## How it works

- **Dependency graph, not a linear queue.** Tasks declare `dependencies` and `output_files`;
  `po` groups them into layers and runs everything in a layer concurrently, serializing only
  tasks that would write the same file.
- **Isolated git worktrees.** Every task gets its own worktree and branch (`po/<task-id>`), so
  concurrent agents never see each other's uncommitted state. A task's branch is rebased onto
  `main` and fast-forward merged once its agent finishes and its `verification` command passes.
- **Retries with model escalation.** A failed task retries with the model ladder stepped up
  (`haiku → sonnet → opus`) instead of hammering the same model that just failed.
- **Resumable.** A task's worktree and branch are never thrown away on failure — a retry, or
  `po reset` + `po run` after you've stepped in, continues from whatever the agent already did.

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full code walkthrough of every command.

## Development

```bash
uv sync                          # install dependencies
uv run pytest                    # run tests
uv run ruff check src tests      # lint
uv run ruff format src tests     # format
uv run mypy src                  # type-check (strict)
```

## License

MIT — see [LICENSE](LICENSE).
