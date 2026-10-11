"""Commit-mined task sources from the user's own Git history.

Session-derived sources need a clean-start proof that most historical rollouts
never captured, so ~96% of them are rejected with ``NO_EXACT_BASE_SHA``. A commit
has that proof by construction: its first parent *is* the pre-change state. This
module mines non-merge commits from repositories Retro already saw in the
catalog and turns the eligible ones into :class:`~.selection.SourceCandidate`
objects that flow through the existing bundle → build stages.

The user's repositories are read-only here: only ``git rev-list``, ``git show``,
``git diff`` and ``git cat-file`` plumbing runs, never a checkout, fetch or any
command that writes to the working tree.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ...catalog import Catalog, load_catalog
from ...schema import NormalizedEvent, read_events
from ...storage import Layout
from ...utils import event_file_paths, event_text
from . import git_state, sampling
from .selection import SourceCandidate, SourceRejection

COMMIT_SOURCE_KIND = "commit"
SESSION_SOURCE_KIND = "session"
COMMIT_HOST = "git"
COMMIT_PROVENANCE_SCHEMA = "retro-commit-source-provenance-v1"

DEFAULT_WINDOW_DAYS = 180
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_LINES = 2000
DEFAULT_SCAN_LIMIT = 200
DEFAULT_LINK_WINDOW_HOURS = 6.0

_RECORD_SEPARATOR = "\x1e"
_FIELD_SEPARATOR = "\x1f"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_TEST_NAME_RE = re.compile(
    r"(?:^test_|_test\.|\.test\.|\.spec\.|_spec\.|^conftest\.py$|Test[A-Za-z0-9]*\.(?:java|kt|cs)$)"
)
_TEST_DIR_PARTS = frozenset(
    {"test", "tests", "spec", "specs", "__tests__", "testing", "e2e", "integration_tests"}
)
_DOC_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc", ".mdx"})
_DOC_DIR_PARTS = frozenset({"docs", "doc", "documentation", "website", "man"})
_LOCKFILES = frozenset(
    {
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "requirements.lock",
        "uv.lock",
        "yarn.lock",
    }
)
_VENDOR_DIR_PARTS = frozenset(
    {
        ".venv",
        "bower_components",
        "build",
        "dist",
        "generated",
        "node_modules",
        "site-packages",
        "third_party",
        "vendor",
    }
)


def commit_work_kind(subject: str, files: Mapping[str, str] | None = None) -> str:
    """Map a commit subject (and its changed paths) onto a signal work kind."""
    text = subject.strip().lower()
    head = text.split(":", 1)[0] if ":" in text[:24] else text
    rules: tuple[tuple[tuple[str, ...], str], ...] = (
        (("fix", "bug", "hotfix", "patch", "repair", "revert"), "debug"),
        (("refactor", "cleanup", "clean up", "rename", "simplify", "tidy"), "refactor"),
        (("test", "tests", "spec", "coverage"), "test"),
        (("docs", "doc", "readme", "changelog"), "docs"),
        (("ci", "chore", "build", "deps", "bump", "release", "infra"), "ops"),
        (("feat", "add", "implement", "support", "introduce", "perf"), "build"),
    )
    for keywords, kind in rules:
        if head in keywords or any(text.startswith(f"{keyword} ") for keyword in keywords):
            return kind
    kinds = set((files or {}).values())
    if kinds and kinds <= {"test"}:
        return "test"
    if kinds and kinds <= {"docs"}:
        return "docs"
    return "build"


class CommitSourceError(RuntimeError):
    """Operator-facing commit mining input error."""


@dataclass(frozen=True)
class CommitLimits:
    """Enumeration window and size gates; every value is operator-configurable."""

    window_days: int = DEFAULT_WINDOW_DAYS
    max_files: int = DEFAULT_MAX_FILES
    max_lines: int = DEFAULT_MAX_LINES
    scan_limit: int = DEFAULT_SCAN_LIMIT
    link_window_hours: float = DEFAULT_LINK_WINDOW_HOURS
    include_merges: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.window_days,
            "max_files": self.max_files,
            "max_lines": self.max_lines,
            "scan_limit": self.scan_limit,
            "link_window_hours": self.link_window_hours,
            "include_merges": self.include_merges,
        }


def classify_path(path: str) -> str:
    """Bucket one changed path: vendored, lockfile, test, docs or source."""
    posix = PurePosixPath(path)
    parts = set(posix.parts[:-1])
    if parts & _VENDOR_DIR_PARTS:
        return "vendored"
    if posix.name in _LOCKFILES:
        return "lockfile"
    if parts & _TEST_DIR_PARTS or _TEST_NAME_RE.search(posix.name):
        return "test"
    if posix.suffix.lower() in _DOC_SUFFIXES or parts & _DOC_DIR_PARTS:
        return "docs"
    return "source"


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


def repo_slug(root: Path) -> str:
    """Short, path-safe repository label used inside commit source IDs."""
    remote = git_state.canonical_remote(root)
    if remote:
        parts = [part for part in re.split(r"[/:]", remote) if part]
        material = "_".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    else:
        material = root.name or "repo"
    slug = _SLUG_RE.sub("-", material).strip("-._").lower()
    return slug or "repo"


def commit_source_id(root: Path, sha: str) -> str:
    return f"commit__{repo_slug(root)}__{sha[:12]}"


@dataclass(frozen=True)
class CommitFacts:
    """Everything read from Git for one commit, before eligibility gates."""

    sha: str
    parents: tuple[str, ...]
    committed_at: str
    authored_at: str
    author: str
    subject: str
    message: str
    files: dict[str, str] = field(default_factory=dict)
    insertions: int = 0
    deletions: int = 0
    binary_files: tuple[str, ...] = ()

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def files_changed(self) -> int:
        return len(self.files)

    @property
    def lines_changed(self) -> int:
        return self.insertions + self.deletions

    def paths_by_kind(self, kind: str) -> list[str]:
        return sorted(path for path, value in self.files.items() if value == kind)

    @property
    def touches_tests(self) -> bool:
        return any(value == "test" for value in self.files.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "parent_sha": self.parents[0] if self.parents else None,
            "parents": list(self.parents),
            "committed_at": self.committed_at,
            "authored_at": self.authored_at,
            "author": self.author,
            "subject": self.subject,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "lines_changed": self.lines_changed,
            "touches_tests": self.touches_tests,
            "test_files": self.paths_by_kind("test"),
            "source_files": self.paths_by_kind("source"),
            "docs_files": self.paths_by_kind("docs"),
            "vendored_files": self.paths_by_kind("vendored"),
            "lockfiles": self.paths_by_kind("lockfile"),
            "binary_files": list(self.binary_files),
        }


def is_revert_commit(commit: CommitFacts) -> bool:
    """True for commits that undo earlier work rather than adding new work."""
    subject = commit.subject.strip().lower()
    return subject.startswith("revert ") or "this reverts commit" in commit.message.lower()


def read_commit(root: Path, sha: str) -> CommitFacts | None:
    """Read commit metadata and its first-parent diff with Git plumbing only."""
    fmt = _FIELD_SEPARATOR.join(["%H", "%P", "%cI", "%aI", "%an", "%s", "%B"])
    raw = git_state.run_git(root, "show", "-s", f"--format={fmt}{_RECORD_SEPARATOR}", sha, check=False)
    record = raw.split(_RECORD_SEPARATOR)[0]
    parts = record.split(_FIELD_SEPARATOR)
    if len(parts) < 7 or not parts[0].strip():
        return None
    resolved, parents_text, committed_at, authored_at, author, subject, message = parts[:7]
    parents = tuple(item for item in parents_text.split() if item)
    files: dict[str, str] = {}
    insertions = 0
    deletions = 0
    binary: list[str] = []
    if parents:
        numstat = git_state.run_git(
            root, "diff", "--numstat", "--no-renames", parents[0], resolved, check=False
        )
    else:
        numstat = git_state.run_git(
            root, "show", "--numstat", "--no-renames", "--format=", resolved, check=False
        )
    for line in numstat.splitlines():
        columns = line.split("\t")
        if len(columns) < 3:
            continue
        added, removed, path = columns[0], columns[1], columns[2]
        files[path] = classify_path(path)
        if added.isdigit() and removed.isdigit():
            insertions += int(added)
            deletions += int(removed)
        else:
            binary.append(path)
    return CommitFacts(
        sha=resolved.strip(),
        parents=parents,
        committed_at=committed_at.strip(),
        authored_at=authored_at.strip(),
        author=author.strip(),
        subject=subject.strip(),
        message=message.strip(),
        files=files,
        insertions=insertions,
        deletions=deletions,
        binary_files=tuple(sorted(binary)),
    )


def enumerate_commits(
    root: Path,
    *,
    branch: str = "HEAD",
    limits: CommitLimits | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Newest-first commit SHAs on *branch* inside the enumeration window."""
    bounds = limits or CommitLimits()
    reference = now or datetime.now(timezone.utc)
    start = since or (reference - timedelta(days=bounds.window_days))
    args = ["rev-list", f"--max-count={max(1, bounds.scan_limit)}", f"--since={start.isoformat()}"]
    if until is not None:
        args.append(f"--until={until.isoformat()}")
    args.append(branch)
    output = git_state.run_git(root, *args, check=False)
    return [line.strip() for line in output.splitlines() if line.strip()]


