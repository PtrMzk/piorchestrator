"""Running task verification commands under a hard timeout."""

from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from po.config import DEFAULT_VERIFICATION_TIMEOUT_S

logger = logging.getLogger(__name__)

# Verification output can be enormous; keep error messages readable.
_DETAIL_CHARS = 500


@dataclass
class VerificationOutcome:
    """Result of running a verification command."""

    ok: bool
    detail: str = ""


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command and everything it spawned."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, OSError):
        # No process groups (Windows), or the process is already gone.
        proc.kill()


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) > _DETAIL_CHARS:
        return "..." + text[-_DETAIL_CHARS:]
    return text


def run_verification(
    command: str,
    cwd: Path,
    log_file: Path,
    timeout: float = DEFAULT_VERIFICATION_TIMEOUT_S,
) -> VerificationOutcome:
    """Run `command` in `cwd`, log its full output, and enforce `timeout`.

    Specs routinely name verification commands that never exit on their own —
    `npm run dev`, `vite`, a stray `playwright test --ui`. Without a timeout one
    of those wedges the run permanently, and with nothing on screen, because the
    output is captured. The command gets its own process group so the timeout
    kills the whole tree: killing just the shell we spawned would leave the
    server it started holding the pipes open, and we would block on the read
    instead.
    """
    argv = shlex.split(command)
    if not argv:
        return VerificationOutcome(ok=True)

    timed_out = False
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        # Raising here would take down the whole orchestrator, since callers run
        # inside an executor. A bad command is just a failed verification.
        detail = f"could not run verification command: {exc}"
        log_file.write_text(f"Command: {command}\n{detail}\n")
        return VerificationOutcome(ok=False, detail=detail)

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning(
            "Verification command timed out after %.0fs, killing: %s",
            timeout, command,
        )
        _kill_process_group(proc)
        stdout, stderr = proc.communicate()

    exit_code = f"timed out after {timeout:.0f}s" if timed_out else str(proc.returncode)
    with open(log_file, "w") as fh:
        fh.write(f"Command: {command}\n")
        fh.write(f"Exit code: {exit_code}\n")
        fh.write(f"--- stdout ---\n{stdout}\n")
        fh.write(f"--- stderr ---\n{stderr}\n")

    if timed_out:
        return VerificationOutcome(
            ok=False,
            detail=f"timed out after {timeout:.0f}s "
                   f"(last output: {_tail(stderr) or _tail(stdout) or 'none'})",
        )
    if proc.returncode == 0:
        return VerificationOutcome(ok=True)

    return VerificationOutcome(
        ok=False, detail=_tail(stderr) or _tail(stdout) or "(no output)",
    )
