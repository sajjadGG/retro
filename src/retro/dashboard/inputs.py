"""Readers for the on-disk contracts the dashboard consumes.

The dashboard never imports ``retro.catalog`` or ``retro.signals``: it reads
their published files so a build can run against any archive, including one
produced by a different version of those packages.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..storage import Layout
from ..utils import iter_jsonl

CATALOG_RELPATH = "catalog/sessions.jsonl"
SIGNAL_DEFINITIONS_RELPATH = "signals/definitions.json"
SIGNAL_SESSIONS_RELPATH = "signals/sessions.jsonl"
SYNC_RUNS_RELDIR = "sync/runs"
SYNC_STATE_RELPATH = "state/sync.json"


def catalog_path(layout: Layout) -> Path:
    return layout.root / "catalog" / "sessions.jsonl"


def signal_definitions_path(layout: Layout) -> Path:
    return layout.root / "signals" / "definitions.json"


def signal_sessions_path(layout: Layout) -> Path:
    return layout.root / "signals" / "sessions.jsonl"


def sync_runs_dir(layout: Layout) -> Path:
    return layout.root / "sync" / "runs"


def sync_state_path(layout: Layout) -> Path:
    return layout.root / "state" / "sync.json"


@dataclass(frozen=True)
class ProjectRef:
    id: str
    name: str
    source: str
    evidence: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> ProjectRef:
        if not isinstance(value, dict):
            return cls(id="unknown", name="unknown", source="unknown", evidence=None)
        project_id = _as_str(value.get("id")) or "unknown"
        return cls(
            id=project_id,
            name=_as_str(value.get("name")) or project_id,
            source=_as_str(value.get("source")) or "unknown",
            evidence=_as_str(value.get("evidence")),
        )


@dataclass
class SessionRecord:
    """A §3 catalog record, defensively decoded."""

    key: str
    host: str
    session_id: str
    agent: str
    client: str
    interactive: bool | None
    is_subagent: bool
    parent_key: str | None
    title: str | None
    first_prompt: str | None
    started_at: str | None
    ended_at: str | None
    duration_s: float | None
    active_s: float | None
    cwd: str | None
    cwd_resolved: str | None
    git: dict[str, Any]
    project: ProjectRef
    models: list[str]
    counts: dict[str, int]
    markers: list[dict[str, Any]]
    tags: list[str]
    tag_sources: dict[str, list[str]]
    excluded: bool
    fingerprint: str

    @property
    def started_epoch(self) -> float | None:
        return parse_iso(self.started_at)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SessionRecord | None:
        key = _as_str(value.get("key"))
        if not key:
            return None
        host = _as_str(value.get("host")) or key.split("/", 1)[0]
        session_id = _as_str(value.get("session_id")) or key.split("/", 1)[-1]
        return cls(
            key=key,
            host=host,
            session_id=session_id,
            agent=_as_str(value.get("agent")) or host,
            client=_as_str(value.get("client")) or "unknown",
            interactive=_as_bool(value.get("interactive")),
            is_subagent=bool(value.get("is_subagent")),
            parent_key=_as_str(value.get("parent_key")),
            title=_as_str(value.get("title")),
            first_prompt=_as_str(value.get("first_prompt")),
            started_at=_as_str(value.get("started_at")),
            ended_at=_as_str(value.get("ended_at")),
            duration_s=_as_float(value.get("duration_s")),
            active_s=_as_float(value.get("active_s")),
            cwd=_as_str(value.get("cwd")),
            cwd_resolved=_as_str(value.get("cwd_resolved")),
            git=_as_dict(value.get("git")),
            project=ProjectRef.from_value(value.get("project")),
            models=_as_str_list(value.get("models")),
            counts=_as_int_map(value.get("counts")),
            markers=[item for item in _as_list(value.get("markers")) if isinstance(item, dict)],
            tags=_as_str_list(value.get("tags")),
            tag_sources=_as_str_list_map(value.get("tag_sources")),
            excluded=bool(value.get("excluded")),
            fingerprint=_as_str(value.get("fingerprint")) or "",
        )


@dataclass
class CatalogView:
    path: Path
    records: list[SessionRecord] = field(default_factory=list)
    missing: bool = False
    malformed: int = 0

    def __len__(self) -> int:
        return len(self.records)

    def by_key(self) -> dict[str, SessionRecord]:
        return {record.key: record for record in self.records}


@dataclass(frozen=True)
class SignalDefinition:
    id: str
    title: str
    category: str
    kind: str
    unit: str | None
    polarity: str
    method: str
    version: int
    description: str
    categories: list[str] | None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SignalDefinition | None:
        signal_id = _as_str(value.get("id"))
        if not signal_id:
            return None
        categories = _as_str_list(value.get("categories")) if value.get("categories") else None
        return cls(
            id=signal_id,
            title=_as_str(value.get("title")) or signal_id,
            category=_as_str(value.get("category")) or "other",
            kind=_as_str(value.get("kind")) or "number",
            unit=_as_str(value.get("unit")),
            polarity=_as_str(value.get("polarity")) or "neutral",
            method=_as_str(value.get("method")) or "heuristic",
            version=int(_as_float(value.get("version")) or 1),
            description=_as_str(value.get("description")) or "",
            categories=categories or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "kind": self.kind,
            "unit": self.unit,
            "polarity": self.polarity,
            "method": self.method,
            "version": self.version,
            "description": self.description,
            "categories": self.categories,
        }


@dataclass
class SignalRecord:
    key: str
    fingerprint: str
    version: str
    values: dict[str, Any]
    evidence: dict[str, list[str]]
    confidence: dict[str, float]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SignalRecord | None:
        key = _as_str(value.get("key"))
        if not key:
            return None
        values = value.get("values") if isinstance(value.get("values"), dict) else {}
        confidence: dict[str, float] = {}
        raw_confidence = value.get("confidence")
        if isinstance(raw_confidence, dict):
            for name, number in raw_confidence.items():
                parsed = _as_float(number)
                if parsed is not None:
                    confidence[str(name)] = parsed
        return cls(
            key=key,
            fingerprint=_as_str(value.get("fingerprint")) or "",
            version=_as_str(value.get("version")) or "",
            values=dict(values or {}),
            evidence=_as_str_list_map(value.get("evidence")),
            confidence=confidence,
        )


@dataclass
class SignalStoreView:
    definitions_path: Path
    sessions_path: Path
    version: str = ""
    definitions: list[SignalDefinition] = field(default_factory=list)
    records: dict[str, SignalRecord] = field(default_factory=dict)
    missing_definitions: bool = False
    missing_sessions: bool = False
    malformed: int = 0
    stale: list[str] = field(default_factory=list)

    def definition(self, signal_id: str) -> SignalDefinition | None:
        for definition in self.definitions:
            if definition.id == signal_id:
                return definition
        return None

    def values(self, key: str) -> dict[str, Any]:
        record = self.records.get(key)
        return record.values if record else {}


@dataclass
class SyncView:
    directory: Path
    latest: dict[str, Any] | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] | None = None
    state_path: Path | None = None


def read_catalog(layout: Layout, *, path: Path | None = None) -> CatalogView:
    target = path or catalog_path(layout)
    view = CatalogView(path=target)
    if not target.exists():
        view.missing = True
        return view
    for _, payload in iter_jsonl(target):
        record = SessionRecord.from_dict(payload)
        if record is None:
            view.malformed += 1
            continue
        view.records.append(record)
    return view


def read_signals(
    layout: Layout,
    *,
    definitions_path: Path | None = None,
    sessions_path: Path | None = None,
) -> SignalStoreView:
    definitions_target = definitions_path or signal_definitions_path(layout)
    sessions_target = sessions_path or signal_sessions_path(layout)
    view = SignalStoreView(definitions_path=definitions_target, sessions_path=sessions_target)

    document = _read_json(definitions_target)
    if document is None:
        view.missing_definitions = True
    else:
        view.version = _as_str(document.get("version")) or ""
        for entry in _as_list(document.get("signals")):
            if not isinstance(entry, dict):
                continue
            definition = SignalDefinition.from_dict(entry)
            if definition is not None:
                view.definitions.append(definition)

    if not sessions_target.exists():
        view.missing_sessions = True
        return view
    for _, payload in iter_jsonl(sessions_target):
        record = SignalRecord.from_dict(payload)
        if record is None:
            view.malformed += 1
            continue
        view.records[record.key] = record
    return view


def read_sync_runs(layout: Layout, *, limit: int = 8) -> SyncView:
    directory = sync_runs_dir(layout)
    view = SyncView(directory=directory)
    state_target = sync_state_path(layout)
    if state_target.exists():
        view.state_path = state_target
        view.state = _read_json(state_target)
    if not directory.exists():
        return view
    paths = sorted(directory.glob("*.json"))
    for path in reversed(paths[-limit:]):
        document = _read_json(path)
        if document is None:
            continue
        document.setdefault("report", path.name)
        view.history.append(document)
    if view.history:
        view.latest = view.history[0]
    return view


def mark_stale_signals(catalog: CatalogView, signals: SignalStoreView) -> None:
    """Record keys whose signal record no longer matches the catalog fingerprint."""
    stale: list[str] = []
    for record in catalog.records:
        signal_record = signals.records.get(record.key)
        if signal_record is None:
            continue
        if not record.fingerprint or not signal_record.fingerprint:
            continue
        if signal_record.fingerprint != record.fingerprint:
            stale.append(record.key)
    signals.stale = stale


def parse_iso(value: str | None) -> float | None:
    """Return a UTC epoch timestamp for an ISO-8601 string, or None."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return {str(name): item for name, item in value.items()} if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_str_list(value: Any) -> list[str]:
    return [item for item in _as_list(value) if isinstance(item, str) and item]


def _as_str_list_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, list[str]] = {}
    for name, entries in value.items():
        if isinstance(entries, list):
            result[str(name)] = [str(item) for item in entries if isinstance(item, (str, int, float))]
        elif isinstance(entries, str):
            result[str(name)] = [entries]
    return result


def _as_int_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for name, number in value.items():
        parsed = _as_float(number)
        if parsed is not None:
            result[str(name)] = int(parsed)
    return result


def iter_records(catalog: CatalogView, *, included_only: bool = False) -> Iterator[SessionRecord]:
    for record in catalog.records:
        if included_only and record.excluded:
            continue
        yield record


def unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value not in seen:
            seen[value] = None
    return list(seen)
