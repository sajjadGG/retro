"""Importer for Copilot CLI and VS Code Agent Host session rollouts."""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schema import Actor, EventType, Host, NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import artifact_ref, truncate_summary
from .base import (
    ImportResult,
    has_only_repo_state_capture,
    is_ghostlab_copilot_session_id,
)
from .vscode_copilot import (
    _completion_event_type,
    _copy_stable,
    _export_session_store,
    _iso_timestamp,
    _source_is_newer,
    _tool_event_type,
    _tool_summary,
)

COPILOT_HOME_ENV = "COPILOT_HOME"
COPILOT_SESSION_STATE_DIRS_ENV = "COPILOT_SESSION_STATE_DIRS"

_SESSION_METADATA_FILES = (
    "workspace.yaml",
    "vscode.metadata.json",
    "vscode.requests.metadata.json",
)


@dataclass
class CopilotCliSession:
    session_id: str
    state_dir: Path
    events_path: Path
    session_store_db: Path | None
    cwd: str
    repository: str
    branch: str
    title: str
    models: tuple[str, ...]
    request_count: int
    size_bytes: int
    mtime: float
    created_at: str | None
    updated_at: str | None
    active: bool
    workspace_name: str
    source_kind: str = "copilot-cli"

    @property
    def display_title(self) -> str:
        return self.title[:120]

    @property
    def display_model(self) -> str:
        return ", ".join(self.models[:2]) if self.models else "-"


def _default_copilot_home() -> Path:
    explicit = os.environ.get(COPILOT_HOME_ENV)
    if explicit:
        return Path(explicit).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home()))).expanduser()
    return state_home / ".copilot"


def _resolve_session_state_dirs(
    explicit: tuple[Path, ...] | None = None,
) -> list[Path]:
    if explicit:
        return [path.expanduser() for path in explicit]
    env = os.environ.get(COPILOT_SESSION_STATE_DIRS_ENV)
    if env:
        roots = [Path(piece.strip()).expanduser() for piece in env.split(",") if piece.strip()]
        if roots:
            return roots
    return [_default_copilot_home() / "session-state"]


