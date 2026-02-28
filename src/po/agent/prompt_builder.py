"""Build agent prompts from task specs and context."""

from __future__ import annotations


def build_prompt(
    task_id: str,
    description: str,
    global_context: str,
    context_files_content: dict[str, str],
    verification: str,
    output_files: list[str],
    previous_error: str = "",
) -> str:
    """Build the prompt string sent to the Claude Code agent."""
    parts: list[str] = []

    parts.append(f"# Task: {task_id}\n")
    parts.append(description)

    if previous_error:
        parts.append(
            "\n## RETRY — Previous Attempt Failed\n"
            "This is a retry. A previous attempt already made progress but "
            "failed verification. The branch contains the previous work.\n\n"
            "**Error:**\n"
            f"```\n{previous_error}\n```\n\n"
            "**IMPORTANT — Retry rules:**\n"
            "1. You have limited turns. Do NOT investigate extensively — "
            "go straight to fixing the error.\n"
            "2. Run the verification command FIRST to reproduce the failure, "
            "then fix the minimum needed to make it pass.\n"
            "3. Commit immediately after the fix passes verification.\n"
            "4. Do NOT do cosmetic cleanup, README updates, or refactoring — "
            "ONLY fix what's broken."
        )

    if global_context:
        parts.append(f"\n## Project Context\n{global_context}")

    if context_files_content:
        parts.append("\n## Reference Files")
        for filepath, content in context_files_content.items():
            parts.append(f"\n### {filepath}\n```\n{content}\n```")

    if output_files:
        parts.append(f"\n## Expected Output Files\n{', '.join(output_files)}")

    if verification:
        parts.append(
            "\n## Verification\n"
            f"After completing your work, run this command to verify: `{verification}`"
        )

    parts.append("\n## Rules")
    parts.append(
        "- Follow TDD: write a failing test first, then implement "
        "the minimum code to make it pass."
    )
    parts.append("- Commit after every meaningful change to protect against timeouts.")
    parts.append("- Keep changes focused on the task description above.")
    parts.append(
        "- If this task is too large to complete, write subtasks to "
        "`.po-subtasks.json` as a JSON array of objects with fields: "
        "id, description, dependencies, output_files, verification."
    )
    parts.append(
        "- If you cannot complete this task, write the reason to `.po-failure.json` "
        'as a JSON object with a "reason" field.'
    )

    return "\n".join(parts)
