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

from ..schema import NormalizedEvent, RawRef, write_events
from ..storage import Layout
from ..utils import iter_jsonl, truncate_summary
from .base import ImportResult

CODEX_HOME = Path.home() / ".codex"
STATE_DB = CODEX_HOME / "state_5.sqlite"
LOGS_DB = CODEX_HOME / "logs_2.sqlite"

RootKind = Literal["sqlite_home", "sessions_dir", "jsonl_dir"]

# Codex tool/function name -> normalized event_type.
_FUNCTION_TYPE_OVERRIDES: dict[str, str] = {
    "exec_command": "command",
    "shell": "command",
    "apply_patch": "file_edit",
    "read_file": "file_read",
    "view_image": "file_read",
}
_CUSTOM_TOOL_TYPE_OVERRIDES: dict[str, str] = {
    "apply_patch": "file_edit",
}


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
    host = "codex"

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

    def _pick_primary_home(self) -> Path:
        for root in self.roots:
            if _classify_root(root) == "sqlite_home":
                return root
        return self.roots[0] if self.roots else CODEX_HOME

    # ---- discovery -----------------------------------------------------------

    def discover(self) -> list[CodexThread]:
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
        return threads

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
        return [
            CodexThread(
                thread_id=r["id"],
                rollout_path=Path(r["rollout_path"]),
                title=r["title"] or "",
                cwd=r["cwd"] or "",
                created_at=r["created_at"] or 0,
                updated_at=r["updated_at"] or 0,
                model_provider=r["model_provider"] or "",
                git_branch=r["git_branch"],
                archived=bool(r["archived"]),
                source_kind="sqlite_home",
                codex_home=root,
            )
            for r in rows
        ]

    def _discover_jsonl_dir(self, scan_dir: Path, root: Path, kind: RootKind) -> list[CodexThread]:
        if not scan_dir.is_dir():
            return []
        out: list[CodexThread] = []
        for jsonl in scan_dir.rglob("*.jsonl"):
            meta = _read_session_meta(jsonl)
            if meta is None:
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
        if raw_dir.exists() and not force:
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

        events, unknown, gaps = self._normalize(raw_rollout, thread.thread_id)
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

    # ---- normalization -------------------------------------------------------

    def _normalize(
        self, rollout_path: Path, thread_id: str
    ) -> tuple[list[NormalizedEvent], int, list[str]]:
        events: list[NormalizedEvent] = []
        unknown = 0
        gaps: set[str] = set()
        call_id_to_event_type: dict[str, str] = {}
        call_id_to_name: dict[str, str] = {}

        for line_no, raw in iter_jsonl(rollout_path):
            seq = line_no
            etype = raw.get("type")
            ts = raw.get("timestamp")
            raw_payload = raw.get("payload")
            payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
            ptype = payload.get("type")
            event_id = f"{thread_id}:{line_no}"
            raw_ref = RawRef(path=str(rollout_path), line=line_no)
            common = dict(
                event_id=event_id,
                session_id=thread_id,
                host="codex",
                sequence=seq,
                timestamp=ts,
                parent_event_id=None,
                raw_ref=raw_ref,
            )

            if etype == "session_meta":
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="session_start",
                        summary=f"codex session start cwd={payload.get('cwd')}",
                        payload=payload,
                        **common,
                    )
                )
            elif etype == "turn_context":
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="attachment",
                        summary=f"turn_context turn_id={payload.get('turn_id') or raw.get('turn_id')}",
                        payload=payload or raw,
                        **common,
                    )
                )
            elif etype == "event_msg":
                ev = self._event_msg(ptype, payload, common)
                if ev is None:
                    unknown += 1
                    gaps.add(f"event_msg/{ptype}")
                    events.append(
                        NormalizedEvent(
                            actor="system",
                            event_type="unknown",
                            summary=f"event_msg unknown payload.type={ptype}",
                            payload=payload,
                            **common,
                        )
                    )
                else:
                    events.append(ev)
            elif etype == "response_item":
                ev = self._response_item(
                    ptype, payload, common, call_id_to_event_type, call_id_to_name
                )
                if ev is None:
                    continue  # intentional skips (duplicates of event_msg)
                if ev.event_type == "unknown":
                    unknown += 1
                    gaps.add(f"response_item/{ptype}")
                events.append(ev)
            else:
                unknown += 1
                gaps.add(etype or "<missing>")
                events.append(
                    NormalizedEvent(
                        actor="system",
                        event_type="unknown",
                        summary=f"unknown type={etype}",
                        payload=raw,
                        **common,
                    )
                )

        return events, unknown, sorted(gaps)

    def _event_msg(
        self, ptype: str | None, payload: dict[str, Any], common: dict[str, Any]
    ) -> NormalizedEvent | None:
        if ptype == "user_message":
            text = payload.get("message", "")
            return NormalizedEvent(
                actor="user",
                event_type="message",
                summary=truncate_summary(text),
                payload={"text": text, "images": payload.get("images") or []},
                **common,
            )
        if ptype == "agent_message":
            text = payload.get("message", "")
            return NormalizedEvent(
                actor="assistant",
                event_type="message",
                summary=truncate_summary(text),
                payload={"text": text, "phase": payload.get("phase")},
                **common,
            )
        if ptype == "task_started":
            return NormalizedEvent(
                actor="system",
                event_type="attachment",
                summary=f"task_started turn={payload.get('turn_id')}",
                payload=payload,
                **common,
            )
        if ptype == "task_complete":
            return NormalizedEvent(
                actor="system",
                event_type="attachment",
                summary=f"task_complete duration_ms={payload.get('duration_ms')}",
                payload=payload,
                **common,
            )
        if ptype == "token_count":
            return NormalizedEvent(
                actor="system",
                event_type="attachment",
                summary="token_count",
                payload=payload,
                **common,
            )
        if ptype == "patch_apply_end":
            ok = payload.get("success")
            files = list((payload.get("changes") or {}).keys())
            return NormalizedEvent(
                actor="tool",
                event_type="file_edit",
                summary=f"patch_apply success={ok} files={len(files)}",
                payload=payload,
                **common,
            )
        if ptype == "web_search_end":
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"web_search query={truncate_summary(payload.get('query',''))}",
                payload=payload,
                **common,
            )
        return None

    def _response_item(
        self,
        ptype: str | None,
        payload: dict[str, Any],
        common: dict[str, Any],
        call_id_to_event_type: dict[str, str],
        call_id_to_name: dict[str, str],
    ) -> NormalizedEvent | None:
        if ptype == "message":
            role = payload.get("role")
            if role == "developer":
                # developer-role message is the system/permissions prompt
                text = _flatten_content(payload.get("content"))
                return NormalizedEvent(
                    actor="system",
                    event_type="message",
                    summary=truncate_summary(text),
                    payload={"role": role, "content": payload.get("content")},
                    **common,
                )
            # user/assistant text already surfaced via event_msg.{user_message,agent_message}
            return None
        if ptype == "reasoning":
            # Reasoning content is typically encrypted; keep the envelope for completeness.
            return NormalizedEvent(
                actor="assistant",
                event_type="reasoning",
                summary="reasoning (encrypted)",
                payload=payload,
                **common,
            )
        if ptype == "function_call":
            name = payload.get("name", "?")
            args_raw = payload.get("arguments", "")
            args = _maybe_json(args_raw)
            event_type = _FUNCTION_TYPE_OVERRIDES.get(name, "tool_call")
            call_id = payload.get("call_id")
            if call_id:
                call_id_to_event_type[call_id] = event_type
                call_id_to_name[call_id] = name
            return NormalizedEvent(
                actor="assistant",
                event_type=event_type,
                summary=f"{name}({_summarize_args(args)})",
                payload={"name": name, "arguments": args, "call_id": call_id},
                **common,
            )
        if ptype == "function_call_output":
            call_id = payload.get("call_id")
            name = call_id_to_name.get(call_id, "?")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"tool_result: {name}",
                payload={
                    "name": name,
                    "call_id": call_id,
                    "output": payload.get("output"),
                },
                **common,
            )
        if ptype == "custom_tool_call":
            name = payload.get("name", "?")
            event_type = _CUSTOM_TOOL_TYPE_OVERRIDES.get(name, "tool_call")
            call_id = payload.get("call_id")
            if call_id:
                call_id_to_event_type[call_id] = event_type
                call_id_to_name[call_id] = name
            return NormalizedEvent(
                actor="assistant",
                event_type=event_type,
                summary=f"{name} (custom_tool)",
                payload={"name": name, "input": payload.get("input"), "call_id": call_id},
                **common,
            )
        if ptype == "custom_tool_call_output":
            call_id = payload.get("call_id")
            name = call_id_to_name.get(call_id, "?")
            return NormalizedEvent(
                actor="tool",
                event_type="tool_result",
                summary=f"tool_result: {name}",
                payload={
                    "name": name,
                    "call_id": call_id,
                    "output": payload.get("output"),
                },
                **common,
            )
        if ptype == "web_search_call":
            return NormalizedEvent(
                actor="assistant",
                event_type="tool_call",
                summary="web_search",
                payload=payload,
                **common,
            )
        return NormalizedEvent(
            actor="system",
            event_type="unknown",
            summary=f"response_item unknown payload.type={ptype}",
            payload=payload,
            **common,
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
