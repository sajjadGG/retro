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


def test_analyze_session_extracts_project_info_codex():
    from unittest.mock import patch
    pricing = PricingMap({}, {})
    events: list[dict[str, Any]] = []
    
    with patch("dashboard.build_dashboard.read_raw_meta") as mock_read:
        mock_read.return_value = {"cwd": "/Users/sajad/Dev/repos/Mem"}
        res = analyze_session(
            host="codex",
            session_id="s1",
            normalized_path=Path("/tmp/s1.jsonl"),
            events=events,
            pricing=pricing,
        )
        assert res["project_name"] == "Mem"
        assert res["project_path"] == "/Users/sajad/Dev/repos/Mem"


def test_analyze_session_extracts_project_info_claude():
    from unittest.mock import patch
    pricing = PricingMap({}, {})
    events: list[dict[str, Any]] = [
        {
            "event_type": "file_read",
            "actor": "assistant",
            "payload": {"file_path": "/Users/sajad/Dev/repos/Mem/src/retro/schema.py"}
        }
    ]
    
    with patch("dashboard.build_dashboard.read_raw_meta") as mock_read:
        mock_read.return_value = {"project_slug": "Mem"}
        res = analyze_session(
            host="claude-code",
            session_id="s1",
            normalized_path=Path("/tmp/s1.jsonl"),
            events=events,
            pricing=pricing,
        )
        assert res["project_name"] == "Mem"
        assert res["project_path"] == "/Users/sajad/Dev/repos/Mem"


def test_summarize_portfolio_groups_projects():
    s1 = _make_session("2026-06-01", "codex", "s1", 100, 0)
    s1["project_name"] = "Mem"
    s1["project_path"] = "/Users/sajad/Dev/repos/Mem"
    s1["estimated_cost_usd"] = 0.05

    s2 = _make_session("2026-06-01", "claude-code", "s2", 200, 0)
    s2["project_name"] = "Mem"
    s2["project_path"] = "/Users/sajad/Dev/repos/Mem"
    s2["estimated_cost_usd"] = 0.10

    s3 = _make_session("2026-06-01", "codex", "s3", 50, 0)
    s3["project_name"] = "other"
    s3["project_path"] = "/Users/sajad/Dev/repos/other"
    s3["estimated_cost_usd"] = 0.01

    summary = summarize_portfolio([s1, s2, s3])
    
    assert summary["projects_count"] == 2
    projs = {p["name"]: p for p in summary["projects"]}
    assert "Mem" in projs
    assert "other" in projs
    
    assert projs["Mem"]["sessions"] == 2
    assert projs["Mem"]["tokens"] == 300
    assert projs["Mem"]["cost"] == 0.15
    assert projs["Mem"]["hosts"] == ["claude-code", "codex"]
    assert projs["Mem"]["path"] == "/Users/sajad/Dev/repos/Mem"

