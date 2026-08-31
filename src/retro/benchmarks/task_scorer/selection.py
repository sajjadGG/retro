"""Source eligibility and explicit rejection records.

``retro benchmark taskset select`` accepts a rollout only when every §5.1
condition holds. Each failed condition produces a stable rejection code so that
yield is itself a measurable pipeline metric.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ...schema import HOSTS, Host, NormalizedEvent, read_events
from ...storage import Layout
from ...utils import atomic_write_text
from . import git_state
from .schema import ProjectEnvironment, RepoAnchor, SchemaError, require_rejection_code

SELECTION_SCHEMA = "retro-taskset-selection-v1"
DEFAULT_STABILITY_HORIZON_DAYS = 7
_ACCEPTANCE_RE = re.compile(
    r"^\s*(?:lgtm|looks good(?: to me)?|approved|ship it|merge it|"
    r"that works|accepted|i accept(?: this)?)(?:[\s,.!]|$)",
    re.IGNORECASE,
)
_ACCEPTANCE_RETRACTION_RE = re.compile(r"\b(?:but|however|except|not|don't|do not)\b", re.IGNORECASE)

EnvironmentResolver = Callable[["SourceCandidate"], Optional[ProjectEnvironment]]


def source_id_for(host: str, session_id: str) -> str:
    return f"{host}__{session_id}"


@dataclass(frozen=True)
class SourceRejection:
    source_id: str
    host: str
    session_id: str
    code: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_rejection_code(self.code, "rejection.code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "host": self.host,
            "session_id": self.session_id,
            "code": self.code,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SourceCandidate:
    """A rollout that passed every eligibility gate up to environment resolution."""

    source_id: str
    host: str
    session_id: str
    normalized_path: Path
    raw_dir: Path
    repo_root: Path
    cwd: str
    branch: str
    started_at: str | None
    ended_at: str | None
    base: git_state.BaseResolution
    outcome: git_state.OutcomeResolution
    events: Sequence[NormalizedEvent] = ()
    environment: ProjectEnvironment | None = None

    @property
    def base_sha(self) -> str:
        assert self.base.base_sha is not None
        return self.base.base_sha

    @property
    def base_tree(self) -> str:
        assert self.base.base_tree is not None
        return self.base.base_tree

    @property
    def outcome_sha(self) -> str:
        assert self.outcome.outcome_sha is not None
        return self.outcome.outcome_sha

    @property
    def outcome_tree(self) -> str:
        assert self.outcome.outcome_tree is not None
        return self.outcome.outcome_tree

    def repo_anchor(self) -> RepoAnchor:
        return RepoAnchor(
            root_at_capture=str(self.repo_root),
            repo_id=git_state.repo_identity(self.repo_root),
            base_sha=self.base_sha,
            base_tree=self.base_tree,
            outcome_sha=self.outcome_sha,
            outcome_tree=self.outcome_tree,
            base_resolution=self.base.resolution,
            outcome_resolution=self.outcome.resolution,
            state_confidence=self.base.state_confidence or "approximate",
            subdir=".",
            environment_id=self.environment.environment_id if self.environment else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "host": self.host,
            "session_id": self.session_id,
            "normalized_path": str(self.normalized_path),
            "raw_dir": str(self.raw_dir),
            "repo_root": str(self.repo_root),
            "cwd": self.cwd,
            "branch": self.branch,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "base": {
                "sha": self.base_sha,
                "tree": self.base_tree,
                "resolution": self.base.resolution,
                "state_confidence": self.base.state_confidence,
                "evidence": self.base.evidence,
            },
            "outcome": {
                "sha": self.outcome_sha,
                "tree": self.outcome_tree,
                "resolution": self.outcome.resolution,
                "evidence": self.outcome.evidence,
            },
            "environment_id": self.environment.environment_id if self.environment else None,
            "environment": self.environment.to_dict() if self.environment else None,
        }

    def selection_record(self) -> dict[str, Any]:
        """Per-source proof of selection embedded in the bundle."""
        return {
            "schema_version": SELECTION_SCHEMA,
            "status": "selected",
            "selected": True,
            "source_id": self.source_id,
            "host": self.host,
            "session_id": self.session_id,
            "base_sha": self.base_sha,
            "base_tree": self.base_tree,
            "outcome_sha": self.outcome_sha,
            "outcome_tree": self.outcome_tree,
            "base_resolution": self.base.resolution,
            "outcome_resolution": self.outcome.resolution,
            "state_confidence": self.base.state_confidence,
            "environment_id": self.environment.environment_id if self.environment else None,
            "environment_validated": self.environment is not None,
        }


@dataclass(frozen=True)
class SelectionResult:
    selected: list[SourceCandidate] = field(default_factory=list)
    rejections: list[SourceRejection] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.code] = counts.get(rejection.code, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SELECTION_SCHEMA,
            "selected": [candidate.to_dict() for candidate in self.selected],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
            "counts": {
                "selected": len(self.selected),
                "rejected": len(self.rejections),
                "by_code": self.rejection_counts(),
            },
            "environment": dict(self.environment),
        }


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _session_bounds(
    events: Sequence[NormalizedEvent],
) -> tuple[str | None, str | None]:
    stamps: list[tuple[datetime, str]] = []
    for event in events:
        if not event.timestamp:
            continue
        parsed = _parse_timestamp(event.timestamp)
        if parsed is None:
            return None, None
        stamps.append((parsed, event.timestamp))
    if not stamps:
        return None, None
    return min(stamps, key=lambda item: item[0])[1], max(stamps, key=lambda item: item[0])[1]


def _user_accepted(events: Sequence[NormalizedEvent]) -> bool:
    """True when the rollout contains an explicit user acceptance message."""
    from ...utils import event_text

    for event in reversed(list(events)):
        if event.event_type != "message" or event.actor != "user":
            continue
        text = event_text(event).strip().lower()
        return bool(_ACCEPTANCE_RE.search(text)) and not bool(
            _ACCEPTANCE_RETRACTION_RE.search(text)
        )
    return False


def select_source(
    *,
    layout: Layout,
    host: Host,
    session_id: str,
    branch: str = "HEAD",
    environment_resolver: EnvironmentResolver | None = None,
    require_environment: bool = True,
    stability_horizon_days: int = DEFAULT_STABILITY_HORIZON_DAYS,
) -> tuple[SourceCandidate | None, SourceRejection | None]:
    """Evaluate one rollout against every §5.1 eligibility condition."""
    host = normalize_host(host)
    session_id = require_safe_id(session_id, "session id")
    source_id = source_id_for(host, session_id)

    def reject(code: str, detail: str, **evidence: Any) -> tuple[None, SourceRejection]:
        return None, SourceRejection(
            source_id=source_id,
            host=host,
            session_id=session_id,
            code=code,
            detail=detail,
            evidence=dict(evidence),
        )

    normalized_path = layout.normalized_path(host, session_id)
    if not normalized_path.is_file():
        return reject("NO_NORMALIZED_ROLLOUT", f"no normalized events at {normalized_path}")
    try:
        events = list(read_events(normalized_path))
    except (OSError, ValueError, TypeError) as error:
        return reject("NO_NORMALIZED_ROLLOUT", f"unreadable normalized events: {error}")
    if not events:
        return reject("NO_NORMALIZED_ROLLOUT", "normalized event stream is empty")

    raw_dir = layout.raw_dir(host, session_id)
    cwd_resolution = git_state.resolve_session_cwd(raw_dir=raw_dir, events=events)
    if not cwd_resolution.cwd:
        return reject("NO_REPO_CWD", "no working directory recorded in raw capture or events")
    if not cwd_resolution.exists:
        return reject(
            "NO_REPO_CWD",
            "recorded working directory no longer exists",
            cwd=cwd_resolution.cwd,
        )
    root = cwd_resolution.root
    if root is None:
        return reject(
            "NOT_GIT_REPOSITORY",
            "recorded working directory is not inside a Git repository",
            cwd=cwd_resolution.cwd,
        )

    started_at, ended_at = _session_bounds(events)
    if not started_at:
        return reject("NO_NORMALIZED_ROLLOUT", "no event timestamps; session start is unknown")

    captured_start = git_state.load_captured_start(raw_dir)
    try:
        base = git_state.resolve_base(
            root=root,
            events=events,
            captured_start=captured_start,
            session_started_at=started_at,
        )
    except git_state.GitError as error:
        return reject("HARNESS_ERROR", f"git failure during base resolution: {error}")
    if not base.resolved:
        return reject(
            base.rejection_code or "NO_EXACT_BASE_SHA",
            base.detail or "base commit could not be resolved",
            **base.evidence,
        )

    captured_end = git_state.load_captured_end(raw_dir)
    try:
        outcome = git_state.resolve_outcome(
            root=root,
            events=events,
            base_sha=base.base_sha or "",
            branch=branch,
            captured_end=captured_end,
            user_accepted=_user_accepted(events),
            session_ended_at=ended_at,
        )
    except git_state.GitError as error:
        return reject("HARNESS_ERROR", f"git failure during outcome resolution: {error}")
    if not outcome.resolved:
        return reject(
            outcome.rejection_code or "NO_OUTCOME_SHA",
            outcome.detail or "outcome commit could not be resolved",
            **outcome.evidence,
        )

    assert base.base_sha and base.base_tree and outcome.outcome_sha and outcome.outcome_tree
    if base.base_tree == outcome.outcome_tree:
        return reject(
            "OUTCOME_NOT_DURABLE",
            "base and outcome trees are identical",
            base_tree=base.base_tree,
        )
    if not git_state.is_ancestor(root, base.base_sha, outcome.outcome_sha):
        return reject(
            "OUTCOME_NOT_DURABLE",
            "outcome commit does not descend from the base commit",
            base_sha=base.base_sha,
            outcome_sha=outcome.outcome_sha,
        )
    revert = git_state.find_revert(root, outcome.outcome_sha, branch=branch)
    if revert is not None:
        reverted_at = _parse_timestamp(str(revert.get("committed_at")))
        outcome_at = _parse_timestamp(
            git_state.run_git(root, "show", "-s", "--format=%cI", outcome.outcome_sha, check=False)
        )
        horizon = timedelta(days=stability_horizon_days)
        if reverted_at and outcome_at and (reverted_at - outcome_at) < horizon:
            return reject(
                "OUTCOME_NOT_DURABLE",
                "outcome was reverted before the stability horizon",
                revert=revert,
            )
        if not reverted_at or not outcome_at:
            return reject(
                "OUTCOME_NOT_DURABLE",
                "outcome was reverted and revert timing could not be established",
                revert=revert,
            )

    candidate = SourceCandidate(
        source_id=source_id,
        host=host,
        session_id=session_id,
        normalized_path=normalized_path,
        raw_dir=raw_dir,
        repo_root=root,
        cwd=cwd_resolution.cwd,
        branch=branch,
        started_at=started_at,
        ended_at=ended_at,
        base=base,
        outcome=outcome,
        events=events,
    )

    environment: ProjectEnvironment | None = None
    if environment_resolver is not None:
        try:
            environment = environment_resolver(candidate)
        except (OSError, RuntimeError, ValueError) as error:
            return reject("ENVIRONMENT_UNAVAILABLE", f"environment resolution failed: {error}")
    if environment is None and require_environment:
        return reject(
            "ENVIRONMENT_UNAVAILABLE",
            "no validated project environment for this repository state",
        )
    if environment is not None and environment.base_sha != candidate.base_sha:
        return reject(
            "ENVIRONMENT_UNAVAILABLE",
            "project environment was validated against a different base commit",
            environment_base_sha=environment.base_sha,
            base_sha=candidate.base_sha,
        )

    if environment is not None:
        candidate = replace(candidate, environment=environment)
    return candidate, None


def select_sources(
    *,
    layout: Layout,
    sessions: Sequence[tuple[Host, str]],
    branch: str = "HEAD",
    environment_resolver: EnvironmentResolver | None = None,
    require_environment: bool = True,
    stability_horizon_days: int = DEFAULT_STABILITY_HORIZON_DAYS,
) -> SelectionResult:
    selected: list[SourceCandidate] = []
    rejections: list[SourceRejection] = []
    for host, session_id in sessions:
        candidate, rejection = select_source(
            layout=layout,
            host=host,
            session_id=session_id,
            branch=branch,
            environment_resolver=environment_resolver,
            require_environment=require_environment,
            stability_horizon_days=stability_horizon_days,
        )
        if candidate is not None:
            selected.append(candidate)
        if rejection is not None:
            rejections.append(rejection)
    selected.sort(key=lambda item: item.source_id)
    rejections.sort(key=lambda item: (item.source_id, item.code))
    return SelectionResult(selected=selected, rejections=rejections)


def selection_path(layout: Layout, name: str) -> Path:
    return layout.benchmark_taskset_dir(require_safe_id(name, "taskset name")) / "selection.json"


def write_selection(layout: Layout, name: str, result: SelectionResult) -> Path:
    """Publish the selection report atomically; never mutate raw/ or normalized/."""
    path = selection_path(layout, name)
    atomic_write_text(path, json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n")
    return path


class SelectionError(RuntimeError):
    """Operator-facing selection input error (bad selector, missing contract)."""


ENVIRONMENT_CONTRACT_SCHEMA = "retro-project-environment-contract-v1"
_HOST_ALIASES: dict[str, Host] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "cc": "claude-code",
    "codex": "codex",
    "cx": "codex",
    "copilot": "vscode-copilot",
    "copilot_cli": "vscode-copilot",
    "gh-copilot": "vscode-copilot",
    "vscode": "vscode-copilot",
    "vscode_copilot": "vscode-copilot",
    "vscode-copilot": "vscode-copilot",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


def normalize_host(value: str) -> Host:
    try:
        return _HOST_ALIASES[value.strip().lower()]
    except KeyError as error:
        expected = ", ".join(HOSTS)
        raise SelectionError(f"unknown host {value!r}; expected one of {expected}") from error


def require_safe_id(value: str, label: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise SelectionError(f"{label} {value!r} contains unsupported characters")
    return value


def parse_session_selector(text: str, *, host: str | None = None) -> tuple[str, str]:
    """Parse one ``--session-file`` line: ``host/session_id`` or a bare session id."""
    value = text.strip()
    if not value:
        raise SelectionError("empty session selector")
    expected_host = normalize_host(host) if host is not None else None
    for separator in ("/", ":"):
        if separator in value:
            prefix, _, suffix = value.partition(separator)
            prefix = prefix.strip()
            suffix = suffix.strip()
            try:
                parsed_host = normalize_host(prefix)
            except SelectionError:
                continue
            if suffix:
                if expected_host is not None and parsed_host != expected_host:
                    raise SelectionError(
                        f"session selector {value!r} does not match --host {host!r}"
                    )
                return parsed_host, require_safe_id(suffix, "session id")
    if expected_host is None:
        raise SelectionError(
            f"session selector {value!r} has no host; use 'host/session_id' or pass --host"
        )
    return expected_host, require_safe_id(value, "session id")


def load_session_file(path: Path, *, host: str | None = None) -> list[tuple[str, str]]:
    """Read a ``--session-file``: one selector per line, ``#`` comments allowed."""
    file_path = Path(path)
    if not file_path.is_file():
        raise SelectionError(f"session file does not exist: {file_path}")
    sessions: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        try:
            selector = parse_session_selector(text, host=host)
        except SelectionError as error:
            raise SelectionError(f"{file_path}:{number}: {error}") from error
        if selector in seen:
            continue
        seen.add(selector)
        sessions.append(selector)
    if not sessions:
        raise SelectionError(f"session file has no selectors: {file_path}")
    return sessions


