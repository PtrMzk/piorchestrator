"""Tests for the scaffold stub generator."""

from __future__ import annotations

from pathlib import Path

from po.scaffold.generator import generate_scaffolds
from po.spec.schema import ProjectSpec, TaskSpec


def _make_spec(tasks: list[TaskSpec]) -> ProjectSpec:
    return ProjectSpec(project_name="test", tasks=tasks)


class TestGenerateScaffolds:
    def test_creates_py_stub_with_docstring(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Build widget", output_files=["src/widget.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        stub = tmp_path / "src" / "widget.py"
        assert stub in created
        content = stub.read_text()
        assert '"""' in content
        assert "t1" in content

    def test_shared_output_lists_all_tasks(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Add feature A", output_files=["shared.py"]),
            TaskSpec(id="t2", description="Add feature B", output_files=["shared.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        content = (tmp_path / "shared.py").read_text()
        assert "t1" in content
        assert "t2" in content
        assert len(created) == 1

    def test_skips_existing_files_with_content(self, tmp_path: Path) -> None:
        existing = tmp_path / "existing.py"
        existing.write_text("real content")
        spec = _make_spec([
            TaskSpec(id="t1", description="Modify", output_files=["existing.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        assert existing not in created
        assert existing.read_text() == "real content"

    def test_overwrites_zero_byte_files(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.py"
        empty.write_text("")
        spec = _make_spec([
            TaskSpec(id="t1", description="Fill in", output_files=["empty.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        assert empty in created
        assert empty.stat().st_size > 0

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Deep file", output_files=["a/b/c/deep.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        assert (tmp_path / "a" / "b" / "c" / "deep.py") in created

    def test_json_stub(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Config", output_files=["config.json"]),
        ])
        generate_scaffolds(spec, tmp_path)
        assert (tmp_path / "config.json").read_text() == "{}\n"

    def test_md_stub(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Write docs", output_files=["docs/guide.md"]),
        ])
        generate_scaffolds(spec, tmp_path)
        content = (tmp_path / "docs" / "guide.md").read_text()
        assert "# guide" in content
        assert "t1" in content

    def test_unknown_extension_uses_comment(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="Config file", output_files=["app.cfg"]),
        ])
        generate_scaffolds(spec, tmp_path)
        content = (tmp_path / "app.cfg").read_text()
        assert content.startswith("#")
        assert "t1" in content

    def test_returns_only_created_paths(self, tmp_path: Path) -> None:
        (tmp_path / "exists.py").write_text("content")
        spec = _make_spec([
            TaskSpec(id="t1", description="Existing", output_files=["exists.py"]),
            TaskSpec(id="t2", description="New", output_files=["new.py"]),
        ])
        created = generate_scaffolds(spec, tmp_path)
        assert len(created) == 1
        assert (tmp_path / "new.py") in created

    def test_js_stub(self, tmp_path: Path) -> None:
        spec = _make_spec([
            TaskSpec(id="t1", description="UI component", output_files=["app.tsx"]),
        ])
        generate_scaffolds(spec, tmp_path)
        content = (tmp_path / "app.tsx").read_text()
        assert "/**" in content
        assert "t1" in content
