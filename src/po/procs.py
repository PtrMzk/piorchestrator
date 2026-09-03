"""A registry of live child processes, so shutdown can actually kill them.

The orchestrator's SIGINT handler cancels asyncio tasks. That reaches the agent
subprocesses, which are awaited by those tasks. It does *not* reach anything
running in an executor thread: merges and verification commands block a worker
thread in `communicate()`, and cancelling the coroutine that awaits the executor
leaves the thread running. Worse, `asyncio.run()` joins the default executor on
the way out, so the interpreter then hangs waiting for the very work the user
asked to stop.

Every spawn registers here so the handler can kill it directly — `register()`
for the ones we own a `Popen` for, `register_pid()` for asyncio subprocesses,
whose own transport does the reaping.

All of it is process-group based, and that is the load-bearing part. Agents,
verification commands and merge agents all spawn their own children; signalling
only the process we started leaves those running, still holding the captured
pipes open, so `communicate()` blocks anyway and nothing is actually
interrupted. Callers must therefore spawn with `start_new_session=True`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Grace period between SIGTERM and SIGKILL when tearing a process group down.
_TERM_GRACE_S = 3.0
# How long to wait for a SIGKILLed child to be reaped.
_REAP_S = 5.0
# Grace period for pid-only groups, where there is no Popen to wait on.
_PID_GRACE_S = 1.0

_lock = threading.Lock()
_live: set[subprocess.Popen[str] | subprocess.Popen[bytes]] = set()
# Process groups tracked by pid alone, for spawns we do not own a Popen for —
# asyncio subprocesses, whose own transport reaps them.
_live_pids: set[int] = set()
_shutting_down = False


def signal_group(pid: int, sig: signal.Signals) -> bool:
    """Signal a process's whole group. False if it is already gone."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (AttributeError, OSError):
        # No process groups (Windows), or the process is already gone.
        return False
    return True


def group_alive(pgid: int) -> bool:
    """True if any process is still in the group, even if its leader has exited."""
    try:
        os.killpg(pgid, 0)
    except (AttributeError, OSError, TypeError):
        return False
    return True


def signal_pgid(pgid: int, sig: signal.Signals) -> bool:
    """Signal a process group by id. False if it has no members left."""
    try:
        os.killpg(pgid, sig)
    except (AttributeError, OSError, TypeError):
        return False
    return True


def _pgrp_of(pid: int) -> int | None:
    """Read a pid's process group from /proc. None if it can't be determined."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) is user-controlled and may contain spaces/parens; the
    # kernel guarantees it is wrapped in the *last* ')' on the line, per
    # proc(5). Fields after that are space-separated: state, ppid, pgrp, ...
    after_comm = raw.rsplit(")", 1)
    if len(after_comm) != 2:
        return None
    fields = after_comm[1].split()
    try:
        return int(fields[2])
    except (IndexError, ValueError):
        return None


def group_has_marker(pgid: int, token: str) -> bool:
    """True if a live process in `pgid` still carries our spawn marker.

    Signalling a process group by its bare numeric id is unsafe once the
    process we spawned may already have exited: the kernel is free to hand
    that same number to an unrelated new session (notably a fresh SSH login —
    sshd calls setsid() per connection, so its pgid equals its own pid). If we
    then `killpg` that number believing it's still ours, we tear down whatever
    that number now belongs to instead.

    We close that gap by tagging every spawned group with a unique token in
    its environment (inherited by every descendant) and refusing to signal
    unless a live process in the target pgid still carries it. Best-effort:
    Linux-only (needs /proc and read access to a process's own environ,
    which is only available for processes we own). Elsewhere, or if
    unreadable, this returns False — callers should treat that as "can't
    confirm it's ours," not "definitely gone."
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return False
    marker = token.encode()
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _pgrp_of(pid) != pgid:
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if marker in environ:
            return True
    return False


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


def register_pid(pid: int) -> None:
    """Track a process group we do not own a `Popen` for (asyncio subprocesses)."""
    with _lock:
        _live_pids.add(pid)


def unregister_pid(pid: int) -> None:
    """Stop tracking a pid. Safe to call more than once."""
    with _lock:
        _live_pids.discard(pid)


def kill_group(proc: subprocess.Popen[str] | subprocess.Popen[bytes]) -> None:
    """Kill a process and everything it spawned."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if proc.poll() is not None:
            return
        if not signal_group(proc.pid, sig):
            # No process group to signal; fall back to the process itself.
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
        pids = list(_live_pids)
        _live.clear()
        _live_pids.clear()
    for proc in victims:
        logger.debug("Killing process group for pid %d", proc.pid)
        kill_group(proc)
    for pid in pids:
        # No Popen to wait on — signal the group and let the owning task reap.
        logger.debug("Killing process group for pid %d", pid)
        if signal_group(pid, signal.SIGTERM):
            time.sleep(_PID_GRACE_S)
            signal_group(pid, signal.SIGKILL)
    return len(victims) + len(pids)


def reset() -> None:
    """Clear all state. For tests."""
    global _shutting_down
    with _lock:
        _live.clear()
        _live_pids.clear()
    _shutting_down = False
