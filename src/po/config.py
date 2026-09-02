"""Constants, defaults, and paths for PO."""

from __future__ import annotations

import os
from pathlib import Path

# Environment variables that mark an already-running Claude Code session. These
# must not leak into a spawned agent, or the child mistakes itself for a nested
# session. Everything else — notably CLAUDE_CONFIG_DIR and
# CLAUDE_CODE_OAUTH_TOKEN — carries authentication and must survive: stripping
# the whole CLAUDE* prefix makes the child start logged out.
NESTED_SESSION_ENV_VARS = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
    }
)


def agent_env() -> dict[str, str]:
    """Return the environment for a spawned `claude` process.

    Drops only the nesting markers, preserving auth and config.
    """
    return {k: v for k, v in os.environ.items() if k not in NESTED_SESSION_ENV_VARS}


# Default directory for PO state
PO_DIR = ".po"
STATE_DB = "state.db"
WORKTREES_DIR = "worktrees"
LOGS_DIR = "logs"

# Default concurrency
DEFAULT_MAX_CONCURRENCY = 5

# Default model for task agents. Single source of truth: `spec/schema.py`
# reads this for both `TaskSpec.model` and `ProjectSpec.default_model`.
DEFAULT_MODEL = "sonnet"

# Model escalation ladder (weakest → strongest)
MODEL_LADDER = ["haiku", "sonnet", "opus"]

# Default max budget per task in USD
DEFAULT_MAX_BUDGET_USD = 2.0

# Default max turns for agent
DEFAULT_MAX_TURNS = 50

# Hard timeout for a task's verification command, in seconds. Generous enough
# for a real test suite, short enough that a command which never exits (a dev
# server named as `verification`) cannot wedge the run overnight.
DEFAULT_VERIFICATION_TIMEOUT_S = 600.0

# Patterns seeded into the project's .gitignore. Committed once at the start of
# `po run`, before any task branch is cut — see OrchestratorLoop._prepare_repo.
GITIGNORE_PATTERNS = [f"{PO_DIR}/", "node_modules/", "dist/", "build/"]

# Subtask/failure file names (written by agents)
SUBTASKS_FILE = ".po-subtasks.json"
FAILURE_FILE = ".po-failure.json"

# Task statuses
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_DECOMPOSED = "decomposed"

TERMINAL_STATUSES = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_DECOMPOSED,
    }
)

# Task sources
SOURCE_SPEC = "spec"
SOURCE_RUNTIME = "runtime"


def escalate_model(base_model: str, attempt: int) -> str:
    """Return the model to use for a given retry attempt.

    attempt 1 → base model, attempt 2 → next up the ladder, attempt 3+ → opus.
    Models not in the ladder are returned unchanged.
    """
    if base_model not in MODEL_LADDER:
        return base_model
    base_idx = MODEL_LADDER.index(base_model)
    target_idx = min(base_idx + attempt - 1, len(MODEL_LADDER) - 1)
    return MODEL_LADDER[target_idx]


def po_dir(project_root: Path) -> Path:
    """Return the .po directory for a project."""
    return project_root / PO_DIR


def ensure_gitignore(project_root: Path) -> bool:
    """Ensure the project's .gitignore covers po state and build artifacts.

    Idempotent: no-ops if every pattern is already present. Returns True if the
    file was changed.
    """
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""

    missing = [p for p in GITIGNORE_PATTERNS if p not in existing]
    if not missing:
        return False

    lines = existing.rstrip("\n")
    if lines:
        lines += "\n"
    lines += "\n".join(missing) + "\n"
    gitignore.write_text(lines)
    return True


def state_db_path(project_root: Path) -> Path:
    """Return the path to the state database."""
    return po_dir(project_root) / STATE_DB


def worktrees_dir(project_root: Path) -> Path:
    """Return the path to the worktrees directory."""
    return po_dir(project_root) / WORKTREES_DIR


def logs_dir(project_root: Path) -> Path:
    """Return the path to the logs directory."""
    return po_dir(project_root) / LOGS_DIR


def ensure_logs_dir(project_root: Path) -> Path:
    """Create the logs directory with restricted permissions (0o700).

    Returns the log directory path.
    """
    log_dir = logs_dir(project_root)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.chmod(0o700)
    return log_dir
