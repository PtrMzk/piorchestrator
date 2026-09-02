"""Tests for po.init.generator module."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from po.init.generator import (
    _build_init_prompt,
    _build_outline_prompt,
    _build_spec_from_outline_prompt,
    _detect_codebase_docs,
    _extract_json,
    _invoke_claude,
    generate_outline,
    generate_spec,
    generate_spec_from_outline,
)

# --- _extract_json tests ---


class TestExtractJson:
    """Tests for _extract_json."""

    def test_clean_json(self):
        data = {"project_name": "test", "tasks": []}
        result = _extract_json(json.dumps(data))
        assert result == data

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"project_name": "test", "tasks": []}\n```'
        result = _extract_json(raw)
        assert result == {"project_name": "test", "tasks": []}

    def test_json_with_bare_fences(self):
        raw = '```\n{"project_name": "test", "tasks": []}\n```'
        result = _extract_json(raw)
        assert result == {"project_name": "test", "tasks": []}

    def test_json_with_preamble_text(self):
        raw = 'Here is the spec:\n\n{"project_name": "test", "tasks": []}'
        result = _extract_json(raw)
        assert result == {"project_name": "test", "tasks": []}

    def test_json_with_trailing_text(self):
        raw = '{"project_name": "test", "tasks": []}\n\nLet me know if you need changes.'
        result = _extract_json(raw)
        assert result == {"project_name": "test", "tasks": []}

    def test_json_with_preamble_and_fences(self):
        raw = 'Here is the spec:\n\n```json\n{"project_name": "test", "tasks": []}\n```\n\nEnjoy!'
        result = _extract_json(raw)
        assert result == {"project_name": "test", "tasks": []}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Could not extract valid JSON"):
            _extract_json("This is not JSON at all")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Could not extract valid JSON"):
            _extract_json("")

    def test_nested_json_extracted(self):
        raw = 'Result:\n{"project_name": "x", "tasks": [{"id": "a", "description": "b"}]}'
        result = _extract_json(raw)
        assert result["project_name"] == "x"
        assert len(result["tasks"]) == 1


# --- _build_init_prompt tests ---


class TestBuildInitPrompt:
    """Tests for _build_init_prompt."""

    def test_contains_description(self):
        prompt = _build_init_prompt("a calculator CLI in Python")
        assert "a calculator CLI in Python" in prompt

    def test_contains_schema_fields(self):
        prompt = _build_init_prompt("test project")
        assert "project_name" in prompt
        assert "dependencies" in prompt
        assert "output_files" in prompt
        assert "verification" in prompt
        assert "max_budget_usd" in prompt

    def test_contains_example(self):
        prompt = _build_init_prompt("test project")
        assert "todo-api" in prompt
        assert "init-project" in prompt

    def test_contains_constraints(self):
        prompt = _build_init_prompt("test project")
        assert "DAG" in prompt
        assert "kebab-case" in prompt
        assert "TDD" in prompt or "failing tests first" in prompt
        assert "test files" in prompt or "test suite" in prompt
        assert "TypeScript" in prompt
        assert "Bun" in prompt
        assert "built-in" in prompt.lower()
        assert "user_stories" in prompt
        assert "Playwright" in prompt or "playwright" in prompt

    def test_contains_doc_task_constraints(self):
        prompt = _build_init_prompt("test project")
        assert "doc-" in prompt or "doc task" in prompt.lower()
        assert "docs/" in prompt
        assert "context_files" in prompt


# --- generate_spec tests ---


class TestGenerateSpec:
    """Tests for generate_spec."""

    VALID_SPEC = {
        "project_name": "calc",
        "description": "A calculator",
        "default_model": "sonnet",
        "max_concurrency": 2,
        "global_context": "Use Python",
        "tasks": [
            {
                "id": "init",
                "description": "Set up the project",
                "dependencies": [],
                "output_files": ["pyproject.toml"],
                "priority": 10,
            },
            {
                "id": "add-ops",
                "description": "Add calculator operations",
                "dependencies": ["init"],
                "output_files": ["src/calc.py"],
                "priority": 5,
            },
        ],
    }

    def test_generates_and_writes_spec(self, tmp_path):
        output = tmp_path / "spec.json"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=(raw_response, None)):
            result = generate_spec("a calculator", output)

        assert result == output
        assert output.exists()
        written = json.loads(output.read_text())
        assert written["project_name"] == "calc"
        assert len(written["tasks"]) == 2

    def test_file_exists_raises(self, tmp_path):
        output = tmp_path / "spec.json"
        output.write_text("{}")

        with pytest.raises(FileExistsError, match="already exists"):
            generate_spec("test", output)

    def test_invalid_spec_raises(self, tmp_path):
        output = tmp_path / "spec.json"
        # Missing project_name and tasks
        bad_spec = {"project_name": "", "tasks": []}
        raw_response = json.dumps(bad_spec)

        with (
            patch("po.init.generator._invoke_claude", return_value=(raw_response, None)),
            pytest.raises(ValueError, match="failed validation"),
        ):
            generate_spec("test", output)

    def test_claude_failure_raises(self, tmp_path):
        output = tmp_path / "spec.json"

        with (
            patch(
                "po.init.generator._invoke_claude",
                side_effect=RuntimeError("Claude CLI failed"),
            ),
            pytest.raises(RuntimeError, match="Claude CLI failed"),
        ):
            generate_spec("test", output)

    def test_creates_parent_directories(self, tmp_path):
        output = tmp_path / "sub" / "dir" / "spec.json"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=(raw_response, None)):
            result = generate_spec("a calculator", output)

        assert result == output
        assert output.exists()

    def test_unparseable_response_raises(self, tmp_path):
        output = tmp_path / "spec.json"

        with (
            patch("po.init.generator._invoke_claude", return_value=("I cannot help", None)),
            pytest.raises(ValueError, match="Could not extract valid JSON"),
        ):
            generate_spec("test", output)

    def test_passes_model_to_invoke(self, tmp_path):
        output = tmp_path / "spec.json"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=(raw_response, None)) as mock:
            generate_spec("a calculator", output, model="opus")

        mock.assert_called_once()
        assert mock.call_args[0][1] == "opus"


# --- _build_outline_prompt tests ---


class TestBuildOutlinePrompt:
    """Tests for _build_outline_prompt."""

    def test_contains_description(self):
        prompt = _build_outline_prompt("a REST API for todos")
        assert "a REST API for todos" in prompt

    def test_does_not_ask_for_json(self):
        prompt = _build_outline_prompt("test project")
        assert "Do NOT generate JSON" in prompt

    def test_asks_for_markdown(self):
        prompt = _build_outline_prompt("test project")
        assert "markdown" in prompt.lower()

    def test_feedback_included_when_provided(self):
        prompt = _build_outline_prompt("test project", feedback="Add auth tasks")
        assert "Add auth tasks" in prompt
        assert "feedback" in prompt.lower()

    def test_no_feedback_section_when_none(self):
        prompt = _build_outline_prompt("test project")
        assert "Previous outline was rejected" not in prompt


# --- generate_outline tests ---


class TestGenerateOutline:
    """Tests for generate_outline."""

    def test_returns_claude_response_and_session_id(self):
        outline_text = "## Project: my-app\n- init-project\n- add-feature"
        with patch("po.init.generator._invoke_claude", return_value=(outline_text, "sess-123")):
            outline, sid = generate_outline("build an app")
        assert outline == outline_text
        assert sid == "sess-123"

    def test_passes_model(self):
        with patch("po.init.generator._invoke_claude", return_value=("outline", None)) as mock:
            generate_outline("test", model="sonnet")
        assert mock.call_args[0][1] == "sonnet"

    def test_passes_feedback(self):
        with patch("po.init.generator._invoke_claude", return_value=("outline", None)) as mock:
            generate_outline("test", feedback="add more tasks")
        prompt = mock.call_args[0][0]
        assert "add more tasks" in prompt

    def test_passes_session_id(self):
        rv = ("outline", "sess-456")
        with patch("po.init.generator._invoke_claude", return_value=rv) as mock:
            generate_outline("test", session_id="sess-123")
        assert mock.call_args[1]["session_id"] == "sess-123"

    def test_claude_failure_propagates(self):
        with (
            patch(
                "po.init.generator._invoke_claude",
                side_effect=RuntimeError("Claude CLI failed"),
            ),
            pytest.raises(RuntimeError, match="Claude CLI failed"),
        ):
            generate_outline("test")


# --- generate_spec_from_outline tests ---


class TestGenerateSpecFromOutline:
    """Tests for generate_spec_from_outline."""

    VALID_SPEC = TestGenerateSpec.VALID_SPEC

    def test_generates_spec_from_outline(self, tmp_path):
        output = tmp_path / "spec.json"
        outline = "## Project: calc\n- init\n- add-ops"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=(raw_response, None)):
            result = generate_spec_from_outline("a calculator", outline, output)

        assert result == output
        assert output.exists()
        written = json.loads(output.read_text())
        assert written["project_name"] == "calc"

    def test_outline_included_in_prompt(self, tmp_path):
        output = tmp_path / "spec.json"
        outline = "## Project: calc\n- init\n- add-ops"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=(raw_response, None)) as mock:
            generate_spec_from_outline("a calculator", outline, output)

        prompt = mock.call_args[0][0]
        assert outline in prompt

    def test_file_exists_raises(self, tmp_path):
        output = tmp_path / "spec.json"
        output.write_text("{}")

        with pytest.raises(FileExistsError, match="already exists"):
            generate_spec_from_outline("test", "outline", output)

    def test_invalid_spec_raises(self, tmp_path):
        output = tmp_path / "spec.json"
        bad_spec = {"project_name": "", "tasks": []}
        raw_response = json.dumps(bad_spec)

        with (
            patch("po.init.generator._invoke_claude", return_value=(raw_response, None)),
            pytest.raises(ValueError, match="failed validation"),
        ):
            generate_spec_from_outline("test", "outline", output)


# --- _detect_codebase_docs tests ---


class TestDetectCodebaseDocs:
    """Tests for _detect_codebase_docs."""

    def test_returns_none_when_no_project_root(self):
        assert _detect_codebase_docs(None) is None

    def test_returns_none_when_dir_missing(self, tmp_path):
        assert _detect_codebase_docs(tmp_path) is None

    def test_returns_none_when_dir_empty(self, tmp_path):
        (tmp_path / "docs" / "codebase").mkdir(parents=True)
        assert _detect_codebase_docs(tmp_path) is None

    def test_returns_listing_with_files(self, tmp_path):
        docs_dir = tmp_path / "docs" / "codebase"
        docs_dir.mkdir(parents=True)
        (docs_dir / "index.md").write_text("# Index\n")
        (docs_dir / "api.md").write_text("# API\n")

        listing = _detect_codebase_docs(tmp_path)
        assert listing is not None
        assert "docs/codebase/index.md" in listing
        assert "docs/codebase/api.md" in listing

    def test_includes_nested_files(self, tmp_path):
        docs_dir = tmp_path / "docs" / "codebase" / "sub"
        docs_dir.mkdir(parents=True)
        (docs_dir / "deep.md").write_text("# Deep\n")

        listing = _detect_codebase_docs(tmp_path)
        assert listing is not None
        assert "docs/codebase/sub/deep.md" in listing


# --- Codebase docs integration into prompts ---


class TestCodebaseDocsInPrompts:
    """Tests for codebase docs integration in prompt builders."""

    def test_outline_prompt_includes_docs_when_present(self, tmp_path):
        docs_dir = tmp_path / "docs" / "codebase"
        docs_dir.mkdir(parents=True)
        (docs_dir / "index.md").write_text("# Index\n")

        prompt = _build_outline_prompt("test project", project_root=tmp_path)
        assert "docs/codebase/index.md" in prompt
        assert "global_context_files" in prompt

    def test_outline_prompt_no_docs_without_dir(self, tmp_path):
        prompt = _build_outline_prompt("test project", project_root=tmp_path)
        assert "Pre-scanned codebase" not in prompt

    def test_spec_from_outline_prompt_includes_docs(self, tmp_path):
        docs_dir = tmp_path / "docs" / "codebase"
        docs_dir.mkdir(parents=True)
        (docs_dir / "index.md").write_text("# Index\n")

        prompt = _build_spec_from_outline_prompt(
            "test project",
            "## outline",
            project_root=tmp_path,
        )
        assert "docs/codebase/index.md" in prompt
        assert "global_context_files" in prompt

    def test_init_prompt_includes_docs(self, tmp_path):
        docs_dir = tmp_path / "docs" / "codebase"
        docs_dir.mkdir(parents=True)
        (docs_dir / "index.md").write_text("# Index\n")

        prompt = _build_init_prompt("test project", project_root=tmp_path)
        assert "docs/codebase/index.md" in prompt

    def test_init_prompt_no_docs_without_project_root(self):
        prompt = _build_init_prompt("test project")
        assert "Pre-scanned codebase" not in prompt


class TestInvokeClaudeSubprocess:
    """Exercise the real subprocess handling in `_invoke_claude`.

    These tests use a stub `claude` on PATH rather than patching, because the
    behaviour under test *is* the pipe handling.
    """

    @staticmethod
    def _stub_claude(bin_dir: Path, body: str) -> None:
        bin_dir.mkdir(parents=True, exist_ok=True)
        script = bin_dir / "claude"
        # Absolute interpreter path: these tests replace PATH with bin_dir.
        script.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
        script.chmod(0o755)

    def test_large_stderr_does_not_deadlock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: >64KB on stderr used to fill the pipe and hang forever.

        The child blocks writing to stderr while the parent is blocked reading
        stdout, so neither side ever advances.
        """
        result = json.dumps({"type": "result", "result": "ok", "session_id": "s1"})
        self._stub_claude(
            tmp_path / "bin",
            # 1 MB of stderr — far past the ~64KB pipe buffer — written before
            # the result line so the deadlock would trigger prior to any stdout.
            f"sys.stderr.write('x' * 1_000_000)\nsys.stderr.flush()\nprint({result!r})\n",
        )
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        text, session_id = _invoke_claude("prompt", "sonnet", project_root=tmp_path)

        assert text == "ok"
        assert session_id == "s1"

    def test_missing_claude_is_a_runtime_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`cmd_init` catches RuntimeError; a bare FileNotFoundError was a traceback."""
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(RuntimeError, match="not on PATH"):
            _invoke_claude("prompt", "haiku", project_root=tmp_path)

    def test_no_result_surfaces_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit 0 with no result message must report stderr, not swallow it."""
        self._stub_claude(
            tmp_path / "bin",
            "sys.stderr.write('rate limit exceeded')\n",
        )
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            _invoke_claude("prompt", "sonnet", project_root=tmp_path)

    def test_stdin_is_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin must be /dev/null so a prompting child cannot hang the run."""
        result = json.dumps({"type": "result", "result": "ok"})
        self._stub_claude(
            tmp_path / "bin",
            f"assert sys.stdin.read() == '', 'stdin was not empty'\nprint({result!r})\n",
        )
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))

        text, _ = _invoke_claude("prompt", "sonnet", project_root=tmp_path)
        assert text == "ok"
