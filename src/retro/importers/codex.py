"""Codex importer.

Discovery prefers ~/.codex/state_5.sqlite.threads.rollout_path. `CODEX_HOME`
(comma-separated) overrides defaults — matches ccusage behavior. Each root is
classified as one of:

  - **sqlite_home**: has `state_5.sqlite`; use SQLite-backed discovery.
  - **sessions_dir**: has `sessions/` but no SQLite; scan that directory for
    rollout JSONL files (read `session_meta` from each to recover metadata).
  - **jsonl_dir**: neither; treat the directory itself as a flat JSONL bag,
    which matches `codex exec --json` output.

Rollout JSONL is the source of truth and is copied verbatim to raw/.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..schema import Actor, EventType, Host, NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import artifact_ref, iter_jsonl, payload_tool_output, truncate_summary
from .base import (
    ImportResult,
    has_only_repo_state_capture,
    is_ghostlab_originator,
)
from .normalization import normalized_payload, normalized_result_fields, resolve_timestamp

CODEX_HOME = Path.home() / ".codex"
STATE_DB = CODEX_HOME / "state_5.sqlite"
LOGS_DB = CODEX_HOME / "logs_2.sqlite"

RootKind = Literal["sqlite_home", "sessions_dir", "jsonl_dir"]

# Codex tool/function name -> normalized event_type.
_FUNCTION_TYPE_OVERRIDES: dict[str, EventType] = {
    "exec_command": "command",
    "shell": "command",
    "apply_patch": "file_edit",
    "read_file": "file_read",
    "view_image": "file_read",
}
_CUSTOM_TOOL_TYPE_OVERRIDES: dict[str, EventType] = {
    "apply_patch": "file_edit",
}
NORMALIZER_VERSION = 3


@dataclass
class CodexThread:
    thread_id: str
    rollout_path: Path
    title: str
    cwd: str
    created_at: int
    updated_at: int
    model_provider: str
    git_branch: str | None
    archived: bool
    originator: str = ""
    source_kind: RootKind = "sqlite_home"
    codex_home: Path | None = None  # the root this thread was discovered under

    @property
    def display_title(self) -> str:
        t = (self.title or "").strip().splitlines()[0] if self.title else ""
        return t[:120]


def _resolve_codex_roots(explicit: tuple[Path, ...] | None = None) -> list[Path]:
    """Resolve Codex roots to scan.

    `CODEX_HOME` wins when set (comma-separated supported). Otherwise default
    to `~/.codex`.
    """
    if explicit:
        return list(explicit)
    env = os.environ.get("CODEX_HOME")
    if env:
        roots: list[Path] = []
        for raw in env.split(","):
            piece = raw.strip()
            if not piece:
                continue
            roots.append(Path(piece).expanduser())
        if roots:
            return roots
    return [CODEX_HOME]


def _classify_root(root: Path) -> RootKind:
    if (root / "state_5.sqlite").exists():
        return "sqlite_home"
    if (root / "sessions").is_dir():
        return "sessions_dir"
    return "jsonl_dir"


class CodexImporter:
    host: Host = "codex"

    def __init__(
        self,
        layout: Layout,
        codex_home: Path | None = None,
        roots: tuple[Path, ...] | None = None,
    ):
        self.layout = layout
        if codex_home is not None:
            self.roots: list[Path] = [codex_home]
        else:
            self.roots = _resolve_codex_roots(roots)
        # Primary SQLite home — used for spawn-edge lookups. Prefer the first
        # root that classifies as a SQLite home; otherwise fall back to the
        # first root (lookups will gracefully return empty).
        self.codex_home = self._pick_primary_home()
        self.state_db = self.codex_home / "state_5.sqlite"
        self._discover_cache: list[CodexThread] | None = None

    def _pick_primary_home(self) -> Path:
        for root in self.roots:
            if _classify_root(root) == "sqlite_home":
                return root
        return self.roots[0] if self.roots else CODEX_HOME

    # ---- discovery -----------------------------------------------------------

    def discover(self) -> list[CodexThread]:
        if self._discover_cache is not None:
            return list(self._discover_cache)
        threads: list[CodexThread] = []
        seen_ids: set[str] = set()
        for root in self.roots:
            kind = _classify_root(root)
            for thread in self._discover_root(root, kind):
                if thread.thread_id in seen_ids:
                    continue
                seen_ids.add(thread.thread_id)
                threads.append(thread)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        self._discover_cache = threads
        return list(self._discover_cache)

    def _discover_root(self, root: Path, kind: RootKind) -> list[CodexThread]:
        if kind == "sqlite_home":
            return self._discover_sqlite(root)
        scan_dir = root / "sessions" if kind == "sessions_dir" else root
        return self._discover_jsonl_dir(scan_dir, root, kind)

    def _discover_sqlite(self, root: Path) -> list[CodexThread]:
        state_db = root / "state_5.sqlite"
        if not state_db.exists():
            return []
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT id, rollout_path, title, cwd, created_at, updated_at, "
                "model_provider, git_branch, archived FROM threads "
                "ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            con.close()
        threads: list[CodexThread] = []
        for row in rows:
            rollout_path = Path(row["rollout_path"])
            meta = _read_session_meta(rollout_path) or {}
            originator = meta.get("originator")
            if is_ghostlab_originator(originator):
                continue
            threads.append(
                CodexThread(
                    thread_id=row["id"],
                    rollout_path=rollout_path,
                    title=row["title"] or "",
                    cwd=row["cwd"] or "",
                    created_at=row["created_at"] or 0,
                    updated_at=row["updated_at"] or 0,
                    model_provider=row["model_provider"] or "",
                    git_branch=row["git_branch"],
                    archived=bool(row["archived"]),
                    originator=str(originator or ""),
                    source_kind="sqlite_home",
                    codex_home=root,
                )
            )
        return threads

    def _discover_jsonl_dir(self, scan_dir: Path, root: Path, kind: RootKind) -> list[CodexThread]:
        if not scan_dir.is_dir():
            return []
        out: list[CodexThread] = []
        for jsonl in scan_dir.rglob("*.jsonl"):
            meta = _read_session_meta(jsonl)
            if meta is None:
                continue
            originator = meta.get("originator")
            if is_ghostlab_originator(originator):
                continue
            try:
                mtime = int(jsonl.stat().st_mtime)
            except FileNotFoundError:
                continue
            out.append(
                CodexThread(
                    thread_id=meta.get("id") or jsonl.stem,
                    rollout_path=jsonl,
                    title=meta.get("title") or "",
                    cwd=meta.get("cwd") or "",
                    created_at=meta.get("created_at") or mtime,
                    updated_at=meta.get("updated_at") or mtime,
                    model_provider=meta.get("model_provider") or "",
                    git_branch=meta.get("git_branch"),
                    archived=False,
                    originator=str(originator or ""),
                    source_kind=kind,
                    codex_home=root,
                )
            )
        return out

    def find_thread(self, thread_id: str) -> CodexThread | None:
        for t in self.discover():
            if t.thread_id == thread_id:
                return t
        return None

    def latest(self) -> CodexThread | None:
        threads = self.discover()
        return threads[0] if threads else None

    def _spawn_edges(self, thread_id: str) -> dict[str, list[str]]:
        if not self.state_db.exists():
            return {"parents": [], "children": []}
        con = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True)
        try:
            parents = [
                r[0]
                for r in con.execute(
                    "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
            children = [
                r[0]
                for r in con.execute(
                    "SELECT child_thread_id FROM thread_spawn_edges WHERE parent_thread_id = ?",
                    (thread_id,),
                ).fetchall()
            ]
        finally:
            con.close()
        return {"parents": parents, "children": children}

    # ---- import --------------------------------------------------------------

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        thread = self.find_thread(identifier)
        if thread is None:
            raise FileNotFoundError(f"No Codex thread found with id {identifier!r}")
        if not thread.rollout_path.exists():
            raise FileNotFoundError(
                f"Codex thread {thread.thread_id} references missing rollout file "
                f"{thread.rollout_path}"
            )

        raw_dir = self.layout.raw_dir(self.host, thread.thread_id)
        raw_rollout = raw_dir / "rollout.jsonl"
        if raw_dir.exists() and not force:
            if raw_rollout.exists():
                should_raise = False
                try:
                    src_stat = thread.rollout_path.stat()
                    raw_stat = raw_rollout.stat()
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
        raw_rollout = raw_dir / "rollout.jsonl"
        shutil.copy2(thread.rollout_path, raw_rollout)

        thread_meta: dict[str, Any] = {
            "thread_id": thread.thread_id,
            "title": thread.title,
            "cwd": thread.cwd,
            "model_provider": thread.model_provider,
            "git_branch": thread.git_branch,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "archived": thread.archived,
            "originator": thread.originator,
            "source_rollout": str(thread.rollout_path),
            "source_kind": thread.source_kind,
            "codex_home": str(thread.codex_home) if thread.codex_home else None,
            "spawn_edges": (
                self._spawn_edges(thread.thread_id)
                if thread.source_kind == "sqlite_home"
                else {"parents": [], "children": []}
            ),
        }
        (raw_dir / "thread.json").write_text(
            json.dumps(thread_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        events, unknown, gaps = self.normalize_raw_dir(raw_dir, thread.thread_id)
        normalized_path = self.layout.normalized_path(self.host, thread.thread_id)
        count = write_events(normalized_path, events)

        return ImportResult(
            host=self.host,
            session_id=thread.thread_id,
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
        rollout_path = raw_dir / "rollout.jsonl"
        if not rollout_path.is_file():
            raise FileNotFoundError(f"Archived Codex rollout is missing: {rollout_path}")
        return self._normalize(rollout_path, session_id)

    # ---- normalization -------------------------------------------------------

    def _normalize(
        self, rollout_path: Path, thread_id: str
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()
        call_id_to_name: dict[str, str] = {}
        call_id_to_event_id: dict[str, str] = {}
        represented_messages: dict[tuple[Actor, str], str] = {}
        last_sequence = 0
        last_timestamp: str | None = None
        last_raw_ref: RawRef | None = None
        session_cwd = ""
        assistant_actor: Actor = "assistant"

        for line_no, raw in iter_jsonl(rollout_path):
            seq = line_no
            etype = raw.get("type")
            raw_timestamp = raw.get("timestamp")
            explicit_timestamp = raw_timestamp if isinstance(raw_timestamp, str) else None
            timestamp, last_timestamp, timestamp_inferred = resolve_timestamp(
                explicit_timestamp,
                last_timestamp,
            )
            raw_payload = raw.get("payload")
            payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
            ptype = payload.get("type")
            event_id = f"{thread_id}:{line_no}"
            raw_ref = RawRef(
                path=artifact_ref(rollout_path, self.layout.root),
                line=line_no,
            )
            last_sequence = seq
            last_raw_ref = raw_ref
            common: dict[str, Any] = dict(
                event_id=event_id,
                session_id=thread_id,
                host="codex",
                sequence=seq,
                timestamp=timestamp,
                parent_event_id=None,
                raw_ref=raw_ref,
            )

            if etype == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str):
                    session_cwd = cwd
                if _is_subagent_session(payload):
                    assistant_actor = "subagent"
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="session_start",
                        summary=f"codex session start cwd={payload.get('cwd')}",
                        payload=normalized_payload(
                            payload,
                            timestamp_inferred=timestamp_inferred,
                            subagent=assistant_actor == "subagent",
                        ),
                        **common,
                    )
                )
            elif etype == "turn_context":
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="metadata",
                        summary=f"turn_context turn_id={payload.get('turn_id') or raw.get('turn_id')}",
                        payload=normalized_payload(
                            payload or raw,
                            timestamp_inferred=timestamp_inferred,
                            metadata_kind="turn_context",
                        ),
                        **common,
                    )
                )
            elif etype == "event_msg":
                ev = self._event_msg(
                    ptype,
                    payload,
                    common,
                    timestamp_inferred=timestamp_inferred,
                    assistant_actor=assistant_actor,
                    represented_messages=represented_messages,
                    call_id_to_name=call_id_to_name,
                    call_id_to_event_id=call_id_to_event_id,
                )
                if ev is None:
                    unknown += 1
                    gaps.add(f"event_msg/{ptype}")
                    events.append(
                        NormalizedEvent(
                            actor="system",
                            event_type="unknown",
                            summary=f"event_msg unknown payload.type={ptype}",
                            payload=normalized_payload(
                                payload,
                                timestamp_inferred=timestamp_inferred,
                            ),
                            **common,
                        )
                    )
                else:
                    events.append(ev)
            elif etype == "response_item":
                ev = self._response_item(
                    ptype,
                    payload,
                    common,
                    timestamp_inferred=timestamp_inferred,
                    assistant_actor=assistant_actor,
                    represented_messages=represented_messages,
                    call_id_to_name=call_id_to_name,
                    call_id_to_event_id=call_id_to_event_id,
                )
                if ev.event_type == "unknown":
                    unknown += 1
                    gaps.add(f"response_item/{ptype}")
                events.append(ev)
            elif etype == "world_state":
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="metadata",
                        summary="world state",
                        payload=normalized_payload(
                            payload,
                            timestamp_inferred=timestamp_inferred,
                            metadata_kind="world_state",
                        ),
                        **common,
                    )
                )
            elif etype == "compacted":
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="compaction",
                        summary="context compacted",
                        payload=normalized_payload(
                            payload,
                            timestamp_inferred=timestamp_inferred,
                            lifecycle="compaction_complete",
                        ),
                        **common,
                    )
                )
            elif etype == "inter_agent_communication_metadata":
                events.append(
                    NormalizedEvent(
                        actor="subagent",
                        event_type="metadata",
                        summary="inter-agent communication metadata",
                        payload=normalized_payload(
                            payload,
                            timestamp_inferred=timestamp_inferred,
                            metadata_kind="inter_agent_communication",
                        ),
                        **common,
                    )
                )
            else:
                unknown += 1
                gaps.add(etype or "<missing>")
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="unknown",
                        summary=f"unknown type={etype}",
                        payload=normalized_payload(
                            raw,
                            timestamp_inferred=timestamp_inferred,
                        ),
                        **common,
                    )
                )

        for event in events:
            if event.event_type != "tool_result" or event.parent_event_id is not None:
                continue
            call_id = event.payload.get("call_id")
            if call_id:
                event.parent_event_id = call_id_to_event_id.get(str(call_id))
        _deduplicate_tool_results(events)

        if events and last_raw_ref is not None:
            events.append(
                NormalizedEvent(
                    event_id=f"{thread_id}:session-end",
                    session_id=thread_id,
                    host="codex",
                    sequence=last_sequence + 1,
                    timestamp=last_timestamp,
                    parent_event_id=None,
                    raw_ref=last_raw_ref,
                    actor="system",
                    event_type="session_end",
                    summary="codex session end",
                    payload={"cwd": session_cwd} if session_cwd else {},
                )
            )

        return events, unknown, sorted(gaps)

    def _event_msg(
        self,
        ptype: str | None,
        payload: dict[str, Any],
        common: dict[str, Any],
        *,
        timestamp_inferred: bool,
        assistant_actor: Actor,
        represented_messages: dict[tuple[Actor, str], str],
        call_id_to_name: dict[str, str],
        call_id_to_event_id: dict[str, str],
    ) -> NormalizedEvent | None:
        if ptype == "user_message":
            text = str(payload.get("message") or "")
            actor: Actor = "subagent" if assistant_actor == "subagent" else "user"
            duplicate_of = represented_messages.get((actor, text))
            represented_messages.setdefault((actor, text), str(common["event_id"]))
            return NormalizedEvent(
                actor=actor,
                event_type="metadata" if duplicate_of else "message",
                summary=truncate_summary(text),
                payload=_message_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    duplicate_of=duplicate_of,
                ),
                **common,
            )
        if ptype == "agent_message":
            text = str(payload.get("message") or "")
            duplicate_of = represented_messages.get((assistant_actor, text))
            represented_messages.setdefault(
                (assistant_actor, text),
                str(common["event_id"]),
            )
            return NormalizedEvent(
                actor=assistant_actor,
                event_type="metadata" if duplicate_of else "message",
                summary=truncate_summary(text),
                payload=_message_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    duplicate_of=duplicate_of,
                ),
                **common,
            )
        if ptype == "agent_reasoning":
            text = str(payload.get("text") or "")
            return NormalizedEvent(
                actor=assistant_actor,
                event_type="reasoning",
                summary=truncate_summary(text),
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
        if ptype == "task_started":
            return NormalizedEvent(
                actor="system",
                event_type="lifecycle",
                summary=f"task_started turn={payload.get('turn_id')}",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle="task_started",
                ),
                **common,
            )
        if ptype == "task_complete":
            return NormalizedEvent(
                actor="system",
                event_type="lifecycle",
                summary=f"task_complete duration_ms={payload.get('duration_ms')}",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle="task_complete",
                    **normalized_result_fields(payload),
                ),
                **common,
            )
        if ptype == "token_count":
            return NormalizedEvent(
                actor="system",
                event_type="usage",
                summary="token_count",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
        if ptype in {
            "patch_apply_end",
            "exec_command_end",
            "mcp_tool_call_end",
            "web_search_end",
        }:
            call_id_value = payload.get("call_id")
            call_id = str(call_id_value) if call_id_value else None
            name = call_id_to_name.get(call_id or "", ptype.removesuffix("_end"))
            result_common = dict(common)
            result_common["parent_event_id"] = call_id_to_event_id.get(call_id or "")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"tool_result: {name}",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name=name,
                    call_id=call_id,
                    **normalized_result_fields(payload),
                ),
                **result_common,
            )
        if ptype == "item_completed":
            return NormalizedEvent(
                actor="system",
                event_type="lifecycle",
                summary="item completed",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle="item_completed",
                ),
                **common,
            )
        if ptype == "thread_settings_applied":
            return NormalizedEvent(
                actor="system",
                event_type="metadata",
                summary="thread settings applied",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    metadata_kind="thread_settings",
                ),
                **common,
            )
        if ptype == "context_compacted":
            return NormalizedEvent(
                actor="system",
                event_type="compaction",
                summary="context compacted",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle="compaction_complete",
                ),
                **common,
            )
        if ptype == "sub_agent_activity":
            kind = str(payload.get("kind") or "activity")
            event_type: EventType
            if kind == "started":
                event_type = "subagent_start"
            elif kind in {"completed", "failed"}:
                event_type = "subagent_end"
            else:
                event_type = "lifecycle"
            call_id = payload.get("event_id")
            activity_common = dict(common)
            activity_common["parent_event_id"] = call_id_to_event_id.get(str(call_id or ""))
            return NormalizedEvent(
                actor="subagent",
                event_type=event_type,
                summary=f"subagent {kind}: {payload.get('agent_path') or ''}".strip(),
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle=f"subagent_{kind}",
                    call_id=call_id,
                    **normalized_result_fields(payload),
                ),
                **activity_common,
            )
        if ptype in {"turn_aborted", "thread_rolled_back"}:
            lifecycle = "turn_aborted" if ptype == "turn_aborted" else "thread_rolled_back"
            return NormalizedEvent(
                actor="system",
                event_type="lifecycle",
                summary=lifecycle.replace("_", " "),
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    lifecycle=lifecycle,
                ),
                **common,
            )
        return None

    def _response_item(
        self,
        ptype: str | None,
        payload: dict[str, Any],
        common: dict[str, Any],
        *,
        timestamp_inferred: bool,
        assistant_actor: Actor,
        represented_messages: dict[tuple[Actor, str], str],
        call_id_to_name: dict[str, str],
        call_id_to_event_id: dict[str, str],
    ) -> NormalizedEvent:
        if ptype == "message":
            role = str(payload.get("role") or "")
            text = _flatten_content(payload.get("content"))
            if role == "developer":
                return NormalizedEvent(
                    actor="system",
                    event_type="attachment",
                    summary=truncate_summary(text),
                    payload=normalized_payload(
                        payload,
                        timestamp_inferred=timestamp_inferred,
                    ),
                    **common,
                )
            actor: Actor = (
                "subagent"
                if assistant_actor == "subagent"
                else "user"
                if role == "user"
                else "assistant"
            )
            duplicate_of = represented_messages.get((actor, text))
            represented_messages.setdefault((actor, text), str(common["event_id"]))
            return NormalizedEvent(
                actor=actor,
                event_type="metadata" if duplicate_of else "message",
                summary=truncate_summary(text),
                payload=_message_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    duplicate_of=duplicate_of,
                ),
                **common,
            )
        if ptype == "reasoning":
            text = _flatten_reasoning(payload)
            return NormalizedEvent(
                actor=assistant_actor,
                event_type="reasoning",
                summary=truncate_summary(text) or "reasoning (encrypted)",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
        if ptype == "function_call":
            name = str(payload.get("name") or "?")
            args_raw = payload.get("arguments", "")
            args = _maybe_json(args_raw)
            event_type = _FUNCTION_TYPE_OVERRIDES.get(name, "tool_call")
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else None
            if call_id:
                call_id_to_name[call_id] = name
                call_id_to_event_id[call_id] = str(common["event_id"])
            return NormalizedEvent(
                actor=assistant_actor,
                event_type=event_type,
                summary=f"{name}({_summarize_args(args)})",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    call_id=call_id,
                ),
                **common,
            )
        if ptype == "function_call_output":
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else None
            name = call_id_to_name.get(call_id or "", "?")
            result_common = dict(common)
            result_common["parent_event_id"] = call_id_to_event_id.get(call_id or "")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"tool_result: {name}",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name=name,
                    call_id=call_id,
                    **normalized_result_fields(payload),
                ),
                **result_common,
            )
        if ptype == "custom_tool_call":
            name = str(payload.get("name") or "?")
            event_type = _CUSTOM_TOOL_TYPE_OVERRIDES.get(name, "tool_call")
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else None
            if call_id:
                call_id_to_name[call_id] = name
                call_id_to_event_id[call_id] = str(common["event_id"])
            return NormalizedEvent(
                actor=assistant_actor,
                event_type=event_type,
                summary=f"{name} (custom_tool)",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    call_id=call_id,
                ),
                **common,
            )
        if ptype == "custom_tool_call_output":
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else None
            name = call_id_to_name.get(call_id or "", "?")
            result_common = dict(common)
            result_common["parent_event_id"] = call_id_to_event_id.get(call_id or "")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"tool_result: {name}",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name=name,
                    call_id=call_id,
                    **normalized_result_fields(payload),
                ),
                **result_common,
            )
        if ptype == "web_search_call":
            call_id = str(payload.get("call_id") or payload.get("id") or common["event_id"])
            call_id_to_name[call_id] = "web_search"
            call_id_to_event_id[call_id] = str(common["event_id"])
            return NormalizedEvent(
                actor=assistant_actor,
                event_type="tool_call",
                summary="web_search",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name="web_search",
                    call_id=call_id,
                ),
                **common,
            )
        if ptype == "tool_search_call":
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else str(common["event_id"])
            call_id_to_name[call_id] = "tool_search"
            call_id_to_event_id[call_id] = str(common["event_id"])
            return NormalizedEvent(
                actor=assistant_actor,
                event_type="tool_call",
                summary=f"tool_search({_summarize_args(payload.get('arguments'))})",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name="tool_search",
                    call_id=call_id,
                ),
                **common,
            )
        if ptype == "tool_search_output":
            raw_call_id = payload.get("call_id")
            call_id = str(raw_call_id) if raw_call_id else None
            result_common = dict(common)
            result_common["parent_event_id"] = call_id_to_event_id.get(call_id or "")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary="tool_result: tool_search",
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                    name="tool_search",
                    call_id=call_id,
                    **normalized_result_fields(payload),
                ),
                **result_common,
            )
        if ptype == "agent_message":
            text = _flatten_content(payload.get("content"))
            author = str(payload.get("author") or "")
            message_actor: Actor = (
                "subagent" if author and author != "/root" else assistant_actor
            )
            return NormalizedEvent(
                actor=message_actor,
                event_type="message",
                summary=truncate_summary(text),
                payload=normalized_payload(
                    payload,
                    timestamp_inferred=timestamp_inferred,
                ),
                **common,
            )
        return NormalizedEvent(
            actor="system",
            event_type="unknown",
            summary=f"response_item unknown payload.type={ptype}",
            payload=normalized_payload(
                payload,
                timestamp_inferred=timestamp_inferred,
            ),
            **common,
        )


def _message_payload(
    payload: dict[str, Any],
    *,
    timestamp_inferred: bool,
    duplicate_of: str | None,
) -> dict[str, Any]:
    if duplicate_of is not None:
        return normalized_payload(
            {
                "metadata_kind": "duplicate_message",
                "duplicate_of": duplicate_of,
                "source_payload_in_raw": True,
            },
            timestamp_inferred=timestamp_inferred,
        )
    return normalized_payload(
        payload,
        timestamp_inferred=timestamp_inferred,
    )


def _deduplicate_tool_results(events: list[NormalizedEvent]) -> None:
    canonical: dict[tuple[str, str], NormalizedEvent] = {}
    detailed_types = {
        "exec_command_end",
        "patch_apply_end",
        "mcp_tool_call_end",
        "web_search_end",
    }
    for event in events:
        if event.event_type != "tool_result":
            continue
        call_id = event.payload.get("call_id")
        output = payload_tool_output(event.payload)
        if not call_id or output in (None, "", [], {}):
            continue
        try:
            output_key = json.dumps(
                output,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            output_key = repr(output)
        key = str(call_id), output_key
        previous = canonical.get(key)
        if previous is None:
            canonical[key] = event
            continue
        previous_detailed = previous.payload.get("type") in detailed_types
        event_detailed = event.payload.get("type") in detailed_types
        if event_detailed and not previous_detailed:
            _mark_duplicate_result(previous, event)
            canonical[key] = event
        else:
            _mark_duplicate_result(event, previous)


def _mark_duplicate_result(
    duplicate: NormalizedEvent,
    canonical: NormalizedEvent,
) -> None:
    duplicate.actor = "system"
    duplicate.event_type = "metadata"
    duplicate.summary = "duplicate tool result"
    duplicate.payload = normalized_payload(
        {
            "metadata_kind": "duplicate_tool_result",
            "duplicate_of": canonical.event_id,
            "call_id": duplicate.payload.get("call_id"),
            "source_payload_in_raw": True,
        },
        timestamp_inferred=duplicate.payload.get("timestamp_inferred") is True,
    )


def _read_session_meta(path: Path) -> dict[str, Any] | None:
    """Return the first session_meta payload in a Codex rollout JSONL, or None.

    Used when discovering rollouts that live outside a SQLite-backed Codex home
    (e.g., `codex exec --json` output). Reads at most ~16 KB so giant files
    don't penalize discovery.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            for _ in range(8):
                line = fh.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "session_meta":
                    payload = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                    return payload or None
    except OSError:
        return None
    return None


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("text") or c.get("input_text") or ""
                parts.append(t if isinstance(t, str) else json.dumps(t))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    return str(content)


def _flatten_reasoning(payload: dict[str, Any]) -> str:
    for key in ("content", "summary"):
        text = _flatten_content(payload.get(key))
        if text:
            return text
    return ""


def _is_subagent_session(payload: dict[str, Any]) -> bool:
    if payload.get("parent_thread_id") or payload.get("agent_path"):
        return True
    source = payload.get("source")
    thread_source = payload.get("thread_source")
    return (
        isinstance(source, dict)
        and "subagent" in source
        or isinstance(thread_source, dict)
        and "subagent" in thread_source
    )


def _maybe_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _summarize_args(args: Any, limit: int = 80) -> str:
    if isinstance(args, dict):
        for k in ("cmd", "command", "path", "file", "workdir", "query"):
            if k in args:
                return f"{k}={truncate_summary(str(args[k]), limit)}"
        return truncate_summary(json.dumps(args, ensure_ascii=False), limit)
    return truncate_summary(str(args), limit)
