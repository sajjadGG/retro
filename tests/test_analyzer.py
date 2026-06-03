"""Tests for the retro command & tool call analyzer."""

from __future__ import annotations

from retro.analyzer import (
    _is_failed,
    analyze_sessions,
    extract_command_line,
    generate_report,
    get_base_command,
)
from retro.schema import NormalizedEvent, RawRef


def test_get_base_command():
    assert get_base_command("git commit -m 'test'") == "git"
    assert get_base_command(".venv/bin/pytest tests/ -v") == "pytest"
    assert get_base_command("python -m ruff check src/") == "ruff"
    assert get_base_command("python src/main.py") == "main.py"
    assert get_base_command("PORT=3000 python main.py") == "main.py"
    assert get_base_command("poetry run black .") == "black"
    assert get_base_command("npx next dev") == "next"
    assert get_base_command("   ") == ""


def test_extract_command_line():
    ev_dict = NormalizedEvent(
        event_id="1",
        session_id="s1",
        host="claude-code",
        sequence=1,
        actor="assistant",
        event_type="command",
        summary="Bash",
        raw_ref=RawRef(path="path", line=1),
        payload={"input": {"command": "git pull"}},
    )
    assert extract_command_line(ev_dict) == "git pull"

    ev_str = NormalizedEvent(
        event_id="2",
        session_id="s1",
        host="claude-code",
        sequence=2,
        actor="assistant",
        event_type="command",
        summary="Bash",
        raw_ref=RawRef(path="path", line=2),
        payload={"input": "git push"},
    )
    assert extract_command_line(ev_str) == "git push"

    ev_args = NormalizedEvent(
        event_id="3",
        session_id="s1",
        host="codex",
        sequence=3,
        actor="assistant",
        event_type="command",
        summary="shell",
        raw_ref=RawRef(path="path", line=3),
        payload={"arguments": {"cmd": "npm install"}},
    )
    assert extract_command_line(ev_args) == "npm install"


def test_is_failed():
    ev_fail = NormalizedEvent(
        event_id="1",
        session_id="s1",
        host="claude-code",
        sequence=1,
        actor="tool",
        event_type="tool_result",
        summary="failed command",
        raw_ref=RawRef(path="path", line=1),
        payload={"success": False},
    )
    assert _is_failed(ev_fail) is True

    ev_exit = NormalizedEvent(
        event_id="2",
        session_id="s1",
        host="claude-code",
        sequence=2,
        actor="tool",
        event_type="tool_result",
        summary="exit command",
        raw_ref=RawRef(path="path", line=2),
        payload={"output": "Process exited with code 1"},
    )
    assert _is_failed(ev_exit) is True


def test_analyze_sessions(claude_imported, tmp_path):
    layout, session_id = claude_imported
    stats = analyze_sessions(layout)

    assert stats["claude-code"]["sessions"] == 1
    assert stats["codex"]["sessions"] == 0
    assert stats["total"]["sessions"] == 1

    assert len(stats["claude-code"]["commands"]) > 0

    report_file = tmp_path / "report.md"
    generate_report(stats, report_file)
    assert report_file.exists()
    content = report_file.read_text(encoding="utf-8")
    assert "# Command & Tool Use Analysis Report" in content
    assert "Overview Summary" in content
