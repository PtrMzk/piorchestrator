"""Nested BFS documentation tree generator.

Generates a layered documentation structure from a project spec:
  CLAUDE.md -> docs/L1 files -> docs/components/L2 files
"""

from __future__ import annotations

from pathlib import Path

from po.spec.schema import ProjectSpec


def generate_doc_tree(spec: ProjectSpec, project_root: Path) -> list[Path]:
    """Generate a BFS documentation tree from the project spec.

    Returns the list of created file paths.
    """
    created: list[Path] = []
    docs_dir = project_root / "docs"
    components_dir = docs_dir / "components"

    # Collect unique tags for component docs
    tags: set[str] = set()
    for task in spec.tasks:
        tags.update(task.tags)

    # Generate top-level CLAUDE.md — append if it already exists
    claude_md = project_root / "CLAUDE.md"
    claude_content = _build_claude_md(spec, tags)
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        separator = "\n\n---\n\n"
        marker = "<!-- po:generated -->"
        if marker in existing:
            # Replace only the PO-generated section
            before = existing.split(marker)[0].rstrip()
            claude_md.write_text(
                f"{before}\n\n{marker}\n{claude_content}",
                encoding="utf-8",
            )
        else:
            claude_md.write_text(
                f"{existing.rstrip()}{separator}{marker}\n{claude_content}",
                encoding="utf-8",
            )
    else:
        claude_md.write_text(claude_content, encoding="utf-8")
    created.append(claude_md)

    # Generate L1 docs
    docs_dir.mkdir(parents=True, exist_ok=True)

    system_design = docs_dir / "SYSTEM_DESIGN.md"
    system_design.write_text(_build_system_design(spec), encoding="utf-8")
    created.append(system_design)

    code_paths = docs_dir / "CODE_PATHS.md"
    code_paths.write_text(_build_code_paths(spec), encoding="utf-8")
    created.append(code_paths)

    api_contracts = docs_dir / "API_CONTRACTS.md"
    api_contracts.write_text(_build_api_contracts(spec), encoding="utf-8")
    created.append(api_contracts)

    # Generate L2 component docs from tags
    if tags:
        components_dir.mkdir(parents=True, exist_ok=True)
        for tag in sorted(tags):
            component_file = components_dir / f"{tag.upper()}.md"
            component_file.write_text(
                _build_component_doc(spec, tag), encoding="utf-8"
            )
            created.append(component_file)

    return created


def _build_claude_md(spec: ProjectSpec, tags: set[str]) -> str:
    lines = [
        f"# {spec.project_name}",
        "",
        spec.description or "No description provided.",
        "",
        "## Documentation",
        "",
        "- [System Design](docs/SYSTEM_DESIGN.md) — Architecture and key patterns",
        "- [Code Paths](docs/CODE_PATHS.md) — Module map and entry points",
        "- [API Contracts](docs/API_CONTRACTS.md) — Interfaces and data shapes",
    ]

    if tags:
        lines.append("")
        lines.append("### Components")
        for tag in sorted(tags):
            lines.append(f"- [{tag}](docs/components/{tag.upper()}.md)")

    if spec.global_context:
        lines.append("")
        lines.append("## Context")
        lines.append("")
        lines.append(spec.global_context)

    lines.append("")
    return "\n".join(lines)


def _build_system_design(spec: ProjectSpec) -> str:
    lines = [
        f"# System Design — {spec.project_name}",
        "",
        "## Overview",
        "",
        spec.description or "TODO: Add system overview.",
        "",
        "## Tasks",
        "",
    ]

    for task in spec.tasks:
        deps = ", ".join(task.dependencies) if task.dependencies else "none"
        lines.append(f"### {task.id}")
        lines.append(f"- **Description**: {task.description}")
        lines.append(f"- **Dependencies**: {deps}")
        lines.append(f"- **Output files**: {', '.join(task.output_files) or 'none'}")
        lines.append("")

    return "\n".join(lines)


def _build_code_paths(spec: ProjectSpec) -> str:
    lines = [
        f"# Code Paths — {spec.project_name}",
        "",
        "## Output File Map",
        "",
    ]

    file_to_tasks: dict[str, list[str]] = {}
    for task in spec.tasks:
        for f in task.output_files:
            file_to_tasks.setdefault(f, []).append(task.id)

    for filepath in sorted(file_to_tasks):
        tasks = ", ".join(file_to_tasks[filepath])
        lines.append(f"- `{filepath}` — tasks: {tasks}")

    lines.append("")
    return "\n".join(lines)


def _build_api_contracts(spec: ProjectSpec) -> str:
    lines = [
        f"# API Contracts — {spec.project_name}",
        "",
        "## Task Interfaces",
        "",
        "TODO: Document interfaces and data shapes for the project.",
        "",
    ]
    return "\n".join(lines)


def _build_component_doc(spec: ProjectSpec, tag: str) -> str:
    lines = [
        f"# Component: {tag}",
        "",
        "## Tasks",
        "",
    ]

    for task in spec.tasks:
        if tag in task.tags:
            lines.append(f"### {task.id}")
            lines.append(f"{task.description}")
            lines.append("")

    return "\n".join(lines)
