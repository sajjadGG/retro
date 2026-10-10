from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from retro.config import RetroConfig, save_config
from retro.schedule import (
    install_schedule,
    parse_interval,
    schedule_status,
    uninstall_schedule,
)


def test_parse_interval():
    assert parse_interval("15m") == 900
    assert parse_interval("1h") == 3600
    assert parse_interval("120") == 120
    with pytest.raises(ValueError):
        parse_interval("30s")
    with pytest.raises(ValueError):
        parse_interval("later")


def test_launch_agent_generation(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("retro.schedule.sys.platform", "darwin")
    path = tmp_path / "io.retro.sync.plist"
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("RETRO_LAUNCH_AGENT_PATH", str(path))

    result = install_schedule(900, python_executable=python, load=False)

    assert result == path
    payload = plistlib.loads(path.read_bytes())
    assert payload["Label"] == "io.retro.sync"
    assert payload["StartInterval"] == 900
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        str(python.absolute()),
        "-m",
        "retro.cli",
        "sync",
        "--scheduled",
    ]
    assert payload["StandardOutPath"].endswith("sync.log")
    environment = payload["EnvironmentVariables"]
    assert environment["RETRO_DATA_DIR"] == str(tmp_path / "retro-user-data")
    assert environment["RETRO_LOG_DIR"] == str(tmp_path / "retro-user-data" / "logs")
    assert environment["RETRO_CONFIG_PATH"] == str(
        tmp_path / "retro-user-data" / "config.json"
    )


def test_launch_agent_rejects_project_virtualenv(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("retro.schedule.sys.platform", "darwin")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="project/worktree virtualenv"):
        install_schedule(900, python_executable=python, load=False)


def test_schedule_status_and_uninstall(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("retro.schedule.sys.platform", "darwin")
    path = tmp_path / "io.retro.sync.plist"
    path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("RETRO_LAUNCH_AGENT_PATH", str(path))

    def fake_launchctl(arguments, check=True):
        assert arguments[0] in {"print", "bootout"}
        return subprocess.CompletedProcess(["launchctl"], 0, "loaded", "")

    monkeypatch.setattr("retro.schedule._run_launchctl", fake_launchctl)

    status = schedule_status()
    assert status["installed"] is True
    assert status["loaded"] is True
    assert uninstall_schedule() == path
    assert not path.exists()


def test_schedule_reports_unsupported_off_macos(monkeypatch, tmp_path: Path):
    path = tmp_path / "io.retro.sync.plist"
    monkeypatch.setattr("retro.schedule.sys.platform", "linux")
    monkeypatch.setenv("RETRO_LAUNCH_AGENT_PATH", str(path))

    status = schedule_status()

    assert status == {
        "supported": False,
        "installed": False,
        "loaded": False,
        "path": str(path),
    }
    with pytest.raises(RuntimeError, match="supported on macOS"):
        install_schedule(900, python_executable=tmp_path / "python", load=False)


def test_schedule_status_surfaces_external_volume_tcc_denial(
    monkeypatch,
    tmp_path: Path,
):
    volume = Path("/Volumes/retro-schedule-tcc")
    python_app = Path("/Applications/Xcode.app/Test/Python.app")
    save_config(
        RetroConfig(
            archive_root=str(volume / "archive"),
            dashboard_dir=str(tmp_path / "dashboard"),
        )
    )
    monkeypatch.setattr("retro.schedule.sys.platform", "darwin")
    monkeypatch.setattr(
        "retro.schedule._run_launchctl",
        lambda args, check=False: subprocess.CompletedProcess(
            args, 1, "", "not loaded"
        ),
    )
    monkeypatch.setattr("retro.archive_health.os.path.ismount", lambda path: True)
    real_stat = os.stat

    def denied_stat(path, *args, **kwargs):
        if str(path).startswith(str(volume)):
            raise PermissionError(1, "Operation not permitted", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("retro.archive_health.os.stat", denied_stat)
    monkeypatch.setattr("retro.archive_health.python_app_bundle", lambda: python_app)

    status = schedule_status()

    access = status["archive_access"]
    assert access["accessible"] is False
    assert str(python_app) in access["message"]
    assert "Removable Volumes" in access["message"]


def test_launch_agent_forwards_archive_and_source_overrides(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("retro.schedule.sys.platform", "darwin")
    path = tmp_path / "io.retro.sync.plist"
    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("RETRO_LAUNCH_AGENT_PATH", str(path))
    monkeypatch.setenv("RETRO_ROOT", "/Volumes/Archive/rollout-memory")
    monkeypatch.setenv("RETRO_DASHBOARD_DIR", "/Volumes/Archive/dashboard")
    monkeypatch.setenv("CODEX_HOME", "/Users/me/.codex-work")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    install_schedule(900, python_executable=python, load=False)

    environment = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
    assert environment["RETRO_ROOT"] == "/Volumes/Archive/rollout-memory"
    assert environment["RETRO_DASHBOARD_DIR"] == "/Volumes/Archive/dashboard"
    assert environment["CODEX_HOME"] == "/Users/me/.codex-work"
    assert "CLAUDE_CONFIG_DIR" not in environment
