"""Incremental catalog extraction and atomic persistence."""
from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import RetroConfig, load_config
from ..experiment_traces import EXPERIMENT_RULE_ID, is_experiment_tag
from ..schema import read_events
from ..storage import Layout
from ..utils import (
    atomic_write_text,
    event_text,
    event_tool_input,
    event_tool_output,
    iter_jsonl,
    truncate,
)
from .annotations import AnnotationState, load_annotations
from .ignore import IgnoreResult, read_retroignore
from .markers import parse_markers
from .metadata import HostMetadata, extract_host_metadata
from .models import Catalog, CatalogUpdate, SessionRecord
from .projects import (
    decode_claude_slug,
    git_input_signature,
    normalize_remote,
    resolve_cwd,
    resolve_git_and_project,
)
from .rules import evaluate_rules, has_experiment_provenance, is_temp_path

CATALOG_SCHEMA = 1
EXTRACTOR_VERSION = "catalog-v1-v3-compatible-17"
EVALUATION_STATE_SCHEMA = 1
TAG_EVALUATOR_VERSION = "catalog-tags-v4"
PROJECT_EVALUATOR_VERSION = "catalog-projects-v3"
_NORMALIZED_SUFFIX = ".events.jsonl"
_TOOL_CALL_TYPES = {"tool_call", "command", "file_edit", "file_read", "subagent_start"}
_SYNTHETIC_USER_PREFIXES = (
    "# agents.md instructions",
    "<environment_context>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<recommended_plugins>",
    "<relay ",
    "<subagent_notification>",
    "<task-notification>",
    "<turn_aborted>",
    "<wake ",
)


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


@dataclass(frozen=True)
class EvaluationContext:
    cwd_resolved: str | None
    ignore: IgnoreResult
    ephemeral: bool
    git_signature: str
    experiment_parent_key: str | None = None


@dataclass
class CodexRawFallback:
    first_prompt: str | None = None
    user_messages: int = 0
    assistant_messages: int = 0
    tool_calls: int = 0
    assistant_activity: bool = False


def catalog_path(layout: Layout) -> Path:
    return layout.root / "catalog" / "sessions.jsonl"


def catalog_state_path(layout: Layout) -> Path:
    return layout.root / "catalog" / "state.json"


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


def _load_evaluation_state(layout: Layout) -> dict[str, dict[str, str]]:
    path = catalog_state_path(layout)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != EVALUATION_STATE_SCHEMA:
        return {}
    raw_records = value.get("records")
    if not isinstance(raw_records, dict):
        return {}
    records: dict[str, dict[str, str]] = {}
    for key, raw in raw_records.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        tag = raw.get("tag")
        project = raw.get("project")
        if isinstance(tag, str) and isinstance(project, str):
            records[key] = {"tag": tag, "project": project}
    return records


