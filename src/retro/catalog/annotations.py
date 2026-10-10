"""Canonical append-only user annotations."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import TAG_PATTERN
from ..utils import atomic_write_text

ANNOTATION_OPS = {"tag", "untag", "set-project", "clear-project", "note"}


@dataclass
class AnnotationState:
    added_tags: set[str] = field(default_factory=set)
    suppressed_tags: set[str] = field(default_factory=set)
    project: str | None = None
    project_is_set: bool = False
    notes: list[str] = field(default_factory=list)
    entries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return self._digest_ops(ANNOTATION_OPS)

    @property
    def tag_digest(self) -> str:
        return self._digest_ops({"tag", "untag"})

    @property
    def project_digest(self) -> str:
        return self._digest_ops({"set-project", "clear-project"})

    def _digest_ops(self, operations: set[str]) -> str:
        encoded = json.dumps(
            [entry for entry in self.entries if entry["op"] in operations],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def annotations_path(root: Path) -> Path:
    return root / "catalog" / "annotations.jsonl"


def load_annotations(root: Path) -> dict[str, AnnotationState]:
    path = annotations_path(root)
    if not path.is_file():
        return {}
    states: dict[str, AnnotationState] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid catalog annotation at {path}:{line_no}: {exc}") from exc
            _validate_annotation(entry, where=f"{path}:{line_no}")
            state = states.setdefault(entry["key"], AnnotationState())
            state.entries.append(entry)
            op = entry["op"]
            value = entry.get("value")
            if op == "tag":
                state.added_tags.add(value)
                state.suppressed_tags.discard(value)
            elif op == "untag":
                state.added_tags.discard(value)
                state.suppressed_tags.add(value)
            elif op == "set-project":
                state.project = value
                state.project_is_set = True
            elif op == "clear-project":
                state.project = None
                state.project_is_set = False
            elif op == "note":
                state.notes.append(value)
    return states


def append_annotation(
    root: Path,
    *,
    key: str,
    op: str,
    value: str | None,
) -> dict[str, Any]:
    entry = {
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "key": key,
        "op": op,
        "value": value,
    }
    _validate_annotation(entry, where="annotation")
    path = annotations_path(root)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    atomic_write_text(
        path,
        existing + json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n",
    )
    return entry


def _validate_annotation(value: Any, *, where: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(value) - {"at", "key", "op", "value"})
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")
    at = value.get("at")
    if not isinstance(at, str):
        raise ValueError(f"{where}.at must be an ISO-8601 string")
    try:
        datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where}.at must be an ISO-8601 string") from exc
    key = value.get("key")
    if not isinstance(key, str) or "/" not in key:
        raise ValueError(f"{where}.key must be a host/session key")
    op = value.get("op")
    if op not in ANNOTATION_OPS:
        raise ValueError(f"{where}.op must be one of {', '.join(sorted(ANNOTATION_OPS))}")
    annotation_value = value.get("value")
    if op == "clear-project":
        if annotation_value is not None:
            raise ValueError(f"{where}.value must be null for clear-project")
        return
    if not isinstance(annotation_value, str) or not annotation_value.strip():
        raise ValueError(f"{where}.value must be a non-empty string for {op}")
    if op in {"tag", "untag"} and not TAG_PATTERN.fullmatch(annotation_value):
        raise ValueError(f"{where}.value must match {TAG_PATTERN.pattern}")
