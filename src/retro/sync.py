"""Incremental staged capture orchestration."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .archive_health import (
    ArchiveVolumeUnavailable,
    archive_access,
    describe_archive_error,
    is_external_volume_permission_error,
)
from .config import load_config, resolve_dashboard_dir, user_state_dir
from .extensions import (
    cleanup_stale_import_staging,
    create_source,
    source_host_names,
)
from .importers.claude import ClaudeSession
from .importers.codex import CodexThread
from .importers.copilot_cli import CopilotCliSession
from .importers.vscode_copilot import CopilotSession
from .locking import LockUnavailableError, archive_lock
from .memory_store import reindex as reindex_memory
from .renderer import render_file
from .schema import Host, validate_host_id, validate_session_id
from .sdk import SessionDescriptor
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
SYNC_STATE_SCHEMA = 2
STAGE_NAMES = ("capture", "catalog", "render", "signals", "memory", "dashboard")
DERIVED_STAGE_NAMES = STAGE_NAMES[1:]


@dataclass(frozen=True)
class CatalogUpdate:
    changed_keys: set[str]


@dataclass(frozen=True)
class CatalogWork:
    changed_keys: set[str]
    force: bool = False
    catalog: Any = None


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
    rendered: list[str] = field(default_factory=list)
    revisions: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    signals_recomputed: bool = False
    memory_reindexed: bool = False
    dashboard_rebuilt: bool = False
    derived_deferred: bool = False
    pending_sessions: list[str] = field(default_factory=list)
    stale_staging_removed: list[str] = field(default_factory=list)
    warning: str | None = None
    skip_reason: str | None = None

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
        started_at=_now(),
        scheduled=scheduled,
        archive_root=str(layout.root),
        dashboard_dir=str(output_dir),
    )
    access = archive_access(layout.root)
    if not access.mounted:
        report.status = "skipped"
        report.skip_reason = access.message
        report.warning = access.message
        report.completed_at = _now()
        _set_unrun_stages(report, "skipped", access.message or "archive unavailable")
        return report
    if not access.accessible:
        message = access.message or f"Archive is not accessible: {layout.root}"
        report.status = "failed"
        report.warning = message
        report.failures.append({"session": "*", "error": message})
        report.completed_at = _now()
        _set_unrun_stages(report, "failed", message)
        return report

    try:
        with archive_lock(layout):
            _run_sync_locked(
                layout,
                output_dir=output_dir,
                report=report,
                force_derived=force_derived,
            )
    except ArchiveVolumeUnavailable as exc:
        report.status = "skipped"
        report.skip_reason = str(exc)
        report.warning = str(exc)
        report.completed_at = _now()
        _set_unrun_stages(report, "skipped", str(exc))
    except LockUnavailableError:
        report.status = "locked"
        report.completed_at = _now()
        report.failures.append(
            {"session": "*", "error": "Another Retro archive operation is running"}
        )
    except Exception as exc:
        message = describe_archive_error(exc, layout.root)
        report.status = "failed"
        report.completed_at = _now()
        report.failures.append({"session": "*", "error": message})
        _try_write_run_report(layout, report)
    return report


def _run_sync_locked(
    layout: Layout,
    *,
    output_dir: Path,
    report: SyncReport,
    force_derived: bool,
) -> None:
    usage_path = Path(archive_access(layout.root).volume or layout.root.parent)
    report.free_bytes = shutil.disk_usage(usage_path).free
    if report.scheduled and report.free_bytes < MIN_SYNC_FREE_BYTES:
        raise RuntimeError(
            f"Scheduled sync requires 2 GiB free; only "
            f"{report.free_bytes / 1024**3:.1f} GiB remains"
        )
    if report.free_bytes < WARN_FREE_BYTES:
        report.warning = (
            f"Low disk space: {report.free_bytes / 1024**3:.1f} GiB free"
        )

    layout.ensure()
    state = _load_sync_state(layout)
    pending = _pending(state)
    signal_signature = _signal_signature()
    memory_signature = _memory_source_signature(layout)
    all_keys = _all_normalized_keys(layout)

    if force_derived:
        pending["catalog"]["keys"] = sorted(
            set(pending["catalog"]["keys"]) | all_keys
        )
        pending["render"]["all"] = True
        pending["signals"]["all"] = True
        pending["memory"]["needed"] = True
        pending["dashboard"]["needed"] = True
    if state.get("signal_signature") != signal_signature:
        pending["signals"]["all"] = True
        pending["dashboard"]["needed"] = True
    if state.get("memory_signature") != memory_signature:
        pending["memory"]["needed"] = True
    if not (output_dir / "index.html").exists():
        pending["dashboard"]["needed"] = True

    previous_stage_statuses = {
        name: _stage_status(state, name) for name in DERIVED_STAGE_NAMES
    }

    def capture_operation() -> tuple[set[str], dict[str, Any], str]:
        removed = cleanup_stale_import_staging(layout)
        report.stale_staging_removed.extend(str(path) for path in removed)
        failure_count = len(report.failures)
        changed = capture_sessions(layout, report)
        _add_pending_keys(pending["catalog"], changed)
        status = "partial" if len(report.failures) > failure_count else "success"
        return (
            changed,
            {
                "changed_keys": len(changed),
                "removed_staging_dirs": len(removed),
            },
            status,
        )

    _capture_ok, _changed = _call_stage(
        "capture",
        layout=layout,
        state=state,
        report=report,
        operation=capture_operation,
    )
    retry_failed = any(
        previous_stage_statuses[name] == "failed" and _stage_has_pending(pending, name)
        for name in DERIVED_STAGE_NAMES
    )
    derived_due = _derived_due(
        state,
        scheduled=report.scheduled,
        interval_seconds=load_config().derived_interval_seconds,
    )
    should_run_derived = (
        force_derived
        or not report.scheduled
        or derived_due
        or retry_failed
    )

    if should_run_derived:
        _run_derived_stages(
            layout,
            output_dir=output_dir,
            state=state,
            report=report,
            signal_signature=signal_signature,
            memory_signature=memory_signature,
        )
    else:
        report.derived_deferred = any(
            _stage_has_pending(pending, name) for name in DERIVED_STAGE_NAMES
        )
        for name in DERIVED_STAGE_NAMES:
            status = "deferred" if _stage_has_pending(pending, name) else "skipped"
            reason = (
                "scheduled derived interval has not elapsed"
                if status == "deferred"
                else "inputs unchanged"
            )
            _record_stage(
                name,
                state=state,
                report=report,
                status=status,
                started_at=_now(),
                duration_seconds=0.0,
                detail={"reason": reason},
            )
            _persist_sync_state(layout, state, report)

    report.pending_sessions = _pending_session_keys(pending)
    stage_failed = any(
        stage.get("status") == "failed" for stage in report.stages.values()
    )
    if stage_failed:
        report.status = "failed"
    elif report.failures:
        report.status = "partial"
    else:
        report.status = "success"
    report.completed_at = _now()
    _persist_sync_state(layout, state, report)
    _write_run_report(layout, report)


def capture_sessions(layout: Layout, report: SyncReport) -> set[str]:
    changed: set[str] = set()
    for host in source_host_names():
        try:
            source = create_source(host, layout)
            sessions = source.discover()
        except Exception as exc:
            if is_external_volume_permission_error(exc, layout.root):
                raise
            report.failures.append(
                {
                    "session": f"{host}/*",
                    "error": describe_archive_error(exc, layout.root),
                }
            )
            continue
        for session in sessions:
            session_id = _session_id(session)
            key = f"{source.host}/{session_id}"
            raw_dir = layout.raw_dir(source.host, session_id)
            try:
                revision = _preserve_rewritten_raw(
                    layout,
                    source.host,
                    session,
                    raw_dir,
                )
                if revision is not None:
                    report.revisions.append(str(revision))
                result = source.import_session(
                    identifier=session_id,
                    force=False,
                    acquire_lock=False,
                    descriptor=session,
                )
            except FileExistsError:
                report.unchanged.append(key)
                continue
            except Exception as exc:
                if is_external_volume_permission_error(exc, layout.root):
                    raise
                report.failures.append(
                    {
                        "session": key,
                        "error": describe_archive_error(exc, layout.root),
                    }
                )
                continue
            imported_key = f"{result.host}/{result.session_id}"
            changed.add(imported_key)
            report.imported.append(imported_key)
    return changed


def update_catalog(
    layout: Layout,
    *,
    keys: set[str],
    force: bool = False,
) -> CatalogUpdate:
    del layout, force
    return CatalogUpdate(changed_keys=set(keys))


def update_rendered(
    layout: Layout,
    *,
    keys: set[str],
    force: bool = False,
) -> set[str]:
    targets = _all_normalized_keys(layout) if force else set(keys)
    rendered: set[str] = set()
    for key in sorted(targets):
        parsed = _parse_key(key)
        if parsed is None:
            continue
        host, session_id = parsed
        normalized = layout.normalized_path(host, session_id)
        if not normalized.exists():
            continue
        render_file(normalized, layout.rendered_path(host, session_id))
        rendered.add(key)
    return rendered


def update_signals(layout: Layout, catalog: CatalogWork) -> None:
    readings_path = layout.root / "signals" / "readings.jsonl"
    full = catalog.force or not readings_path.exists()
    if full:
        readings = run_signals(layout)
        write_signal_artifacts(layout, readings)
        return
    pending = {
        parsed
        for key in catalog.changed_keys
        if (parsed := _parse_key(key)) is not None
    }
    if not pending:
        return
    existing = read_signal_readings(layout)
    replacements = []
    by_host: dict[Host, list[str]] = defaultdict(list)
    for host, session_id in pending:
        by_host[host].append(session_id)
    for host, session_ids in by_host.items():
        replacements.extend(
            run_signals(
                layout,
                host=host,
                session_ids=session_ids,
            )
        )
    merged = replace_session_readings(existing, replacements, pending)
    write_signal_artifacts(layout, merged)


def update_memory(layout: Layout) -> None:
    reindex_memory(layout)


def build_and_publish(layout: Layout, output_dir: Path) -> None:
    _rebuild_dashboard_atomically(layout, output_dir)


def _run_derived_stages(
    layout: Layout,
    *,
    output_dir: Path,
    state: dict[str, Any],
    report: SyncReport,
    signal_signature: str,
    memory_signature: str,
) -> None:
    pending = _pending(state)
    catalog_result: Any = None

    catalog_keys = set(pending["catalog"]["keys"])
    if catalog_keys:
        def catalog_operation() -> tuple[Any, dict[str, Any], str]:
            result = update_catalog(layout, keys=catalog_keys)
            changed_keys = _catalog_changed_keys(result, catalog_keys)
            pending["catalog"]["keys"] = []
            _add_pending_keys(pending["render"], changed_keys)
            _add_pending_keys(pending["signals"], changed_keys)
            if changed_keys:
                pending["dashboard"]["needed"] = True
            return result, {"changed_keys": len(changed_keys)}, "success"

        _catalog_ok, catalog_result = _call_stage(
            "catalog",
            layout=layout,
            state=state,
            report=report,
            operation=catalog_operation,
        )
    else:
        _skip_stage("catalog", layout=layout, state=state, report=report)

    render_keys = set(pending["render"]["keys"])
    render_all = bool(pending["render"]["all"])
    if render_keys or render_all:
        def render_operation() -> tuple[set[str], dict[str, Any], str]:
            rendered = update_rendered(
                layout,
                keys=render_keys,
                force=render_all,
            )
            pending["render"] = {"keys": [], "all": False}
            pending["dashboard"]["needed"] = True
            return rendered, {"rendered": len(rendered), "full": render_all}, "success"

        render_ok, rendered = _call_stage(
            "render",
            layout=layout,
            state=state,
            report=report,
            operation=render_operation,
        )
        if render_ok:
            report.rendered.extend(sorted(rendered))
    else:
        _skip_stage("render", layout=layout, state=state, report=report)

    signal_keys = set(pending["signals"]["keys"])
    signal_all = bool(pending["signals"]["all"])
    if signal_keys or signal_all:
        def signals_operation() -> tuple[None, dict[str, Any], str]:
            update_signals(
                layout,
                CatalogWork(
                    changed_keys=signal_keys,
                    force=signal_all,
                    catalog=catalog_result,
                ),
            )
            pending["signals"] = {"keys": [], "all": False}
            state["signal_signature"] = signal_signature
            pending["dashboard"]["needed"] = True
            return (
                None,
                {"changed_keys": len(signal_keys), "full": signal_all},
                "success",
            )

        signals_ok, _ = _call_stage(
            "signals",
            layout=layout,
            state=state,
            report=report,
            operation=signals_operation,
        )
        if signals_ok:
            report.signals_recomputed = True
    else:
        _skip_stage("signals", layout=layout, state=state, report=report)

    if pending["memory"]["needed"]:
        def memory_operation() -> tuple[None, dict[str, Any], str]:
            update_memory(layout)
            pending["memory"]["needed"] = False
            state["memory_signature"] = memory_signature
            pending["dashboard"]["needed"] = True
            return None, {"reindexed": True}, "success"

        memory_ok, _ = _call_stage(
            "memory",
            layout=layout,
            state=state,
            report=report,
            operation=memory_operation,
        )
        if memory_ok:
            report.memory_reindexed = True
    else:
        _skip_stage("memory", layout=layout, state=state, report=report)

    upstream_pending = any(
        _stage_has_pending(pending, name)
        for name in ("catalog", "render", "signals", "memory")
    )
    if pending["dashboard"]["needed"] and not upstream_pending:
        def dashboard_operation() -> tuple[None, dict[str, Any], str]:
            build_and_publish(layout, output_dir)
            pending["dashboard"]["needed"] = False
            state["last_derived_at"] = _now()
            return None, {"output_dir": str(output_dir)}, "success"

        dashboard_ok, _ = _call_stage(
            "dashboard",
            layout=layout,
            state=state,
            report=report,
            operation=dashboard_operation,
        )
        if dashboard_ok:
            report.dashboard_rebuilt = True
    elif pending["dashboard"]["needed"]:
        _skip_stage(
            "dashboard",
            layout=layout,
            state=state,
            report=report,
            status="blocked",
            reason="upstream stage has pending work",
        )
    else:
        _skip_stage("dashboard", layout=layout, state=state, report=report)


def _call_stage(
    name: str,
    *,
    layout: Layout,
    state: dict[str, Any],
    report: SyncReport,
    operation: Callable[[], tuple[Any, dict[str, Any], str]],
) -> tuple[bool, Any]:
    started_at = _now()
    started = time.perf_counter()
    try:
        result, detail, status = operation()
    except Exception as exc:
        message = describe_archive_error(exc, layout.root)
        report.failures.append({"session": f"stage:{name}", "error": message})
        _record_stage(
            name,
            state=state,
            report=report,
            status="failed",
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
            detail={"error": message},
        )
        _persist_sync_state(layout, state, report)
        return False, None
    _record_stage(
        name,
        state=state,
        report=report,
        status=status,
        started_at=started_at,
        duration_seconds=time.perf_counter() - started,
        detail=detail,
    )
    _persist_sync_state(layout, state, report)
    return status in {"success", "partial"}, result


def _skip_stage(
    name: str,
    *,
    layout: Layout,
    state: dict[str, Any],
    report: SyncReport,
    status: str = "skipped",
    reason: str = "inputs unchanged",
) -> None:
    _record_stage(
        name,
        state=state,
        report=report,
        status=status,
        started_at=_now(),
        duration_seconds=0.0,
        detail={"reason": reason},
    )
    _persist_sync_state(layout, state, report)


def _record_stage(
    name: str,
    *,
    state: dict[str, Any],
    report: SyncReport,
    status: str,
    started_at: str,
    duration_seconds: float,
    detail: dict[str, Any],
) -> None:
    completed_at = _now()
    record = {
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": round(duration_seconds, 6),
        **detail,
    }
    previous = state.setdefault("stages", {}).get(name)
    if isinstance(previous, dict) and isinstance(previous.get("last_success_at"), str):
        record["last_success_at"] = previous["last_success_at"]
    if status == "success":
        record["last_success_at"] = completed_at
    state["stages"][name] = record
    report.stages[name] = dict(record)


def _load_sync_state(layout: Layout) -> dict[str, Any]:
    state_path = layout.sync_state_path()
    state = _read_json(state_path)
    if state:
        return _normalize_state(state)
    legacy_path = user_state_dir() / "last-sync.json"
    legacy = _read_json(legacy_path)
    state = _empty_state()
    if not legacy:
        return state
    state["migrated_from"] = str(legacy_path)
    state["migrated_at"] = _now()
    state["signal_signature"] = legacy.get("signal_signature")
    state["memory_signature"] = legacy.get("memory_signature")
    state["last_derived_at"] = legacy.get("last_derived_at")
    if not state["last_derived_at"] and legacy.get("dashboard_rebuilt"):
        state["last_derived_at"] = legacy.get("completed_at")
    keys = {
        value
        for value in legacy.get("pending_sessions", [])
        if isinstance(value, str) and _parse_key(value) is not None
    }
    for name in ("catalog", "render", "signals"):
        state["pending"][name]["keys"] = sorted(keys)
    state["pending"]["dashboard"]["needed"] = bool(keys)
    return state


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SYNC_STATE_SCHEMA,
        "stages": {},
        "pending": {
            "catalog": {"keys": []},
            "render": {"keys": [], "all": False},
            "signals": {"keys": [], "all": False},
            "memory": {"needed": False},
            "dashboard": {"needed": False},
        },
        "signal_signature": None,
        "memory_signature": None,
        "last_derived_at": None,
    }


def _normalize_state(value: dict[str, Any]) -> dict[str, Any]:
    state = _empty_state()
    state.update(value)
    state["schema_version"] = SYNC_STATE_SCHEMA
    normalized_pending = _empty_state()["pending"]
    raw_pending = value.get("pending")
    if isinstance(raw_pending, dict):
        for name, default in normalized_pending.items():
            candidate = raw_pending.get(name)
            if isinstance(candidate, dict):
                default.update(candidate)
    state["pending"] = normalized_pending
    if not isinstance(state.get("stages"), dict):
        state["stages"] = {}
    _pending(state)
    return state


def _pending(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    pending = state.setdefault("pending", _empty_state()["pending"])
    for name in ("catalog", "render", "signals"):
        record = pending.setdefault(name, {"keys": []})
        keys = record.get("keys")
        record["keys"] = sorted(
            {
                key
                for key in keys if isinstance(key, str) and _parse_key(key) is not None
            }
            if isinstance(keys, list)
            else set()
        )
    pending["render"]["all"] = bool(pending["render"].get("all"))
    pending["signals"]["all"] = bool(pending["signals"].get("all"))
    pending.setdefault("memory", {})["needed"] = bool(
        pending.get("memory", {}).get("needed")
    )
    pending.setdefault("dashboard", {})["needed"] = bool(
        pending.get("dashboard", {}).get("needed")
    )
    return pending


def _persist_sync_state(
    layout: Layout,
    state: dict[str, Any],
    report: SyncReport,
) -> None:
    state["schema_version"] = SYNC_STATE_SCHEMA
    state["updated_at"] = _now()
    state["last_run"] = {
        "started_at": report.started_at,
        "completed_at": report.completed_at,
        "status": report.status,
        "scheduled": report.scheduled,
    }
    atomic_write_text(
        layout.sync_state_path(),
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


def _add_pending_keys(record: dict[str, Any], keys: set[str]) -> None:
    record["keys"] = sorted(set(record.get("keys", [])) | set(keys))


def _stage_has_pending(
    pending: dict[str, dict[str, Any]],
    name: str,
) -> bool:
    if name in {"catalog", "render", "signals"}:
        return bool(pending[name].get("keys")) or bool(pending[name].get("all"))
    return bool(pending[name].get("needed"))


def _pending_session_keys(pending: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        set(pending["catalog"]["keys"])
        | set(pending["render"]["keys"])
        | set(pending["signals"]["keys"])
    )


def _catalog_changed_keys(result: Any, fallback: set[str]) -> set[str]:
    changed = getattr(result, "changed_keys", None)
    if changed is None and isinstance(result, (set, list, tuple)):
        changed = result
    if changed is None:
        return set(fallback)
    return {
        key
        for key in changed
        if isinstance(key, str) and _parse_key(key) is not None
    }


def _all_normalized_keys(layout: Layout) -> set[str]:
    return {
        f"{host}/{session_id}"
        for host in layout.list_normalized_hosts()
        for session_id in layout.list_normalized(host)
    }


def _parse_key(value: str) -> tuple[Host, str] | None:
    if "/" not in value:
        return None
    raw_host, raw_session_id = value.split("/", 1)
    try:
        return validate_host_id(raw_host), validate_session_id(raw_session_id)
    except ValueError:
        return None


def _stage_status(state: dict[str, Any], name: str) -> str | None:
    record = state.get("stages", {}).get(name)
    return record.get("status") if isinstance(record, dict) else None


def _set_unrun_stages(report: SyncReport, status: str, reason: str) -> None:
    now = _now()
    for name in STAGE_NAMES:
        report.stages[name] = {
            "status": status,
            "started_at": now,
            "completed_at": now,
            "duration_seconds": 0.0,
            "reason": reason,
        }


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
    if isinstance(session, SessionDescriptor):
        raw_filename = session.metadata.get("raw_filename")
        if session.source_path is not None and isinstance(raw_filename, str):
            return session.source_path, raw_dir / raw_filename
        return None
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


def _derived_due(
    previous_state: dict[str, Any],
    *,
    scheduled: bool,
    interval_seconds: int,
) -> bool:
    if not scheduled:
        return True
    raw = previous_state.get("last_derived_at")
    if not isinstance(raw, str):
        return True
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval_seconds


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


def _try_write_run_report(layout: Layout, report: SyncReport) -> None:
    try:
        _write_run_report(layout, report)
    except OSError:
        pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
