"""Normalized event schema shared by all host importers.

The wire format intentionally mirrors the JSON shape in
`specs/full_rollout_capture_feature_spec.md` so that downstream tools can
consume it without consulting Python.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Actor = Literal["user", "assistant", "tool", "system", "subagent", "hook"]
EventType = Literal[
    "message",
    "tool_call",
    "tool_result",
    "file_read",
    "file_edit",
    "command",
    "error",
    "session_start",
    "session_end",
    "subagent_start",
    "subagent_end",
    "compaction",
    "permission",
    "attachment",
    "reasoning",
    "unknown",
]
BuiltinHost = Literal["claude-code", "codex", "vscode-copilot"]
Host = str
BUILTIN_HOSTS: tuple[BuiltinHost, ...] = ("claude-code", "codex", "vscode-copilot")
HOSTS = BUILTIN_HOSTS
HOST_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


def validate_host_id(host: str) -> Host:
    if not isinstance(host, str) or not HOST_ID_PATTERN.fullmatch(host):
        raise ValueError(
            f"invalid host ID {host!r}; expected a lowercase path-safe ID matching "
            "^[a-z][a-z0-9-]*$"
        )
    return host


def validate_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            f"invalid session ID {session_id!r}; expected a path-safe identifier "
            "containing letters, digits, dots, underscores, or hyphens"
        )
    return session_id


@dataclass
class RawRef:
    path: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line}


@dataclass
class NormalizedEvent:
    event_id: str
    session_id: str
    host: Host
    sequence: int
    actor: Actor
    event_type: EventType
    summary: str
    raw_ref: RawRef
    timestamp: str | None = None
    parent_event_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.host = validate_host_id(self.host)
        self.session_id = validate_session_id(self.session_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["raw_ref"] = self.raw_ref.to_dict()
        return d


def write_events(path: Path, events: Iterable[NormalizedEvent]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(json.dumps(ev.to_dict(), ensure_ascii=False))
                fh.write("\n")
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return count


def read_events(path: Path) -> Iterator[NormalizedEvent]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            raw_ref = d.pop("raw_ref")
            yield NormalizedEvent(raw_ref=RawRef(**raw_ref), **d)