def update_catalog(
    layout: Layout,
    keys: Iterable[str] | None = None,
    force: bool = False,
) -> CatalogUpdate:
    config = load_config()
    existing = dict(load_catalog(layout))
    state_path = catalog_state_path(layout)
    legacy_state_missing = not state_path.is_file()
    evaluation_state = _load_evaluation_state(layout)
    initial_state = {
        key: dict(value)
        for key, value in evaluation_state.items()
    }
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
    tag_policy_digest = _tag_policy_digest(config)
    project_policy_digest = _project_policy_digest(config)
    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    if keys is None:
        for stale in sorted(set(existing) - available):
            del existing[stale]
            evaluation_state.pop(stale, None)
            removed.append(stale)
    else:
        for stale in sorted(selected - available):
            if stale in existing:
                del existing[stale]
                evaluation_state.pop(stale, None)
                removed.append(stale)

    for key in sorted(selected & available):
        host, session_id = _split_key(key)
        prior = existing.get(key)
        source_digest = _source_digest(layout, host, session_id)
        annotation = annotations.get(key, AnnotationState())
        context = _evaluation_context(prior, prior=prior) if prior else None
        project_evaluation = (
            _project_evaluation_digest(
                project_policy_digest,
                annotation.project_digest,
                context,
            )
            if context is not None
            else ""
        )
        tag_evaluation = (
            _tag_evaluation_digest(
                tag_policy_digest,
                annotation.tag_digest,
                context,
                project_evaluation,
            )
            if context is not None
            else ""
        )
        stored_evaluation = evaluation_state.get(key, {})
        content_unchanged = bool(
            prior is not None and prior.fingerprint == source_digest
        )
        legacy_migration = False
        if (
            prior is not None
            and not force
            and not content_unchanged
            and legacy_state_missing
            and context is not None
            and prior.fingerprint
            == _legacy_fingerprint(
                source_digest,
                _legacy_policy_digest(config),
                annotation.digest,
                _legacy_context_digest(context),
            )
        ):
            content_unchanged = True
            legacy_migration = True
        if prior is not None and not force and content_unchanged:
            assert context is not None
            needs_project = stored_evaluation.get("project") != project_evaluation
            needs_tag = (
                needs_project
                or stored_evaluation.get("tag") != tag_evaluation
            )
            if not needs_project and not needs_tag and not legacy_migration:
                continue
            record = SessionRecord.from_dict(prior.to_dict())
            record.fingerprint = source_digest
            if needs_project:
                _reevaluate_project(
                    record,
                    config=config,
                    annotation=annotation,
                    context=context,
                )
            if needs_tag:
                _reevaluate_tags(
                    record,
                    config=config,
                    annotation=annotation,
                    context=context,
                )
            evaluation_state[key] = {
                "tag": tag_evaluation,
                "project": project_evaluation,
            }
            if record.to_dict() != prior.to_dict():
                existing[key] = record
                changed.append(key)
            continue

        record = _build_content_record(
            layout,
            host,
            session_id,
            source_digest=source_digest,
        )
        context = _evaluation_context(record, prior=prior)
        _reevaluate_project(
            record,
            config=config,
            annotation=annotation,
            context=context,
        )
        _reevaluate_tags(
            record,
            config=config,
            annotation=annotation,
            context=context,
        )
        project_evaluation = _project_evaluation_digest(
            project_policy_digest,
            annotation.project_digest,
            context,
        )
        tag_evaluation = _tag_evaluation_digest(
            tag_policy_digest,
            annotation.tag_digest,
            context,
            project_evaluation,
        )
        evaluation_state[key] = {
            "tag": tag_evaluation,
            "project": project_evaluation,
        }
        existing[key] = record
        changed.append(key)
        if prior is None:
            added.append(key)

    changed.extend(
        _update_experiment_lineage(
            existing,
            config=config,
            annotations=annotations,
            evaluation_state=evaluation_state,
            tag_policy_digest=tag_policy_digest,
            project_policy_digest=project_policy_digest,
        )
    )
    changed = sorted(set(changed))
    if keys is None:
        for stale in set(evaluation_state) - set(existing):
            del evaluation_state[stale]
    if changed or removed or not catalog_path(layout).is_file():
        _write_catalog(layout, existing)
    if evaluation_state != initial_state or not state_path.is_file():
        _write_evaluation_state(layout, evaluation_state)
    excluded_total = sum(1 for record in existing.values() if record.excluded)
    return CatalogUpdate(
        changed_keys=changed,
        added_keys=added,
        removed_keys=removed,
        total=len(existing),
        excluded_total=excluded_total,
    )


def _update_experiment_lineage(
    records: dict[str, SessionRecord],
    *,
    config: RetroConfig,
    annotations: dict[str, AnnotationState],
    evaluation_state: dict[str, dict[str, str]],
    tag_policy_digest: str,
    project_policy_digest: str,
) -> list[str]:
    parents: dict[str, str] = {}
    if EXPERIMENT_RULE_ID not in config.disabled_rules:
        children: dict[str, list[str]] = {}
        roots = {
            key for key, record in records.items() if has_experiment_provenance(record)
        }
        for key, record in records.items():
            if record.parent_key:
                children.setdefault(record.parent_key, []).append(key)
        pending = deque(sorted(roots))
        seen = set(roots)
        while pending:
            parent = pending.popleft()
            for child in sorted(children.get(parent, [])):
                if child in seen:
                    continue
                seen.add(child)
                parents[child] = parent
                pending.append(child)

    changed: list[str] = []
    for key, prior in sorted(records.items()):
        experiment_parent = parents.get(key)
        if prior.experiment_parent_key == experiment_parent:
            continue
        record = SessionRecord.from_dict(prior.to_dict())
        record.experiment_parent_key = experiment_parent
        annotation = annotations.get(key, AnnotationState())
        context = _evaluation_context(record, prior=prior)
        _reevaluate_project(
            record,
            config=config,
            annotation=annotation,
            context=context,
        )
        _reevaluate_tags(
            record,
            config=config,
            annotation=annotation,
            context=context,
        )
        project_evaluation = _project_evaluation_digest(
            project_policy_digest,
            annotation.project_digest,
            context,
        )
        evaluation_state[key] = {
            "tag": _tag_evaluation_digest(
                tag_policy_digest,
                annotation.tag_digest,
                context,
                project_evaluation,
            ),
            "project": project_evaluation,
        }
        records[key] = record
        changed.append(key)
    return changed