class CopilotCliImporter:
    host: Host = "vscode-copilot"

    def __init__(
        self,
        layout: Layout,
        session_state_dir: Path | None = None,
        session_store_db: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ):
        self.layout = layout
        self.session_state_dirs = (
            [session_state_dir.expanduser()]
            if session_state_dir is not None
            else _resolve_session_state_dirs(roots)
        )
        self.explicit_session_store_db = (
            session_store_db.expanduser() if session_store_db is not None else None
        )
        self._discover_cache: list[CopilotCliSession] | None = None

    def discover(self) -> list[CopilotCliSession]:
        if self._discover_cache is not None:
            return list(self._discover_cache)
        sessions: dict[str, CopilotCliSession] = {}
        for state_root in self.session_state_dirs:
            if not state_root.is_dir():
                continue
            store_db = self.explicit_session_store_db or state_root.parent / "session-store.db"
            index = _read_session_index(store_db)
            for state_dir in state_root.iterdir():
                events_path = state_dir / "events.jsonl"
                if not state_dir.is_dir() or not events_path.is_file():
                    continue
                session_id = state_dir.name
                if is_ghostlab_copilot_session_id(session_id):
                    continue
                metadata = index.get(session_id, {})
                preview_title, preview_models = _read_event_preview(events_path)
                workspace = _read_workspace_metadata(state_dir / "workspace.yaml")
                cwd = _first_text(metadata.get("cwd"), workspace.get("cwd"))
                repository = _first_text(
                    metadata.get("repository"),
                    workspace.get("repository"),
                    workspace.get("git_root"),
                )
                branch = _first_text(metadata.get("branch"), workspace.get("branch"))
                title = _first_text(metadata.get("summary"), preview_title, session_id)
                models = tuple(
                    dict.fromkeys(
                        [
                            *metadata.get("models", ()),
                            *preview_models,
                        ]
                    )
                )
                stat = events_path.stat()
                session = CopilotCliSession(
                    session_id=session_id,
                    state_dir=state_dir,
                    events_path=events_path,
                    session_store_db=store_db if store_db.is_file() else None,
                    cwd=cwd,
                    repository=repository,
                    branch=branch,
                    title=truncate_summary(title, 180),
                    models=models,
                    request_count=int(metadata.get("turn_count") or 0),
                    size_bytes=stat.st_size,
                    mtime=stat.st_mtime,
                    created_at=_optional_text(metadata.get("created_at")),
                    updated_at=_optional_text(metadata.get("updated_at")),
                    active=any(state_dir.glob("inuse.*.lock")),
                    workspace_name=_workspace_name(cwd, repository),
                )
                previous = sessions.get(session_id)
                if previous is None or session.mtime > previous.mtime:
                    sessions[session_id] = session
        self._discover_cache = sorted(
            sessions.values(),
            key=lambda session: session.mtime,
            reverse=True,
        )
        return list(self._discover_cache)

    def find_session(self, session_id: str) -> CopilotCliSession | None:
        return next(
            (session for session in self.discover() if session.session_id == session_id),
            None,
        )

    def latest(self) -> CopilotCliSession | None:
        sessions = self.discover()
        return sessions[0] if sessions else None

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        session = self.find_session(identifier)
        if session is None:
            roots = ", ".join(str(path) for path in self.session_state_dirs)
            raise FileNotFoundError(
                f"No Copilot CLI or Agent Host session found with id {identifier!r} under {roots}"
            )

        raw_dir = self.layout.raw_dir(self.host, session.session_id)
        fingerprint = _capture_fingerprint(session)
        if (
            raw_dir.exists()
            and not force
            and not has_only_repo_state_capture(raw_dir)
            and not _source_is_newer(raw_dir, fingerprint)
        ):
            raise FileExistsError(
                f"Raw capture already exists at {raw_dir} (pass force=True to overwrite)"
            )

        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_events = raw_dir / "events.jsonl"
        _copy_stable(session.events_path, raw_events, attempts=10)
        self._snapshot_sidecars(session, raw_dir)

        meta = {
            "host": self.host,
            "session_id": session.session_id,
            "source_kind": session.source_kind,
            "title": session.title,
            "cwd": session.cwd,
            "repository": session.repository,
            "branch": session.branch,
            "workspace_name": session.workspace_name,
            "models": list(session.models),
            "request_count": session.request_count,
            "active": session.active,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "source_events": str(session.events_path),
            "source_state_dir": str(session.state_dir),
            "source_session_store": (
                str(session.session_store_db) if session.session_store_db else None
            ),
            "size_bytes": session.size_bytes,
            "captured_mtime_ns": fingerprint[0],
            "captured_size_bytes": fingerprint[1],
        }
        (raw_dir / "import_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        events, unknown, gaps = self._normalize(raw_events, session.session_id)
        normalized_path = self.layout.normalized_path(self.host, session.session_id)
        count = write_events(normalized_path, events)
        return ImportResult(
            host=self.host,
            session_id=session.session_id,
            raw_dir=raw_dir,
            normalized_path=normalized_path,
            event_count=count,
            unknown_event_count=unknown,
            gaps=gaps,
        )

    def _snapshot_sidecars(self, session: CopilotCliSession, raw_dir: Path) -> None:
        sidecars = raw_dir / "sidecars"
        for name in _SESSION_METADATA_FILES:
            source = session.state_dir / name
            if source.is_file():
                _copy_stable(source, sidecars / name)
        if session.session_store_db is not None:
            exported = _export_session_store(
                session.session_store_db,
                session.session_id,
            )
            if exported is not None:
                sidecars.mkdir(parents=True, exist_ok=True)
                (sidecars / "session-store.json").write_text(
                    json.dumps(exported, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

    def _normalize(
        self,
        events_path: Path,
        session_id: str,
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()
        current_user_event_id: str | None = None
        session_start_event_id: str | None = None
        assistant_by_turn: dict[str, str] = {}
        tool_event_ids: dict[str, str] = {}
        tool_events: dict[str, NormalizedEvent] = {}
        tool_names: dict[str, str] = {}
        external_tool_event_ids: dict[str, str] = {}
        subagent_event_ids: dict[str, str] = {}
        chunk_buffers: dict[tuple[Any, ...], list[tuple[int, dict[str, Any]]]] = {}

        def emit(
            *,
            raw: dict[str, Any],
            line_no: int,
            suffix: str = "",
            actor: Actor,
            event_type: EventType,
            summary: str,
            payload: dict[str, Any],
            parent_event_id: str | None = None,
            raw_event_ids: list[str] | None = None,
            raw_lines: list[int] | None = None,
        ) -> str:
            raw_id = str(raw.get("id") or f"line-{line_no}")
            event_id = f"{session_id}:agent-host:{raw_id}{suffix}"
            event_payload = dict(payload)
            if raw.get("parentId"):
                event_payload["raw_parent_event_id"] = raw["parentId"]
            if raw.get("agentId"):
                event_payload["agent_id"] = raw["agentId"]
            if raw_event_ids:
                event_payload["raw_event_ids"] = raw_event_ids
            if raw_lines:
                event_payload["raw_lines"] = raw_lines
            events.append(
                NormalizedEvent(
                    event_id=event_id,
                    session_id=session_id,
                    host=self.host,
                    sequence=len(events) + 1,
                    actor=actor,
                    event_type=event_type,
                    summary=summary,
                    raw_ref=RawRef(
                        path=artifact_ref(events_path, self.layout.root),
                        line=line_no,
                    ),
                    timestamp=_iso_timestamp(raw.get("timestamp")),
                    parent_event_id=parent_event_id,
                    payload=event_payload,
                )
            )
            return event_id

        def logical_parent(data: dict[str, Any]) -> str | None:
            parent_call = data.get("parentToolCallId")
            if parent_call:
                return tool_event_ids.get(str(parent_call))
            turn_id = data.get("turnId")
            if turn_id and str(turn_id) in assistant_by_turn:
                return assistant_by_turn[str(turn_id)]
            return current_user_event_id or session_start_event_id

        def process_assistant_message(
            raw: dict[str, Any],
            line_no: int,
            *,
            raw_event_ids: list[str] | None = None,
            raw_lines: list[int] | None = None,
        ) -> None:
            data = _event_data(raw)
            parent = logical_parent(data)
            actor: Actor = (
                "subagent"
                if data.get("parentToolCallId") or raw.get("agentId")
                else "assistant"
            )
            content = data.get("content")
            text = content if isinstance(content, str) else ""
            reasoning = data.get("reasoningText")
            thinking = reasoning if isinstance(reasoning, str) else ""
            turn_id = str(data.get("turnId") or "")
            message_payload = {
                key: value
                for key, value in data.items()
                if key
                not in {
                    "content",
                    "encryptedContent",
                    "reasoningText",
                    "reasoningOpaque",
                    "toolRequests",
                }
            }
            canonical_event_id: str | None = None
            if thinking:
                canonical_event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    suffix="#reasoning" if text else "",
                    actor=actor,
                    event_type="reasoning",
                    summary=truncate_summary(thinking),
                    payload={"thinking": thinking, **message_payload},
                    parent_event_id=parent,
                    raw_event_ids=raw_event_ids,
                    raw_lines=raw_lines,
                )
            if text:
                canonical_event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    actor=actor,
                    event_type="message",
                    summary=truncate_summary(text),
                    payload={"text": text, **message_payload},
                    parent_event_id=parent,
                    raw_event_ids=raw_event_ids,
                    raw_lines=raw_lines,
                )
            if canonical_event_id is not None and turn_id:
                assistant_by_turn[turn_id] = canonical_event_id

            for index, request in enumerate(data.get("toolRequests") or []):
                if not isinstance(request, dict):
                    continue
                call_id = str(request.get("toolCallId") or "")
                existing_call = tool_events.get(call_id)
                if existing_call is not None:
                    existing_call.payload["request"] = request
                    existing_call.payload["requested_only"] = False
                    continue
                name = str(request.get("name") or request.get("toolName") or "unknown")
                arguments = request.get("arguments")
                call_type = _tool_event_type(name)
                call_event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    suffix=f"#tool-request-{index}",
                    actor="subagent" if actor == "subagent" else "assistant",
                    event_type=call_type,
                    summary=_tool_summary(name, arguments),
                    payload={
                        "name": name,
                        "tool_name": name,
                        "tool_id": call_id or None,
                        "call_id": call_id or None,
                        "input": arguments,
                        "requested_only": True,
                        "request": request,
                    },
                    parent_event_id=canonical_event_id or parent,
                    raw_event_ids=raw_event_ids,
                    raw_lines=raw_lines,
                )
                if call_id:
                    tool_event_ids[call_id] = call_event_id
                    tool_events[call_id] = events[-1]
                    tool_names[call_id] = name

            if (
                canonical_event_id is None
                and not data.get("toolRequests")
                and data.get("encryptedContent")
            ):
                encrypted = data.get("encryptedContent")
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor=actor,
                    event_type="attachment",
                    summary="encrypted assistant message",
                    payload={
                        **message_payload,
                        "encrypted_content_bytes": len(str(encrypted)),
                    },
                    parent_event_id=parent,
                    raw_event_ids=raw_event_ids,
                    raw_lines=raw_lines,
                )

        def flush_chunks(key: tuple[Any, ...]) -> None:
            buffer = chunk_buffers.pop(key, [])
            if not buffer:
                return
            ordered = sorted(
                buffer,
                key=lambda item: int(_event_data(item[1]).get("chunkIndex") or 0),
            )
            first_line, first_raw = ordered[0]
            first_data = dict(_event_data(first_raw))
            expected = int(first_data.get("chunkCount") or len(ordered))
            indexes = [
                int(_event_data(raw).get("chunkIndex") or 0)
                for _, raw in ordered
            ]
            if indexes != list(range(expected)):
                gaps.add("assistant.message/incomplete_chunks")
            first_data["content"] = "".join(
                str(_event_data(raw).get("content") or "")
                for _, raw in ordered
            )
            first_data["reasoningText"] = "".join(
                str(_event_data(raw).get("reasoningText") or "")
                for _, raw in ordered
            )
            requests: list[Any] = []
            seen_calls: set[str] = set()
            for _, raw in ordered:
                for request in _event_data(raw).get("toolRequests") or []:
                    if not isinstance(request, dict):
                        requests.append(request)
                        continue
                    call_id = str(request.get("toolCallId") or "")
                    if call_id and call_id in seen_calls:
                        continue
                    if call_id:
                        seen_calls.add(call_id)
                    requests.append(request)
            first_data["toolRequests"] = requests
            first_data["persisted_chunk_count"] = len(ordered)
            first_data.pop("chunkIndex", None)
            first_data.pop("chunkCount", None)
            merged = dict(first_raw)
            merged["data"] = first_data
            process_assistant_message(
                merged,
                first_line,
                raw_event_ids=[
                    str(raw.get("id") or f"line-{line}")
                    for line, raw in ordered
                ],
                raw_lines=[line for line, _ in ordered],
            )

        def process_event(line_no: int, raw: dict[str, Any]) -> None:
            nonlocal unknown, current_user_event_id, session_start_event_id
            event_type = raw.get("type")
            data = _event_data(raw)
            if event_type == "session.start":
                session_start_event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="session_start",
                    summary="Copilot Agent Host session start",
                    payload=data,
                )
            elif event_type == "session.resume":
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="attachment",
                    summary="Copilot Agent Host session resume",
                    payload=data,
                    parent_event_id=session_start_event_id,
                )
            elif event_type == "session.shutdown":
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="session_end",
                    summary=f"session shutdown: {data.get('shutdownType') or 'unknown'}",
                    payload=data,
                    parent_event_id=current_user_event_id or session_start_event_id,
                )
            elif event_type == "user.message":
                content = data.get("content")
                text = content if isinstance(content, str) else ""
                current_user_event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    actor="user",
                    event_type="message",
                    summary=truncate_summary(text or "empty user message"),
                    payload={"text": text, **data},
                    parent_event_id=session_start_event_id,
                )
            elif event_type == "assistant.message":
                process_assistant_message(raw, line_no)
            elif event_type in {"assistant.turn_start", "assistant.turn_end"}:
                actor: Actor = "subagent" if raw.get("agentId") else "assistant"
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor=actor,
                    event_type="attachment",
                    summary=str(event_type),
                    payload=data,
                    parent_event_id=logical_parent(data),
                )
            elif event_type == "tool.execution_start":
                call_id = str(data.get("toolCallId") or raw.get("id") or line_no)
                name = str(data.get("toolName") or "unknown")
                arguments = data.get("arguments")
                normalized_type = _tool_event_type(name)
                parent = logical_parent(data)
                existing_call = tool_events.get(call_id)
                if existing_call is not None:
                    existing_call.actor = (
                        "subagent" if data.get("parentToolCallId") else "assistant"
                    )
                    existing_call.event_type = normalized_type
                    existing_call.summary = _tool_summary(name, arguments)
                    existing_call.payload.update(
                        {
                            **data,
                            "name": name,
                            "tool_name": name,
                            "tool_id": call_id,
                            "call_id": call_id,
                            "input": arguments,
                            "requested_only": False,
                            "execution_raw_ref": {
                                "path": str(events_path),
                                "line": line_no,
                            },
                            "execution_raw_event_id": raw.get("id"),
                        }
                    )
                    call_event_id = existing_call.event_id
                else:
                    call_event_id = emit(
                        raw=raw,
                        line_no=line_no,
                        actor="subagent" if data.get("parentToolCallId") else "assistant",
                        event_type=normalized_type,
                        summary=_tool_summary(name, arguments),
                        payload={
                            **data,
                            "name": name,
                            "tool_name": name,
                            "tool_id": call_id,
                            "call_id": call_id,
                            "input": arguments,
                            "requested_only": False,
                        },
                        parent_event_id=parent,
                    )
                    tool_events[call_id] = events[-1]
                tool_event_ids[call_id] = call_event_id
                tool_names[call_id] = name
            elif event_type == "tool.execution_complete":
                call_id = str(data.get("toolCallId") or raw.get("id") or line_no)
                start_id = tool_event_ids.get(call_id)
                name = tool_names.get(call_id, "unknown")
                call_type = _tool_event_type(name)
                normalized_type = _completion_event_type(call_type)
                success = data.get("success")
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor=(
                        "subagent"
                        if normalized_type == "subagent_end"
                        or data.get("parentToolCallId")
                        else "tool"
                    ),
                    event_type=normalized_type,
                    summary=f"{name}: {'completed' if success is not False else 'failed'}",
                    payload={
                        **data,
                        "name": name,
                        "tool_name": name,
                        "tool_id": call_id,
                        "call_id": call_id,
                        "is_error": success is False,
                        "output": data.get("result"),
                    },
                    parent_event_id=start_id,
                )
            elif event_type == "external_tool.requested":
                request_id = str(data.get("requestId") or raw.get("id") or line_no)
                name = str(data.get("toolName") or "external_tool")
                event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    actor="assistant",
                    event_type=_tool_event_type(name),
                    summary=_tool_summary(name, data.get("arguments")),
                    payload={
                        **data,
                        "name": name,
                        "tool_name": name,
                        "call_id": data.get("toolCallId") or request_id,
                        "input": data.get("arguments"),
                    },
                    parent_event_id=current_user_event_id,
                )
                external_tool_event_ids[request_id] = event_id
            elif event_type == "external_tool.completed":
                request_id = str(data.get("requestId") or raw.get("id") or line_no)
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="tool",
                    event_type="tool_result",
                    summary="external tool completed",
                    payload=data,
                    parent_event_id=external_tool_event_ids.get(request_id),
                )
            elif event_type == "permission.requested":
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="permission",
                    summary="permission requested",
                    payload=data,
                    parent_event_id=current_user_event_id,
                )
            elif event_type == "permission.completed":
                call_id = str(data.get("toolCallId") or "")
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="permission",
                    summary="permission completed",
                    payload=data,
                    parent_event_id=tool_event_ids.get(call_id) or current_user_event_id,
                )
            elif event_type in {"hook.start", "hook.end"}:
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="hook",
                    event_type="attachment",
                    summary=f"{event_type}: {data.get('hookType') or 'unknown'}",
                    payload=data,
                    parent_event_id=current_user_event_id,
                )
            elif event_type in {"system.message", "system.notification"}:
                content = data.get("content")
                text = content if isinstance(content, str) else str(event_type)
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="message",
                    summary=truncate_summary(text),
                    payload={"text": text, **data},
                    parent_event_id=current_user_event_id or session_start_event_id,
                )
            elif event_type == "subagent.started":
                call_id = str(data.get("toolCallId") or raw.get("id") or line_no)
                event_id = emit(
                    raw=raw,
                    line_no=line_no,
                    actor="subagent",
                    event_type="subagent_start",
                    summary=f"subagent started: {data.get('agentDisplayName') or data.get('agentName')}",
                    payload=data,
                    parent_event_id=tool_event_ids.get(call_id) or current_user_event_id,
                )
                subagent_event_ids[call_id] = event_id
            elif event_type in {"subagent.completed", "subagent.failed"}:
                call_id = str(data.get("toolCallId") or "")
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="subagent",
                    event_type="subagent_end",
                    summary=(
                        f"subagent {'failed' if event_type == 'subagent.failed' else 'completed'}: "
                        f"{data.get('agentDisplayName') or data.get('agentName')}"
                    ),
                    payload={
                        **data,
                        "success": event_type != "subagent.failed",
                        "is_error": event_type == "subagent.failed",
                    },
                    parent_event_id=(
                        subagent_event_ids.get(call_id)
                        or tool_event_ids.get(call_id)
                        or current_user_event_id
                    ),
                )
            elif event_type == "session.workspace_file_changed":
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="file_edit",
                    summary=f"{data.get('operation') or 'changed'} {data.get('path') or 'file'}",
                    payload=data,
                    parent_event_id=current_user_event_id,
                )
            elif event_type == "session.permissions_changed":
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="permission",
                    summary="session permissions changed",
                    payload=data,
                    parent_event_id=current_user_event_id or session_start_event_id,
                )
            elif event_type in {"session.error", "abort"}:
                message = data.get("message") or data.get("reason") or event_type
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="error",
                    summary=truncate_summary(str(message)),
                    payload=data,
                    parent_event_id=current_user_event_id or session_start_event_id,
                )
            elif event_type == "session.binary_asset":
                compact = {
                    key: value
                    for key, value in data.items()
                    if key != "data"
                }
                compact["data_omitted_from_normalized"] = "data" in data
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="attachment",
                    summary=f"binary asset: {data.get('mimeType') or 'unknown'}",
                    payload=compact,
                    parent_event_id=current_user_event_id,
                )
            elif event_type == "skill.invoked":
                compact = {
                    key: value
                    for key, value in data.items()
                    if key != "content"
                }
                if isinstance(data.get("content"), str):
                    compact["content_length"] = len(data["content"])
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="attachment",
                    summary=f"skill invoked: {data.get('name') or 'unknown'}",
                    payload=compact,
                    parent_event_id=current_user_event_id,
                )
            elif event_type in {
                "session.mode_changed",
                "session.model_change",
                "session.task_complete",
                "session.usage_checkpoint",
                "subagent.deselected",
            }:
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="attachment",
                    summary=str(event_type),
                    payload=data,
                    parent_event_id=current_user_event_id or session_start_event_id,
                )
            else:
                unknown += 1
                gaps.add(str(event_type or "<missing>"))
                emit(
                    raw=raw,
                    line_no=line_no,
                    actor="system",
                    event_type="unknown",
                    summary=f"unknown Agent Host event type={event_type}",
                    payload=raw,
                    parent_event_id=current_user_event_id or session_start_event_id,
                )

        for line_no, raw in _iter_jsonl_strict(events_path):
            if raw.get("type") == "assistant.message":
                data = _event_data(raw)
                count = data.get("chunkCount")
                index = data.get("chunkIndex")
                if isinstance(count, int) and count > 1 and isinstance(index, int):
                    candidate_key = (
                        data.get("turnId"),
                        data.get("parentToolCallId"),
                        data.get("interactionId"),
                        data.get("model"),
                        count,
                    )
                    buffer = chunk_buffers.setdefault(candidate_key, [])
                    buffer.append((line_no, raw))
                    if len(buffer) == count:
                        flush_chunks(candidate_key)
                    continue
            process_event(line_no, raw)
        for candidate_key in list(chunk_buffers):
            flush_chunks(candidate_key)
        return events, unknown, sorted(gaps)


