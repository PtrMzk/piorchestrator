"""Tests for spec loading and validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from po.spec.loader import JsonSpecLoader
from po.spec.schema import ProjectSpec, TaskSpec


class TestTaskSpec:
    def test_valid_task(self) -> None:
        task = TaskSpec(id="my-task", description="Do something")
        assert task.validate() == []

    def test_empty_id(self) -> None:
        task = TaskSpec(id="", description="Do something")
        errors = task.validate()
        assert any("empty" in e for e in errors)

    def test_invalid_id_chars(self) -> None:
        task = TaskSpec(id="my task!", description="Do something")
        errors = task.validate()
        assert any("invalid characters" in e for e in errors)

    def test_empty_description(self) -> None:
        task = TaskSpec(id="task-1", description="")
        errors = task.validate()
        assert any("description" in e for e in errors)

    def test_negative_priority(self) -> None:
        task = TaskSpec(id="task-1", description="Do something", priority=-1)
        errors = task.validate()
        assert any("priority" in e for e in errors)

    def test_negative_budget(self) -> None:
        task = TaskSpec(id="task-1", description="Do something", max_budget_usd=-1.0)
        errors = task.validate()
        assert any("max_budget_usd" in e for e in errors)

    def test_from_dict(self) -> None:
        data = {
            "id": "t1",
            "description": "Test",
            "dependencies": ["t0"],
            "priority": 5,
            "unknown_key": "ignored",
        }
        task = TaskSpec.from_dict(data)
        assert task.id == "t1"
        assert task.dependencies == ["t0"]
        assert task.priority == 5

    def test_from_dict_defaults(self) -> None:
        task = TaskSpec.from_dict({"id": "t1", "description": "Test"})
        assert task.dependencies == []
        assert task.model == "sonnet"
        assert task.max_budget_usd == 2.0


class TestProjectSpec:
    def test_valid_spec(self, sample_spec: ProjectSpec) -> None:
        errors = sample_spec.validate()
        assert errors == []

    def test_empty_name(self) -> None:
        spec = ProjectSpec(project_name="", tasks=[TaskSpec(id="t", description="d")])
        errors = spec.validate()
        assert any("project_name" in e for e in errors)

    def test_no_tasks(self) -> None:
        spec = ProjectSpec(project_name="p", tasks=[])
        errors = spec.validate()
        assert any("at least one task" in e for e in errors)

    def test_duplicate_ids(self) -> None:
        spec = ProjectSpec(
            project_name="p",
            tasks=[
                TaskSpec(id="t1", description="A"),
                TaskSpec(id="t1", description="B"),
            ],
        )
        errors = spec.validate()
        assert any("Duplicate" in e for e in errors)

    def test_unknown_dependency(self) -> None:
        spec = ProjectSpec(
            project_name="p",
            tasks=[TaskSpec(id="t1", description="A", dependencies=["nonexistent"])],
        )
        errors = spec.validate()
        assert any("unknown task" in e for e in errors)

    def test_self_dependency(self) -> None:
        spec = ProjectSpec(
            project_name="p",
            tasks=[TaskSpec(id="t1", description="A", dependencies=["t1"])],
        )
        errors = spec.validate()
        assert any("depends on itself" in e for e in errors)

    def test_max_concurrency_zero(self) -> None:
        spec = ProjectSpec(
            project_name="p",
            tasks=[TaskSpec(id="t1", description="A")],
            max_concurrency=0,
        )
        errors = spec.validate()
        assert any("max_concurrency" in e for e in errors)

    def test_from_dict(self) -> None:
        data = {
            "project_name": "test",
            "description": "desc",
            "max_concurrency": 5,
            "tasks": [{"id": "t1", "description": "do it"}],
        }
        spec = ProjectSpec.from_dict(data)
        assert spec.project_name == "test"
        assert spec.max_concurrency == 5
        assert len(spec.tasks) == 1

    def test_from_dict_applies_default_model(self) -> None:
        data = {
            "project_name": "test",
            "default_model": "opus",
            "tasks": [{"id": "t1", "description": "d"}],
        }
        spec = ProjectSpec.from_dict(data)
        assert spec.tasks[0].model == "opus"

    def test_from_dict_task_overrides_model(self) -> None:
        data = {
            "project_name": "test",
            "default_model": "opus",
            "tasks": [{"id": "t1", "description": "d", "model": "haiku"}],
        }
        spec = ProjectSpec.from_dict(data)
        assert spec.tasks[0].model == "haiku"

    def test_from_json(self) -> None:
        data = {
            "project_name": "test",
            "tasks": [{"id": "t1", "description": "d"}],
        }
        spec = ProjectSpec.from_json(json.dumps(data))
        assert spec.project_name == "test"


class TestJsonSpecLoader:
    def test_load_valid(self, sample_spec_json: Path) -> None:
        loader = JsonSpecLoader()
        spec = loader.load(sample_spec_json)
        assert spec.project_name == "test-project"
        assert len(spec.tasks) == 4

    def test_load_missing_file(self, tmp_path: Path) -> None:
        loader = JsonSpecLoader()
        with pytest.raises(FileNotFoundError):
            loader.load(tmp_path / "nonexistent.json")

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json")
        loader = JsonSpecLoader()
        with pytest.raises(json.JSONDecodeError):
            loader.load(bad_file)

    def test_load_validation_error(self, tmp_path: Path) -> None:
        bad_spec = tmp_path / "bad_spec.json"
        bad_spec.write_text(json.dumps({"project_name": "", "tasks": []}))
        loader = JsonSpecLoader()
        with pytest.raises(ValueError, match="Invalid spec"):
            loader.load(bad_spec)

    def test_load_example_spec(self) -> None:
        example = Path(__file__).parent.parent / "examples" / "todo-api.json"
        if example.exists():
            loader = JsonSpecLoader()
            spec = loader.load(example)
            assert spec.project_name == "todo-api"
            assert len(spec.tasks) == 7
