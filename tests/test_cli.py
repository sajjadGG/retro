"""CLI smoke tests using Typer's CliRunner."""
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from retro.cli import app
from retro.config import load_config

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


def test_import_copilot_no_args():
    result = runner.invoke(app, ["import", "copilot"])
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


def test_global_archive_command_help():
    for args in (
        ["config", "--help"],
        ["archive", "--help"],
        ["benchmark", "--help"],
        ["schedule", "--help"],
        ["setup", "--help"],
        ["sync", "--help"],
        ["doctor", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output


def test_benchmark_run_help():
    result = runner.invoke(app, ["benchmark", "run", "--help"])
    assert result.exit_code == 0
    assert "GhostLab OpenShell" in result.output
    assert "--use-git-credential" in result.output


def test_config_set_and_show(tmp_path):
    archive = tmp_path / "archive"
    set_result = runner.invoke(
        app,
        ["config", "set", "archive-root", str(archive)],
    )
    assert set_result.exit_code == 0

    show_result = runner.invoke(app, ["config", "show"])
    assert show_result.exit_code == 0
    assert "archive_root" in show_result.output
    assert load_config().archive_root == str(archive.resolve())


def test_setup_without_schedule(tmp_path):
    archive = tmp_path / "archive"
    dashboard = tmp_path / "dashboard"
    result = runner.invoke(
        app,
        [
            "setup",
            "--archive-root",
            str(archive),
            "--dashboard-dir",
            str(dashboard),
            "--periodic",
            "15m",
            "--no-schedule",
        ],
    )

    assert result.exit_code == 0
    assert archive.is_dir()
    assert dashboard.is_dir()


def test_doctor_fresh_install_and_dashboard_override(monkeypatch, tmp_path):
    override = Path("/tmp/retro-override-dashboard")
    monkeypatch.setenv("RETRO_DASHBOARD_DIR", str(override))

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Retro global archive" in result.output
    assert "override-dashboard" in result.output
