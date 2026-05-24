"""Tests for retro.schema — NormalizedEvent I/O."""
from __future__ import annotations

from pathlib import Path

from retro.schema import NormalizedEvent, RawRef, read_events, write_events


def test_round_trip(tmp_path: Path):
    events = [
        NormalizedEvent(
            event_id="ev-1",
            session_id="sess-1",
            host="claude-code",
            sequence=1,
            actor="user",
            event_type="message",
            summary="hello",
            raw_ref=RawRef(path="/tmp/test.jsonl", line=1),
            timestamp="2025-06-01T10:00:00Z",
            payload={"text": "hello world"},
        ),
        NormalizedEvent(
            event_id="ev-2",
            session_id="sess-1",
            host="claude-code",
            sequence=2,
            actor="assistant",
            event_type="tool_call",
            summary="Read(file_path=/foo)",
            raw_ref=RawRef(path="/tmp/test.jsonl", line=2),
            payload={"name": "Read", "input": {"file_path": "/foo"}},
        ),
    ]
    path = tmp_path / "events.jsonl"
    count = write_events(path, events)
    assert count == 2

    loaded = list(read_events(path))
    assert len(loaded) == 2
    assert loaded[0].event_id == "ev-1"
    assert loaded[0].actor == "user"
    assert loaded[0].payload["text"] == "hello world"
    assert loaded[1].event_type == "tool_call"
    assert loaded[1].raw_ref.line == 2


def test_to_dict():
    ev = NormalizedEvent(
        event_id="ev-1",
        session_id="s",
        host="codex",
        sequence=1,
        actor="tool",
        event_type="tool_result",
        summary="result",
        raw_ref=RawRef(path="p", line=3),
    )
    d = ev.to_dict()
    assert d["event_id"] == "ev-1"
    assert d["raw_ref"] == {"path": "p", "line": 3}
    assert d["host"] == "codex"
