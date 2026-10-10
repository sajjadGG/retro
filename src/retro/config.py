"""Per-user Retro configuration and platform path resolution."""
from __future__ import annotations

import copy
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import atomic_write_text

CONFIG_PATH_ENV = "RETRO_CONFIG_PATH"
ROOT_ENV = "RETRO_ROOT"
LEGACY_ROOT_ENV = "RETRO_ARTIFACT_ROOT"
DASHBOARD_ENV = "RETRO_DASHBOARD_DIR"

TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
RULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RULE_KEYS = {"id", "description", "enabled", "match", "tags"}
_RULE_LIST_FIELDS = {
    "host",
    "agent",
    "client",
    "cwd",
    "project",
    "remote",
    "branch",
    "model",
}
_RULE_BOOL_FIELDS = {"interactive", "is_subagent"}
_RULE_REGEX_FIELDS = {"title", "prompt", "session_id"}
_RULE_COUNT_FIELDS = {"min_user_messages", "max_user_messages"}
_RULE_DATE_FIELDS = {"started_after", "started_before"}
_RULE_MATCH_FIELDS = (
    _RULE_LIST_FIELDS
    | _RULE_BOOL_FIELDS
    | _RULE_REGEX_FIELDS
    | _RULE_COUNT_FIELDS
    | _RULE_DATE_FIELDS
)
_CONFIG_KEYS = {
    "archive_root",
    "dashboard_dir",
    "sync_interval_seconds",
    "derived_interval_seconds",
    "sync_on_login",
    "exclude_tags",
    "disabled_rules",
    "rules",
    "projects",
}


@dataclass(frozen=True)
class RetroConfig:
    archive_root: str
    dashboard_dir: str
    sync_interval_seconds: int = 900
    derived_interval_seconds: int = 21600
    sync_on_login: bool = True
    exclude_tags: list[str] = field(default_factory=lambda: ["ignore"])
    disabled_rules: list[str] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)
    projects: dict[str, Any] = field(
        default_factory=lambda: {"aliases": {}, "paths": [], "names": {}}
    )
    extra: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        payload = copy.deepcopy(self.extra)
        payload.update(
            {
                "archive_root": self.archive_root,
                "dashboard_dir": self.dashboard_dir,
                "sync_interval_seconds": self.sync_interval_seconds,
                "derived_interval_seconds": self.derived_interval_seconds,
                "sync_on_login": self.sync_on_login,
                "exclude_tags": list(self.exclude_tags),
                "disabled_rules": list(self.disabled_rules),
                "rules": copy.deepcopy(self.rules),
                "projects": copy.deepcopy(self.projects),
            }
        )
        return payload


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
    try:
        return _config_from_raw(raw, defaults)
    except ValueError as exc:
        raise ValueError(f"Invalid Retro config at {path}: {exc}") from exc


def save_config(config: RetroConfig) -> Path:
    normalized = _config_from_raw(config.to_dict(), default_config())
    path = config_path()
    atomic_write_text(
        path,
        json.dumps(normalized.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )
    return path


def update_config(**updates: Any) -> RetroConfig:
    current = load_config().to_dict()
    current.update(updates)
    config = _config_from_raw(current, default_config())
    save_config(config)
    return config


def validate_rule_spec(rule: Any, *, where: str = "rule") -> dict[str, Any]:
    if not isinstance(rule, dict):
        raise ValueError(f"{where} must be an object")
    unknown = sorted(set(rule) - _RULE_KEYS)
    if unknown:
        raise ValueError(f"{where} has unknown keys: {', '.join(unknown)}")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not RULE_ID_PATTERN.fullmatch(rule_id):
        raise ValueError(
            f"{where}.id must match {RULE_ID_PATTERN.pattern}"
        )
    description = rule.get("description", "")
    if not isinstance(description, str):
        raise ValueError(f"{where}.description must be a string")
    enabled = rule.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}.enabled must be boolean")
    match = rule.get("match", {})
    if not isinstance(match, dict):
        raise ValueError(f"{where}.match must be an object")
    unknown_match = sorted(set(match) - _RULE_MATCH_FIELDS)
    if unknown_match:
        raise ValueError(
            f"{where}.match has unknown fields: {', '.join(unknown_match)}"
        )
    normalized_match: dict[str, Any] = {}
    for name, value in match.items():
        location = f"{where}.match.{name}"
        if name in _RULE_LIST_FIELDS:
            normalized_match[name] = _string_list(value, location)
        elif name in _RULE_BOOL_FIELDS:
            if not isinstance(value, bool):
                raise ValueError(f"{location} must be boolean")
            normalized_match[name] = value
        elif name in _RULE_REGEX_FIELDS:
            if not isinstance(value, str):
                raise ValueError(f"{location} must be a regex string")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{location} is not a valid regex: {exc}") from exc
            normalized_match[name] = value
        elif name in _RULE_COUNT_FIELDS:
            if type(value) is not int or value < 0:
                raise ValueError(f"{location} must be a non-negative integer")
            normalized_match[name] = value
        elif name in _RULE_DATE_FIELDS:
            if not isinstance(value, str) or _parse_config_datetime(value) is None:
                raise ValueError(f"{location} must be an ISO-8601 date or timestamp")
            normalized_match[name] = value
    minimum = normalized_match.get("min_user_messages")
    maximum = normalized_match.get("max_user_messages")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError(
            f"{where}.match.min_user_messages must not exceed max_user_messages"
        )
    tags = _tag_list(rule.get("tags"), f"{where}.tags", allow_empty=False)
    return {
        "id": rule_id,
        "description": description,
        "enabled": enabled,
        "match": normalized_match,
        "tags": tags,
    }


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