def discover_sessions(layout: Layout, *, host: str | None = None) -> list[tuple[str, str]]:
    """Every normalized session under ``layout``, optionally filtered by ``--host``."""
    hosts: tuple[Host, ...] = (normalize_host(host),) if host is not None else HOSTS
    found: list[tuple[str, str]] = []
    for name in hosts:
        directory = layout.root / "normalized" / name
        if not directory.is_dir():
            continue
        for entry in sorted(directory.glob("*.events.jsonl")):
            found.append((name, entry.name[: -len(".events.jsonl")]))
    return found


def resolve_sessions(
    *,
    layout: Layout,
    host: str | None = None,
    session_file: Path | None = None,
    sessions: Sequence[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """Apply ``--host`` / ``--session-file`` precedence to produce an explicit set."""
    if sessions is not None:
        resolved = [
            parse_session_selector(f"{item[0]}/{item[1]}", host=host) for item in sessions
        ]
    elif session_file is not None:
        resolved = load_session_file(Path(session_file), host=host)
    else:
        resolved = discover_sessions(layout, host=host)
    if not resolved:
        raise SelectionError("no sessions to select; pass --session-file or import a session")
    ordered: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for selector in resolved:
        if selector in seen:
            continue
        seen.add(selector)
        ordered.append(selector)
    ordered.sort()
    return ordered


@dataclass(frozen=True)
class EnvironmentContract:
    """Explicitly validated ``retro-project-environment-v1`` records, keyed by base."""

    path: Path
    environments: dict[str, ProjectEnvironment] = field(default_factory=dict)

    def for_base(self, base_sha: str) -> ProjectEnvironment | None:
        return self.environments.get(base_sha)

    def resolver(self) -> EnvironmentResolver:
        def resolve(candidate: SourceCandidate) -> ProjectEnvironment | None:
            return self.for_base(candidate.base_sha)

        return resolve

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ENVIRONMENT_CONTRACT_SCHEMA,
            "contract_path": str(self.path),
            "base_shas": sorted(self.environments),
            "environment_ids": sorted(
                environment.environment_id for environment in self.environments.values()
            ),
        }


def load_environment_contract(path: Path) -> EnvironmentContract:
    """Load explicitly validated project environments.

    Ambient developer setup is never treated as validated: every environment must
    be a ``retro-project-environment-v1`` record whose ``validated.base`` and
    ``validated.outcome`` are true, which :class:`ProjectEnvironment` enforces.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise SelectionError(f"environment contract does not exist: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SelectionError(f"{file_path}: invalid JSON: {error}") from error
    if isinstance(payload, dict) and payload.get("schema_version") == ENVIRONMENT_CONTRACT_SCHEMA:
        entries = payload.get("environments")
        if not isinstance(entries, list) or not entries:
            raise SelectionError(f"{file_path}: environments must be a non-empty list")
        records: list[Any] = list(entries)
    elif isinstance(payload, list):
        records = list(payload)
    elif isinstance(payload, dict):
        records = [payload]
    else:
        raise SelectionError(f"{file_path}: environment contract must be an object or list")
    environments: dict[str, ProjectEnvironment] = {}
    for index, record in enumerate(records):
        try:
            environment = ProjectEnvironment.from_dict(record, f"environment[{index}]")
        except SchemaError as error:
            raise SelectionError(f"{file_path}: {error}") from error
        if environment.base_sha in environments:
            raise SelectionError(
                f"{file_path}: duplicate environment for base {environment.base_sha}"
            )
        environments[environment.base_sha] = environment
    if not environments:
        raise SelectionError(f"{file_path}: environment contract is empty")
    return EnvironmentContract(path=file_path, environments=environments)


@dataclass(frozen=True)
class TasksetSelection:
    """Result of ``retro benchmark taskset select --name <name>``."""

    name: str
    result: SelectionResult
    sessions: list[tuple[str, str]] = field(default_factory=list)
    path: Path | None = None
    contract: EnvironmentContract | None = None

    @property
    def selected(self) -> list[SourceCandidate]:
        return list(self.result.selected)

    @property
    def rejections(self) -> list[SourceRejection]:
        return list(self.result.rejections)

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.result.to_dict())
        payload["name"] = self.name
        payload["path"] = str(self.path) if self.path is not None else None
        payload["sessions"] = [f"{host}/{session_id}" for host, session_id in self.sessions]
        return payload


def select_taskset(
    *,
    layout: Layout,
    name: str,
    host: str | None = None,
    session_file: Path | None = None,
    sessions: Sequence[tuple[str, str]] | None = None,
    environment_file: Path | None = None,
    environment_resolver: EnvironmentResolver | None = None,
    require_environment: bool = True,
    branch: str = "HEAD",
    stability_horizon_days: int = DEFAULT_STABILITY_HORIZON_DAYS,
    write: bool = True,
) -> TasksetSelection:
    """Select sessions into ``benchmarks/<name>/task-scorer/selection.json``.

    ``host``/``session_file`` mirror the spec CLI flags. Environment validation is
    never inferred from ambient setup: pass ``environment_file`` with an explicit
    validated contract, or pass ``require_environment=False`` to record an
    unvalidated selection (which cannot generate adjacent tasks).
    """
    require_safe_id(name, "taskset name")
    selectors = resolve_sessions(
        layout=layout, host=host, session_file=session_file, sessions=sessions
    )
    contract: EnvironmentContract | None = None
    resolver: EnvironmentResolver | None = None
    if environment_file is not None and environment_resolver is not None:
        raise SelectionError("pass either environment_file or environment_resolver, not both")
    if environment_file is not None:
        contract = load_environment_contract(Path(environment_file))
        resolver = contract.resolver()
    elif environment_resolver is not None:
        resolver = environment_resolver
    elif require_environment:
        raise SelectionError(
            "no environment contract supplied: pass an explicit validated "
            "retro-project-environment-v1 contract (--environment-file), or set "
            "require_environment=False to record an unvalidated selection. Ambient "
            "developer setup is never recorded as validated."
        )
    result = select_sources(
        layout=layout,
        sessions=[(item[0], item[1]) for item in selectors],  # type: ignore[misc]
        branch=branch,
        environment_resolver=resolver,
        require_environment=require_environment,
        stability_horizon_days=stability_horizon_days,
    )
    environment_provenance: dict[str, Any] = {
        "required": require_environment,
        "contract": contract.to_dict() if contract is not None else None,
        "resolver": (
            "contract"
            if contract is not None
            else "automatic"
            if environment_resolver is not None
            else None
        ),
        "validated": bool(
            require_environment
            and resolver is not None
            and all(candidate.environment is not None for candidate in result.selected)
        ),
    }
    result = replace(result, environment=environment_provenance)
    path = write_selection(layout, name, result) if write else None
    return TasksetSelection(
        name=name,
        result=result,
        sessions=list(selectors),
        path=path,
        contract=contract,
    )


def load_selection(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SELECTION_SCHEMA:
        raise SchemaError(
            f"selection schema_version must be {SELECTION_SCHEMA!r}, "
            f"got {payload.get('schema_version')!r}"
        )
    return payload


__all__ = [
    "DEFAULT_STABILITY_HORIZON_DAYS",
    "ENVIRONMENT_CONTRACT_SCHEMA",
    "SELECTION_SCHEMA",
    "EnvironmentContract",
    "EnvironmentResolver",
    "SelectionError",
    "SelectionResult",
    "SourceCandidate",
    "SourceRejection",
    "TasksetSelection",
    "discover_sessions",
    "load_environment_contract",
    "load_selection",
    "load_session_file",
    "normalize_host",
    "parse_session_selector",
    "require_safe_id",
    "resolve_sessions",
    "select_source",
    "select_sources",
    "select_taskset",
    "selection_path",
    "source_id_for",
    "write_selection",
]
