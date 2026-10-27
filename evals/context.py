"""Optional local source inputs for context-aware evaluation runs."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CaseContextSource:
    checkout: Path | None = None
    rules: Path | None = None


class ContextManifestError(ValueError):
    """A context manifest is malformed or refers to missing local inputs."""


def load_context_manifest(path: Path, case_ids: set[str]) -> dict[str, CaseContextSource]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextManifestError(f"cannot read context manifest {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ContextManifestError("context manifest must be a JSON object keyed by case id")
    result: dict[str, CaseContextSource] = {}
    for case_id, entry in raw.items():
        if case_id not in case_ids:
            raise ContextManifestError(f"context manifest has unknown case id {case_id!r}")
        if not isinstance(entry, dict) or not entry or set(entry) - {"checkout", "rules"}:
            raise ContextManifestError(
                f"context entry for {case_id!r} must contain checkout and/or rules paths"
            )
        paths: dict[str, Path | None] = {"checkout": None, "rules": None}
        for name in paths:
            value: Any = entry.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ContextManifestError(f"{name} for {case_id!r} must be a path string")
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            candidate = candidate.resolve()
            if name == "checkout" and not candidate.is_dir():
                raise ContextManifestError(
                    f"checkout for {case_id!r} is not a directory: {candidate}"
                )
            if name == "rules" and not candidate.is_file():
                raise ContextManifestError(f"rules for {case_id!r} is not a file: {candidate}")
            paths[name] = candidate
        result[case_id] = CaseContextSource(**paths)
    return result
