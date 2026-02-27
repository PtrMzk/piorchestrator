"""Generate a PO spec file from a plain English description using Claude."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from po.config import logs_dir
from po.spec.schema import ProjectSpec

logger = logging.getLogger(__name__)

_EXAMPLE_SPEC = """\
{
  "project_name": "todo-api",
  "description": "REST API for a todo list",
  "default_model": "opus",
  "max_concurrency": 3,
  "global_context": "TypeScript on Bun. Prefer built-in Bun APIs. Only well-known deps.",
  "global_context_files": ["README.md"],
  "tasks": [
    {
      "id": "init-project",
      "description": "Create package.json, tsconfig.json, src/index.ts with Bun.serve",
      "dependencies": [],
      "context_files": [],
      "output_files": [
        "package.json",
        "tsconfig.json",
        "src/index.ts"
      ],
      "verification": "bun test --timeout 5000",
      "priority": 10,
      "model": "opus",
      "max_budget_usd": 1.0,
      "tags": ["setup"]
    },
    {
      "id": "define-models",
      "description": "TDD: tests/models.test.ts first, then src/models.ts",
      "dependencies": ["init-project"],
      "context_files": [],
      "output_files": ["src/models.ts", "tests/models.test.ts"],
      "verification": "bun test tests/models.test.ts",
      "priority": 9,
      "model": "opus",
      "max_budget_usd": 1.0,
      "tags": ["models"]
    },
    {
      "id": "define-db",
      "description": "TDD: tests/database.test.ts first, then src/database.ts with bun:sqlite",
      "dependencies": ["init-project"],
      "context_files": [],
      "output_files": ["src/database.ts", "tests/database.test.ts"],
      "verification": "bun test tests/database.test.ts",
      "priority": 9,
      "model": "opus",
      "max_budget_usd": 1.0,
      "tags": ["database"]
    },
    {
      "id": "doc-models",
      "description": "Document the models module: API surface, purpose, and integration points",
      "dependencies": ["define-models"],
      "context_files": ["src/models.ts", "tests/models.test.ts"],
      "output_files": ["docs/models.md"],
      "verification": "test -f docs/models.md",
      "priority": 7,
      "model": "sonnet",
      "max_budget_usd": 0.50,
      "tags": ["docs"]
    },
    {
      "id": "doc-db",
      "description": "Document the database module: API surface, purpose, and integration points",
      "dependencies": ["define-db"],
      "context_files": ["src/database.ts", "tests/database.test.ts"],
      "output_files": ["docs/database.md"],
      "verification": "test -f docs/database.md",
      "priority": 7,
      "model": "sonnet",
      "max_budget_usd": 0.50,
      "tags": ["docs"]
    },
    {
      "id": "crud-endpoints",
      "description": "TDD: tests/routes.test.ts first, then CRUD in src/routes.ts",
      "dependencies": ["define-models", "define-db"],
      "context_files": [
        "src/models.ts",
        "src/database.ts",
        "docs/models.md",
        "docs/database.md"
      ],
      "output_files": ["src/routes.ts", "tests/routes.test.ts"],
      "verification": "bun test tests/routes.test.ts",
      "priority": 8,
      "model": "opus",
      "max_budget_usd": 1.0,
      "tags": ["api", "crud"]
    },
    {
      "id": "doc-routes",
      "description": "Document the routes module: API surface, purpose, and integration points",
      "dependencies": ["crud-endpoints"],
      "context_files": ["src/routes.ts", "tests/routes.test.ts"],
      "output_files": ["docs/routes.md"],
      "verification": "test -f docs/routes.md",
      "priority": 6,
      "model": "sonnet",
      "max_budget_usd": 0.50,
      "tags": ["docs"]
    },
    {
      "id": "e2e-todo-crud",
      "description": "Playwright e2e test: user can add a todo and see it in the list",
      "dependencies": ["crud-endpoints"],
      "context_files": ["src/routes.ts", "docs/routes.md"],
      "output_files": ["tests/e2e/todo-crud.spec.ts"],
      "verification": "bunx playwright test tests/e2e/todo-crud.spec.ts",
      "priority": 5,
      "model": "opus",
      "max_budget_usd": 1.0,
      "tags": ["e2e", "playwright"]
    }
  ],
  "user_stories": ["User can add a todo and see it in the list"]
}"""


def _build_outline_prompt(description: str, feedback: str | None = None) -> str:
    """Build a prompt that asks Claude for a high-level spec outline."""
    base = f"""\
