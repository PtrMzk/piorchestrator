"""A registry of live child processes, so shutdown can actually kill them.

The orchestrator's SIGINT handler cancels asyncio tasks. That is enough for the
agent subprocesses — they are asyncio subprocesses awaited by those tasks, and
`ClaudeCodeRunner` terminates them on `CancelledError`. It is *not* enough for
anything running in an executor thread: merges and verification commands block
a worker thread in `communicate()`, and cancelling the coroutine that awaits the
executor leaves the thread running. Worse, `asyncio.run()` joins the default
executor on the way out, so the interpreter then hangs waiting for the very work
the user asked to stop.

Spawns from those paths register here, so the signal handler can kill them
directly. Registration is process-group based: verification commands and merge
agents start their own trees, and killing only the process we spawned leaves
its children holding the captured pipes open — so `communicate()` blocks anyway
and nothing is actually interrupted.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading

logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing a process group down.
_TERM_GRACE_S = 3.0
# How long to wait for a SIGKILLed child to be reaped.
_REAP_S = 5.0

_lock = threading.Lock()
_live: set[subprocess.Popen[str] | subprocess.Popen[bytes]] = set()
_shutting_down = False


def is_shutting_down() -> bool:
    """True once `shutdown()` has been called."""
    return _shutting_down


def register(proc: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Track a child process for the duration of its life.

    Callers must spawn with ``start_new_session=True`` so the process is the
    leader of its own group; see the module docstring.
    """
    with _lock:
        _live.add(proc)


def unregister(proc: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Stop tracking a child process. Safe to call more than once."""
    with _lock:
        _live.discard(proc)


def kill_group(proc: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Kill a process and everything it spawned."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (AttributeError, OSError):
            # No process groups (Windows), or the process is already gone.
            with contextlib.suppress(OSError):
                proc.kill()
            return
        if sig is signal.SIGTERM:
            try:
                proc.wait(timeout=_TERM_GRACE_S)
                return
            except subprocess.TimeoutExpired:
                logger.debug("pid %d ignored SIGTERM, escalating", proc.pid)
        else:
            # Reap it, or the killed child lingers as a zombie for the rest of
            # the run — and `poll()` keeps reporting it as alive.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=_REAP_S)


def shutdown() -> int:
    """Kill every tracked process. Returns how many were still running.

    Idempotent: later calls kill whatever registered in the meantime, which is
    what makes a second Ctrl-C during cleanup do something useful.
    """
    global _shutting_down
    _shutting_down = True
    with _lock:
        victims = list(_live)
        _live.clear()
    for proc in victims:
        logger.debug("Killing process group for pid %d", proc.pid)
        kill_group(proc)
    return len(victims)


def reset() -> None:
    """Clear all state. For tests."""
    global _shutting_down
    with _lock:
        _live.clear()
    _shutting_down = False
