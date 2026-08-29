from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

GHOSTLAB_ORIGINATOR = "ghostlab"
GHOSTLAB_COPILOT_SESSION_ID_PREFIX = "67686f73-746c-"


def is_ghostlab_originator(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() == GHOSTLAB_ORIGINATOR


def is_ghostlab_copilot_session_id(session_id: str) -> bool:
    return session_id.casefold().startswith(GHOSTLAB_COPILOT_SESSION_ID_PREFIX)


@dataclass
class ImportResult:
    host: str
    session_id: str
    raw_dir: Path
    normalized_path: Path
    event_count: int
    unknown_event_count: int = 0
    gaps: list[str] = field(default_factory=list)


class Importer(Protocol):
    host: str

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult: ...

    def discover(self) -> list[dict]:
        """Return a list of session descriptors visible to this host."""
        ...
