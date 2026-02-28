"""Constants, defaults, and paths for PO."""

from __future__ import annotations

from pathlib import Path

# Default directory for PO state
PO_DIR = ".po"
STATE_DB = "state.db"
WORKTREES_DIR = "worktrees"
LOGS_DIR = "logs"

# Default concurrency
DEFAULT_MAX_CONCURRENCY = 5

# Default model for task agents
DEFAULT_MODEL = "haiku"

# Model escalation ladder (weakest → strongest)
MODEL_LADDER = ["haiku", "sonnet", "opus"]

# Default max budget per task in USD
DEFAULT_MAX_BUDGET_USD = 2.0

# Default max turns for agent
DEFAULT_MAX_TURNS = 50

# Subtask/failure file names (written by agents)
SUBTASKS_FILE = ".po-subtasks.json"
FAILURE_FILE = ".po-failure.json"

# Sandbox defaults
SANDBOX_IMAGE_NAME = "po-agent:latest"
SANDBOX_API_HOST = "api.anthropic.com"
SANDBOX_REGISTRY_HOSTS = ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org"]

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


def ensure_po_gitignore(project_root: Path) -> None:
    """Ensure .po/ is listed in the project's .gitignore.

    Idempotent: no-ops if .po/ is already present.
    """
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if ".po/" in existing:
        return
    lines = existing.rstrip("\n")
    if lines:
        lines += "\n"
    lines += ".po/\n"
    gitignore.write_text(lines)


def state_db_path(project_root: Path) -> Path:
    """Return the path to the state database."""
    return po_dir(project_root) / STATE_DB


def worktrees_dir(project_root: Path) -> Path:
    """Return the path to the worktrees directory."""
    return po_dir(project_root) / WORKTREES_DIR


def logs_dir(project_root: Path) -> Path:
    """Return the path to the logs directory."""
    return po_dir(project_root) / LOGS_DIR
