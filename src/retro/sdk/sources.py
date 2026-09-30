"""Public contracts for Retro source extensions."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..schema import Host, validate_host_id, validate_session_id


@dataclass(frozen=True)
class SessionDescriptor:
    host: Host
    session_id: str
    title: str = ""
    cwd: str | None = None
    updated_at: int | float | None = None
    source_path: Path | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_host_id(self.host)
        validate_session_id(self.session_id)


@dataclass
class ImportResult:
    host: Host
    session_id: str
    raw_dir: Path
    normalized_path: Path
    event_count: int
    unknown_event_count: int = 0
    gaps: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.host = validate_host_id(self.host)
        self.session_id = validate_session_id(self.session_id)


@runtime_checkable
class SourceAdapter(Protocol):
    host: Host
    display_name: str

    def discover(self) -> list[SessionDescriptor]: ...

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult: ...
