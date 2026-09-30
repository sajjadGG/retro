"""Tests for the public source SDK and managed extension runtime."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from retro import extensions as extensions_module
from retro.extensions import SourceHandle, create_source, doctor_sources
from retro.schema import NormalizedEvent, RawRef, read_events, write_events
from retro.sdk import (
    EXTENSION_API_VERSION,
    ImportResult,
    ProviderCollisionError,
    ProviderError,
    SessionDescriptor,
    SourceAdapter,
    SourceRegistry,
)
from retro.storage import Layout


class FakeSource:
    RETRO_EXTENSION_API = "1"
    host = "opencode"
    display_name = "OpenCode"

    def __init__(self, layout: Layout):
        self.layout = layout

    def discover(self) -> list[SessionDescriptor]:
        return [
            SessionDescriptor(
                host=self.host,
                session_id="session-1",
                title="Demo",
                source_path=Path("/source/session-1.jsonl"),
            )
        ]

    def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
        raw_dir = self.layout.raw_dir(self.host, identifier)
        if raw_dir.exists() and not force:
            raise FileExistsError(f"Raw capture already exists at {raw_dir}")
        raw_dir.mkdir(parents=True, exist_ok=True)
        source = raw_dir / "session.jsonl"
        source.write_text('{"type":"future"}\n', encoding="utf-8")
        normalized = self.layout.normalized_path(self.host, identifier)
        count = write_events(
            normalized,
            [
                NormalizedEvent(
                    event_id=f"{identifier}:1",
                    session_id=identifier,
                    host=self.host,
                    sequence=1,
                    actor="system",
                    event_type="unknown",
                    summary="preserved",
                    raw_ref=RawRef(
                        path=f"raw/{self.host}/{identifier}/session.jsonl",
                        line=1,
                    ),
                    payload={"type": "future"},
                )
            ],
        )
        return ImportResult(
            host=self.host,
            session_id=identifier,
            raw_dir=raw_dir,
            normalized_path=normalized,
            event_count=count,
            unknown_event_count=1,
            gaps=["future"],
        )


def _handle(layout: Layout, provider=FakeSource) -> SourceHandle:
    registry = SourceRegistry(include_entry_points=False)
    registry.register_builtin("opencode", provider, source="test")
    return SourceHandle(registry.get_record("opencode"), layout)


def test_public_source_contract():
    source = FakeSource(Layout(Path("/tmp/retro-extension-contract")))
    descriptor = source.discover()[0]

    assert isinstance(source, SourceAdapter)
    assert descriptor.host == "opencode"
    assert descriptor.session_id == "session-1"
    assert EXTENSION_API_VERSION == "1"


def test_source_handle_stages_and_publishes_atomically(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    source = _handle(layout)

    result = source.import_session(identifier="session-1")

    assert result.raw_dir == layout.raw_dir("opencode", "session-1")
    assert result.normalized_path == layout.normalized_path("opencode", "session-1")
    assert result.raw_dir.joinpath("session.jsonl").is_file()
    event = next(read_events(result.normalized_path))
    assert event.host == "opencode"
    assert event.raw_ref.path == "raw/opencode/session-1/session.jsonl"
    assert not list(layout.root.glob(".retro-import-*"))


def test_failed_source_import_preserves_existing_capture(tmp_path: Path):
    class FailingSource(FakeSource):
        def import_session(self, *, identifier: str, force: bool = False) -> ImportResult:
            raw_dir = self.layout.raw_dir(self.host, identifier)
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "session.jsonl").write_text("partial\n", encoding="utf-8")
            raise RuntimeError("normalization failed")

    layout = Layout(tmp_path / "archive")
    raw_dir = layout.raw_dir("opencode", "session-1")
    raw_dir.mkdir(parents=True)
    original = raw_dir / "session.jsonl"
    original.write_text("original\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="normalization failed"):
        _handle(layout, FailingSource).import_session(
            identifier="session-1",
            force=True,
        )

    assert original.read_text(encoding="utf-8") == "original\n"
    assert not layout.normalized_path("opencode", "session-1").exists()


def test_publication_failure_rolls_back_raw_and_normalized(
    monkeypatch,
    tmp_path: Path,
):
    layout = Layout(tmp_path / "archive")
    raw_dir = layout.raw_dir("opencode", "session-1")
    raw_dir.mkdir(parents=True)
    raw_path = raw_dir / "session.jsonl"
    raw_path.write_text("original\n", encoding="utf-8")
    normalized = layout.normalized_path("opencode", "session-1")
    write_events(
        normalized,
        [
            NormalizedEvent(
                event_id="original",
                session_id="session-1",
                host="opencode",
                sequence=1,
                actor="system",
                event_type="unknown",
                summary="original",
                raw_ref=RawRef(
                    path="raw/opencode/session-1/session.jsonl",
                    line=1,
                ),
            )
        ],
    )
    original_replace = extensions_module.os.replace
    failed = False

    def fail_normalized_publish(source, target):
        nonlocal failed
        source_path = Path(source)
        if (
            not failed
            and Path(target) == normalized
            and ".retro-import-" in str(source_path)
        ):
            failed = True
            raise OSError("simulated normalized publish failure")
        original_replace(source, target)

    monkeypatch.setattr(extensions_module.os, "replace", fail_normalized_publish)

    with pytest.raises(OSError, match="simulated normalized publish failure"):
        _handle(layout).import_session(identifier="session-1", force=True)

    assert raw_path.read_text(encoding="utf-8") == "original\n"
    assert next(read_events(normalized)).summary == "original"


def test_builtin_source_uses_managed_incremental_publication(
    tmp_path: Path,
    claude_transcript: Path,
):
    claude_home = tmp_path / "claude"
    project = claude_home / "projects" / "demo"
    project.mkdir(parents=True)
    transcript = project / "session-1.jsonl"
    shutil.copy2(claude_transcript, transcript)
    layout = Layout(tmp_path / "archive")
    source = create_source("claude-code", layout, claude_home=claude_home)

    first = source.import_session(identifier="session-1")
    with pytest.raises(FileExistsError):
        source.import_session(identifier="session-1")
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"type":"future-event","uuid":"future-1","sessionId":"session-1"}\n'
        )
    updated = source.import_session(identifier="session-1")

    assert updated.event_count == first.event_count + 1
    assert updated.unknown_event_count == first.unknown_event_count + 1
    assert not list(layout.root.glob(".retro-import-*"))


def test_registry_loads_entry_points_lazily(monkeypatch):
    loads: list[str] = []

    class FakeEntryPoint:
        name = "opencode"
        value = "retro_opencode:OpenCodeSource"
        dist = None

        def load(self):
            loads.append(self.name)
            return FakeSource

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            return self if group == "retro.sources" else []

    monkeypatch.setattr(
        "retro.sdk.registry.metadata.entry_points",
        lambda: FakeEntryPoints([FakeEntryPoint()]),
    )
    registry = SourceRegistry()

    records = registry.discover()

    assert [record.name for record in records] == ["opencode"]
    assert loads == []
    assert registry.get_record("opencode").load() is FakeSource
    assert loads == ["opencode"]


def test_registry_rejects_provider_collisions(monkeypatch):
    class FakeEntryPoint:
        name = "codex"
        value = "retro_codex:CodexSource"
        dist = None

        def load(self):
            return FakeSource

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            return self

    monkeypatch.setattr(
        "retro.sdk.registry.metadata.entry_points",
        lambda: FakeEntryPoints([FakeEntryPoint()]),
    )
    registry = SourceRegistry()
    registry.register_builtin("codex", FakeSource)

    assert "codex" in registry.collisions()
    with pytest.raises(ProviderCollisionError, match="collision"):
        registry.get_record("codex")


def test_registry_rejects_unsupported_extension_api(monkeypatch):
    class FutureSource(FakeSource):
        RETRO_EXTENSION_API = "2"

    class FakeEntryPoint:
        name = "opencode"
        value = "retro_opencode:OpenCodeSource"
        dist = None

        def load(self):
            return FutureSource

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            return self

    monkeypatch.setattr(
        "retro.sdk.registry.metadata.entry_points",
        lambda: FakeEntryPoints([FakeEntryPoint()]),
    )
    registry = SourceRegistry()

    with pytest.raises(ProviderError, match="unsupported Retro extension API '2'"):
        registry.get_record("opencode").load()


def test_broken_optional_source_does_not_break_builtin(monkeypatch):
    class BrokenEntryPoint:
        name = "broken"
        value = "broken:Source"
        dist = None

        def load(self):
            raise ImportError("optional dependency unavailable")

    class FakeEntryPoints(list):
        def select(self, *, group: str):
            return self

    monkeypatch.setattr(
        "retro.sdk.registry.metadata.entry_points",
        lambda: FakeEntryPoints([BrokenEntryPoint()]),
    )
    registry = SourceRegistry()
    registry.register_builtin("opencode", FakeSource)

    assert registry.get_record("opencode").load() is FakeSource
    with pytest.raises(ProviderError, match="optional dependency unavailable"):
        registry.get_record("broken").load()


def test_builtin_sources_pass_doctor():
    results = [result for result in doctor_sources() if result.record.builtin]

    assert len(results) == 3
    assert all(result.ok for result in results)
