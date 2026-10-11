"""Agent-host capture hook installation, fail-open entry point, and coverage."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from retro import capture_hooks
from retro.cli import app
from retro.storage import Layout
from tests.task_scorer_helpers import git

runner = CliRunner()
LAUNCHER = ["/opt/retro/bin/python", "-m", "retro.capture_hooks"]


@pytest.fixture
def hook_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "fake-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".copilot").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("COPILOT_HOME", str(home / ".copilot"))
    monkeypatch.delenv("RETRO_HOOK_COMMAND", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(capture_hooks, "hook_launcher", lambda **_: list(LAUNCHER))
    return home


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.email", "retro@example.invalid")
    git(root, "config", "user.name", "Retro Tests")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "initial")
    return root


def _settings(home: Path) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8"))


def _write_claude_settings(home: Path, payload: dict) -> Path:
    path = home / ".claude" / "settings.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


EXISTING_CLAUDE = {
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
        ],
    },
}


def test_targets_resolve_documented_user_level_paths(hook_home: Path):
    targets = {target.host: target for target in capture_hooks.hook_targets()}
    assert targets["claude"].path == hook_home / ".claude" / "settings.json"
    assert targets["codex"].path == hook_home / ".codex" / "hooks.json"
    assert targets["copilot"].path == (
        hook_home / ".copilot" / "hooks" / "retro-capture.json"
    )
    assert all(target.supported for target in targets.values())


def test_install_merges_without_clobbering_existing_hooks(hook_home: Path):
    _write_claude_settings(hook_home, EXISTING_CLAUDE)

    changes = {change.host: change for change in capture_hooks.install_hooks()}

    assert {change.action for change in changes.values()} == {"install"}
    settings = _settings(hook_home)
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert settings["hooks"]["PreToolUse"] == EXISTING_CLAUDE["hooks"]["PreToolUse"]
    start = settings["hooks"]["SessionStart"]
    assert start[0] == {"hooks": [{"type": "command", "command": "echo mine"}]}
    assert "--host claude --event start" in start[1]["hooks"][0]["command"]
    end = settings["hooks"]["SessionEnd"][0]["hooks"][0]
    assert "--host claude --event end" in end["command"]
    assert end["timeout"] == 10
    assert changes["claude"].backup is not None
    assert Path(changes["claude"].backup).read_text(encoding="utf-8") == json.dumps(
        EXISTING_CLAUDE, indent=2
    ) + "\n"


def test_install_writes_host_native_formats(hook_home: Path):
    capture_hooks.install_hooks()

    codex = json.loads((hook_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    assert list(codex["hooks"]) == ["SessionStart", "SessionEnd"]
    assert codex["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 3

    copilot = json.loads(
        (hook_home / ".copilot" / "hooks" / "retro-capture.json").read_text(
            encoding="utf-8"
        )
    )
    assert copilot["version"] == 1
    assert list(copilot["hooks"]) == ["sessionStart", "sessionEnd"]
    entry = copilot["hooks"]["sessionStart"][0]
    assert entry["type"] == "command"
    assert entry["timeoutSec"] == 10
    assert "--host copilot --event start" in entry["command"]


def test_install_is_idempotent(hook_home: Path):
    _write_claude_settings(hook_home, EXISTING_CLAUDE)
    capture_hooks.install_hooks()
    first = (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8")
    backups = sorted((hook_home / ".claude").glob("settings.json.*.bak"))

    changes = capture_hooks.install_hooks()

    assert {change.action for change in changes} == {"unchanged"}
    assert not any(change.changed for change in changes)
    assert (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8") == first
    assert sorted((hook_home / ".claude").glob("settings.json.*.bak")) == backups


def test_install_replaces_a_stale_retro_entry(hook_home: Path):
    _write_claude_settings(
        hook_home,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "/old/python -m retro.capture_hooks "
                                "--host claude --event start",
                            }
                        ]
                    }
                ]
            }
        },
    )

    capture_hooks.install_hooks(["claude"])

    start = _settings(hook_home)["hooks"]["SessionStart"]
    assert len(start) == 1
    assert start[0]["hooks"][0]["command"].startswith(LAUNCHER[0])


def test_dry_run_changes_nothing(hook_home: Path):
    _write_claude_settings(hook_home, EXISTING_CLAUDE)
    before = (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8")

    changes = capture_hooks.install_hooks(dry_run=True)

    assert all(change.dry_run and change.changed for change in changes)
    assert (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8") == before
    assert not list((hook_home / ".claude").glob("*.bak"))
    assert not (hook_home / ".codex" / "hooks.json").exists()
    assert not (hook_home / ".copilot" / "hooks").exists()


def test_uninstall_preserves_foreign_hooks_and_prunes_retro_files(hook_home: Path):
    _write_claude_settings(hook_home, EXISTING_CLAUDE)
    capture_hooks.install_hooks()

    changes = {change.host: change for change in capture_hooks.uninstall_hooks()}

    assert {change.action for change in changes.values()} == {"uninstall"}
    settings = _settings(hook_home)
    assert settings == EXISTING_CLAUDE
    assert not (hook_home / ".codex" / "hooks.json").exists()
    assert not (hook_home / ".copilot" / "hooks" / "retro-capture.json").exists()
    assert changes["codex"].backup is not None
    assert capture_hooks.uninstall_hooks()[0].action == "unchanged"


def test_uninstall_dry_run_keeps_hooks(hook_home: Path):
    capture_hooks.install_hooks(["claude"])
    before = (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8")

    changes = capture_hooks.uninstall_hooks(["claude"], dry_run=True)

    assert changes[0].changed and changes[0].dry_run
    assert (hook_home / ".claude" / "settings.json").read_text(encoding="utf-8") == before


def test_status_tracks_installation(hook_home: Path):
    before = {status.host: status for status in capture_hooks.hook_status()}
    assert all(status.summary() == "missing" for status in before.values())

    capture_hooks.install_hooks(["claude", "codex"])
    after = {status.host: status for status in capture_hooks.hook_status()}

    assert after["claude"].summary() == "installed"
    assert after["claude"].installed
    assert after["codex"].start_installed and after["codex"].end_installed
    assert after["copilot"].summary() == "missing"
    assert after["claude"].to_dict()["state"] == "installed"


def test_status_reports_partial_installation(hook_home: Path):
    capture_hooks.install_hooks(["claude"])
    settings = _settings(hook_home)
    settings["hooks"].pop("SessionEnd")
    _write_claude_settings(hook_home, settings)

    status = capture_hooks.hook_status(["claude"])[0]

    assert status.summary() == "partial"
    assert status.start_installed and not status.end_installed


def test_invalid_json_is_reported_without_editing(hook_home: Path):
    path = hook_home / ".claude" / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    change = capture_hooks.install_hooks(["claude"])[0]

    assert change.action == "error"
    assert not change.changed
    assert path.read_text(encoding="utf-8") == "{not json"
    assert capture_hooks.hook_status(["claude"])[0].message is not None


def test_unknown_host_is_rejected():
    with pytest.raises(ValueError):
        capture_hooks.hook_targets(["emacs"])


def test_hook_launcher_prefers_the_interpreter_module(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("RETRO_HOOK_COMMAND", raising=False)
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(capture_hooks, "_module_importable", lambda path: True)

    assert capture_hooks.hook_launcher(executable=python) == [
        str(python),
        "-m",
        "retro.capture_hooks",
    ]


def test_hook_launcher_falls_back_to_the_console_script(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("RETRO_HOOK_COMMAND", raising=False)
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    script = tmp_path / "retro"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(capture_hooks, "_module_importable", lambda path: False)
    monkeypatch.setattr(capture_hooks.shutil, "which", lambda name: str(script))

    assert capture_hooks.hook_launcher(executable=python) == [
        str(script),
        "capture",
        "hook",
    ]


def test_hook_launcher_honours_the_environment_override(monkeypatch):
    monkeypatch.setenv("RETRO_HOOK_COMMAND", "/usr/bin/env retro capture hook")

    assert capture_hooks.hook_launcher() == [
        "/usr/bin/env",
        "retro",
        "capture",
        "hook",
    ]


def test_hook_command_quotes_paths_with_spaces():
    command = capture_hooks.hook_command(
        "claude", "start", launcher=["/Apps/My Tools/retro", "capture", "hook"]
    )

    assert command.startswith("'/Apps/My Tools/retro' capture hook")
    assert capture_hooks.is_retro_hook_command({"command": command})


@pytest.mark.parametrize(
    ("host", "archive_host", "payload"),
    [
        (
            "claude",
            "claude-code",
            {
                "session_id": "claude-session-1",
                "cwd": None,
                "hook_event_name": "SessionStart",
                "source": "startup",
                "transcript_path": "/tmp/transcript.jsonl",
            },
        ),
        (
            "codex",
            "codex",
            {
                "session_id": "codex-session-1",
                "cwd": None,
                "hook_event_name": "SessionStart",
                "model": "gpt-5",
            },
        ),
        (
            "copilot",
            "vscode-copilot",
            {"sessionId": "copilot-session-1", "cwd": None, "source": "startup"},
        ),
        (
            "copilot",
            "vscode-copilot",
            {
                "session_id": "copilot-session-2",
                "cwd": None,
                "hook_event_name": "SessionStart",
            },
        ),
    ],
)
def test_hook_entry_captures_each_host_payload(
    tmp_path: Path, git_repo: Path, capsys, host, archive_host, payload
):
    archive = tmp_path / "archive"
    payload = {**payload, "cwd": str(git_repo)}

    code = capture_hooks.run_hook_entry(
        host, "start", stream=StringIO(json.dumps(payload)), root=archive
    )

    assert code == 0
    assert capsys.readouterr().out == ""
    session_id = payload.get("session_id") or payload["sessionId"]
    capture = Layout(archive).raw_dir(archive_host, session_id) / "repo_start.json"
    assert capture.is_file()
    state = json.loads(capture.read_text(encoding="utf-8"))
    assert state["schema_version"] == "retro-repo-state-v1"
    assert state["head_sha"]


def test_hook_entry_captures_end_phase_and_never_overwrites(
    tmp_path: Path, git_repo: Path
):
    archive = tmp_path / "archive"
    payload = json.dumps({"session_id": "session-end-1", "cwd": str(git_repo)})
    layout = Layout(archive)

    assert (
        capture_hooks.run_hook_entry(
            "claude", "end", stream=StringIO(payload), root=archive
        )
        == 0
    )
    capture = layout.raw_dir("claude-code", "session-end-1") / "repo_end.json"
    original = capture.read_text(encoding="utf-8")

    (git_repo / "README.md").write_text("# changed\n", encoding="utf-8")
    assert (
        capture_hooks.run_hook_entry(
            "claude", "end", stream=StringIO(payload), root=archive
        )
        == 0
    )

    assert capture.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "stream_text",
    ["not json at all", "", "[]", json.dumps({"cwd": "/tmp"}), json.dumps({"session_id": "../escape"})],
)
def test_hook_entry_is_fail_open_for_bad_payloads(tmp_path: Path, capsys, stream_text):
    archive = tmp_path / "archive"

    assert (
        capture_hooks.run_hook_entry(
            "claude", "start", stream=StringIO(stream_text), root=archive
        )
        == 0
    )

    assert capsys.readouterr().out == ""
    assert not (archive / "raw").exists()


def test_hook_entry_skips_directories_outside_git(tmp_path: Path, capsys):
    archive = tmp_path / "archive"
    plain = tmp_path / "plain"
    plain.mkdir()
    payload = json.dumps({"session_id": "no-git", "cwd": str(plain)})

    assert (
        capture_hooks.run_hook_entry("codex", "start", stream=StringIO(payload), root=archive)
        == 0
    )

    assert capsys.readouterr().out == ""
    assert not (archive / "raw").exists()


def test_hook_entry_skips_missing_directories(tmp_path: Path):
    archive = tmp_path / "archive"
    payload = json.dumps({"session_id": "gone", "cwd": str(tmp_path / "missing")})

    assert (
        capture_hooks.run_hook_entry("claude", "start", stream=StringIO(payload), root=archive)
        == 0
    )
    assert not (archive / "raw").exists()


def test_hook_entry_logs_unknown_host_without_failing(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("RETRO_LOG_DIR", str(tmp_path / "logs"))
    payload = json.dumps({"session_id": "x", "cwd": str(tmp_path)})

    assert capture_hooks.run_hook_entry("emacs", "start", stream=StringIO(payload)) == 0

    assert capsys.readouterr().out == ""
    assert "emacs" in capture_hooks.hook_log_path().read_text(encoding="utf-8")


def test_module_main_is_fail_open(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr("sys.stdin", StringIO(json.dumps({"session_id": "a", "cwd": "/"})))

    assert capture_hooks.main(["--host", "claude", "--event", "start"]) == 0
    assert capture_hooks.main(["--host", "claude"]) == 0
    assert capsys.readouterr().out == ""


def _catalog_record(
    *,
    host: str,
    session_id: str,
    started_at: str,
    git_remote: str | None = "github.com/example/repo",
    excluded: bool = False,
) -> str:
    return json.dumps(
        {
            "schema": 1,
            "key": f"{host}/{session_id}",
            "host": host,
            "session_id": session_id,
            "started_at": started_at,
            "excluded": excluded,
            "git": {"remote": git_remote, "branch": "main" if git_remote else None},
        }
    )


def test_capture_coverage_reads_the_catalog(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    catalog = layout.root / "catalog" / "sessions.jsonl"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "\n".join(
            [
                _catalog_record(host="codex", session_id="covered", started_at=recent),
                _catalog_record(host="codex", session_id="bare", started_at=recent),
                _catalog_record(host="codex", session_id="old", started_at=stale),
                _catalog_record(
                    host="codex", session_id="nogit", started_at=recent, git_remote=None
                ),
                _catalog_record(
                    host="codex", session_id="skipped", started_at=recent, excluded=True
                ),
                "{ broken",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    covered = layout.raw_dir("codex", "covered")
    covered.mkdir(parents=True)
    (covered / "repo_start.json").write_text("{}", encoding="utf-8")
    (covered / "repo_end.json").write_text("{}", encoding="utf-8")
    layout.raw_dir("codex", "bare").mkdir(parents=True)

    coverage = capture_hooks.capture_coverage(layout, days=30)

    assert coverage.source == "catalog"
    assert coverage.git_filtered
    assert coverage.sessions == 2
    assert coverage.with_start == 1
    assert coverage.with_end == 1
    assert coverage.ratio == pytest.approx(0.5)
    assert "1/2 sessions (50%)" in coverage.summary()


def test_capture_coverage_falls_back_to_raw_sessions(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    covered = layout.raw_dir("claude-code", "one")
    covered.mkdir(parents=True)
    (covered / "repo_start.json").write_text("{}", encoding="utf-8")
    layout.raw_dir("claude-code", "two").mkdir(parents=True)
    stale = layout.raw_dir("claude-code", "three")
    stale.mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).timestamp()
    os.utime(stale, (old, old))

    coverage = capture_hooks.capture_coverage(layout, days=30)

    assert coverage.source == "raw"
    assert not coverage.git_filtered
    assert coverage.sessions == 2
    assert coverage.with_start == 1
    assert coverage.to_dict()["ratio"] == pytest.approx(0.5)


def test_capture_coverage_without_sessions(tmp_path: Path):
    coverage = capture_hooks.capture_coverage(Layout(tmp_path / "archive"), days=30)

    assert coverage.sessions == 0
    assert coverage.ratio is None
    assert "no sessions" in coverage.summary()


# ---- CLI ---------------------------------------------------------------------


def test_cli_hooks_install_status_and_uninstall(hook_home: Path):
    dry = runner.invoke(app, ["capture", "hooks", "install", "--dry-run"])
    assert dry.exit_code == 0
    assert not (hook_home / ".codex" / "hooks.json").exists()

    installed = runner.invoke(app, ["capture", "hooks", "install"])
    assert installed.exit_code == 0
    assert (hook_home / ".codex" / "hooks.json").is_file()

    status = runner.invoke(app, ["capture", "hooks", "status", "--json"])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert {entry["host"] for entry in payload} == {"claude", "codex", "copilot"}
    assert all(entry["installed"] for entry in payload)

    removed = runner.invoke(app, ["capture", "hooks", "uninstall", "--host", "codex"])
    assert removed.exit_code == 0
    assert not (hook_home / ".codex" / "hooks.json").exists()
    assert (hook_home / ".claude" / "settings.json").is_file()


def test_cli_hooks_install_rejects_unknown_host(hook_home: Path):
    result = runner.invoke(app, ["capture", "hooks", "install", "--host", "emacs"])

    assert result.exit_code != 0


def test_cli_hook_entry_reads_stdin(tmp_path: Path, git_repo: Path, hook_home: Path):
    archive = tmp_path / "archive"
    payload = json.dumps({"session_id": "cli-session", "cwd": str(git_repo)})

    result = runner.invoke(
        app,
        ["capture", "hook", "--host", "claude", "--event", "start", "--root", str(archive)],
        input=payload,
    )

    assert result.exit_code == 0
    assert result.stdout == ""
    assert (
        Layout(archive).raw_dir("claude-code", "cli-session") / "repo_start.json"
    ).is_file()


def test_cli_hook_entry_never_fails_on_bad_input(tmp_path: Path, hook_home: Path):
    result = runner.invoke(
        app, ["capture", "hook", "--host", "claude", "--event", "start"], input="nonsense"
    )

    assert result.exit_code == 0
    assert result.stdout == ""


def test_cli_setup_installs_hooks_only_when_opted_in(hook_home: Path, tmp_path: Path):
    archive = tmp_path / "setup-archive"
    dashboard = tmp_path / "setup-dashboard"
    base = [
        "setup",
        "--archive-root",
        str(archive),
        "--dashboard-dir",
        str(dashboard),
        "--no-schedule",
    ]

    default_run = runner.invoke(app, base)
    assert default_run.exit_code == 0
    assert not (hook_home / ".codex" / "hooks.json").exists()

    opted_in = runner.invoke(app, [*base, "--capture-hooks"])
    assert opted_in.exit_code == 0
    assert (hook_home / ".codex" / "hooks.json").is_file()
    assert all(status.installed for status in capture_hooks.hook_status())


def test_cli_doctor_reports_hooks_and_coverage(hook_home: Path):
    capture_hooks.install_hooks(["claude"])

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "capture hook claude" in result.stdout
    assert "capture coverage" in result.stdout
