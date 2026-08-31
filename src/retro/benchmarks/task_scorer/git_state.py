"""Git provenance for rollout-backed benchmark sources.

Every base and outcome state is proven from captured evidence. Timestamp
inference ("latest commit before the session started") is never used, because a
commit timestamp identifies neither the checked-out branch nor the worktree
state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...schema import HOSTS, Host, NormalizedEvent
from ...storage import Layout
from ...utils import atomic_write_text, event_command_text, event_text, iter_jsonl
from .schema import SchemaError, require_hex40

_HEX40_RE = re.compile(r"\b[0-9a-f]{40}\b")
_SHORT_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
_REV_PARSE_HEAD_RE = re.compile(r"\bgit\b[^\n|;&]*\brev-parse\b[^\n|;&]*\bHEAD\b")
_STATUS_SHORT_RE = re.compile(
    r"\bgit\b[^\n|;&]*\bstatus\b[^\n|;&]*(?:--porcelain(?:=v[12])?|--short|-s)\b"
)
_GIT_COMMIT_RE = re.compile(r"\bgit\b[^\n|;&]*\bcommit\b")
_COMMIT_ANNOUNCE_RE = re.compile(r"^\[[^\]\s]+(?:\s+\(root-commit\))?\s+(?P<sha>[0-9a-f]{7,40})\]")
_PR_URL_RE = re.compile(
    r"https?://(?P<host>[^\s/]+)/(?P<owner>[^\s/]+)/(?P<repo>[^\s/?#]+)/pull/"
    r"(?P<number>\d+)"
)
_PR_HASH_RE = re.compile(
    r"\b(?:PR|pull\s+request)\s*#(?P<number>\d{1,7})\b",
    re.IGNORECASE,
)
_CREDENTIALS_RE = re.compile(r"//[^/@\s]+@")
_SAFE_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_WRAPPED_EXIT_RE = re.compile(r"(?:Process )?[Ee]xited with code (?P<code>\d+)")
_PORCELAIN_V1_ENTRY_RE = re.compile(r"^[ MADRCUT?!]{2} ")
_PORCELAIN_V2_HEADER_RE = re.compile(
    r"^# (?:branch\.(?:oid|head|upstream|ab)|stash)(?: |$)"
)
_SCP_REMOTE_RE = re.compile(r"^(?:[^@\s]+@)?(?P<host>[^:\s]+):(?P<path>.+)$")

_MUTATING_EVENT_TYPES = frozenset({"file_edit"})
_MUTATING_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:apply_patch|patch\b|sed\s+-i|tee\b|"
    r"git\s+(?:apply|checkout|switch|reset|stash|commit|merge|rebase|cherry-pick)\b|"
    r">{1,2}\s*\S)"
)

CAPTURE_SCHEMA = "retro-repo-state-v1"


class GitError(RuntimeError):
    """A git invocation failed."""


def run_git(cwd: Path, *args: str, check: bool = True) -> str:
    process = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        if check:
            detail = process.stderr.strip() or process.stdout.strip()
            raise GitError(f"git {' '.join(args)} failed: {detail}")
        return ""
    return process.stdout.strip()


def repo_root(path: Path) -> Path | None:
    """Return the Git top level containing *path*, or None."""
    if not path.exists():
        return None
    probe = path if path.is_dir() else path.parent
    try:
        top = run_git(probe, "rev-parse", "--show-toplevel")
    except GitError:
        return None
    return Path(top) if top else None


def commit_exists(root: Path, sha: str) -> bool:
    if not _HEX40_RE.fullmatch(sha or ""):
        return False
    process = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return process.returncode == 0


def commit_tree(root: Path, sha: str) -> str:
    return run_git(root, "rev-parse", f"{sha}^{{tree}}")


def resolve_rev(root: Path, rev: str) -> str | None:
    process = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    value = process.stdout.strip()
    return value if process.returncode == 0 and _HEX40_RE.fullmatch(value) else None


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        text=True,
        capture_output=True,
        check=False,
    )
    return process.returncode == 0


def canonical_remote(root: Path) -> str | None:
    url = run_git(root, "config", "--get", "remote.origin.url", check=False)
    if not url:
        return None
    stripped = _CREDENTIALS_RE.sub("//", url.strip())
    if stripped.endswith(".git"):
        stripped = stripped[: -len(".git")]
    return stripped.rstrip("/")


def repo_identity(root: Path) -> str:
    """Stable ``repo_id`` from the canonical remote, falling back to the root path."""
    remote = canonical_remote(root)
    material = remote if remote else str(root.resolve())
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# cwd resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CwdResolution:
    cwd: str | None
    source: str
    exists: bool = False
    root: Path | None = None


def _first_cwd_from_mapping(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("cwd", "current_working_directory", "workspace", "workspaceFolder"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def resolve_session_cwd(
    *,
    raw_dir: Path | None = None,
    events: Sequence[NormalizedEvent] = (),
) -> CwdResolution:
    """Resolve the session working directory from raw capture, then events.

    Codex stores it in ``thread.json``; Claude Code carries it on raw transcript
    events; every host also leaves it on normalized session/start payloads.
    """
    candidates: list[tuple[str, str]] = []
    if raw_dir is not None:
        thread_json = raw_dir / "thread.json"
        if thread_json.is_file():
            try:
                meta = json.loads(thread_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = None
            value = _first_cwd_from_mapping(meta)
            if value:
                candidates.append((value, "thread.json"))
        if not candidates:
            for name in ("transcript.jsonl", "rollout.jsonl", "events.jsonl", "session.jsonl"):
                raw_file = raw_dir / name
                if not raw_file.is_file():
                    continue
                for _line, record in iter_jsonl(raw_file):
                    value = _first_cwd_from_mapping(record)
                    if value is None:
                        value = _first_cwd_from_mapping(record.get("payload"))
                    if value:
                        candidates.append((value, f"raw:{name}"))
                        break
                if candidates:
                    break
        if not candidates:
            meta_path = raw_dir / "import_meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    meta = None
                value = _first_cwd_from_mapping(meta)
                if value:
                    candidates.append((value, "import_meta.json"))

    if not candidates:
        for event in events:
            value = _first_cwd_from_mapping(event.payload)
            if value:
                candidates.append((value, "normalized_event"))
                break

    if not candidates:
        return CwdResolution(cwd=None, source="unresolved")
    cwd, source = candidates[0]
    path = Path(cwd)
    if not path.exists():
        return CwdResolution(cwd=cwd, source=source, exists=False)
    return CwdResolution(cwd=cwd, source=source, exists=True, root=repo_root(path))


# ---------------------------------------------------------------------------
# clean-state capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoStateCapture:
    root: str
    head_sha: str
    head_tree: str
    porcelain: str
    submodules: str
    captured_at: str | None = None
    branch: str | None = None

    @property
    def dirty_entries(self) -> list[str]:
        return _porcelain_dirty_entries(self.porcelain)

    @property
    def dirty_submodules(self) -> list[str]:
        dirty: list[str] = []
        for line in self.submodules.splitlines():
            if line and line[0] in "+-U":
                dirty.append(line.strip())
        return dirty

    @property
    def clean(self) -> bool:
        return not self.dirty_entries and not self.dirty_submodules

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURE_SCHEMA,
            "root": self.root,
            "head_sha": self.head_sha,
            "head_tree": self.head_tree,
            "branch": self.branch,
            "porcelain": self.porcelain,
            "submodules": self.submodules,
            "clean": self.clean,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "repo_state") -> RepoStateCapture:
        if not isinstance(data, dict):
            raise SchemaError(f"{where} must be an object")
        version = data.get("schema_version")
        if version != CAPTURE_SCHEMA:
            raise SchemaError(f"{where}.schema_version must be {CAPTURE_SCHEMA!r}, got {version!r}")
        for key in ("root", "head_sha", "head_tree"):
            if not isinstance(data.get(key), str):
                raise SchemaError(f"{where}.{key} must be a string")
        require_hex40(str(data["head_sha"]), f"{where}.head_sha")
        require_hex40(str(data["head_tree"]), f"{where}.head_tree")
        porcelain = data.get("porcelain", "")
        submodules = data.get("submodules", "")
        if not isinstance(porcelain, str) or not isinstance(submodules, str):
            raise SchemaError(f"{where}.porcelain and {where}.submodules must be strings")
        branch = data.get("branch")
        captured_at = data.get("captured_at")
        if branch is not None and not isinstance(branch, str):
            raise SchemaError(f"{where}.branch must be a string or null")
        if captured_at is not None and not isinstance(captured_at, str):
            raise SchemaError(f"{where}.captured_at must be a string or null")
        return cls(
            root=str(data["root"]),
            head_sha=str(data["head_sha"]),
            head_tree=str(data["head_tree"]),
            porcelain=porcelain,
            submodules=submodules,
            captured_at=captured_at,
            branch=branch,
        )


def capture_repo_state(root: Path, *, captured_at: str | None = None) -> RepoStateCapture:
    """Run the exact §5.3 command set and return the captured state."""
    top = run_git(root, "rev-parse", "--show-toplevel")
    head_sha = run_git(root, "rev-parse", "HEAD")
    head_tree = run_git(root, "rev-parse", "HEAD^{tree}")
    porcelain = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain=v2", "-z", "--untracked-files=all"],
        text=True,
        capture_output=True,
        check=False,
    )
    if porcelain.returncode != 0:
        raise GitError(f"git status failed: {porcelain.stderr.strip()}")
    submodules = run_git(root, "submodule", "status", "--recursive")
    branch = run_git(root, "rev-parse", "--abbrev-ref", "HEAD", check=False) or None
    return RepoStateCapture(
        root=top,
        head_sha=head_sha,
        head_tree=head_tree,
        porcelain=porcelain.stdout,
        submodules=submodules,
        captured_at=captured_at,
        branch=branch,
    )


def write_repo_state(path: Path, state: RepoStateCapture) -> Path:
    atomic_write_text(path, json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return path


CAPTURE_PHASES: tuple[str, ...] = ("start", "end")
CAPTURE_FILENAMES: dict[str, str] = {
    "start": "repo_start.json",
    "end": "repo_end.json",
}


class CaptureExistsError(GitError):
    """Raised when a phase capture already exists; captures are never rewritten."""

    def __init__(self, path: Path, state: RepoStateCapture | None = None) -> None:
        super().__init__(f"capture already exists at {path}")
        self.path = path
        self.state = state


@dataclass(frozen=True)
class CaptureRecord:
    """One immutable phase capture written next to a session's raw files."""

    phase: str
    path: Path
    state: RepoStateCapture
    host: str | None = None
    session_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phase": self.phase,
            "path": str(self.path),
            "root": self.state.root,
            "head_sha": self.state.head_sha,
            "head_tree": self.state.head_tree,
            "branch": self.state.branch,
            "clean": self.state.clean,
            "captured_at": self.state.captured_at,
        }
        if self.host is not None:
            payload["host"] = self.host
        if self.session_id is not None:
            payload["session_id"] = self.session_id
        return payload


