"""Deterministic, immutable ``SourceBundle`` construction.

A bundle is the only input the TaskDefiner and ScorerBuilder ever see. Identical
inputs must produce an identical ``content_sha256`` so that unchanged build
stages can be reused instead of re-run.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ...renderer import render_markdown
from ...schema import read_events
from ...storage import Layout
from ...utils import atomic_write_text
from . import git_state
from .schema import (
    ProjectEnvironment,
    SchemaError,
    SourceBundleManifest,
    TaskLimits,
    canonical_json,
    require_sha256_hex,
)
from .selection import (
    DEFAULT_STABILITY_HORIZON_DAYS,
    SourceCandidate,
    load_environment_contract,
    load_selection,
    select_source,
    select_taskset,
    selection_path,
)

BUNDLE_REPORT_SCHEMA = "retro-taskset-bundles-v1"
BUNDLE_INPUT_SCHEMA = "retro-source-bundle-input-v1"
MANIFEST_NAME = "manifest.json"
SELECTION_RECORD_NAME = "selection.json"
BUNDLE_LAYOUT = (
    "manifest.json",
    "selection.json",
    "rollout/events.jsonl",
    "rollout/transcript.md",
    "repo/base",
    "repo/outcome",
    "repo/change.patch",
    "repo/git-log.jsonl",
    "context/environment.json",
    "context/project-files.json",
    "context/test-commands.json",
)

PROJECT_FILE_NAMES = (
    "AGENTS.md",
    "CODEOWNERS",
    "Cargo.lock",
    "Cargo.toml",
    "Dockerfile",
    "Gemfile",
    "Gemfile.lock",
    "Makefile",
    "package-lock.json",
    "package.json",
    "poetry.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "ruff.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "yarn.lock",
    "go.mod",
    "go.sum",
    "mypy.ini",
    "pytest.ini",
    ".editorconfig",
    ".pre-commit-config.yaml",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".dockerignore",
)
PROJECT_FILE_PREFIXES = (
    "README",
    "CONTRIBUTING",
    "CLAUDE",
    "COPILOT",
    ".eslintrc",
    ".prettierrc",
    ".flake8",
)
PROJECT_FILE_DIRS = (
    ".github/workflows",
    ".devcontainer",
)
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".java": "java",
    ".kt": "kotlin",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".sql": "sql",
    ".md": "markdown",
}


class BundleError(RuntimeError):
    """Bundle construction failed."""


@dataclass(frozen=True)
class SourceBundle:
    source_id: str
    path: Path
    manifest: SourceBundleManifest

    @property
    def content_sha256(self) -> str:
        assert self.manifest.content_sha256 is not None
        return self.manifest.content_sha256


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_executable_mode(mode: int) -> str:
    return "0755" if mode & 0o111 else "0644"


def _regular_file_digest(path: Path, *, content_sha256: str | None = None) -> dict[str, str]:
    return {
        "type": "file",
        "mode": _normalized_executable_mode(path.lstat().st_mode),
        "content_sha256": content_sha256 or file_sha256(path),
    }


def directory_digest(
    root: Path, *, exclude: tuple[str, ...] = ()
) -> dict[str, dict[str, str]]:
    """Describe every filesystem entry below *root* for deterministic hashing."""
    digests: dict[str, dict[str, str]] = {}
    excluded = set(exclude)

    def visit(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = path.relative_to(root).as_posix()
            if relative in excluded:
                continue
            metadata = path.lstat()
            mode = _normalized_executable_mode(metadata.st_mode)
            if stat.S_ISLNK(metadata.st_mode):
                digests[relative] = {
                    "type": "symlink",
                    "mode": mode,
                    "target": os.readlink(path),
                }
            elif stat.S_ISDIR(metadata.st_mode):
                digests[relative] = {"type": "directory", "mode": mode}
                visit(path)
            elif stat.S_ISREG(metadata.st_mode):
                digests[relative] = _regular_file_digest(path)
            else:
                raise BundleError(f"unsupported filesystem entry in bundle: {path}")

    visit(root)
    return digests


def _manifest_input(manifest: SourceBundleManifest) -> dict[str, Any]:
    payload = manifest.to_dict()
    payload.pop("content_sha256", None)
    return payload


def _source_manifest(
    candidate: SourceCandidate,
    task_limits: TaskLimits,
    rollout_events_sha256: str,
) -> SourceBundleManifest:
    return SourceBundleManifest(
        source_id=candidate.source_id,
        host=candidate.host,
        session_id=candidate.session_id,
        started_at=candidate.started_at,
        ended_at=candidate.ended_at,
        rollout_events_sha256=rollout_events_sha256,
        repo=candidate.repo_anchor(),
        task_limits=task_limits,
    )


def _input_fingerprint(
    manifest: SourceBundleManifest,
    *,
    selection: Mapping[str, Any],
    environment: Mapping[str, Any] | None,
) -> str:
    payload = {
        "schema_version": BUNDLE_INPUT_SCHEMA,
        "manifest": _manifest_input(manifest),
        "selection": dict(selection),
        "environment": dict(environment) if environment is not None else None,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _current_input_fingerprint(
    candidate: SourceCandidate,
    task_limits: TaskLimits,
) -> str:
    manifest = _source_manifest(
        candidate,
        task_limits,
        file_sha256(candidate.normalized_path),
    )
    environment = candidate.environment.to_dict() if candidate.environment is not None else None
    return _input_fingerprint(
        manifest,
        selection=candidate.selection_record(),
        environment=environment,
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BundleError(f"invalid {label} at {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BundleError(f"{label} at {path} must be an object")
    return payload


def _stored_input_fingerprint(path: Path, manifest: SourceBundleManifest) -> str:
    environment_path = path / "context" / "environment.json"
    environment = (
        _read_json_object(environment_path, "bundle environment")
        if environment_path.is_file()
        else None
    )
    return _input_fingerprint(
        manifest,
        selection=_read_json_object(path / SELECTION_RECORD_NAME, "bundle selection"),
        environment=environment,
    )


def _publish_bundle(staging: Path, target: Path) -> None:
    previous: Path | None = None
    if target.exists():
        previous = staging.with_name(f"{staging.name}.previous")
        os.replace(target, previous)
    try:
        os.replace(staging, target)
    except Exception as publish_error:
        if previous is not None:
            try:
                os.replace(previous, target)
            except OSError as rollback_error:
                raise BundleError(
                    f"failed to publish {target} and roll back its prior bundle; "
                    f"the prior bundle remains at {previous}"
                ) from rollback_error
        raise publish_error
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)


def compute_content_hash(bundle_dir: Path) -> str:
    """Content hash over bundle data, entry semantics, and unhashed manifest fields."""
    digests = directory_digest(bundle_dir, exclude=(MANIFEST_NAME,))
    manifest_path = bundle_dir / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise BundleError(f"bundle manifest must be a regular file: {manifest_path}")
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise BundleError(f"invalid bundle manifest at {manifest_path}: {error}") from error
        if not isinstance(manifest, dict):
            raise BundleError(f"bundle manifest at {manifest_path} must be an object")
        manifest.pop("content_sha256", None)
        digests[MANIFEST_NAME] = _regular_file_digest(
            manifest_path,
            content_sha256=hashlib.sha256(
                canonical_json(manifest).encode("utf-8")
            ).hexdigest(),
        )
    return hashlib.sha256(canonical_json(digests).encode("utf-8")).hexdigest()


def _tracked_files(root: Path, sha: str) -> list[str]:
    output = git_state.run_git(root, "ls-tree", "-r", "--name-only", sha)
    return sorted(line.strip() for line in output.splitlines() if line.strip())


def _is_project_file(path: str) -> bool:
    posix = PurePosixPath(path)
    name = posix.name
    if name in PROJECT_FILE_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in PROJECT_FILE_PREFIXES):
        return True
    parent = posix.parent.as_posix()
    return any(parent == directory or parent.startswith(directory + "/") for directory in PROJECT_FILE_DIRS)


def build_project_files(
    repo_root: Path,
    base_sha: str,
    base_checkout: Path,
) -> dict[str, Any]:
    """Deterministic project context derived only from the base commit."""
    tracked = _tracked_files(repo_root, base_sha)
    files: list[dict[str, Any]] = []
    for relative in tracked:
        if not _is_project_file(relative):
            continue
        candidate = base_checkout / relative
        if not candidate.is_file():
            continue
        files.append(
            {
                "path": relative,
                "sha256": file_sha256(candidate),
                "bytes": candidate.stat().st_size,
            }
        )

    languages: dict[str, int] = {}
    for relative in tracked:
        language = LANGUAGE_BY_SUFFIX.get(PurePosixPath(relative).suffix.lower())
        if language:
            languages[language] = languages.get(language, 0) + 1

    top_level: list[str] = []
    seen_dirs: set[str] = set()
    for relative in tracked:
        parts = PurePosixPath(relative).parts
        for depth in (1, 2):
            if len(parts) > depth:
                directory = "/".join(parts[:depth])
                if directory not in seen_dirs:
                    seen_dirs.add(directory)
                    top_level.append(directory)

    return {
        "schema_version": "retro-project-files-v1",
        "base_sha": base_sha,
        "remote": git_state.canonical_remote(repo_root),
        "tracked_file_count": len(tracked),
        "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        "directories": sorted(top_level),
        "files": sorted(files, key=lambda item: str(item["path"])),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", "utf-8")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(canonical_json(record))
            handle.write("\n")


def _populate(
    staging: Path,
    *,
    candidate: SourceCandidate,
    task_limits: TaskLimits,
    environment: ProjectEnvironment | None,
) -> SourceBundleManifest:
    root = candidate.repo_root
    base_sha = candidate.base_sha
    outcome_sha = candidate.outcome_sha

    rollout_dir = staging / "rollout"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    events_path = rollout_dir / "events.jsonl"
    shutil.copyfile(candidate.normalized_path, events_path)
    transcript = render_markdown(read_events(events_path))
    (rollout_dir / "transcript.md").write_text(transcript, encoding="utf-8")

    repo_dir = staging / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    git_state.materialize_tree(root, base_sha, repo_dir / "base")
    git_state.materialize_tree(root, outcome_sha, repo_dir / "outcome")
    (repo_dir / "change.patch").write_text(
        git_state.change_patch(root, base_sha, outcome_sha), encoding="utf-8"
    )
    _write_jsonl(repo_dir / "git-log.jsonl", git_state.commit_range(root, base_sha, outcome_sha))

    context_dir = staging / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        context_dir / "project-files.json",
        build_project_files(root, base_sha, repo_dir / "base"),
    )
    _write_json(
        context_dir / "test-commands.json",
        environment.test_commands()
        if environment is not None
        else {
            "schema_version": "retro-project-environment-v1",
            "environment_id": None,
            "workdir": None,
            "setup": [],
            "smoke": [],
            "test": [],
        },
    )
    if environment is not None:
        _write_json(context_dir / "environment.json", environment.to_dict())

    _write_json(staging / SELECTION_RECORD_NAME, candidate.selection_record())

    manifest = _source_manifest(candidate, task_limits, file_sha256(events_path))
    _write_json(staging / MANIFEST_NAME, manifest.to_dict())
    manifest = manifest.with_content_hash(compute_content_hash(staging))
    _write_json(staging / MANIFEST_NAME, manifest.to_dict())
    return manifest


def build_source_bundle(
    candidate: SourceCandidate,
    *,
    layout: Layout,
    name: str,
    task_limits: TaskLimits | None = None,
    force: bool = False,
) -> SourceBundle:
    """Materialize one immutable SourceBundle directory, published atomically."""
    limits = task_limits or TaskLimits()
    if limits.adjacent_per_replay and candidate.environment is None:
        raise BundleError("adjacent generation requires a validated project environment")
    target = layout.benchmark_taskset_source_dir(name, candidate.source_id)
    if target.exists():
        if not force:
            existing = load_bundle(target)
            if not verify_bundle(target):
                raise BundleError(
                    f"existing source bundle failed checksum verification: {target}"
                )
            if _stored_input_fingerprint(
                target, existing.manifest
            ) != _current_input_fingerprint(candidate, limits):
                raise BundleError(
                    f"existing source bundle inputs differ from current inputs: {target}; "
                    "use force=True to replace it"
                )
            return existing
    target.parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{candidate.source_id}.", dir=target.parent))
    try:
        manifest = _populate(
            staging,
            candidate=candidate,
            task_limits=limits,
            environment=candidate.environment,
        )
        if not verify_bundle(staging):
            raise BundleError(f"staged source bundle failed checksum verification: {staging}")
        _publish_bundle(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return SourceBundle(source_id=candidate.source_id, path=target, manifest=manifest)


@dataclass(frozen=True)
class BundleOutcome:
    """One source's result from ``retro benchmark taskset bundle``."""

    source_id: str
    host: str
    session_id: str
    status: str
    path: Path | None = None
    content_sha256: str | None = None
    code: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "host": self.host,
            "session_id": self.session_id,
            "status": self.status,
            "path": str(self.path) if self.path is not None else None,
            "content_sha256": self.content_sha256,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TasksetBundleReport:
    """Result of bundling every selected source of a taskset."""

    name: str
    outcomes: list[BundleOutcome] = dataclasses.field(default_factory=list)
    path: Path | None = None

    @property
    def bundled(self) -> list[BundleOutcome]:
        return [item for item in self.outcomes if item.status in ("bundled", "reused")]

    @property
    def skipped(self) -> list[BundleOutcome]:
        return [item for item in self.outcomes if item.status == "skipped"]

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BUNDLE_REPORT_SCHEMA,
            "name": self.name,
            "sources": [outcome.to_dict() for outcome in self.outcomes],
            "counts": {
                "bundled": len(self.bundled),
                "skipped": len(self.skipped),
                "by_status": self.status_counts(),
            },
        }


