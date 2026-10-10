"""Where the set of sessions to evaluate comes from.

`update_signals` is given a catalog, but `retro.catalog` must not be imported
here: signals and the catalog are built in parallel and the signal layer only
needs four attributes. The structural `Protocol` below is the whole contract.
`NormalizedSource` is the fallback used by `retro signal run` before the
catalog exists and by the tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - typing_extensions is unnecessary on 3.8+
    from typing import Protocol, runtime_checkable
except ImportError:  # pragma: no cover
    from typing_extensions import Protocol, runtime_checkable  # type: ignore

from ..storage import Layout
from .meta import is_temp_cwd, meta_cwd, read_raw_meta


@runtime_checkable
class SessionRecordLike(Protocol):
    """The subset of a catalog record the signal layer relies on."""

    @property
    def key(self) -> str: ...

    @property
    def host(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def fingerprint(self) -> str: ...


class SessionSource(Protocol):
    """Anything that can enumerate the sessions worth evaluating."""

    def included(self) -> Iterable[SessionRecordLike]: ...


@dataclass(frozen=True)
class FileSessionRecord:
    """A catalog-shaped record derived from the files on disk."""

    key: str
    host: str
    session_id: str
    fingerprint: str
    client: str | None = None
    agent: str | None = None
    interactive: bool | None = None
    project: dict[str, Any] | None = None
    tags: tuple[str, ...] = ()
    started_at: str | None = None
    models: tuple[str, ...] = ()
    cwd: str | None = None


@dataclass
class NormalizedSource:
    """Enumerate sessions straight from `normalized/`, with stat fingerprints.

    Copilot sessions whose working directory is a temp directory are skipped by
    default: on the real archive those are ~30k programmatic `copilot -p`
    evaluation runs, not work the user did.
    """

    layout: Layout
    host: str | None = None
    session_ids: Sequence[str] | None = None
    skip_temp: bool = True
    _wanted: set | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._wanted = set(self.session_ids) if self.session_ids else None

    def included(self) -> Iterator[FileSessionRecord]:
        root = self.layout.root / "normalized"
        if not root.exists():
            return
        hosts = [self.host] if self.host else self.layout.list_normalized_hosts()
        for host in hosts:
            host_dir = root / host
            if not host_dir.is_dir():
                continue
            for path in sorted(host_dir.glob("*.events.jsonl")):
                session_id = path.name[: -len(".events.jsonl")]
                if self._wanted is not None and session_id not in self._wanted:
                    continue
                raw_dir = self.layout.raw_dir(host, session_id)
                meta = read_raw_meta(raw_dir) if self.skip_temp else {}
                cwd = meta_cwd(meta) if meta else None
                if self.skip_temp and is_temp_cwd(cwd):
                    continue
                yield FileSessionRecord(
                    key=f"{host}/{session_id}",
                    host=host,
                    session_id=session_id,
                    fingerprint=stat_fingerprint(path, raw_dir),
                    cwd=cwd,
                    client=_meta_str(meta, "client_name"),
                    started_at=_meta_str(meta, "created_at") or _meta_str(meta, "started_at"),
                )


def stat_fingerprint(normalized_path: Path, raw_dir: Path | None = None) -> str:
    """Cheap change detector: size and mtime of the inputs, no hashing."""
    parts: list[str] = []
    try:
        info = normalized_path.stat()
        parts.append(f"{info.st_size:x}.{info.st_mtime_ns:x}")
    except OSError:
        parts.append("missing")
    if raw_dir is not None:
        try:
            parts.append(f"{raw_dir.stat().st_mtime_ns:x}")
        except OSError:
            parts.append("0")
    return "stat1:" + "-".join(parts)


def _meta_str(meta: Mapping[str, Any], key: str) -> str | None:
    value = meta.get(key)
    return value if isinstance(value, str) and value else None


def resolve_source(
    layout: Layout,
    catalog: Any = None,
    *,
    host: str | None = None,
    session_ids: Sequence[str] | None = None,
) -> SessionSource:
    """Use the catalog when one is supplied, otherwise walk `normalized/`."""
    if catalog is not None and hasattr(catalog, "included"):
        return catalog
    return NormalizedSource(layout=layout, host=host, session_ids=session_ids)


def record_attr(record: Any, name: str, default: Any = None) -> Any:
    value = getattr(record, name, None)
    if value is None and isinstance(record, Mapping):
        value = record.get(name)
    return default if value is None else value


def session_key(record: Any) -> str:
    key = record_attr(record, "key")
    if isinstance(key, str) and key:
        return key
    return "{}/{}".format(record_attr(record, "host", ""), record_attr(record, "session_id", ""))


__all__ = [
    "FileSessionRecord",
    "NormalizedSource",
    "SessionRecordLike",
    "SessionSource",
    "record_attr",
    "resolve_source",
    "session_key",
    "stat_fingerprint",
]
