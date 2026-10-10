"""Dashboard build: catalog + signals in, static site out."""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..storage import Layout
from .facts import (
    COLUMNS,
    FLAGS,
    HEADLINE_SIGNALS,
    DashboardModel,
    build_model,
)
from .inputs import (
    CatalogView,
    SignalStoreView,
    SyncView,
    mark_stale_signals,
    read_catalog,
    read_signals,
    read_sync_runs,
)
from .shards import SESSION_SHARD_ROWS, ShardWriter, write_js

SCHEMA = 1
BUILD_MODES = ("auto", "full")
_LEGACY_MODES = {"incremental": "auto", "calculate": "auto", "display": "auto"}
ASSETS_DIR = Path(__file__).resolve().parent / "assets"


@dataclass
class DashboardInputs:
    layout: Layout
    catalog: CatalogView
    signals: SignalStoreView
    sync: SyncView
    model: DashboardModel


@dataclass
class BuildReport:
    index_path: Path
    output_dir: Path
    mode: str
    sessions: int = 0
    included: int = 0
    excluded: int = 0
    with_signals: int = 0
    signal_definitions: int = 0
    session_shards: int = 0
    signal_shards: int = 0
    detail_shards: int = 0
    detail_cached: int = 0
    detail_written: int = 0
    bytes_total: int = 0
    index_bytes: int = 0
    duration_s: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_path": str(self.index_path),
            "output_dir": str(self.output_dir),
            "mode": self.mode,
            "sessions": self.sessions,
            "included": self.included,
            "excluded": self.excluded,
            "with_signals": self.with_signals,
            "signal_definitions": self.signal_definitions,
            "session_shards": self.session_shards,
            "signal_shards": self.signal_shards,
            "detail_shards": self.detail_shards,
            "detail_cached": self.detail_cached,
            "detail_written": self.detail_written,
            "bytes_total": self.bytes_total,
            "index_bytes": self.index_bytes,
            "duration_s": round(self.duration_s, 3),
            "warnings": self.warnings,
        }


def normalize_mode(mode: str) -> str:
    resolved = _LEGACY_MODES.get(mode, mode)
    if resolved not in BUILD_MODES:
        raise ValueError(f"mode must be one of: {', '.join(BUILD_MODES)}")
    return resolved


def build(
    layout: Layout,
    output_dir: Path,
    *,
    mode: str = "auto",
    cache_dir: Path | None = None,
) -> BuildReport:
    """Render the static dashboard into *output_dir* (which may be a staging dir)."""
    resolved_mode = normalize_mode(mode)
    started = time.monotonic()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    inputs = load_inputs(layout)
    catalog, signals, model = inputs.catalog, inputs.signals, inputs.model

    copy_assets(output_dir)
    data_dir = output_dir / "data"
    writer = ShardWriter(model, data_dir, cache_dir=cache_dir, mode=resolved_mode)
    shard_report = writer.write_all()

    warnings = input_warnings(catalog, signals)
    manifest = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": resolved_mode,
        "archive": {
            "root": str(layout.root),
            "url": _file_url(layout.root),
            "rendered": "rendered",
        },
        "columns": list(COLUMNS),
        "flags": dict(FLAGS),
        "shard_rows": SESSION_SHARD_ROWS,
        "dict": model.dictionaries.to_dict(),
        "counts": {
            "sessions": len(model.rows),
            "included": model.quality["included"],
            "excluded": model.quality["excluded"],
            "with_signals": model.quality["with_signals"],
            "detail_buckets": model.bucket_count,
        },
        "shards": {
            "sessions": writer.session_shards,
            "signals": writer.signal_shards,
            "detail": {"pattern": "data/detail/d-%04d.js", "buckets": model.bucket_count},
        },
        "signals": {
            "version": signals.version,
            "headline": list(HEADLINE_SIGNALS),
            "definitions": [definition.to_dict() for definition in model.definitions],
            "encodings": {
                signal_id: {"encoding": encoding.encoding, "categories": encoding.categories}
                for signal_id, encoding in sorted(model.encodings.items())
            },
            "summary": model.summaries,
        },
        "quality": model.quality,
        "sync": model.sync,
        "warnings": warnings,
    }
    write_js(data_dir / "manifest.js", "manifest", manifest)

    index_path = output_dir / "index.html"
    report = BuildReport(
        index_path=index_path,
        output_dir=output_dir,
        mode=resolved_mode,
        sessions=len(model.rows),
        included=model.quality["included"],
        excluded=model.quality["excluded"],
        with_signals=model.quality["with_signals"],
        signal_definitions=len(model.definitions),
        session_shards=shard_report.session_shards,
        signal_shards=shard_report.signal_shards,
        detail_shards=shard_report.detail_shards,
        detail_cached=shard_report.detail_cached,
        detail_written=shard_report.detail_written,
        warnings=warnings,
    )
    report.index_bytes = index_path.stat().st_size if index_path.exists() else 0
    report.bytes_total = directory_size(output_dir)
    report.duration_s = time.monotonic() - started
    write_js(data_dir / "build.js", "build", report.to_dict())
    return report


def load_inputs(layout: Layout) -> DashboardInputs:
    """Read the catalog, signal store and sync reports, then build the fact table."""
    catalog = read_catalog(layout)
    signals = read_signals(layout)
    mark_stale_signals(catalog, signals)
    sync = read_sync_runs(layout)
    model = build_model(layout, catalog, signals, sync)
    return DashboardInputs(layout=layout, catalog=catalog, signals=signals, sync=sync, model=model)


def copy_assets(output_dir: Path) -> None:
    assets_out = output_dir / "assets"
    assets_out.mkdir(parents=True, exist_ok=True)
    for source in sorted(ASSETS_DIR.iterdir()):
        if not source.is_file() or source.name.startswith("."):
            continue
        destination = output_dir / source.name if source.name == "index.html" else assets_out / source.name
        shutil.copyfile(source, destination)


def directory_size(path: Path) -> int:
    """Bytes published by a build, ignoring caches and other dot directories."""
    total = 0
    for item in path.rglob("*"):
        if any(part.startswith(".") for part in item.relative_to(path).parts):
            continue
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
            elif item.is_symlink():
                total += item.lstat().st_size
        except OSError:
            continue
    return total


def input_warnings(catalog: CatalogView, signals: SignalStoreView) -> list[str]:
    warnings: list[str] = []
    if catalog.missing:
        warnings.append(
            f"catalog missing at {catalog.path}; run `retro catalog update` to populate the dashboard"
        )
    elif not catalog.records:
        warnings.append("catalog is empty; no sessions to show")
    if signals.missing_definitions or signals.missing_sessions:
        warnings.append(
            "signal store missing; run `retro signal update` for outcomes, cost and friction metrics"
        )
    if signals.stale:
        warnings.append(f"{len(signals.stale)} sessions have signals older than their catalog fingerprint")
    if catalog.malformed:
        warnings.append(f"{catalog.malformed} malformed catalog lines were skipped")
    if signals.malformed:
        warnings.append(f"{signals.malformed} malformed signal lines were skipped")
    return warnings


def _file_url(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except ValueError:
        return ""
