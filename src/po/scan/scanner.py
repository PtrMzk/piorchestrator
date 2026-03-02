"""Scan an existing codebase and generate documentation using Claude."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console
from rich.status import Status

from po.config import ensure_logs_dir
from po.display.tools import tool_summary

logger = logging.getLogger(__name__)


def _build_scan_prompt(output_dir: str) -> str:
    """Build a prompt that instructs Claude to analyze a codebase and create docs."""
    return f"""\
Analyze this codebase and generate comprehensive documentation.

Create a nested documentation structure under the directory `{output_dir}/`.

You MUST create the following files:

1. `{output_dir}/index.md` — Top-level overview: project purpose, tech stack, \
architecture summary, and links to component docs.

2. For each major module/component, create a dedicated doc file under `{output_dir}/`, e.g.:
   - `{output_dir}/api.md` — API endpoints, request/response formats
   - `{output_dir}/models.md` — Data models, schemas, relationships
   - `{output_dir}/auth.md` — Authentication/authorization flow
   - `{output_dir}/database.md` — Database schema, migrations, queries
   - (adapt file names to the actual codebase structure)

Each component doc should cover:
- Purpose and responsibility
- Public API surface (functions, classes, endpoints)
- Key data structures
- Integration points with other components
- Important implementation details

Guidelines:
- Keep docs concise and focused on what another developer needs to know
- Use code references (file paths, function names) liberally
- Focus on architecture and design decisions, not line-by-line code review
- Create only as many doc files as needed — don't create docs for trivial modules
- Write in markdown format"""


def _invoke_scan_agent(
    prompt: str, model: str, project_root: Path,
) -> str:
    """Call the Claude CLI synchronously to scan the codebase.

    Returns the result text from the agent.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    cmd = [
        "claude",
        "-p", prompt,
        "--verbose",
        "--output-format", "stream-json",
        "--model", model,
        "--permission-mode", "bypassPermissions",
    ]

    log_dir = ensure_logs_dir(project_root)
    log_file = log_dir / "scan.jsonl"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    result_text: str | None = None
    assert proc.stdout is not None
    stderr_console = Console(stderr=True)
    status = Status("Scanning codebase…", console=stderr_console)
    status.start()

    with open(log_file, "wb") as fh:
        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    fh.write(raw_line)
                    fh.flush()
                    continue

                msg["timestamp"] = datetime.now(UTC).isoformat()
                fh.write(json.dumps(msg).encode())
                fh.write(b"\n")
                fh.flush()

                if msg.get("type") == "assistant":
                    content = msg.get("message", {}).get("content")
                    if isinstance(content, list):
                        for block in content:
                            if block.get("type") == "tool_use":
                                status.update(tool_summary(block))
                            elif block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    first_line = text.split("\n")[0][:80]
                                    status.update(f"> {first_line}")
                    elif isinstance(content, str) and content.strip():
                        first_line = content.strip().split("\n")[0][:80]
                        status.update(f"> {first_line}")

                if msg.get("type") == "result":
                    result_text = msg.get("result", "")
        finally:
            status.stop()

    proc.wait()

    if proc.returncode != 0:
        assert proc.stderr is not None
        err = proc.stderr.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Claude CLI failed (exit {proc.returncode}): {err}")

    if result_text is None:
        raise RuntimeError("Claude CLI returned no result")

    logger.info("Scan logs written to %s", log_file)
    return result_text


def scan_codebase(
    model: str = "opus",
    output_dir: str = "docs/codebase",
    project_root: Path | None = None,
) -> Path:
    """Scan the codebase and generate documentation.

    Args:
        model: Claude model to use for scanning.
        output_dir: Relative path for output docs (from project root).
        project_root: Project root directory.

    Returns:
        The absolute path to the output directory.

    Raises:
        RuntimeError: If the Claude CLI invocation fails or no docs created.
    """
    root = (project_root or Path(".")).resolve()

    prompt = _build_scan_prompt(output_dir)
    _invoke_scan_agent(prompt, model, root)

    docs_path = root / output_dir
    if not docs_path.exists():
        raise RuntimeError(
            f"Scan completed but output directory '{output_dir}' was not created. "
            "The agent may not have generated any documentation files."
        )

    return docs_path
