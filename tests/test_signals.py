"""Tests for the signals subsystem."""
from __future__ import annotations

from pathlib import Path

from retro.schema import NormalizedEvent, RawRef
from retro.signals import REGISTRY, run_signals
from retro.signals.base import SessionContext


def _ev(
    event_id="x",
    actor="user",
    event_type="message",
    summary="",
    payload=None,
    timestamp="2025-06-01T10:00:00Z",
):
    return NormalizedEvent(
        event_id=event_id,
        session_id="s",
        host="claude-code",
        sequence=1,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="p", line=1),
        timestamp=timestamp,
        payload=payload or {},
    )


def _ctx(events, host="claude-code", session_id="s"):
    return SessionContext(
        host=host,
        session_id=session_id,
        events=events,
        raw_dir=Path("/tmp/fake-raw"),
    )


class TestSignalRegistry:
    def test_registry_not_empty(self):
        assert len(REGISTRY) > 0

    def test_all_signals_have_required_fields(self):
        for name, sig in REGISTRY.items():
            assert sig.name == name
            assert sig.group in ("activity", "outcome", "cost", "risk")
            assert sig.kind in ("numeric", "boolean", "categorical", "text")
            assert sig.method in ("heuristic", "regex", "external", "llm_judge")
            assert sig.description


class TestHeuristicSignals:
    def test_command_count(self):
        events = [
            _ev(actor="assistant", event_type="command", summary="Bash(cmd=ls)"),
            _ev(actor="assistant", event_type="command", summary="Bash(cmd=pwd)"),
            _ev(actor="assistant", event_type="message", summary="hello"),
        ]
        sig = REGISTRY["command_count"]
        readings = sig(_ctx(events))
        assert len(readings) == 1
        assert readings[0].value == 2

    def test_file_edit_count(self):
        events = [
            _ev(actor="assistant", event_type="file_edit", summary="Edit()"),
            _ev(actor="tool", event_type="file_edit", summary="patch result"),
        ]
        sig = REGISTRY["file_edit_count"]
        readings = sig(_ctx(events))
        assert readings[0].value == 2

    def test_unique_files_edited(self):
        events = [
            _ev(
                event_type="file_edit",
                payload={"input": {"file_path": "/a.py"}},
            ),
            _ev(
                event_type="file_edit",
                payload={"input": {"file_path": "/a.py"}},
            ),
            _ev(
                event_type="file_edit",
                payload={"input": {"file_path": "/b.py"}},
            ),
        ]
        sig = REGISTRY["unique_files_edited"]
        readings = sig(_ctx(events))
        assert readings[0].value == 2

    def test_user_message_count(self):
        events = [
            _ev(actor="user", event_type="message"),
            _ev(actor="user", event_type="message"),
            _ev(actor="assistant", event_type="message"),
        ]
        sig = REGISTRY["user_message_count"]
        readings = sig(_ctx(events))
        assert readings[0].value == 2

    def test_failed_command_count(self):
        events = [
            _ev(actor="tool", event_type="tool_result", payload={"is_error": True}),
            _ev(actor="tool", event_type="tool_result", payload={"is_error": False}),
            _ev(actor="tool", event_type="command", payload={"success": False}),
        ]
        sig = REGISTRY["failed_command_count"]
        readings = sig(_ctx(events))
        assert readings[0].value == 2

    def test_failed_command_ratio(self):
        events = [
            _ev(actor="tool", event_type="tool_result", payload={"is_error": True}),
            _ev(actor="tool", event_type="tool_result", payload={"is_error": False}),
        ]
        sig = REGISTRY["failed_command_ratio"]
        readings = sig(_ctx(events))
        assert readings[0].value == 0.5

    def test_unknown_event_count(self):
        events = [
            _ev(event_type="unknown", summary="unknown: type=foo"),
            _ev(event_type="message"),
            _ev(event_type="unknown", summary="unknown: type=bar"),
        ]
        sig = REGISTRY["unknown_event_count"]
        readings = sig(_ctx(events))
        assert readings[0].value == 2

    def test_session_duration(self):
        events = [
            _ev(timestamp="2025-06-01T10:00:00Z"),
            _ev(timestamp="2025-06-01T10:05:00Z"),
        ]
        sig = REGISTRY["session_duration_seconds"]
        readings = sig(_ctx(events))
        assert readings[0].value == 300

    def test_user_satisfaction_positive(self):
        events = [
            _ev(
                actor="user",
                event_type="message",
                payload={"text": "Thanks, that's perfect!"},
            ),
        ]
        sig = REGISTRY["user_satisfaction_lexical"]
        readings = sig(_ctx(events))
        assert readings[0].value == "positive"

    def test_user_satisfaction_negative(self):
        events = [
            _ev(
                actor="user",
                event_type="message",
                payload={"text": "That's broken, doesn't work at all"},
            ),
        ]
        sig = REGISTRY["user_satisfaction_lexical"]
        readings = sig(_ctx(events))
        assert readings[0].value == "negative"

    def test_secret_exposure_detects_api_key(self):
        events = [
            _ev(
                event_type="message",
                payload={"text": "My key is sk-proj-abc123def456ghi789jkl012mno"},
            ),
        ]
        sig = REGISTRY["secret_exposure_signal"]
        readings = sig(_ctx(events))
        assert readings[0].value is True

    def test_secret_exposure_clean(self):
        events = [
            _ev(event_type="message", payload={"text": "Nothing secret here."}),
        ]
        sig = REGISTRY["secret_exposure_signal"]
        readings = sig(_ctx(events))
        assert readings[0].value is False

    def test_capture_gap_signal(self):
        events = [
            _ev(event_type="unknown"),
        ]
        sig = REGISTRY["capture_gap_signal"]
        readings = sig(_ctx(events))
        assert readings[0].value is True


class TestSignalRunner:
    def test_run_signals_on_imported_session(self, claude_imported):
        layout, session_id = claude_imported
        readings = run_signals(layout)
        assert len(readings) > 0
        signal_names = {r.signal for r in readings}
        assert "command_count" in signal_names
        assert "file_edit_count" in signal_names