def discover_repos(catalog: Catalog) -> list[Path]:
    """Git top levels of every included catalog session, deduplicated."""
    roots: dict[str, Path] = {}
    for record in catalog.values():
        if record.excluded:
            continue
        for value in (record.git.get("root"), record.cwd_resolved, record.cwd):
            if not isinstance(value, str) or not value.strip():
                continue
            path = Path(value).expanduser()
            if not path.exists():
                continue
            root = git_state.repo_root(path)
            if root is None:
                continue
            resolved = root.resolve()
            roots.setdefault(str(resolved), resolved)
            break
    return [roots[key] for key in sorted(roots)]


def _relative_paths(values: Iterable[str], root: Path) -> set[str]:
    relative: set[str] = set()
    root_text = str(root.resolve())
    for value in values:
        if not value:
            continue
        text = value.strip()
        if text.startswith(root_text):
            text = text[len(root_text) :].lstrip("/")
        if text.startswith("./"):
            text = text[2:]
        relative.add(PurePosixPath(text).as_posix())
    return {item for item in relative if item and item != "."}


def _session_edited_paths(events: Sequence[NormalizedEvent], root: Path) -> set[str]:
    paths: set[str] = set()
    for event in events:
        if event.event_type != "file_edit":
            continue
        paths |= event_file_paths(event)
    return _relative_paths(paths, root)


