"""Fact table construction: catalog + signal records become compact columnar rows.

The browser does all slicing and aggregation over these rows, so the Python side
only has to emit dictionaries, the fact table, a sparse signal matrix and the
handful of summaries that cannot be derived from rows alone.
"""
from __future__ import annotations

import math
import os
import zlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..storage import Layout
from ..utils import truncate
from .inputs import (
    CatalogView,
    SessionRecord,
    SignalDefinition,
    SignalStoreView,
    SyncView,
    parse_iso,
)

COLUMNS = (
    "key",
    "title",
    "prompt",
    "project",
    "agent",
    "client",
    "started",
    "duration",
    "active",
    "cost",
    "tokens",
    "turns",
    "tools",
    "edits",
    "events",
    "unknown",
    "outcome",
    "work",
    "flags",
    "tags",
    "models",
    "corrections",
    "errors",
    "basis",
    "bucket",
    "parent",
)

COLUMN_INDEX = {name: position for position, name in enumerate(COLUMNS)}

FLAGS = {
    "excluded": 1,
    "interactive": 2,
    "automated": 4,
    "subagent": 8,
    "child": 16,
    "parent": 32,
    "committed": 64,
    "validated": 128,
    "secrets": 256,
    "signals": 512,
    "transcript": 1024,
    "worktree": 2048,
}

COST_SCALE = 1_000_000
RATIO_SCALE = 1000
TITLE_LIMIT = 110
EXCLUDED_TITLE_LIMIT = 80
PROMPT_LIMIT = 180
DETAIL_PROMPT_LIMIT = 2000
EXCLUDED_PROMPT_LIMIT = 400
PROJECT_NAME_LIMIT = 80
DETAIL_BUCKET_TARGET = 64
EXCLUDED_DETAIL_BUCKET_TARGET = 320
MAX_DETAIL_BUCKETS = 4096
SPLIT_DICTIONARIES = ("agents", "clients", "projects", "models", "tags")
COST_BASIS_ORDER = ("exact", "family", "mixed", "embedded", "unpriced", "none")
COST_BASIS_SIGNAL = "quality.cost_basis"
EXAMPLE_LIMIT = 6

HEADLINE_SIGNALS = (
    "usage.cost_usd",
    "usage.tokens_total",
    "usage.tokens_input",
    "usage.tokens_output",
    "usage.tokens_cache_read",
    "usage.duration_s",
    "usage.active_s",
    "activity.user_turns",
    "activity.tool_calls",
    "activity.file_edits",
    "activity.files_touched",
    "activity.commands",
    "outcome.status",
    "outcome.validated",
    "outcome.committed",
    "friction.user_corrections",
    "friction.tool_error_rate",
    "friction.interruptions",
    "risk.secret_exposure",
    "work.kind",
)

OUTCOME_ORDER = ("success", "partial", "failed", "abandoned", "unknown")
WORK_ORDER = (
    "build",
    "debug",
    "refactor",
    "test",
    "explore",
    "review",
    "docs",
    "ops",
    "research",
    "chat",
    "other",
)


@dataclass
class Dictionaries:
    agents: list[str] = field(default_factory=list)
    clients: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    work: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "clients": self.clients,
            "hosts": self.hosts,
            "projects": self.projects,
            "models": self.models,
            "tags": self.tags,
            "outcomes": self.outcomes,
            "work": self.work,
        }


@dataclass
class SignalEncoding:
    id: str
    encoding: str
    categories: list[str] = field(default_factory=list)


@dataclass
class DashboardModel:
    rows: list[list[Any]]
    index: dict[str, int]
    dictionaries: Dictionaries
    records: dict[str, SessionRecord]
    signals: SignalStoreView
    definitions: list[SignalDefinition]
    encodings: dict[str, SignalEncoding]
    matrix: dict[int, dict[str, Any]]
    summaries: dict[str, dict[str, Any]]
    quality: dict[str, Any]
    sync: dict[str, Any]
    bucket_count: int
    fingerprints: dict[str, str]
    children: dict[str, list[str]]
    included_count: int = 0
    excluded_bucket_count: int = 1
    dictionary_split: dict[str, int] = field(default_factory=dict)
    cost_basis: list[str] = field(default_factory=list)

    def row(self, key: str) -> list[Any] | None:
        position = self.index.get(key)
        return self.rows[position] if position is not None else None

    @property
    def included_rows(self) -> list[list[Any]]:
        return self.rows[: self.included_count]

    @property
    def excluded_rows(self) -> list[list[Any]]:
        return self.rows[self.included_count :]


