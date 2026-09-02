"""Tests for docs/generator — documentation tree generation."""

from __future__ import annotations

from pathlib import Path

from po.docs.generator import generate_doc_tree
from po.spec.schema import ProjectSpec


def _make_spec(**overrides) -> ProjectSpec:
    defaults = {
        "project_name": "test-proj",
        "description": "A test project",
        "global_context": "Use Python 3.12",
        "tasks": [
            {
                "id": "t1",
                "description": "First task",
                "output_files": ["src/a.py"],
                "tags": ["setup"],
            },
            {
                "id": "t2",
                "description": "Second task",
                "dependencies": ["t1"],
                "output_files": ["src/b.py"],
                "tags": ["core"],
            },
        ],
    }
    defaults.update(overrides)
    return ProjectSpec.from_dict(defaults)


class TestGenerateDocTree:
    def test_creates_all_files_with_tags(self, tmp_path: Path) -> None:
        spec = _make_spec()
        created = generate_doc_tree(spec, tmp_path)
        # CLAUDE.md + 3 L1 docs + 2 component docs (setup, core)
        assert len(created) == 6
        names = [p.name for p in created]
        assert "CLAUDE.md" in names
        assert "SYSTEM_DESIGN.md" in names
        assert "CODE_PATHS.md" in names
        assert "API_CONTRACTS.md" in names

    def test_no_tags_no_components(self, tmp_path: Path) -> None:
        spec = _make_spec(
            tasks=[
                {"id": "t1", "description": "No tags task", "output_files": ["x.py"]},
            ]
        )
        created = generate_doc_tree(spec, tmp_path)
        assert len(created) == 4  # CLAUDE.md + 3 L1 docs
        assert not (tmp_path / "docs" / "components").exists()

    def test_existing_claude_md_appended(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\nCustom content here.", encoding="utf-8")

        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)

        content = claude_md.read_text(encoding="utf-8")
        assert "Custom content here." in content
        assert "<!-- po:generated -->" in content
        assert "test-proj" in content

    def test_existing_claude_md_with_marker_replaces(self, tmp_path: Path) -> None:
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# My Project\nKeep this.\n\n<!-- po:generated -->\n# old-name\nOld content.\n",
            encoding="utf-8",
        )

        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)

        content = claude_md.read_text(encoding="utf-8")
        assert "Keep this." in content
        assert "old-name" not in content
        assert "test-proj" in content

    def test_fresh_claude_md_no_marker(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        # Fresh file should NOT have the marker
        assert "<!-- po:generated -->" not in content


class TestSystemDesign:
    def test_contains_task_info(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "docs" / "SYSTEM_DESIGN.md").read_text(encoding="utf-8")
        assert "t1" in content
        assert "First task" in content
        assert "t2" in content
        assert "Second task" in content

    def test_contains_dependencies(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "docs" / "SYSTEM_DESIGN.md").read_text(encoding="utf-8")
        assert "none" in content  # t1 has no deps
        assert "t1" in content  # t2 depends on t1

    def test_contains_output_files(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "docs" / "SYSTEM_DESIGN.md").read_text(encoding="utf-8")
        assert "src/a.py" in content
        assert "src/b.py" in content


class TestCodePaths:
    def test_contains_file_map(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "docs" / "CODE_PATHS.md").read_text(encoding="utf-8")
        assert "`src/a.py`" in content
        assert "`src/b.py`" in content

    def test_shared_files_map_to_multiple_tasks(self, tmp_path: Path) -> None:
        spec = _make_spec(
            tasks=[
                {"id": "t1", "description": "A", "output_files": ["shared.py"]},
                {"id": "t2", "description": "B", "output_files": ["shared.py"]},
            ]
        )
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "docs" / "CODE_PATHS.md").read_text(encoding="utf-8")
        assert "t1" in content
        assert "t2" in content
        assert "shared.py" in content


class TestComponentDocs:
    def test_correct_tasks_per_component(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)

        setup_doc = (tmp_path / "docs" / "components" / "SETUP.md").read_text(encoding="utf-8")
        assert "t1" in setup_doc
        assert "t2" not in setup_doc

        core_doc = (tmp_path / "docs" / "components" / "CORE.md").read_text(encoding="utf-8")
        assert "t2" in core_doc
        assert "t1" not in core_doc

    def test_tag_filenames_uppercased(self, tmp_path: Path) -> None:
        spec = _make_spec(
            tasks=[
                {"id": "t1", "description": "A", "output_files": ["x.py"], "tags": ["my-tag"]},
            ]
        )
        generate_doc_tree(spec, tmp_path)
        assert (tmp_path / "docs" / "components" / "MY-TAG.md").exists()


class TestClaudeMdContent:
    def test_includes_doc_links(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "SYSTEM_DESIGN.md" in content
        assert "CODE_PATHS.md" in content
        assert "API_CONTRACTS.md" in content

    def test_includes_component_links(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "setup" in content
        assert "core" in content

    def test_no_component_links_without_tags(self, tmp_path: Path) -> None:
        spec = _make_spec(
            tasks=[
                {"id": "t1", "description": "A", "output_files": ["x.py"]},
            ]
        )
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Components" not in content

    def test_includes_global_context(self, tmp_path: Path) -> None:
        spec = _make_spec()
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "Use Python 3.12" in content

    def test_no_context_when_empty(self, tmp_path: Path) -> None:
        spec = _make_spec(global_context="")
        generate_doc_tree(spec, tmp_path)
        content = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
        assert "## Context" not in content
