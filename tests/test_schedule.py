from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

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
