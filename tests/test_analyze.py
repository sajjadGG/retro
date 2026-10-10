"""Tests for cross-session command/tool analysis (`retro analyze`)."""

from __future__ import annotations

from retro.schema import NormalizedEvent, RawRef, write_events
from retro.signals import analyze_sessions, build_profile, generate_report, session_tips
from retro.signals.commands import command_text, get_base_command
from retro.signals.profile import _result_failed, _result_text
from retro.storage import Layout


def _event(seq, actor, event_type, payload, host="claude-code", summary=""):
    return NormalizedEvent(
        event_id=f"e{seq}",
        session_id="s1",
        host=host,
        sequence=seq,
        actor=actor,
        event_type=event_type,
        summary=summary,
        raw_ref=RawRef(path="raw.jsonl", line=seq),
        payload=payload,
    )


def test_get_base_command():
    assert get_base_command("git commit -m 'test'") == "git"
    assert get_base_command(".venv/bin/pytest tests/ -v") == "pytest"
    assert get_base_command("python -m ruff check src/") == "ruff"
    assert get_base_command("python src/main.py") == "main.py"
    assert get_base_command("PORT=3000 python main.py") == "main.py"
    assert get_base_command("poetry run black .") == "black"
    assert get_base_command("npx next dev") == "next"
    assert get_base_command("   ") == ""


def test_segment_bases_skips_navigation():
    from retro.signals.commands import segment_bases

    assert segment_bases("cd repo && ruff check src && pytest -q") == ["ruff", "pytest"]
    assert segment_bases("export FOO=1") == []


def test_command_text_across_hosts():
    assert command_text(_event(1, "assistant", "command", {"input": {"command": "git pull"}})) == "git pull"
    assert (
        command_text(_event(2, "assistant", "command", {"arguments": {"cmd": "npm install"}}))
        == "npm install"
    )
    assert command_text(_event(3, "assistant", "command", {"arguments": '{"cmd": "ls -la"}'})) == "ls -la"
    assert command_text(_event(4, "assistant", "command", {"input": "git push"})) == ""
    assert (
        command_text(
            _event(
                5,
                "assistant",
                "command",
                {"part": {"input": {"command": "ruff check src"}}},
            )
        )
        == "ruff check src"
    )


def test_result_failure_detection():
    failed = _event(1, "tool", "tool_result", {"success": False})
    assert _result_failed(failed, failed.payload, "") is True
    exited = _event(2, "tool", "tool_result", {"output": "Process exited with code 1"})
    assert _result_failed(exited, exited.payload, _result_text(exited.payload, "")) is True
    ok = _event(3, "tool", "tool_result", {"output": "Process exited with code 0"})
    assert _result_failed(ok, ok.payload, _result_text(ok.payload, "")) is False


def test_analyze_sessions(claude_imported, tmp_path):
    layout, _ = claude_imported
    stats = analyze_sessions(layout)

    assert stats["sessions"] == 1
    assert stats["hosts"]["claude-code"]["sessions"] == 1
    assert stats["total"]["commands"] > 0

    report_file = tmp_path / "report.md"
    generate_report(stats, report_file)
    content = report_file.read_text(encoding="utf-8")
    assert "# Command & tool analysis" in content
    assert "Top commands" in content


def test_analyze_sessions_includes_extension_hosts(tmp_path):
    layout = Layout(tmp_path / "archive")
    write_events(
        layout.normalized_path("opencode", "session-1"),
        [
            NormalizedEvent(
                event_id="call-1",
                session_id="session-1",
                host="opencode",
                sequence=1,
                actor="assistant",
                event_type="command",
                summary="bash(command=pytest)",
                raw_ref=RawRef(path="raw/opencode/session-1/session.jsonl", line=1),
                payload={"name": "bash", "input": {"command": "pytest"}},
            )
        ],
    )
    stats = analyze_sessions(layout)
    report_file = tmp_path / "report.md"
    generate_report(stats, report_file)

    assert stats["hosts"]["opencode"]["sessions"] == 1
    assert stats["sessions"] == 1
    assert "opencode" in report_file.read_text(encoding="utf-8")


def test_session_tips_are_evidence_backed(tmp_path):
    events = [_event(1, "assistant", "file_edit", {"file_path": "/w/a.py"})]
    profile = build_profile(
        key="claude-code/s1",
        host="claude-code",
        session_id="s1",
        events=events,
        raw_dir=tmp_path,
    )
    tips = session_tips(profile)
    assert any("validation" in tip for tip in tips)
