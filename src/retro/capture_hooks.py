"""Agent-host hooks that capture repository state at session boundaries.

Installs user-level session start/end hooks that invoke ``retro capture`` so
``raw/<host>/<session>/repo_start.json`` and ``repo_end.json`` exist without a
manual step. The hook entry point is fail-open: it never fails a session, never
writes to stdout (Claude Code injects ``SessionStart`` stdout into the model
context), and skips silently outside Git repositories.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any

from .config import user_log_dir
from .schema import SESSION_ID_PATTERN
from .storage import Layout, default_layout
from .utils import atomic_write_text

HOOK_HOSTS: tuple[str, ...] = ("claude", "codex", "copilot")
HOOK_PHASES: tuple[str, ...] = ("start", "end")
HOOK_COMMAND_ENV = "RETRO_HOOK_COMMAND"
HOOK_LOG_NAME = "capture-hooks.log"
HOOK_MARKERS: tuple[str, ...] = ("retro.capture_hooks", "capture hook --host")

CLAUDE_DOCS_URL = "https://code.claude.com/docs/en/hooks"
CODEX_DOCS_URL = "https://developers.openai.com/codex/hooks"
COPILOT_DOCS_URL = "https://docs.github.com/en/copilot/reference/hooks-reference"

_PHASE_ALIASES = {
    "start": "start",
    "sessionstart": "start",
    "session_start": "start",
    "end": "end",
    "sessionend": "end",
    "session_end": "end",
}
_BACKUP_SUFFIX = ".bak"


class HookConfigError(RuntimeError):
    """A host hook configuration file cannot be read or merged safely."""


@dataclass(frozen=True)
class HookTarget:
    host: str
    label: str
    path: Path
    event_names: dict[str, str]
    grouped: bool
    timeout_key: str
    timeouts: dict[str, int]
    docs_url: str
    document_defaults: dict[str, Any] = field(default_factory=dict)
    retro_owned_file: bool = False
    supported: bool = True
    unsupported_reason: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class HookStatus:
    host: str
    label: str
    path: str
    supported: bool
    exists: bool
    start_installed: bool
    end_installed: bool
    command: str | None = None
    docs_url: str | None = None
    message: str | None = None

    @property
    def installed(self) -> bool:
        return self.start_installed and self.end_installed

    def summary(self) -> str:
        if not self.supported:
            return "unsupported"
        if self.installed:
            return "installed"
        if self.start_installed or self.end_installed:
            return "partial"
        return "missing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "label": self.label,
            "path": self.path,
            "supported": self.supported,
            "exists": self.exists,
            "start_installed": self.start_installed,
            "end_installed": self.end_installed,
            "installed": self.installed,
            "state": self.summary(),
            "command": self.command,
            "docs_url": self.docs_url,
            "message": self.message,
        }


@dataclass(frozen=True)
class HookChange:
    host: str
    label: str
    path: str
    action: str
    changed: bool
    dry_run: bool = False
    backup: str | None = None
    command: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "label": self.label,
            "path": self.path,
            "action": self.action,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "backup": self.backup,
            "command": self.command,
            "message": self.message,
        }


@dataclass(frozen=True)
class CaptureCoverage:
    days: int
    source: str
    sessions: int
    with_start: int
    with_end: int
    git_filtered: bool

    @property
    def ratio(self) -> float | None:
        if self.sessions <= 0:
            return None
        return self.with_start / self.sessions

    def summary(self) -> str:
        if self.sessions <= 0:
            return f"no sessions in the last {self.days}d"
        share = self.ratio or 0.0
        return (
            f"{self.with_start}/{self.sessions} sessions ({share * 100:.0f}%) "
            f"have repo_start.json"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "source": self.source,
            "sessions": self.sessions,
            "with_start": self.with_start,
            "with_end": self.with_end,
            "git_filtered": self.git_filtered,
            "ratio": self.ratio,
        }


# ---- host configuration locations -------------------------------------------


def _first_path(value: str | None) -> Path | None:
    if not value:
        return None
    first = value.split(",")[0].strip()
    if not first:
        return None
    return Path(first).expanduser()


def claude_settings_path() -> Path:
    home = _first_path(os.environ.get("CLAUDE_CONFIG_DIR")) or Path.home() / ".claude"
    return home / "settings.json"


def codex_hooks_path() -> Path:
    home = _first_path(os.environ.get("CODEX_HOME")) or Path.home() / ".codex"
    return home / "hooks.json"


def copilot_hooks_path() -> Path:
    explicit = _first_path(os.environ.get("COPILOT_HOME"))
    if explicit is None:
        state_home = Path(os.environ.get("XDG_STATE_HOME", str(Path.home()))).expanduser()
        explicit = state_home / ".copilot"
    return explicit / "hooks" / "retro-capture.json"


def hook_targets(hosts: Sequence[str] | None = None) -> list[HookTarget]:
    targets = [
        HookTarget(
            host="claude",
            label="Claude Code",
            path=claude_settings_path(),
            event_names={"start": "SessionStart", "end": "SessionEnd"},
            grouped=True,
            timeout_key="timeout",
            timeouts={"start": 10, "end": 10},
            docs_url=CLAUDE_DOCS_URL,
        ),
        HookTarget(
            host="codex",
            label="Codex CLI",
            path=codex_hooks_path(),
            event_names={"start": "SessionStart", "end": "SessionEnd"},
            grouped=True,
            timeout_key="timeout",
            timeouts={"start": 10, "end": 3},
            docs_url=CODEX_DOCS_URL,
            note="SessionEnd hooks are capped at 3 seconds by Codex",
        ),
        HookTarget(
            host="copilot",
            label="GitHub Copilot CLI",
            path=copilot_hooks_path(),
            event_names={"start": "sessionStart", "end": "sessionEnd"},
            grouped=False,
            timeout_key="timeoutSec",
            timeouts={"start": 10, "end": 10},
            docs_url=COPILOT_DOCS_URL,
            document_defaults={"version": 1},
            retro_owned_file=True,
            note=(
                "covers Copilot CLI and Copilot sessions on the VS Code Agent Host; "
                "the VS Code Local harness has no SessionEnd event"
            ),
        ),
    ]
    if hosts is None:
        return targets
    wanted = [_normalize_host(host) for host in hosts]
    by_host = {target.host: target for target in targets}
    return [by_host[host] for host in wanted]


def _normalize_host(host: str) -> str:
    value = host.strip().lower()
    aliases = {
        "claude": "claude",
        "claude-code": "claude",
        "claudecode": "claude",
        "cc": "claude",
        "codex": "codex",
        "cx": "codex",
        "copilot": "copilot",
        "copilot-cli": "copilot",
        "gh-copilot": "copilot",
        "vscode-copilot": "copilot",
        "vscode": "copilot",
    }
    if value not in aliases:
        raise ValueError(f"unknown hook host {host!r}; use {'|'.join(HOOK_HOSTS)}")
    return aliases[value]


def normalize_phase(event: str) -> str:
    value = str(event).strip().lower()
    if value not in _PHASE_ALIASES:
        raise ValueError(f"unknown hook event {event!r}; use start|end")
    return _PHASE_ALIASES[value]


# ---- hook command resolution -------------------------------------------------


def _module_importable(executable: Path) -> bool:
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHONPATH"
    }
    try:
        result = subprocess.run(
            [str(executable), "-c", "import retro.capture_hooks"],
            check=False,
            capture_output=True,
            cwd=str(executable.parent if executable.parent.is_dir() else Path.home()),
            env=environment,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def hook_launcher(*, executable: Path | None = None) -> list[str]:
    """Return the argv prefix that runs the hook entry point.

    An absolute interpreter path is preferred over the ``retro`` console script
    because it survives shells that never activate the installing virtualenv,
    and because importing ``retro.capture_hooks`` is far cheaper than loading
    the full Typer CLI inside a session-end timeout.
    """
    override = os.environ.get(HOOK_COMMAND_ENV)
    if override:
        return shlex.split(override)
    python = Path(executable or sys.executable)
    if python.is_file() and _module_importable(python):
        return [str(python), "-m", "retro.capture_hooks"]
    found = shutil.which("retro")
    if found:
        return [str(Path(found).resolve()), "capture", "hook"]
    return [str(python), "-m", "retro.capture_hooks"]


def hook_command(
    host: str,
    phase: str,
    *,
    launcher: Sequence[str] | None = None,
    root: Path | None = None,
) -> str:
    parts = list(launcher) if launcher is not None else hook_launcher()
    parts += ["--host", _normalize_host(host), "--event", normalize_phase(phase)]
    if root is not None:
        parts += ["--root", str(root)]
    return " ".join(shlex.quote(part) for part in parts)


def is_retro_hook_command(value: Any, *, known_commands: frozenset[str] = frozenset()) -> bool:
    if not isinstance(value, dict):
        return False
    texts: list[str] = []
    for key in ("command", "bash", "powershell", "exec", "url"):
        candidate = value.get(key)
        if isinstance(candidate, str):
            texts.append(candidate)
    arguments = value.get("args")
    if isinstance(arguments, list):
        texts.extend(item for item in arguments if isinstance(item, str))
    joined = " ".join(texts)
    if joined.strip() in known_commands:
        return True
    return any(marker in text for text in texts for marker in HOOK_MARKERS)


# ---- configuration merging ---------------------------------------------------


def _read_document(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.exists():
        return {}, False
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as error:
        raise HookConfigError(f"cannot read {path}: {error}") from error
    if not raw.strip():
        return {}, True
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookConfigError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise HookConfigError(f"{path} must contain a JSON object")
    return document, True


def _hook_entry(target: HookTarget, phase: str, command: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "command", "command": command}
    entry[target.timeout_key] = target.timeouts[phase]
    return entry


def _strip_retro_entries(
    entries: Any,
    *,
    grouped: bool,
    known_commands: frozenset[str] = frozenset(),
) -> list[Any]:
    if not isinstance(entries, list):
        return []
    kept: list[Any] = []
    for item in entries:
        if grouped and isinstance(item, dict) and isinstance(item.get("hooks"), list):
            inner = [
                hook
                for hook in item["hooks"]
                if not is_retro_hook_command(hook, known_commands=known_commands)
            ]
            if not inner:
                continue
            if len(inner) != len(item["hooks"]):
                item = {**item, "hooks": inner}
            kept.append(item)
            continue
        if is_retro_hook_command(item, known_commands=known_commands):
            continue
        kept.append(item)
    return kept


def _plan_document(
    target: HookTarget,
    document: dict[str, Any],
    *,
    command_for: dict[str, str] | None,
) -> dict[str, Any]:
    planned = json.loads(json.dumps(document)) if document else {}
    known_commands = frozenset(command_for.values()) if command_for else frozenset()
    hooks = planned.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    for phase in HOOK_PHASES:
        event = target.event_names[phase]
        entries = _strip_retro_entries(
            hooks.get(event), grouped=target.grouped, known_commands=known_commands
        )
        if command_for is not None:
            entry = _hook_entry(target, phase, command_for[phase])
            entries.append({"hooks": [entry]} if target.grouped else entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if hooks:
        planned["hooks"] = hooks
        if command_for is not None:
            for key, value in target.document_defaults.items():
                planned.setdefault(key, value)
    else:
        planned.pop("hooks", None)
    return planned


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.{stamp}{_BACKUP_SUFFIX}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.{stamp}-{counter}{_BACKUP_SUFFIX}")
        counter += 1
    return candidate


def _write_document(path: Path, document: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(document, indent=2, sort_keys=False) + "\n")


def _apply(
    target: HookTarget,
    *,
    command_for: dict[str, str] | None,
    dry_run: bool,
) -> HookChange:
    action = "install" if command_for is not None else "uninstall"
    command = command_for["start"] if command_for else None
    if not target.supported:
        return HookChange(
            host=target.host,
            label=target.label,
            path=str(target.path),
            action="unsupported",
            changed=False,
            dry_run=dry_run,
            message=target.unsupported_reason,
        )
    try:
        document, existed = _read_document(target.path)
    except HookConfigError as error:
        return HookChange(
            host=target.host,
            label=target.label,
            path=str(target.path),
            action="error",
            changed=False,
            dry_run=dry_run,
            message=str(error),
        )
    planned = _plan_document(target, document, command_for=command_for)
    remove_file = (
        command_for is None
        and existed
        and (not planned or (target.retro_owned_file and not planned.get("hooks")))
    )
    unchanged = planned == document and not remove_file
    if unchanged and (existed or command_for is None):
        return HookChange(
            host=target.host,
            label=target.label,
            path=str(target.path),
            action="unchanged",
            changed=False,
            dry_run=dry_run,
            command=command,
            message=target.note,
        )
    if dry_run:
        return HookChange(
            host=target.host,
            label=target.label,
            path=str(target.path),
            action=action,
            changed=True,
            dry_run=True,
            command=command,
            message=target.note,
        )
    backup: Path | None = None
    if existed:
        backup = _backup_path(target.path)
        shutil.copy2(target.path, backup)
    if remove_file:
        target.path.unlink(missing_ok=True)
    else:
        _write_document(target.path, planned)
    return HookChange(
        host=target.host,
        label=target.label,
        path=str(target.path),
        action=action,
        changed=True,
        dry_run=False,
        backup=str(backup) if backup is not None else None,
        command=command,
        message=target.note,
    )


def install_hooks(
    hosts: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
    launcher: Sequence[str] | None = None,
    root: Path | None = None,
) -> list[HookChange]:
    resolved = list(launcher) if launcher is not None else hook_launcher()
    changes: list[HookChange] = []
    for target in hook_targets(hosts):
        command_for = {
            phase: hook_command(target.host, phase, launcher=resolved, root=root)
            for phase in HOOK_PHASES
        }
        changes.append(_apply(target, command_for=command_for, dry_run=dry_run))
    return changes


def uninstall_hooks(
    hosts: Sequence[str] | None = None,
    *,
    dry_run: bool = False,
) -> list[HookChange]:
    return [
        _apply(target, command_for=None, dry_run=dry_run)
        for target in hook_targets(hosts)
    ]


def hook_status(hosts: Sequence[str] | None = None) -> list[HookStatus]:
    statuses: list[HookStatus] = []
    for target in hook_targets(hosts):
        if not target.supported:
            statuses.append(
                HookStatus(
                    host=target.host,
                    label=target.label,
                    path=str(target.path),
                    supported=False,
                    exists=False,
                    start_installed=False,
                    end_installed=False,
                    docs_url=target.docs_url,
                    message=target.unsupported_reason,
                )
            )
            continue
        try:
            document, existed = _read_document(target.path)
        except HookConfigError as error:
            statuses.append(
                HookStatus(
                    host=target.host,
                    label=target.label,
                    path=str(target.path),
                    supported=True,
                    exists=target.path.exists(),
                    start_installed=False,
                    end_installed=False,
                    docs_url=target.docs_url,
                    message=str(error),
                )
            )
            continue
        hooks = document.get("hooks")
        hooks = hooks if isinstance(hooks, dict) else {}
        installed: dict[str, str | None] = {}
        for phase in HOOK_PHASES:
            installed[phase] = _installed_command(
                hooks.get(target.event_names[phase]), grouped=target.grouped
            )
        statuses.append(
            HookStatus(
                host=target.host,
                label=target.label,
                path=str(target.path),
                supported=True,
                exists=existed,
                start_installed=installed["start"] is not None,
                end_installed=installed["end"] is not None,
                command=installed["start"] or installed["end"],
                docs_url=target.docs_url,
                message=target.note,
            )
        )
    return statuses


def _installed_command(entries: Any, *, grouped: bool) -> str | None:
    if not isinstance(entries, list):
        return None
    for item in entries:
        candidates: list[Any] = []
        if grouped and isinstance(item, dict) and isinstance(item.get("hooks"), list):
            candidates.extend(item["hooks"])
        else:
            candidates.append(item)
        for candidate in candidates:
            if is_retro_hook_command(candidate):
                for key in ("command", "bash", "exec"):
                    value = candidate.get(key)
                    if isinstance(value, str):
                        return value
                return ""
    return None


# ---- hook entry point --------------------------------------------------------


def hook_log_path() -> Path:
    return user_log_dir() / HOOK_LOG_NAME


def _log(message: str) -> None:
    try:
        path = hook_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:  # logging must never break a session
        pass


def _read_payload(stream: IO[str] | None) -> dict[str, Any]:
    source = stream if stream is not None else sys.stdin
    if source is None:
        return {}
    try:
        raw = source.read()
    except Exception:  # a closed or binary stdin is not fatal
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def payload_session_id(payload: dict[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "thread_id", "threadId"):
        value = payload.get(key)
        if isinstance(value, str) and SESSION_ID_PATTERN.fullmatch(value.strip()):
            return value.strip()
    return None


def payload_cwd(payload: dict[str, Any]) -> Path | None:
    for key in ("cwd", "workspace", "working_directory", "workingDirectory"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser()
    try:
        return Path.cwd()
    except OSError:
        return None


def inside_git_repo(path: Path) -> bool:
    try:
        probe = path.resolve()
    except OSError:
        return False
    if not probe.is_dir():
        probe = probe.parent
    while True:
        if (probe / ".git").exists():
            return True
        if probe.parent == probe:
            return False
        probe = probe.parent


def run_hook_entry(
    host: str,
    event: str,
    *,
    stream: IO[str] | None = None,
    payload: dict[str, Any] | None = None,
    root: Path | None = None,
) -> int:
    """Capture repository state from a host hook payload. Never fails."""
    try:
        _run_hook_entry(host, event, stream=stream, payload=payload, root=root)
    except Exception as error:  # hooks must never break a session
        _log(f"{host}/{event} failed: {type(error).__name__}: {error}")
    return 0


def _run_hook_entry(
    host: str,
    event: str,
    *,
    stream: IO[str] | None,
    payload: dict[str, Any] | None,
    root: Path | None,
) -> None:
    phase = normalize_phase(event)
    canonical = _canonical_archive_host(host)
    data = payload if payload is not None else _read_payload(stream)
    session_id = payload_session_id(data)
    if session_id is None:
        _log(f"{host}/{phase}: payload has no usable session id")
        return
    cwd = payload_cwd(data)
    if cwd is None or not cwd.is_dir():
        return
    if not inside_git_repo(cwd):
        return

    from .benchmarks.task_scorer.git_state import (
        CaptureExistsError,
        GitError,
        capture_repository_state,
    )

    layout = default_layout(root)
    try:
        capture_repository_state(
            layout=layout,
            host=canonical,
            session_id=session_id,
            cwd=cwd,
            phase=phase,
        )
    except CaptureExistsError:
        return
    except GitError as error:
        _log(f"{host}/{phase} {session_id}: {error}")


def _canonical_archive_host(host: str) -> str:
    from .extensions import canonical_source_host

    return canonical_source_host(_normalize_host(host))


# ---- doctor coverage ---------------------------------------------------------


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_git_evidence(record: dict[str, Any]) -> bool:
    git = record.get("git")
    if not isinstance(git, dict):
        return False
    return any(bool(value) for value in git.values())


def _raw_session_dir(layout: Layout, host: Any, session_id: Any) -> Path | None:
    if not isinstance(host, str) or not isinstance(session_id, str):
        return None
    try:
        return layout.raw_dir(host, session_id)
    except ValueError:
        return None


def capture_coverage(
    layout: Layout | None = None,
    *,
    days: int = 30,
    limit: int = 5000,
) -> CaptureCoverage:
    """Share of recent Git-backed sessions that carry a start-state sidecar."""
    lay = layout if layout is not None else default_layout()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    catalog = lay.root / "catalog" / "sessions.jsonl"
    if catalog.is_file():
        return _catalog_coverage(lay, catalog, cutoff=cutoff, days=days, limit=limit)
    return _raw_coverage(lay, cutoff=cutoff, days=days, limit=limit)


def _catalog_coverage(
    layout: Layout,
    catalog: Path,
    *,
    cutoff: datetime,
    days: int,
    limit: int,
) -> CaptureCoverage:
    sessions = 0
    with_start = 0
    with_end = 0
    try:
        handle = catalog.open("r", encoding="utf-8")
    except OSError:
        return _raw_coverage(layout, cutoff=cutoff, days=days, limit=limit)
    with handle:
        for line in handle:
            if sessions >= limit:
                break
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("excluded"):
                continue
            started = _parse_timestamp(record.get("started_at"))
            if started is None or started < cutoff:
                continue
            if not _has_git_evidence(record):
                continue
            raw_dir = _raw_session_dir(layout, record.get("host"), record.get("session_id"))
            if raw_dir is None:
                continue
            sessions += 1
            if (raw_dir / "repo_start.json").is_file():
                with_start += 1
            if (raw_dir / "repo_end.json").is_file():
                with_end += 1
    return CaptureCoverage(
        days=days,
        source="catalog",
        sessions=sessions,
        with_start=with_start,
        with_end=with_end,
        git_filtered=True,
    )


def _raw_coverage(
    layout: Layout,
    *,
    cutoff: datetime,
    days: int,
    limit: int,
) -> CaptureCoverage:
    raw_root = layout.root / "raw"
    sessions = 0
    with_start = 0
    with_end = 0
    threshold = cutoff.timestamp()
    if raw_root.is_dir():
        for host_dir in sorted(raw_root.iterdir()):
            if not host_dir.is_dir() or host_dir.name.startswith("."):
                continue
            for session_dir in sorted(host_dir.iterdir()):
                if sessions >= limit:
                    break
                if not session_dir.is_dir() or session_dir.name.startswith("."):
                    continue
                try:
                    modified = session_dir.stat().st_mtime
                except OSError:
                    continue
                if modified < threshold:
                    continue
                sessions += 1
                if (session_dir / "repo_start.json").is_file():
                    with_start += 1
                if (session_dir / "repo_end.json").is_file():
                    with_end += 1
    return CaptureCoverage(
        days=days,
        source="raw",
        sessions=sessions,
        with_start=with_start,
        with_end=with_end,
        git_filtered=False,
    )


# ---- module entry point ------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retro.capture_hooks",
        description="Capture repository state from an agent host session hook.",
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--root", default=None)
    try:
        arguments = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit:
        return 0
    root = Path(arguments.root).expanduser() if arguments.root else None
    return run_hook_entry(arguments.host, arguments.event, root=root)


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
