from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from retro.sdk import ImportResult, Layout, SessionDescriptor, write_events

from .normalizer import OpenCodeNormalizer, iter_source_records

LayoutKind = Literal["sessions", "project-sessions"]


@dataclass(frozen=True)
class _Session:
    session_id: str
    transcript: Path
    root: Path
    layout_kind: LayoutKind
    title: str
    cwd: str
    updated_at: int | float
    model: str
    project: str


class OpenCodeSource:
    RETRO_EXTENSION_API = "1"
    host = "opencode"
    display_name = "OpenCode"

    def __init__(
        self,
        layout: Layout,
        opencode_home: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ):
        self.layout = layout
        self.roots = [opencode_home] if opencode_home is not None else _resolve_roots(roots)
        self.normalizer = OpenCodeNormalizer()

    def discover(self) -> list[SessionDescriptor]:
        return [self._descriptor(session) for session in self._sessions()]

    def _descriptor(self, session: _Session) -> SessionDescriptor:
        return SessionDescriptor(
            host=self.host,
            session_id=session.session_id,
            title=session.title,
            cwd=session.cwd or None,
            updated_at=session.updated_at,
            source_path=session.transcript,
            metadata={
                "layout_kind": session.layout_kind,
                "opencode_home": str(session.root),
                "model": session.model,
                "project": session.project,
                "raw_filename": "session.jsonl",
            },
        )

    def _sessions(self) -> list[_Session]:
        found: list[_Session] = []
        seen: set[str] = set()
        for root in self.roots:
            for layout_kind, scan_dir in _scan_dirs(root):
                for transcript in sorted(scan_dir.rglob("*.jsonl")):
                    session = _session_from_transcript(
                        root,
                        layout_kind,
                        transcript,
                    )
                    if session is None or session.session_id in seen:
                        continue
                    seen.add(session.session_id)
                    found.append(session)
        return sorted(found, key=lambda session: session.updated_at, reverse=True)

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        session = next(
            (candidate for candidate in self._sessions() if candidate.session_id == identifier),
            None,
        )
        if session is None:
            raise FileNotFoundError(f"No OpenCode session found with id {identifier!r}")

        raw_dir = self.layout.raw_dir(self.host, identifier)
        raw_transcript = raw_dir / "session.jsonl"
        if raw_dir.exists() and not force and _capture_is_current(
            session.transcript,
            raw_transcript,
        ):
            raise FileExistsError(
                f"Raw capture already exists at {raw_dir} (pass force=True to overwrite)"
            )
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(session.transcript, raw_transcript)
        metadata = {
            **self._descriptor(session).metadata,
            "host": self.host,
            "session_id": identifier,
            "title": session.title,
            "cwd": session.cwd,
            "updated_at": session.updated_at,
            "source_path": str(session.transcript),
        }
        (raw_dir / "import_meta.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        events = list(
            self.normalizer.normalize(
                raw_dir=raw_dir,
                session_id=identifier,
            )
        )
        normalized_path = self.layout.normalized_path(self.host, identifier)
        count = write_events(normalized_path, events)
        unknown = [event for event in events if event.event_type == "unknown"]
        return ImportResult(
            host=self.host,
            session_id=identifier,
            raw_dir=raw_dir,
            normalized_path=normalized_path,
            event_count=count,
            unknown_event_count=len(unknown),
            gaps=sorted({event.summary for event in unknown}),
        )


def _resolve_roots(explicit: tuple[Path, ...] | None) -> list[Path]:
    if explicit:
        return [path.expanduser() for path in explicit]
    configured = os.environ.get("OPENCODE_HOME")
    if configured:
        roots = [
            Path(piece.strip()).expanduser()
            for piece in configured.split(",")
            if piece.strip()
        ]
        if roots:
            return roots
    return [Path.home() / ".opencode"]


def _scan_dirs(root: Path) -> list[tuple[LayoutKind, Path]]:
    result: list[tuple[LayoutKind, Path]] = []
    sessions = root / "sessions"
    if sessions.is_dir():
        result.append(("sessions", sessions))
    projects = root / "projects"
    if projects.is_dir():
        for project in sorted(projects.iterdir()):
            project_sessions = project / "sessions"
            if project_sessions.is_dir():
                result.append(("project-sessions", project_sessions))
    return result


def _session_from_transcript(
    root: Path,
    layout_kind: LayoutKind,
    transcript: Path,
) -> _Session | None:
    try:
        stat = transcript.stat()
    except FileNotFoundError:
        return None
    metadata: dict[str, Any] = {}
    first_user_text = ""
    for _, record in iter_source_records(transcript):
        if record.get("_retro_error"):
            continue
        metadata.update(_metadata_from_record(record))
        if not first_user_text and _record_role(record) == "user":
            first_user_text = _coerce_text(
                record.get("content") or record.get("message") or record.get("text")
            )
    session_id = str(metadata.get("session_id") or transcript.stem)
    if not session_id:
        return None
    project = str(metadata.get("project") or "")
    if not project and layout_kind == "project-sessions":
        try:
            project = transcript.relative_to(root / "projects").parts[0]
        except (ValueError, IndexError):
            project = ""
    title = str(metadata.get("title") or first_user_text or session_id)
    return _Session(
        session_id=session_id,
        transcript=transcript,
        root=root,
        layout_kind=layout_kind,
        title=title.strip().splitlines()[0][:120],
        cwd=str(metadata.get("cwd") or ""),
        updated_at=_numeric_time(
            metadata.get("updated_at") or metadata.get("timestamp"),
            stat.st_mtime,
        ),
        model=str(metadata.get("model") or ""),
        project=project,
    )


def _metadata_from_record(record: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    keys = ("session_id", "title", "cwd", "updated_at", "timestamp", "model", "project")
    for key in keys:
        if record.get(key) not in (None, ""):
            metadata[key] = record[key]
    if (
        record.get("id") not in (None, "")
        and record.get("type") in {"session", "session_start", "session_metadata"}
    ):
        metadata["session_id"] = record["id"]
    session = record.get("session")
    if isinstance(session, dict):
        for key in (*keys, "id"):
            if session.get(key) not in (None, ""):
                metadata["session_id" if key == "id" else key] = session[key]
    return metadata


def _record_role(record: dict[str, Any]) -> str:
    role = record.get("role") or record.get("actor")
    if isinstance(role, str):
        return role
    source_type = record.get("type")
    return source_type if isinstance(source_type, str) else ""


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            text
            for item in value
            for text in (
                item.get("text") if isinstance(item, dict) else str(item),
            )
            if isinstance(text, str) and text
        )
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return "" if value is None else str(value)


def _numeric_time(value: Any, fallback: float) -> int | float:
    return value if isinstance(value, (int, float)) else fallback


def _capture_is_current(source: Path, captured: Path) -> bool:
    if not captured.is_file():
        return False
    try:
        source_stat = source.stat()
        captured_stat = captured.stat()
    except OSError:
        return False
    return (
        source_stat.st_mtime_ns <= captured_stat.st_mtime_ns
        and source_stat.st_size <= captured_stat.st_size
    )