def require_capture_phase(phase: str) -> str:
    if phase not in CAPTURE_PHASES:
        raise GitError(f"capture phase must be one of {CAPTURE_PHASES}, got {phase!r}")
    return phase


def capture_path(raw_dir: Path, phase: str) -> Path:
    return Path(raw_dir) / CAPTURE_FILENAMES[require_capture_phase(phase)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_exclusive(path: Path, content: str) -> None:
    """Create ``path`` with O_EXCL semantics and fully written content.

    The payload is staged in the destination directory and published with
    ``os.link``, so a concurrent writer loses the race instead of truncating an
    existing capture, and readers never observe a partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    staged = Path(staged_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(staged, path)
        except FileExistsError as error:
            raise CaptureExistsError(path) from error
        except OSError:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError as exists:
                raise CaptureExistsError(path) from exists
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_session_capture(
    raw_dir: Path,
    phase: str,
    *,
    cwd: Path | None = None,
    captured_at: str | None = None,
) -> CaptureRecord:
    """Capture the working tree state for ``phase`` and publish it immutably.

    This is the contract behind ``retro capture start|end``: it runs exactly the
    §5.3 command set (``rev-parse --show-toplevel``, ``rev-parse HEAD``,
    ``rev-parse HEAD^{tree}``, ``status --porcelain=v2 -z --untracked-files=all``
    and ``submodule status --recursive``) against the live worktree and writes
    ``repo_start.json``/``repo_end.json`` with O_EXCL semantics. An existing
    capture is never overwritten, so selection can trust it as evidence.
    """
    require_capture_phase(phase)
    origin = Path(cwd) if cwd is not None else Path.cwd()
    if not origin.exists():
        raise GitError(f"capture directory does not exist: {origin}")
    root = repo_root(origin)
    if root is None:
        raise GitError(f"not inside a Git repository: {origin}")
    path = capture_path(raw_dir, phase)
    if path.exists():
        raise CaptureExistsError(path, read_capture(raw_dir, phase))
    state = capture_repo_state(root, captured_at=captured_at or _utc_now())
    _write_exclusive(path, json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return CaptureRecord(phase=phase, path=path, state=state)


def read_capture(raw_dir: Path, phase: str) -> RepoStateCapture | None:
    return load_repo_state(capture_path(raw_dir, phase))


def capture_repository_state(
    *,
    layout: Layout,
    host: Host,
    session_id: str,
    cwd: Path | None = None,
    phase: str = "start",
) -> CaptureRecord:
    """CLI entry point for ``retro capture start|end``.

    Writes ``raw/<host>/<session_id>/repo_<phase>.json`` from the live worktree
    at ``cwd``. The capture is immutable: a second call for the same phase
    raises :class:`CaptureExistsError` instead of rewriting evidence. Importers
    preserve these sidecars, so a capture made before the session starts remains
    the exact-base proof after a post-hoc import.
    """
    require_capture_phase(phase)
    if host not in HOSTS:
        raise GitError(f"capture host must be one of {HOSTS}, got {host!r}")
    if not _SAFE_CAPTURE_ID_RE.fullmatch(session_id):
        raise GitError("capture session id contains unsupported characters")
    raw_dir = layout.raw_dir(host, session_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    record = write_session_capture(raw_dir, phase, cwd=cwd)
    return replace(record, host=host, session_id=session_id)


def load_repo_state(path: Path) -> RepoStateCapture | None:
    if not path.is_file():
        return None
    return RepoStateCapture.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_captured_start(raw_dir: Path) -> RepoStateCapture | None:
    return read_capture(raw_dir, "start")


def load_captured_end(raw_dir: Path) -> RepoStateCapture | None:
    return read_capture(raw_dir, "end")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _capture_timing_problem(
    capture: RepoStateCapture,
    *,
    boundary: str | None,
    phase: str,
) -> str | None:
    if boundary is None:
        return None
    captured_at = _parse_timestamp(capture.captured_at)
    boundary_at = _parse_timestamp(boundary)
    filename = CAPTURE_FILENAMES[phase]
    if captured_at is None:
        return f"{filename} has no valid captured_at timestamp"
    if boundary_at is None:
        return f"session {phase} boundary has no valid timestamp"
    if phase == "start" and captured_at > boundary_at:
        return f"{filename} was captured after the rollout started"
    if phase == "end" and captured_at < boundary_at:
        return f"{filename} was captured before the rollout ended"
    return None


def _event_timestamp_boundary(
    events: Sequence[NormalizedEvent],
    *,
    earliest: bool,
) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for event in events:
        if not event.timestamp:
            continue
        timestamp = _parse_timestamp(event.timestamp)
        if timestamp is None:
            return event.timestamp
        parsed.append((timestamp, event.timestamp))
    if not parsed:
        return None
    selector = min if earliest else max
    return selector(parsed, key=lambda item: item[0])[1]


# ---------------------------------------------------------------------------
# rollout evidence
# ---------------------------------------------------------------------------


def _event_output_text(event: NormalizedEvent) -> str:
    payload = event.payload or {}
    chunks: list[str] = []
    for key in ("output", "stdout", "result", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str):
            chunks.append(value)
    if not chunks:
        chunks.append(event_text(event))
    return "\n".join(chunks)


def _event_explicit_output(event: NormalizedEvent) -> str | None:
    payload = event.payload or {}
    for key in ("output", "stdout", "result", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def _command_output(value: str) -> tuple[str, int | None]:
    marker = "\nOutput:\n"
    body = value.split(marker, 1)[1] if marker in value else value
    match = _WRAPPED_EXIT_RE.search(value)
    return body, int(match.group("code")) if match else None


def _event_succeeded(event: NormalizedEvent) -> bool:
    payload = event.payload or {}
    if payload.get("is_error") is True:
        return False
    for key in ("exit_code", "return_code", "returncode"):
        value = payload.get(key)
        if isinstance(value, int):
            return value == 0
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"error", "failed", "failure", "timeout"}:
        return False
    output = _event_explicit_output(event)
    if output is not None:
        _body, wrapped_exit = _command_output(output)
        if wrapped_exit is not None:
            return wrapped_exit == 0
    return True


@dataclass(frozen=True)
class CleanStartProof:
    observed: bool
    clean: bool
    event_id: str | None = None
    detail: str = ""


def rollout_clean_start_proof(
    events: Sequence[NormalizedEvent],
    *,
    before_index: int | None,
) -> CleanStartProof:
    """Find a successful empty porcelain-status result before the first mutation."""
    limit = len(events) if before_index is None else before_index
    pending: dict[str, str] = {}
    for event in events[:limit]:
        command = event_command_text(event)
        payload = event.payload or {}
        call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "")
        if command and _STATUS_SHORT_RE.search(command):
            output = _event_explicit_output(event)
            if output is not None:
                if not _event_succeeded(event):
                    continue
                body, _exit_code = _command_output(output)
                return CleanStartProof(
                    observed=True,
                    clean=_short_status_is_clean(body),
                    event_id=event.event_id,
                    detail=body[:1000],
                )
            pending[call_id or "__last__"] = event.event_id
            continue
        if event.event_type not in ("tool_result", "command"):
            continue
        source_event = pending.pop(call_id, None) or pending.pop("__last__", None)
        if source_event is None or not _event_succeeded(event):
            continue
        output = _event_explicit_output(event)
        if output is None:
            continue
        body, wrapped_exit = _command_output(output)
        if wrapped_exit is not None and wrapped_exit != 0:
            continue
        return CleanStartProof(
            observed=True,
            clean=_short_status_is_clean(body),
            event_id=source_event,
            detail=body[:1000],
        )
    return CleanStartProof(observed=False, clean=False)


def _short_status_is_clean(output: str) -> bool:
    """Accept v1/v2 porcelain evidence only when it contains no worktree rows."""
    return not _porcelain_dirty_entries(output)


def _porcelain_dirty_entries(output: str) -> list[str]:
    """Return dirty or malformed porcelain records, handling LF and NUL framing."""
    dirty: list[str] = []
    for raw_record in re.split(r"[\0\n]", output):
        record = raw_record.rstrip("\r")
        if not record:
            continue
        if record.startswith("## ") or _PORCELAIN_V2_HEADER_RE.match(record):
            continue
        if (
            _PORCELAIN_V1_ENTRY_RE.match(record)
            or record.startswith(("1 ", "2 ", "u ", "? ", "! "))
        ):
            dirty.append(record)
            continue
        # Porcelain output has a closed record grammar. Unknown non-empty text
        # cannot safely establish a clean worktree.
        dirty.append(record)
    return dirty


def is_mutating_event(event: NormalizedEvent) -> bool:
    if event.event_type in _MUTATING_EVENT_TYPES:
        return True
    if event.event_type in ("command", "tool_call"):
        command = event_command_text(event)
        if command and _MUTATING_COMMAND_RE.search(command):
            return True
    return False


def first_mutating_index(events: Sequence[NormalizedEvent]) -> int | None:
    for index, event in enumerate(events):
        if is_mutating_event(event):
            return index
    return None


@dataclass(frozen=True)
class RolloutCommit:
    sha: str
    event_id: str


def rollout_head_observations(
    events: Sequence[NormalizedEvent],
    *,
    before_index: int | None = None,
) -> list[tuple[str, str]]:
    """Return ``(sha, event_id)`` for successful ``git rev-parse HEAD`` results."""
    limit = len(events) if before_index is None else before_index
    observations: list[tuple[str, str]] = []
    pending: dict[str, str] = {}
    for event in events[:limit]:
        command = event_command_text(event)
        payload = event.payload or {}
        call_id = str(payload.get("call_id") or payload.get("tool_call_id") or "")
        if command and _REV_PARSE_HEAD_RE.search(command):
            output = _event_output_text(event)
            match = _HEX40_RE.search(output)
            if match:
                observations.append((match.group(0), event.event_id))
                continue
            if call_id:
                pending[call_id] = event.event_id
            else:
                pending["__last__"] = event.event_id
            continue
        if event.event_type in ("tool_result", "command"):
            source_event = pending.pop(call_id, None) or pending.pop("__last__", None)
            if source_event is None:
                continue
            match = _HEX40_RE.search(_event_output_text(event))
            if match:
                observations.append((match.group(0), source_event))
    return observations


def rollout_created_commits(
    events: Sequence[NormalizedEvent],
    root: Path,
) -> list[RolloutCommit]:
    """Commits the rollout itself created, in chronological order."""
    commits: list[RolloutCommit] = []
    seen: set[str] = set()
    pending_event_id: str | None = None
    for event in events:
        command = event_command_text(event)
        if command and _GIT_COMMIT_RE.search(command):
            pending_event_id = event.event_id
        if pending_event_id is None:
            continue
        for line in _event_output_text(event).splitlines():
            match = _COMMIT_ANNOUNCE_RE.match(line.strip())
            if not match:
                continue
            resolved = resolve_rev(root, match.group("sha"))
            if resolved and resolved not in seen:
                seen.add(resolved)
                commits.append(RolloutCommit(sha=resolved, event_id=pending_event_id))
                pending_event_id = None
                break
    return commits


def _repository_coordinates(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    text = value.strip()
    host: str
    path: str
    if "://" in text:
        parsed = urlsplit(text)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        path = parsed.path
    else:
        match = _SCP_REMOTE_RE.match(text)
        if match is None:
            return None
        host = match.group("host")
        path = match.group("path")
    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    if not owner or not repo:
        return None
    return host.casefold(), owner.casefold(), repo.casefold()


def linked_pull_requests(
    events: Iterable[NormalizedEvent],
    *,
    root: Path | None = None,
) -> list[int]:
    """Return local PR numbers explicitly referenced in rollout text."""
    selected_repo = _repository_coordinates(canonical_remote(root)) if root is not None else None
    references: list[tuple[int, str]] = []
    local_url_numbers: set[int] = set()
    external_url_numbers: set[int] = set()
    for event in events:
        text = "\n".join(filter(None, (event.summary, event_text(event), event_command_text(event))))
        if not text:
            continue
        for match in _PR_URL_RE.finditer(text):
            number = int(match.group("number"))
            linked_repo = (
                match.group("host").casefold(),
                match.group("owner").casefold(),
                match.group("repo").removesuffix(".git").casefold(),
            )
            if selected_repo is None or linked_repo != selected_repo:
                external_url_numbers.add(number)
                references.append((number, "external_url"))
            else:
                local_url_numbers.add(number)
                references.append((number, "local_url"))
        for match in _PR_HASH_RE.finditer(text):
            references.append((int(match.group("number")), "number"))
    numbers: list[int] = []
    seen: set[int] = set()
    for number, reference_kind in references:
        local_reference = reference_kind == "local_url" or (
            reference_kind == "number"
            and (number not in external_url_numbers or number in local_url_numbers)
        )
        if local_reference and number not in seen:
            seen.add(number)
            numbers.append(number)
    return numbers


def pr_merge_commit(root: Path, number: int, *, branch: str) -> str | None:
    output = run_git(
        root,
        "log",
        "--merges",
        "--format=%H%x1f%s",
        f"--grep=#{number}",
        branch,
        check=False,
    )
    boundary = re.compile(rf"#{number}(?!\d)")
    for line in output.splitlines():
        sha, _, subject = line.partition("\x1f")
        if _HEX40_RE.fullmatch(sha) and boundary.search(subject):
            return sha
    return None


def _has_visible_completion_after(
    events: Sequence[NormalizedEvent],
    event_id: str,
) -> bool:
    event_index = next(
        (index for index, event in enumerate(events) if event.event_id == event_id),
        None,
    )
    if event_index is None:
        return False
    return any(
        event.actor == "assistant"
        and event.event_type == "message"
        and bool(event_text(event).strip())
        for event in events[event_index + 1 :]
    )


# ---------------------------------------------------------------------------
# base and outcome resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseResolution:
    base_sha: str | None
    base_tree: str | None
    resolution: str
    state_confidence: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    rejection_code: str | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.base_sha is not None and self.rejection_code is None


def resolve_base(
    *,
    root: Path,
    events: Sequence[NormalizedEvent],
    captured_start: RepoStateCapture | None = None,
    session_started_at: str | None = None,
) -> BaseResolution:
    """Resolve the base commit using only captured or rollout-proved evidence."""
    if captured_start is not None:
        if Path(captured_start.root).resolve() != root.resolve():
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="NO_EXACT_BASE_SHA",
                detail="repo_start.json belongs to a different repository root",
                evidence={"captured_root": captured_start.root, "resolved_root": str(root)},
            )
        if not captured_start.clean:
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="DIRTY_START_STATE",
                detail="repo_start.json reports a dirty worktree or submodule",
                evidence={"dirty_entries": captured_start.dirty_entries[:20]},
            )
        start_boundary = session_started_at or _event_timestamp_boundary(events, earliest=True)
        timing_problem = _capture_timing_problem(
            captured_start,
            boundary=start_boundary,
            phase="start",
        )
        if timing_problem is not None:
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="NO_EXACT_BASE_SHA",
                detail=timing_problem,
                evidence={
                    "captured_at": captured_start.captured_at,
                    "session_started_at": start_boundary,
                },
            )
        if not commit_exists(root, captured_start.head_sha):
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="NO_EXACT_BASE_SHA",
                detail="captured start commit is not present in the local repository",
                evidence={"head_sha": captured_start.head_sha},
            )
        tree = commit_tree(root, captured_start.head_sha)
        if tree != captured_start.head_tree:
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="DIRTY_START_STATE",
                detail="base_sha^{tree} does not equal the captured start tree",
                evidence={"captured_tree": captured_start.head_tree, "commit_tree": tree},
            )
        return BaseResolution(
            base_sha=captured_start.head_sha,
            base_tree=tree,
            resolution="captured_start",
            state_confidence="exact_clean_commit",
            evidence={"source": "repo_start.json"},
        )

    mutating_index = first_mutating_index(events)
    clean_proof = rollout_clean_start_proof(events, before_index=mutating_index)
    observations = rollout_head_observations(events, before_index=mutating_index)
    if observations and not clean_proof.observed:
        return BaseResolution(
            base_sha=None,
            base_tree=None,
            resolution="unresolved",
            rejection_code="DIRTY_START_STATE",
            detail="rollout recorded HEAD but did not prove a clean start worktree",
            evidence={"head_event_ids": [event_id for _, event_id in observations][:5]},
        )
    if observations and not clean_proof.clean:
        return BaseResolution(
            base_sha=None,
            base_tree=None,
            resolution="unresolved",
            rejection_code="DIRTY_START_STATE",
            detail="rollout porcelain status proves the start worktree was dirty",
            evidence={"status_event_id": clean_proof.event_id, "status": clean_proof.detail},
        )
    for sha, event_id in observations:
        if commit_exists(root, sha):
            return BaseResolution(
                base_sha=sha,
                base_tree=commit_tree(root, sha),
                resolution="rollout_command",
                state_confidence="exact_clean_commit",
                evidence={
                    "event_id": event_id,
                    "source": "git rev-parse HEAD",
                    "clean_status_event_id": clean_proof.event_id,
                },
            )
    if observations:
        return BaseResolution(
            base_sha=None,
            base_tree=None,
            resolution="unresolved",
            rejection_code="NO_EXACT_BASE_SHA",
            detail="rollout HEAD observation is not a commit in this repository",
            evidence={"observed": [sha for sha, _ in observations][:5]},
        )

    commits = rollout_created_commits(events, root)
    if commits:
        first = commits[0]
        parents = run_git(root, "rev-list", "--parents", "-n", "1", first.sha, check=False).split()
        if len(parents) < 2:
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="NO_EXACT_BASE_SHA",
                detail="first rollout commit has no parent",
                evidence={"commit": first.sha},
            )
        parent = parents[1]
        if not clean_proof.observed or not clean_proof.clean:
            return BaseResolution(
                base_sha=None,
                base_tree=None,
                resolution="unresolved",
                rejection_code="DIRTY_START_STATE",
                detail="first commit parent cannot be used without a clean-start porcelain proof",
                evidence={
                    "commit": first.sha,
                    "parent": parent,
                    "status_event_id": clean_proof.event_id,
                    "status": clean_proof.detail,
                },
            )
        return BaseResolution(
            base_sha=parent,
            base_tree=commit_tree(root, parent),
            resolution="first_commit_parent",
            state_confidence="approximate",
            evidence={
                "commit": first.sha,
                "event_id": first.event_id,
                "clean_status_event_id": clean_proof.event_id,
            },
        )

    return BaseResolution(
        base_sha=None,
        base_tree=None,
        resolution="unresolved",
        rejection_code="NO_EXACT_BASE_SHA",
        detail="no captured start state, HEAD observation, or rollout commit",
    )


@dataclass(frozen=True)
class OutcomeResolution:
    outcome_sha: str | None
    outcome_tree: str | None
    resolution: str
    evidence: dict[str, Any] = field(default_factory=dict)
    rejection_code: str | None = None
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.outcome_sha is not None and self.rejection_code is None


def resolve_outcome(
    *,
    root: Path,
    events: Sequence[NormalizedEvent],
    base_sha: str,
    branch: str = "HEAD",
    captured_end: RepoStateCapture | None = None,
    user_accepted: bool = False,
    session_ended_at: str | None = None,
) -> OutcomeResolution:
    """Resolve the accepted outcome commit in the §5.4 priority order."""
    for number in linked_pull_requests(events, root=root):
        merge_sha = pr_merge_commit(root, number, branch=branch)
        if merge_sha and is_ancestor(root, base_sha, merge_sha):
            return OutcomeResolution(
                outcome_sha=merge_sha,
                outcome_tree=commit_tree(root, merge_sha),
                resolution="linked_pr_merge",
                evidence={"pull_request": number},
            )

    durable = [
        commit
        for commit in rollout_created_commits(events, root)
        if is_ancestor(root, commit.sha, branch)
        and is_ancestor(root, base_sha, commit.sha)
        and (user_accepted or _has_visible_completion_after(events, commit.event_id))
    ]
    if durable:
        final = durable[-1]
        return OutcomeResolution(
            outcome_sha=final.sha,
            outcome_tree=commit_tree(root, final.sha),
            resolution="rollout_commit",
            evidence={"event_id": final.event_id, "commit_count": len(durable)},
        )

    if captured_end is not None:
        if not captured_end.clean:
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="OUTCOME_NOT_DURABLE",
                detail="session ended with uncommitted work; v1 rejects dirty accepted outcomes",
            )
        if not user_accepted:
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="NO_OUTCOME_SHA",
                detail="captured end state exists but the user did not explicitly accept it",
            )
        if Path(captured_end.root).resolve() != root.resolve():
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="NO_OUTCOME_SHA",
                detail="repo_end.json belongs to a different repository root",
            )
        end_boundary = session_ended_at or _event_timestamp_boundary(events, earliest=False)
        timing_problem = _capture_timing_problem(
            captured_end,
            boundary=end_boundary,
            phase="end",
        )
        if timing_problem is not None:
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="NO_OUTCOME_SHA",
                detail=timing_problem,
                evidence={
                    "captured_at": captured_end.captured_at,
                    "session_ended_at": end_boundary,
                },
            )
        if not commit_exists(root, captured_end.head_sha):
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="NO_OUTCOME_SHA",
                detail="captured end commit is not present in the local repository",
            )
        tree = commit_tree(root, captured_end.head_sha)
        if tree != captured_end.head_tree:
            return OutcomeResolution(
                outcome_sha=None,
                outcome_tree=None,
                resolution="unresolved",
                rejection_code="OUTCOME_NOT_DURABLE",
                detail="outcome_sha^{tree} does not equal the captured end tree",
                evidence={"captured_tree": captured_end.head_tree, "commit_tree": tree},
            )
        return OutcomeResolution(
            outcome_sha=captured_end.head_sha,
            outcome_tree=tree,
            resolution="captured_end",
            evidence={"source": "repo_end.json"},
        )

    return OutcomeResolution(
        outcome_sha=None,
        outcome_tree=None,
        resolution="unresolved",
        rejection_code="NO_OUTCOME_SHA",
        detail="no linked pull request, durable rollout commit, or accepted clean end state",
    )


def find_revert(root: Path, sha: str, *, branch: str = "HEAD") -> dict[str, Any] | None:
    """Return the commit that reverted *sha* on *branch*, if any."""
    short = sha[:7]
    output = run_git(
        root,
        "log",
        "--format=%H%x1f%cI%x1f%B%x1e",
        f"{sha}..{branch}",
        check=False,
    )
    for record in output.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 3:
            continue
        commit_sha, committed_at, message = parts[0], parts[1], parts[2]
        if not _HEX40_RE.fullmatch(commit_sha):
            continue
        lowered = message.lower()
        if "revert" in lowered and (sha in message or short in message):
            return {"sha": commit_sha, "committed_at": committed_at}
    return None


# ---------------------------------------------------------------------------
# materialization and diffs
# ---------------------------------------------------------------------------


def materialize_tree(root: Path, sha: str, dest: Path) -> Path:
    """Materialize *sha* as a detached, ``.git``-free checkout at *dest*."""
    if not commit_exists(root, sha):
        raise GitError(f"commit {sha} does not exist in {root}")
    dest.mkdir(parents=True, exist_ok=True)
    fd, archive_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tar", dir=dest.parent)
    archive_path = Path(archive_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            process = subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar", sha],
                stdout=handle,
                stderr=subprocess.PIPE,
                check=False,
            )
        if process.returncode != 0:
            raise GitError(f"git archive failed: {process.stderr.decode('utf-8', 'replace').strip()}")
        with tarfile.open(archive_path, "r") as tar:
            destination = dest.resolve()
            for member in tar.getmembers():
                target = (destination / member.name).resolve()
                if target != destination and destination not in target.parents:
                    raise GitError(f"refusing to extract outside destination: {member.name}")
                if member.isdev() or member.isfifo():
                    raise GitError(f"refusing to extract special file: {member.name}")
                if member.issym() or member.islnk():
                    link_base = target.parent if member.issym() else destination
                    link_target = (link_base / member.linkname).resolve()
                    if link_target != destination and destination not in link_target.parents:
                        raise GitError(
                            f"refusing archive link outside destination: "
                            f"{member.name} -> {member.linkname}"
                        )
            tar.extractall(dest)  # noqa: S202 - members validated above
    finally:
        if archive_path.exists():
            archive_path.unlink()
    return dest


def change_patch(root: Path, base_sha: str, outcome_sha: str) -> str:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.abbrev=40",
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--binary",
            f"{base_sha}..{outcome_sha}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        raise GitError(f"git diff failed: {process.stderr.strip()}")
    return process.stdout


def changed_paths(root: Path, base_sha: str, outcome_sha: str) -> list[str]:
    output = run_git(root, "diff", "--name-only", f"{base_sha}..{outcome_sha}")
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def commit_range(root: Path, base_sha: str, outcome_sha: str) -> list[dict[str, Any]]:
    """Structured ``git log`` entries for ``base..outcome`` in chronological order."""
    output = run_git(
        root,
        "log",
        "--reverse",
        "--format=%H%x1f%P%x1f%aI%x1f%cI%x1f%s%x1e",
        f"{base_sha}..{outcome_sha}",
        check=False,
    )
    entries: list[dict[str, Any]] = []
    for record in output.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f")
        if len(parts) < 5:
            continue
        sha, parents, authored_at, committed_at, subject = parts[:5]
        if not _HEX40_RE.fullmatch(sha):
            continue
        entries.append(
            {
                "sha": sha,
                "parents": parents.split() if parents else [],
                "authored_at": authored_at,
                "committed_at": committed_at,
                "subject": subject,
                "files": changed_paths(root, f"{sha}^", sha) if parents else [],
            }
        )
    return entries


def added_lines(patch: str) -> list[str]:
    """Added source lines from a unified diff, excluding file headers."""
    lines: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            lines.append(line[1:])
    return lines


def copy_tree(source: Path, dest: Path) -> None:
    shutil.copytree(source, dest)


__all__ = [
    "CAPTURE_FILENAMES",
    "CAPTURE_PHASES",
    "CAPTURE_SCHEMA",
    "BaseResolution",
    "CaptureExistsError",
    "CaptureRecord",
    "CwdResolution",
    "GitError",
    "OutcomeResolution",
    "RepoStateCapture",
    "RolloutCommit",
    "added_lines",
    "canonical_remote",
    "capture_path",
    "capture_repo_state",
    "capture_repository_state",
    "change_patch",
    "changed_paths",
    "commit_exists",
    "commit_range",
    "commit_tree",
    "copy_tree",
    "find_revert",
    "first_mutating_index",
    "is_ancestor",
    "is_mutating_event",
    "linked_pull_requests",
    "load_captured_end",
    "load_captured_start",
    "load_repo_state",
    "materialize_tree",
    "pr_merge_commit",
    "read_capture",
    "repo_identity",
    "repo_root",
    "resolve_base",
    "resolve_outcome",
    "resolve_rev",
    "resolve_session_cwd",
    "rollout_created_commits",
    "rollout_head_observations",
    "require_capture_phase",
    "run_git",
    "write_repo_state",
    "write_session_capture",
]