class _Interner:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.split = 0
        self._positions: dict[str, int] = {}

    def mark_split(self) -> None:
        self.split = len(self.values)

    def index(self, value: str) -> int:
        position = self._positions.get(value)
        if position is None:
            position = len(self.values)
            self._positions[value] = position
            self.values.append(value)
        return position


def build_model(
    layout: Layout,
    catalog: CatalogView,
    signals: SignalStoreView,
    sync: SyncView,
) -> DashboardModel:
    ordered = sorted(
        catalog.records,
        key=lambda record: (-(parse_iso(record.started_at) or -math.inf), record.key),
    )
    # Included sessions come first so their dictionary entries, fact rows and
    # signal-matrix positions all live in the prefix the browser loads at boot.
    records = [record for record in ordered if not record.excluded]
    included_count = len(records)
    records = records + [record for record in ordered if record.excluded]
    by_key = {record.key: record for record in records}
    transcripts = _rendered_session_ids(layout, {record.host for record in records})

    children: dict[str, list[str]] = {}
    for record in records:
        if record.parent_key and record.parent_key in by_key:
            children.setdefault(record.parent_key, []).append(record.key)

    agents = _Interner()
    clients = _Interner()
    hosts = _Interner()
    models = _Interner()
    tags = _Interner()
    projects = _Interner()
    project_meta: dict[str, dict[str, Any]] = {}
    outcomes = _Interner()
    work_kinds = _Interner()
    for value in OUTCOME_ORDER:
        outcomes.index(value)
    for value in WORK_ORDER:
        work_kinds.index(value)

    cost_basis = _Interner()
    for value in COST_BASIS_ORDER:
        cost_basis.index(value)
    bucket_count = _bucket_count(included_count)
    excluded_bucket_count = _bucket_count(
        len(records) - included_count, target=EXCLUDED_DETAIL_BUCKET_TARGET
    )
    rows: list[list[Any]] = []
    index: dict[str, int] = {}
    fingerprints: dict[str, str] = {}

    for position, record in enumerate(records):
        if position == included_count:
            for interner in (agents, clients, projects, models, tags):
                interner.mark_split()
        values = signals.values(record.key)
        project_index = projects.index(record.project.id)
        meta = project_meta.setdefault(
            record.project.id,
            {
                "id": record.project.id,
                "name": truncate(record.project.name, PROJECT_NAME_LIMIT),
                "source": record.project.source,
                "remote": None,
            },
        )
        if meta.get("remote") is None:
            remote = record.git.get("remote") if isinstance(record.git, dict) else None
            if isinstance(remote, str) and remote:
                meta["remote"] = remote
        if meta.get("source") in (None, "unknown") and record.project.source:
            meta["source"] = record.project.source

        flags = 0
        if record.excluded:
            flags |= FLAGS["excluded"]
        if record.interactive is True:
            flags |= FLAGS["interactive"]
        elif record.interactive is False:
            flags |= FLAGS["automated"]
        if record.is_subagent:
            flags |= FLAGS["subagent"]
        if record.parent_key:
            flags |= FLAGS["child"]
        if children.get(record.key):
            flags |= FLAGS["parent"]
        if _truthy(values.get("outcome.committed")):
            flags |= FLAGS["committed"]
        if _truthy(values.get("outcome.validated")):
            flags |= FLAGS["validated"]
        if _truthy(values.get("risk.secret_exposure")):
            flags |= FLAGS["secrets"]
        if record.key in signals.records:
            flags |= FLAGS["signals"]
        if record.session_id in transcripts.get(record.host, frozenset()):
            flags |= FLAGS["transcript"]
        if isinstance(record.git, dict) and record.git.get("worktree") is True:
            flags |= FLAGS["worktree"]

        title_limit = EXCLUDED_TITLE_LIMIT if record.excluded else TITLE_LIMIT
        row = [
            record.key,
            truncate(record.title or record.first_prompt or "", title_limit),
            "" if record.excluded else truncate(record.first_prompt or "", PROMPT_LIMIT),
            project_index,
            agents.index(record.agent),
            clients.index(record.client),
            _epoch_seconds(record.started_at),
            _duration(record.duration_s, values.get("usage.duration_s")),
            _duration(record.active_s, values.get("usage.active_s")),
            _scaled(values.get("usage.cost_usd"), COST_SCALE),
            _integer(values.get("usage.tokens_total")),
            _integer(values.get("activity.user_turns"), record.counts.get("user_messages")),
            _integer(values.get("activity.tool_calls"), record.counts.get("tool_calls")),
            _integer(values.get("activity.file_edits")),
            _integer(record.counts.get("events")),
            _integer(record.counts.get("unknown_events")),
            _category_index(outcomes, values.get("outcome.status")),
            _category_index(work_kinds, values.get("work.kind")),
            flags,
            [tags.index(tag) for tag in record.tags],
            [models.index(model) for model in record.models],
            _integer(values.get("friction.user_corrections")),
            _scaled(values.get("friction.tool_error_rate"), RATIO_SCALE),
            _category_index(cost_basis, values.get(COST_BASIS_SIGNAL)),
            _bucket_index(
                record.key,
                excluded_bucket_count if record.excluded else bucket_count,
            ),
            record.parent_key if record.parent_key in by_key else None,
        ]
        hosts.index(record.host)
        rows.append(row)
        index[record.key] = position
        fingerprints[record.key] = record.fingerprint

    if included_count == len(records):
        for interner in (agents, clients, projects, models, tags):
            interner.mark_split()
    dictionary_split = {
        "agents": agents.split,
        "clients": clients.split,
        "projects": projects.split,
        "models": models.split,
        "tags": tags.split,
    }
    encodings, matrix = _signal_matrix(records, index, signals)
    summaries = _signal_summaries(signals, encodings, matrix)
    dictionaries = Dictionaries(
        agents=agents.values,
        clients=clients.values,
        hosts=hosts.values,
        projects=[project_meta[value] for value in projects.values],
        models=models.values,
        tags=tags.values,
        outcomes=outcomes.values,
        work=work_kinds.values,
    )
    quality = _quality(records, signals, catalog, transcripts, tags.values, cost_basis.values)
    return DashboardModel(
        rows=rows,
        index=index,
        dictionaries=dictionaries,
        records=by_key,
        signals=signals,
        definitions=list(signals.definitions),
        encodings=encodings,
        matrix=matrix,
        summaries=summaries,
        quality=quality,
        sync=_sync_summary(sync),
        bucket_count=bucket_count,
        fingerprints=fingerprints,
        children=children,
        included_count=included_count,
        excluded_bucket_count=excluded_bucket_count,
        dictionary_split=dictionary_split,
        cost_basis=cost_basis.values,
    )


