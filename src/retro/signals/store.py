"""The on-disk signal store: `signals/definitions.json` + `signals/sessions.jsonl`.

One compact line per included session, keyed by the catalog fingerprint and the
digest of the registered signal set. A session is recomputed only when its
inputs changed or the signal set changed; sessions that left the catalog are
dropped. The v1 layout (`readings.jsonl`, `aggregates.json`, `summary.md`) was
one row per (session, signal) — 600 MB for this archive — and is deleted on the
first write of the new store.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..schema import read_events
from ..storage import Layout
from ..utils import atomic_write_text
from .pricing import PricingMap, default_pricing
from .profile import SessionProfile, build_profile
from .registry import definitions_payload, definitions_version, evaluate
from .sources import SessionSource, record_attr, resolve_source, session_key

SCHEMA = 2
DEFINITIONS_NAME = "definitions.json"
SESSIONS_NAME = "sessions.jsonl"
LEGACY_NAMES = ("readings.jsonl", "aggregates.json", "summary.md")


def signals_dir(layout: Layout) -> Path:
    return layout.root / "signals"


@dataclass
class SignalUpdate:
    version: str
    total: int = 0
    added_keys: list[str] = field(default_factory=list)
    changed_keys: list[str] = field(default_factory=list)
    removed_keys: list[str] = field(default_factory=list)
    reused: int = 0
    rewritten: bool = True
    duration_s: float = 0.0

    @property
    def recomputed(self) -> int:
        return len(self.added_keys) + len(self.changed_keys)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "total": self.total,
            "added": len(self.added_keys),
            "changed": len(self.changed_keys),
            "removed": len(self.removed_keys),
            "reused": self.reused,
            "rewritten": self.rewritten,
            "duration_s": round(self.duration_s, 3),
        }


class SignalStore:
    """Read view over the store; cheap to construct, no recomputation."""

    def __init__(
        self,
        definitions: Sequence[Mapping[str, Any]],
        version: str,
        records: Mapping[str, Mapping[str, Any]],
    ):
        self.definitions: list[dict[str, Any]] = [dict(item) for item in definitions]
        self.version = version
        self._records: dict[str, dict[str, Any]] = {k: dict(v) for k, v in records.items()}

    def __len__(self) -> int:
        return len(self._records)

    def __contains__(self, key: object) -> bool:
        return key in self._records

    @property
    def keys(self) -> list[str]:
        return sorted(self._records)

    def record(self, key: str) -> dict[str, Any] | None:
        return self._records.get(key)

    def values(self, key: str) -> dict[str, Any]:
        record = self._records.get(key)
        return dict(record.get("values") or {}) if record else {}

    def evidence(self, key: str) -> dict[str, list[str]]:
        record = self._records.get(key)
        return dict(record.get("evidence") or {}) if record else {}

    def confidence(self, key: str) -> dict[str, float]:
        record = self._records.get(key)
        return dict(record.get("confidence") or {}) if record else {}

    def records(self) -> Iterator[dict[str, Any]]:
        for key in sorted(self._records):
            yield self._records[key]

    def all_values(self) -> Iterator[dict[str, Any]]:
        for record in self.records():
            yield dict(record.get("values") or {})

    def definition(self, signal_id: str) -> dict[str, Any] | None:
        for item in self.definitions:
            if item.get("id") == signal_id:
                return item
        return None

    def is_current(self) -> bool:
        return self.version == definitions_version()


def load_signal_store(layout: Layout) -> SignalStore:
    """Load the store from disk. Missing files yield an empty store."""
    directory = signals_dir(layout)
    definitions: list[dict[str, Any]] = []
    version = ""
    definitions_path = directory / DEFINITIONS_NAME
    if definitions_path.exists():
        try:
            payload = json.loads(definitions_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        if isinstance(payload, dict):
            version = str(payload.get("version") or "")
            raw = payload.get("signals")
            if isinstance(raw, list):
                definitions = [item for item in raw if isinstance(item, dict)]

    records: dict[str, dict[str, Any]] = {}
    sessions_path = directory / SESSIONS_NAME
    if sessions_path.exists():
        with sessions_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = record.get("key")
                if isinstance(key, str) and key:
                    records[key] = record
    return SignalStore(definitions, version, records)


def update_signals(
    layout: Layout,
    catalog: Any = None,
    *,
    force: bool = False,
    host: str | None = None,
    session_ids: Sequence[str] | None = None,
    prune: bool = True,
    pricing: PricingMap | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> SignalUpdate:
    """Recompute signals for sessions whose fingerprint or signal set changed.

    `prune=False` keeps stored sessions the source did not enumerate, so a
    filtered run (`--host`, `--session-id`) updates a slice without discarding
    the rest of the store.
    """
    started = time.monotonic()
    version = definitions_version()
    source: SessionSource = resolve_source(layout, catalog, host=host, session_ids=session_ids)
    existing = load_signal_store(layout)
    pricing_map = pricing or default_pricing()

    update = SignalUpdate(version=version)
    records: dict[str, dict[str, Any]] = {}
    seen = set()

    for index, record in enumerate(source.included(), start=1):
        key = session_key(record)
        if not key or key == "/":
            continue
        seen.add(key)
        fingerprint = str(record_attr(record, "fingerprint", ""))
        previous = existing.record(key)
        if (
            not force
            and previous is not None
            and previous.get("fingerprint") == fingerprint
            and previous.get("version") == version
        ):
            records[key] = previous
            update.reused += 1
            continue
        records[key] = _compute(layout, record, key, fingerprint, version, pricing_map)
        if previous is None:
            update.added_keys.append(key)
        else:
            update.changed_keys.append(key)
        if progress is not None:
            progress(index, key)

    stale = sorted(set(existing.keys) - seen)
    if prune:
        update.removed_keys = stale
    else:
        for key in stale:
            previous = existing.record(key)
            if previous is not None:
                records[key] = previous
    update.total = len(records)
    if _store_is_fresh(layout, existing, version, update):
        update.rewritten = False
    else:
        _write_store(layout, version, records)
        update.rewritten = True
    update.duration_s = time.monotonic() - started
    return update


def _store_is_fresh(
    layout: Layout, existing: SignalStore, version: str, update: SignalUpdate
) -> bool:
    """True when the files on disk already say exactly what we would write.

    A no-op sync must not touch them: the dashboard keys its caches on input
    mtimes, so rewriting identical bytes invalidates work for nothing.
    """
    if update.added_keys or update.changed_keys or update.removed_keys:
        return False
    if existing.version != version:
        return False
    directory = signals_dir(layout)
    if not (directory / DEFINITIONS_NAME).exists() or not (directory / SESSIONS_NAME).exists():
        return False
    if any((directory / name).exists() for name in LEGACY_NAMES):
        return False
    return existing.definitions == definitions_payload()


def _compute(
    layout: Layout,
    record: Any,
    key: str,
    fingerprint: str,
    version: str,
    pricing: PricingMap,
) -> dict[str, Any]:
    host = str(record_attr(record, "host", ""))
    session_id = str(record_attr(record, "session_id", ""))
    path = layout.normalized_path(host, session_id)
    events = list(read_events(path)) if path.exists() else []
    profile = build_profile(
        key=key,
        host=host,
        session_id=session_id,
        events=events,
        raw_dir=layout.raw_dir(host, session_id),
        record=record,
        pricing=pricing,
    )
    return record_from_profile(profile, fingerprint, version)


def record_from_profile(profile: SessionProfile, fingerprint: str, version: str) -> dict[str, Any]:
    values, evidence, confidence = evaluate(profile)
    record: dict[str, Any] = {
        "key": profile.key,
        "fingerprint": fingerprint,
        "version": version,
        "values": values,
    }
    if evidence:
        record["evidence"] = evidence
    if confidence:
        record["confidence"] = confidence
    return record


def _write_store(layout: Layout, version: str, records: Mapping[str, Mapping[str, Any]]) -> None:
    directory = signals_dir(layout)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        directory / DEFINITIONS_NAME,
        json.dumps(
            {"schema": SCHEMA, "version": version, "signals": definitions_payload()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    _atomic_write_lines(
        directory / SESSIONS_NAME,
        (
            json.dumps(records[key], ensure_ascii=False, separators=(",", ":")) + "\n"
            for key in sorted(records)
        ),
    )
    for name in LEGACY_NAMES:
        legacy = directory / name
        if legacy.exists():
            legacy.unlink()


def _atomic_write_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
