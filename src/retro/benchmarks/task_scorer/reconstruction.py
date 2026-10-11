"""Opt-in anchored reconstruction for historical rollout sources."""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from ...schema import NormalizedEvent
from ...utils import event_command_text, event_tool_input
from . import git_state

_PATCH_BEGIN = "*** Begin Patch"
_PATCH_END = "*** End Patch"
_PATCH_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")
_SHELL_FILE_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:apply_patch|patch\b|sed\s+-i|tee\b|"
    r"git\s+(?:apply|checkout|switch|reset|stash|merge|rebase|cherry-pick)\b|"
    r"(?:cp|mv|rm|truncate)\b|[^;&|]*(?:>{1,2})\s*\S)"
)
_PATH_KEYS = ("file_path", "filePath", "path", "uri", "target_file", "targetFile")
_OLD_KEYS = ("old_string", "oldString", "old_text", "oldText", "search", "search_string")
_NEW_KEYS = (
    "new_string",
    "newString",
    "new_text",
    "newText",
    "replacement",
    "replacement_string",
)
_CONTENT_KEYS = ("content", "contents", "file_content", "fileContent")
_EDIT_LIST_KEYS = ("edits", "replacements", "changes")


@dataclass(frozen=True)
class ReconstructionReport:
    anchor_sha: str
    anchor_method: str
    files_touched: tuple[str, ...]
    files_verified_pre: tuple[str, ...]
    files_verified_post: tuple[str, ...]
    unverifiable_edits: tuple[dict[str, Any], ...]
    pre_mismatches: tuple[dict[str, Any], ...]
    post_mismatches: tuple[str, ...]
    changed_paths: tuple[str, ...]
    per_file: dict[str, dict[str, Any]]
    touched_paths_clean: bool
    confidence: float

    @property
    def accepted(self) -> bool:
        touched = set(self.files_touched)
        return bool(touched) and (
            touched == set(self.files_verified_pre)
            == set(self.files_verified_post)
            and not self.unverifiable_edits
            and not self.pre_mismatches
            and not self.post_mismatches
            and self.confidence >= 0.8
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_sha": self.anchor_sha,
            "anchor_method": self.anchor_method,
            "files_touched": list(self.files_touched),
            "files_verified_pre": list(self.files_verified_pre),
            "files_verified_post": list(self.files_verified_post),
            "unverifiable_edits": [dict(item) for item in self.unverifiable_edits],
            "pre_mismatches": [dict(item) for item in self.pre_mismatches],
            "post_mismatches": list(self.post_mismatches),
            "changed_paths": list(self.changed_paths),
            "per_file": {path: dict(value) for path, value in self.per_file.items()},
            "touched_paths_clean": self.touched_paths_clean,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class _Edit:
    event_id: str
    kind: str
    path: str | None = None
    old: str | None = None
    new: str | None = None
    replace_all: bool = False
    patch: str | None = None
    reason: str = ""


@dataclass
class _FileEvidence:
    event_ids: list[str] = field(default_factory=list)
    pre_verified: bool = False
    post_verified: bool = False


@dataclass
class _PatchSection:
    action: str
    path: str
    lines: list[str] = field(default_factory=list)
    move_to: str | None = None
    end_of_file: bool = False


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _eligible_commit(root: Path, sha: str, session_started_at: str) -> tuple[bool, str | None]:
    if not git_state.commit_exists(root, sha):
        return False, None
    committed_at = git_state.run_git(root, "show", "-s", "--format=%cI", sha, check=False)
    commit_time = _parse_timestamp(committed_at)
    session_time = _parse_timestamp(session_started_at)
    return bool(commit_time and session_time and commit_time <= session_time), committed_at or None


def resolve_anchor(
    *,
    root: Path,
    events: list[NormalizedEvent],
    session_started_at: str,
    recorded_branch: str | None = None,
) -> git_state.BaseResolution:
    """Choose an immutable pre-session anchor without consulting reflogs."""
    attempts: list[dict[str, Any]] = []
    commits = git_state.rollout_created_commits(events, root)
    if commits:
        first = commits[0]
        parents = git_state.run_git(
            root, "rev-list", "--parents", "-n", "1", first.sha, check=False
        ).split()
        if len(parents) >= 2:
            parent = parents[1]
            eligible, committed_at = _eligible_commit(root, parent, session_started_at)
            attempts.append(
                {
                    "method": "first_commit_parent",
                    "sha": parent,
                    "eligible": eligible,
                }
            )
            if eligible:
                return git_state.BaseResolution(
                    base_sha=parent,
                    base_tree=git_state.commit_tree(root, parent),
                    resolution="anchored_reconstruction",
                    state_confidence="approximate",
                    evidence={
                        "anchor_sha": parent,
                        "anchor_method": "first_commit_parent",
                        "anchor_event_id": first.event_id,
                        "anchor_commit_timestamp": committed_at,
                    },
                )

    for sha, event_id in git_state.rollout_head_observations(events):
        eligible, committed_at = _eligible_commit(root, sha, session_started_at)
        attempts.append({"method": "rollout_head", "sha": sha, "eligible": eligible})
        if eligible:
            return git_state.BaseResolution(
                base_sha=sha,
                base_tree=git_state.commit_tree(root, sha),
                resolution="anchored_reconstruction",
                state_confidence="approximate",
                evidence={
                    "anchor_sha": sha,
                    "anchor_method": "rollout_head",
                    "anchor_event_id": event_id,
                    "anchor_commit_timestamp": committed_at,
                },
            )

    branch = recorded_branch.strip() if recorded_branch else ""
    if branch and branch != "HEAD":
        branch_tip = git_state.resolve_rev(root, branch)
        sha = (
            git_state.run_git(
                root,
                "rev-list",
                "-1",
                f"--before={session_started_at}",
                branch_tip,
                check=False,
            )
            if branch_tip
            else ""
        )
        if sha and git_state.commit_exists(root, sha):
            eligible, committed_at = _eligible_commit(root, sha, session_started_at)
            attempts.append(
                {
                    "method": "branch_before_session",
                    "sha": sha,
                    "branch": branch,
                    "eligible": eligible,
                }
            )
            if eligible:
                return git_state.BaseResolution(
                    base_sha=sha,
                    base_tree=git_state.commit_tree(root, sha),
                    resolution="anchored_reconstruction",
                    state_confidence="approximate",
                    evidence={
                        "anchor_sha": sha,
                        "anchor_method": "branch_before_session",
                        "anchor_branch": branch,
                        "anchor_commit_timestamp": committed_at,
                    },
                )

    return git_state.BaseResolution(
        base_sha=None,
        base_tree=None,
        resolution="unresolved",
        rejection_code="NO_EXACT_BASE_SHA",
        detail="no reconstruction anchor committed at or before the session start",
        evidence={"reconstruction_anchor_attempts": attempts},
    )


def _tool_name(event: NormalizedEvent) -> str:
    payload = event.payload or {}
    for key in ("name", "tool_name", "toolName", "toolId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("part", "request", "invocation", "vscode_chat_invocation"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        for nested_key in ("name", "tool_name", "toolName", "toolId"):
            value = nested.get(nested_key)
            if isinstance(value, str) and value:
                return value
    return ""


def _call_id(event: NormalizedEvent) -> str | None:
    payload = event.payload or {}
    for key in ("call_id", "tool_call_id", "toolCallId", "tool_id"):
        value = payload.get(key)
        if value:
            return str(value)
    part = payload.get("part")
    if isinstance(part, dict):
        for key in ("toolCallId", "id"):
            value = part.get(key)
            if value:
                return str(value)
    return None


def _failed_edit_events(events: list[NormalizedEvent]) -> tuple[set[str], set[str]]:
    failed_calls: set[str] = set()
    failed_parents: set[str] = set()
    for event in events:
        if event.event_type not in ("tool_result", "command"):
            continue
        payload = event.payload or {}
        failed = payload.get("success") is False or payload.get("is_error") is True
        for key in ("exit_code", "return_code", "returncode"):
            value = payload.get(key)
            if isinstance(value, int) and value != 0:
                failed = True
        if not failed:
            continue
        call_id = _call_id(event)
        if call_id:
            failed_calls.add(call_id)
        if event.parent_event_id:
            failed_parents.add(event.parent_event_id)
    return failed_calls, failed_parents


def _input_value(event: NormalizedEvent) -> Any:
    value = event_tool_input(event)
    if value is None:
        part = (event.payload or {}).get("part")
        value = part if isinstance(part, dict) else event.payload
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _first_string(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def _working_subdir(root: Path, captured_cwd: str) -> PurePosixPath:
    cwd = Path(captured_cwd).expanduser()
    try:
        return PurePosixPath(cwd.resolve().relative_to(root.resolve()).as_posix())
    except (OSError, ValueError):
        return PurePosixPath(".")


def _relative_path(
    value: str,
    *,
    root: Path,
    captured_cwd: str,
) -> str | None:
    text = value.strip()
    if text.startswith("file:"):
        parsed = urlsplit(text)
        if parsed.scheme != "file":
            return None
        text = unquote(parsed.path)
    if "\x00" in text:
        return None
    candidate = Path(text).expanduser()
    subdir = _working_subdir(root, captured_cwd)
    if candidate.is_absolute():
        relative: Path | None = None
        for base in (root, Path(captured_cwd).expanduser()):
            try:
                relative = candidate.relative_to(base)
                if base != root:
                    relative = Path(subdir.as_posix()) / relative
                break
            except ValueError:
                continue
        if relative is None:
            return None
        pure = PurePosixPath(relative.as_posix())
    else:
        pure = subdir / PurePosixPath(text.replace("\\", "/"))
    if (
        not pure.parts
        or pure.is_absolute()
        or any(part in ("", ".", "..", ".git") for part in pure.parts)
    ):
        return None
    return pure.as_posix()


def _extract_edits(
    event: NormalizedEvent,
    *,
    root: Path,
    captured_cwd: str,
) -> list[_Edit]:
    value = _input_value(event)
    name = _tool_name(event).lower()
    if isinstance(value, str):
        if _PATCH_BEGIN in value:
            return [_Edit(event_id=event.event_id, kind="patch", patch=value)]
        return [
            _Edit(
                event_id=event.event_id,
                kind="unverifiable",
                reason="file-edit input is not a structured edit or patch",
            )
        ]
    if not isinstance(value, dict):
        return [
            _Edit(
                event_id=event.event_id,
                kind="unverifiable",
                reason="file-edit event has no structured input",
            )
        ]
    patch = _first_string(value, ("patch", "diff"))
    if patch and _PATCH_BEGIN in patch:
        return [_Edit(event_id=event.event_id, kind="patch", patch=patch)]

    parent_path = _first_string(value, _PATH_KEYS)
    for key in _EDIT_LIST_KEYS:
        items = value.get(key)
        if not isinstance(items, list):
            continue
        edits: list[_Edit] = []
        for item in items:
            if not isinstance(item, dict):
                edits.append(
                    _Edit(
                        event_id=event.event_id,
                        kind="unverifiable",
                        reason=f"{key} contains a non-object edit",
                    )
                )
                continue
            merged = dict(item)
            if parent_path and _first_string(merged, _PATH_KEYS) is None:
                merged["path"] = parent_path
            edits.extend(
                _extract_mapping_edit(
                    event.event_id,
                    name,
                    merged,
                    root=root,
                    captured_cwd=captured_cwd,
                )
            )
        return edits
    return _extract_mapping_edit(
        event.event_id,
        name,
        value,
        root=root,
        captured_cwd=captured_cwd,
    )


def _extract_mapping_edit(
    event_id: str,
    name: str,
    data: dict[str, Any],
    *,
    root: Path,
    captured_cwd: str,
) -> list[_Edit]:
    raw_path = _first_string(data, _PATH_KEYS)
    path = (
        _relative_path(raw_path, root=root, captured_cwd=captured_cwd)
        if raw_path is not None
        else None
    )
    old = _first_string(data, _OLD_KEYS)
    new = _first_string(data, _NEW_KEYS)
    if old is not None and new is not None:
        return [
            _Edit(
                event_id=event_id,
                kind="replace",
                path=path,
                old=old,
                new=new,
                replace_all=bool(data.get("replace_all") or data.get("replaceAll")),
                reason="edit path is outside the repository" if path is None else "",
            )
        ]
    content = _first_string(data, _CONTENT_KEYS)
    if content is not None and any(token in name for token in ("write", "create")):
        return [
            _Edit(
                event_id=event_id,
                kind="write",
                path=path,
                new=content,
                reason="write path is outside the repository" if path is None else "",
            )
        ]
    return [
        _Edit(
            event_id=event_id,
            kind="unverifiable",
            path=path,
            reason=(
                "file-edit path is outside the repository"
                if raw_path is not None and path is None
                else "file-edit event has no replayable old/new strings or patch"
            ),
        )
    ]


def _parse_patch(patch: str) -> list[_PatchSection] | None:
    lines = patch.splitlines()
    try:
        begin = lines.index(_PATCH_BEGIN)
        end = lines.index(_PATCH_END, begin + 1)
    except ValueError:
        return None
    sections: list[_PatchSection] = []
    current: _PatchSection | None = None
    for line in lines[begin + 1 : end]:
        match = _PATCH_FILE_RE.match(line)
        if match:
            current = _PatchSection(action=match.group(1).lower(), path=match.group(2).strip())
            sections.append(current)
            continue
        if line.startswith("*** Move to: "):
            if current is None:
                return None
            current.move_to = line.removeprefix("*** Move to: ").strip()
            continue
        if line == "*** End of File":
            if current is None:
                return None
            current.end_of_file = True
            continue
        if current is None:
            return None
        current.lines.append(line)
    return sections or None


def _safe_target(workspace: Path, relative: str) -> Path | None:
    target = workspace / relative
    current = workspace
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved = target.resolve(strict=False)
        base = workspace.resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return target


def _read_text(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _apply_replace(target: Path, edit: _Edit) -> tuple[bool, str]:
    current = _read_text(target)
    if current is None or edit.old is None or edit.new is None or not edit.old:
        return False, "target is missing, non-text, or has an empty old string"
    count = current.count(edit.old)
    if count == 0:
        return False, "old string is absent"
    if count > 1 and not edit.replace_all:
        return False, "old string is ambiguous"
    replacement_count = -1 if edit.replace_all else 1
    target.write_text(current.replace(edit.old, edit.new, replacement_count), encoding="utf-8")
    return True, ""


def _patch_hunks(lines: list[str]) -> list[list[str]] | None:
    hunks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("@@"):
            if current:
                hunks.append(current)
                current = []
            continue
        if not line or line[0] not in (" ", "+", "-"):
            return None
        current.append(line)
    if current:
        hunks.append(current)
    return hunks or None


def _apply_update_patch(target: Path, section: _PatchSection) -> tuple[bool, str]:
    current = _read_text(target)
    hunks = _patch_hunks(section.lines)
    if current is None or hunks is None:
        return False, "update target is missing, non-text, or patch hunks are malformed"
    newline = "\r\n" if "\r\n" in current and "\n" not in current.replace("\r\n", "") else "\n"
    trailing_newline = current.endswith(("\n", "\r"))
    content = current.splitlines()
    cursor = 0
    for hunk in hunks:
        old_lines = [line[1:] for line in hunk if line[0] in (" ", "-")]
        new_lines = [line[1:] for line in hunk if line[0] in (" ", "+")]
        if not old_lines:
            return False, "patch hunk has no old/context lines"
        matches = [
            index
            for index in range(cursor, len(content) - len(old_lines) + 1)
            if content[index : index + len(old_lines)] == old_lines
        ]
        if not matches:
            return False, "patch context does not apply"
        index = matches[0]
        content[index : index + len(old_lines)] = new_lines
        cursor = index + len(new_lines)
    rendered = newline.join(content)
    if trailing_newline and not section.end_of_file:
        rendered += newline
    target.write_text(rendered, encoding="utf-8")
    return True, ""


def _record_file(records: dict[str, _FileEvidence], path: str, event_id: str) -> bool:
    record = records.setdefault(path, _FileEvidence())
    first = not record.event_ids
    if event_id not in record.event_ids:
        record.event_ids.append(event_id)
    return first


def _unverifiable(event_id: str, path: str | None, reason: str) -> dict[str, Any]:
    result: dict[str, Any] = {"event_id": event_id, "reason": reason}
    if path is not None:
        result["path"] = path
    return result


def _apply_patch_edit(
    workspace: Path,
    edit: _Edit,
    records: dict[str, _FileEvidence],
    *,
    root: Path,
    captured_cwd: str,
    unverifiable: list[dict[str, Any]],
    pre_mismatches: list[dict[str, Any]],
) -> None:
    sections = _parse_patch(edit.patch or "")
    if sections is None:
        unverifiable.append(
            _unverifiable(edit.event_id, None, "apply_patch payload is malformed or unsupported")
        )
        return
    for section in sections:
        path = _relative_path(section.path, root=root, captured_cwd=captured_cwd)
        move_to = (
            _relative_path(section.move_to, root=root, captured_cwd=captured_cwd)
            if section.move_to
            else None
        )
        if path is None or (section.move_to and move_to is None):
            unverifiable.append(
                _unverifiable(edit.event_id, path, "patch path is outside the repository")
            )
            continue
        first = _record_file(records, path, edit.event_id)
        target = _safe_target(workspace, path)
        if target is None:
            unverifiable.append(
                _unverifiable(edit.event_id, path, "patch path traverses a symlink")
            )
            continue
        if section.action == "add":
            if target.exists() or target.is_symlink():
                pre_mismatches.append(
                    _unverifiable(edit.event_id, path, "add target already exists at the anchor")
                )
                continue
            if any(not line.startswith("+") for line in section.lines):
                unverifiable.append(
                    _unverifiable(edit.event_id, path, "add-file patch is malformed")
                )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            content = "\n".join(line[1:] for line in section.lines)
            if section.lines and not section.end_of_file:
                content += "\n"
            target.write_text(content, encoding="utf-8")
            if first:
                records[path].pre_verified = True
        elif section.action == "update":
            applied, reason = _apply_update_patch(target, section)
            if not applied:
                pre_mismatches.append(_unverifiable(edit.event_id, path, reason))
                continue
            if first:
                records[path].pre_verified = True
        else:
            if section.lines:
                current = _read_text(target)
                expected = "\n".join(
                    line[1:] for line in section.lines if line.startswith("-")
                )
                if current is None or current.rstrip("\n") != expected:
                    pre_mismatches.append(
                        _unverifiable(
                            edit.event_id,
                            path,
                            "delete-file content does not match the anchor",
                        )
                    )
                    continue
                records[path].pre_verified = True
            else:
                unverifiable.append(
                    _unverifiable(
                        edit.event_id,
                        path,
                        "delete-file patch carries no pre-edit content",
                    )
                )
            if target.is_file() or target.is_symlink():
                target.unlink()
        if move_to is not None:
            _record_file(records, move_to, edit.event_id)
            destination = _safe_target(workspace, move_to)
            if destination is None or destination.exists() or destination.is_symlink():
                pre_mismatches.append(
                    _unverifiable(edit.event_id, move_to, "move destination is not absent")
                )
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            target.rename(destination)
            records[move_to].pre_verified = True


def _path_state(path: Path) -> tuple[Any, ...]:
    if path.is_symlink():
        return ("symlink", os.readlink(path))
    if not path.exists():
        return ("missing",)
    if path.is_file():
        mode = path.stat().st_mode & 0o111
        return ("file", mode, path.read_bytes())
    if path.is_dir():
        return ("directory",)
    return ("special",)


def _changed_paths(root: Path, anchor_sha: str, outcome_sha: str) -> list[str]:
    output = git_state.run_git(
        root,
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        f"{anchor_sha}..{outcome_sha}",
    )
    return sorted(path for path in output.split("\x00") if path)


def _report(
    *,
    anchor: git_state.BaseResolution,
    records: dict[str, _FileEvidence],
    changed_paths: list[str],
    unverifiable: list[dict[str, Any]],
    pre_mismatches: list[dict[str, Any]],
    post_mismatches: list[str],
) -> ReconstructionReport:
    touched = tuple(sorted(records))
    verified_pre = tuple(sorted(path for path, item in records.items() if item.pre_verified))
    verified_post = tuple(sorted(path for path, item in records.items() if item.post_verified))
    denominator = max(len(touched), len(unverifiable), 1)
    pre_ratio = len(verified_pre) / max(len(touched), 1)
    post_ratio = len(verified_post) / max(len(touched), 1)
    unverifiable_fraction = min(len(unverifiable) / denominator, 1.0)
    raw_confidence = round(
        max(0.0, min(1.0, min(pre_ratio, post_ratio) * (1.0 - unverifiable_fraction))),
        6,
    )
    fully_verified = bool(touched) and (
        set(touched) == set(verified_pre) == set(verified_post)
        and not unverifiable
        and not pre_mismatches
        and not post_mismatches
    )
    confidence = 1.0 if fully_verified else min(raw_confidence, 0.79)
    anchor_sha = anchor.base_sha or ""
    anchor_method = str(anchor.evidence.get("anchor_method") or "unknown")
    return ReconstructionReport(
        anchor_sha=anchor_sha,
        anchor_method=anchor_method,
        files_touched=touched,
        files_verified_pre=verified_pre,
        files_verified_post=verified_post,
        unverifiable_edits=tuple(unverifiable),
        pre_mismatches=tuple(pre_mismatches),
        post_mismatches=tuple(sorted(post_mismatches)),
        changed_paths=tuple(changed_paths),
        per_file={
            path: {
                "event_ids": list(item.event_ids),
                "pre_verified": item.pre_verified,
                "post_verified": item.post_verified,
            }
            for path, item in sorted(records.items())
        },
        touched_paths_clean=bool(touched) and set(touched) == set(verified_pre),
        confidence=confidence,
    )


def verify_reconstruction(
    *,
    root: Path,
    events: list[NormalizedEvent],
    anchor: git_state.BaseResolution,
    outcome_sha: str,
    captured_cwd: str,
) -> git_state.BaseResolution:
    """Replay normalized edits in detached snapshots and verify both boundaries."""
    if not anchor.resolved or not anchor.base_sha:
        return anchor
    failed_calls, failed_parents = _failed_edit_events(events)
    records: dict[str, _FileEvidence] = {}
    unverifiable: list[dict[str, Any]] = []
    pre_mismatches: list[dict[str, Any]] = []
    post_mismatches: list[str] = []
    recorded_paths: set[str] = set()

    try:
        with tempfile.TemporaryDirectory(
            prefix=".retro-reconstruction.",
            dir=root.parent,
        ) as scratch_name:
            scratch = Path(scratch_name)
            replay = git_state.materialize_tree(root, anchor.base_sha, scratch / "replay")
            outcome = git_state.materialize_tree(root, outcome_sha, scratch / "outcome")

            for event in events:
                call_id = _call_id(event)
                if event.event_id in failed_parents or (call_id and call_id in failed_calls):
                    continue
                if event.event_type == "file_edit":
                    edits = _extract_edits(event, root=root, captured_cwd=captured_cwd)
                    for edit in edits:
                        if edit.kind == "patch":
                            before = set(records)
                            _apply_patch_edit(
                                replay,
                                edit,
                                records,
                                root=root,
                                captured_cwd=captured_cwd,
                                unverifiable=unverifiable,
                                pre_mismatches=pre_mismatches,
                            )
                            recorded_paths.update(set(records) - before)
                            recorded_paths.update(
                                path
                                for path, item in records.items()
                                if edit.event_id in item.event_ids
                            )
                            continue
                        if edit.path is None:
                            unverifiable.append(
                                _unverifiable(
                                    edit.event_id,
                                    None,
                                    edit.reason or "file-edit path is unavailable",
                                )
                            )
                            continue
                        first = _record_file(records, edit.path, edit.event_id)
                        recorded_paths.add(edit.path)
                        target = _safe_target(replay, edit.path)
                        if target is None:
                            unverifiable.append(
                                _unverifiable(
                                    edit.event_id,
                                    edit.path,
                                    "edit path traverses a symlink",
                                )
                            )
                            continue
                        if edit.kind == "replace":
                            applied, reason = _apply_replace(target, edit)
                            if not applied:
                                pre_mismatches.append(
                                    _unverifiable(edit.event_id, edit.path, reason)
                                )
                            elif first:
                                records[edit.path].pre_verified = True
                        elif edit.kind == "write" and edit.new is not None:
                            existed = target.exists() or target.is_symlink()
                            if target.is_symlink() or (existed and not target.is_file()):
                                unverifiable.append(
                                    _unverifiable(
                                        edit.event_id,
                                        edit.path,
                                        "write target is not a regular file",
                                    )
                                )
                                continue
                            if first and existed:
                                unverifiable.append(
                                    _unverifiable(
                                        edit.event_id,
                                        edit.path,
                                        "full-file write carries no pre-edit content",
                                    )
                                )
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(edit.new, encoding="utf-8")
                            if first and not existed:
                                records[edit.path].pre_verified = True
                        else:
                            if not (
                                event.actor == "system"
                                and edit.path in recorded_paths
                            ):
                                unverifiable.append(
                                    _unverifiable(
                                        edit.event_id,
                                        edit.path,
                                        edit.reason or "file edit is not replayable",
                                    )
                                )
                elif event.event_type in ("command", "tool_call"):
                    command = event_command_text(event)
                    if (
                        command
                        and _SHELL_FILE_MUTATION_RE.search(command)
                    ):
                        unverifiable.append(
                            _unverifiable(
                                event.event_id,
                                None,
                                "shell command may have changed repository files",
                            )
                        )

            changed_paths = _changed_paths(root, anchor.base_sha, outcome_sha)
            for path in changed_paths:
                if path not in records:
                    records[path] = _FileEvidence()
                if path not in recorded_paths:
                    unverifiable.append(
                        _unverifiable(
                            "outcome-diff",
                            path,
                            "outcome changed a path with no replayable file-edit event",
                        )
                    )
            for path, file_evidence in records.items():
                replay_path = _safe_target(replay, path)
                outcome_path = _safe_target(outcome, path)
                if replay_path is None or outcome_path is None:
                    unverifiable.append(
                        _unverifiable(
                            "path-verification",
                            path,
                            "touched path traverses a symlink",
                        )
                    )
                    continue
                if _path_state(replay_path) == _path_state(outcome_path):
                    file_evidence.post_verified = True
                else:
                    post_mismatches.append(path)
    except (OSError, git_state.GitError) as error:
        return git_state.BaseResolution(
            base_sha=None,
            base_tree=None,
            resolution="unresolved",
            rejection_code="HARNESS_ERROR",
            detail=f"reconstruction snapshot failure: {error}",
            evidence=dict(anchor.evidence),
        )

    report = _report(
        anchor=anchor,
        records=records,
        changed_paths=changed_paths,
        unverifiable=unverifiable,
        pre_mismatches=pre_mismatches,
        post_mismatches=post_mismatches,
    )
    base_evidence = {**anchor.evidence, "reconstruction": report.to_dict()}
    if report.accepted:
        return git_state.BaseResolution(
            base_sha=anchor.base_sha,
            base_tree=anchor.base_tree,
            resolution="anchored_reconstruction",
            state_confidence="approximate",
            evidence=base_evidence,
        )
    if report.unverifiable_edits:
        code = "RECONSTRUCTION_UNVERIFIABLE_EDIT"
        detail = "reconstruction contains edits without replayable pre-state evidence"
    elif report.pre_mismatches or set(report.files_touched) != set(report.files_verified_pre):
        code = "RECONSTRUCTION_PRE_MISMATCH"
        detail = "rollout pre-edit evidence does not match the anchor"
    else:
        code = "RECONSTRUCTION_POST_MISMATCH"
        detail = "forward-replayed edits do not match the durable outcome"
    return git_state.BaseResolution(
        base_sha=None,
        base_tree=None,
        resolution="unresolved",
        rejection_code=code,
        detail=detail,
        evidence=base_evidence,
    )


__all__ = [
    "ReconstructionReport",
    "resolve_anchor",
    "verify_reconstruction",
]
