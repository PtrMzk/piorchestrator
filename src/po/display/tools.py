"""Human-readable summaries for Claude tool_use blocks."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def tool_summary(block: dict[str, Any]) -> str:
    """Return a human-readable one-line summary of a tool_use block.

    Mappings:
        Read        → "Read <basename>"
        Glob / Grep → "Glob <pattern>" / "Grep <pattern>"
        Bash        → description field, or truncated command
        Edit / Write→ "Edit <basename>" / "Write <basename>"
        Task        → "Task <description>"
        Fallback    → just the tool name
    """
    name = str(block.get("name", "?"))
    inp = block.get("input", {})

    if name in ("Read", "read"):
        path = inp.get("file_path", "")
        return f"Read {PurePosixPath(path).name}" if path else "Read"

    if name in ("Glob", "glob"):
        pattern = inp.get("pattern", "")
        return f"Glob {pattern}" if pattern else "Glob"

    if name in ("Grep", "grep"):
        pattern = inp.get("pattern", "")
        return f"Grep '{pattern}'" if pattern else "Grep"

    if name in ("Bash", "bash"):
        desc = inp.get("description", "")
        if desc:
            return f"Bash: {desc[:60]}"
        cmd = inp.get("command", "")
        if cmd:
            return f"Bash: {cmd[:60]}"
        return "Bash"

    if name in ("Edit", "edit"):
        path = inp.get("file_path", "")
        return f"Edit {PurePosixPath(path).name}" if path else "Edit"

    if name in ("Write", "write", "write_file"):
        path = inp.get("file_path", "")
        return f"Write {PurePosixPath(path).name}" if path else "Write"

    if name in ("Task", "task"):
        desc = inp.get("description", "")
        return f"Task: {desc[:60]}" if desc else "Task"

    return name