Given this project description, generate a high-level execution plan outline.

Project description:
{description}

Show the outline in this format:
- Project name (kebab-case)
- One-line description
- Tech stack / global context
- User stories (extracted from description)
- Tasks grouped by execution layer, where each task shows:
  - Task ID (kebab-case)
  - Brief description (1 line)
  - Dependencies (task IDs)
  - Key output files

Guidelines:
- Break work into small, focused tasks
- Start with a setup/init task
- Follow TDD: feature tasks should write tests first
- Include doc companion tasks for implementation tasks
- Include Playwright e2e tasks for user stories
- Dependencies must form a DAG (no cycles)

Do NOT generate JSON. Return a readable markdown outline only."""

    if feedback:
        base += f"""

Previous outline was rejected. User feedback:
{feedback}

Please incorporate the feedback and generate an updated outline."""

    return base


def generate_outline(
    description: str,
    model: str = "opus",
    project_root: Path | None = None,
    feedback: str | None = None,
) -> str:
    """Generate a high-level spec outline from a description.

    Args:
        description: Plain English project description.
        model: Claude model to use.
        project_root: Project root for log output.
        feedback: Optional user feedback on a previous outline.

    Returns:
        Markdown outline string.
    """
    prompt = _build_outline_prompt(description, feedback)
    return _invoke_claude(prompt, model, project_root=project_root)


def _build_spec_from_outline_prompt(description: str, outline: str) -> str:
    """Build prompt to generate the full JSON spec from an approved outline."""
    return f"""\
Generate a valid PO orchestrator spec JSON file based on the approved outline below.

Original project description:
{description}

Approved outline:
{outline}

{_spec_schema_instructions()}

Return ONLY the JSON object, no markdown fences or explanation."""


def _spec_schema_instructions() -> str:
    """Return the shared schema and constraint instructions for spec generation."""
    return f"""\
The spec must be a JSON object with these fields:
- "project_name" (string, required): short kebab-case identifier
- "description" (string): one-line summary
- "default_model" (string): model for tasks, use "opus"
- "max_concurrency" (integer): how many tasks can run in parallel, typically 3
- "global_context" (string): shared instructions for all tasks
- "global_context_files" (list of strings): files all tasks should read
- "user_stories" (list of strings): plain-English user stories
- "tasks" (list, required): array of task objects

