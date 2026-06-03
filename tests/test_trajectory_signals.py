"""Tests for experimental trajectory builder and signals."""
from __future__ import annotations

from pathlib import Path

from retro.schema import Actor, EventType, NormalizedEvent, RawRef
from retro.signals import REGISTRY
from retro.signals.base import SessionContext
from retro.trajectory import build_trajectory


def _ev(
    event_id: str,
    actor: Actor,
    event_type: EventType,
    summary: str = "",
    payload=None,
    sequence: int = 1,
):
    return NormalizedEvent(
        event_id=event_id,
        session_id="s",
        host="codex",
        sequence=sequence,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="p", line=sequence),
        timestamp="2026-05-24T10:00:00Z",
        payload=payload or {},
    )


def _ctx(events):
    return SessionContext(host="codex", session_id="s", events=events, raw_dir=Path("/tmp/raw"))


def test_build_trajectory_categorizes_search_edit_test():
    events = [
        _ev("u1", "user", "message", payload={"text": "fix failing test"}),
        _ev("r1", "assistant", "reasoning", payload={"thinking": "Find the failing code."}),
        _ev("a1", "assistant", "command", payload={"cmd": "rg failing_function src tests"}),
        _ev("o1", "tool", "tool_result", payload={"output": "src/app.py:12"}),
        _ev("a2", "assistant", "file_edit", payload={"input": {"file_path": "src/app.py"}}),
        _ev("o2", "tool", "tool_result", payload={"output": "patched"}),
        _ev("a3", "assistant", "command", payload={"cmd": "pytest tests/test_app.py"}),
        _ev("o3", "tool", "tool_result", payload={"output": "Process exited with code 0"}),
    ]

    steps = build_trajectory(events)

    assert [s.action_category for s in steps] == ["search", "generate_fix", "run_tests"]
    assert steps[0].thought_text == "Find the failing code."
    assert steps[1].action_event_id == "a2"


def test_trajectory_validation_signal_after_edit():
    events = [
        _ev("a1", "assistant", "file_edit", payload={"input": {"file_path": "src/app.py"}}),
        _ev("o1", "tool", "tool_result", payload={"output": "patched"}),
        _ev("a2", "assistant", "command", payload={"cmd": "pytest"}),
        _ev("o2", "tool", "tool_result", payload={"output": "Process exited with code 0"}),
    ]

    readings = REGISTRY["trajectory_validation_presence"](_ctx(events))

    assert readings[0].value is True
    assert readings[0].metadata["validation_steps"] == [2]


def test_trajectory_repetition_signal_detects_repeated_command():
    events = [
        _ev("a1", "assistant", "command", payload={"cmd": "rg Widget src"}),
        _ev("o1", "tool", "tool_result", payload={"output": "none"}),
        _ev("a2", "assistant", "command", payload={"cmd": "rg Widget src"}),
        _ev("o2", "tool", "tool_result", payload={"output": "none"}),
    ]

    repeated = REGISTRY["trajectory_repeated_action_without_progress"](_ctx(events))
    redundancy = REGISTRY["trajectory_action_redundancy"](_ctx(events))

    assert repeated[0].value is True
    assert redundancy[0].value == 1


def test_trajectory_premature_finish_without_validation():
    events = [
        _ev("a1", "assistant", "file_edit", payload={"input": {"file_path": "src/app.py"}}),
        _ev("o1", "tool", "tool_result", payload={"output": "patched"}),
        _ev("m1", "assistant", "message", payload={"text": "Done."}),
    ]

    readings = REGISTRY["trajectory_premature_finish_without_validation"](_ctx(events))

    assert readings[0].value is True
    assert readings[0].metadata["last_fix_step"] == 1
