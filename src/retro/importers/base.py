from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


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
