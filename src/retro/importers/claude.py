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

from ..schema import NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import iter_jsonl, truncate_summary
from .base import ImportResult

CLAUDE_HOME = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Default roots scanned when CLAUDE_CONFIG_DIR is not set.
DEFAULT_CLAUDE_ROOTS: tuple[Path, ...] = (
    Path.home() / ".claude",
    Path.home() / ".config" / "claude",
)

# Tool name -> normalized event_type override for tool_call events.
# tool_result events inherit the matching tool_call's override via id-pairing.
_TOOL_TYPE_OVERRIDES: dict[str, str] = {
    "Read": "file_read",
    "Edit": "file_edit",
    "Write": "file_edit",
    "MultiEdit": "file_edit",
    "NotebookEdit": "file_edit",
    "Bash": "command",
}


@dataclass
class ClaudeSession:
    session_id: str
    transcript_path: Path
    project_slug: str
    size_bytes: int
    mtime: float
    claude_home: Path


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
    host = "claude-code"

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

    def _pick_sidecar_root(self) -> Path:
        for root in self.roots:
            if (root / "projects").exists():
                return root
        return self.roots[0] if self.roots else CLAUDE_HOME

    # ---- discovery -----------------------------------------------------------

    def discover(self) -> list[ClaudeSession]:
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
                            size_bytes=st.st_size,
                            mtime=st.st_mtime,
                            claude_home=root,
                        )
                    )
        out.sort(key=lambda s: s.mtime, reverse=True)
        return out

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
            else:
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
            "source_transcript": str(session.transcript_path),
            "claude_home": str(session.claude_home),
            "size_bytes": session.size_bytes,
        }
        (raw_dir / "import_meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        self._snapshot_sidecars(session, raw_dir)

        events, unknown, gaps = self._normalize(raw_transcript, session.session_id)
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
        tool_use_types: dict[str, str] = {}  # tool_use_id -> event_type
        tool_use_names: dict[str, str] = {}
        seq = 0

        for line_no, raw_event in iter_jsonl(transcript_path):
            seq += 1
            etype = raw_event.get("type")
            ts = raw_event.get("timestamp")
            uuid = raw_event.get("uuid") or f"{session_id}:{line_no}"
            parent = raw_event.get("parentUuid")
            raw_ref = RawRef(path=str(transcript_path), line=line_no)

            common = dict(
                session_id=session_id,
                host="claude-code",
                sequence=seq,
                timestamp=ts,
                parent_event_id=parent,
                raw_ref=raw_ref,
            )

            if etype == "user":
                events.extend(
                    self._user_events(raw_event, uuid, common, tool_use_types, tool_use_names)
                )
            elif etype == "assistant":
                events.extend(
                    self._assistant_events(raw_event, uuid, common, tool_use_types, tool_use_names)
                )
            elif etype == "system":
                msg = raw_event.get("message") or raw_event.get("content") or ""
                if isinstance(msg, dict):
                    msg = msg.get("content", "") or ""
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="message",
                        summary=truncate_summary(str(msg)),
                        payload={"message": msg},
                        **common,
                    )
                )
            elif etype == "attachment":
                att = raw_event.get("attachment") or {}
                atype = att.get("type", "attachment")
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="attachment",
                        summary=f"attachment: {atype}",
                        payload={"attachment": att},
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
                        payload=raw_event,
                        **common,
                    )
                )
            elif etype in {"file-history-snapshot", "ai-title", "last-prompt", "pr-link"}:
                events.append(
                    NormalizedEvent(
                        event_id=uuid,
                        actor="system",
                        event_type="attachment",
                        summary=etype,
                        payload=raw_event,
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
                        payload=raw_event,
                        **common,
                    )
                )

        return events, unknown, sorted(gaps)

    def _user_events(
        self,
        raw: dict[str, Any],
        uuid: str,
        common: dict[str, Any],
        tool_use_types: dict[str, str],
        tool_use_names: dict[str, str],
    ) -> Iterator[NormalizedEvent]:
        msg = raw.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            yield NormalizedEvent(
                event_id=uuid,
                actor="user",
                event_type="message",
                summary=truncate_summary(content),
                payload={"text": content},
                **common,
            )
            return
        if not isinstance(content, list):
            yield NormalizedEvent(
                event_id=uuid,
                actor="user",
                event_type="message",
                summary=truncate_summary(str(content)),
                payload={"raw_content": content},
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
                    actor="user",
                    event_type="message",
                    summary=truncate_summary(text),
                    payload={"text": text},
                    **common,
                )
            elif ptype == "tool_result":
                tool_use_id = part.get("tool_use_id")
                inferred_type = tool_use_types.get(tool_use_id, "tool_result")
                # If the original call had a typed override (file_edit/file_read/command),
                # the result mirrors it but we keep it specifically as tool_result for clarity.
                tool_name = tool_use_names.get(tool_use_id, "?")
                payload = {
                    "tool_use_id": tool_use_id,
                    "tool_name": tool_name,
                    "is_error": part.get("is_error", False),
                    "content": part.get("content"),
                }
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="tool",
                    event_type="tool_result" if inferred_type == "tool_result" else inferred_type,
                    summary=f"tool_result: {tool_name}",
                    payload=payload,
                    **common,
                )
            else:
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="user",
                    event_type="unknown",
                    summary=f"user content[{idx}].type={ptype}",
                    payload={"part": part},
                    **common,
                )

    def _assistant_events(
        self,
        raw: dict[str, Any],
        uuid: str,
        common: dict[str, Any],
        tool_use_types: dict[str, str],
        tool_use_names: dict[str, str],
    ) -> Iterator[NormalizedEvent]:
        msg = raw.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            yield NormalizedEvent(
                event_id=uuid,
                actor="assistant",
                event_type="message",
                summary=truncate_summary(str(content)),
                payload={"raw_content": content},
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
                    actor="assistant",
                    event_type="message",
                    summary=truncate_summary(text),
                    payload={"text": text},
                    **common,
                )
            elif ptype == "thinking":
                text = part.get("thinking", "")
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="assistant",
                    event_type="reasoning",
                    summary=truncate_summary(text),
                    payload={"thinking": text},
                    **common,
                )
            elif ptype == "tool_use":
                tool_name = part.get("name", "?")
                tool_input = part.get("input", {})
                tool_id = part.get("id")
                event_type = _TOOL_TYPE_OVERRIDES.get(tool_name, "tool_call")
                if tool_id:
                    tool_use_types[tool_id] = event_type
                    tool_use_names[tool_id] = tool_name
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="assistant",
                    event_type=event_type,
                    summary=f"{tool_name}({_summarize_input(tool_input)})",
                    payload={"tool_id": tool_id, "name": tool_name, "input": tool_input},
                    **common,
                )
            else:
                yield NormalizedEvent(
                    event_id=part_id,
                    actor="assistant",
                    event_type="unknown",
                    summary=f"assistant content[{idx}].type={ptype}",
                    payload={"part": part},
                    **common,
                )


def _summarize_input(value: Any, limit: int = 80) -> str:
    if isinstance(value, dict):
        # Prefer a couple of likely-meaningful keys.
        for k in ("file_path", "path", "command", "pattern", "url", "query", "description"):
            if k in value and isinstance(value[k], (str, int, float)):
                return f"{k}={truncate_summary(str(value[k]), limit)}"
        return truncate_summary(json.dumps(value, ensure_ascii=False), limit)
    return truncate_summary(str(value), limit)
