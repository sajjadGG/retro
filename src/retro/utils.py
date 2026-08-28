"""Shared helpers used across importers, signals, and mining."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .schema import NormalizedEvent


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    """Yield (line_number, parsed_dict) for each non-empty line in a JSONL file."""
    with path.open("r", encoding="utf-8") as fh:
        for i, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except json.JSONDecodeError:
                continue


def event_text(ev: NormalizedEvent) -> str:
    """Extract the best textual content from a normalized event."""
    payload = ev.payload or {}
    for key in ("text", "message", "thinking"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    raw = payload.get("raw_content")
    if raw is not None:
        if isinstance(raw, str):
            return raw
        return json.dumps(raw, ensure_ascii=False)
    return ev.summary or ""


def iter_messages(
    events: Sequence[NormalizedEvent], actor: str | None = None
) -> Iterator[NormalizedEvent]:
    """Yield message-type events, optionally filtered by actor."""
    for ev in events:
        if ev.event_type != "message":
            continue
        if actor is None or ev.actor == actor:
            yield ev


def truncate(text: str, limit: int) -> str:
    """Truncate text to *limit* characters, appending ellipsis if trimmed."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def truncate_summary(text: str, limit: int = 200) -> str:
    """Flatten newlines and truncate, suitable for single-line summary fields."""
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def artifact_ref(path: Path, archive_root: Path) -> str:
    """Return an archive-relative path when *path* is inside the archive."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except ValueError:
        return str(path)


def resolve_artifact_ref(reference: str, archive_root: Path) -> Path:
    path = Path(reference)
    return path if path.is_absolute() else archive_root / path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