def _first_user_message(events: Sequence[NormalizedEvent]) -> tuple[str | None, str | None]:
    for event in events:
        if event.event_type == "message" and event.actor == "user":
            text = event_text(event).strip()
            if text:
                return text, event.event_id
    return None, None


def _record_repo_paths(record: Any) -> list[str]:
    values = [record.git.get("root"), record.cwd_resolved, record.cwd]
    return [value for value in values if isinstance(value, str) and value.strip()]


def _record_in_repo(record: Any, root: Path, remote: str | None) -> bool:
    resolved_root = root.resolve()
    for value in _record_repo_paths(record):
        try:
            candidate = Path(value).expanduser().resolve()
        except OSError:
            continue
        if candidate == resolved_root or resolved_root in candidate.parents:
            return True
    record_remote = record.git.get("remote_url") or record.git.get("remote")
    if not remote or not isinstance(record_remote, str) or not record_remote:
        return False
    return remote.endswith(record_remote)


@dataclass(frozen=True)
class SessionLink:
    """Evidence that a captured session produced a mined commit."""

    session_key: str
    host: str
    session_id: str
    normalized_path: Path
    started_at: str | None
    ended_at: str | None
    matched_files: tuple[str, ...]
    gap_minutes: float | None
    first_prompt: str | None
    first_prompt_event_id: str | None
    events: tuple[NormalizedEvent, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "host": self.host,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "matched_files": list(self.matched_files),
            "gap_minutes": self.gap_minutes,
            "first_prompt_event_id": self.first_prompt_event_id,
        }


