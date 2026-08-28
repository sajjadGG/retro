"""Checksummed, resumable archive migration and verification."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import user_data_dir, user_state_dir
from .locking import exclusive_lock
from .memory_store import reindex as reindex_memory
from .renderer import render_file
from .schema import HOSTS
from .signals import run_signals, write_signal_artifacts
from .storage import Layout
from .utils import atomic_write_text, resolve_artifact_ref

MIGRATION_SCHEMA_VERSION = 1
MIN_MIGRATION_FREE_BYTES = 4 * 1024**3
_DERIVED_PREFIXES = {"signals"}
_CACHE_NAMES = {
    ".DS_Store",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}


def create_migration_plan(
    sources: list[Path],
    destination: Path,
    *,
    experiment_roots: list[Path] | None = None,
    dashboard_dir: Path | None = None,
    migration_id: str | None = None,
) -> Path:
    resolved_sources = _unique_paths(sources)
    if not resolved_sources:
        raise ValueError("At least one source archive is required")
    destination = destination.expanduser().resolve()
    for source in resolved_sources:
        if not source.is_dir():
            raise FileNotFoundError(f"Source archive does not exist: {source}")
        if source == destination:
            raise ValueError(f"Source and destination are the same: {source}")

    migration_id = migration_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    migration_dir = destination / "migrations" / migration_id
    plan_path = migration_dir / "plan.json"
    if plan_path.exists():
        raise FileExistsError(f"Migration plan already exists: {plan_path}")
    migration_dir.mkdir(parents=True, exist_ok=False)

    source_labels = _source_labels(resolved_sources)
    entries: list[dict[str, Any]] = []
    target_entries: dict[str, dict[str, Any]] = {}
    inventories: list[dict[str, Any]] = []

    for source in resolved_sources:
        label = source_labels[source]
        inventory = _inventory_archive(source)
        inventory["label"] = label
        inventories.append(inventory)
        for path in _iter_source_files(source):
            relative = path.relative_to(source)
            action = _archive_action(relative)
            target = _target_for_action(
                destination,
                migration_dir,
                label,
                relative,
                action,
            )
            entry = _manifest_entry(
                source=path,
                source_root=source,
                source_label=label,
                relative=relative,
                target=target,
                action=action,
                category=relative.parts[0] if relative.parts else "root",
            )
            if action in {"copy", "rewrite-normalized"}:
                key = str(target)
                previous = target_entries.get(key)
                if previous is not None:
                    if previous["source_sha256"] == entry["source_sha256"]:
                        entry["action"] = "skip-identical-source"
                        entry["status"] = "skipped"
                    else:
                        entry["action"] = "conflict"
                        entry["status"] = "blocked"
                else:
                    target_entries[key] = entry
            entries.append(entry)

    for experiment_root in _unique_paths(experiment_roots or []):
        if not experiment_root.is_dir():
            raise FileNotFoundError(f"Experiment source does not exist: {experiment_root}")
        label = _safe_label(experiment_root.name)
        for path in _iter_experiment_files(experiment_root):
            relative = path.relative_to(experiment_root)
            target = destination / "experiments" / label / relative
            entry = _manifest_entry(
                source=path,
                source_root=experiment_root,
                source_label=label,
                relative=relative,
                target=target,
                action="copy",
                category="experiments",
            )
            key = str(target)
            previous = target_entries.get(key)
            if previous is not None:
                if previous["source_sha256"] == entry["source_sha256"]:
                    entry["action"] = "skip-identical-source"
                    entry["status"] = "skipped"
                else:
                    entry["action"] = "conflict"
                    entry["status"] = "blocked"
            else:
                target_entries[key] = entry
            entries.append(entry)

    plan = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "planned",
        "destination": str(destination),
        "dashboard_dir": str(dashboard_dir.expanduser().resolve()) if dashboard_dir else None,
        "sources": [str(path) for path in resolved_sources],
        "experiment_roots": [
            str(path) for path in _unique_paths(experiment_roots or [])
        ],
        "inventories": inventories,
        "entries": entries,
        "summary": _entry_summary(entries),
    }
    atomic_write_text(plan_path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    return plan_path


def execute_migration(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    plan = _load_plan(plan_path)
    destination = Path(plan["destination"])
    free = shutil.disk_usage(destination.parent).free
    if free < MIN_MIGRATION_FREE_BYTES:
        raise RuntimeError(
            f"Migration requires at least 4 GiB free; only {free / 1024**3:.1f} GiB available"
        )
    blocked = [entry for entry in plan["entries"] if entry["action"] == "conflict"]
    if blocked:
        raise RuntimeError(
            f"Migration plan has {len(blocked)} conflicting targets; resolve them first"
        )

    lock_path = user_state_dir() / "archive.lock"
    with exclusive_lock(lock_path):
        plan.pop("verification", None)
        plan.pop("derived_rebuild", None)
        plan["status"] = "running"
        plan["started_at"] = datetime.now(timezone.utc).isoformat()
        _write_plan(plan_path, plan)
        for index, entry in enumerate(plan["entries"], start=1):
            action = entry["action"]
            if entry.get("status") in {"copied", "rewritten", "verified", "skipped"}:
                continue
            try:
                if action in {"skip-derived", "skip-identical-source"}:
                    entry["status"] = "skipped"
                elif action == "backup-only":
                    _copy_entry(entry, conflict_dir=plan_path.parent / "conflicts")
                    entry["status"] = "copied"
                elif action == "copy":
                    _copy_entry(entry, conflict_dir=plan_path.parent / "conflicts")
                    entry["status"] = "copied"
                elif action == "rewrite-normalized":
                    _rewrite_normalized_entry(
                        entry,
                        sources=[Path(path) for path in plan["sources"]],
                        destination=destination,
                        conflict_dir=plan_path.parent / "conflicts",
                    )
                    entry["status"] = "rewritten"
                else:
                    raise ValueError(f"Unknown migration action {action!r}")
            except Exception as exc:
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                plan["status"] = "failed"
                plan["summary"] = _entry_summary(plan["entries"])
                _write_plan(plan_path, plan)
                raise
            if index % 50 == 0:
                plan["summary"] = _entry_summary(plan["entries"])
                _write_plan(plan_path, plan)

        plan["status"] = "copied"
        plan["copied_at"] = datetime.now(timezone.utc).isoformat()
        plan["summary"] = _entry_summary(plan["entries"])
        _write_plan(plan_path, plan)
        return plan


def rebuild_derived_artifacts(
    plan_path: Path,
    *,
    dashboard_dir: Path | None = None,
) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    plan = _load_plan(plan_path)
    layout = Layout(Path(plan["destination"]))
    output_dir = (
        dashboard_dir.expanduser().resolve()
        if dashboard_dir is not None
        else Path(plan["dashboard_dir"])
        if plan.get("dashboard_dir")
        else user_data_dir() / "dashboard"
    )
    migration_dir = plan_path.parent

    with exclusive_lock(user_state_dir() / "archive.lock"):
        plan.pop("verification", None)
        plan["status"] = "rebuilding"
        _write_plan(plan_path, plan)
        render_count = 0
        for host in HOSTS:
            for session_id in layout.list_normalized(host):
                render_file(
                    layout.normalized_path(host, session_id),
                    layout.rendered_path(host, session_id),
                )
                render_count += 1

        memory_report = reindex_memory(layout)
        readings = run_signals(layout)
        signal_paths = write_signal_artifacts(layout, readings)

        from .dashboard_publish import build_and_publish_dashboard

        index_path = build_and_publish_dashboard(
            artifact_root=layout.root,
            output_dir=output_dir,
            backup_dir=migration_dir / "backups" / "dashboard",
        )

        report = {
            "rendered_sessions": render_count,
            "memory": {
                "indexed": memory_report.indexed,
                "source_records": memory_report.source_records,
                "mined_records": memory_report.mined_records,
                "evidence_refs": memory_report.evidence_refs,
                "links": memory_report.links,
            },
            "signal_readings": len(readings),
            "signal_paths": {key: str(value) for key, value in signal_paths.items()},
            "dashboard": str(output_dir / index_path.name),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_text(
            migration_dir / "derived-rebuild.json",
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        )
        plan["derived_rebuild"] = report
        plan["status"] = "rebuilt"
        _write_plan(plan_path, plan)
        return report


def verify_migration(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.expanduser().resolve()
    with exclusive_lock(user_state_dir() / "archive.lock"):
        return _verify_migration_locked(plan_path)


def _verify_migration_locked(plan_path: Path) -> dict[str, Any]:
    plan = _load_plan(plan_path)
    destination = Path(plan["destination"])
    errors: list[str] = []
    checked_files = 0

    for entry in plan["entries"]:
        if entry["action"] not in {"copy", "backup-only", "rewrite-normalized"}:
            continue
        target = Path(entry["target"])
        if not target.is_file():
            errors.append(f"missing target: {target}")
            continue
        checked_files += 1
        target_hash = sha256_file(target)
        expected = entry.get("target_sha256")
        if expected and target_hash != expected:
            errors.append(f"checksum mismatch: {target}")

    session_counts: Counter[str] = Counter()
    event_count = 0
    invalid_jsonl = 0
    unresolved_refs = 0
    for path in sorted((destination / "normalized").glob("*/*.events.jsonl")):
        host = path.parent.name
        session_id = path.name[: -len(".events.jsonl")]
        session_counts[host] += 1
        rendered = destination / "rendered" / host / f"{session_id}.md"
        if not rendered.is_file():
            errors.append(f"missing rendered transcript: {rendered}")
        previous_sequence = -1
        with path.open("r", encoding="utf-8") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                event_count += 1
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    invalid_jsonl += 1
                    continue
                if event.get("host") != host or event.get("session_id") != session_id:
                    errors.append(f"event identity mismatch: {path}:{line_no}")
                sequence = event.get("sequence")
                if isinstance(sequence, int) and sequence < previous_sequence:
                    errors.append(f"non-monotonic sequence: {path}:{line_no}")
                if isinstance(sequence, int):
                    previous_sequence = sequence
                raw_ref = event.get("raw_ref")
                reference = raw_ref.get("path") if isinstance(raw_ref, dict) else None
                if not isinstance(reference, str):
                    unresolved_refs += 1
                else:
                    resolved = resolve_artifact_ref(reference, destination)
                    if not resolved.is_file():
                        unresolved_refs += 1

    raw_session_counts: Counter[str] = Counter()
    raw_root = destination / "raw"
    if raw_root.exists():
        for host_dir in raw_root.iterdir():
            if not host_dir.is_dir():
                continue
            raw_session_counts[host_dir.name] = sum(
                1 for path in host_dir.iterdir() if path.is_dir()
            )

    memory = _memory_counts(destination / "memories" / "index.sqlite")
    report = {
        "checked_files": checked_files,
        "session_counts": dict(session_counts),
        "raw_session_counts": dict(raw_session_counts),
        "normalized_events": event_count,
        "invalid_jsonl": invalid_jsonl,
        "unresolved_raw_refs": unresolved_refs,
        "memory": memory,
        "errors": errors,
        "ok": not errors and invalid_jsonl == 0 and unresolved_refs == 0,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_text(
        plan_path.parent / "verification.json",
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
    )
    plan["verification"] = report
    plan["status"] = "verified" if report["ok"] else "verification-failed"
    _write_plan(plan_path, plan)
    return report


def cutover_compatibility_link(plan_path: Path, link_path: Path) -> Path:
    plan_path = plan_path.expanduser().resolve()
    plan = _load_plan(plan_path)
    verification = plan.get("verification")
    rebuild = plan.get("derived_rebuild")
    if (
        plan.get("status") != "verified"
        or not isinstance(verification, dict)
        or not verification.get("ok")
        or not isinstance(rebuild, dict)
    ):
        raise RuntimeError("Migration must pass verification before cutover")
    if str(verification.get("verified_at", "")) <= str(rebuild.get("completed_at", "")):
        raise RuntimeError("Migration must be verified after the derived rebuild")

    destination = Path(plan["destination"]).resolve()
    link_path = link_path.expanduser().absolute()
    if link_path.is_symlink():
        if link_path.resolve() == destination:
            return link_path
        raise FileExistsError(f"Compatibility path links elsewhere: {link_path}")
    if not link_path.is_dir():
        raise FileNotFoundError(f"Compatibility source directory is missing: {link_path}")

    backup = (
        user_data_dir()
        / "source-backups"
        / str(plan["migration_id"])
        / f"{link_path.name}.pre-migration"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise FileExistsError(f"Cutover backup already exists: {backup}")
    if link_path.stat().st_dev != backup.parent.stat().st_dev:
        raise RuntimeError("Compatibility cutover requires source and backup on one filesystem")

    with exclusive_lock(user_state_dir() / "archive.lock"):
        os.replace(link_path, backup)
        try:
            link_path.symlink_to(destination, target_is_directory=True)
        except Exception:
            os.replace(backup, link_path)
            raise
        plan["cutover"] = {
            "link_path": str(link_path),
            "target": str(destination),
            "source_backup": str(backup),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        plan["status"] = "complete"
        _write_plan(plan_path, plan)
    return link_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inventory_archive(root: Path) -> dict[str, Any]:
    files = [path for path in root.rglob("*") if path.is_file()]
    raw_sessions: Counter[str] = Counter()
    normalized_sessions: Counter[str] = Counter()
    for host_dir in (root / "raw").iterdir() if (root / "raw").is_dir() else []:
        if host_dir.is_dir():
            raw_sessions[host_dir.name] = sum(
                1 for path in host_dir.iterdir() if path.is_dir()
            )
    for path in (root / "normalized").glob("*/*.events.jsonl"):
        normalized_sessions[path.parent.name] += 1
    return {
        "root": str(root),
        "files": len(files),
        "bytes": sum(path.stat().st_size for path in files),
        "raw_sessions": dict(raw_sessions),
        "normalized_sessions": dict(normalized_sessions),
    }


def _iter_source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _CACHE_NAMES for part in relative.parts):
            continue
        if relative.parts and relative.parts[0] == "migrations":
            continue
        yield path


def _iter_experiment_files(root: Path):
    logs = root / "logs"
    if logs.is_dir():
        for path in sorted(logs.rglob("*")):
            if path.is_file() and not any(part in _CACHE_NAMES for part in path.parts):
                yield path
    for path in sorted(root.glob("*.json")):
        if path.is_file():
            yield path


def _archive_action(relative: Path) -> str:
    if not relative.parts:
        return "copy"
    if relative.parts[0] in _DERIVED_PREFIXES:
        return "skip-derived"
    if relative.parts[0] == "rendered":
        return "backup-only"
    if relative.parts[0] == "memories" and relative.name.startswith("index.sqlite"):
        return "backup-only"
    if (
        relative.parts[0] == "normalized"
        and relative.name.endswith(".events.jsonl")
    ):
        return "rewrite-normalized"
    return "copy"


def _target_for_action(
    destination: Path,
    migration_dir: Path,
    label: str,
    relative: Path,
    action: str,
) -> Path:
    if action in {"backup-only", "skip-derived"}:
        return migration_dir / "backups" / label / relative
    return destination / relative


def _manifest_entry(
    *,
    source: Path,
    source_root: Path,
    source_label: str,
    relative: Path,
    target: Path,
    action: str,
    category: str,
) -> dict[str, Any]:
    stat = source.stat()
    return {
        "source": str(source),
        "source_root": str(source_root),
        "source_label": source_label,
        "relative_path": relative.as_posix(),
        "target": str(target),
        "category": category,
        "action": action,
        "status": "planned",
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "source_sha256": sha256_file(source),
    }


def _copy_entry(entry: dict[str, Any], *, conflict_dir: Path) -> None:
    source = Path(entry["source"])
    target = Path(entry["target"])
    expected = str(entry["source_sha256"])
    if target.exists():
        if target.is_file() and sha256_file(target) == expected:
            entry["target_sha256"] = expected
            return
        _quarantine_conflict(source, entry, conflict_dir)
        raise RuntimeError(f"Refusing to overwrite conflicting target: {target}")
    if entry["action"] == "backup-only" and source.name == "index.sqlite":
        _backup_sqlite(source, target)
        actual = sha256_file(target)
        entry["target_sha256"] = actual
        entry["backup_method"] = "sqlite-online-backup"
        return
    _atomic_copy(source, target)
    actual = sha256_file(target)
    if actual != expected:
        target.unlink()
        raise RuntimeError(f"Checksum verification failed after copying {source}")
    entry["target_sha256"] = actual


def _rewrite_normalized_entry(
    entry: dict[str, Any],
    *,
    sources: list[Path],
    destination: Path,
    conflict_dir: Path,
) -> None:
    source = Path(entry["source"])
    target = Path(entry["target"])
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with source.open("r", encoding="utf-8") as src, os.fdopen(
            fd, "w", encoding="utf-8"
        ) as dst:
            for line_no, raw_line in enumerate(src, start=1):
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid normalized JSONL at {source}:{line_no}") from exc
                raw_ref = event.get("raw_ref")
                if not isinstance(raw_ref, dict) or not isinstance(raw_ref.get("path"), str):
                    raise ValueError(f"Missing raw_ref.path at {source}:{line_no}")
                raw_ref["path"] = _portable_reference(
                    raw_ref["path"],
                    sources=sources,
                    destination=destination,
                )
                dst.write(json.dumps(event, ensure_ascii=False))
                dst.write("\n")
            dst.flush()
            os.fsync(dst.fileno())
        rewritten_hash = sha256_file(temporary)
        if target.exists():
            if target.is_file() and sha256_file(target) == rewritten_hash:
                entry["target_sha256"] = rewritten_hash
                return
            _quarantine_conflict(source, entry, conflict_dir)
            raise RuntimeError(f"Refusing to overwrite conflicting target: {target}")
        os.replace(temporary, target)
        entry["target_sha256"] = rewritten_hash
    finally:
        if temporary.exists():
            temporary.unlink()


def _portable_reference(reference: str, *, sources: list[Path], destination: Path) -> str:
    path = Path(reference)
    if not path.is_absolute():
        return path.as_posix()
    for source in sources:
        try:
            relative = path.resolve().relative_to(source.resolve())
        except ValueError:
            continue
        return relative.as_posix()
    try:
        return path.resolve().relative_to(destination.resolve()).as_posix()
    except ValueError:
        return reference


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _quarantine_conflict(
    source: Path,
    entry: dict[str, Any],
    conflict_dir: Path,
) -> None:
    target = conflict_dir / str(entry["source_label"]) / str(entry["relative_path"])
    if not target.exists():
        _atomic_copy(source, target)
    entry["conflict_copy"] = str(target)
    entry["status"] = "blocked"


def _backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.backup")
    if temporary.exists():
        temporary.unlink()
    source_con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_con = sqlite3.connect(temporary)
    try:
        source_con.backup(target_con)
    finally:
        target_con.close()
        source_con.close()
    os.replace(temporary, target)


def _memory_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"available": False}
    con = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    try:
        quick_check = con.execute("PRAGMA quick_check").fetchone()[0]
        memory = con.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        evidence = con.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()[0]
        events = con.execute("SELECT COUNT(*) FROM memory_event").fetchone()[0]
        return {
            "available": True,
            "quick_check": quick_check,
            "memory": memory,
            "evidence": evidence,
            "events": events,
        }
    finally:
        con.close()


def _entry_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "actions": dict(Counter(str(entry["action"]) for entry in entries)),
        "statuses": dict(Counter(str(entry["status"]) for entry in entries)),
        "files": len(entries),
        "bytes": sum(int(entry.get("bytes", 0)) for entry in entries),
    }


def _source_labels(sources: list[Path]) -> dict[Path, str]:
    counts = Counter(_safe_label(path.parent.name or path.name) for path in sources)
    labels: dict[Path, str] = {}
    for path in sources:
        base = _safe_label(path.parent.name or path.name)
        if counts[base] > 1:
            base = f"{base}-{hashlib.sha256(str(path).encode()).hexdigest()[:8]}"
        labels[path] = base
    return labels


def _safe_label(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in value)
    return cleaned.strip("-") or "source"


def _unique_paths(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        resolved = path.expanduser().resolve()
        if resolved not in seen:
            out.append(resolved)
            seen.add(resolved)
    return out


def _load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid migration plan at {path}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != MIGRATION_SCHEMA_VERSION:
        raise ValueError(f"Unsupported migration plan: {path}")
    return plan


def _write_plan(path: Path, plan: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