def _build_content_record(
    layout: Layout,
    host: str,
    session_id: str,
    *,
    source_digest: str,
) -> SessionRecord:
    key = f"{host}/{session_id}"
    raw_dir = layout.raw_dir(host, session_id)
    metadata = extract_host_metadata(host, session_id, raw_dir)
    summary = _scan_events(layout.normalized_path(host, session_id))
    if host == "codex" and (
        summary.first_prompt is None
        or not _has_assistant_or_tool_activity(summary.counts)
    ):
        _apply_codex_raw_fallback(summary, raw_dir / "rollout.jsonl")
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
    started_at = summary.started_at or _normalize_timestamp(metadata.started_at)
    ended_at = summary.ended_at or _normalize_timestamp(metadata.ended_at)
    duration_s = summary.duration_s
    if duration_s is None:
        started = _parse_timestamp(started_at)
        ended = _parse_timestamp(ended_at)
        if started is not None and ended is not None:
            duration_s = round(max(0.0, (ended - started).total_seconds()), 6)
    models = list(dict.fromkeys([*metadata.models, *summary.models]))
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
        git={
            "remote": normalize_remote(metadata.remote_url),
            "remote_url": metadata.remote_url,
            "branch": metadata.branch,
            "commit": metadata.commit,
            "root": None,
            "worktree": None,
        },
        project={
            "id": "unknown",
            "name": "Unknown",
            "source": "unknown",
            "evidence": None,
        },
        models=models,
        counts=summary.counts,
        markers=markers,
        tags=[],
        tag_sources={},
        excluded=False,
        fingerprint=source_digest,
        originator=metadata.originator,
    )
    return record


def _evaluation_context(
    record: SessionRecord | None,
    *,
    prior: SessionRecord | None,
) -> EvaluationContext:
    if record is None:
        return EvaluationContext(None, IgnoreResult([], None, "none"), False, "none")
    cwd_resolved = (
        resolve_cwd(record.cwd)
        if record.cwd and record.cwd != record.cwd_resolved
        else record.cwd_resolved
    )
    ephemeral = bool(cwd_resolved and is_temp_path(cwd_resolved))
    cwd_exists = bool(
        cwd_resolved
        and "://" not in cwd_resolved
        and Path(cwd_resolved).exists()
    )
    ignore = read_retroignore(
        cwd_resolved if cwd_exists else None,
        prior=prior,
    )
    git_signature = (
        "ephemeral"
        if ephemeral and not cwd_exists
        else git_input_signature(cwd_resolved)
    )
    return EvaluationContext(
        cwd_resolved=cwd_resolved,
        ignore=ignore,
        ephemeral=ephemeral,
        git_signature=git_signature,
        experiment_parent_key=record.experiment_parent_key,
    )


def _reevaluate_project(
    record: SessionRecord,
    *,
    config: RetroConfig,
    annotation: AnnotationState,
    context: EvaluationContext,
) -> None:
    record.cwd_resolved = context.cwd_resolved
    metadata = HostMetadata(
        agent=record.agent,
        client=record.client,
        interactive=record.interactive,
        is_subagent=record.is_subagent,
        parent_key=record.parent_key,
        title=record.title,
        cwd=record.cwd,
        remote_url=record.git.get("remote_url") or record.git.get("remote"),
        branch=record.git.get("branch"),
        commit=record.git.get("commit"),
        models=list(record.models),
        started_at=record.started_at,
        ended_at=record.ended_at,
        originator=record.originator,
    )
    marker_project = next(
        (
            marker["value"]
            for marker in reversed(record.markers)
            if marker["directive"] == "project"
        ),
        None,
    )
    annotation_project = annotation.project if annotation.project_is_set else None
    record.git, record.project = resolve_git_and_project(
        metadata,
        cwd_resolved=context.cwd_resolved,
        config=config,
        marker_project=marker_project,
        annotation_project=annotation_project,
        is_temp=context.ephemeral,
    )