def bundle_report_path(layout: Layout, name: str) -> Path:
    return layout.benchmark_taskset_dir(name) / "bundles.json"


def _reselect(
    *,
    layout: Layout,
    host: str,
    session_id: str,
    branch: str,
    resolver: Any,
    require_environment: bool,
    stability_horizon_days: int,
) -> tuple[SourceCandidate | None, str | None, str]:
    candidate, rejection = select_source(
        layout=layout,
        host=host,  # type: ignore[arg-type]
        session_id=session_id,
        branch=branch,
        environment_resolver=resolver,
        require_environment=require_environment,
        stability_horizon_days=stability_horizon_days,
    )
    if candidate is None:
        code = rejection.code if rejection is not None else "NO_NORMALIZED_ROLLOUT"
        detail = rejection.detail if rejection is not None else "source is no longer selectable"
        return None, code, detail
    return candidate, None, ""


def bundle_taskset(
    *,
    layout: Layout,
    name: str,
    selected_only: bool = True,
    host: str | None = None,
    session_file: Path | None = None,
    sessions: Sequence[tuple[str, str]] | None = None,
    environment_file: Path | None = None,
    require_environment: bool = True,
    branch: str = "HEAD",
    stability_horizon_days: int = DEFAULT_STABILITY_HORIZON_DAYS,
    task_limits: TaskLimits | None = None,
    force: bool = False,
    write: bool = True,
) -> TasksetBundleReport:
    """Bundle a taskset's sources for ``retro benchmark taskset bundle``.

    With ``selected_only`` (the spec's ``--selected-only``) the source set comes
    from ``selection.json`` and each source is re-proven from Git evidence before
    materialization; a source whose base or outcome no longer resolves the same
    way is skipped rather than silently re-anchored. Otherwise the sessions are
    selected first using ``--host``/``--session-file``.
    """
    limits = task_limits or TaskLimits()
    contract = (
        load_environment_contract(Path(environment_file)) if environment_file is not None else None
    )
    outcomes: list[BundleOutcome] = []

    if selected_only:
        path = selection_path(layout, name)
        if not path.is_file():
            raise BundleError(
                f"no selection at {path}; run 'retro benchmark taskset select --name {name}' first"
            )
        payload = load_selection(path)
        recorded = payload.get("environment") or {}
        entries = payload.get("selected") or []
        if not isinstance(entries, list):
            raise BundleError("selection.json selected entries must be an array")
        if contract is None and isinstance(recorded, Mapping):
            recorded_contract = recorded.get("contract")
            if isinstance(recorded_contract, Mapping):
                contract_path = recorded_contract.get("contract_path")
                if isinstance(contract_path, str) and Path(contract_path).is_file():
                    contract = load_environment_contract(Path(contract_path))
        embedded_environments: dict[str, ProjectEnvironment] = {}
        if contract is None:
            for entry in entries:
                if not isinstance(entry, Mapping) or entry.get("environment") is None:
                    continue
                try:
                    environment = ProjectEnvironment.from_dict(entry["environment"])
                except SchemaError as error:
                    raise BundleError(f"selection contains an invalid environment: {error}") from error
                embedded_environments[environment.base_sha] = environment
        if contract is None and not embedded_environments and require_environment:
            required = bool(recorded.get("required")) if isinstance(recorded, Mapping) else True
            if required:
                raise BundleError(
                    "selection recorded a validated environment contract that is no longer "
                    "readable; re-run select with an explicit --environment-file"
                )
            require_environment = False
        resolver = None
        if contract is not None:
            resolver = contract.resolver()
        elif embedded_environments:
            def resolve_embedded(candidate: SourceCandidate) -> ProjectEnvironment | None:
                return embedded_environments.get(candidate.base_sha)

            resolver = resolve_embedded
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise BundleError("selection.json selected entries must be objects")
            entry_host = str(entry.get("host", ""))
            session_id = str(entry.get("session_id", ""))
            source_id = str(entry.get("source_id") or f"{entry_host}__{session_id}")
            candidate, code, detail = _reselect(
                layout=layout,
                host=entry_host,
                session_id=session_id,
                branch=str(entry.get("branch") or branch),
                resolver=resolver,
                require_environment=require_environment,
                stability_horizon_days=stability_horizon_days,
            )
            if candidate is None:
                outcomes.append(
                    BundleOutcome(
                        source_id=source_id,
                        host=entry_host,
                        session_id=session_id,
                        status="skipped",
                        code=code,
                        detail=detail,
                    )
                )
                continue
            drift = _selection_drift(entry, candidate)
            if drift:
                outcomes.append(
                    BundleOutcome(
                        source_id=source_id,
                        host=entry_host,
                        session_id=session_id,
                        status="skipped",
                        code="HARNESS_ERROR",
                        detail=f"selection is stale: {drift}",
                    )
                )
                continue
            outcomes.append(
                _materialize(
                    candidate, layout=layout, name=name, task_limits=limits, force=force
                )
            )
    else:
        selection = select_taskset(
            layout=layout,
            name=name,
            host=host,
            session_file=session_file,
            sessions=sessions,
            environment_file=environment_file,
            require_environment=require_environment,
            branch=branch,
            stability_horizon_days=stability_horizon_days,
            write=write,
        )
        for rejection in selection.rejections:
            outcomes.append(
                BundleOutcome(
                    source_id=rejection.source_id,
                    host=rejection.host,
                    session_id=rejection.session_id,
                    status="skipped",
                    code=rejection.code,
                    detail=rejection.detail,
                )
            )
        for candidate in selection.selected:
            outcomes.append(
                _materialize(
                    candidate, layout=layout, name=name, task_limits=limits, force=force
                )
            )

    outcomes.sort(key=lambda item: (item.source_id, item.status))
    report = TasksetBundleReport(name=name, outcomes=outcomes)
    report_path: Path | None = None
    if write:
        report_path = bundle_report_path(layout, name)
        atomic_write_text(
            report_path, json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
        )
    return TasksetBundleReport(name=name, outcomes=outcomes, path=report_path)


