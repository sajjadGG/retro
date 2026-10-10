"""Per-host archived metadata extraction and client classification."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils import iter_jsonl


@dataclass
class HostMetadata:
    agent: str
    client: str = "unknown"
    interactive: bool | None = None
    is_subagent: bool = False
    parent_key: str | None = None
    title: str | None = None
    cwd: str | None = None
    remote_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    project_slug: str | None = None
    models: list[str] = field(default_factory=list)
    started_at: str | None = None
    ended_at: str | None = None


def extract_host_metadata(host: str, session_id: str, raw_dir: Path) -> HostMetadata:
    if host == "codex":
        return _codex_metadata(session_id, raw_dir)
    if host == "claude-code":
        return _claude_metadata(session_id, raw_dir)
    if host == "vscode-copilot":
        return _copilot_metadata(session_id, raw_dir)
    return _unknown_metadata(host, raw_dir)


def parse_simple_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: dict[str, Any] = {}
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = raw_line.strip()
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            continue
        value = raw_value.strip()
        if value in {"|", "|-", "|+", ">", ">-", ">+"}:
            block: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip():
                    candidate_indent = len(candidate) - len(candidate.lstrip())
                    if candidate_indent <= indent:
                        break
                    block.append(candidate[min(candidate_indent, indent + 2) :])
                else:
                    block.append("")
                index += 1
            separator = "\n" if value.startswith("|") else " "
            parsed = separator.join(block)
            if value.endswith("-"):
                parsed = parsed.rstrip("\n")
            elif value.endswith("+"):
                parsed += "\n"
            out[key] = parsed
            continue
        out[key] = _yaml_scalar(value)
    return out


def _codex_metadata(session_id: str, raw_dir: Path) -> HostMetadata:
    thread = _read_json(raw_dir / "thread.json")
    payload: dict[str, Any] = {}
    rollout = raw_dir / "rollout.jsonl"
    if rollout.is_file():
        for _, entry in iter_jsonl(rollout):
            if entry.get("type") == "session_meta":
                candidate = entry.get("payload")
                payload = candidate if isinstance(candidate, dict) else {}
                break

    originator = _text(payload.get("originator")) or _text(thread.get("originator"))
    source = payload.get("source")
    is_subagent, parent_id = _codex_subagent(source)
    spawn_edges = thread.get("spawn_edges")
    if parent_id is None and isinstance(spawn_edges, dict):
        parents = spawn_edges.get("parents")
        if isinstance(parents, list) and parents:
            parent_id = _text(parents[0])
            is_subagent = parent_id is not None

    source_name = source.casefold() if isinstance(source, str) else ""
    originator_name = originator.casefold() if originator else ""
    if originator_name == "codex desktop":
        client, interactive = "codex-desktop", True
    elif source_name == "vscode":
        client, interactive = "codex-vscode", True
    elif source_name == "exec" or originator_name == "codex_exec":
        client, interactive = "codex-exec", False
    elif source_name in {"cli", "terminal"} or originator_name in {"codex", "codex_cli"}:
        client, interactive = "codex-cli", True
    elif is_subagent:
        client, interactive = "codex-exec", False
    else:
        client, interactive = "unknown", None
    if is_subagent:
        interactive = False

    git = payload.get("git")
    git_data = git if isinstance(git, dict) else {}
    return HostMetadata(
        agent="codex",
        client=client,
        interactive=interactive,
        is_subagent=is_subagent,
        parent_key=f"codex/{parent_id}" if parent_id else None,
        title=_title(thread.get("title") or payload.get("title")),
        cwd=_text(payload.get("cwd")) or _text(thread.get("cwd")),
        remote_url=_text(
            git_data.get("repository_url")
            or git_data.get("remote_url")
            or git_data.get("remote")
        ),
        branch=_text(git_data.get("branch") or thread.get("git_branch")),
        commit=_text(
            git_data.get("commit_hash")
            or git_data.get("commit")
            or git_data.get("sha")
        ),
        models=_strings(payload.get("model")),
        started_at=_text(payload.get("timestamp") or payload.get("created_at")),
        ended_at=_text(thread.get("updated_at")),
    )


def _codex_subagent(source: Any) -> tuple[bool, str | None]:
    if not isinstance(source, dict):
        return False, None
    subagent = source.get("subagent")
    if not isinstance(subagent, dict):
        return False, None
    thread_spawn = subagent.get("thread_spawn")
    if isinstance(thread_spawn, dict):
        return True, _text(thread_spawn.get("parent_thread_id"))
    return True, None


def _claude_metadata(session_id: str, raw_dir: Path) -> HostMetadata:
    imported = _read_json(raw_dir / "import_meta.json")
    found: dict[str, Any] = {}
    models: list[str] = []
    transcript = raw_dir / "transcript.jsonl"
    if transcript.is_file():
        for _, entry in iter_jsonl(transcript):
            for key in (
                "entrypoint",
                "cwd",
                "gitBranch",
                "isSidechain",
                "parentSessionId",
                "parent_session_id",
                "session_id",
                "sessionId",
            ):
                if key in entry and key not in found:
                    found[key] = entry[key]
            if entry.get("aiTitle"):
                found["aiTitle"] = entry["aiTitle"]
            message = entry.get("message")
            if isinstance(message, dict):
                models.extend(_strings(message.get("model")))
            usage = entry.get("modelUsage")
            if isinstance(usage, dict):
                models.extend(str(name) for name in usage if name)

    entrypoint = (_text(found.get("entrypoint")) or "").casefold()
    if entrypoint == "cli":
        client, interactive = "claude-cli", True
    elif entrypoint == "claude-desktop":
        client, interactive = "claude-desktop", True
    elif entrypoint in {"sdk", "sdk-cli"}:
        client, interactive = "claude-sdk", False
    else:
        client, interactive = "unknown", None
    is_subagent = bool(found.get("isSidechain"))
    if is_subagent:
        interactive = False
    parent_id = _text(found.get("parentSessionId") or found.get("parent_session_id"))
    if parent_id == session_id:
        parent_id = None
    return HostMetadata(
        agent="claude-code",
        client=client,
        interactive=interactive,
        is_subagent=is_subagent,
        parent_key=f"claude-code/{parent_id}" if parent_id else None,
        title=_title(found.get("aiTitle") or imported.get("title")),
        cwd=_text(found.get("cwd") or imported.get("cwd")),
        branch=_text(found.get("gitBranch")),
        project_slug=_text(imported.get("project_slug")),
        models=list(dict.fromkeys(models)),
    )


def _copilot_metadata(session_id: str, raw_dir: Path) -> HostMetadata:
    imported = _read_json(raw_dir / "import_meta.json")
    workspace = parse_simple_yaml(raw_dir / "sidecars" / "workspace.yaml")
    source_kind = _text(imported.get("source_kind")) or ""
    client_name = (_text(workspace.get("client_name")) or "").casefold()
    if source_kind == "vscode-chat":
        client, interactive = "copilot-vscode-chat", True
    elif client_name == "vscode-agent-host":
        client, interactive = "copilot-vscode-agent", True
    elif client_name == "vscode":
        client, interactive = "copilot-vscode-agent", True
    elif client_name in {"github/cli", "copilot-cli"} or source_kind == "copilot-cli":
        client = "copilot-cli"
        interactive = not _copilot_non_interactive(imported, workspace, raw_dir)
    else:
        client, interactive = "unknown", None
    parent_id = _text(
        workspace.get("parent_session_id")
        or workspace.get("parentSessionId")
        or imported.get("parent_session_id")
        or imported.get("parentSessionId")
    )
    is_subagent = bool(parent_id or workspace.get("is_subagent") or imported.get("is_subagent"))
    if is_subagent:
        interactive = False
    return HostMetadata(
        agent="copilot",
        client=client,
        interactive=interactive,
        is_subagent=is_subagent,
        parent_key=f"vscode-copilot/{parent_id}" if parent_id else None,
        title=_title(
            workspace.get("name")
            or imported.get("title")
            or imported.get("workspace_name")
        ),
        cwd=_text(imported.get("cwd") or workspace.get("cwd")),
        remote_url=_text(
            imported.get("repository")
            or workspace.get("repository")
            or workspace.get("remote")
        ),
        branch=_text(imported.get("branch") or workspace.get("branch")),
        commit=_text(imported.get("commit") or workspace.get("commit")),
        models=_strings(imported.get("models"), workspace.get("model")),
        started_at=_text(imported.get("created_at") or workspace.get("created_at")),
        ended_at=_text(imported.get("updated_at") or workspace.get("updated_at")),
    )


def _copilot_non_interactive(
    imported: dict[str, Any],
    workspace: dict[str, Any],
    raw_dir: Path,
) -> bool:
    values = " ".join(_flatten_strings({"import": imported, "workspace": workspace}))
    if re.search(
        r"(?:^|\s)(?:gh\s+)?copilot(?:\s+\w+)*\s+[^\\n]*"
        r"(?:-p(?:\s|=)|--prompt(?:\s|=))",
        values,
        re.IGNORECASE,
    ):
        return True
    cwd = _text(imported.get("cwd") or workspace.get("cwd")) or ""
    if bool(
        re.match(r"^/(?:private/)?var/folders/.+/T/tmp[^/]*(?:/|$)", cwd)
        or re.match(r"^/(?:private/)?tmp(?:/|$)", cwd)
    ):
        return True
    events = raw_dir / "events.jsonl"
    if events.is_file():
        for _, event in iter_jsonl(events):
            if event.get("type") != "user.message":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                return False
            return (
                not data.get("messageId")
                and bool(data.get("parentAgentTaskId"))
                and data.get("delivery") == "idle"
            )
    return False


def _unknown_metadata(host: str, raw_dir: Path) -> HostMetadata:
    imported = _read_json(raw_dir / "import_meta.json")
    return HostMetadata(
        agent=host,
        title=_title(imported.get("title")),
        cwd=_text(imported.get("cwd")),
        remote_url=_text(imported.get("repository") or imported.get("remote")),
        branch=_text(imported.get("branch")),
        commit=_text(imported.get("commit")),
        models=_strings(imported.get("models"), imported.get("model")),
        started_at=_text(imported.get("created_at")),
        ended_at=_text(imported.get("updated_at")),
    )


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _yaml_scalar(value: str) -> Any:
    if not value:
        return ""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    lowered = value.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value.strip("\"'")


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_flatten_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_strings(item))
        return out
    return []


def _strings(*values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip():
            out.append(value.strip())
        elif isinstance(value, (list, tuple)):
            out.extend(str(item).strip() for item in value if str(item).strip())
    return list(dict.fromkeys(out))


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _title(value: Any) -> str | None:
    return _text(value)
