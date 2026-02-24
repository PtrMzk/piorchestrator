"""Generate a PO spec file from a plain English description using Claude."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from po.spec.schema import ProjectSpec

_EXAMPLE_SPEC = """\
{
  "project_name": "todo-api",
  "description": "REST API for a todo list",
  "default_model": "sonnet",
  "max_concurrency": 3,
  "global_context": "Use Python 3.12, FastAPI, and SQLite.",
  "global_context_files": ["README.md"],
  "tasks": [
    {
      "id": "init-project",
      "description": "Create pyproject.toml, src/app/main.py with FastAPI app",
      "dependencies": [],
      "context_files": [],
      "output_files": [
        "pyproject.toml",
        "src/app/__init__.py",
        "src/app/main.py"
      ],
      "verification": "python -c \\"from app.main import app\\"",
      "priority": 10,
      "model": "sonnet",
      "max_budget_usd": 1.0,
      "tags": ["setup"]
    },
    {
      "id": "define-models",
      "description": "Create src/app/models.py with Pydantic models",
      "dependencies": ["init-project"],
      "context_files": [],
      "output_files": ["src/app/models.py"],
      "verification": "python -c \\"from app.models import TodoCreate\\"",
      "priority": 9,
      "model": "sonnet",
      "max_budget_usd": 1.0,
      "tags": ["models"]
    },
    {
      "id": "define-db",
      "description": "Create src/app/database.py with SQLite management",
      "dependencies": ["init-project"],
      "context_files": [],
      "output_files": ["src/app/database.py"],
      "verification": "python -c \\"from app.database import init_db\\"",
      "priority": 9,
      "model": "sonnet",
      "max_budget_usd": 1.0,
      "tags": ["database"]
    },
    {
      "id": "crud-endpoints",
      "description": "Add CRUD endpoints for /todos to src/app/main.py",
      "dependencies": ["define-models", "define-db"],
      "context_files": [
        "src/app/models.py",
        "src/app/database.py"
      ],
      "output_files": ["src/app/main.py"],
      "verification": "python -c \\"from app.main import app\\"",
      "priority": 8,
      "model": "sonnet",
      "max_budget_usd": 1.0,
      "tags": ["api", "crud"]
    }
  ]
}"""


def _build_init_prompt(description: str) -> str:
    """Build the prompt that asks Claude to generate a PO spec."""
    return f"""\
Generate a valid PO orchestrator spec JSON file for the following project description:

{description}

The spec must be a JSON object with these fields:
- "project_name" (string, required): short kebab-case identifier
- "description" (string): one-line summary
- "default_model" (string): model for tasks, use "sonnet"
- "max_concurrency" (integer): how many tasks can run in parallel, typically 3
- "global_context" (string): shared instructions for all tasks
- "global_context_files" (list of strings): files all tasks should read
- "tasks" (list, required): array of task objects

Each task object has:
- "id" (string, required): unique kebab-case identifier
- "description" (string, required): what the task should accomplish (be specific and detailed)
- "dependencies" (list of strings): task IDs that must complete first
- "context_files" (list of strings): files the task should read
- "output_files" (list of strings): files the task will create or modify
- "verification" (string): shell command to verify the task succeeded
- "priority" (integer): higher runs first among independent tasks (10 = highest)
- "model" (string): "sonnet" for most tasks, "opus" for complex ones
- "max_budget_usd" (float): cost cap per task, typically 1.0
- "tags" (list of strings): categorical labels

Constraints:
- Tasks should be broken into small, focused units of work
- Dependencies must form a DAG (no cycles)
- Task IDs must be alphanumeric with hyphens/underscores only
- output_files should list every file the task creates or modifies
- context_files should list files the task needs to read from prior tasks
- verification commands should be quick import checks or unit test runs
- Start with a setup/init task that other tasks depend on

Here is a complete example of a valid spec:

{_EXAMPLE_SPEC}

Return ONLY the JSON object, no markdown fences or explanation."""


def _invoke_claude(prompt: str, model: str) -> str:
    """Call the Claude CLI synchronously and return raw stdout."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "text",
        "--model", model,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(f"Claude CLI failed (exit {result.returncode}): {stderr}")
    return result.stdout


def _extract_json(raw: str) -> dict:
    """Extract a JSON object from Claude's response.

    Handles markdown fences, preamble text, and trailing text.
    """
    text = raw.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown fences
    if "```" in text:
        # Find content between first ``` and last ```
        parts = text.split("```")
        for part in parts[1:]:
            # Skip the language identifier line if present (e.g., "json\n")
            candidate = part.strip()
            if candidate.startswith(("json", "JSON")):
                candidate = candidate.split("\n", 1)[-1].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

    # Try to find JSON object in the text by locating first { and last }
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError("Could not extract valid JSON from Claude's response")


def generate_spec(description: str, output: Path, model: str = "sonnet") -> Path:
    """Generate a PO spec from a description, validate it, and write to file.

    Args:
        description: Plain English project description.
        output: Path to write the spec JSON file.
        model: Claude model to use.

    Returns:
        The path the spec was written to.

    Raises:
        FileExistsError: If the output file already exists.
        ValueError: If the generated spec fails validation.
        RuntimeError: If the Claude CLI invocation fails.
    """
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")

    prompt = _build_init_prompt(description)
    raw = _invoke_claude(prompt, model)
    data = _extract_json(raw)

    # Validate through ProjectSpec
    spec = ProjectSpec.from_dict(data)
    errors = spec.validate()
    if errors:
        raise ValueError(f"Generated spec failed validation: {'; '.join(errors)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return output
