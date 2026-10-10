from __future__ import annotations

from pathlib import Path

from ..sdk.sources import ImportResult, SourceAdapter

__all__ = [
    "GHOSTLAB_COPILOT_SESSION_ID_PREFIX",
    "GHOSTLAB_ORIGINATOR",
    "Importer",
    "ImportResult",
    "has_only_repo_state_capture",
    "is_ghostlab_copilot_session_id",
    "is_ghostlab_originator",
]

GHOSTLAB_ORIGINATOR = "ghostlab"
GHOSTLAB_COPILOT_SESSION_ID_PREFIX = "67686f73-746c-"
_REPO_STATE_FILES = frozenset({"repo_start.json", "repo_end.json"})


def is_ghostlab_originator(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() == GHOSTLAB_ORIGINATOR


def is_ghostlab_copilot_session_id(session_id: str) -> bool:
    return session_id.casefold().startswith(GHOSTLAB_COPILOT_SESSION_ID_PREFIX)


Importer = SourceAdapter


def has_only_repo_state_capture(raw_dir: Path) -> bool:
    if not raw_dir.is_dir():
        return False
    entries = list(raw_dir.iterdir())
    return bool(entries) and all(
        entry.is_file() and entry.name in _REPO_STATE_FILES for entry in entries
    )