def _read_session_index(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.is_file():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "sessions" not in tables:
            return {}
        out = {
            str(row["id"]): dict(row)
            for row in con.execute("SELECT * FROM sessions")
        }
        if "turns" in tables:
            for row in con.execute(
                "SELECT session_id, COUNT(*) AS turn_count FROM turns GROUP BY session_id"
            ):
                out.setdefault(str(row["session_id"]), {})["turn_count"] = row["turn_count"]
        if "assistant_usage_events" in tables:
            for row in con.execute(
                "SELECT session_id, model, COUNT(*) AS usage_count "
                "FROM assistant_usage_events GROUP BY session_id, model "
                "ORDER BY usage_count DESC"
            ):
                metadata = out.setdefault(str(row["session_id"]), {})
                metadata.setdefault("models", []).append(str(row["model"]))
        return out
    finally:
        con.close()


def _read_event_preview(events_path: Path) -> tuple[str, tuple[str, ...]]:
    title = ""
    models: list[str] = []
    with events_path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid Copilot Agent Host JSONL at {events_path}:{line_no}: {exc}"
                ) from exc
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Copilot Agent Host record at {events_path}:{line_no} is not an object"
                )
            data = _event_data(raw)
            model = data.get("selectedModel") or data.get("model")
            if isinstance(model, str) and model and model not in models:
                models.append(model)
            if raw.get("type") == "user.message":
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    title = content
                    break
    return truncate_summary(title, 180), tuple(models)


def _read_workspace_metadata(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    out: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        value = raw_value.strip()
        if not key.strip():
            continue
        try:
            out[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            out[key.strip()] = value.strip("'\"")
    return out


def _iter_jsonl_strict(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record at {path}:{line_no} is not an object")
            yield line_no, value


def _event_data(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data")
    return data if isinstance(data, dict) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _workspace_name(cwd: str, repository: str) -> str:
    if cwd:
        return Path(cwd).name or cwd
    if repository:
        return repository.rstrip("/").rsplit("/", 1)[-1]
    return "Agent Host"


def _capture_fingerprint(session: CopilotCliSession) -> tuple[int, int]:
    sources = [session.events_path]
    sources.extend(
        path
        for name in _SESSION_METADATA_FILES
        if (path := session.state_dir / name).is_file()
    )
    stats = [path.stat() for path in sources]
    return max(stat.st_mtime_ns for stat in stats), sum(stat.st_size for stat in stats)
