"""Read-only metadata helpers shared by the profile builder and session sources."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_YAML_SCALAR_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]+(.*?)[ \t]*$")
_TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/", "/var/tmp/")

_META_FILES = ("thread.json", "import_meta.json")


def read_raw_meta(raw_dir: Path) -> dict[str, Any]:
    """Best-effort session metadata from the immutable raw copy."""
    meta: dict[str, Any] = {}
    for name in _META_FILES:
        path = raw_dir / name
        if not path.exists():
            continue
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                meta.setdefault(key, value)
    workspace = raw_dir / "sidecars" / "workspace.yaml"
    if workspace.exists():
        for key, value in _read_scalar_yaml(workspace).items():
            meta.setdefault(key, value)
    return meta


def _read_scalar_yaml(path: Path) -> dict[str, str]:
    """Parse top-level `key: value` scalars only; block scalars are skipped."""
    out: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line or line[0] in " \t\n#-":
                    continue
                match = _YAML_SCALAR_RE.match(line.rstrip("\n"))
                if not match:
                    continue
                value = match.group(2).strip().strip("'\"")
                if value and value not in ("|", "|-", ">", ">-"):
                    out[match.group(1)] = value
    except OSError:
        return out
    return out


def meta_cwd(meta: dict[str, Any]) -> str | None:
    for key in ("cwd", "current_working_directory", "workspace", "project_path"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    payload = meta.get("payload")
    if isinstance(payload, dict):
        value = payload.get("cwd")
        if isinstance(value, str) and value:
            return value
    return None


def is_temp_cwd(cwd: str | None) -> bool:
    """True for scratch/evaluation sessions that run out of a temp directory."""
    if not cwd:
        return False
    path = cwd if cwd.startswith("/") else "/" + cwd
    return path.startswith(_TEMP_PREFIXES)
