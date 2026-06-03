"""Tests for subscription ceiling heatmap and daily progress rings logic."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dashboard.build_dashboard import (
    PricingMap,
    _is_rate_limit_hit,
    analyze_session,
    summarize_portfolio,
)


def test_is_rate_limit_hit():
    assert _is_rate_limit_hit({"summary": "Rate limit exceeded"}) is True
    assert _is_rate_limit_hit(
        {"summary": "Something else", "payload": {"message": "wait until 10:00"}}
    ) is True
    assert _is_rate_limit_hit(
        {"summary": "error", "payload": {"error": "exceeded your quota"}}
    ) is True
    assert _is_rate_limit_hit({"summary": "no problem"}) is False


def test_analyze_session_counts_rate_limits():
    events = [
        {
            "event_type": "message",
            "actor": "user",
            "timestamp": "2026-06-01T12:00:00Z",
            "summary": "Hello",
        },
        {
            "event_type": "error",
            "actor": "system",
            "timestamp": "2026-06-01T12:01:00Z",
            "summary": "ResourceExhausted: rate limit hit",
        },
    ]

    pricing = PricingMap({}, {})
    res = analyze_session(
        host="claude-code",
        session_id="s1",
        normalized_path=Path("/tmp/s1.jsonl"),
        events=events,
        pricing=pricing,
    )

    assert res["rate_limit_hits"] == 1


def _make_session(
    date: str, host: str, session_id: str, tokens: int, rate_limit_hits: int
) -> dict[str, Any]:
    return {
        "date": date,
        "host": host,
        "session_id": session_id,
        "tokens": {"total_tokens": tokens},
        "rate_limit_hits": rate_limit_hits,
        "event_count": 0,
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_call_events": 0,
        "tool_result_events": 0,
        "command_events": 0,
        "file_read_events": 0,
        "file_edit_events": 0,
        "failed_events": 0,
        "unknown_events": 0,
        "estimated_cost_usd": 0.0,
        "cost_categories": {
            "input": 0.0,
            "cache_create": 0.0,
            "cache_read": 0.0,
            "output": 0.0,
        },
        "duration_seconds": None,
    }


def test_summarize_portfolio_accumulates_rate_limits():
    sessions = [
        _make_session("2026-06-01", "codex", "s1", 100, 2),
        _make_session("2026-06-01", "codex", "s2", 50, 1),
    ]

    summary = summarize_portfolio(sessions)
    assert summary["totals"]["rate_limit_hits"] == 3
    assert summary["by_day"]["2026-06-01"]["rate_limit_hits"] == 3