def _selection_drift(entry: Mapping[str, Any], candidate: SourceCandidate) -> str:
    base_value = entry.get("base")
    outcome_value = entry.get("outcome")
    recorded_base: Mapping[str, Any] = base_value if isinstance(base_value, Mapping) else {}
    recorded_outcome: Mapping[str, Any] = (
        outcome_value if isinstance(outcome_value, Mapping) else {}
    )
    expected = {
        "base_sha": recorded_base.get("sha"),
        "base_tree": recorded_base.get("tree"),
        "outcome_sha": recorded_outcome.get("sha"),
        "outcome_tree": recorded_outcome.get("tree"),
    }
    actual = {
        "base_sha": candidate.base_sha,
        "base_tree": candidate.base_tree,
        "outcome_sha": candidate.outcome_sha,
        "outcome_tree": candidate.outcome_tree,
    }
    differences = [
        f"{key} {expected[key]} -> {actual[key]}"
        for key in sorted(expected)
        if expected[key] is not None and expected[key] != actual[key]
    ]
    return "; ".join(differences)


def _materialize(
    candidate: SourceCandidate,
    *,
    layout: Layout,
    name: str,
    task_limits: TaskLimits,
    force: bool,
) -> BundleOutcome:
    target = layout.benchmark_taskset_source_dir(name, candidate.source_id)
    existed = target.exists()
    bundle = build_source_bundle(
        candidate, layout=layout, name=name, task_limits=task_limits, force=force
    )
    return BundleOutcome(
        source_id=candidate.source_id,
        host=candidate.host,
        session_id=candidate.session_id,
        status="reused" if existed and not force else "bundled",
        path=bundle.path,
        content_sha256=bundle.manifest.content_sha256,
    )


