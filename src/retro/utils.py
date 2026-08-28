"""Shared helpers used across importers, signals, and mining."""
from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .schema import NormalizedEvent

_PATH_KEYS = ("file_path", "filePath", "filepath", "path", "file", "uri")
_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (?P<path>.+?)\s*$",
    re.MULTILINE,
)
_PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (?P<path>.+?)\s*$", re.MULTILINE)
_DIFF_PATH_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>[^\t\n]+)", re.MULTILINE)
_FILE_URI_RE = re.compile(r"file://[^\s)\]}'\"]+")
_EDIT_RESULT_PATH_RES = (
    re.compile(r"^The file (?P<path>/.*?) has been updated successfully", re.MULTILINE),
    re.compile(r"^File created successfully at: (?P<path>/.*?)(?: \(|$)", re.MULTILINE),
    re.compile(
        r"^(?:Added|Updated|Modified|Deleted) \d+ file\(s\): (?P<path>/.*?)(?: \(|$)",
        re.MULTILINE,
    ),
)


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


def event_command_text(ev: NormalizedEvent) -> str:
    """Return a normalized command string for command-like events."""
    payload = ev.payload or {}
    for key in ("command", "cmd"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    for container_key in ("arguments", "input"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            for key in ("command", "cmd"):
                value = container.get(key)
                if isinstance(value, str):
                    return value
        elif isinstance(container, str) and ev.event_type == "command":
            return container
    return ""


def event_file_paths(
    ev: NormalizedEvent,
    *,
    include_command_candidates: bool = False,
) -> set[str]:
    """Extract file-path candidates from normalized file and command events."""
    payload = ev.payload or {}
    paths: set[str] = set()
    _collect_mapping_paths(payload, paths)

    for container_key in ("arguments", "input"):
        value = payload.get(container_key)
        if isinstance(value, str) and ev.event_type == "file_edit":
            paths.update(_patch_paths(value))

    if ev.event_type == "file_edit":
        for value in payload.values():
            if isinstance(value, str):
                paths.update(_patch_paths(value))
        paths.update(_embedded_edit_paths(payload))

    if include_command_candidates and ev.event_type == "command":
        paths.update(_command_path_candidates(event_command_text(ev)))
    return {unquote(path[7:]) if path.startswith("file://") else path for path in paths}


def _collect_mapping_paths(value: dict[str, Any], paths: set[str]) -> None:
    for key in _PATH_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            paths.add(candidate)

    changes = value.get("changes")
    if isinstance(changes, dict):
        paths.update(str(path) for path in changes if isinstance(path, str))

    for container_key in ("arguments", "input"):
        container = value.get(container_key)
        if isinstance(container, dict):
            _collect_mapping_paths(container, paths)


def _patch_paths(text: str) -> set[str]:
    paths = {match.group("path").strip() for match in _PATCH_PATH_RE.finditer(text)}
    paths.update(match.group("path").strip() for match in _PATCH_MOVE_RE.finditer(text))
    for match in _DIFF_PATH_RE.finditer(text):
        path = match.group("path").strip()
        if path != "/dev/null":
            paths.add(path)
    return paths


def _embedded_edit_paths(value: Any) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        uris = value.get("uris")
        if isinstance(uris, dict):
            paths.update(uri for uri in uris if isinstance(uri, str))
        for key in (
            "content",
            "detailedContent",
            "invocationMessage",
            "output",
            "result",
            "value",
            "vscode_chat_invocation",
        ):
            if key in value:
                paths.update(_embedded_edit_paths(value[key]))
        return paths
    if isinstance(value, list):
        for item in value:
            paths.update(_embedded_edit_paths(item))
        return paths
    if not isinstance(value, str):
        return paths

    paths.update(_FILE_URI_RE.findall(value))
    paths.update(_patch_paths(value))
    for pattern in _EDIT_RESULT_PATH_RES:
        paths.update(match.group("path").strip() for match in pattern.finditer(value))
    return paths


def _command_path_candidates(command: str) -> set[str]:
    if not command:
        return set()
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    paths: set[str] = set()
    for token in tokens:
        candidate = token.strip(" \t\r\n'\"`()[]{};,")
        if not candidate or candidate.startswith("-") or "://" in candidate:
            continue
        if "=" in candidate and not candidate.startswith(("./", "../", "/")):
            candidate = candidate.split("=", 1)[1]
        candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
        if candidate.startswith("file://"):
            paths.add(candidate)
            continue
        if "/" in candidate or re.search(r"\.[A-Za-z0-9][A-Za-z0-9_-]{0,12}$", candidate):
            paths.add(candidate)
    return paths


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
