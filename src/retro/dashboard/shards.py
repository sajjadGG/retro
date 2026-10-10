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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import atomic_write_text
from .facts import (
    COLUMN_INDEX,
    SPLIT_DICTIONARIES,
    DashboardModel,
    detail_payload,
    excluded_detail_payload,
)

SESSION_SHARD_ROWS = 2000
_KEY = COLUMN_INDEX["key"]
_STARTED = COLUMN_INDEX["started"]
_BUCKET = COLUMN_INDEX["bucket"]
CACHE_DIRNAME = ".retro-cache"
DETAIL_DIRNAME = "detail"
EXCLUDED_DETAIL_DIRNAME = "detail-x"
EXCLUDED_SHARD_ROWS = 4000


@dataclass
class ShardReport:
    session_shards: int = 0
    excluded_shards: int = 0
    signal_shards: int = 0
    detail_shards: int = 0
    excluded_detail_shards: int = 0
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
        self.excluded_shards: list[dict[str, Any]] = []
        self.signal_shards: list[dict[str, Any]] = []
        self.dictionary_tail: dict[str, Any] | None = None

    def write_all(self) -> ShardReport:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.write_sessions()
        self.write_dictionary_tail()
        self.write_signal_matrix()
        self.write_details()
        return self.report

    def write_sessions(self) -> None:
        """Included rows load at boot; excluded rows only when the user asks for them."""
        included = self.model.included_rows
        self.session_shards = self._write_row_shards(
            included, prefix="sessions", name="sessions", chunk=SESSION_SHARD_ROWS, offset=0
        )
        self.report.session_shards = len(self.session_shards)
        self.excluded_shards = self._write_row_shards(
            self.model.excluded_rows,
            prefix="excluded",
            name="excluded",
            chunk=EXCLUDED_SHARD_ROWS,
            offset=len(included),
        )
        self.report.excluded_shards = len(self.excluded_shards)

    def _write_row_shards(
        self,
        rows: list[list[Any]],
        *,
        prefix: str,
        name: str,
        chunk: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        shards: list[dict[str, Any]] = []
        shard_count = max(1, (len(rows) + chunk - 1) // chunk) if rows else 0
        for shard in range(shard_count):
            start = shard * chunk
            block = rows[start : start + chunk]
            path = self.data_dir / f"{prefix}-{shard:03d}.js"
            write_js(
                path,
                f"{name}:{shard}",
                {"shard": shard, "offset": offset + start, "rows": block},
            )
            self.report.record(path, self.data_dir.parent)
            started = [row[_STARTED] for row in block if row[_STARTED] is not None]
            shards.append(
                {
                    "file": f"data/{path.name}",
                    "shard": shard,
                    "offset": offset + start,
                    "rows": len(block),
                    "from": min(started) if started else None,
                    "to": max(started) if started else None,
                }
            )
        return shards

    def write_dictionary_tail(self) -> None:
        """Dictionary entries only excluded rows reference ship with the excluded set."""
        dictionaries = self.model.dictionaries.to_dict()
        tail: dict[str, Any] = {}
        for name in SPLIT_DICTIONARIES:
            values = dictionaries.get(name) or []
            split = self.model.dictionary_split.get(name, len(values))
            if split < len(values):
                tail[name] = {"offset": split, "items": [_compact_entry(item) for item in values[split:]]}
        if not tail:
            return
        path = self.data_dir / "dict-tail.js"
        write_js(path, "dict-tail", tail)
        self.report.record(path, self.data_dir.parent)
        self.dictionary_tail = {"file": f"data/{path.name}"}

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
        self.report.detail_shards = self._write_detail_set(
            self.model.included_rows,
            bucket_count=self.model.bucket_count,
            directory=DETAIL_DIRNAME,
            prefix="d",
            channel="detail",
            render=detail_payload,
        )
        self.report.excluded_detail_shards = self._write_detail_set(
            self.model.excluded_rows,
            bucket_count=self.model.excluded_bucket_count,
            directory=EXCLUDED_DETAIL_DIRNAME,
            prefix="x",
            channel="detailx",
            render=excluded_detail_payload,
        )

    def _write_detail_set(
        self,
        rows: list[list[Any]],
        *,
        bucket_count: int,
        directory: str,
        prefix: str,
        channel: str,
        render: Callable[[DashboardModel, str], dict[str, Any]],
    ) -> int:
        members: dict[int, list[str]] = {}
        for row in rows:
            members.setdefault(int(row[_BUCKET]), []).append(row[_KEY])

        cache_dir = self.cache_dir / directory if self.cache_dir else None
        cache = _DetailCache(cache_dir, bucket_count, self.model.signals.version, prefix=prefix)
        if self.mode == "full":
            cache.reset()
        target_dir = self.data_dir / directory
        if members:
            target_dir.mkdir(parents=True, exist_ok=True)

        written = 0
        for bucket in range(bucket_count):
            keys = sorted(members.get(bucket, []))
            if not keys:
                continue
            signature = _bucket_signature(keys, self.model.fingerprints)
            name = f"{prefix}-{bucket:04d}.js"
            destination = target_dir / name
            if cache.take(bucket, signature, destination):
                self.report.detail_cached += 1
            else:
                payload = {
                    "bucket": bucket,
                    "sessions": {key: render(self.model, key) for key in keys},
                }
                cache.store(bucket, signature, name, js_payload(f"{channel}:{bucket}", payload), destination)
                self.report.detail_written += 1
            self.report.record(destination, self.data_dir.parent)
            written += 1
        cache.flush()
        return written


class _DetailCache:
    """Reuses previously rendered detail buckets whose member fingerprints match."""

    def __init__(
        self,
        directory: Path | None,
        bucket_count: int,
        signals_version: str,
        *,
        prefix: str = "d",
    ) -> None:
        self.directory = directory
        self.prefix = prefix
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
        source = self.directory / f"{self.prefix}-{bucket:04d}.js"
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
                stale = self.directory / f"{self.prefix}-{int(bucket):04d}.js"
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


def _compact_entry(item: Any) -> Any:
    """Drop null and redundant dictionary fields; the client restores defaults."""
    if not isinstance(item, dict):
        return item
    compact = {key: value for key, value in item.items() if value is not None}
    if compact.get("name") == compact.get("id"):
        compact.pop("name", None)
    return compact


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
