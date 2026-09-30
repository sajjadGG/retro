"""Stable public API for Retro extensions."""
from __future__ import annotations

from ..schema import (
    Actor,
    EventType,
    Host,
    NormalizedEvent,
    RawRef,
    read_events,
    validate_host_id,
    validate_session_id,
    write_events,
)
from ..storage import Layout
from .registry import (
    EXTENSION_API_VERSION,
    SOURCE_ENTRY_POINT_GROUP,
    SOURCE_REGISTRY,
    SUPPORTED_EXTENSION_API_VERSIONS,
    ProviderCollisionError,
    ProviderError,
    ProviderRecord,
    SourceRegistry,
    UnsupportedExtensionAPIError,
    validate_extension_api,
)
from .sources import ImportResult, SessionDescriptor, SourceAdapter

RETRO_EXTENSION_API = EXTENSION_API_VERSION

__all__ = [
    "Actor",
    "EXTENSION_API_VERSION",
    "EventType",
    "Host",
    "ImportResult",
    "Layout",
    "NormalizedEvent",
    "ProviderCollisionError",
    "ProviderError",
    "ProviderRecord",
    "RETRO_EXTENSION_API",
    "RawRef",
    "SOURCE_ENTRY_POINT_GROUP",
    "SOURCE_REGISTRY",
    "SUPPORTED_EXTENSION_API_VERSIONS",
    "SessionDescriptor",
    "SourceAdapter",
    "SourceRegistry",
    "UnsupportedExtensionAPIError",
    "read_events",
    "validate_extension_api",
    "validate_host_id",
    "validate_session_id",
    "write_events",
]
