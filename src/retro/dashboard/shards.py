"""Shard writers.

Every shard is a plain ``.js`` file that calls ``RETRO_DATA(name, payload)``.
That keeps the dashboard loadable from ``file://`` (where ``fetch`` and ES
modules are blocked) while still letting the shell load data lazily.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import atomic_write_text
from .facts import COLUMN_INDEX, DashboardModel, detail_payload

SESSION_SHARD_ROWS = 2000
_KEY = COLUMN_INDEX["key"]
_STARTED = COLUMN_INDEX["started"]
_BUCKET = COLUMN_INDEX["bucket"]
CACHE_DIRNAME = ".retro-cache"
DETAIL_DIRNAME = "detail"


@dataclass
class ShardReport:
    session_shards: int = 0
    signal_shards: int = 0
    detail_shards: int = 0
    detail_cached: int = 0
    detail_written: int = 0
    files: list[str] = field(default_factory=list)
    bytes_written: int = 0

    def record(self, path: Path, root: Path) -> None:
        self.files.append(path.relative_to(root).as_posix())
        try:
            self.bytes_written += path.stat().st_size
        except OSError:
            pass


def js_payload(name: str, payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return "RETRO_DATA(" + json.dumps(name) + "," + text + ");\n"


def write_js(path: Path, name: str, payload: Any) -> None:
    """Write a shard into a staging directory (publication, not the file, is atomic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(js_payload(name, payload), encoding="utf-8")


class ShardWriter:
    def __init__(
        self,
        model: DashboardModel,
        data_dir: Path,
        *,
        cache_dir: Path | None = None,
        mode: str = "auto",
    ) -> None:
        self.model = model
        self.data_dir = data_dir
        self.cache_dir = cache_dir
        self.mode = mode
        self.report = ShardReport()
        self.session_shards: list[dict[str, Any]] = []
        self.signal_shards: list[dict[str, Any]] = []

    def write_all(self) -> ShardReport:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.write_sessions()
        self.write_signal_matrix()
        self.write_details()
        return self.report

    def write_sessions(self) -> None:
        rows = self.model.rows
        total = len(rows)
        shard_count = max(1, (total + SESSION_SHARD_ROWS - 1) // SESSION_SHARD_ROWS)
        for shard in range(shard_count):
            start = shard * SESSION_SHARD_ROWS
            chunk = rows[start : start + SESSION_SHARD_ROWS]
            path = self.data_dir / f"sessions-{shard:03d}.js"
            write_js(path, f"sessions:{shard}", {"shard": shard, "offset": start, "rows": chunk})
            self.report.record(path, self.data_dir.parent)
            started = [row[_STARTED] for row in chunk if row[_STARTED] is not None]
            self.session_shards.append(
                {
                    "file": f"data/{path.name}",
                    "shard": shard,
                    "offset": start,
                    "rows": len(chunk),
                    "from": min(started) if started else None,
                    "to": max(started) if started else None,
                }
            )
        self.report.session_shards = shard_count

    def write_signal_matrix(self) -> None:
        matrix = self.model.matrix
        if not matrix:
            return
        highest = max(matrix) if matrix else 0
        shard_count = highest // SESSION_SHARD_ROWS + 1
        for shard in range(shard_count):
            start = shard * SESSION_SHARD_ROWS
            columns: dict[str, list[Any]] = {}
            for position in range(start, start + SESSION_SHARD_ROWS):
                encoded = matrix.get(position)
                if not encoded:
                    continue
                local = position - start
                for signal_id, value in encoded.items():
                    column = columns.setdefault(signal_id, [])
                    column.append(local)
                    column.append(value)
            if not columns:
                continue
            path = self.data_dir / f"signals-{shard:03d}.js"
            write_js(
                path,
                f"signals:{shard}",
                {"shard": shard, "offset": start, "signals": columns},
            )
            self.report.record(path, self.data_dir.parent)
            self.signal_shards.append({"file": f"data/{path.name}", "shard": shard, "offset": start})
        self.report.signal_shards = len(self.signal_shards)

    def write_details(self) -> None:
        bucket_count = self.model.bucket_count
        members: dict[int, list[str]] = {}
        for row in self.model.rows:
            members.setdefault(int(row[_BUCKET]), []).append(row[_KEY])

        cache = _DetailCache(self.cache_dir, bucket_count, self.model.signals.version)
        if self.mode == "full":
            cache.reset()
        target_dir = self.data_dir / DETAIL_DIRNAME
        target_dir.mkdir(parents=True, exist_ok=True)

        for bucket in range(bucket_count):
            keys = sorted(members.get(bucket, []))
            if not keys:
                continue
            signature = _bucket_signature(keys, self.model.fingerprints)
            name = f"d-{bucket:04d}.js"
            destination = target_dir / name
            cached = cache.take(bucket, signature, destination)
            if cached:
                self.report.detail_cached += 1
            else:
                payload = {
                    "bucket": bucket,
                    "sessions": {key: detail_payload(self.model, key) for key in keys},
                }
                text = js_payload(f"detail:{bucket}", payload)
                cache.store(bucket, signature, name, text, destination)
                self.report.detail_written += 1
            self.report.record(destination, self.data_dir.parent)
            self.report.detail_shards += 1
        cache.flush()


class _DetailCache:
    """Reuses previously rendered detail buckets whose member fingerprints match."""

    def __init__(self, directory: Path | None, bucket_count: int, signals_version: str) -> None:
        self.directory = directory
        self.bucket_count = bucket_count
        self.signals_version = signals_version
        self.entries: dict[str, str] = {}
        self.used: set[str] = set()
        if directory is None:
            return
        directory.mkdir(parents=True, exist_ok=True)
        index = _read_json(directory / "index.json")
        if (
            index
            and index.get("bucket_count") == bucket_count
            and index.get("signals_version") == signals_version
            and isinstance(index.get("buckets"), dict)
        ):
            self.entries = {str(key): str(value) for key, value in index["buckets"].items()}

    def reset(self) -> None:
        self.entries = {}

    def take(self, bucket: int, signature: str, destination: Path) -> bool:
        if self.directory is None:
            return False
        if self.entries.get(str(bucket)) != signature:
            return False
        source = self.directory / f"d-{bucket:04d}.js"
        if not source.exists():
            return False
        _link_or_copy(source, destination)
        self.used.add(str(bucket))
        return True

    def store(self, bucket: int, signature: str, name: str, text: str, destination: Path) -> None:
        if self.directory is None:
            atomic_write_text(destination, text)
            return
        source = self.directory / name
        atomic_write_text(source, text)
        _link_or_copy(source, destination)
        self.entries[str(bucket)] = signature
        self.used.add(str(bucket))

    def flush(self) -> None:
        if self.directory is None:
            return
        for bucket in list(self.entries):
            if bucket not in self.used:
                self.entries.pop(bucket, None)
                stale = self.directory / f"d-{int(bucket):04d}.js"
                if stale.exists():
                    stale.unlink()
        atomic_write_text(
            self.directory / "index.json",
            json.dumps(
                {
                    "bucket_count": self.bucket_count,
                    "signals_version": self.signals_version,
                    "buckets": self.entries,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )


def _bucket_signature(keys: list[str], fingerprints: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(fingerprints.get(key, "").encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None