def _signal_matrix(
    records: list[SessionRecord],
    index: dict[str, int],
    signals: SignalStoreView,
) -> tuple[dict[str, SignalEncoding], dict[int, dict[str, Any]]]:
    definitions = {definition.id: definition for definition in signals.definitions}
    encodings: dict[str, SignalEncoding] = {}
    matrix: dict[int, dict[str, Any]] = {}

    def encoding_for(signal_id: str, value: Any) -> SignalEncoding:
        existing = encodings.get(signal_id)
        if existing is not None:
            return existing
        definition = definitions.get(signal_id)
        kind = definition.kind if definition else None
        if kind == "boolean" or (kind is None and isinstance(value, bool)):
            encoding = SignalEncoding(id=signal_id, encoding="bool")
        elif kind == "category" or (kind is None and isinstance(value, str)):
            categories = list(definition.categories) if definition and definition.categories else []
            encoding = SignalEncoding(id=signal_id, encoding="cat", categories=categories)
        else:
            encoding = SignalEncoding(id=signal_id, encoding="num")
        encodings[signal_id] = encoding
        return encoding

    for record in records:
        signal_record = signals.records.get(record.key)
        if signal_record is None:
            continue
        position = index.get(record.key)
        if position is None:
            continue
        encoded: dict[str, Any] = {}
        for signal_id, value in signal_record.values.items():
            if value is None:
                continue
            encoding = encoding_for(signal_id, value)
            if encoding.encoding == "bool":
                encoded[signal_id] = 1 if _truthy(value) else 0
            elif encoding.encoding == "cat":
                text = str(value)
                if text not in encoding.categories:
                    encoding.categories.append(text)
                encoded[signal_id] = encoding.categories.index(text)
            else:
                number = _number(value)
                if number is None:
                    continue
                encoded[signal_id] = round(number, 6)
        if encoded:
            matrix[position] = encoded
    for definition in signals.definitions:
        if definition.id not in encodings:
            encoding_for(definition.id, None)
    return encodings, matrix


