"""VS Code GitHub Copilot Chat importer.

VS Code persists local chat sessions as either a complete JSON document or an
append-only JSONL mutation log. GitHub Copilot Chat can additionally persist a
direct event transcript and editing/resource sidecars for agent sessions.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, TypedDict, Union
from urllib.parse import unquote, urlparse

from ..schema import Actor, EventType, Host, NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import truncate_summary
from .base import ImportResult

VSCODE_COPILOT_USER_DIRS_ENV = "VSCODE_COPILOT_USER_DIRS"

CoreFormat = Literal["json", "jsonl"]
ObjectPath = tuple[Union[str, int], ...]


class _TranscriptCommon(TypedDict):
    raw_id: str
    parent_id: str | None
    line_no: int
    timestamp: str | None

_FILE_READ_TOOLS = {
    "copilot_findFiles",
    "copilot_findTextInFiles",
    "copilot_listDirectory",
    "copilot_readFile",
    "file_search",
    "grep_search",
    "list_dir",
    "read_file",
}
_FILE_EDIT_TOOLS = {
    "apply_patch",
    "copilot_applyPatch",
    "copilot_createFile",
    "copilot_multiReplaceString",
    "copilot_replaceString",
    "create_file",
    "multi_replace_string_in_file",
    "replace_string_in_file",
}
_COMMAND_TOOLS = {
    "kill_terminal",
    "run_in_terminal",
    "terminal_last_command",
}
_SUBAGENT_TOOLS = {"runSubagent", "run_subagent"}


@dataclass
class CoreSessionState:
    data: dict[str, Any]
    source_lines: dict[ObjectPath, int]

    def line_for(self, path: ObjectPath) -> int:
        current = path
        while current:
            line = self.source_lines.get(current)
            if line is not None:
                return line
            current = current[:-1]
        return self.source_lines.get((), 1)


@dataclass
class CopilotSession:
    session_id: str
    session_path: Path
    core_format: CoreFormat
    profile_root: Path
    workspace_storage_dir: Path | None
    workspace_id: str
    workspace_uri: str | None
    workspace_name: str
    cwd: str
    title: str
    models: tuple[str, ...]
    request_count: int
    size_bytes: int
    mtime: float
    transcript_path: Path | None
    editing_session_dir: Path | None
    resources_dir: Path | None

    @property
    def display_title(self) -> str:
        return self.title[:120]

    @property
    def display_model(self) -> str:
        return ", ".join(self.models[:2]) if self.models else "-"


def _default_vscode_user_dirs() -> list[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        roots = [
            home / "Library" / "Application Support" / "Code" / "User",
            home / "Library" / "Application Support" / "Code - Insiders" / "User",
        ]
    elif sys.platform == "win32":
        app_data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        roots = [app_data / "Code" / "User", app_data / "Code - Insiders" / "User"]
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        roots = [config_home / "Code" / "User", config_home / "Code - Insiders" / "User"]

    roots.extend(
        [
            home / ".vscode-server" / "data" / "User",
            home / ".vscode-server-insiders" / "data" / "User",
        ]
    )
    return roots


def _resolve_vscode_user_dirs(explicit: tuple[Path, ...] | None = None) -> list[Path]:
    if explicit:
        return [path.expanduser() for path in explicit]
    env = os.environ.get(VSCODE_COPILOT_USER_DIRS_ENV)
    if env:
        roots = [Path(piece.strip()).expanduser() for piece in env.split(",") if piece.strip()]
        if roots:
            return roots
    return _default_vscode_user_dirs()


def _profile_roots(user_dir: Path) -> list[Path]:
    roots = [user_dir]
    profiles_dir = user_dir / "profiles"
    if profiles_dir.is_dir():
        roots.extend(sorted(path for path in profiles_dir.iterdir() if path.is_dir()))
    return roots


class VscodeCopilotImporter:
    host: Host = "vscode-copilot"

    def __init__(
        self,
        layout: Layout,
        user_data_dir: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ):
        self.layout = layout
        self.user_dirs = (
            [user_data_dir.expanduser()]
            if user_data_dir is not None
            else _resolve_vscode_user_dirs(roots)
        )

    def discover(self) -> list[CopilotSession]:
        by_id: dict[str, CopilotSession] = {}
        for user_dir in self.user_dirs:
            if not user_dir.is_dir():
                continue
            for profile_root in _profile_roots(user_dir):
                for path, workspace_dir in self._session_files(profile_root):
                    try:
                        session = self._describe_session(path, profile_root, workspace_dir)
                    except FileNotFoundError:
                        continue
                    if session is None:
                        continue
                    previous = by_id.get(session.session_id)
                    if previous is None or self._session_rank(session) > self._session_rank(previous):
                        by_id[session.session_id] = session
        return sorted(by_id.values(), key=lambda session: session.mtime, reverse=True)

    def _session_files(self, profile_root: Path) -> list[tuple[Path, Path | None]]:
        found: list[tuple[Path, Path | None]] = []
        workspace_root = profile_root / "workspaceStorage"
        if workspace_root.is_dir():
            for chat_dir in sorted(workspace_root.glob("*/chatSessions")):
                if not chat_dir.is_dir():
                    continue
                workspace_dir = chat_dir.parent
                for path in sorted(chat_dir.iterdir()):
                    if path.is_file() and path.suffix in {".json", ".jsonl"}:
                        found.append((path, workspace_dir))

        global_storage = profile_root / "globalStorage"
        for directory_name in (
            "emptyWindowChatSessions",
            "transferredChatSessions",
        ):
            chat_dir = global_storage / directory_name
            if not chat_dir.is_dir():
                continue
            for path in sorted(chat_dir.iterdir()):
                if path.is_file() and path.suffix in {".json", ".jsonl"}:
                    found.append((path, None))
        return found

    def _describe_session(
        self,
        path: Path,
        profile_root: Path,
        workspace_dir: Path | None,
    ) -> CopilotSession | None:
        core = _load_core_session(path)
        session_id = str(core.data.get("sessionId") or path.stem)
        transcript_path = _existing_path(
            workspace_dir
            / "GitHub.copilot-chat"
            / "transcripts"
            / f"{session_id}.jsonl"
            if workspace_dir
            else None
        )
        if not _is_copilot_session(core.data, transcript_path):
            return None

        requests = core.data.get("requests")
        request_list = requests if isinstance(requests, list) else []
        if not request_list and transcript_path is None:
            return None

        workspace_uri, workspace_name, cwd = _workspace_metadata(workspace_dir)
        workspace_id = workspace_dir.name if workspace_dir else "empty-window"
        title = _session_title(core.data, request_list)
        models = tuple(
            dict.fromkeys(
                str(request.get("modelId"))
                for request in request_list
                if isinstance(request, dict)
                and isinstance(request.get("modelId"), str)
                and request.get("modelId")
            )
        )
        editing_session_dir = _existing_path(
            workspace_dir / "chatEditingSessions" / session_id if workspace_dir else None,
            directory=True,
        )
        resources_dir = _existing_path(
            workspace_dir
            / "GitHub.copilot-chat"
            / "chat-session-resources"
            / session_id
            if workspace_dir
            else None,
            directory=True,
        )
        stat = path.stat()
        mtimes = [stat.st_mtime]
        if transcript_path is not None:
            mtimes.append(transcript_path.stat().st_mtime)

        return CopilotSession(
            session_id=session_id,
            session_path=path,
            core_format="jsonl" if path.suffix == ".jsonl" else "json",
            profile_root=profile_root,
            workspace_storage_dir=workspace_dir,
            workspace_id=workspace_id,
            workspace_uri=workspace_uri,
            workspace_name=workspace_name,
            cwd=cwd,
            title=title,
            models=models,
            request_count=len(request_list),
            size_bytes=stat.st_size,
            mtime=max(mtimes),
            transcript_path=transcript_path,
            editing_session_dir=editing_session_dir,
            resources_dir=resources_dir,
        )

    @staticmethod
    def _session_rank(session: CopilotSession) -> tuple[float, bool, bool, int]:
        return (
            session.mtime,
            session.transcript_path is not None,
            session.core_format == "jsonl",
            session.size_bytes,
        )

    def find_session(self, session_id: str) -> CopilotSession | None:
        return next(
            (session for session in self.discover() if session.session_id == session_id),
            None,
        )

    def latest(self) -> CopilotSession | None:
        sessions = self.discover()
        return sessions[0] if sessions else None

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        session = self.find_session(identifier)
        if session is None:
            roots = ", ".join(str(path) for path in self.user_dirs)
            raise FileNotFoundError(
                f"No VS Code Copilot chat session found with id {identifier!r} under {roots}"
            )

        raw_dir = self.layout.raw_dir(self.host, session.session_id)
        fingerprint = _capture_fingerprint(session)
        if raw_dir.exists() and not force and not _source_is_newer(raw_dir, fingerprint):
            raise FileExistsError(
                f"Raw capture already exists at {raw_dir} (pass force=True to overwrite)"
            )

        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_session = raw_dir / f"session.{session.core_format}"
        _copy_stable(session.session_path, raw_session)
        stale_source = raw_dir / (
            "session.json" if session.core_format == "jsonl" else "session.jsonl"
        )
        if stale_source.exists():
            stale_source.unlink()

        core = _load_core_session(raw_session)
        snapshot_path = raw_dir / "session.snapshot.json"
        snapshot_path.write_text(
            json.dumps(core.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        raw_transcript: Path | None = None
        if session.transcript_path is not None:
            raw_transcript = raw_dir / "transcript.jsonl"
            _copy_stable(session.transcript_path, raw_transcript)

        self._snapshot_sidecars(session, raw_dir)
        meta = {
            "host": self.host,
            "session_id": session.session_id,
            "title": session.title,
            "cwd": session.cwd,
            "workspace_id": session.workspace_id,
            "workspace_name": session.workspace_name,
            "workspace_uri": session.workspace_uri,
            "models": list(session.models),
            "request_count": session.request_count,
            "core_format": session.core_format,
            "source_session": str(session.session_path),
            "source_transcript": (
                str(session.transcript_path) if session.transcript_path else None
            ),
            "source_user_dir": str(session.profile_root),
            "size_bytes": session.size_bytes,
            "captured_mtime_ns": fingerprint[0],
            "captured_size_bytes": fingerprint[1],
            "sidecars": {
                "transcript": raw_transcript is not None,
                "editing_session": session.editing_session_dir is not None,
                "resources": session.resources_dir is not None,
            },
        }
        (raw_dir / "import_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if raw_transcript is not None:
            events, unknown, gaps = self._normalize_transcript(
                raw_transcript,
                session.session_id,
                core,
                raw_session,
            )
        else:
            events, unknown, gaps = self._normalize_core(
                core,
                raw_session,
                session.session_id,
            )

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

    def _snapshot_sidecars(self, session: CopilotSession, raw_dir: Path) -> None:
        sidecars = raw_dir / "sidecars"
        if session.workspace_storage_dir is not None:
            workspace_meta = session.workspace_storage_dir / "workspace.json"
            if workspace_meta.is_file():
                _copy_stable(workspace_meta, sidecars / "workspace.json")
        if session.editing_session_dir is not None:
            _copy_stable_tree(
                session.editing_session_dir,
                sidecars / "chatEditingSession",
            )
        if session.resources_dir is not None:
            _copy_stable_tree(
                session.resources_dir,
                sidecars / "chat-session-resources",
            )

        session_store = (
            session.profile_root
            / "globalStorage"
            / "github.copilot-chat"
            / "session-store.db"
        )
        if session_store.is_file():
            exported = _export_session_store(session_store, session.session_id)
            if exported is not None:
                sidecars.mkdir(parents=True, exist_ok=True)
                (sidecars / "session-store.json").write_text(
                    json.dumps(exported, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

    def _normalize_transcript(
        self,
        transcript_path: Path,
        session_id: str,
        core: CoreSessionState,
        core_path: Path,
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        records = _read_strict_jsonl(transcript_path)
        executed_tool_ids: set[str] = set()
        for _, raw in records:
            if raw.get("type") != "tool.execution_start":
                continue
            candidate_data = raw.get("data")
            if isinstance(candidate_data, dict) and candidate_data.get("toolCallId"):
                executed_tool_ids.add(str(candidate_data["toolCallId"]))
        core_tools = _core_tool_parts(core, core_path)
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()
        tool_types: dict[str, EventType] = {}
        tool_names: dict[str, str] = {}

        def emit(
            *,
            raw_id: str,
            suffix: str,
            parent_id: str | None,
            line_no: int,
            timestamp: str | None,
            actor: Actor,
            event_type: EventType,
            summary: str,
            payload: dict[str, Any],
        ) -> str:
            event_id = _transcript_event_id(session_id, raw_id, suffix)
            events.append(
                NormalizedEvent(
                    event_id=event_id,
                    session_id=session_id,
                    host=self.host,
                    sequence=len(events) + 1,
                    actor=actor,
                    event_type=event_type,
                    summary=summary,
                    raw_ref=RawRef(path=str(transcript_path), line=line_no),
                    timestamp=timestamp,
                    parent_event_id=(
                        _transcript_event_id(session_id, parent_id)
                        if parent_id
                        else None
                    ),
                    payload=payload,
                )
            )
            return event_id

        for line_no, raw in records:
            raw_type = raw.get("type")
            raw_data = raw.get("data")
            event_data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            raw_id = str(raw.get("id") or f"line-{line_no}")
            parent_id = str(raw["parentId"]) if raw.get("parentId") else None
            timestamp = _iso_timestamp(raw.get("timestamp"))
            common: _TranscriptCommon = {
                "raw_id": raw_id,
                "parent_id": parent_id,
                "line_no": line_no,
                "timestamp": timestamp,
            }

            if raw_type == "session.start":
                emit(
                    **common,
                    suffix="",
                    actor="system",
                    event_type="session_start",
                    summary="VS Code Copilot session start",
                    payload=event_data,
                )
            elif raw_type == "user.message":
                user_content = str(event_data.get("content") or "")
                emit(
                    **common,
                    suffix="",
                    actor="user",
                    event_type="message",
                    summary=truncate_summary(user_content),
                    payload={
                        "text": user_content,
                        "attachments": event_data.get("attachments") or [],
                        "message_id": event_data.get("messageId"),
                    },
                )
            elif raw_type in {"assistant.turn_start", "assistant.turn_end"}:
                emit(
                    **common,
                    suffix="",
                    actor="assistant",
                    event_type="attachment",
                    summary=str(raw_type),
                    payload=event_data,
                )
            elif raw_type == "assistant.message":
                reasoning = event_data.get("reasoningText")
                assistant_content = event_data.get("content")
                assistant_text = assistant_content if isinstance(assistant_content, str) else ""
                has_content = bool(assistant_text)
                emitted = False
                if isinstance(reasoning, str) and reasoning:
                    emit(
                        **common,
                        suffix="#reasoning" if has_content else "",
                        actor="assistant",
                        event_type="reasoning",
                        summary=truncate_summary(reasoning),
                        payload={
                            "thinking": reasoning,
                            "message_id": event_data.get("messageId"),
                        },
                    )
                    emitted = True
                if has_content:
                    emit(
                        **common,
                        suffix="",
                        actor="assistant",
                        event_type="message",
                        summary=truncate_summary(assistant_text),
                        payload={
                            "text": assistant_text,
                            "message_id": event_data.get("messageId"),
                        },
                    )
                    emitted = True
                for index, request in enumerate(event_data.get("toolRequests") or []):
                    if not isinstance(request, dict):
                        continue
                    call_id = str(request.get("toolCallId") or "")
                    if call_id and call_id in executed_tool_ids:
                        continue
                    name = str(request.get("name") or "unknown")
                    arguments = request.get("arguments")
                    event_type = _tool_event_type(name)
                    if call_id:
                        tool_types[call_id] = event_type
                        tool_names[call_id] = name
                    emit(
                        **common,
                        suffix=f"#tool-request-{index}",
                        actor="assistant",
                        event_type=event_type,
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
                    )
                    emitted = True
                if not emitted:
                    emit(
                        **common,
                        suffix="",
                        actor="assistant",
                        event_type="attachment",
                        summary="assistant.message",
                        payload=event_data,
                    )
            elif raw_type == "tool.execution_start":
                call_id = str(event_data.get("toolCallId") or raw_id)
                name = str(event_data.get("toolName") or "unknown")
                arguments = event_data.get("arguments")
                event_type = _tool_event_type(name)
                tool_types[call_id] = event_type
                tool_names[call_id] = name
                start_payload: dict[str, Any] = {
                    "name": name,
                    "tool_name": name,
                    "tool_id": call_id,
                    "call_id": call_id,
                    "input": arguments,
                    "arguments": arguments,
                }
                _add_core_enrichment(start_payload, core_tools.get(call_id))
                emit(
                    **common,
                    suffix="",
                    actor="assistant",
                    event_type=event_type,
                    summary=_tool_summary(name, arguments),
                    payload=start_payload,
                )
            elif raw_type == "tool.execution_complete":
                call_id = str(event_data.get("toolCallId") or raw_id)
                core_tool = core_tools.get(call_id)
                name = tool_names.get(call_id) or _core_tool_name(core_tool) or "unknown"
                call_type = tool_types.get(call_id) or _tool_event_type(name)
                success = event_data.get("success")
                result_type = _completion_event_type(call_type)
                completion_payload: dict[str, Any] = {
                    "name": name,
                    "tool_name": name,
                    "tool_id": call_id,
                    "call_id": call_id,
                    "success": success,
                    "is_error": success is False,
                }
                _add_core_enrichment(completion_payload, core_tool)
                emit(
                    **common,
                    suffix="",
                    actor="subagent" if result_type == "subagent_end" else "tool",
                    event_type=result_type,
                    summary=f"{name}: {'completed' if success is not False else 'failed'}",
                    payload=completion_payload,
                )
            else:
                unknown += 1
                gaps.add(str(raw_type or "<missing>"))
                emit(
                    **common,
                    suffix="",
                    actor="system",
                    event_type="unknown",
                    summary=f"unknown transcript type={raw_type}",
                    payload=raw,
                )

        return events, unknown, sorted(gaps)

    def _normalize_core(
        self,
        core: CoreSessionState,
        source_path: Path,
        session_id: str,
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()

        def emit(
            *,
            event_id: str,
            actor: Actor,
            event_type: EventType,
            summary: str,
            payload: dict[str, Any],
            timestamp: str | None,
            parent_event_id: str | None,
            line_no: int,
        ) -> str:
            events.append(
                NormalizedEvent(
                    event_id=event_id,
                    session_id=session_id,
                    host=self.host,
                    sequence=len(events) + 1,
                    actor=actor,
                    event_type=event_type,
                    summary=summary,
                    raw_ref=RawRef(path=str(source_path), line=line_no),
                    timestamp=timestamp,
                    parent_event_id=parent_event_id,
                    payload=payload,
                )
            )
            return event_id

        session_timestamp = _iso_timestamp(core.data.get("creationDate"))
        session_payload = {
            key: value
            for key, value in core.data.items()
            if key not in {"requests", "pendingRequests"}
        }
        emit(
            event_id=f"{session_id}:core:session-start",
            actor="system",
            event_type="session_start",
            summary="VS Code Copilot chat snapshot",
            payload=session_payload,
            timestamp=session_timestamp,
            parent_event_id=None,
            line_no=core.line_for(()),
        )

        requests = core.data.get("requests")
        request_list = requests if isinstance(requests, list) else []
        for request_index, request in enumerate(request_list):
            if not isinstance(request, dict):
                unknown += 1
                gaps.add("request/non-object")
                emit(
                    event_id=f"{session_id}:core:request-{request_index}",
                    actor="system",
                    event_type="unknown",
                    summary=f"request[{request_index}] is not an object",
                    payload={"request": request},
                    timestamp=session_timestamp,
                    parent_event_id=None,
                    line_no=core.line_for(("requests", request_index)),
                )
                continue

            request_id = str(request.get("requestId") or f"request-{request_index}")
            timestamp = _iso_timestamp(request.get("timestamp")) or session_timestamp
            user_event_id = f"{session_id}:core:{request_id}:user"
            text = _request_text(request)
            request_meta = {
                key: value
                for key, value in request.items()
                if key not in {"message", "response"}
            }
            emit(
                event_id=user_event_id,
                actor="user",
                event_type="message",
                summary=truncate_summary(text),
                payload={
                    "text": text,
                    "message": request.get("message"),
                    "request": request_meta,
                },
                timestamp=timestamp,
                parent_event_id=None,
                line_no=core.line_for(("requests", request_index, "message")),
            )

            response = request.get("response")
            parts = response if isinstance(response, list) else []
            for part_index, part in enumerate(parts):
                part_path = ("requests", request_index, "response", part_index)
                line_no = core.line_for(part_path)
                base_id = f"{session_id}:core:{request_id}:response-{part_index}"
                if not isinstance(part, dict):
                    unknown += 1
                    gaps.add("response/non-object")
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="unknown",
                        summary=f"response[{part_index}] is not an object",
                        payload={"part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                    continue

                kind = part.get("kind")
                if kind is None and isinstance(part.get("value"), str):
                    text_part = str(part["value"])
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="message",
                        summary=truncate_summary(text_part),
                        payload={"text": text_part, "part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind == "thinking":
                    thinking = str(part.get("value") or "")
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="reasoning",
                        summary=truncate_summary(thinking),
                        payload={"thinking": thinking, "part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind == "toolInvocationSerialized":
                    name = str(part.get("toolId") or part.get("toolName") or "unknown")
                    call_id = str(part.get("toolCallId") or base_id)
                    call_type = _tool_event_type(name)
                    tool_input = part.get("toolSpecificData")
                    call_event_id = emit(
                        event_id=f"{base_id}:call",
                        actor="subagent" if call_type == "subagent_start" else "assistant",
                        event_type=call_type,
                        summary=_tool_summary(name, tool_input),
                        payload={
                            "name": name,
                            "tool_name": name,
                            "tool_id": call_id,
                            "call_id": call_id,
                            "input": tool_input,
                            "invocation": part,
                        },
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                    if part.get("isComplete") is True:
                        is_error = _core_tool_failed(part)
                        result_type = _completion_event_type(call_type)
                        emit(
                            event_id=f"{base_id}:result",
                            actor="subagent" if result_type == "subagent_end" else "tool",
                            event_type=result_type,
                            summary=f"{name}: {'failed' if is_error else 'completed'}",
                            payload={
                                "name": name,
                                "tool_name": name,
                                "tool_id": call_id,
                                "call_id": call_id,
                                "success": not is_error,
                                "is_error": is_error,
                                "result": part.get("resultDetails"),
                                "invocation": part,
                            },
                            timestamp=timestamp,
                            parent_event_id=call_event_id,
                            line_no=line_no,
                        )
                elif kind == "textEditGroup":
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="file_edit",
                        summary=f"edited {_uri_label(part.get('uri'))}",
                        payload={"part": part, "uri": part.get("uri"), "edits": part.get("edits")},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind in {"codeblockUri", "inlineReference"}:
                    emit(
                        event_id=base_id,
                        actor="system",
                        event_type="attachment",
                        summary=str(kind),
                        payload={"part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind == "confirmation":
                    emit(
                        event_id=base_id,
                        actor="system",
                        event_type="permission",
                        summary=truncate_summary(str(part.get("title") or "confirmation")),
                        payload={"part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind == "prepareToolInvocation":
                    name = str(part.get("toolName") or "unknown")
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type=_tool_event_type(name),
                        summary=f"prepare {name}",
                        payload={"name": name, "tool_name": name, "part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind in {
                    "disabledClaudeHooks",
                    "mcpServersStarting",
                    "questionCarousel",
                    "undoStop",
                }:
                    emit(
                        event_id=base_id,
                        actor="system",
                        event_type="attachment",
                        summary=str(kind),
                        payload={"part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                elif kind in {"progressMessage", "progressTaskSerialized"}:
                    text_part = str(part.get("content") or kind)
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="message",
                        summary=truncate_summary(text_part),
                        payload={"text": text_part, "part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )
                else:
                    unknown += 1
                    gaps.add(f"response/{kind if kind is not None else '<missing>'}")
                    emit(
                        event_id=base_id,
                        actor="assistant",
                        event_type="unknown",
                        summary=f"unknown response kind={kind}",
                        payload={"part": part},
                        timestamp=timestamp,
                        parent_event_id=user_event_id,
                        line_no=line_no,
                    )

        return events, unknown, sorted(gaps)


def _load_core_session(path: Path) -> CoreSessionState:
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid VS Code chat JSON at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"VS Code chat session at {path} is not a JSON object")
        source_lines: dict[ObjectPath, int] = {}
        _mark_source_lines(source_lines, (), data, 1)
        return CoreSessionState(data=data, source_lines=source_lines)

    if path.suffix != ".jsonl":
        raise ValueError(f"Unsupported VS Code chat session format: {path}")

    state: Any = None
    source_lines = {}
    line_count = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            line_count += 1
            try:
                entry = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid VS Code chat mutation at {path}:{line_no}: {exc}"
                ) from exc
            if not isinstance(entry, dict):
                raise ValueError(f"VS Code chat mutation at {path}:{line_no} is not an object")
            kind = entry.get("kind")
            if kind == 0:
                state = entry.get("v")
                source_lines.clear()
                _mark_source_lines(source_lines, (), state, line_no)
                continue
            if state is None:
                raise ValueError(f"VS Code chat log at {path} is missing its initial entry")
            key_path = entry.get("k")
            if not isinstance(key_path, list) or not key_path:
                raise ValueError(
                    f"VS Code chat mutation at {path}:{line_no} has an invalid object path"
                )
            object_path = tuple(key_path)
            if kind == 1:
                _set_path(state, key_path, entry.get("v"))
                _clear_source_lines(source_lines, object_path)
                _mark_source_lines(source_lines, object_path, entry.get("v"), line_no)
            elif kind == 2:
                values = entry.get("v") or []
                if not isinstance(values, list):
                    raise ValueError(
                        f"VS Code chat array mutation at {path}:{line_no} has non-array values"
                    )
                array = _array_at_path(state, key_path)
                start_index = entry.get("i")
                if start_index is not None:
                    if not isinstance(start_index, int) or not 0 <= start_index <= len(array):
                        raise ValueError(
                            f"VS Code chat array mutation at {path}:{line_no} has invalid index"
                        )
                    del array[start_index:]
                    _clear_array_source_lines(source_lines, object_path, start_index)
                else:
                    start_index = len(array)
                for offset, value in enumerate(values):
                    array.append(value)
                    _mark_source_lines(
                        source_lines,
                        object_path + (start_index + offset,),
                        value,
                        line_no,
                    )
            elif kind == 3:
                _delete_path(state, key_path)
                _clear_source_lines(source_lines, object_path)
            else:
                raise ValueError(
                    f"VS Code chat mutation at {path}:{line_no} has unknown kind {kind!r}"
                )

    if line_count == 0 or not isinstance(state, dict):
        raise ValueError(f"VS Code chat log at {path} is empty or has no object state")
    return CoreSessionState(data=state, source_lines=source_lines)


def _set_path(state: Any, path: list[str | int], value: Any) -> None:
    current = state
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


def _delete_path(state: Any, path: list[str | int]) -> None:
    current = state
    for key in path[:-1]:
        current = current[key]
    key = path[-1]
    if isinstance(current, dict):
        current.pop(key, None)
    else:
        current[key] = None


def _array_at_path(state: Any, path: list[str | int]) -> list[Any]:
    current = state
    for key in path[:-1]:
        current = current[key]
    key = path[-1]
    if isinstance(current, dict):
        array = current.get(key, [])
    else:
        array = current[key]
    if array is None:
        array = []
    if not isinstance(array, list):
        raise ValueError(f"VS Code chat mutation path {path!r} does not reference an array")
    current[key] = array
    return array


def _mark_source_lines(
    source_lines: dict[ObjectPath, int],
    path: ObjectPath,
    value: Any,
    line_no: int,
) -> None:
    source_lines[path] = line_no
    if isinstance(value, dict):
        for key, child in value.items():
            _mark_source_lines(source_lines, path + (key,), child, line_no)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _mark_source_lines(source_lines, path + (index,), child, line_no)


def _clear_source_lines(source_lines: dict[ObjectPath, int], prefix: ObjectPath) -> None:
    for key in [key for key in source_lines if key[: len(prefix)] == prefix]:
        del source_lines[key]


def _clear_array_source_lines(
    source_lines: dict[ObjectPath, int],
    prefix: ObjectPath,
    start_index: int,
) -> None:
    to_remove: list[ObjectPath] = []
    for key in source_lines:
        if len(key) <= len(prefix) or key[: len(prefix)] != prefix:
            continue
        segment = key[len(prefix)]
        if isinstance(segment, int) and segment >= start_index:
            to_remove.append(key)
    for key in to_remove:
        del source_lines[key]


def _read_strict_jsonl(path: Path) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
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
            records.append((line_no, value))
    return records


def _workspace_metadata(workspace_dir: Path | None) -> tuple[str | None, str, str]:
    if workspace_dir is None:
        return None, "Empty Window", ""
    workspace_meta = workspace_dir / "workspace.json"
    if not workspace_meta.is_file():
        return None, workspace_dir.name, ""
    try:
        data = json.loads(workspace_meta.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid VS Code workspace metadata at {workspace_meta}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"VS Code workspace metadata at {workspace_meta} is not an object")
    raw_uri = data.get("folder") or data.get("workspace")
    if not isinstance(raw_uri, str) or not raw_uri:
        return None, workspace_dir.name, ""
    parsed = urlparse(raw_uri)
    if parsed.scheme == "file":
        local_path = Path(unquote(parsed.path))
        is_workspace_file = local_path.suffix == ".code-workspace"
        cwd_path = local_path.parent if is_workspace_file else local_path
        name = local_path.stem if is_workspace_file else local_path.name
        return raw_uri, name or workspace_dir.name, str(cwd_path)
    return raw_uri, Path(parsed.path).name or workspace_dir.name, raw_uri


def _is_copilot_session(data: dict[str, Any], transcript_path: Path | None) -> bool:
    if transcript_path is not None:
        return True
    responder = data.get("responderUsername")
    if isinstance(responder, str) and "copilot" in responder.lower():
        return True
    requests = data.get("requests")
    if not isinstance(requests, list):
        return False
    for request in requests:
        if not isinstance(request, dict):
            continue
        model_id = request.get("modelId")
        if isinstance(model_id, str) and model_id.startswith("copilot/"):
            return True
        agent = request.get("agent")
        if isinstance(agent, dict):
            agent = agent.get("id") or agent.get("name")
        if isinstance(agent, str) and agent.lower().startswith("github.copilot"):
            return True
    return False


def _session_title(data: dict[str, Any], requests: list[Any]) -> str:
    custom_title = data.get("customTitle")
    if isinstance(custom_title, str) and custom_title.strip():
        return truncate_summary(custom_title, 180)
    for request in requests:
        if not isinstance(request, dict):
            continue
        text = _request_text(request)
        if text:
            return truncate_summary(text, 180)
    return str(data.get("sessionId") or "untitled")


def _request_text(request: dict[str, Any]) -> str:
    message = request.get("message")
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        text = message.get("text")
        if isinstance(text, str):
            return text
    return ""


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000 if abs(float(value)) >= 100_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _tool_event_type(name: str) -> EventType:
    if name in _FILE_READ_TOOLS:
        return "file_read"
    if name in _FILE_EDIT_TOOLS:
        return "file_edit"
    if name in _COMMAND_TOOLS:
        return "command"
    if name in _SUBAGENT_TOOLS:
        return "subagent_start"
    return "tool_call"


def _completion_event_type(call_type: EventType) -> EventType:
    if call_type == "subagent_start":
        return "subagent_end"
    if call_type == "tool_call":
        return "tool_result"
    return call_type


def _tool_summary(name: str, arguments: Any) -> str:
    if isinstance(arguments, dict):
        for key in (
            "filePath",
            "file_path",
            "path",
            "command",
            "query",
            "url",
            "description",
        ):
            value = arguments.get(key)
            if isinstance(value, (str, int, float)):
                return f"{name}({key}={truncate_summary(str(value), 100)})"
    return name


def _transcript_event_id(session_id: str, raw_id: str, suffix: str = "") -> str:
    return f"{session_id}:transcript:{raw_id}{suffix}"


def _core_tool_parts(
    core: CoreSessionState,
    core_path: Path,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    requests = core.data.get("requests")
    if not isinstance(requests, list):
        return out
    for request_index, request in enumerate(requests):
        if not isinstance(request, dict):
            continue
        response = request.get("response")
        if not isinstance(response, list):
            continue
        for part_index, part in enumerate(response):
            if not isinstance(part, dict) or part.get("kind") != "toolInvocationSerialized":
                continue
            call_id = part.get("toolCallId")
            if not call_id:
                continue
            path = ("requests", request_index, "response", part_index)
            out[str(call_id)] = {
                "part": part,
                "raw_ref": {
                    "path": str(core_path),
                    "line": core.line_for(path),
                },
            }
    return out


def _core_tool_name(core_tool: dict[str, Any] | None) -> str | None:
    if not core_tool:
        return None
    part = core_tool.get("part")
    if not isinstance(part, dict):
        return None
    value = part.get("toolId") or part.get("toolName")
    return str(value) if value else None


def _add_core_enrichment(
    payload: dict[str, Any],
    core_tool: dict[str, Any] | None,
) -> None:
    if not core_tool:
        return
    part = core_tool.get("part")
    if isinstance(part, dict):
        payload["vscode_chat_invocation"] = part
        if "resultDetails" in part:
            payload["result"] = part.get("resultDetails")
    payload["vscode_chat_raw_ref"] = core_tool.get("raw_ref")


def _core_tool_failed(part: dict[str, Any]) -> bool:
    result = part.get("resultDetails")
    if isinstance(result, dict):
        if result.get("isError") is True or result.get("is_error") is True:
            return True
        if result.get("success") is False:
            return True
        status = result.get("status")
        if isinstance(status, str) and status.lower() in {"error", "failed"}:
            return True
    return False


def _uri_label(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("path", "fsPath"):
            path = value.get(key)
            if isinstance(path, str):
                return path
    return "file"


def _existing_path(path: Path | None, *, directory: bool = False) -> Path | None:
    if path is None:
        return None
    exists = path.is_dir() if directory else path.is_file()
    return path if exists else None


def _capture_fingerprint(session: CopilotSession) -> tuple[int, int]:
    sources = [session.session_path]
    if session.transcript_path is not None:
        sources.append(session.transcript_path)
    for directory in (session.editing_session_dir, session.resources_dir):
        if directory is not None:
            sources.extend(path for path in directory.rglob("*") if path.is_file())
    stats = [path.stat() for path in sources]
    return max(stat.st_mtime_ns for stat in stats), sum(stat.st_size for stat in stats)


def _source_is_newer(raw_dir: Path, fingerprint: tuple[int, int]) -> bool:
    meta_path = raw_dir / "import_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid existing import metadata at {meta_path}: {exc}") from exc
        previous_mtime = meta.get("captured_mtime_ns")
        previous_size = meta.get("captured_size_bytes")
        if isinstance(previous_mtime, int) and isinstance(previous_size, int):
            return fingerprint[0] > previous_mtime or fingerprint[1] > previous_size

    captured_sources = [
        path for path in (raw_dir / "session.json", raw_dir / "session.jsonl") if path.is_file()
    ]
    if not captured_sources:
        return False
    latest = max(path.stat().st_mtime_ns for path in captured_sources)
    largest = max(path.stat().st_size for path in captured_sources)
    return fingerprint[0] > latest or fingerprint[1] > largest


def _copy_stable(source: Path, destination: Path, attempts: int = 3) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        for _ in range(attempts):
            before = source.stat()
            shutil.copy2(source, temporary)
            after = source.stat()
            if (
                before.st_mtime_ns == after.st_mtime_ns
                and before.st_size == after.st_size
                and temporary.stat().st_size == after.st_size
            ):
                os.replace(temporary, destination)
                return
        raise RuntimeError(f"Source kept changing while capturing {source}")
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_stable_tree(source: Path, destination: Path, attempts: int = 3) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp")
    previous = destination.with_name(f".{destination.name}.previous")
    for path in (temporary, previous):
        if path.exists():
            shutil.rmtree(path)

    try:
        for _ in range(attempts):
            if temporary.exists():
                shutil.rmtree(temporary)
            directories_before, files_before = _tree_manifest(source)
            temporary.mkdir(parents=True)
            for relative in directories_before:
                (temporary / relative).mkdir(parents=True, exist_ok=True)
            for relative in files_before:
                _copy_stable(source / relative, temporary / relative)
            directories_after, files_after = _tree_manifest(source)
            if directories_before != directories_after or files_before != files_after:
                continue

            if destination.exists():
                os.replace(destination, previous)
            try:
                os.replace(temporary, destination)
            except OSError:
                if previous.exists() and not destination.exists():
                    os.replace(previous, destination)
                raise
            if previous.exists():
                shutil.rmtree(previous)
            return
        raise RuntimeError(f"Source tree kept changing while capturing {source}")
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if previous.exists():
            if destination.exists():
                shutil.rmtree(previous)
            else:
                os.replace(previous, destination)


def _tree_manifest(source: Path) -> tuple[tuple[Path, ...], dict[Path, tuple[int, int]]]:
    directories: list[Path] = []
    files: dict[Path, tuple[int, int]] = {}
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if path.is_dir():
            directories.append(relative)
        elif path.is_file():
            stat = path.stat()
            files[relative] = (stat.st_mtime_ns, stat.st_size)
    return tuple(sorted(directories)), files


def _export_session_store(db_path: Path, session_id: str) -> dict[str, Any] | None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        available = {
            str(row["name"])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        tables = {
            "sessions": "id",
            "turns": "session_id",
            "checkpoints": "session_id",
            "session_files": "session_id",
            "session_refs": "session_id",
        }
        exported: dict[str, Any] = {}
        for table, key in tables.items():
            if table not in available:
                continue
            rows = con.execute(
                f"SELECT * FROM {table} WHERE {key} = ?",
                (session_id,),
            ).fetchall()
            if rows:
                exported[table] = [dict(row) for row in rows]
        return exported or None
    finally:
        con.close()
