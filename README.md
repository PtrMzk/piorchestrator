# piorchestrator

Orchestrate parallel Claude Code agents from a JSON spec.

## Install

```bash
uv tool install -e /path/to/piorchestrator
```

This makes `po` available globally from any directory. The `-e` flag means changes to the source take effect immediately without reinstalling.

## Usage

```bash
# 1. Write a spec file (see examples/todo-api.json)
# 2. Plan the execution
po plan myproject.json

# 3. Run it
po run

# 4. Monitor
po status
po cost
po logs <task-id>
```

## Development

```bash
uv sync          # install dependencies
uv run pytest    # run tests
uv run ruff check src tests  # lint
```

## License

MIT — see [LICENSE](LICENSE).