def _reevaluate_tags(
    record: SessionRecord,
    *,
    config: RetroConfig,
    annotation: AnnotationState,
    context: EvaluationContext,
) -> None:
    sources = evaluate_rules(record, config)
    if context.ignore.path:
        for tag in context.ignore.tags:
            sources.setdefault(tag, []).append(
                f"dotfile:{context.ignore.path}"
            )
    marker_keep = False
    for marker in record.markers:
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


def _is_user_authored_text(text: str) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    lowered = stripped.casefold()
    if lowered.startswith(_SYNTHETIC_USER_PREFIXES):
        return False
    return not lowered.startswith("<bash-stdout>")


def _has_assistant_or_tool_activity(counts: dict[str, int]) -> bool:
    return bool(
        counts.get("assistant_messages", 0)
        or counts.get("tool_calls", 0)
    )


def _apply_codex_raw_fallback(summary: EventSummary, path: Path) -> None:
    fallback = _scan_codex_raw_fallback(path)
    if summary.first_prompt is None and fallback.first_prompt:
        summary.first_prompt = truncate(
            _collapse_whitespace(fallback.first_prompt),
            2000,
        )
        summary.counts["user_messages"] = max(
            summary.counts["user_messages"],
            1,
        )
        summary.markers.extend(
            parse_markers(fallback.first_prompt, event_id=None)
        )
    if not _has_assistant_or_tool_activity(summary.counts):
        summary.counts["assistant_messages"] = fallback.assistant_messages
        summary.counts["tool_calls"] = fallback.tool_calls
        if (
            fallback.assistant_activity
            and not _has_assistant_or_tool_activity(summary.counts)
        ):
            summary.counts["assistant_messages"] = 1


def _scan_codex_raw_fallback(path: Path) -> CodexRawFallback:
    fallback = CodexRawFallback()
    if not path.is_file():
        return fallback
    for _, raw in iter_jsonl(path):
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            continue
        outer_type = raw.get("type")
        payload_type = payload.get("type")
        if outer_type == "event_msg":
            if payload_type == "user_message":
                text = payload.get("message")
                if isinstance(text, str) and _is_user_authored_text(text):
                    fallback.user_messages += 1
                    if fallback.first_prompt is None:
                        fallback.first_prompt = text
            elif payload_type == "agent_message":
                fallback.assistant_activity = True
                fallback.assistant_messages += 1
            elif payload_type in {
                "agent_reasoning",
                "patch_apply_end",
                "web_search_end",
                "tool_search_output",
            }:
                fallback.assistant_activity = True
                if payload_type != "agent_reasoning":
                    fallback.tool_calls += 1
            elif payload_type == "item_completed":
                item = payload.get("item")
                if isinstance(item, dict):
                    _consume_codex_response_item(fallback, item)
        elif outer_type == "response_item":
            _consume_codex_response_item(fallback, payload)
    return fallback


def _consume_codex_response_item(
    fallback: CodexRawFallback,
    payload: dict[str, Any],
) -> None:
    payload_type = payload.get("type")
    if payload_type == "message":
        role = payload.get("role")
        text = _flatten_raw_content(payload.get("content"))
        if role == "user" and _is_user_authored_text(text):
            fallback.user_messages += 1
            if fallback.first_prompt is None:
                fallback.first_prompt = text
        elif role == "assistant":
            fallback.assistant_activity = True
            if text.strip():
                fallback.assistant_messages += 1
        return
    if payload_type in {
        "function_call",
        "custom_tool_call",
        "web_search_call",
        "tool_search_call",
    }:
        fallback.assistant_activity = True
        fallback.tool_calls += 1
    elif payload_type in {
        "function_call_output",
        "custom_tool_call_output",
        "tool_search_output",
    }:
        fallback.assistant_activity = True
        fallback.tool_calls = max(1, fallback.tool_calls)
    elif payload_type == "reasoning":
        fallback.assistant_activity = True


