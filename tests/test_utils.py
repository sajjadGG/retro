"""Tests for retro.utils — shared helpers."""
from __future__ import annotations

import json
from pathlib import Path

from retro.schema import NormalizedEvent, RawRef
from retro.utils import event_text, iter_jsonl, iter_messages, truncate, truncate_summary


def _ev(actor="user", event_type="message", payload=None, summary=""):
    return NormalizedEvent(
        event_id="x",
        session_id="s",
        host="claude-code",
        sequence=1,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="p", line=1),
        payload=payload or {},
    )


def test_event_text_prefers_text_key():
    assert event_text(_ev(payload={"text": "hello"})) == "hello"


def test_event_text_falls_back_to_summary():
    assert event_text(_ev(summary="fallback")) == "fallback"


def test_event_text_handles_non_string_raw_content():
    ev = _ev(payload={"raw_content": {"key": "val"}})
    result = event_text(ev)
    assert "key" in result
    parsed = json.loads(result)
    assert parsed == {"key": "val"}


def test_event_text_handles_string_raw_content():
    assert event_text(_ev(payload={"raw_content": "raw"})) == "raw"


def test_truncate():
    assert truncate("short", 100) == "short"
    assert len(truncate("a" * 200, 50)) == 50


def test_truncate_summary_flattens_newlines():
    result = truncate_summary("line1\nline2\nline3", 200)
    assert "\n" not in result
    assert "line1 line2 line3" == result


def test_iter_messages_filters_by_actor():
    events = [
        _ev(actor="user", event_type="message"),
        _ev(actor="assistant", event_type="message"),
        _ev(actor="user", event_type="tool_call"),
        _ev(actor="user", event_type="message"),
    ]
    user_msgs = list(iter_messages(events, "user"))
    assert len(user_msgs) == 2

    all_msgs = list(iter_messages(events))
    assert len(all_msgs) == 3


def test_iter_jsonl(tmp_path: Path):
    path = tmp_path / "test.jsonl"
    path.write_text(
        '{"a": 1}\n'
        "\n"
        '{"b": 2}\n'
        "not-json\n"
        '{"c": 3}\n',
        encoding="utf-8",
    )
    results = list(iter_jsonl(path))
    assert len(results) == 3
    assert results[0] == (1, {"a": 1})
    assert results[1] == (3, {"b": 2})
    assert results[2] == (5, {"c": 3})
