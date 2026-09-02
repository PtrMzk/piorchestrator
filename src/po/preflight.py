"""Checks that must pass before `po run` touches the repository.

Each check corresponds to a failure that used to surface late and badly: as a
raw traceback from `git commit`, as every task failing with "claude not found"
after burning its retries, or — worst — as uncommitted work silently discarded
by the merge's `git checkout -f`. Running them up front costs a few
subprocess calls and turns each into one sentence the user can act on.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path

# po's own file. `po plan` writes it and `OrchestratorLoop._prepare_repo`
# commits it (with a pathspec, nothing else) before the first branch is cut,
# which happens *after* pre-flight. Flagging it here blames the user for a file
# po created seconds earlier.
_OWN_FILES = frozenset({".gitignore"})


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _is_git_repo(project_root: Path) -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"], project_root).returncode == 0


def check_claude_on_path() -> str | None:
    """The agents, the merge agent, `po init` and `po scan` all exec `claude`."""
    if shutil.which("claude"):
        return None
    return (
        "Claude CLI not found: 'claude' is not on PATH. "
        "Install Claude Code (https://claude.com/claude-code) and try again."
    )


def check_git_identity(project_root: Path) -> str | None:
    """po commits on the user's behalf (initial commit, .gitignore, merges).

    `git var GIT_AUTHOR_IDENT` resolves the identity the way `git commit`
    would — config at every level plus the GIT_AUTHOR_* variables — and
    exits non-zero with "Author identity unknown" when there is none. It
    works outside a repository too, which matters for a fresh directory.
    """
    result = _git(["var", "GIT_AUTHOR_IDENT"], project_root)
    if result.returncode == 0:
        return None
    return (
        "Git identity is not configured, so po cannot commit. Run:\n"
        '  git config --global user.name "Your Name"\n'
        '  git config --global user.email "you@example.com"'
    )


def check_clean_worktree(project_root: Path) -> str | None:
    """Uncommitted changes to tracked files would be destroyed by the merge.

    `RebaseMerger` runs `git checkout -f <base>` before every merge and
    `git reset --hard` after a failed verification. Both discard local
    modifications without asking. Untracked files are left alone by both,
    so they are not part of this check — see `check_output_collisions`.
    """
    if not _is_git_repo(project_root):
        return None
    result = _git(["status", "--porcelain", "--untracked-files=no"], project_root)
    dirty = [
        line for line in result.stdout.splitlines()
        if line.strip() and line[3:].strip() not in _OWN_FILES
    ]
    if not dirty:
        return None
    shown = "\n".join(f"  {line}" for line in dirty[:10])
    more = f"\n  ... and {len(dirty) - 10} more" if len(dirty) > 10 else ""
    return (
        "Working tree has uncommitted changes that 'po run' would discard "
        "(the merge runs 'git checkout -f'). Commit or stash them first:\n"
        f"{shown}{more}"
    )


def check_output_collisions(
    project_root: Path, output_files: Iterable[str],
) -> str | None:
    """An untracked file at an agent's output path makes git refuse the merge.

    The agent commits that path on its branch; checking the branch out over
    the untracked file would overwrite it, so git refuses, and the task fails
    after a full agent run. Scaffold stubs are the usual culprit.
    """
    if not _is_git_repo(project_root):
        return None
    wanted = {f for f in output_files if f} - _OWN_FILES
    if not wanted:
        return None
    result = _git(["ls-files", "--others", "--exclude-standard"], project_root)
    untracked = set(result.stdout.splitlines())
    colliding = sorted(wanted & untracked)
    if not colliding:
        return None
    shown = "\n".join(f"  {f}" for f in colliding[:10])
    more = f"\n  ... and {len(colliding) - 10} more" if len(colliding) > 10 else ""
    return (
        "Untracked files match task output files and would block their merge. "
        "Commit or remove them first:\n"
        f"{shown}{more}"
    )


def run_preflight(
    project_root: Path,
    output_files: Iterable[str],
    allow_dirty: bool = False,
) -> list[str]:
    """Return every problem found, in the order a user should fix them."""
    problems: list[str] = []
    for check in (
        check_claude_on_path(),
        check_git_identity(project_root),
        None if allow_dirty else check_clean_worktree(project_root),
        check_output_collisions(project_root, output_files),
    ):
        if check:
            problems.append(check)
    return problems
