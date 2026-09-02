"""Running task verification commands under a hard timeout."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from po import procs
from po.config import DEFAULT_VERIFICATION_TIMEOUT_S

logger = logging.getLogger(__name__)

# Verification output can be enormous; keep error messages readable.
_DETAIL_CHARS = 500


@dataclass
class VerificationOutcome:
    """Result of running a verification command."""

    ok: bool
    detail: str = ""
    cancelled: bool = False


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
    """Run `command` in `cwd` **through a shell**, log its output, enforce `timeout`.

    The shell is load-bearing, not a convenience. Specs overwhelmingly write
    verification as a compound gate — `npx tsc --noEmit && npm run build`,
    `uv run pytest && uv run ruff check src`. Splitting that with shlex and
    exec'ing the argv directly hands `&&` and everything after it to the *first*
    program as positional arguments. tsc then reports TS5112 ("files specified
    on commandline") and the task fails for a reason that has nothing to do with
    its code. Worse, when the first program tolerates junk argv the run *passes*:
    `true && echo hi` exits 0 having never echoed, so half the gate silently
    never ran and the task merges green.

    Specs routinely also name verification commands that never exit on their own
    — `npm run dev`, `vite`, a stray `playwright test --ui`. Without a timeout one
    of those wedges the run permanently, and with nothing on screen, because the
    output is captured. The command gets its own process group so the timeout
    kills the whole tree: killing just the shell we spawn would leave the server
    it started holding the pipes open, and we would block on the read instead.
    """
    if not command.strip():
        return VerificationOutcome(ok=True)
    if procs.is_shutting_down():
        return VerificationOutcome(ok=False, detail="cancelled", cancelled=True)

    timed_out = False
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        # Raising here would take down the whole orchestrator, since callers run
        # inside an executor. Under a shell an unknown command comes back as exit
        # 127 rather than an exception; this catches the shell itself being
        # unavailable, which is still a failed verification and not a crash.
        detail = f"could not run verification command: {exc}"
        log_file.write_text(f"Command: {command}\n{detail}\n")
        return VerificationOutcome(ok=False, detail=detail)

    procs.register(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        logger.warning(
            "Verification command timed out after %.0fs, killing: %s",
            timeout,
            command,
        )
        procs.kill_group(proc)
        stdout, stderr = proc.communicate()
    finally:
        procs.unregister(proc)

    # A shutdown kills the process group out from under communicate(), which
    # then returns a spurious non-zero exit. Report that as cancelled, not as
    # a verification failure that would send the task back for a retry.
    if procs.is_shutting_down():
        return VerificationOutcome(ok=False, detail="cancelled", cancelled=True)

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
        ok=False,
        detail=_tail(stderr) or _tail(stdout) or "(no output)",
    )
