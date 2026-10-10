"""Incremental catalog extraction and atomic persistence."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import RetroConfig, load_config
from ..schema import read_events
from ..storage import Layout
from ..utils import atomic_write_text, event_text, truncate
from .annotations import AnnotationState, load_annotations
from .ignore import read_retroignore
from .markers import parse_markers
from .metadata import extract_host_metadata
from .models import Catalog, CatalogUpdate, SessionRecord
from .projects import (
    decode_claude_slug,
    git_input_signature,
    resolve_cwd,
    resolve_git_and_project,
)
from .rules import evaluate_rules, is_temp_path

CATALOG_SCHEMA = 1
EXTRACTOR_VERSION = "catalog-v1-normalization-v2-compatible-13"
_NORMALIZED_SUFFIX = ".events.jsonl"
_TOOL_CALL_TYPES = {"tool_call", "command", "file_edit", "file_read", "subagent_start"}


@dataclass
class EventSummary:
    first_prompt: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_s: float | None = None
    active_s: float | None = None
    cwd: str | None = None
    models: list[str] = field(default_factory=list)
    markers: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "events": 0,
            "user_messages": 0,
            "assistant_messages": 0,
            "tool_calls": 0,
            "unknown_events": 0,
        }
    )


def catalog_path(layout: Layout) -> Path:
    return layout.root / "catalog" / "sessions.jsonl"


def load_catalog(layout: Layout) -> Catalog:
    path = catalog_path(layout)
    if not path.is_file():
        return Catalog()
    records: dict[str, SessionRecord] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid catalog record at {path}:{line_no}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"Catalog record at {path}:{line_no} must be an object")
            record = SessionRecord.from_dict(value)
            if record.key in records:
                raise ValueError(f"Duplicate catalog key {record.key!r} at {path}:{line_no}")
            records[record.key] = record
    return Catalog(records)


def update_catalog(
    layout: Layout,
    keys: Iterable[str] | None = None,
    force: bool = False,
) -> CatalogUpdate:
    config = load_config()
    existing = dict(load_catalog(layout))
    if keys is None:
        available = _available_keys(layout)
        selected = set(available)
    else:
        selected = _normalize_keys(keys)
        available = {
            key
            for key in selected
            if _key_is_available(layout, key)
        }
    annotations = load_annotations(layout.root)
    policy_digest = _policy_digest(config)
    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    if keys is None:
        for stale in sorted(set(existing) - available):
            del existing[stale]
            removed.append(stale)
    else:
        for stale in sorted(selected - available):
            if stale in existing:
                del existing[stale]
                removed.append(stale)

    for key in sorted(selected & available):
        host, session_id = _split_key(key)
        prior = existing.get(key)
        source_digest = _source_digest(layout, host, session_id)
        annotation = annotations.get(key, AnnotationState())
        current_resolved = None
        if prior:
            current_resolved = (
                resolve_cwd(prior.cwd)
                if prior.cwd and prior.cwd != prior.cwd_resolved
                else prior.cwd_resolved
            )
        ephemeral = bool(
            prior
            and prior.cwd_resolved
            and is_temp_path(prior.cwd_resolved)
        )
        prior_ignore = read_retroignore(
            None if ephemeral else current_resolved,
            prior=prior,
        )
        context_digest = _context_digest(
            current_resolved,
            prior_ignore.signature,
            ephemeral=ephemeral,
        )
        expected = _fingerprint(
            source_digest,
            policy_digest,
            annotation.digest,
            context_digest,
        )
        if prior is not None and not force and prior.fingerprint == expected:
            continue
        record = _build_record(
            layout,
            host,
            session_id,
            config=config,
            annotation=annotation,
            prior=prior,
            source_digest=source_digest,
            policy_digest=policy_digest,
        )
        existing[key] = record
        changed.append(key)
        if prior is None:
            added.append(key)

    if changed or removed or not catalog_path(layout).is_file():
        _write_catalog(layout, existing)
    excluded_total = sum(1 for record in existing.values() if record.excluded)
    return CatalogUpdate(
        changed_keys=changed,
        added_keys=added,
        removed_keys=removed,
        total=len(existing),
        excluded_total=excluded_total,
    )


def _build_record(
    layout: Layout,
    host: str,
    session_id: str,
    *,
    config: RetroConfig,
    annotation: AnnotationState,
    prior: SessionRecord | None,
    source_digest: str,
    policy_digest: str,
) -> SessionRecord:
    key = f"{host}/{session_id}"
    raw_dir = layout.raw_dir(host, session_id)
    metadata = extract_host_metadata(host, session_id, raw_dir)
    summary = _scan_events(layout.normalized_path(host, session_id))
    cwd = metadata.cwd or summary.cwd
    cwd_resolved = resolve_cwd(cwd)
    if cwd_resolved is None and metadata.project_slug:
        cwd_resolved = decode_claude_slug(metadata.project_slug)
    title = truncate(metadata.title, 200) if metadata.title else (
        truncate(summary.first_prompt, 200) if summary.first_prompt else None
    )
    markers = []
    if metadata.title:
        markers.extend(parse_markers(metadata.title, event_id=None))
    markers.extend(summary.markers)
    marker_project = next(
        (
            marker["value"]
            for marker in reversed(markers)
            if marker["directive"] == "project"
        ),
        None,
    )
    annotation_project = annotation.project if annotation.project_is_set else None
    git, project = resolve_git_and_project(
        metadata,
        cwd_resolved=cwd_resolved,
        config=config,
        marker_project=marker_project,
        annotation_project=annotation_project,
    )
    started_at = summary.started_at or _normalize_timestamp(metadata.started_at)
    ended_at = summary.ended_at or _normalize_timestamp(metadata.ended_at)
    duration_s = summary.duration_s
    if duration_s is None:
        started = _parse_timestamp(started_at)
        ended = _parse_timestamp(ended_at)
        if started is not None and ended is not None:
            duration_s = round(max(0.0, (ended - started).total_seconds()), 6)
    models = list(dict.fromkeys([*metadata.models, *summary.models]))
    ignore = read_retroignore(cwd_resolved, prior=prior)
    record = SessionRecord(
        schema=CATALOG_SCHEMA,
        key=key,
        host=host,
        session_id=session_id,
        agent=metadata.agent,
        client=metadata.client,
        interactive=metadata.interactive,
        is_subagent=metadata.is_subagent,
        parent_key=metadata.parent_key,
        title=title,
        first_prompt=summary.first_prompt,
        started_at=started_at,
        ended_at=ended_at,
        duration_s=duration_s,
        active_s=summary.active_s,
        cwd=cwd,
        cwd_resolved=cwd_resolved,
        git=git,
        project=project,
        models=models,
        counts=summary.counts,
        markers=markers,
        tags=[],
        tag_sources={},
        excluded=False,
        fingerprint="",
    )
    sources = evaluate_rules(record, config)
    if ignore.path:
        for tag in ignore.tags:
            sources.setdefault(tag, []).append(f"dotfile:{ignore.path}")
    marker_keep = False
    for marker in markers:
        directive = marker["directive"]
        if directive == "ignore":
            sources.setdefault("ignore", []).append("marker")
        elif directive == "tag" and marker.get("value"):
            sources.setdefault(str(marker["value"]), []).append("marker")
        elif directive == "keep":
            marker_keep = True
    if marker_keep and "ignore" in sources:
        sources["ignore"] = [
            source
            for source in sources["ignore"]
            if not source.startswith(("rule:", "dotfile:"))
        ]
        if not sources["ignore"]:
            del sources["ignore"]
    for tag in annotation.added_tags:
        sources.setdefault(tag, []).append("manual")
    for tag in annotation.suppressed_tags:
        sources.pop(tag, None)
    record.tag_sources = {
        tag: list(dict.fromkeys(tag_sources))
        for tag, tag_sources in sorted(sources.items())
        if tag_sources
    }
    record.tags = sorted(record.tag_sources)
    record.excluded = bool(set(record.tags) & set(config.exclude_tags))
    record.fingerprint = _fingerprint(
        source_digest,
        policy_digest,
        annotation.digest,
        _context_digest(
            cwd_resolved,
            ignore.signature,
            ephemeral=is_temp_path(cwd_resolved) if cwd_resolved else False,
        ),
    )
    return record


def _scan_events(path: Path) -> EventSummary:
    summary = EventSummary()
    if not path.is_file():
        return summary
    timestamps: list[datetime] = []
    previous: datetime | None = None
    active_s = 0.0
    models: list[str] = []
    for event in read_events(path):
        summary.counts["events"] += 1
        if event.event_type == "message" and event.actor == "user":
            summary.counts["user_messages"] += 1
            text = event_text(event)
            if summary.first_prompt is None and text.strip():
                summary.first_prompt = truncate(_collapse_whitespace(text), 2000)
            summary.markers.extend(parse_markers(text, event_id=event.event_id))
        elif event.event_type == "message" and event.actor == "assistant":
            summary.counts["assistant_messages"] += 1
        if event.event_type in _TOOL_CALL_TYPES and event.actor in {"assistant", "subagent"}:
            summary.counts["tool_calls"] += 1
        if event.event_type == "unknown":
            summary.counts["unknown_events"] += 1
        payload = event.payload or {}
        if summary.cwd is None:
            summary.cwd = _payload_cwd(payload)
        models.extend(_payload_models(payload))
        parsed = _parse_timestamp(event.timestamp)
        if parsed is None:
            continue
        timestamps.append(parsed)
        if previous is not None:
            active_s += min(300.0, max(0.0, (parsed - previous).total_seconds()))
        previous = parsed
    summary.models = list(dict.fromkeys(models))
    if timestamps:
        started = min(timestamps)
        ended = max(timestamps)
        summary.started_at = _format_timestamp(started)
        summary.ended_at = _format_timestamp(ended)
        summary.duration_s = round(max(0.0, (ended - started).total_seconds()), 6)
        summary.active_s = round(active_s, 6)
    return summary


def _payload_models(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    stack: list[tuple[Any, int]] = [(payload, 0)]
    visited = 0
    while stack and visited < 256:
        value, depth = stack.pop()
        visited += 1
        if isinstance(value, dict):
            for key, item in value.items():
                folded = str(key).replace("_", "").casefold()
                if folded in {
                    "model",
                    "modelid",
                    "modelname",
                    "selectedmodel",
                    "usagemodel",
                } and isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif folded == "modelusage" and isinstance(item, dict):
                    out.extend(str(name) for name in item if str(name).strip())
                if depth < 4 and isinstance(item, (dict, list)):
                    stack.append((item, depth + 1))
        elif isinstance(value, list) and depth < 4:
            stack.extend((item, depth + 1) for item in value if isinstance(item, (dict, list)))
    return list(dict.fromkeys(out))


def _payload_cwd(payload: dict[str, Any]) -> str | None:
    for key in ("cwd", "current_working_directory", "workingDirectory"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _available_keys(layout: Layout) -> set[str]:
    keys: set[str] = set()
    raw_root = layout.root / "raw"
    if raw_root.is_dir():
        for host_dir in raw_root.iterdir():
            if not host_dir.is_dir():
                continue
            for session_dir in host_dir.iterdir():
                if session_dir.is_dir():
                    keys.add(f"{host_dir.name}/{session_dir.name}")
    normalized_root = layout.root / "normalized"
    if normalized_root.is_dir():
        for host_dir in normalized_root.iterdir():
            if not host_dir.is_dir():
                continue
            for path in host_dir.glob(f"*{_NORMALIZED_SUFFIX}"):
                keys.add(
                    f"{host_dir.name}/{path.name[: -len(_NORMALIZED_SUFFIX)]}"
                )
    return keys


def _normalize_keys(keys: Iterable[str]) -> set[str]:
    normalized: set[str] = set()
    for key in keys:
        host, session_id = _split_key(str(key))
        normalized.add(f"{host}/{session_id}")
    return normalized


def _key_is_available(layout: Layout, key: str) -> bool:
    host, session_id = _split_key(key)
    return (
        layout.raw_dir(host, session_id).is_dir()
        or layout.normalized_path(host, session_id).is_file()
    )


def _split_key(key: str) -> tuple[str, str]:
    host, separator, session_id = key.partition("/")
    if not separator or not host or not session_id:
        raise ValueError(f"invalid catalog key {key!r}; expected host/session_id")
    return host, session_id


def _source_digest(layout: Layout, host: str, session_id: str) -> str:
    normalized = f"normalized/{host}/{session_id}{_NORMALIZED_SUFFIX}"
    raw_prefix = f"raw/{host}/{session_id}"
    relative_paths = [normalized]
    raw_dir = layout.root / raw_prefix
    if host == "codex":
        relative_paths.extend(
            [f"{raw_prefix}/rollout.jsonl", f"{raw_prefix}/thread.json"]
        )
    elif host == "claude-code":
        relative_paths.extend(
            [f"{raw_prefix}/transcript.jsonl", f"{raw_prefix}/import_meta.json"]
        )
    elif host == "vscode-copilot":
        relative_paths.extend(
            [
                f"{raw_prefix}/import_meta.json",
                f"{raw_prefix}/sidecars/workspace.yaml",
                f"{raw_prefix}/events.jsonl",
            ]
        )
    elif raw_dir.is_dir():
        relative_paths.extend(
            path.relative_to(layout.root).as_posix()
            for path in sorted(path for path in raw_dir.rglob("*") if path.is_file())
        )
    parts = [EXTRACTOR_VERSION]
    for relative in relative_paths:
        try:
            stat = os.stat(layout.root / relative)
        except OSError:
            parts.append(f"{relative}:missing")
        else:
            parts.append(f"{relative}:{stat.st_mtime_ns}:{stat.st_size}")
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _policy_digest(config: RetroConfig) -> str:
    payload = {
        "exclude_tags": config.exclude_tags,
        "disabled_rules": config.disabled_rules,
        "rules": config.rules,
        "projects": config.projects,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _fingerprint(
    source_digest: str,
    policy_digest: str,
    annotation_digest: str,
    context_digest: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (source_digest, policy_digest, annotation_digest, context_digest)
        ).encode()
    ).hexdigest()


def _context_digest(
    cwd_resolved: str | None,
    ignore_digest: str,
    *,
    ephemeral: bool,
) -> str:
    git_digest = (
        "ephemeral"
        if ephemeral
        else git_input_signature(cwd_resolved)
    )
    return hashlib.sha256(
        f"{cwd_resolved or ''}\0{ignore_digest}\0{git_digest}".encode()
    ).hexdigest()


def _write_catalog(layout: Layout, records: dict[str, SessionRecord]) -> None:
    content = "".join(
        json.dumps(
            records[key].to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for key in sorted(records)
    )
    atomic_write_text(catalog_path(layout), content)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None:
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000
        try:
            return datetime.fromtimestamp(numeric, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_timestamp(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    return _format_timestamp(parsed) if parsed else None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _collapse_whitespace(value: str) -> str:
    return " ".join(value.split())
