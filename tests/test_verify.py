"""Tests for po.verify — verification commands under a hard timeout."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

from po.verify import run_verification


def _log(tmp_path: Path) -> Path:
    return tmp_path / "verify.log"


class TestRunVerification:
    def test_success(self, tmp_path: Path) -> None:
        outcome = run_verification(
            f"{sys.executable} -c 'print(1)'", tmp_path, _log(tmp_path),
        )
        assert outcome.ok is True

    def test_failure_reports_stderr(self, tmp_path: Path) -> None:
        outcome = run_verification(
            f"{sys.executable} -c \"import sys; sys.stderr.write('boom'); sys.exit(1)\"",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is False
        assert "boom" in outcome.detail

    def test_failure_falls_back_to_stdout(self, tmp_path: Path) -> None:
        outcome = run_verification(
            f"{sys.executable} -c \"print('on stdout'); raise SystemExit(1)\"",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is False
        assert "on stdout" in outcome.detail

    def test_detail_is_truncated(self, tmp_path: Path) -> None:
        outcome = run_verification(
            f"{sys.executable} -c \"import sys; sys.stderr.write('x' * 5000); sys.exit(1)\"",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is False
        assert len(outcome.detail) < 600
        assert outcome.detail.startswith("...")

    def test_runs_in_given_directory(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("here")
        outcome = run_verification("test -f marker.txt", tmp_path, _log(tmp_path))
        assert outcome.ok is True

    def test_log_file_records_output(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        run_verification(
            f"{sys.executable} -c \"print('hello log')\"", tmp_path, log,
        )
        content = log.read_text()
        assert "Exit code: 0" in content
        assert "hello log" in content

    def test_empty_command_is_a_pass(self, tmp_path: Path) -> None:
        """Whitespace-only commands must not crash on an empty argv."""
        assert run_verification("   ", tmp_path, _log(tmp_path)).ok is True

    def test_missing_executable_fails_instead_of_raising(self, tmp_path: Path) -> None:
        """A bad command is a failed verification, not an orchestrator crash.

        Callers run this inside an executor, where a raised FileNotFoundError
        propagates out of the loop and kills the whole run.
        """
        outcome = run_verification(
            "definitely-not-a-real-binary --check", tmp_path, _log(tmp_path),
        )
        assert outcome.ok is False
        assert "could not run verification command" in outcome.detail


class TestTimeout:
    def test_timeout_fails_instead_of_hanging(self, tmp_path: Path) -> None:
        """Regression: a command that never exits used to wedge the run forever."""
        start = time.monotonic()
        outcome = run_verification(
            f"{sys.executable} -c 'import time; time.sleep(60)'",
            tmp_path,
            _log(tmp_path),
            timeout=1.0,
        )
        elapsed = time.monotonic() - start

        assert outcome.ok is False
        assert "timed out after 1s" in outcome.detail
        assert elapsed < 30  # returned promptly, not after the full sleep

    def test_timeout_is_logged(self, tmp_path: Path) -> None:
        log = _log(tmp_path)
        run_verification(
            f"{sys.executable} -c 'import time; time.sleep(60)'",
            tmp_path, log, timeout=1.0,
        )
        assert "timed out after 1s" in log.read_text()

    def test_timeout_kills_the_whole_process_tree(self, tmp_path: Path) -> None:
        """A `npm run dev`-style command outlives its parent unless we kill the group.

        The grandchild would otherwise keep the captured pipes open, so the
        parent blocks on the read even after the timeout fires.
        """
        pid_file = tmp_path / "grandchild.pid"
        script = (
            "import subprocess, sys, time; "
            f"p = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
            "time.sleep(60)"
        )
        start = time.monotonic()
        outcome = run_verification(
            f"{sys.executable} -c {script!r}", tmp_path, _log(tmp_path), timeout=2.0,
        )
        elapsed = time.monotonic() - start

        assert outcome.ok is False
        # Killing only the direct child still returns — but not until the
        # grandchild releases the inherited pipes, i.e. its full 60s.
        assert elapsed < 20, f"blocked {elapsed:.0f}s on the grandchild's pipes"

        grandchild = int(pid_file.read_text())
        # SIGKILL is asynchronous; give the kernel a moment to reap.
        for _ in range(50):
            try:
                os.kill(grandchild, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        # Still alive — clean up so the test does not leak a process.
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild, signal.SIGKILL)
        raise AssertionError(f"grandchild {grandchild} survived the timeout kill")
