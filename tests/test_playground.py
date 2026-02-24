"""Tests for the playground spec generator."""

from __future__ import annotations

from pathlib import Path

import pytest

from po.graph.resolver import get_execution_plan
from po.playground.generator import generate_playground
from po.spec.schema import ProjectSpec


class TestGeneratePlayground:
    def test_creates_spec_file(self, tmp_path: Path) -> None:
        spec_path, _ = generate_playground(tmp_path)
        assert spec_path.exists()
        assert spec_path.name == "playground-calc.json"

    def test_spec_validates(self, tmp_path: Path) -> None:
        spec_path, _ = generate_playground(tmp_path)
        spec = ProjectSpec.from_json(spec_path.read_text())
        errors = spec.validate()
        assert errors == [], f"Validation errors: {errors}"

    def test_spec_has_dependencies_and_overlaps(self, tmp_path: Path) -> None:
        spec_path, _ = generate_playground(tmp_path)
        spec = ProjectSpec.from_json(spec_path.read_text())

        # At least one task has dependencies
        has_deps = any(t.dependencies for t in spec.tasks)
        assert has_deps

        # At least two tasks share an output file (overlap)
        file_counts: dict[str, int] = {}
        for task in spec.tasks:
            for f in task.output_files:
                file_counts[f] = file_counts.get(f, 0) + 1
        has_overlap = any(c > 1 for c in file_counts.values())
        assert has_overlap

    def test_spec_has_verifications_and_context_files(self, tmp_path: Path) -> None:
        spec_path, _ = generate_playground(tmp_path)
        spec = ProjectSpec.from_json(spec_path.read_text())

        has_verification = any(t.verification for t in spec.tasks)
        has_context_files = any(t.context_files for t in spec.tasks)
        assert has_verification
        assert has_context_files

    def test_execution_plan_has_multiple_layers(self, tmp_path: Path) -> None:
        spec_path, _ = generate_playground(tmp_path)
        spec = ProjectSpec.from_json(spec_path.read_text())
        layers = get_execution_plan(spec.tasks)
        assert len(layers) >= 3

    def test_creates_readme_when_missing(self, tmp_path: Path) -> None:
        _, seed_files = generate_playground(tmp_path)
        readme = tmp_path / "README.md"
        assert readme.exists()
        assert readme in seed_files

    def test_preserves_existing_readme(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        readme.write_text("# Existing\n")
        _, seed_files = generate_playground(tmp_path)
        assert readme.read_text() == "# Existing\n"
        assert readme not in seed_files

    def test_raises_if_spec_exists(self, tmp_path: Path) -> None:
        generate_playground(tmp_path)
        with pytest.raises(FileExistsError):
            generate_playground(tmp_path)

    def test_creates_calc_directory(self, tmp_path: Path) -> None:
        generate_playground(tmp_path)
        assert (tmp_path / "src" / "calc").is_dir()
