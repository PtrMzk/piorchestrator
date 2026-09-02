#!/usr/bin/env python3
"""Mock Claude CLI binary for integration tests.

Impersonates the ``claude`` CLI by accepting the same flags and emitting
stream-json (JSONL) output.  Behaviour is determined by the prompt content:

- **Spec mode** (prompt contains ``"Generate a valid PO orchestrator spec"``):
  Returns a canned 3-task linear spec as the ``result`` text.
- **Task mode** (prompt starts with ``"# Task:"``):
  Parses expected output files from the prompt, writes stubs into the cwd
  (the worktree), ``git add . && git commit``, and emits a success result.
- **Fallback**: Emits a generic success result.

Failure modes, selected by markers in the prompt so a test can script them
through a spec's task description or ``po init`` description:

- ``mock:fail`` in a task prompt: writes ``.po-failure.json`` (the agent gave
  up) and exits 0, which is how a real agent reports failure.
- ``mock:bad-spec`` in a spec prompt: returns text that is not JSON.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid

# ── Canned spec ──────────────────────────────────────────────────────────

CANNED_SPEC: dict = {
    "project_name": "mock-project",
    "description": "A mock project for integration testing",
    "default_model": "haiku",
    "max_concurrency": 1,
    "global_context": "Integration test project.",
    "global_context_files": [],
    "tasks": [
        {
            "id": "setup",
            "description": "Create the initial setup file.",
            "dependencies": [],
            "context_files": [],
            "output_files": ["setup.txt"],
            "verification": "test -f setup.txt",
            "priority": 10,
            "model": "haiku",
            "max_budget_usd": 1.0,
            "tags": ["setup"],
        },
        {
            "id": "feature-a",
            "description": "Create feature A, depends on setup.",
            "dependencies": ["setup"],
            "context_files": ["setup.txt"],
            "output_files": ["feature_a.txt"],
            "verification": "test -f feature_a.txt",
            "priority": 5,
            "model": "haiku",
            "max_budget_usd": 1.0,
            "tags": ["feature"],
        },
        {
            "id": "feature-b",
            "description": "Create feature B, depends on feature-a.",
            "dependencies": ["feature-a"],
            "context_files": ["feature_a.txt"],
            "output_files": ["feature_b.txt"],
            "verification": "test -f feature_b.txt",
            "priority": 3,
            "model": "haiku",
            "max_budget_usd": 1.0,
            "tags": ["feature"],
        },
    ],
}

# ── Helpers ───────────────────────────────────────────────────────────────


def _emit(obj: dict) -> None:
    """Write a JSON object as a single line to stdout."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _emit_result(text: str) -> None:
    """Emit the standard assistant + result JSONL pair."""
    _emit(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Working..."}]},
        }
    )
    _emit(
        {
            "type": "result",
            "result": text,
            "cost_usd": 0.01,
            "total_cost_usd": 0.01,
            "session_id": f"mock-{uuid.uuid4().hex[:8]}",
            "num_turns": 1,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
    )


def _parse_output_files(prompt: str) -> list[str]:
    """Extract output files from the ``## Expected Output Files`` section."""
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Expected Output Files"):
            # The file list is on the same line after the heading
            # Format: "## Expected Output Files\nfile_a.txt, file_b.txt"
            continue
        # The next non-empty line after the heading is the file list
        if "Expected Output Files" in prompt:
            idx = prompt.index("Expected Output Files")
            rest = prompt[idx:]
            lines = rest.splitlines()
            if len(lines) >= 2:
                file_line = lines[1].strip()
                return [f.strip() for f in file_line.split(",") if f.strip()]
    return []


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock Claude CLI")
    parser.add_argument("-p", "--prompt", required=True)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--max-budget-usd", type=float, default=None)
    parser.add_argument("--resume", default=None)

    args = parser.parse_args()
    prompt: str = args.prompt

    # ── Spec mode ─────────────────────────────────────────────────────
    if "Generate a valid PO orchestrator spec" in prompt:
        if "mock:bad-spec" in prompt:
            _emit_result("I could not produce a spec for that, sorry.")
            return
        _emit_result(json.dumps(CANNED_SPEC, indent=2))
        return

    # ── Task mode ─────────────────────────────────────────────────────
    if "# Task:" in prompt:
        if "mock:fail" in prompt:
            from pathlib import Path

            Path(".po-failure.json").write_text(json.dumps({"reason": "mock agent gave up"}))
            _emit_result("Could not complete the task.")
            return

        output_files = _parse_output_files(prompt)
        for fname in output_files:
            from pathlib import Path

            p = Path(fname)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# generated by mock claude for {fname}\n")

        if output_files:
            subprocess.run(
                ["git", "add", "."],
                capture_output=True,
                check=True,
            )
            # --allow-empty: a retry reuses the branch, so the files may be
            # identical to what the previous attempt already committed.
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Mock agent work"],
                capture_output=True,
                check=True,
            )

        _emit_result(f"Created files: {', '.join(output_files)}")
        return

    # ── Fallback ──────────────────────────────────────────────────────
    _emit_result("Generic success from mock claude.")


if __name__ == "__main__":
    main()
