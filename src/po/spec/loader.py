"""Load and validate spec JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from po.spec.schema import ProjectSpec


class SpecLoader(Protocol):
    """Protocol for loading project specs."""

    def load(self, path: Path) -> ProjectSpec: ...


class JsonSpecLoader:
    """Load a ProjectSpec from a JSON file."""

    def load(self, path: Path) -> ProjectSpec:
        """Load and validate a spec from a JSON file.

        Raises:
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
            ValueError: If the spec fails validation.
        """
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        spec = ProjectSpec.from_dict(data)
        errors = spec.validate()
        if errors:
            raise ValueError(f"Invalid spec: {'; '.join(errors)}")
        return spec
