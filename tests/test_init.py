"""Tests for po.init.generator module."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from po.init.generator import _build_init_prompt, _extract_json, generate_spec

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

        with patch("po.init.generator._invoke_claude", return_value=raw_response):
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
            patch("po.init.generator._invoke_claude", return_value=raw_response),
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

        with patch("po.init.generator._invoke_claude", return_value=raw_response):
            result = generate_spec("a calculator", output)

        assert result == output
        assert output.exists()

    def test_unparseable_response_raises(self, tmp_path):
        output = tmp_path / "spec.json"

        with (
            patch("po.init.generator._invoke_claude", return_value="I cannot help"),
            pytest.raises(ValueError, match="Could not extract valid JSON"),
        ):
            generate_spec("test", output)

    def test_passes_model_to_invoke(self, tmp_path):
        output = tmp_path / "spec.json"
        raw_response = json.dumps(self.VALID_SPEC)

        with patch("po.init.generator._invoke_claude", return_value=raw_response) as mock:
            generate_spec("a calculator", output, model="opus")

        mock.assert_called_once()
        assert mock.call_args[0][1] == "opus"
