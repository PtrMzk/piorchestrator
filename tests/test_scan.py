"""Tests for po.scan.scanner module."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from po.scan.scanner import _build_scan_prompt, _invoke_scan_agent


class TestBuildScanPrompt:
    """Tests for _build_scan_prompt."""

    def test_contains_output_dir(self):
        prompt = _build_scan_prompt("docs/codebase")
        assert "docs/codebase/" in prompt

    def test_contains_index_file(self):
        prompt = _build_scan_prompt("docs/codebase")
        assert "docs/codebase/index.md" in prompt

    def test_contains_documentation_instructions(self):
        prompt = _build_scan_prompt("docs/codebase")
        assert "Analyze" in prompt
        assert "documentation" in prompt.lower()
        assert "markdown" in prompt.lower()

    def test_custom_output_dir(self):
        prompt = _build_scan_prompt("my-docs/scan")
        assert "my-docs/scan/" in prompt
        assert "my-docs/scan/index.md" in prompt

    def test_contains_component_doc_guidance(self):
        prompt = _build_scan_prompt("docs/codebase")
        assert "module" in prompt.lower() or "component" in prompt.lower()
        assert "API" in prompt or "api" in prompt.lower()


class TestInvokeScanAgentSubprocess:
    """Exercise the real subprocess handling in `_invoke_scan_agent`.

    These tests put a stub `claude` on PATH rather than patching, because the
    behaviour under test *is* the pipe/env handling.
    """

    @staticmethod
    def _stub_claude(bin_dir: Path, body: str) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "claude"
        # Absolute interpreter path so the stub does not depend on PATH order.
        script.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
        script.chmod(0o755)

    def test_large_stderr_does_not_deadlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: >64KB on stderr used to fill the pipe and hang forever.

        The child blocks writing to stderr while the parent is blocked reading
        stdout, so neither side ever advances.
        """
        result = json.dumps({"type": "result", "result": "scanned"})
        self._stub_claude(
            tmp_path / "bin",
            # 1 MB of stderr — far past the ~64KB pipe buffer — written before
            # the result line so the deadlock would trigger prior to any stdout.
            f"sys.stderr.write('x' * 1_000_000)\nsys.stderr.flush()\nprint({result!r})\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        assert _invoke_scan_agent("prompt", "sonnet", tmp_path) == "scanned"

    def test_stdin_is_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin must be /dev/null so a prompting child cannot hang the scan."""
        result = json.dumps({"type": "result", "result": "ok"})
        self._stub_claude(
            tmp_path / "bin",
            f"assert sys.stdin.read() == '', 'stdin was not empty'\nprint({result!r})\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        assert _invoke_scan_agent("prompt", "sonnet", tmp_path) == "ok"

    def test_auth_env_survives(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Auth vars must reach the scan agent; only nesting markers are dropped."""
        self._stub_claude(
            tmp_path / "bin",
            "import json\n"
            "print(json.dumps({'type': 'result', 'result': json.dumps({\n"
            "    'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN', ''),\n"
            "    'nested': os.environ.get('CLAUDECODE', ''),\n"
            "})}))\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-123")
        monkeypatch.setenv("CLAUDECODE", "1")

        seen = json.loads(_invoke_scan_agent("prompt", "sonnet", tmp_path))
        assert seen["token"] == "tok-123"  # auth reaches the agent
        assert seen["nested"] == ""  # nesting marker stripped

    def test_no_result_surfaces_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 with no result message must report stderr, not swallow it."""
        self._stub_claude(
            tmp_path / "bin",
            "sys.stderr.write('rate limit exceeded')\n",
        )
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            _invoke_scan_agent("prompt", "sonnet", tmp_path)