def _flatten_raw_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = (
                item.get("text")
                or item.get("input_text")
                or item.get("output_text")
            )
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


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
            text = event_text(event)
            if _is_user_authored_text(text):
                summary.counts["user_messages"] += 1
                if summary.first_prompt is None:
                    summary.first_prompt = truncate(
                        _collapse_whitespace(text),
                        2000,
                    )
                summary.markers.extend(
                    parse_markers(text, event_id=event.event_id)
                )
            else:
                summary.markers.extend(
                    marker
                    for marker in parse_markers(text, event_id=event.event_id)
                    if marker["directive"] == "tag"
                    and is_experiment_tag(marker.get("value"))
                )
        elif event.event_type == "message" and event.actor == "assistant":
            summary.counts["assistant_messages"] += 1
        if event.event_type in _TOOL_CALL_TYPES and event.actor in {"assistant", "subagent"}:
            summary.counts["tool_calls"] += 1
            models.extend(_payload_models(event_tool_input(event)))
        elif event.event_type in {
            "tool_result",
            "file_edit",
            "file_read",
            "command",
        } and event.actor == "tool":
            models.extend(_payload_models(event_tool_output(event)))
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


def _payload_models(payload: Any) -> list[str]:
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
                    "currentmodel",
                    "model",
                    "modelid",
                    "modelname",
                    "selectedmodel",
                    "usagemodel",
                } and isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif folded in {"modelmetrics", "modelusage"} and isinstance(item, dict):
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
            if not host_dir.is_dir() or host_dir.name.startswith("."):
                continue
            for session_dir in host_dir.iterdir():
                if session_dir.is_dir() and not session_dir.name.startswith("."):
                    keys.add(f"{host_dir.name}/{session_dir.name}")
    normalized_root = layout.root / "normalized"
    if normalized_root.is_dir():
        for host_dir in normalized_root.iterdir():
            if not host_dir.is_dir() or host_dir.name.startswith("."):
                continue
            for path in host_dir.glob(f"*{_NORMALIZED_SUFFIX}"):
                if path.name.startswith("."):
                    continue
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


def _tag_policy_digest(config: RetroConfig) -> str:
    payload = {
        "version": TAG_EVALUATOR_VERSION,
        "exclude_tags": config.exclude_tags,
        "disabled_rules": config.disabled_rules,
        "rules": config.rules,
    }
    return _json_digest(payload)


def _project_policy_digest(config: RetroConfig) -> str:
    return _json_digest(
        {
            "version": PROJECT_EVALUATOR_VERSION,
            "projects": config.projects,
        }
    )


def _project_evaluation_digest(
    policy_digest: str,
    annotation_digest: str,
    context: EvaluationContext,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                policy_digest,
                annotation_digest,
                context.cwd_resolved or "",
                context.git_signature,
            )
        ).encode()
    ).hexdigest()


def _tag_evaluation_digest(
    policy_digest: str,
    annotation_digest: str,
    context: EvaluationContext,
    project_evaluation_digest: str,
) -> str:
    return hashlib.sha256(
        "\0".join(
            (
                policy_digest,
                annotation_digest,
                context.cwd_resolved or "",
                context.ignore.signature,
                project_evaluation_digest,
                context.experiment_parent_key or "",
            )
        ).encode()
    ).hexdigest()


def _legacy_policy_digest(config: RetroConfig) -> str:
    return _json_digest(
        {
            "exclude_tags": config.exclude_tags,
            "disabled_rules": config.disabled_rules,
            "rules": config.rules,
            "projects": config.projects,
        }
    )


def _legacy_fingerprint(
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


def _legacy_context_digest(context: EvaluationContext) -> str:
    return hashlib.sha256(
        f"{context.cwd_resolved or ''}\0"
        f"{context.ignore.signature}\0{context.git_signature}".encode()
    ).hexdigest()


def _json_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
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


def _write_evaluation_state(
    layout: Layout,
    records: dict[str, dict[str, str]],
) -> None:
    atomic_write_text(
        catalog_state_path(layout),
        json.dumps(
            {
                "schema": EVALUATION_STATE_SCHEMA,
                "records": {
                    key: records[key]
                    for key in sorted(records)
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
    )


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
