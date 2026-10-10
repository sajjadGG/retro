"""Claude Code importer.

Claude Code writes per-session JSONL transcripts at:
    ~/.claude/projects/<project-slug>/<session-id>.jsonl
    ~/.config/claude/projects/<project-slug>/<session-id>.jsonl

Both default locations are scanned. `CLAUDE_CONFIG_DIR` (comma-separated)
overrides the defaults — matches ccusage behavior. The transcript is the
source of truth; sidecars (file-history, todos, tasks) are snapshotted.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..schema import Actor, EventType, Host, NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import artifact_ref, iter_jsonl, truncate_summary
from .base import (
    ImportResult,
    has_only_repo_state_capture,
)
from .normalization import normalized_payload, normalized_result_fields, resolve_timestamp

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Default roots scanned when CLAUDE_CONFIG_DIR is not set.
DEFAULT_CLAUDE_ROOTS: tuple[Path, ...] = (
    Path.home() / ".claude",
    Path.home() / ".config" / "claude",
)

# Tool name -> normalized event_type override for tool_call events.
# tool_result events inherit the matching tool_call's override via id-pairing.
_TOOL_TYPE_OVERRIDES: dict[str, EventType] = {
    "Read": "file_read",
    "Edit": "file_edit",
    "Write": "file_edit",
    "MultiEdit": "file_edit",
    "NotebookEdit": "file_edit",
    "Bash": "command",
}
NORMALIZER_VERSION = 3


@dataclass
class ClaudeSession:
    session_id: str
    transcript_path: Path
    project_slug: str
    cwd: str
    size_bytes: int
    mtime: float
    claude_home: Path


def _read_transcript_cwd(path: Path) -> str:
    for _, event in iter_jsonl(path):
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd.strip():
            return cwd
    return ""


def _resolve_claude_roots(explicit: tuple[Path, ...] | None = None) -> list[Path]:
    """Resolve the list of Claude data roots to scan.

    `CLAUDE_CONFIG_DIR` wins when set (comma-separated supported). Otherwise we
    use the union of `~/.claude` and `~/.config/claude` so neither location is
    silently missed.
    """
    if explicit:
        return list(explicit)
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        roots: list[Path] = []
        for raw in env.split(","):
            piece = raw.strip()
            if not piece:
                continue
            roots.append(Path(piece).expanduser())
        if roots:
            return roots
    return list(DEFAULT_CLAUDE_ROOTS)


class ClaudeImporter:
    host: Host = "claude-code"

    def __init__(
        self,
        layout: Layout,
        claude_home: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ):
        self.layout = layout
        # `claude_home` retained for sidecar lookups + tests. When passed, it
        # forces a single-root scan rooted there.
        if claude_home is not None:
            self.roots: list[Path] = [claude_home]
        else:
            self.roots = _resolve_claude_roots(roots)
        # Primary root is used for sidecar resolution (todos/tasks) — the
        # latest-modified root with a projects/ dir wins.
        self.claude_home = self._pick_sidecar_root()
        self.projects_dir = self.claude_home / "projects"
        self._discover_cache: list[ClaudeSession] | None = None

    def _pick_sidecar_root(self) -> Path:
        for root in self.roots:
            if (root / "projects").exists():
                return root
        return self.roots[0] if self.roots else CLAUDE_HOME

    # ---- discovery -----------------------------------------------------------

    def discover(self) -> list[ClaudeSession]:
        if self._discover_cache is not None:
            return list(self._discover_cache)
        out: list[ClaudeSession] = []
        seen_ids: set[str] = set()
        for root in self.roots:
            projects_dir = root / "projects"
            if not projects_dir.exists():
                continue
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl in proj_dir.glob("*.jsonl"):
                    if jsonl.stem in seen_ids:
                        # Same session id found in another root; prefer the
                        # first (higher-priority) root.
                        continue
                    try:
                        st = jsonl.stat()
                    except FileNotFoundError:
                        continue
                    seen_ids.add(jsonl.stem)
                    out.append(
                        ClaudeSession(
                            session_id=jsonl.stem,
                            transcript_path=jsonl,
                            project_slug=proj_dir.name,
                            cwd=_read_transcript_cwd(jsonl),
                            size_bytes=st.st_size,
                            mtime=st.st_mtime,
                            claude_home=root,
                        )
                    )
        out.sort(key=lambda s: s.mtime, reverse=True)
        self._discover_cache = out
        return list(self._discover_cache)

    def find_session(self, session_id: str) -> ClaudeSession | None:
        for s in self.discover():
            if s.session_id == session_id:
                return s
        return None

    def latest(self) -> ClaudeSession | None:
        sessions = self.discover()
        return sessions[0] if sessions else None

    # ---- import --------------------------------------------------------------

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        session = self.find_session(identifier)
        if session is None:
            raise FileNotFoundError(
                f"No Claude Code transcript found for session id {identifier!r} "
                f"under {self.projects_dir}"
            )
        current_stat = session.transcript_path.stat()
        session.size_bytes = current_stat.st_size
        session.mtime = current_stat.st_mtime
        raw_dir = self.layout.raw_dir(self.host, session.session_id)
        raw_transcript = raw_dir / "transcript.jsonl"
        if raw_dir.exists() and not force:
            if raw_transcript.exists():
                should_raise = False
                try:
                    src_stat = session.transcript_path.stat()
                    raw_stat = raw_transcript.stat()
                    if src_stat.st_mtime <= raw_stat.st_mtime and src_stat.st_size <= raw_stat.st_size:
                        should_raise = True
                except OSError:
                    pass
                if should_raise:
                    raise FileExistsError(
                        f"Raw capture already exists at {raw_dir} (pass force=True to overwrite)"
                    )
            elif not has_only_repo_state_capture(raw_dir):
                raise FileExistsError(
                    f"Raw capture already exists at {raw_dir} (pass force=True to overwrite)"
                )
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_transcript = raw_dir / "transcript.jsonl"
        shutil.copy2(session.transcript_path, raw_transcript)

        meta = {
            "host": self.host,
            "session_id": session.session_id,
            "project_slug": session.project_slug,
            "cwd": session.cwd,
            "source_transcript": str(session.transcript_path),
            "claude_home": str(session.claude_home),
            "size_bytes": session.size_bytes,
        }
        (raw_dir / "import_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        self._snapshot_sidecars(session, raw_dir)

        events, unknown, gaps = self.normalize_raw_dir(raw_dir, session.session_id)
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

    def normalize_raw_dir(
        self,
        raw_dir: Path,
        session_id: str,
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        transcript_path = raw_dir / "transcript.jsonl"
        if not transcript_path.is_file():
            raise FileNotFoundError(
                f"Archived Claude Code transcript is missing: {transcript_path}"
            )
        return self._normalize(transcript_path, session_id)

    # ---- sidecar snapshot ----------------------------------------------------

    def _snapshot_sidecars(self, session: ClaudeSession, raw_dir: Path) -> None:
        sidecars = raw_dir / "sidecars"
        # Resolve sidecars relative to the session's own claude_home so
        # multi-root setups don't cross-pollinate.
        for sub in ("todos", "tasks"):
            src_dir = session.claude_home / sub
            if not src_dir.exists():
                continue
            matches = list(src_dir.glob(f"{session.session_id}*"))
            if not matches:
                continue
            dest = sidecars / sub
            dest.mkdir(parents=True, exist_ok=True)
            for m in matches:
                target = dest / m.name
                if m.is_dir():
                    shutil.copytree(m, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(m, target)

    # ---- normalization -------------------------------------------------------

    def _normalize(
        self, transcript_path: Path, session_id: str
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()
        tool_use_names: dict[str, str] = {}
        tool_event_ids: dict[str, str] = {}
        seq = 0
        session_started = False
        last_timestamp: str | None = None
        last_raw_ref: RawRef | None = None
        last_cwd = _read_transcript_cwd(transcript_path)

        for line_no, raw_event in iter_jsonl(transcript_path):
            seq += 1
            etype = raw_event.get("type")
            raw_timestamp = raw_event.get("timestamp")
            explicit_timestamp = raw_timestamp if isinstance(raw_timestamp, str) else None
            timestamp, last_timestamp, timestamp_inferred = resolve_timestamp(
                explicit_timestamp,
                last_timestamp,
            )
            cwd = raw_event.get("cwd")
            if isinstance(cwd, str) and cwd:
                last_cwd = cwd
            uuid = raw_event.get("uuid") or f"{session_id}:{line_no}"
            parent = raw_event.get("parentUuid")
            raw_ref = RawRef(
                path=artifact_ref(transcript_path, self.layout.root),
                line=line_no,
            )
            last_raw_ref = raw_ref

            if not session_started:
                events.append(
                    NormalizedEvent(
                        event_id=f"{session_id}:session-start",
                        session_id=session_id,
                        host="claude-code",
                        sequence=0,
                        timestamp=timestamp,
                        parent_event_id=None,
                        raw_ref=raw_ref,
                        actor="system",
                        event_type="session_start",
                        summary=f"claude-code session start cwd={last_cwd or None}",
                        payload=normalized_payload(
                            {"cwd": last_cwd} if last_cwd else {},
                            timestamp_inferred=timestamp_inferred,
                        ),
                    )
                )
                session_started = True

            common: dict[str, Any] = dict(
                session_id=session_id,
                host="claude-code",
                sequence=seq,
                timestamp=timestamp,
                parent_event_id=parent,
                raw_ref=raw_ref,
            )

            if etype == "user":
                events.extend(
                    self._user_events(
                        raw_event,
                        uuid,
                        common,
                        timestamp_inferred=timestamp_inferred,
                        tool_use_names=tool_use_names,
                        tool_event_ids=tool_event_ids,
                    )
                )
            elif etype == "assistant":
                events.extend(
                    self._assistant_events(
                        raw_event,
                        uuid,
                        common,
                        timestamp_inferred=timestamp_inferred,
                        tool_use_names=tool_use_names,
                        tool_event_ids=tool_event_ids,
                    )
                )
            elif etype == "system":
                event = self._system_event(
                    raw_event,
                    uuid,
                    common,
                    timestamp_inferred=timestamp_inferred,
                )
                if event.event_type == "unknown":
                    unknown += 1
                    gaps.add(f"system/{raw_event.get('subtype') or '<missing>'}")
                events.append(event)
            elif etype == "attachment":
                att = raw_event.get("attachment") or {}
                atype = att.get("type", "attachment")
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="attachment",
                        summary=f"attachment: {atype}",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                        ),
                        **common,
                    )
                )
            elif etype == "permission-mode":
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="permission",
                        summary=f"permission-mode={raw_event.get('permissionMode')}",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                        ),
                        **common,
                    )
                )
            elif etype in {
                "file-history-snapshot",
                "file-history-delta",
                "ai-title",
                "last-prompt",
                "pr-link",
                "bridge-session",
                "atis-latch",
                "agent-name",
                "worktree-state",
                "frame-link",
            }:
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="subagent" if etype == "agent-name" else "system",
                        event_type="metadata",
                        summary=etype,
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                            metadata_kind=etype.replace("-", "_"),
                        ),
                        **common,
                    )
                )
            elif etype == "cost-state":
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="usage",
                        summary="cost state",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                        ),
                        **common,
                    )
                )
            elif etype == "mode":
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="lifecycle",
                        summary=f"mode={raw_event.get('mode')}",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                            lifecycle="mode_change",
                        ),
                        **common,
                    )
                )
            elif etype == "queue-operation":
                operation = str(raw_event.get("operation") or "update")
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="lifecycle",
                        summary=f"queue {operation}",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                            lifecycle=f"queue_{operation}",
                        ),
                        **common,
                    )
                )
            else:
                unknown += 1
                gaps.add(etype or "<missing>")
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="unknown",
                        summary=f"unknown: type={etype}",
                        payload=normalized_payload(
                            raw_event,
                            timestamp_inferred=timestamp_inferred,
                        ),
                        **common,
                    )
                )

        if session_started and last_raw_ref is not None:
            events.append(
                NormalizedEvent(
                    event_id=f"{session_id}:session-end",
                    session_id=session_id,
                    host="claude-code",
                    sequence=seq + 1,
                    timestamp=last_timestamp,
                    parent_event_id=None,
                    raw_ref=last_raw_ref,
                    actor="system",
                    event_type="session_end",
                    summary="claude-code session end",
                    payload={"cwd": last_cwd} if last_cwd else {},
                )
            )

        unknown_events = [event for event in events if event.event_type == "unknown"]
        unknown = len(unknown_events)
        gaps.update(
            event.summary
            for event in unknown_events
            if event.summary.startswith(("user content[", "assistant content[", "system subtype="))
        )
        return events, unknown, sorted(gaps)

    def _system_event(
        self,
        raw: dict[str, Any],
        uuid: str,
        common: dict[str, Any],
        *,
        timestamp_inferred: bool,
    ) -> NormalizedEvent:
        subtype = str(raw.get("subtype") or "")
        message = raw.get("message") or raw.get("content") or ""
        if isinstance(message, dict):
            message = message.get("content", "") or ""
        text = str(message)
        payload = normalized_payload(
            raw,
            timestamp_inferred=timestamp_inferred,
            **({"text": text} if text else {}),
        )
        if subtype == "api_error":
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="error",
                summary=truncate_summary(
                    str((raw.get("error") or {}).get("message") or "Claude API error")
                    if isinstance(raw.get("error"), dict)
                    else str(raw.get("error") or "Claude API error")
                ),
                payload=payload,
                **common,
            )
        if subtype == "turn_duration":
            payload["lifecycle"] = "turn_end"
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="lifecycle",
                summary=f"turn duration {raw.get('durationMs')}ms",
                payload=payload,
                **common,
            )
        if subtype == "stop_hook_summary":
            payload["lifecycle"] = "hook_end"
            return NormalizedEvent(
                event_id=uuid,
                actor="hook",
                event_type="lifecycle",
                summary="stop hook summary",
                payload=payload,
                **common,
            )
        if subtype == "scheduled_task_fire":
            payload["lifecycle"] = "scheduled_task_fire"
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="lifecycle",
                summary=truncate_summary(text or "scheduled task fired"),
                payload=payload,
                **common,
            )
        if subtype == "away_summary":
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="attachment",
                summary=truncate_summary(text or "away summary"),
                payload=payload,
                **common,
            )
        if subtype == "local_command":
            payload["command"] = text
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="command",
                summary=truncate_summary(text or "local command"),
                payload=payload,
                **common,
            )
        if subtype == "informational":
            return NormalizedEvent(
                event_id=uuid,
                actor="system",
                event_type="message",
                summary=truncate_summary(text or "informational"),
                payload=payload,
                **common,
            )
        return NormalizedEvent(
            event_id=uuid,
            actor="system",
            event_type="unknown",
            summary=f"system subtype={subtype or '<missing>'}",
            payload=payload,
            **common,
        )

    def _user_events(
        self,
        raw: dict[str, Any],
        uuid: str,
        common: dict[str, Any],
        *,
        timestamp_inferred: bool,
        tool_use_names: dict[str, str],
        tool_event_ids: dict[str, str],
    ) -> Iterator[NormalizedEvent]:
        msg = raw.get("message") or {}
        content = msg.get("content")
        sidechain = raw.get("isSidechain") is True
        message_actor: Actor = "subagent" if sidechain else "user"
        injected = raw.get("isMeta") is True
        if isinstance(content, str):
            yield NormalizedEvent(
                event_id=uuid,
                actor="system" if injected else message_actor,
                event_type="attachment" if injected else "message",
                summary=truncate_summary(content),
                payload=_claude_content_payload(
                    raw,
                    msg,
                    None,
                    0,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
            return
        if not isinstance(content, list):
            yield NormalizedEvent(
                event_id=uuid,
                actor=message_actor,
                event_type="message",
                summary=truncate_summary(str(content)),
                payload=_claude_content_payload(
                    raw,
                    msg,
                    None,
                    0,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
            return
        result_indices = [
            index
            for index, item in enumerate(content)
            if isinstance(item, dict) and item.get("type") == "tool_result"
        ]
        structured_result_index = result_indices[-1] if result_indices else None
        for idx, part in enumerate(content):
            part_id = uuid if idx == 0 else f"{uuid}#{idx}"
            ptype = part.get("type") if isinstance(part, dict) else None
            if ptype == "text":
                text = part.get("text", "")
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="system" if injected else message_actor,
                    event_type="attachment" if injected else "message",
                    summary=truncate_summary(text),
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )
            elif ptype == "tool_result":
                raw_tool_use_id = part.get("tool_use_id")
                call_id = str(raw_tool_use_id) if raw_tool_use_id else None
                tool_name = tool_use_names.get(call_id or "", "?")
                result_common = dict(common)
                result_common["parent_event_id"] = tool_event_ids.get(call_id or "") or common.get(
                    "parent_event_id"
                )
                tool_use_result = raw.get("toolUseResult")
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="tool",
                    event_type="tool_result",
                    summary=f"tool_result: {tool_name}",
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                        name=tool_name,
                        tool_name=tool_name,
                        tool_use_id=call_id,
                        call_id=call_id,
                        **(
                            {"tool_use_result": tool_use_result}
                            if idx == structured_result_index
                            and tool_use_result is not None
                            else {}
                        ),
                        **normalized_result_fields(part, tool_use_result),
                    ),
                    **result_common,
                )
            elif ptype == "image":
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=message_actor,
                    event_type="attachment",
                    summary="image attachment",
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )
            else:
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=message_actor,
                    event_type="unknown",
                    summary=f"user content[{idx}].type={ptype}",
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )

    def _assistant_events(
        self,
        raw: dict[str, Any],
        uuid: str,
        common: dict[str, Any],
        *,
        timestamp_inferred: bool,
        tool_use_names: dict[str, str],
        tool_event_ids: dict[str, str],
    ) -> Iterator[NormalizedEvent]:
        msg = raw.get("message") or {}
        content = msg.get("content")
        actor: Actor = "subagent" if raw.get("isSidechain") is True else "assistant"
        if not isinstance(content, list):
            yield NormalizedEvent(
                event_id=uuid,
                actor=actor,
                event_type="message",
                summary=truncate_summary(str(content)),
                payload=_claude_content_payload(
                    raw,
                    msg,
                    None,
                    0,
                    timestamp_inferred=timestamp_inferred,
                    raw_content=content,
                ),
                **common,
            )
            return
        for idx, part in enumerate(content):
            part_id = uuid if idx == 0 else f"{uuid}#{idx}"
            ptype = part.get("type") if isinstance(part, dict) else None
            if ptype == "text":
                text = part.get("text", "")
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=actor,
                    event_type="message",
                    summary=truncate_summary(text),
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )
            elif ptype == "thinking":
                text = part.get("thinking", "")
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=actor,
                    event_type="reasoning",
                    summary=truncate_summary(text),
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )
            elif ptype == "tool_use":
                tool_name = part.get("name", "?")
                tool_input = part.get("input", {})
                tool_id = part.get("id")
                event_type = _TOOL_TYPE_OVERRIDES.get(tool_name, "tool_call")
                if tool_id:
                    call_id = str(tool_id)
                    tool_use_names[call_id] = tool_name
                    tool_event_ids[call_id] = part_id
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=actor,
                    event_type=event_type,
                    summary=f"{tool_name}({_summarize_input(tool_input)})",
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                        tool_id=tool_id,
                        call_id=tool_id,
                        name=tool_name,
                    ),
                    **common,
                )
            else:
                yield NormalizedEvent(
                    event_id=part_id,
                    actor=actor,
                    event_type="unknown",
                    summary=f"assistant content[{idx}].type={ptype}",
                    payload=_claude_content_payload(
                        raw,
                        msg,
                        part,
                        idx,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )


def _claude_content_payload(
    raw: dict[str, Any],
    message: dict[str, Any],
    part: Any,
    index: int,
    *,
    timestamp_inferred: bool,
    **fields: Any,
) -> dict[str, Any]:
    payload = (
        {
            key: value
            for key, value in raw.items()
            if key not in {"message", "toolUseResult"}
        }
        if index == 0
        else {}
    )
    if part is not None:
        payload["part"] = part
    if index == 0 and part is None:
        payload["message"] = message
    elif index == 0:
        message_metadata = {
            key: value
            for key, value in message.items()
            if key != "content"
        }
        if message_metadata:
            payload["message"] = message_metadata
    return normalized_payload(
        payload,
        timestamp_inferred=timestamp_inferred,
        **fields,
    )


def _summarize_input(value: Any, limit: int = 80) -> str:
    if isinstance(value, dict):
        # Prefer a couple of likely-meaningful keys.
        for k in ("file_path", "path", "command", "pattern", "url", "query", "description"):
            if k in value and isinstance(value[k], (str, int, float)):
                return f"{k}={truncate_summary(str(value[k]), limit)}"
        return truncate_summary(json.dumps(value, ensure_ascii=False), limit)
    return truncate_summary(str(value), limit)
