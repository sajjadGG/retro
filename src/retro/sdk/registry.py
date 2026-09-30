"""Lazy Python entry-point discovery for Retro source extensions."""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from importlib import metadata
from typing import Any

EXTENSION_API_VERSION = "1"
SUPPORTED_EXTENSION_API_VERSIONS = frozenset({EXTENSION_API_VERSION})
SOURCE_ENTRY_POINT_GROUP = "retro.sources"


class ProviderError(RuntimeError):
    pass


class ProviderCollisionError(ProviderError):
    pass


class UnsupportedExtensionAPIError(ProviderError):
    pass


@dataclass
class ProviderRecord:
    name: str
    source: str
    value: str
    builtin: bool = False
    api_version: str | None = None
    error: str | None = None
    _provider: Any = field(default=None, repr=False)
    _entry_point: metadata.EntryPoint | None = field(default=None, repr=False)
    _loaded: bool = field(default=False, repr=False)

    @property
    def status(self) -> str:
        if self.error:
            return "error"
        if self._loaded:
            return "ready"
        return "discovered"

    def load(self) -> Any:
        if self.error:
            raise ProviderError(self.error)
        if self._loaded:
            return self._provider
        if self._entry_point is None:
            raise ProviderError(f"source provider {self.name!r} has no loader")
        try:
            provider = self._entry_point.load()
            version = _extension_api_version(provider)
            validate_extension_api(version)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            raise ProviderError(self.error) from exc
        self._provider = provider
        self.api_version = version
        self._loaded = True
        return provider


def _extension_api_version(provider: Any) -> str | None:
    version = getattr(provider, "RETRO_EXTENSION_API", None)
    if version is None:
        module = inspect.getmodule(provider)
        version = getattr(module, "RETRO_EXTENSION_API", None) if module else None
    return str(version) if version is not None else None


def validate_extension_api(version: str | None) -> None:
    if version is None:
        raise UnsupportedExtensionAPIError(
            "missing RETRO_EXTENSION_API; source extensions must declare "
            "RETRO_EXTENSION_API = '1'"
        )
    if version not in SUPPORTED_EXTENSION_API_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_EXTENSION_API_VERSIONS))
        raise UnsupportedExtensionAPIError(
            f"unsupported Retro extension API {version!r}; supported: {supported}"
        )


class SourceRegistry:
    def __init__(self, *, include_entry_points: bool = True) -> None:
        self.include_entry_points = include_entry_points
        self._builtins: dict[str, ProviderRecord] = {}
        self._discovered: list[ProviderRecord] | None = None

    def register_builtin(self, name: str, provider: Any, *, source: str = "retro") -> None:
        if name in self._builtins:
            raise ProviderCollisionError(f"duplicate source provider {name!r}")
        self._builtins[name] = ProviderRecord(
            name=name,
            source=source,
            value=f"{provider.__module__}:{provider.__qualname__}",
            builtin=True,
            api_version=EXTENSION_API_VERSION,
            _provider=provider,
            _loaded=True,
        )

    def has_builtin(self, name: str) -> bool:
        return name in self._builtins

    def discover(self, *, refresh: bool = False) -> list[ProviderRecord]:
        if self._discovered is None or refresh:
            records: list[ProviderRecord] = []
            entry_points = _entry_points() if self.include_entry_points else []
            for entry_point in entry_points:
                dist = getattr(entry_point, "dist", None)
                source = getattr(dist, "name", None) or "installed package"
                records.append(
                    ProviderRecord(
                        name=entry_point.name,
                        source=source,
                        value=entry_point.value,
                        _entry_point=entry_point,
                    )
                )
            self._discovered = records
        return [*self._builtins.values(), *self._discovered]

    def collisions(self) -> dict[str, list[ProviderRecord]]:
        by_name: dict[str, list[ProviderRecord]] = {}
        for record in self.discover():
            by_name.setdefault(record.name, []).append(record)
        return {name: records for name, records in by_name.items() if len(records) > 1}

    def get_record(self, name: str) -> ProviderRecord:
        matches = [record for record in self.discover() if record.name == name]
        if not matches:
            raise KeyError(f"no source provider named {name!r}")
        if len(matches) > 1:
            sources = ", ".join(record.source for record in matches)
            raise ProviderCollisionError(
                f"source provider collision for {name!r} ({sources})"
            )
        return matches[0]

    def reset_discovery(self) -> None:
        self._discovered = None


def _entry_points() -> list[metadata.EntryPoint]:
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        return list(entry_points.select(group=SOURCE_ENTRY_POINT_GROUP))
    return list(entry_points.get(SOURCE_ENTRY_POINT_GROUP, ()))  # type: ignore[union-attr]


SOURCE_REGISTRY = SourceRegistry()