def _signal_summaries(
    signals: SignalStoreView,
    encodings: dict[str, SignalEncoding],
    matrix: dict[int, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    collected: dict[str, list[Any]] = {}
    for encoded in matrix.values():
        for signal_id, value in encoded.items():
            collected.setdefault(signal_id, []).append(value)

    summaries: dict[str, dict[str, Any]] = {}
    for signal_id, values in collected.items():
        encoding = encodings.get(signal_id)
        kind = encoding.encoding if encoding else "num"
        summary: dict[str, Any] = {"n": len(values)}
        if kind == "bool":
            true_count = sum(1 for value in values if value)
            summary["true"] = true_count
            summary["true_rate"] = round(true_count / len(values), 4) if values else 0.0
        elif kind == "cat":
            counts: dict[str, int] = {}
            categories = encoding.categories if encoding else []
            for value in values:
                position = int(value)
                name = categories[position] if 0 <= position < len(categories) else str(position)
                counts[name] = counts.get(name, 0) + 1
            summary["counts"] = counts
        else:
            numbers = sorted(float(value) for value in values)
            total = math.fsum(numbers)
            summary["sum"] = round(total, 6)
            summary["mean"] = round(total / len(numbers), 6)
            summary["min"] = round(numbers[0], 6)
            summary["max"] = round(numbers[-1], 6)
            summary["median"] = round(_percentile(numbers, 0.5), 6)
            summary["p90"] = round(_percentile(numbers, 0.9), 6)
        summaries[signal_id] = summary
    for definition in signals.definitions:
        summaries.setdefault(definition.id, {"n": 0})
    return summaries


def _quality(
    records: list[SessionRecord],
    signals: SignalStoreView,
    catalog: CatalogView,
    transcripts: dict[str, frozenset],
    tag_names: list[str],
    cost_basis_names: list[str],
) -> dict[str, Any]:
    included = [record for record in records if not record.excluded]
    excluded = [record for record in records if record.excluded]

    tag_counts: dict[str, int] = {}
    tag_examples: dict[str, list[str]] = {}
    tag_source_counts: dict[str, dict[str, int]] = {}
    for record in excluded:
        for tag in record.tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            examples = tag_examples.setdefault(tag, [])
            if len(examples) < EXAMPLE_LIMIT:
                examples.append(record.key)
            sources = tag_source_counts.setdefault(tag, {})
            for source in record.tag_sources.get(tag, []) or ["unknown"]:
                sources[source] = sources.get(source, 0) + 1

    all_tag_counts: dict[str, int] = {}
    for record in records:
        for tag in record.tags:
            all_tag_counts[tag] = all_tag_counts.get(tag, 0) + 1

    interactive = sum(1 for record in records if record.interactive is True)
    automated = sum(1 for record in records if record.interactive is False)
    unknown_interactive = len(records) - interactive - automated

    per_host: dict[str, dict[str, int]] = {}
    for record in records:
        bucket = per_host.setdefault(
            record.host,
            {"sessions": 0, "events": 0, "unknown_events": 0, "missing_transcript": 0},
        )
        bucket["sessions"] += 1
        bucket["events"] += int(record.counts.get("events", 0) or 0)
        bucket["unknown_events"] += int(record.counts.get("unknown_events", 0) or 0)
        if record.session_id not in transcripts.get(record.host, frozenset()):
            bucket["missing_transcript"] += 1

    marker_counts: dict[str, int] = {}
    for record in records:
        for marker in record.markers:
            directive = str(marker.get("directive") or "unknown")
            marker_counts[directive] = marker_counts.get(directive, 0) + 1

    known_keys = {record.key for record in records}
    priced = 0
    unpriced = 0
    basis_counts: dict[str, int] = {}
    basis_examples: dict[str, list[str]] = {}
    for record in included:
        values = signals.values(record.key)
        basis = values.get(COST_BASIS_SIGNAL)
        name = str(basis) if basis else ("none" if record.key in signals.records else "unscored")
        if values.get("usage.cost_usd") is None:
            unpriced += 1
            basis_counts[name] = basis_counts.get(name, 0) + 1
            examples = basis_examples.setdefault(name, [])
            if len(examples) < EXAMPLE_LIMIT:
                examples.append(record.key)
        else:
            priced += 1

    missing_signals = [record.key for record in included if record.key not in signals.records]
    orphan_signals = [key for key in signals.records if key not in known_keys]
    events_total = sum(int(record.counts.get("events", 0) or 0) for record in records)
    unknown_total = sum(int(record.counts.get("unknown_events", 0) or 0) for record in records)

    return {
        "sessions": len(records),
        "included": len(included),
        "excluded": len(excluded),
        "with_signals": len(known_keys & set(signals.records)),
        "stale_signals": len(signals.stale),
        "stale_examples": signals.stale[:EXAMPLE_LIMIT],
        "missing_signals": len(missing_signals),
        "missing_signal_examples": missing_signals[:EXAMPLE_LIMIT],
        "orphan_signals": len(orphan_signals),
        "events": events_total,
        "unknown_events": unknown_total,
        "unknown_rate": round(unknown_total / events_total, 6) if events_total else 0.0,
        "interactive": interactive,
        "automated": automated,
        "interactive_unknown": unknown_interactive,
        "tags": [
            {"tag": tag, "count": count}
            for tag, count in sorted(all_tag_counts.items(), key=lambda item: -item[1])
        ],
        "excluded_by_tag": [
            {
                "tag": tag,
                "count": count,
                "sources": tag_source_counts.get(tag, {}),
                "examples": tag_examples.get(tag, []),
            }
            for tag, count in sorted(tag_counts.items(), key=lambda item: -item[1])
        ],
        "markers": marker_counts,
        "hosts": [dict(values, host=host) for host, values in sorted(per_host.items())],
        "catalog": {
            "path": str(catalog.path),
            "missing": catalog.missing,
            "malformed": catalog.malformed,
        },
        "signals": {
            "definitions_path": str(signals.definitions_path),
            "sessions_path": str(signals.sessions_path),
            "missing_definitions": signals.missing_definitions,
            "missing_sessions": signals.missing_sessions,
            "malformed": signals.malformed,
            "version": signals.version,
            "count": len(signals.definitions),
        },
        "known_tags": tag_names,
        "cost": {
            "priced": priced,
            "unpriced": unpriced,
            "basis": [
                {"basis": name, "count": count, "examples": basis_examples.get(name, [])}
                for name, count in sorted(basis_counts.items(), key=lambda item: -item[1])
            ],
            "names": cost_basis_names,
        },
    }


def _sync_summary(sync: SyncView) -> dict[str, Any]:
    reports = [_shrink_sync_report(report) for report in sync.history]
    state = _sync_state_summary(sync.state)
    latest = reports[0] if reports else None
    if state is not None:
        if latest is None or _newer(state.get("started_at"), latest.get("started_at")):
            # The state file describes a newer run than any published report.
            latest = state
        elif state.get("started_at") == latest.get("started_at"):
            merged = dict(latest)
            merged["stages"] = dict(state.get("stages") or {}, **(latest.get("stages") or {}))
            for key in ("completed_at", "status"):
                if not merged.get(key) and state.get(key):
                    merged[key] = state[key]
            latest = merged
    if latest is not None:
        latest = dict(latest)
        latest.update(_sync_health(latest))
    return {
        "latest": latest,
        "history": reports,
        "directory": str(sync.directory),
        "state_path": str(sync.state_path) if sync.state_path else None,
    }


def _shrink_sync_report(report: dict[str, Any]) -> dict[str, Any]:
    failures = report.get("failures")
    failure_list = failures if isinstance(failures, list) else []
    return {
        "started_at": report.get("started_at"),
        "completed_at": report.get("completed_at"),
        "status": report.get("status"),
        "scheduled": bool(report.get("scheduled")),
        "archive_root": report.get("archive_root"),
        "dashboard_dir": report.get("dashboard_dir"),
        "free_bytes": report.get("free_bytes"),
        "imported": _count_of(report.get("imported")),
        "unchanged": _count_of(report.get("unchanged")),
        "rendered": _count_of(report.get("rendered")),
        "revisions": _count_of(report.get("revisions")),
        "pending": _count_of(report.get("pending_sessions")),
        "stages": _stage_summary(report.get("stages")),
        "failures": [
            {
                "session": str(item.get("session") or item.get("key") or "?"),
                "error": truncate(str(item.get("error") or item.get("message") or ""), 240),
            }
            for item in failure_list[:20]
            if isinstance(item, dict)
        ],
        "failure_count": len(failure_list),
        "warning": report.get("warning"),
        "skip_reason": report.get("skip_reason"),
        "signals_recomputed": bool(report.get("signals_recomputed")),
        "dashboard_rebuilt": bool(report.get("dashboard_rebuilt")),
        "report": report.get("report"),
    }


def _sync_state_summary(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not state:
        return None
    raw_last_run = state.get("last_run")
    last_run: dict[str, Any] = raw_last_run if isinstance(raw_last_run, dict) else {}
    stages = _stage_summary(state.get("stages"))
    if not stages and not last_run:
        return None
    return {
        "started_at": last_run.get("started_at"),
        "completed_at": last_run.get("completed_at"),
        "status": last_run.get("status"),
        "scheduled": bool(last_run.get("scheduled")),
        "stages": stages,
        "source": "state",
        "updated_at": state.get("updated_at"),
    }


def _stage_summary(stages: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(stages, dict):
        return {}
    summary: dict[str, dict[str, Any]] = {}
    for name, stage in stages.items():
        if not isinstance(stage, dict):
            continue
        entry: dict[str, Any] = {
            "status": str(stage.get("status") or "unknown"),
            "started_at": stage.get("started_at"),
            "completed_at": stage.get("completed_at"),
            "duration_seconds": stage.get("duration_seconds"),
            "last_success_at": stage.get("last_success_at"),
        }
        for key in ("changed_keys", "removed_keys", "total", "excluded", "rendered",
                    "recomputed", "reused", "removed", "error", "reason"):
            if stage.get(key) is not None:
                entry[key] = stage[key]
        summary[str(name)] = entry
    return summary


SYNC_BAD_STATUSES = {"failed", "error", "partial", "degraded"}
SYNC_PENDING_STATUSES = {"running", "pending", "in_progress"}


def _sync_health(report: dict[str, Any]) -> dict[str, Any]:
    """Health of the run this dashboard was built from.

    The dashboard stage runs inside the sync, so the run report is normally
    still ``running`` when it is read — and this generation stays published
    until the next rebuild. A run whose finished stages all succeeded is
    therefore reported as healthy and *past*, never as "in progress"; callers
    use ``built_during_sync`` to say so.
    """
    stages = report.get("stages") or {}
    failed = sorted(
        name for name, stage in stages.items() if str(stage.get("status")) in SYNC_BAD_STATUSES
    )
    unfinished = sorted(
        name
        for name, stage in stages.items()
        if str(stage.get("status")) in {"running", "in_progress"}
    )
    succeeded = sorted(
        name for name, stage in stages.items() if str(stage.get("status")) in {"success", "ok", "skipped"}
    )
    status = str(report.get("status") or "unknown")
    if failed or status in {"failed", "error"}:
        health = "failed"
    elif status in SYNC_BAD_STATUSES or report.get("failure_count"):
        health = "degraded"
    else:
        health = "ok"
    return {
        "health": health,
        "built_during_sync": status in SYNC_PENDING_STATUSES or bool(unfinished),
        "stages_failed": failed,
        "stages_unfinished": unfinished,
        "stages_ok": succeeded,
        "observed_at": report.get("completed_at") or report.get("started_at"),
    }


def _newer(left: Any, right: Any) -> bool:
    left_epoch = parse_iso(left if isinstance(left, str) else None)
    right_epoch = parse_iso(right if isinstance(right, str) else None)
    if left_epoch is None:
        return False
    if right_epoch is None:
        return True
    return left_epoch > right_epoch


def _rendered_session_ids(layout: Layout, hosts: Iterable[str]) -> dict[str, frozenset]:
    result: dict[str, frozenset] = {}
    for host in hosts:
        directory = layout.root / "rendered" / host
        names: set[str] = set()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.endswith(".md"):
                        names.add(entry.name[: -len(".md")])
        except (OSError, ValueError):
            pass
        result[host] = frozenset(names)
    return result


def _bucket_count(session_count: int, *, target: int = DETAIL_BUCKET_TARGET) -> int:
    if session_count <= target:
        return 1
    exponent = max(0, math.ceil(math.log(session_count / target, 2)))
    return min(MAX_DETAIL_BUCKETS, 2 ** exponent)


def _bucket_index(key: str, bucket_count: int) -> int:
    if bucket_count <= 1:
        return 0
    return zlib.crc32(key.encode("utf-8")) % bucket_count


def _epoch_seconds(value: str | None) -> int | None:
    epoch = parse_iso(value)
    return int(epoch) if epoch is not None else None


def _duration(primary: float | None, fallback: Any) -> int | None:
    if primary is not None:
        return int(round(primary))
    number = _number(fallback)
    return int(round(number)) if number is not None else None


def _integer(*candidates: Any) -> int | None:
    for candidate in candidates:
        number = _number(candidate)
        if number is not None:
            return int(round(number))
    return None


def _scaled(value: Any, scale: int) -> int | None:
    number = _number(value)
    return int(round(number * scale)) if number is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _category_index(interner: _Interner, value: Any) -> int:
    if value is None:
        return -1
    text = str(value).strip()
    if not text:
        return -1
    return interner.index(text)


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[int(position)]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _count_of(value: Any) -> int:
    """Run reports store these as a list of keys or, since sync v2, a count."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, (list, tuple, dict)):
        return len(value)
    return 0


def excluded_detail_payload(model: DashboardModel, key: str) -> dict[str, Any]:
    """Catalog-only payload for excluded sessions.

    Everything the fact row already carries (title, agent, client, project,
    timings, tags, artifact paths) is omitted; the browser merges the row in.
    """
    record = model.records[key]
    payload: dict[str, Any] = {"minimal": True, "counts": record.counts}
    cwd = record.cwd_resolved or record.cwd
    project_path = record.project.id[5:] if record.project.id.startswith("path:") else None
    if cwd and cwd != project_path:
        payload["cwd"] = cwd
    if record.ended_at:
        payload["ended_at"] = record.ended_at
    if record.tag_sources:
        payload["tag_sources"] = record.tag_sources
    if record.markers:
        payload["markers"] = record.markers
    evidence = record.project.evidence
    if evidence and evidence != cwd:
        payload["project_evidence"] = truncate(evidence, 160)
    return payload


def detail_payload(model: DashboardModel, key: str) -> dict[str, Any]:
    """Full per-session payload; artifact paths are archive-relative."""
    record = model.records[key]
    signal_record = model.signals.records.get(key)
    payload: dict[str, Any] = {
        "key": key,
        "host": record.host,
        "session_id": record.session_id,
        "agent": record.agent,
        "client": record.client,
        "interactive": record.interactive,
        "is_subagent": record.is_subagent,
        "parent": record.parent_key,
        "children": model.children.get(key, []),
        "title": record.title,
        "prompt": truncate(
            record.first_prompt or "",
            EXCLUDED_PROMPT_LIMIT if record.excluded else DETAIL_PROMPT_LIMIT,
        ),
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "duration_s": record.duration_s,
        "active_s": record.active_s,
        "cwd": record.cwd,
        "cwd_resolved": record.cwd_resolved,
        "git": record.git,
        "project": {
            "id": record.project.id,
            "name": record.project.name,
            "source": record.project.source,
            "evidence": record.project.evidence,
        },
        "models": record.models,
        "counts": record.counts,
        "markers": record.markers,
        "tags": record.tags,
        "tag_sources": record.tag_sources,
        "excluded": record.excluded,
        "fingerprint": record.fingerprint,
        "paths": _artifact_paths(record),
    }
    if signal_record is not None:
        payload["signals"] = {
            "version": signal_record.version,
            "values": signal_record.values,
            "evidence": {
                signal_id: list(events)[:5] for signal_id, events in signal_record.evidence.items() if events
            },
            "confidence": signal_record.confidence,
        }
    return payload


def _artifact_paths(record: SessionRecord) -> dict[str, str]:
    return {
        "transcript": f"rendered/{record.host}/{record.session_id}.md",
        "normalized": f"normalized/{record.host}/{record.session_id}.events.jsonl",
        "raw": f"raw/{record.host}/{record.session_id}",
    }
