"""Rebuild derived normalization artifacts from immutable archived raw captures."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import user_state_dir
from .importers.claude import (
    NORMALIZER_VERSION as CLAUDE_NORMALIZER_VERSION,
)
from .importers.claude import ClaudeImporter
from .importers.codex import NORMALIZER_VERSION as CODEX_NORMALIZER_VERSION
from .importers.codex import CodexImporter
from .importers.copilot import NORMALIZER_VERSION as COPILOT_NORMALIZER_VERSION
from .importers.copilot import CopilotImporter
from .locking import exclusive_lock
from .renderer import render_file
from .schema import Host, validate_host_id, validate_session_id, write_events
from .storage import Layout
from .utils import atomic_write_text

INDEX_SCHEMA = 1
RENDERER_VERSION = 2
NORMALIZER_VERSIONS: dict[Host, int] = {
    "claude-code": CLAUDE_NORMALIZER_VERSION,
    "codex": CODEX_NORMALIZER_VERSION,
    "vscode-copilot": COPILOT_NORMALIZER_VERSION,
}
_PRIMARY_RAW_FILES = {
    "claude-code": ("transcript.jsonl",),
    "codex": ("rollout.jsonl",),
    "vscode-copilot": (
        "events.jsonl",
        "transcript.jsonl",
        "session.jsonl",
        "session.json",
        "session.snapshot.json",
    ),
}


@dataclass(frozen=True)
class RenormalizeFailure:
    host: Host
    session_id: str
    error: str


@dataclass
class RenormalizeReport:
    scanned: int = 0
    outdated: int = 0
    rebuilt: int = 0
    skipped: int = 0
    rendered: int = 0
    event_count: int = 0
    unknown_event_count: int = 0
    raw_bytes: int = 0
    normalized_bytes: int = 0
    dry_run: bool = False
    elapsed_seconds: float = 0.0
    failures: list[RenormalizeFailure] = field(default_factory=list)


def parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since value {value!r}; expected ISO-8601 date or timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def renormalize_archive(
    layout: Layout,
    *,
    host: Host | None = None,
    session_id: str | None = None,
    since: datetime | None = None,
    dry_run: bool = False,
    render: bool = True,
) -> RenormalizeReport:
    if host is not None:
        host = validate_host_id(host)
        if host not in NORMALIZER_VERSIONS:
            raise ValueError(f"host {host!r} does not support archived renormalization")
    if session_id is not None:
        validate_session_id(session_id)

    started = time.perf_counter()
    report = RenormalizeReport(dry_run=dry_run)
    lock_path = user_state_dir() / "archive.lock"
    with exclusive_lock(lock_path):
        index = _load_index(layout)
        importers = _normalizers(layout)
        changed_index = False
        for target_host, target_session, raw_dir in _targets(
            layout,
            host=host,
            session_id=session_id,
            since=since,
        ):
            report.scanned += 1
            fingerprint, raw_bytes = _raw_fingerprint(target_host, raw_dir)
            report.raw_bytes += raw_bytes
            version = NORMALIZER_VERSIONS[target_host]
            record = _session_record(index, target_host, target_session)
            normalized_path = layout.normalized_path(target_host, target_session)
            rendered_path = layout.rendered_path(target_host, target_session)
            outdated = (
                not normalized_path.is_file()
                or record.get("version") != version
                or record.get("raw_fingerprint") != fingerprint
                or (
                    render
                    and (
                        not rendered_path.is_file()
                        or record.get("renderer_version") != RENDERER_VERSION
                    )
                )
            )
            if not outdated:
                report.skipped += 1
                continue
            report.outdated += 1
            if dry_run:
                continue

            staged_normalized = normalized_path.with_name(
                f".{normalized_path.name}.renormalize-{uuid.uuid4().hex}"
            )
            staged_rendered = (
                rendered_path.with_name(
                    f".{rendered_path.name}.renormalize-{uuid.uuid4().hex}"
                )
                if render
                else None
            )
            try:
                events, unknown, _ = importers[target_host].normalize_raw_dir(
                    raw_dir,
                    target_session,
                )
                event_count = write_events(staged_normalized, events)
                if staged_rendered is not None:
                    render_file(staged_normalized, staged_rendered)
                _publish_session(
                    staged_normalized=staged_normalized,
                    normalized_path=normalized_path,
                    staged_rendered=staged_rendered,
                    rendered_path=rendered_path if staged_rendered is not None else None,
                )
            except Exception as exc:
                _remove_file(staged_normalized)
                if staged_rendered is not None:
                    _remove_file(staged_rendered)
                report.failures.append(
                    RenormalizeFailure(
                        host=target_host,
                        session_id=target_session,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            normalized_size = normalized_path.stat().st_size
            report.rebuilt += 1
            report.rendered += int(staged_rendered is not None)
            report.event_count += event_count
            report.unknown_event_count += unknown
            report.normalized_bytes += normalized_size
            sessions = index.setdefault("sessions", {}).setdefault(target_host, {})
            sessions[target_session] = {
                "version": version,
                "raw_fingerprint": fingerprint,
                "event_count": event_count,
                "unknown_event_count": unknown,
                "normalized_bytes": normalized_size,
                "renderer_version": RENDERER_VERSION if render else None,
                "normalized_at": datetime.now(timezone.utc).isoformat(),
            }
            changed_index = True

        if changed_index:
            index["host_versions"] = dict(NORMALIZER_VERSIONS)
            _write_index(layout, index)
    report.elapsed_seconds = time.perf_counter() - started
    return report


def _normalizers(layout: Layout) -> dict[Host, Any]:
    disabled_source = layout.root / ".renormalize-source-disabled"
    return {
        "claude-code": ClaudeImporter(layout, claude_home=disabled_source),
        "codex": CodexImporter(layout, codex_home=disabled_source),
        "vscode-copilot": CopilotImporter(
            layout,
            user_data_dir=disabled_source,
            session_state_dir=disabled_source,
        ),
    }


def _targets(
    layout: Layout,
    *,
    host: Host | None,
    session_id: str | None,
    since: datetime | None,
):
    hosts = [host] if host is not None else sorted(NORMALIZER_VERSIONS)
    since_timestamp = since.timestamp() if since is not None else None
    for target_host in hosts:
        raw_host_dir = layout.root / "raw" / target_host
        if not raw_host_dir.is_dir():
            continue
        for raw_dir in sorted(path for path in raw_host_dir.iterdir() if path.is_dir()):
            if session_id is not None and raw_dir.name != session_id:
                continue
            validate_session_id(raw_dir.name)
            primary = _primary_paths(target_host, raw_dir)
            if not primary:
                continue
            if since_timestamp is not None:
                newest = max(path.stat().st_mtime for path in primary)
                if newest < since_timestamp:
                    continue
            yield target_host, raw_dir.name, raw_dir


def _primary_paths(host: Host, raw_dir: Path) -> list[Path]:
    return [
        raw_dir / name
        for name in _PRIMARY_RAW_FILES[host]
        if (raw_dir / name).is_file()
    ]


def _raw_fingerprint(host: Host, raw_dir: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for path in _primary_paths(host, raw_dir):
        stat = path.stat()
        total += stat.st_size
        digest.update(path.name.encode("utf-8"))
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
    return digest.hexdigest(), total


def _index_path(layout: Layout) -> Path:
    return layout.root / "normalized" / "normalizer-index.json"


def _load_index(layout: Layout) -> dict[str, Any]:
    path = _index_path(layout)
    if not path.is_file():
        return {
            "schema": INDEX_SCHEMA,
            "host_versions": {},
            "sessions": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "schema": INDEX_SCHEMA,
            "host_versions": {},
            "sessions": {},
        }
    if not isinstance(value, dict) or value.get("schema") != INDEX_SCHEMA:
        return {
            "schema": INDEX_SCHEMA,
            "host_versions": {},
            "sessions": {},
        }
    return value


def _session_record(
    index: dict[str, Any],
    host: Host,
    session_id: str,
) -> dict[str, Any]:
    sessions = index.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    host_sessions = sessions.get(host)
    if not isinstance(host_sessions, dict):
        return {}
    record = host_sessions.get(session_id)
    return record if isinstance(record, dict) else {}


def _write_index(layout: Layout, index: dict[str, Any]) -> None:
    atomic_write_text(
        _index_path(layout),
        json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _publish_session(
    *,
    staged_normalized: Path,
    normalized_path: Path,
    staged_rendered: Path | None,
    rendered_path: Path | None,
) -> None:
    suffix = uuid.uuid4().hex
    normalized_backup = normalized_path.with_name(
        f".{normalized_path.name}.backup-{suffix}"
    )
    rendered_backup = (
        rendered_path.with_name(f".{rendered_path.name}.backup-{suffix}")
        if rendered_path is not None
        else None
    )
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    if rendered_path is not None:
        rendered_path.parent.mkdir(parents=True, exist_ok=True)
    moved_normalized = False
    moved_rendered = False
    try:
        if normalized_path.exists():
            os.replace(normalized_path, normalized_backup)
        if rendered_path is not None and rendered_backup is not None and rendered_path.exists():
            os.replace(rendered_path, rendered_backup)
        os.replace(staged_normalized, normalized_path)
        moved_normalized = True
        if staged_rendered is not None and rendered_path is not None:
            os.replace(staged_rendered, rendered_path)
            moved_rendered = True
    except Exception:
        if moved_rendered and rendered_path is not None:
            _remove_file(rendered_path)
        if moved_normalized:
            _remove_file(normalized_path)
        if normalized_backup.exists():
            os.replace(normalized_backup, normalized_path)
        if (
            rendered_backup is not None
            and rendered_path is not None
            and rendered_backup.exists()
        ):
            os.replace(rendered_backup, rendered_path)
        raise
    else:
        _remove_file(normalized_backup)
        if rendered_backup is not None:
            _remove_file(rendered_backup)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
