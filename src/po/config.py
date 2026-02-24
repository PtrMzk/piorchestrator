"""Constants, defaults, and paths for PO."""

from __future__ import annotations

from pathlib import Path

# Default directory for PO state
PO_DIR = ".po"
STATE_DB = "state.db"
WORKTREES_DIR = "worktrees"
LOGS_DIR = "logs"

# Default concurrency
DEFAULT_MAX_CONCURRENCY = 3

# Default model
DEFAULT_MODEL = "sonnet"

# Default max budget per task in USD
DEFAULT_MAX_BUDGET_USD = 2.0

# Default max turns for agent
DEFAULT_MAX_TURNS = 50

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

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_DECOMPOSED,
})

# Task sources
SOURCE_SPEC = "spec"
SOURCE_RUNTIME = "runtime"


def po_dir(project_root: Path) -> Path:
    """Return the .po directory for a project."""
    return project_root / PO_DIR


def state_db_path(project_root: Path) -> Path:
    """Return the path to the state database."""
    return po_dir(project_root) / STATE_DB


def worktrees_dir(project_root: Path) -> Path:
    """Return the path to the worktrees directory."""
    return po_dir(project_root) / WORKTREES_DIR


def logs_dir(project_root: Path) -> Path:
    """Return the path to the logs directory."""
    return po_dir(project_root) / LOGS_DIR
