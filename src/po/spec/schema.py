"""Dataclasses for task specs and project specs, plus validation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskSpec:
    """Specification for a single task in the project."""

    id: str
    description: str
    dependencies: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)
    verification: str = ""
    priority: int = 0
    model: str = "sonnet"
    max_budget_usd: float = 2.0
    tags: list[str] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Validate this task spec, returning a list of error messages."""
        errors: list[str] = []
        if not self.id:
            errors.append("Task id must not be empty")
        if not self.id.replace("-", "").replace("_", "").isalnum():
            errors.append(
                f"Task id '{self.id}' contains invalid characters "
                "(use alphanumeric, hyphens, underscores)"
            )
        if not self.description:
            errors.append(f"Task '{self.id}' must have a description")
        if self.priority < 0:
            errors.append(f"Task '{self.id}' priority must be non-negative")
        if self.max_budget_usd < 0:
            errors.append(f"Task '{self.id}' max_budget_usd must be non-negative")
        return errors

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSpec:
        """Create a TaskSpec from a dictionary, ignoring unknown keys."""
        known_fields = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


SPEC_VERSION = "1"


@dataclass
class ProjectSpec:
    """Specification for the entire project."""

    project_name: str
    tasks: list[TaskSpec]
    description: str = ""
    default_model: str = "opus"
    max_concurrency: int = 3
    global_context: str = ""
    global_context_files: list[str] = field(default_factory=list)
    user_stories: list[str] = field(default_factory=list)
    version: str = SPEC_VERSION

    def validate(self) -> list[str]:
        """Validate the entire project spec, returning a list of error messages."""
        errors: list[str] = []
        if not self.project_name:
            errors.append("project_name must not be empty")
        if not self.tasks:
            errors.append("Project must have at least one task")
        if self.max_concurrency < 1:
            errors.append("max_concurrency must be at least 1")

        # Validate individual tasks
        task_ids: set[str] = set()
        for task in self.tasks:
            if task.id in task_ids:
                errors.append(f"Duplicate task id: '{task.id}'")
            task_ids.add(task.id)
            errors.extend(task.validate())

        # Validate dependencies reference existing tasks
        for task in self.tasks:
            for dep in task.dependencies:
                if dep not in task_ids:
                    errors.append(f"Task '{task.id}' depends on unknown task '{dep}'")

        # Check for self-dependencies
        for task in self.tasks:
            if task.id in task.dependencies:
                errors.append(f"Task '{task.id}' depends on itself")

        return errors

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectSpec:
        """Create a ProjectSpec from a dictionary."""
        tasks_data = data.get("tasks", [])
        tasks = [TaskSpec.from_dict(t) for t in tasks_data]

        # Apply default_model to tasks that don't override it
        default_model = data.get("default_model", "opus")
        for i, task_data in enumerate(tasks_data):
            if "model" not in task_data:
                tasks[i].model = default_model

        return cls(
            project_name=data.get("project_name", ""),
            tasks=tasks,
            description=data.get("description", ""),
            default_model=default_model,
            max_concurrency=data.get("max_concurrency", 3),
            global_context=data.get("global_context", ""),
            global_context_files=data.get("global_context_files", []),
            user_stories=data.get("user_stories", []),
            version=data.get("version", SPEC_VERSION),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ProjectSpec:
        """Parse a ProjectSpec from a JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)