def _config_from_raw(raw: dict[str, Any], defaults: RetroConfig) -> RetroConfig:
    archive_root = _configured_path(raw.get("archive_root"), defaults.archive_root)
    dashboard_dir = _configured_path(raw.get("dashboard_dir"), defaults.dashboard_dir)
    interval = raw.get("sync_interval_seconds", defaults.sync_interval_seconds)
    if type(interval) is not int or interval < 60:
        raise ValueError("sync_interval_seconds must be an integer of at least 60")
    derived_interval = raw.get(
        "derived_interval_seconds",
        defaults.derived_interval_seconds,
    )
    if type(derived_interval) is not int or derived_interval < 60:
        raise ValueError("derived_interval_seconds must be an integer of at least 60")
    sync_on_login = raw.get("sync_on_login", defaults.sync_on_login)
    if not isinstance(sync_on_login, bool):
        raise ValueError("sync_on_login must be boolean")
    exclude_tags = _tag_list(
        raw.get("exclude_tags", defaults.exclude_tags),
        "exclude_tags",
        allow_empty=True,
    )
    disabled_rules = _string_list(
        raw.get("disabled_rules", defaults.disabled_rules),
        "disabled_rules",
    )
    for index, value in enumerate(disabled_rules):
        if not RULE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"disabled_rules[{index}] must match {RULE_ID_PATTERN.pattern}"
            )
    raw_rules = raw.get("rules", defaults.rules)
    if not isinstance(raw_rules, list):
        raise ValueError("rules must be an array")
    rules = [
        validate_rule_spec(rule, where=f"rules[{index}]")
        for index, rule in enumerate(raw_rules)
    ]
    ids = [rule["id"] for rule in rules]
    reserved_ids = sorted(rule_id for rule_id in ids if rule_id.startswith("builtin."))
    if reserved_ids:
        raise ValueError(
            f"rules uses reserved built-in ids: {', '.join(reserved_ids)}"
        )
    duplicate_ids = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicate_ids:
        raise ValueError(f"rules contains duplicate ids: {', '.join(duplicate_ids)}")
    projects = _validate_projects(raw.get("projects", defaults.projects))
    return RetroConfig(
        archive_root=archive_root,
        dashboard_dir=dashboard_dir,
        sync_interval_seconds=interval,
        derived_interval_seconds=derived_interval,
        sync_on_login=sync_on_login,
        exclude_tags=exclude_tags,
        disabled_rules=list(dict.fromkeys(disabled_rules)),
        rules=rules,
        projects=projects,
        extra={
            key: copy.deepcopy(value)
            for key, value in raw.items()
            if key not in _CONFIG_KEYS
        },
    )


def _validate_projects(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("projects must be an object")
    unknown = sorted(set(value) - {"aliases", "paths", "names"})
    if unknown:
        raise ValueError(f"projects has unknown keys: {', '.join(unknown)}")
    aliases = _string_mapping(value.get("aliases", {}), "projects.aliases")
    names = _string_mapping(value.get("names", {}), "projects.names")
    raw_paths = value.get("paths", [])
    if not isinstance(raw_paths, list):
        raise ValueError("projects.paths must be an array")
    paths: list[dict[str, str]] = []
    for index, entry in enumerate(raw_paths):
        where = f"projects.paths[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be an object")
        unknown_entry = sorted(set(entry) - {"glob", "project"})
        if unknown_entry:
            raise ValueError(f"{where} has unknown keys: {', '.join(unknown_entry)}")
        glob_value = entry.get("glob")
        project = entry.get("project")
        if not isinstance(glob_value, str) or not glob_value.strip():
            raise ValueError(f"{where}.glob must be a non-empty string")
        if not isinstance(project, str) or not project.strip():
            raise ValueError(f"{where}.project must be a non-empty string")
        paths.append({"glob": glob_value, "project": project})
    for source in aliases:
        seen = {source}
        current = source
        while current in aliases:
            current = aliases[current]
            if current in seen:
                raise ValueError(f"projects.aliases contains a cycle at {current!r}")
            seen.add(current)
    return {"aliases": aliases, "paths": paths, "names": names}


def _string_mapping(value: Any, where: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{where} keys must be non-empty strings")
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}[{key!r}] must be a non-empty string")
        out[key] = item
    return out


def _string_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be an array of strings")
    out: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{where}[{index}] must be a non-empty string")
        out.append(item)
    return list(dict.fromkeys(out))


def _tag_list(value: Any, where: str, *, allow_empty: bool) -> list[str]:
    tags = _string_list(value, where)
    if not allow_empty and not tags:
        raise ValueError(f"{where} must contain at least one tag")
    for index, tag in enumerate(tags):
        if not TAG_PATTERN.fullmatch(tag):
            raise ValueError(f"{where}[{index}] must match {TAG_PATTERN.pattern}")
    return tags


def _parse_config_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _configured_path(value: Any, fallback: str) -> str:
    raw = value if isinstance(value, str) and value.strip() else fallback
    return str(Path(raw).expanduser().resolve())
