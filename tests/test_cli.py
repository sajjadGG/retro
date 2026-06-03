"""CLI smoke tests using Typer's CliRunner."""
from __future__ import annotations

from typer.testing import CliRunner

from retro.cli import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Capture" in result.output


def test_methods():
    result = runner.invoke(app, ["methods"])
    assert result.exit_code == 0
    assert "reme_refine_poc" in result.output
    assert "skill_pro" in result.output
    assert "memp_procedural" in result.output
    assert "risk_aware" in result.output


def test_signal_list():
    result = runner.invoke(app, ["signal", "list"])
    assert result.exit_code == 0
    assert "command_count" in result.output or "command_co" in result.output
    assert "activity" in result.output
    assert "risk" in result.output
    assert "Signals" in result.output


def test_signal_list_filter_by_group():
    result = runner.invoke(app, ["signal", "list", "--group", "risk"])
    assert result.exit_code == 0
    assert "secret_exposure" in result.output
    assert "command_count" not in result.output


def test_import_claude_no_args():
    result = runner.invoke(app, ["import", "claude"])
    assert result.exit_code != 0


def test_import_codex_no_args():
    result = runner.invoke(app, ["import", "codex"])
    assert result.exit_code != 0


def test_show_unknown_host():
    result = runner.invoke(app, ["show", "foobar", "some-id"])
    assert result.exit_code != 0


def test_list_command(tmp_path):
    result = runner.invoke(app, ["list", "--root", str(tmp_path / "rollout-memory")])
    assert result.exit_code == 0


def test_dashboard_view_non_interactive():
    result = runner.invoke(app, ["dashboard", "view"])
    assert result.exit_code == 0
    assert "Retro Rollout Dashboard" in result.output or "Retro Portfolio Dashboard" in result.output
    assert "Imported Sessions Summary" in result.output


def test_analyze_command(tmp_path):
    result = runner.invoke(app, ["analyze", "--root", str(tmp_path / "rollout-memory")])
    assert result.exit_code == 0
    assert "Retro Command & Tool Call Analysis" in result.output
    assert "Wrote analysis report to:" in result.output


