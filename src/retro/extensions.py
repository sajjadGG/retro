"""Source extension runtime and built-in provider adapters."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .importers.base import has_only_repo_state_capture
from .importers.claude import ClaudeImporter
from .importers.codex import CodexImporter
from .importers.copilot import CopilotImporter
from .importers.copilot_cli import (
    CopilotCliSession,
)
from .importers.copilot_cli import (
    _capture_fingerprint as _copilot_cli_fingerprint,
)
from .importers.vscode_copilot import (
    CopilotSession as VscodeCopilotSession,
)
from .importers.vscode_copilot import (
    _capture_fingerprint as _vscode_copilot_fingerprint,
)
from .locking import archive_lock
from .schema import Host, validate_host_id, validate_session_id
from .sdk.registry import (
    EXTENSION_API_VERSION,
    SOURCE_REGISTRY,
    ProviderRecord,
)
from .sdk.sources import ImportResult, SessionDescriptor
from .storage import Layout

_BUILTIN_ALIASES = {
    "claude": "claude-code",
    "cc": "claude-code",
    "cx": "codex",
    "copilot": "vscode-copilot",
    "vscode": "vscode-copilot",
    "gh-copilot": "vscode-copilot",
}
STALE_IMPORT_SECONDS = 24 * 60 * 60


class ClaudeSource:
    RETRO_EXTENSION_API = EXTENSION_API_VERSION
    host = "claude-code"
    display_name = "Claude Code"

    def __init__(self, layout: Layout, **kwargs: Any):
        self.importer = ClaudeImporter(layout, **kwargs)

    def discover(self) -> list[SessionDescriptor]:
        descriptors = []
        for session in self.importer.discover():
            stat = session.transcript_path.stat()
            descriptors.append(
                SessionDescriptor(
                    host=self.host,
                    session_id=session.session_id,
                    title=session.project_slug,
                    cwd=session.cwd or None,
                    updated_at=session.mtime,
                    source_path=session.transcript_path,
                    metadata={
                        "project_slug": session.project_slug,
                        "size_bytes": session.size_bytes,
                        "raw_filename": "transcript.jsonl",
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size_bytes": stat.st_size,
                    },
                )
            )
        return descriptors

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        return self.importer.import_session(identifier=identifier, force=force)


class CodexSource:
    RETRO_EXTENSION_API = EXTENSION_API_VERSION
    host = "codex"
    display_name = "Codex"

    def __init__(self, layout: Layout, **kwargs: Any):
        self.importer = CodexImporter(layout, **kwargs)

    def discover(self) -> list[SessionDescriptor]:
        descriptors = []
        for thread in self.importer.discover():
            stat = thread.rollout_path.stat()
            descriptors.append(
                SessionDescriptor(
                    host=self.host,
                    session_id=thread.thread_id,
                    title=thread.display_title,
                    cwd=thread.cwd or None,
                    updated_at=thread.updated_at,
                    source_path=thread.rollout_path,
                    metadata={
                        "model_provider": thread.model_provider,
                        "source_kind": thread.source_kind,
                        "raw_filename": "rollout.jsonl",
                        "source_mtime_ns": stat.st_mtime_ns,
                        "source_size_bytes": stat.st_size,
                    },
                )
            )
        return descriptors

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        return self.importer.import_session(identifier=identifier, force=force)


class CopilotSource:
    RETRO_EXTENSION_API = EXTENSION_API_VERSION
    host = "vscode-copilot"
    display_name = "VS Code GitHub Copilot"

    def __init__(self, layout: Layout, **kwargs: Any):
        self.importer = CopilotImporter(layout, **kwargs)

    def discover(self) -> list[SessionDescriptor]:
        descriptors: list[SessionDescriptor] = []
        for session in self.importer.discover():
            if isinstance(session, CopilotCliSession):
                source_path = session.events_path
                raw_filename = "events.jsonl"
                fingerprint = _copilot_cli_fingerprint(session)
            elif isinstance(session, VscodeCopilotSession):
                source_path = session.session_path
                raw_filename = f"session.{session.core_format}"
                fingerprint = _vscode_copilot_fingerprint(session)
            else:
                raise TypeError(f"unsupported Copilot session type: {type(session).__name__}")
            primary_stat = source_path.stat()
            descriptors.append(
                SessionDescriptor(
                    host=self.host,
                    session_id=session.session_id,
                    title=session.display_title,
                    cwd=session.cwd or None,
                    updated_at=session.mtime,
                    source_path=source_path,
                    metadata={
                        "source_kind": session.source_kind,
                        "workspace_name": session.workspace_name,
                        "models": list(session.models),
                        "raw_filename": raw_filename,
                        "source_mtime_ns": fingerprint[0],
                        "source_size_bytes": fingerprint[1],
                        "primary_mtime_ns": primary_stat.st_mtime_ns,
                        "primary_size_bytes": primary_stat.st_size,
                    },
                )
            )
        return descriptors

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        return self.importer.import_session(identifier=identifier, force=force)


def _register_builtins() -> None:
    for name, provider in (
        ("claude-code", ClaudeSource),
        ("codex", CodexSource),
        ("vscode-copilot", CopilotSource),
    ):
        if not SOURCE_REGISTRY.has_builtin(name):
            SOURCE_REGISTRY.register_builtin(name, provider)


_register_builtins()


def canonical_source_host(value: str) -> Host:
    host = value.strip().lower()
    return validate_host_id(_BUILTIN_ALIASES.get(host, host))


def source_host_names() -> list[Host]:
    names: set[Host] = set()
    for record in SOURCE_REGISTRY.discover():
        try:
            host = validate_host_id(record.name)
        except ValueError:
            continue
        if not record.builtin and host in _BUILTIN_ALIASES:
            continue
        names.add(host)
    return sorted(names)


def extension_records() -> list[ProviderRecord]:
    return SOURCE_REGISTRY.discover()


def validate_source_provider(
    record: ProviderRecord,
    provider: Any,
    *,
    require_factory: bool = False,
) -> None:
    if require_factory and not callable(provider):
        raise TypeError("source provider entry point must be callable")
    for member in ("host", "display_name", "discover", "import_session"):
        if not hasattr(provider, member):
            raise TypeError(f"missing required member {member!r}")
    host = validate_host_id(provider.host)
    if not record.builtin and record.name in _BUILTIN_ALIASES:
        raise ValueError(
            f"source provider name {record.name!r} is reserved as a built-in alias"
        )
    if host != record.name:
        raise ValueError(
            f"entry-point name {record.name!r} does not match provider host {host!r}"
        )
    if not isinstance(provider.display_name, str) or not provider.display_name.strip():
        raise TypeError("display_name must be a non-empty string")
    if not callable(provider.discover) or not callable(provider.import_session):
        raise TypeError("discover and import_session must be callable")


@dataclass(frozen=True)
class SourceDoctorResult:
    record: ProviderRecord
    ok: bool
    message: str


def doctor_sources() -> list[SourceDoctorResult]:
    results: list[SourceDoctorResult] = []
    collisions = SOURCE_REGISTRY.collisions()
    collided = {id(record) for records in collisions.values() for record in records}
    for record in SOURCE_REGISTRY.discover():
        if id(record) in collided:
            results.append(SourceDoctorResult(record, False, "provider name collision"))
            continue
        try:
            provider = record.load()
            validate_source_provider(record, provider, require_factory=True)
        except Exception as exc:
            results.append(SourceDoctorResult(record, False, str(exc)))
        else:
            results.append(SourceDoctorResult(record, True, "ok"))
    return results


class SourceHandle:
    def __init__(
        self,
        record: ProviderRecord,
        layout: Layout,
        provider_kwargs: dict[str, Any] | None = None,
    ):
        self.record = record
        self.layout = layout
        self.provider_kwargs = dict(provider_kwargs or {})
        self._adapter: Any = None
        self._descriptors: dict[str, SessionDescriptor] = {}

    @property
    def host(self) -> Host:
        return validate_host_id(self.record.name)

    @property
    def display_name(self) -> str:
        return str(self._live_adapter().display_name)

    def _instantiate(self, layout: Layout) -> Any:
        provider = self.record.load()
        validate_source_provider(self.record, provider, require_factory=True)
        adapter = provider(layout, **self.provider_kwargs)
        validate_source_provider(self.record, adapter)
        return adapter

    def _live_adapter(self) -> Any:
        if self._adapter is None:
            self._adapter = self._instantiate(self.layout)
        return self._adapter

    def discover(self) -> list[SessionDescriptor]:
        sessions = self._live_adapter().discover()
        for session in sessions:
            if not isinstance(session, SessionDescriptor):
                raise TypeError(
                    f"{self.host} discover() returned {type(session).__name__}; "
                    "expected SessionDescriptor"
                )
            if session.host != self.host:
                raise ValueError(
                    f"{self.host} discover() returned descriptor for {session.host!r}"
                )
        self._descriptors = {session.session_id: session for session in sessions}
        return sessions

    def import_session(
        self,
        *,
        identifier: str,
        force: bool = False,
        acquire_lock: bool = True,
        descriptor: SessionDescriptor | None = None,
    ) -> ImportResult:
        validate_session_id(identifier)
        selected = descriptor or self._descriptors.get(identifier)
        if selected is None:
            selected = next(
                (
                    session
                    for session in self.discover()
                    if session.session_id == identifier
                ),
                None,
            )
        lock = (
            archive_lock(self.layout)
            if acquire_lock
            else nullcontext()
        )
        with lock:
            if (
                selected is not None
                and not force
                and _descriptor_is_unchanged(self.layout, selected)
            ):
                raise FileExistsError(
                    f"Raw capture already exists at "
                    f"{self.layout.raw_dir(self.host, identifier)} "
                    "(pass force=True to overwrite)"
                )
            return self._stage_and_publish(
                identifier=identifier,
                force=force,
            )

    def _stage_and_publish(self, *, identifier: str, force: bool) -> ImportResult:
        self.layout.ensure()
        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f".retro-import-{self.host}-{identifier}-",
                dir=self.layout.root,
            )
        )
        staging_layout = Layout(staging_root)
        staging_layout.ensure()
        target_raw = self.layout.raw_dir(self.host, identifier)
        target_normalized = self.layout.normalized_path(self.host, identifier)
        staged_raw = staging_layout.raw_dir(self.host, identifier)
        staged_normalized = staging_layout.normalized_path(self.host, identifier)
        try:
            if target_raw.exists():
                staged_raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target_raw, staged_raw)
            adapter = self._instantiate(staging_layout)
            _share_discovery_state(self._live_adapter(), adapter)
            result = adapter.import_session(identifier=identifier, force=force)
            _validate_staged_result(
                result,
                host=self.host,
                session_id=identifier,
                raw_dir=staged_raw,
                normalized_path=staged_normalized,
            )
            _publish_capture(
                staged_raw=staged_raw,
                staged_normalized=staged_normalized,
                target_raw=target_raw,
                target_normalized=target_normalized,
            )
            return ImportResult(
                host=self.host,
                session_id=identifier,
                raw_dir=target_raw,
                normalized_path=target_normalized,
                event_count=result.event_count,
                unknown_event_count=result.unknown_event_count,
                gaps=list(result.gaps),
            )
        finally:
            if staging_root.exists():
                shutil.rmtree(staging_root)


def create_source(
    host: str,
    layout: Layout,
    **provider_kwargs: Any,
) -> SourceHandle:
    canonical = canonical_source_host(host)
    return SourceHandle(
        SOURCE_REGISTRY.get_record(canonical),
        layout,
        provider_kwargs,
    )


def cleanup_stale_import_staging(
    layout: Layout,
    *,
    older_than_seconds: int = STALE_IMPORT_SECONDS,
    now: float | None = None,
) -> list[Path]:
    if not layout.root.is_dir():
        return []
    cutoff = (time.time() if now is None else now) - older_than_seconds
    removed: list[Path] = []
    for path in layout.root.glob(".retro-import-*"):
        try:
            stale = path.is_dir() and not path.is_symlink() and path.stat().st_mtime < cutoff
        except OSError:
            continue
        if stale:
            shutil.rmtree(path)
            removed.append(path)
    return removed


def _descriptor_is_unchanged(
    layout: Layout,
    descriptor: SessionDescriptor,
) -> bool:
    raw_dir = layout.raw_dir(descriptor.host, descriptor.session_id)
    if not raw_dir.is_dir() or has_only_repo_state_capture(raw_dir):
        return False
    fingerprint = _descriptor_fingerprint(descriptor)
    if fingerprint is None:
        return False

    meta_path = raw_dir / "import_meta.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        previous_mtime = meta.get("captured_mtime_ns")
        previous_size = meta.get("captured_size_bytes")
        if isinstance(previous_mtime, int) and isinstance(previous_size, int):
            return (
                fingerprint[0] <= previous_mtime
                and fingerprint[1] <= previous_size
            )

    raw_filename = descriptor.metadata.get("raw_filename")
    if not isinstance(raw_filename, str):
        return False
    captured = raw_dir / raw_filename
    try:
        stat = captured.stat()
    except OSError:
        return False
    return fingerprint[0] <= stat.st_mtime_ns and fingerprint[1] <= stat.st_size


def _descriptor_fingerprint(
    descriptor: SessionDescriptor,
) -> tuple[int, int] | None:
    mtime = descriptor.metadata.get("source_mtime_ns")
    size = descriptor.metadata.get("source_size_bytes")
    if isinstance(mtime, int) and isinstance(size, int):
        if descriptor.source_path is not None:
            try:
                current = descriptor.source_path.stat()
            except OSError:
                return None
            primary_mtime = descriptor.metadata.get("primary_mtime_ns", mtime)
            primary_size = descriptor.metadata.get("primary_size_bytes", size)
            if (
                isinstance(primary_mtime, int)
                and isinstance(primary_size, int)
                and (
                    current.st_mtime_ns > primary_mtime
                    or current.st_size > primary_size
                )
            ):
                return (
                    max(mtime, current.st_mtime_ns),
                    size + max(0, current.st_size - primary_size),
                )
        return mtime, size
    if descriptor.source_path is None:
        return None
    try:
        stat = descriptor.source_path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _share_discovery_state(source: Any, target: Any) -> None:
    if type(source) is not type(target):
        return
    if hasattr(source, "_discover_cache") and hasattr(target, "_discover_cache"):
        target._discover_cache = source._discover_cache
    for name in ("importer", "vscode", "agent_host"):
        source_child = getattr(source, name, None)
        target_child = getattr(target, name, None)
        if source_child is not None and target_child is not None:
            _share_discovery_state(source_child, target_child)


def _validate_staged_result(
    result: ImportResult,
    *,
    host: Host,
    session_id: str,
    raw_dir: Path,
    normalized_path: Path,
) -> None:
    if result.host != host or result.session_id != session_id:
        raise ValueError(
            f"source provider returned {result.host}/{result.session_id}; "
            f"expected {host}/{session_id}"
        )
    if result.raw_dir.resolve() != raw_dir.resolve():
        raise ValueError(
            f"source provider wrote raw capture outside its staging layout: {result.raw_dir}"
        )
    if result.normalized_path.resolve() != normalized_path.resolve():
        raise ValueError(
            "source provider wrote normalized events outside its staging layout: "
            f"{result.normalized_path}"
        )
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"source provider did not create raw capture {raw_dir}")
    if not normalized_path.is_file():
        raise FileNotFoundError(
            f"source provider did not create normalized events {normalized_path}"
        )


def _publish_capture(
    *,
    staged_raw: Path,
    staged_normalized: Path,
    target_raw: Path,
    target_normalized: Path,
) -> None:
    suffix = uuid.uuid4().hex
    raw_backup = target_raw.with_name(f".{target_raw.name}.backup-{suffix}")
    normalized_backup = target_normalized.with_name(
        f".{target_normalized.name}.backup-{suffix}"
    )
    target_raw.parent.mkdir(parents=True, exist_ok=True)
    target_normalized.parent.mkdir(parents=True, exist_ok=True)
    moved_raw = False
    moved_normalized = False
    try:
        if target_raw.exists():
            os.replace(target_raw, raw_backup)
        if target_normalized.exists():
            os.replace(target_normalized, normalized_backup)
        os.replace(staged_raw, target_raw)
        moved_raw = True
        os.replace(staged_normalized, target_normalized)
        moved_normalized = True
    except Exception:
        if moved_normalized:
            _remove_path(target_normalized)
        if moved_raw:
            _remove_path(target_raw)
        if raw_backup.exists():
            os.replace(raw_backup, target_raw)
        if normalized_backup.exists():
            os.replace(normalized_backup, target_normalized)
        raise
    else:
        _remove_path(raw_backup)
        _remove_path(normalized_backup)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
