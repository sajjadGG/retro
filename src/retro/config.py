"""Per-user Retro configuration and platform path resolution."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .utils import atomic_write_text

CONFIG_PATH_ENV = "RETRO_CONFIG_PATH"
ROOT_ENV = "RETRO_ROOT"
LEGACY_ROOT_ENV = "RETRO_ARTIFACT_ROOT"
DASHBOARD_ENV = "RETRO_DASHBOARD_DIR"


@dataclass(frozen=True)
class RetroConfig:
    archive_root: str
    dashboard_dir: str
    sync_interval_seconds: int = 900
    sync_on_login: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def user_data_dir() -> Path:
    override = os.environ.get("RETRO_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "retro"
    if sys.platform == "win32":
        app_data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        return app_data / "retro"
    data_home = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return data_home / "retro"


def user_state_dir() -> Path:
    return user_data_dir() / "state"


def user_log_dir() -> Path:
    override = os.environ.get("RETRO_LOG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Logs" / "retro"
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return local / "retro" / "logs"
    state_home = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
    return state_home / "retro"


def config_path() -> Path:
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return user_data_dir() / "config.json"


def default_config() -> RetroConfig:
    data_dir = user_data_dir()
    return RetroConfig(
        archive_root=str(data_dir / "rollout-memory"),
        dashboard_dir=str(data_dir / "dashboard"),
    )


def load_config() -> RetroConfig:
    path = config_path()
    defaults = default_config()
    if not path.exists():
        return defaults
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid Retro config at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Retro config at {path} must be a JSON object")

    archive_root = _configured_path(raw.get("archive_root"), defaults.archive_root)
    dashboard_dir = _configured_path(raw.get("dashboard_dir"), defaults.dashboard_dir)
    interval = raw.get("sync_interval_seconds", defaults.sync_interval_seconds)
    if not isinstance(interval, int) or interval < 60:
        raise ValueError("sync_interval_seconds must be an integer of at least 60")
    sync_on_login = raw.get("sync_on_login", defaults.sync_on_login)
    if not isinstance(sync_on_login, bool):
        raise ValueError("sync_on_login must be boolean")
    return RetroConfig(
        archive_root=archive_root,
        dashboard_dir=dashboard_dir,
        sync_interval_seconds=interval,
        sync_on_login=sync_on_login,
    )


def save_config(config: RetroConfig) -> Path:
    path = config_path()
    atomic_write_text(
        path,
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    return path


def update_config(**updates: Any) -> RetroConfig:
    current = load_config().to_dict()
    current.update(updates)
    config = RetroConfig(
        archive_root=_configured_path(current.get("archive_root"), default_config().archive_root),
        dashboard_dir=_configured_path(
            current.get("dashboard_dir"),
            default_config().dashboard_dir,
        ),
        sync_interval_seconds=int(current.get("sync_interval_seconds", 900)),
        sync_on_login=bool(current.get("sync_on_login", True)),
    )
    save_config(config)
    return config


def resolve_archive_root(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    for env_name in (ROOT_ENV, LEGACY_ROOT_ENV):
        value = os.environ.get(env_name)
        if value:
            return Path(value).expanduser().resolve()
    return Path(load_config().archive_root).expanduser().resolve()


def resolve_dashboard_dir(explicit: Path | str | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser().resolve()
    value = os.environ.get(DASHBOARD_ENV)
    if value:
        return Path(value).expanduser().resolve()
    return Path(load_config().dashboard_dir).expanduser().resolve()


def _configured_path(value: Any, fallback: str) -> str:
    raw = value if isinstance(value, str) and value.strip() else fallback
    return str(Path(raw).expanduser().resolve())
