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
            f"{sys.executable} -c 'print(1)'",
            tmp_path,
            _log(tmp_path),
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
            f"{sys.executable} -c \"print('hello log')\"",
            tmp_path,
            log,
        )
        content = log.read_text()
        assert "Exit code: 0" in content
        assert "hello log" in content

    def test_empty_command_is_a_pass(self, tmp_path: Path) -> None:
        """Whitespace-only commands must not crash on an empty argv."""
        assert run_verification("   ", tmp_path, _log(tmp_path)).ok is True

    def test_missing_executable_fails_instead_of_raising(self, tmp_path: Path) -> None:
        """A bad command is a failed verification, not an orchestrator crash.

        Callers run this inside an executor, where a raised exception propagates
        out of the loop and kills the whole run. The shell turns an unknown
        command into exit 127, so this must fail without raising either way.
        """
        outcome = run_verification(
            "definitely-not-a-real-binary --check",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is False
        assert "not found" in outcome.detail

    def test_unbalanced_quotes_do_not_raise(self, tmp_path: Path) -> None:
        """shlex.split() raised ValueError here, killing the orchestrator."""
        outcome = run_verification('echo "unterminated', tmp_path, _log(tmp_path))
        assert outcome.ok is False


class TestShellOperators:
    """Regression: verification ran without a shell, so `&&` reached the program.

    `npx tsc --noEmit && npm run build` exec'd tsc with ['&&', 'npm', 'run',
    'build'] as filenames (TS5112) — a failure unrelated to the task's code.
    """

    def test_chained_command_failure_is_detected(self, tmp_path: Path) -> None:
        outcome = run_verification("true && false", tmp_path, _log(tmp_path))
        assert outcome.ok is False

    def test_second_half_of_a_chain_actually_runs(self, tmp_path: Path) -> None:
        """The dangerous direction: `A && B` passed while B never ran.

        `true` ignores the junk argv and exits 0, so the gate reported success
        having tested nothing and the task merged green.
        """
        log = _log(tmp_path)
        outcome = run_verification("true && echo second-half-ran", tmp_path, log)
        assert outcome.ok is True
        assert "second-half-ran" in log.read_text()

    def test_pipes_and_redirection_work(self, tmp_path: Path) -> None:
        outcome = run_verification(
            "echo hello | grep -q hello",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is True

    def test_glob_is_expanded(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("x")
        outcome = run_verification("test -f *.txt", tmp_path, _log(tmp_path))
        assert outcome.ok is True

    def test_nonzero_exit_of_last_command_wins(self, tmp_path: Path) -> None:
        outcome = run_verification(
            "echo ok && exit 3",
            tmp_path,
            _log(tmp_path),
        )
        assert outcome.ok is False


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
            tmp_path,
            log,
            timeout=1.0,
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
            f"{sys.executable} -c {script!r}",
            tmp_path,
            _log(tmp_path),
            timeout=2.0,
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