def load_bundle(path: Path) -> SourceBundle:
    manifest_path = path / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BundleError(f"no bundle manifest at {manifest_path}")
    manifest = SourceBundleManifest.from_dict(
        json.loads(manifest_path.read_text(encoding="utf-8"))
    )
    return SourceBundle(source_id=manifest.source_id, path=path, manifest=manifest)


def verify_bundle(path: Path) -> bool:
    """True when the on-disk contents still match the recorded ``content_sha256``."""
    bundle = load_bundle(path)
    if bundle.manifest.content_sha256 is None:
        raise SchemaError("bundle manifest has no content_sha256")
    require_sha256_hex(bundle.manifest.content_sha256, "content_sha256")
    return compute_content_hash(path) == bundle.manifest.content_sha256


__all__ = [
    "BUNDLE_LAYOUT",
    "BUNDLE_REPORT_SCHEMA",
    "MANIFEST_NAME",
    "SELECTION_RECORD_NAME",
    "BundleError",
    "BundleOutcome",
    "SourceBundle",
    "TasksetBundleReport",
    "build_project_files",
    "build_source_bundle",
    "bundle_report_path",
    "bundle_taskset",
    "compute_content_hash",
    "directory_digest",
    "file_sha256",
    "load_bundle",
    "verify_bundle",
]
