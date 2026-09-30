"""CLI smoke tests using Typer's CliRunner."""
from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import typer
from typer.testing import CliRunner

from retro.benchmarks.task_scorer.build import BuildConfigurationError
from retro.benchmarks.task_scorer.run import TaskVerificationError
from retro.cli import (
    _make_extension_import_command,
    app,
    benchmark_run_cmd,
    taskset_build_cmd,
    taskset_bundle_cmd,
    taskset_report_cmd,
    taskset_run_cmd,
    taskset_select_cmd,
)
from retro.config import load_config
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.sdk import ImportResult, SessionDescriptor

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


def test_extensions_commands():
    listed = runner.invoke(app, ["extensions", "list"])
    checked = runner.invoke(app, ["extensions", "doctor"])

    assert listed.exit_code == 0, listed.output
    assert "claude-code" in listed.output
    assert "vscode-copilot" in listed.output
    assert checked.exit_code == 0, checked.output
    assert "source provider checks passed" in checked.output


def test_extension_import_command_routes_provider(monkeypatch, tmp_path: Path):
    class FakeSource:
        host = "opencode"
        display_name = "OpenCode"

        def __init__(self, layout):
            self.layout = layout

        def discover(self):
            return [
                SessionDescriptor(
                    host=self.host,
                    session_id="session-1",
                    title="Latest",
                )
            ]

        def import_session(self, *, identifier: str, force: bool = False):
            raw_dir = self.layout.raw_dir(self.host, identifier)
            raw_dir.mkdir(parents=True)
            normalized_path = self.layout.normalized_path(self.host, identifier)
            count = write_events(
                normalized_path,
                [
                    NormalizedEvent(
                        event_id=f"{identifier}:1",
                        session_id=identifier,
                        host=self.host,
                        sequence=1,
                        actor="system",
                        event_type="unknown",
                        summary="preserved",
                        raw_ref=RawRef(
                            path=f"raw/{self.host}/{identifier}/source.jsonl",
                            line=1,
                        ),
                    )
                ],
            )
            return ImportResult(
                host=self.host,
                session_id=identifier,
                raw_dir=raw_dir,
                normalized_path=normalized_path,
                event_count=count,
            )

    monkeypatch.setattr(
        "retro.cli.create_source",
        lambda host, layout: FakeSource(layout),
    )
    test_app = typer.Typer()
    test_app.command("opencode")(_make_extension_import_command("opencode"))
    test_app.command("other")(lambda: None)
    root = tmp_path / "rollout-memory"

    result = runner.invoke(
        test_app,
        [
            "opencode",
            "--latest",
            "--no-render",
            "--root",
            str(root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "captured opencode/session-1" in result.output
    assert (root / "normalized/opencode/session-1.events.jsonl").is_file()


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
        ["benchmark", "taskset", "--help"],
        ["capture", "--help"],
        ["schedule", "--help"],
        ["setup", "--help"],
        ["sync", "--help"],
        ["doctor", "--help"],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output


def test_capture_commands_write_immutable_git_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Retro Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "retro@example.invalid"],
        check=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    root = tmp_path / "rollout-memory"
    args = [
        "capture",
        "start",
        "--host",
        "codex",
        "--session-id",
        "session-1",
        "--cwd",
        str(repo),
        "--root",
        str(root),
    ]

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    capture = root / "raw" / "codex" / "session-1" / "repo_start.json"
    first_content = capture.read_text(encoding="utf-8")
    payload = json.loads(first_content)
    assert payload["schema_version"] == "retro-repo-state-v1"
    assert payload["clean"] is True

    duplicate = runner.invoke(app, args)

    assert duplicate.exit_code == 2
    assert capture.read_text(encoding="utf-8") == first_content

    end = runner.invoke(
        app,
        [
            "capture",
            "end",
            "--host",
            "codex",
            "--session-id",
            "session-1",
            "--cwd",
            str(repo),
            "--root",
            str(root),
        ],
    )
    assert end.exit_code == 0, end.output
    assert (capture.parent / "repo_end.json").is_file()


def test_benchmark_run_help():
    result = runner.invoke(app, ["benchmark", "run", "--help"])
    assert result.exit_code == 0
    assert "GhostLab OpenShell" in result.output

    option = inspect.signature(benchmark_run_cmd).parameters["use_git_credential"]
    assert "--use-git-credential" in option.default.param_decls


def test_taskset_command_help_exposes_complete_pipeline():
    expected = {
        "select": (taskset_select_cmd, "environment_config", "--environment-config"),
        "bundle": (
            taskset_bundle_cmd,
            "selected_only",
            "--selected-only/--reselect",
        ),
        "build": (taskset_build_cmd, "task_definer_agent", "--task-definer-agent"),
        "run": (taskset_run_cmd, "seeds", "--seeds"),
        "report": (taskset_report_cmd, "eval_id", "--eval"),
    }
    for command, (callback, parameter_name, option) in expected.items():
        result = runner.invoke(app, ["benchmark", "taskset", command, "--help"])
        assert result.exit_code == 0, result.output
        parameter = inspect.signature(callback).parameters[parameter_name]
        assert option in parameter.default.param_decls


def test_taskset_build_accepts_a_path_command_name(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_build(*args, **_kwargs):
        captured["ghostlab_bin"] = args[2]
        raise BuildConfigurationError("stop after option parsing")

    monkeypatch.setattr("retro.cli.build_taskset", fake_build)
    agents = []
    for name in ("definer", "builder", "auditor"):
        path = tmp_path / f"{name}.json"
        path.write_text("{}", encoding="utf-8")
        agents.append(path)
    result = runner.invoke(
        app,
        [
            "benchmark",
            "taskset",
            "build",
            "--name",
            "pilot",
            "--ghostlab-bin",
            "ghostlab",
            "--task-definer-agent",
            str(agents[0]),
            "--scorer-builder-agent",
            str(agents[1]),
            "--scorer-auditor-agent",
            str(agents[2]),
            "--root",
            str(tmp_path / "archive"),
        ],
    )

    assert result.exit_code == 1
    assert captured["ghostlab_bin"] == "ghostlab"


def test_taskset_run_allows_ghostlab_environment_fallback(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_run(*args, **_kwargs):
        captured["ghostlab_bin"] = args[4]
        raise TaskVerificationError("stop after option parsing")

    monkeypatch.setattr("retro.cli.run_taskset", fake_run)
    agent = tmp_path / "agent.json"
    agent.write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "benchmark",
            "taskset",
            "run",
            "--name",
            "pilot",
            "--agent",
            str(agent),
            "--root",
            str(tmp_path / "archive"),
        ],
    )

    assert result.exit_code == 1
    assert captured["ghostlab_bin"] is None


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
