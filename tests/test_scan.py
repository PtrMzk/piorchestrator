"""Tests for po.scan.scanner module."""

from __future__ import annotations

from po.scan.scanner import _build_scan_prompt


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
