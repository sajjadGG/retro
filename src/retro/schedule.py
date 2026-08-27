"""Per-user periodic scheduler integration."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import config_path, user_log_dir

LAUNCHD_LABEL = "io.retro.sync"


def parse_interval(value: str) -> int:
    raw = value.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600}
    if raw and raw[-1] in multipliers:
        number = raw[:-1]
        multiplier = multipliers[raw[-1]]
    else:
        number = raw
        multiplier = 1
    try:
        seconds = int(number) * multiplier
    except ValueError as exc:
        raise ValueError(f"Invalid interval {value!r}; use values like 15m or 1h") from exc
    if seconds < 60:
        raise ValueError("Periodic interval must be at least 60 seconds")
    return seconds


def launch_agent_path() -> Path:
    override = os.environ.get("RETRO_LAUNCH_AGENT_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_service() -> str:
    return f"{launchd_domain()}/{LAUNCHD_LABEL}"


def install_schedule(
    interval_seconds: int,
    *,
    python_executable: Path | None = None,
    load: bool = True,
) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("Periodic scheduler installation is currently supported on macOS")
    if interval_seconds < 60:
        raise ValueError("Periodic interval must be at least 60 seconds")
    executable = (python_executable or Path(sys.executable)).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(f"Python executable does not exist: {executable}")
    if _is_unstable_executable(executable):
        raise RuntimeError(
            "Refusing to install launchd from a project/worktree virtualenv. "
            "Install Retro into pipx or a persistent per-user runtime first."
        )

    logs = user_log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(executable),
            "-m",
            "retro.cli",
            "sync",
            "--scheduled",
        ],
        "RunAtLoad": True,
        "StartInterval": interval_seconds,
        "ProcessType": "Background",
        "StandardOutPath": str(logs / "sync.log"),
        "StandardErrorPath": str(logs / "sync.error.log"),
        "EnvironmentVariables": {
            "RETRO_CONFIG_PATH": str(config_path()),
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        },
    }
    path = launch_agent_path()
    _atomic_write_bytes(path, plistlib.dumps(payload, sort_keys=True))
    if load:
        _bootout(ignore_failure=True)
        _run_launchctl(["bootstrap", launchd_domain(), str(path)])
    return path


def schedule_status() -> dict[str, Any]:
    path = launch_agent_path()
    if sys.platform != "darwin":
        return {
            "supported": False,
            "installed": path.exists(),
            "loaded": False,
            "path": str(path),
        }
    result = _run_launchctl(["print", launchd_service()], check=False)
    return {
        "supported": True,
        "installed": path.exists(),
        "loaded": result.returncode == 0,
        "path": str(path),
        "service": launchd_service(),
        "detail": result.stdout.strip() if result.returncode == 0 else result.stderr.strip(),
    }


def run_now() -> None:
    if sys.platform != "darwin":
        raise RuntimeError("Periodic scheduler is currently supported on macOS")
    status = schedule_status()
    if not status["loaded"]:
        raise RuntimeError("Retro LaunchAgent is not loaded")
    _run_launchctl(["kickstart", launchd_service()])


def uninstall_schedule() -> Path:
    if sys.platform == "darwin":
        _bootout(ignore_failure=True)
    path = launch_agent_path()
    if path.exists():
        path.unlink()
    return path


def _bootout(*, ignore_failure: bool) -> None:
    result = _run_launchctl(["bootout", launchd_service()], check=False)
    if result.returncode != 0 and not ignore_failure:
        raise RuntimeError(result.stderr.strip() or "launchctl bootout failed")


def _run_launchctl(
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["launchctl", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip()
            or result.stdout.strip()
            or f"launchctl {' '.join(arguments)} failed"
        )
    return result


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _is_unstable_executable(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    return ".venv" in lowered or "venv" in lowered or any(
        part.endswith(".worktrees") for part in lowered
    )