Each task object has:
- "id" (string, required): unique kebab-case identifier
- "description" (string, required): what the task should accomplish (be specific and detailed)
- "dependencies" (list of strings): task IDs that must complete first
- "context_files" (list of strings): files the task should read
- "output_files" (list of strings): files the task will create or modify
- "verification" (string): shell command to verify the task succeeded
- "priority" (integer): higher runs first among independent tasks (10 = highest)
- "model" (string): "opus" for most tasks, "sonnet" for simple ones
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
- Extract user stories from the project description
- For each user story, emit a Playwright e2e task with one story per task
- Follow TDD: each feature task must include test files in output_files
- Task descriptions should explicitly say "write failing tests first, then implement"
- Verification commands should run the test suite (e.g. pytest, bun test)
- Prefer TypeScript on Bun with built-in APIs over third-party packages
- When a dependency is needed, only use well-known, actively maintained packages
- For each implementation task (not setup/init, not e2e), generate a companion doc-<feature> task
- Doc task depends on its parent and reads the parent's output_files as context_files
- Doc task writes a single markdown file to docs/<feature>.md
- Doc tasks use model "sonnet", low budget (0.50), tag ["docs"]
- Subsequent implementation tasks should include relevant docs/*.md files in their context_files
- Doc content scope: module's API surface, purpose, and integration points (keep concise)

Here is a complete example of a valid spec:

{_EXAMPLE_SPEC}"""


def generate_spec_from_outline(
    description: str,
    outline: str,
    output: Path,
    model: str = "opus",
    project_root: Path | None = None,
) -> Path:
    """Generate a full PO spec JSON from an approved outline.

    Args:
        description: Original project description.
        outline: Approved markdown outline.
        output: Path to write the spec JSON file.
        model: Claude model to use.
        project_root: Project root for log output.

    Returns:
        The path the spec was written to.

    Raises:
        FileExistsError: If the output file already exists.
        ValueError: If the generated spec fails validation.
        RuntimeError: If the Claude CLI invocation fails.
    """
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")

    root = project_root or output.parent.resolve()

    prompt = _build_spec_from_outline_prompt(description, outline)
    raw = _invoke_claude(prompt, model, project_root=root)
    data = _extract_json(raw)

    # Validate through ProjectSpec
    spec = ProjectSpec.from_dict(data)
    errors = spec.validate()
    if errors:
        raise ValueError(f"Generated spec failed validation: {'; '.join(errors)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return output


def _build_init_prompt(description: str) -> str:
    """Build the prompt that asks Claude to generate a PO spec."""
    return f"""\
Generate a valid PO orchestrator spec JSON file for the following project description:

{description}

{_spec_schema_instructions()}

Return ONLY the JSON object, no markdown fences or explanation."""


def _invoke_claude(prompt: str, model: str, project_root: Path | None = None) -> str:
    """Call the Claude CLI synchronously, stream logs to disk, and return the result."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    cmd = [
        "claude",
        "-p", prompt,
        "--verbose",
        "--output-format", "stream-json",
        "--model", model,
    ]

    log_file = None
    if project_root is not None:
        log_dir = logs_dir(project_root)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "init.jsonl"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    result_text: str | None = None
    assert proc.stdout is not None
    stderr = Console(stderr=True)

    fh = open(log_file, "wb") if log_file else None
    try:
        for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                if fh:
                    fh.write(raw_line)
                    fh.flush()
                continue

            msg["timestamp"] = datetime.now(timezone.utc).isoformat()
            if fh:
                fh.write(json.dumps(msg).encode())
                fh.write(b"\n")
                fh.flush()

            if msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use":
                            stderr.print(f"  [dim cyan][tool][/] [dim]{block.get('name', '?')}[/]")
                        elif block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                line = text.split("\n")[0][:100]
                                stderr.print(f"  [dim]> {line}[/]")
                elif isinstance(content, str) and content.strip():
                    line = content.strip().split("\n")[0][:100]
                    stderr.print(f"  [dim]> {line}[/]")

            if msg.get("type") == "result":
                result_text = msg.get("result", "")
    finally:
        if fh:
            fh.close()

    proc.wait()

    if proc.returncode != 0:
        assert proc.stderr is not None
        stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {stderr}")

    if result_text is None:
        raise RuntimeError("Claude CLI returned no result")

    if log_file:
        logger.info("Logs written to %s", log_file)

    return result_text


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


def generate_spec(
    description: str,
    output: Path,
    model: str = "opus",
    project_root: Path | None = None,
) -> Path:
    """Generate a PO spec from a description, validate it, and write to file.

    Args:
        description: Plain English project description.
        output: Path to write the spec JSON file.
        model: Claude model to use.
        project_root: Project root for log output (defaults to output's parent).

    Returns:
        The path the spec was written to.

    Raises:
        FileExistsError: If the output file already exists.
        ValueError: If the generated spec fails validation.
        RuntimeError: If the Claude CLI invocation fails.
    """
    if output.exists():
        raise FileExistsError(f"Output file already exists: {output}")

    root = project_root or output.parent.resolve()

    prompt = _build_init_prompt(description)
    raw = _invoke_claude(prompt, model, project_root=root)
    data = _extract_json(raw)

    # Validate through ProjectSpec
    spec = ProjectSpec.from_dict(data)
    errors = spec.validate()
    if errors:
        raise ValueError(f"Generated spec failed validation: {'; '.join(errors)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return output