def link_session(
    *,
    layout: Layout,
    root: Path,
    commit: CommitFacts,
    catalog: Catalog,
    limits: CommitLimits | None = None,
) -> SessionLink | None:
    """Link the captured session that most likely produced *commit*.

    A session qualifies when it ran in the same repository, its window contains
    the commit (or ends at most ``link_window_hours`` before it), and its edited
    files intersect the commit's changed files. The strongest file overlap wins.
    """
    bounds = limits or CommitLimits()
    commit_at = _parse_timestamp(commit.committed_at) or _parse_timestamp(commit.authored_at)
    if commit_at is None:
        return None
    remote = git_state.canonical_remote(root)
    window = timedelta(hours=bounds.link_window_hours)
    changed = set(commit.files)
    best: tuple[int, float, str] | None = None
    best_link: SessionLink | None = None
    for record in catalog.values():
        if record.excluded or not _record_in_repo(record, root, remote):
            continue
        started = _parse_timestamp(record.started_at)
        ended = _parse_timestamp(record.ended_at) or started
        if started is None or ended is None:
            continue
        if started <= commit_at <= ended:
            gap = 0.0
        elif timedelta(0) <= (commit_at - ended) <= window:
            gap = (commit_at - ended).total_seconds() / 60.0
        else:
            continue
        normalized_path = layout.normalized_path(record.host, record.session_id)  # type: ignore[arg-type]
        if not normalized_path.is_file():
            continue
        try:
            events = list(read_events(normalized_path))
        except (OSError, ValueError, TypeError):
            continue
        edited = _session_edited_paths(events, root)
        matched = sorted(edited & changed)
        if not matched:
            edited_names = {PurePosixPath(item).name for item in edited}
            matched = sorted(
                path for path in changed if PurePosixPath(path).name in edited_names
            )
        if not matched:
            continue
        rank = (-len(matched), gap, record.key)
        if best is not None and rank >= best:
            continue
        prompt, prompt_event_id = _first_user_message(events)
        best = rank
        best_link = SessionLink(
            session_key=record.key,
            host=record.host,
            session_id=record.session_id,
            normalized_path=normalized_path,
            started_at=record.started_at,
            ended_at=record.ended_at,
            matched_files=tuple(matched),
            gap_minutes=round(gap, 3),
            first_prompt=prompt or record.first_prompt,
            first_prompt_event_id=prompt_event_id,
            events=tuple(events),
        )
    return best_link


def commit_provenance(
    commit: CommitFacts,
    *,
    repo_root: Path,
    link: SessionLink | None,
    test_command_source: str | None = None,
) -> dict[str, Any]:
    """Prompt provenance for the TaskDefiner plus the linkage evidence."""
    if link is not None and link.first_prompt:
        prompt_provenance = {
            "mode": "linked_session",
            "prompt": link.first_prompt,
            "session_key": link.session_key,
            "event_id": link.first_prompt_event_id,
        }
    else:
        prompt_provenance = {
            "mode": "commit_message",
            "prompt": commit.message or commit.subject,
            "session_key": link.session_key if link is not None else None,
            "event_id": None,
        }
    return {
        "schema_version": COMMIT_PROVENANCE_SCHEMA,
        "source_kind": COMMIT_SOURCE_KIND,
        "repo_root": str(repo_root),
        "repo_id": git_state.repo_identity(repo_root),
        "remote": git_state.canonical_remote(repo_root),
        "work_kind": commit_work_kind(commit.subject, commit.files),
        "commit": commit.to_dict(),
        "prompt_provenance": prompt_provenance,
        "linkage": link.to_dict() if link is not None else None,
        "touches_tests": commit.touches_tests,
        "test_command_source": test_command_source,
    }


def _reject(
    root: Path, sha: str, code: str, detail: str, **evidence: Any
) -> SourceRejection:
    return SourceRejection(
        source_id=commit_source_id(root, sha),
        host=COMMIT_HOST,
        session_id=sha,
        code=code,
        detail=detail,
        evidence={"repo_root": str(root), "commit": sha, **evidence},
    )


