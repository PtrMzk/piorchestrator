"""Tests for po.procs — the live child process registry used by shutdown."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from po import procs


def _spawn(code: str) -> subprocess.Popen[str]:
    """Spawn a tracked-style child in its own process group."""
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestRegistry:
    def test_shutdown_kills_registered_process(self) -> None:
        proc = _spawn("import time; time.sleep(60)")
        procs.register(proc)

        assert procs.shutdown() == 1

        assert _wait_gone(proc.pid)
        assert procs.is_shutting_down() is True

    def test_unregistered_process_survives(self) -> None:
        proc = _spawn("import time; time.sleep(60)")
        procs.register(proc)
        procs.unregister(proc)

        assert procs.shutdown() == 0

        assert _alive(proc.pid)
        procs.kill_group(proc)

    def test_unregister_is_idempotent(self) -> None:
        proc = _spawn("pass")
        procs.register(proc)
        procs.unregister(proc)
        procs.unregister(proc)  # must not raise
        proc.wait()

    def test_shutdown_kills_whatever_registered_since(self) -> None:
        """A second Ctrl-C must reach the cleanup commands the first one started."""
        procs.shutdown()

        late = _spawn("import time; time.sleep(60)")
        procs.register(late)

        assert procs.shutdown() == 1
        assert _wait_gone(late.pid)

    def test_is_shutting_down_defaults_false(self) -> None:
        assert procs.is_shutting_down() is False

    def test_reset_clears_the_flag(self) -> None:
        procs.shutdown()
        procs.reset()
        assert procs.is_shutting_down() is False


class TestKillGroup:
    def test_kills_the_whole_tree(self, tmp_path: Path) -> None:
        """Grandchildren must die too, or they keep the captured pipes open."""
        pid_file = tmp_path / "grandchild.pid"
        proc = _spawn(
            "import subprocess, sys, time; "
            f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "time.sleep(60)"
        )
        # Wait for the grandchild to exist before killing the group.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if pid_file.exists() and pid_file.read_text().strip():
                break
            time.sleep(0.05)
        grandchild = int(pid_file.read_text())

        procs.kill_group(proc)

        assert _wait_gone(proc.pid)
        assert _wait_gone(grandchild), "grandchild survived the group kill"

    def test_already_dead_process_is_a_noop(self) -> None:
        proc = _spawn("pass")
        proc.wait()
        procs.kill_group(proc)  # must not raise

    def test_sigterm_is_tried_before_sigkill(self) -> None:
        """Well-behaved children get a chance to exit cleanly."""
        proc = _spawn(
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
            "time.sleep(60)"
        )
        # Give the handler time to install before signalling.
        time.sleep(0.5)

        procs.kill_group(proc)

        assert _wait_gone(proc.pid)
        assert proc.returncode == 0  # exited via its own handler, not SIGKILL

    def test_sigkill_escalation_for_a_process_ignoring_sigterm(self) -> None:
        proc = _spawn(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(60)"
        )
        time.sleep(0.5)

        procs.kill_group(proc)

        assert _wait_gone(proc.pid)
        assert proc.returncode == -signal.SIGKILL

    def teardown_method(self) -> None:
        with contextlib.suppress(Exception):
            procs.shutdown()
