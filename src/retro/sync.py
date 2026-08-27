"""Incremental periodic capture orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_dashboard_dir, user_state_dir
from .importers.claude import ClaudeImporter, ClaudeSession
from .importers.codex import CodexImporter, CodexThread
from .importers.copilot import CopilotImporter
from .importers.copilot_cli import CopilotCliSession
from .importers.vscode_copilot import CopilotSession
from .locking import LockUnavailableError, exclusive_lock
from .memory_store import reindex as reindex_memory
from .renderer import render_file
from .schema import Host
from .signals import REGISTRY as SIGNAL_REGISTRY
from .signals import (
    read_signal_readings,
    replace_session_readings,
    run_signals,
    write_signal_artifacts,
)
from .storage import Layout
from .utils import atomic_write_text

WARN_FREE_BYTES = 5 * 1024**3
MIN_SYNC_FREE_BYTES = 2 * 1024**3


@dataclass
class SyncReport:
    started_at: str
    completed_at: str | None = None
    status: str = "running"
    scheduled: bool = False
    archive_root: str = ""
    dashboard_dir: str = ""
    free_bytes: int = 0
    imported: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    revisions: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    signals_recomputed: bool = False
    memory_reindexed: bool = False
    dashboard_rebuilt: bool = False
    warning: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_sync(
    layout: Layout,
    *,
    dashboard_dir: Path | None = None,
    scheduled: bool = False,
    force_derived: bool = False,
) -> SyncReport:
    output_dir = resolve_dashboard_dir(dashboard_dir)
    report = SyncReport(
        started_at=datetime.now(timezone.utc).isoformat(),
        scheduled=scheduled,
        archive_root=str(layout.root),
        dashboard_dir=str(output_dir),
    )
    state_path = user_state_dir() / "last-sync.json"
    previous_state = _read_json(state_path)

    try:
        with exclusive_lock(user_state_dir() / "archive.lock"):
            layout.root.parent.mkdir(parents=True, exist_ok=True)
            report.free_bytes = shutil.disk_usage(layout.root.parent).free
            if scheduled and report.free_bytes < MIN_SYNC_FREE_BYTES:
                raise RuntimeError(
                    f"Scheduled sync requires 2 GiB free; only "
                    f"{report.free_bytes / 1024**3:.1f} GiB remains"
                )
            if report.free_bytes < WARN_FREE_BYTES:
                report.warning = (
                    f"Low disk space: {report.free_bytes / 1024**3:.1f} GiB free"
                )

            layout.ensure()
            importers: list[Any] = [
                ClaudeImporter(layout),
                CodexImporter(layout),
                CopilotImporter(layout),
            ]
            changed: set[tuple[Host, str]] = set()
            for importer in importers:
                for session in importer.discover():
                    session_id = _session_id(session)
                    key = f"{importer.host}/{session_id}"
                    raw_dir = layout.raw_dir(importer.host, session_id)
                    try:
                        revision = _preserve_rewritten_raw(
                            layout,
                            importer.host,
                            session,
                            raw_dir,
                        )
                        if revision is not None:
                            report.revisions.append(str(revision))
                        result = importer.import_session(
                            identifier=session_id,
                            force=False,
                        )
                    except FileExistsError:
                        report.unchanged.append(key)
                        continue
                    except Exception as exc:
                        report.failures.append(
                            {
                                "session": key,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        continue
                    changed.add((importer.host, result.session_id))
                    report.imported.append(key)
                    render_file(
                        result.normalized_path,
                        layout.rendered_path(importer.host, result.session_id),
                    )

            signal_signature = _signal_signature()
            full_signal_rebuild = (
                force_derived
                or not (layout.root / "signals" / "readings.jsonl").exists()
                or previous_state.get("signal_signature") != signal_signature
            )
            if full_signal_rebuild:
                readings = run_signals(layout)
                write_signal_artifacts(layout, readings)
                report.signals_recomputed = True
            elif changed:
                existing = read_signal_readings(layout)
                replacements = []
                by_host: dict[Host, list[str]] = defaultdict(list)
                for host, session_id in changed:
                    by_host[host].append(session_id)
                for host, session_ids in by_host.items():
                    replacements.extend(
                        run_signals(
                            layout,
                            host=host,
                            session_ids=session_ids,
                        )
                    )
                merged = replace_session_readings(existing, replacements, changed)
                write_signal_artifacts(layout, merged)
                report.signals_recomputed = True

            memory_signature = _memory_source_signature(layout)
            if (
                force_derived
                or not layout.memory_index_path().exists()
                or previous_state.get("memory_signature") != memory_signature
            ):
                reindex_memory(layout)
                report.memory_reindexed = True

            dashboard_missing = not (output_dir / "index.html").exists()
            if (
                force_derived
                or changed
                or dashboard_missing
                or report.memory_reindexed
                or report.signals_recomputed
            ):
                _rebuild_dashboard_atomically(layout, output_dir)
                report.dashboard_rebuilt = True

            report.status = "partial" if report.failures else "success"
            report.completed_at = datetime.now(timezone.utc).isoformat()
            state = {
                **report.to_dict(),
                "signal_signature": signal_signature,
                "memory_signature": memory_signature,
            }
            atomic_write_text(
                state_path,
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            )
            _write_run_report(layout, report)
    except LockUnavailableError:
        report.status = "locked"
        report.completed_at = datetime.now(timezone.utc).isoformat()
        report.failures.append(
            {"session": "*", "error": "Another Retro archive operation is running"}
        )
        _write_run_report(layout, report)
    except Exception as exc:
        report.status = "failed"
        report.completed_at = datetime.now(timezone.utc).isoformat()
        report.failures.append(
            {"session": "*", "error": f"{type(exc).__name__}: {exc}"}
        )
        _write_run_report(layout, report)
    return report


def _session_id(session: Any) -> str:
    for name in ("session_id", "thread_id"):
        value = getattr(session, name, None)
        if isinstance(value, str):
            return value
    raise ValueError(f"Unsupported session descriptor: {type(session).__name__}")


def _preserve_rewritten_raw(
    layout: Layout,
    host: Host,
    session: Any,
    raw_dir: Path,
) -> Path | None:
    pair = _primary_source_pair(session, raw_dir)
    if pair is None:
        return None
    source, captured = pair
    if not source.is_file() or not captured.is_file():
        return None
    source_stat = source.stat()
    captured_stat = captured.stat()
    if (
        source_stat.st_mtime_ns <= captured_stat.st_mtime_ns
        and source_stat.st_size <= captured_stat.st_size
    ):
        return None
    if _is_prefix(captured, source):
        return None
    revision_hash = _sha256(captured)
    revision = layout.root / "raw-revisions" / host / _session_id(session) / revision_hash
    if revision.exists():
        marker = revision / ".complete"
        if marker.is_file():
            return revision
        shutil.rmtree(revision)
    revision.parent.mkdir(parents=True, exist_ok=True)
    temporary = revision.with_name(f".{revision.name}.tmp")
    if temporary.exists():
        shutil.rmtree(temporary)
    shutil.copytree(raw_dir, temporary)
    (temporary / ".complete").write_text(revision_hash + "\n", encoding="utf-8")
    os.replace(temporary, revision)
    return revision


def _primary_source_pair(session: Any, raw_dir: Path) -> tuple[Path, Path] | None:
    if isinstance(session, ClaudeSession):
        return session.transcript_path, raw_dir / "transcript.jsonl"
    if isinstance(session, CodexThread):
        return session.rollout_path, raw_dir / "rollout.jsonl"
    if isinstance(session, CopilotCliSession):
        return session.events_path, raw_dir / "events.jsonl"
    if isinstance(session, CopilotSession):
        return session.session_path, raw_dir / f"session.{session.core_format}"
    return None


def _is_prefix(prefix: Path, candidate: Path) -> bool:
    if prefix.stat().st_size > candidate.stat().st_size:
        return False
    with prefix.open("rb") as old, candidate.open("rb") as new:
        while True:
            chunk = old.read(1024 * 1024)
            if not chunk:
                return True
            if new.read(len(chunk)) != chunk:
                return False


def _signal_signature() -> str:
    payload = [
        {
            "name": signal.name,
            "group": signal.group,
            "kind": signal.kind,
            "method": signal.method,
            "unit": signal.unit,
            "description": signal.description,
        }
        for signal in SIGNAL_REGISTRY.values()
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _memory_source_signature(layout: Layout) -> str:
    paths = [
        layout.memory_items_path(),
        layout.memory_events_path(),
        *(layout.root / "mined").glob("*/*/*.json"),
    ]
    rows = []
    for path in sorted(path for path in paths if path.is_file()):
        stat = path.stat()
        rows.append((str(path.relative_to(layout.root)), stat.st_size, stat.st_mtime_ns))
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _rebuild_dashboard_atomically(layout: Layout, output_dir: Path) -> None:
    from .dashboard_publish import build_and_publish_dashboard

    build_and_publish_dashboard(
        artifact_root=layout.root,
        output_dir=output_dir,
    )


def _write_run_report(layout: Layout, report: SyncReport) -> None:
    timestamp = report.started_at.replace(":", "").replace("-", "")
    path = layout.root / "sync" / "runs" / f"{timestamp}.json"
    atomic_write_text(
        path,
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    runs = sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime)
    for old in runs[:-30]:
        old.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