def select_commit_source(
    *,
    layout: Layout,
    root: Path,
    sha: str,
    branch: str = "HEAD",
    limits: CommitLimits | None = None,
    stability_horizon_days: int = 7,
    catalog: Catalog | None = None,
    link_sessions: bool = True,
    signals: Any = None,
) -> tuple[SourceCandidate | None, SourceRejection | None]:
    """Prove one commit's eligibility as a task source; never mutate the repo."""
    bounds = limits or CommitLimits()
    try:
        commit = read_commit(root, sha)
    except git_state.GitError as error:
        return None, _reject(root, sha, "HARNESS_ERROR", f"git failure reading commit: {error}")
    if commit is None:
        return None, _reject(root, sha, "HARNESS_ERROR", "commit could not be read")
    if commit.is_merge and not bounds.include_merges:
        return None, _reject(
            root, sha, "COMMIT_MERGE_COMMIT", "merge commits are excluded", parents=list(commit.parents)
        )
    if not commit.parents:
        return None, _reject(
            root, sha, "COMMIT_NO_SOURCE_CHANGE", "root commit has no parent state"
        )
    if is_revert_commit(commit):
        return None, _reject(
            root,
            sha,
            "COMMIT_REVERT_COMMIT",
            "revert commits undo earlier work rather than adding new work",
        )
    source_files = commit.paths_by_kind("source")
    if not source_files:
        return None, _reject(
            root,
            sha,
            "COMMIT_NO_SOURCE_CHANGE",
            "commit changes no non-test source file",
            files=sorted(commit.files),
        )
    if commit.files_changed > bounds.max_files or commit.lines_changed > bounds.max_lines:
        return None, _reject(
            root,
            sha,
            "COMMIT_TOO_LARGE",
            (
                f"commit changes {commit.files_changed} file(s) and "
                f"{commit.lines_changed} line(s)"
            ),
            max_files=bounds.max_files,
            max_lines=bounds.max_lines,
        )

    parent = commit.parents[0]
    try:
        base_tree = git_state.commit_tree(root, parent)
        outcome_tree = git_state.commit_tree(root, commit.sha)
    except git_state.GitError as error:
        return None, _reject(root, sha, "HARNESS_ERROR", f"git failure reading trees: {error}")
    if base_tree == outcome_tree:
        return None, _reject(
            root, sha, "COMMIT_NO_SOURCE_CHANGE", "commit tree is identical to its parent"
        )

    revert = git_state.find_revert(root, commit.sha, branch=branch)
    if revert is not None:
        reverted_at = _parse_timestamp(str(revert.get("committed_at")))
        committed_at = _parse_timestamp(commit.committed_at)
        horizon = timedelta(days=stability_horizon_days)
        if not reverted_at or not committed_at or (reverted_at - committed_at) < horizon:
            return None, _reject(
                root,
                sha,
                "OUTCOME_NOT_DURABLE",
                "commit was reverted before the stability horizon",
                revert=revert,
            )

    resolved_catalog = catalog if catalog is not None else load_catalog(layout)
    link = (
        link_session(layout=layout, root=root, commit=commit, catalog=resolved_catalog, limits=bounds)
        if link_sessions
        else None
    )
    provenance = commit_provenance(commit, repo_root=root, link=link)
    session_key = link.session_key if link is not None else None
    labels = sampling.commit_outcome_labels(
        repo_root=root,
        outcome_sha=commit.sha,
        branch=branch,
        stability_horizon_days=stability_horizon_days,
        linked_session_key=session_key,
        signal_values=signals.values(session_key) if signals is not None and session_key else None,
        signal_evidence=(
            signals.evidence(session_key) if signals is not None and session_key else None
        ),
        events=tuple(link.events) if link is not None else (),
    )

    base = git_state.BaseResolution(
        base_sha=parent,
        base_tree=base_tree,
        resolution="commit_parent",
        state_confidence="exact_clean_commit",
        evidence={
            "rule": "first_parent",
            "commit": commit.sha,
            "parent": parent,
            "committed_at": commit.committed_at,
        },
    )
    outcome = git_state.OutcomeResolution(
        outcome_sha=commit.sha,
        outcome_tree=outcome_tree,
        resolution="mined_commit",
        evidence={
            "rule": "mined_commit",
            "subject": commit.subject,
            "committed_at": commit.committed_at,
            "files_changed": commit.files_changed,
            "lines_changed": commit.lines_changed,
            "touches_tests": commit.touches_tests,
        },
    )
    candidate = SourceCandidate(
        source_id=commit_source_id(root, commit.sha),
        host=COMMIT_HOST,
        session_id=commit.sha,
        normalized_path=link.normalized_path if link is not None else None,
        raw_dir=root,
        repo_root=root,
        cwd=str(root),
        branch=branch,
        started_at=(link.started_at if link is not None else None) or commit.authored_at,
        ended_at=commit.committed_at,
        base=base,
        outcome=outcome,
        events=tuple(link.events) if link is not None else (),
        kind=COMMIT_SOURCE_KIND,
        provenance=provenance,
        outcome_labels=labels.to_dict(),
    )
    return candidate, None


def select_commit_sources(
    *,
    layout: Layout,
    repos: Sequence[Path] | None = None,
    branch: str = "HEAD",
    limits: CommitLimits | None = None,
    stability_horizon_days: int = 7,
    catalog: Catalog | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
    link_sessions: bool = True,
) -> tuple[list[SourceCandidate], list[SourceRejection]]:
    """Mine every eligible commit from *repos* (default: catalog repositories)."""
    from ...signals.store import load_signal_store

    bounds = limits or CommitLimits()
    resolved_catalog = catalog if catalog is not None else load_catalog(layout)
    try:
        signals: Any = load_signal_store(layout)
    except (OSError, ValueError):
        signals = None
    roots: list[Path] = []
    for value in repos if repos is not None else discover_repos(resolved_catalog):
        root = git_state.repo_root(Path(value).expanduser())
        if root is None:
            raise CommitSourceError(f"not a Git repository: {value}")
        if root not in roots:
            roots.append(root)
    selected: list[SourceCandidate] = []
    rejections: list[SourceRejection] = []
    for root in roots:
        for sha in enumerate_commits(
            root, branch=branch, limits=bounds, since=since, until=until, now=now
        ):
            candidate, rejection = select_commit_source(
                layout=layout,
                root=root,
                sha=sha,
                branch=branch,
                limits=bounds,
                stability_horizon_days=stability_horizon_days,
                catalog=resolved_catalog,
                link_sessions=link_sessions,
                signals=signals,
            )
            if candidate is not None:
                selected.append(candidate)
            if rejection is not None:
                rejections.append(rejection)
    return selected, rejections


def limits_from_dict(payload: Mapping[str, Any] | None) -> CommitLimits:
    """Rebuild the enumeration gates a selection was recorded with."""
    values = dict(payload or {})
    defaults = CommitLimits()
    return CommitLimits(
        window_days=int(values.get("window_days", defaults.window_days)),
        max_files=int(values.get("max_files", defaults.max_files)),
        max_lines=int(values.get("max_lines", defaults.max_lines)),
        scan_limit=int(values.get("scan_limit", defaults.scan_limit)),
        link_window_hours=float(values.get("link_window_hours", defaults.link_window_hours)),
        include_merges=bool(values.get("include_merges", defaults.include_merges)),
    )


def reselect_commit_entry(
    *,
    layout: Layout,
    entry: Mapping[str, Any],
    branch: str = "HEAD",
    limits: CommitLimits | None = None,
    stability_horizon_days: int = 7,
    catalog: Catalog | None = None,
) -> tuple[SourceCandidate | None, str | None, str]:
    """Re-prove one recorded commit selection entry before bundling it."""
    provenance = entry.get("provenance")
    repo_root = entry.get("repo_root")
    sha = entry.get("session_id")
    if isinstance(provenance, Mapping):
        repo_root = provenance.get("repo_root", repo_root)
        commit = provenance.get("commit")
        if isinstance(commit, Mapping):
            sha = commit.get("sha", sha)
    if not isinstance(repo_root, str) or not isinstance(sha, str):
        return None, "HARNESS_ERROR", "commit selection entry has no repository or commit"
    root = git_state.repo_root(Path(repo_root).expanduser())
    if root is None:
        return None, "NOT_GIT_REPOSITORY", f"repository is no longer available: {repo_root}"
    candidate, rejection = select_commit_source(
        layout=layout,
        root=root,
        sha=sha,
        branch=str(entry.get("branch") or branch),
        limits=limits,
        stability_horizon_days=stability_horizon_days,
        catalog=catalog,
    )
    if candidate is None:
        code = rejection.code if rejection is not None else "HARNESS_ERROR"
        detail = rejection.detail if rejection is not None else "commit is no longer selectable"
        return None, code, detail
    return candidate, None, ""


__all__ = [
    "COMMIT_HOST",
    "COMMIT_PROVENANCE_SCHEMA",
    "COMMIT_SOURCE_KIND",
    "DEFAULT_LINK_WINDOW_HOURS",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_LINES",
    "DEFAULT_SCAN_LIMIT",
    "DEFAULT_WINDOW_DAYS",
    "SESSION_SOURCE_KIND",
    "CommitFacts",
    "CommitLimits",
    "CommitSourceError",
    "SessionLink",
    "classify_path",
    "commit_provenance",
    "commit_source_id",
    "discover_repos",
    "enumerate_commits",
    "is_revert_commit",
    "limits_from_dict",
    "link_session",
    "read_commit",
    "repo_slug",
    "reselect_commit_entry",
    "select_commit_source",
    "select_commit_sources",
]
