"""Resumable task/scorer construction state machine.

Implements the spec section 14.3 pipeline::

    selected -> bundled -> task_generated -> task_linted
             -> scorer_built -> scorer_validated -> audited -> published

Every transition writes ``stage.json`` atomically, a failed transition keeps
prior artifacts plus an error record, and an unchanged input fingerprint reuses
the previous artifact instead of paying for another Ghostlab run.
"""
from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ...utils import atomic_write_text
from .aggregate import validate_score_report
from .bundle import (
    BUNDLE_REPORT_SCHEMA,
    BundleError,
    compute_content_hash,
    load_bundle,
    verify_bundle,
)
from .ghostlab_cli import (
    SCORE_REPORT_CONTRACT,
    SCORER_AUDIT_CONTRACT,
    SCORER_RUN_REPORT_NAME,
    TASK_DEFINITIONS_CONTRACT,
    ArtifactRunRequest,
    ArtifactRunResult,
    ExportSpec,
    GhostlabCli,
    GhostlabError,
    ScorerRunRequest,
    packaged_contract_errors,
    read_json,
    schema_path,
    sha256_file,
    sha256_json,
    sha256_path,
    sha256_text,
    validate_scorer_run_attestation,
    write_json,
)
from .schema import (
    ProjectEnvironment,
    SchemaError,
    ScorerManifest,
    compute_task_id,
    normalize_prompt,
)

STAGE_SCHEMA = "retro-taskset-stage-v1"
SOURCE_BUNDLE_SCHEMA = "retro-source-bundle-v1"
TASK_DEFINITIONS_SCHEMA = "retro-task-definitions-v1"
SCORER_SCHEMA = "retro-scorer-v1"
VALIDATION_CASES_SCHEMA = "retro-scorer-validation-cases-v1"
SCORER_VALIDATION_SCHEMA = "retro-scorer-validation-v1"
BENCHMARK_TASK_SCHEMA = "retro-benchmark-task-v1"
PROVENANCE_SCHEMA = "retro-task-provenance-v1"
ENVIRONMENT_SCHEMA = "retro-project-environment-v1"
BUILD_REPORT_SCHEMA = "retro-taskset-build-v1"
ACTIVE_TASKS_SCHEMA = "retro-active-tasks-v1"

STAGES: tuple[str, ...] = (
    "selected",
    "bundled",
    "task_generated",
    "task_linted",
    "scorer_built",
    "scorer_validated",
    "audited",
    "published",
)
SOURCE_STAGES: tuple[str, ...] = STAGES[:4]
TASK_STAGES: tuple[str, ...] = STAGES[4:]
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}

SCORER_MODES = frozenset({"deterministic", "judge", "hybrid", "agentic"})
JUDGE_MODES = SCORER_MODES - {"deterministic"}
# Agentic command execution belongs to Ghostlab's deterministic phase; its
# credential-bearing residual judge remains subject to the non-executing floor.
JUDGE_AGENT_TOOL_POLICIES: Mapping[str, Mapping[str, bool]] = {
    "judge": {"bash": False, "webfetch": False},
    "hybrid": {"bash": False, "webfetch": False},
    "agentic": {"bash": False, "webfetch": False},
}
JUDGE_AGENT_PERMISSION_POLICIES: Mapping[str, Mapping[str, str]] = {
    "judge": {"bash": "deny", "edit": "deny", "external_directory": "deny"},
    "hybrid": {"bash": "deny", "edit": "deny", "external_directory": "deny"},
    "agentic": {"bash": "deny", "edit": "deny", "external_directory": "deny"},
}

# Spec section 11.1 required results.
BASE_MAX_TOTAL = 0.20
ORACLE_MIN_TOTAL = 0.90
CONSTRUCT_CHANGING_MIN_DROP = 0.50
CONSTRUCT_PRESERVING_MAX_DELTA = 0.05
PERFORMANCE_MAX_SPREAD = 0.05
JUDGE_MAX_STDEV = 0.10

REQUIRED_VALIDATION_KINDS: tuple[str, ...] = (
    "base",
    "oracle",
    "no_op",
    "construct_changing",
    "construct_preserving",
    "regression",
)

#: Stable rejection code emitted when a mandatory validation case fails.
VALIDATION_CASE_CODES: Mapping[str, str] = {
    "base": "BASE_ALREADY_PASSES",
    "no_op": "BASE_ALREADY_PASSES",
    "oracle": "ORACLE_DOES_NOT_PASS",
    "construct_changing": "NO_OBSERVABLE_OUTCOME",
    "construct_preserving": "SCORER_OVERFIT",
    "regression": "NO_OBSERVABLE_OUTCOME",
}

#: ``artifact-run`` status -> Retro rejection code.
ARTIFACT_STATUS_CODES: Mapping[str, str] = {
    "agent_error": "HARNESS_ERROR",
    "timed_out": "HARNESS_ERROR",
    "timeout": "HARNESS_ERROR",
    "model_unavailable": "HARNESS_ERROR",
    "sandbox_error": "HARNESS_ERROR",
    "harness_error": "HARNESS_ERROR",
    "export_failed": "BUILDER_CONTRACT_ERROR",
    "output_contract_failed": "BUILDER_CONTRACT_ERROR",
    "contract_violation": "BUILDER_CONTRACT_ERROR",
}

PUBLIC_TASK_FILES: tuple[str, ...] = ("task.json", "prompt.txt", "base.bundle", "environment.json")

DEFAULT_FORBIDDEN_SCORER_SUBSTRINGS: tuple[str, ...] = (
    "repo/outcome",
    "reference.patch",
    "reference-state.tar.zst",
)

#: Stable instruction assets shipped in ``task_scorer/instructions/``.
INSTRUCTION_ASSET_DIR = Path(__file__).resolve().parent / "instructions"
PACKAGED_INSTRUCTIONS: Mapping[str, str] = {
    "task-definer": "task-definer.md",
    "scorer-builder": "scorer-builder.md",
    "scorer-auditor": "scorer-auditor.md",
    "residual-judge": "residual-judge.md",
}

_GIT_BUNDLE_MAGICS: tuple[bytes, ...] = (b"# v2 git bundle", b"# v3 git bundle")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PINNED_IMAGE_RE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")


class BuildConfigurationError(RuntimeError):
    """The caller wired the build with missing or inconsistent inputs."""


@dataclass(frozen=True)
class Rejection:
    """One stable-coded reason a source or candidate did not become a task."""

    source_id: str
    stage: str
    code: str
    detail: str
    task_id: str | None = None
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "code": self.code,
            "detail": self.detail,
        }


class StageFailure(Exception):
    """Raised inside a stage to record a coded, non-fatal transition failure."""

    def __init__(self, code: str, detail: str, *, task_id: str | None = None) -> None:
        self.code = code
        self.detail = detail
        self.task_id = task_id
        super().__init__(f"{code}: {detail}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def instruction_path(name: str) -> Path:
    """Absolute path to a packaged agent instruction asset.

    Agent configurations must point ``runtime.instructions`` at these files; the
    pipeline never inlines or paraphrases them.
    """
    filename = PACKAGED_INSTRUCTIONS.get(name)
    if filename is None:
        raise BuildConfigurationError(
            f"unknown packaged instruction {name!r}, expected one of "
            f"{sorted(PACKAGED_INSTRUCTIONS)}"
        )
    path = INSTRUCTION_ASSET_DIR / filename
    if not path.is_file():
        raise BuildConfigurationError(
            f"packaged instruction {name!r} is missing at {path}; reinstall retro-ai"
        )
    return path


def instruction_text(name: str) -> str:
    return instruction_path(name).read_text(encoding="utf-8")


def instruction_sha256(name: str) -> str:
    return sha256_file(instruction_path(name))


def packaged_instruction_index() -> dict[str, str]:
    """Map packaged instruction file name to content hash."""
    index: dict[str, str] = {}
    for filename in sorted(set(PACKAGED_INSTRUCTIONS.values())):
        path = INSTRUCTION_ASSET_DIR / filename
        if path.is_file():
            index[filename] = sha256_file(path)
        else:  # pragma: no cover - broken installation
            index[filename] = ""
    return index


def agent_instruction_hashes(agent_config: Path) -> tuple[dict[str, str], list[str]]:
    """Resolve an agent config's ``runtime.instructions`` to files and hash them.

    Entries resolve against the agent config directory first, then the packaged
    instruction directory, so a config may reference ``instructions/task-definer.md``
    without copying it. Spec section 7.1 requires the instruction hash in the
    build manifest.
    """
    warnings: list[str] = []
    payload = read_json(agent_config, label="agent config")
    runtime = _mapping(payload.get("runtime") if isinstance(payload, Mapping) else None)
    declared = runtime.get("instructions")
    if not isinstance(declared, list):
        return {}, warnings
    packaged = packaged_instruction_index()
    hashes: dict[str, str] = {}
    for entry in declared:
        if not isinstance(entry, str) or not entry:
            continue
        candidates = [
            (agent_config.parent / entry),
            (INSTRUCTION_ASSET_DIR / Path(entry).name),
        ]
        resolved = next((path for path in candidates if path.is_file()), None)
        if resolved is None:
            warnings.append(
                f"{agent_config.name}: instruction {entry!r} does not resolve to a file"
            )
            hashes[entry] = ""
            continue
        digest = sha256_file(resolved)
        hashes[entry] = digest
        expected = packaged.get(Path(entry).name)
        if expected and expected != digest:
            warnings.append(
                f"{agent_config.name}: instruction {entry!r} differs from the packaged "
                f"{Path(entry).name}; the run will not use the stable Retro instruction"
            )
    return hashes, warnings


def _validate_identifier(value: str, label: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise BuildConfigurationError(f"{label} {value!r} contains unsupported characters")
    return value


def _safe_generated_basename(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or Path(value).name != value
        or not _SAFE_ID_RE.fullmatch(value)
    ):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"{label} {value!r} must be a safe basename identifier",
        )
    return value


def _contained_child(root: Path, name: str, label: str) -> Path:
    safe_name = _safe_generated_basename(name, label)
    root_resolved = root.resolve()
    child = root / safe_name
    resolved = child.resolve()
    if resolved == root_resolved or root_resolved not in resolved.parents:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"{label} {safe_name!r} escapes {root}",
        )
    if child.is_symlink():
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"{label} {safe_name!r} resolves through an unsupported symlink",
        )
    return child


@dataclass(frozen=True)
class TasksetPaths:
    """Filesystem layout for ``benchmarks/<name>/task-scorer`` (spec section 14.2).

    Delegates to ``Layout`` helpers when the archive layout already exposes them
    so that the foundation modules and this orchestrator never diverge.
    """

    root: Path
    name: str
    layout: Any = None

    @classmethod
    def from_layout(cls, layout: Any, name: str) -> TasksetPaths:
        _validate_identifier(name, "taskset name")
        helper = getattr(layout, "benchmark_taskset_dir", None)
        if callable(helper):
            return cls(root=Path(helper(name)), name=name, layout=layout)
        benchmark_dir = getattr(layout, "benchmark_dir", None)
        if callable(benchmark_dir):
            return cls(root=Path(benchmark_dir(name)) / "task-scorer", name=name, layout=layout)
        raise BuildConfigurationError(
            "layout does not expose benchmark_dir(); pass TasksetPaths(root=..., name=...)"
        )

    def _delegate(self, method: str, *args: Any) -> Path | None:
        helper = getattr(self.layout, method, None)
        if callable(helper):
            return Path(helper(self.name, *args))
        return None

    def sources_dir(self) -> Path:
        return self._delegate("benchmark_taskset_sources_dir") or self.root / "sources"

    def source_dir(self, source_id: str) -> Path:
        return self._delegate("benchmark_taskset_source_dir", source_id) or (
            self.sources_dir() / source_id
        )

    def bundle_report_path(self) -> Path:
        return self.root / "bundles.json"

    def tasks_dir(self) -> Path:
        return self._delegate("benchmark_taskset_tasks_dir") or self.root / "tasks"

    def task_dir(self, task_id: str) -> Path:
        return self._delegate("benchmark_taskset_task_dir", task_id) or self.tasks_dir() / task_id

    def active_tasks_path(self) -> Path:
        return self._delegate("benchmark_taskset_active_tasks_path") or self.root / "active-tasks.json"

    def build_run_dir(self, build_id: str) -> Path:
        return self._delegate("benchmark_taskset_build_run_dir", build_id) or (
            self.root / "builds" / build_id
        )

    def eval_dir(self, eval_id: str) -> Path:
        return self._delegate("benchmark_taskset_eval_dir", eval_id) or self.root / "evals" / eval_id

    def attempt_dir(self, eval_id: str, task_id: str, agent_id: str, seed: int) -> Path:
        delegated = self._delegate(
            "benchmark_taskset_attempt_dir", eval_id, task_id, agent_id, seed
        )
        if delegated is not None:
            return delegated
        return self.eval_dir(eval_id) / "attempts" / task_id / agent_id / f"seed-{seed}"

    def results_path(self, eval_id: str) -> Path:
        return self._delegate("benchmark_taskset_results_path", eval_id) or (
            self.eval_dir(eval_id) / "results.json"
        )

    def build_source_dir(self, build_id: str, source_id: str) -> Path:
        return self.build_run_dir(build_id) / "sources" / source_id

    def build_task_dir(self, build_id: str, task_id: str) -> Path:
        return self.build_run_dir(build_id) / "tasks" / task_id

    def stage_path(self, build_id: str, source_id: str) -> Path:
        return self.build_source_dir(build_id, source_id) / "stage.json"


@dataclass(frozen=True)
class TaskStageState:
    task_id: str
    candidate_id: str
    stage: str = "task_linted"
    status: str = "ok"
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)
    rejection: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "stage": self.stage,
            "status": self.status,
            "fingerprints": dict(sorted(self.fingerprints.items())),
            "artifacts": dict(sorted(self.artifacts.items())),
            "rejection": dict(self.rejection) if self.rejection else None,
            "error": dict(self.error) if self.error else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TaskStageState:
        return cls(
            task_id=str(payload.get("task_id", "")),
            candidate_id=str(payload.get("candidate_id", "")),
            stage=str(payload.get("stage", "task_linted")),
            status=str(payload.get("status", "ok")),
            fingerprints=dict(payload.get("fingerprints") or {}),
            artifacts=dict(payload.get("artifacts") or {}),
            rejection=payload.get("rejection") or None,
            error=payload.get("error") or None,
        )

    def reached(self, stage: str) -> bool:
        return self.status == "ok" and STAGE_INDEX[self.stage] >= STAGE_INDEX[stage]


@dataclass(frozen=True)
class SourceStageState:
    source_id: str
    stage: str = "selected"
    status: str = "ok"
    completed: tuple[str, ...] = ()
    fingerprints: Mapping[str, str] = field(default_factory=dict)
    artifacts: Mapping[str, str] = field(default_factory=dict)
    tasks: Mapping[str, TaskStageState] = field(default_factory=dict)
    rejections: tuple[Mapping[str, Any], ...] = ()
    error: Mapping[str, Any] | None = None
    warnings: tuple[str, ...] = ()
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STAGE_SCHEMA,
            "source_id": self.source_id,
            "stage": self.stage,
            "status": self.status,
            "completed": list(self.completed),
            "fingerprints": dict(sorted(self.fingerprints.items())),
            "artifacts": dict(sorted(self.artifacts.items())),
            "tasks": {key: value.to_dict() for key, value in sorted(self.tasks.items())},
            "rejections": [dict(item) for item in self.rejections],
            "error": dict(self.error) if self.error else None,
            "warnings": list(self.warnings),
            "updated_at": self.updated_at or utc_now(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SourceStageState:
        tasks_raw = payload.get("tasks") or {}
        tasks = {
            str(key): TaskStageState.from_dict(value)
            for key, value in tasks_raw.items()
            if isinstance(value, Mapping)
        }
        return cls(
            source_id=str(payload.get("source_id", "")),
            stage=str(payload.get("stage", "selected")),
            status=str(payload.get("status", "ok")),
            completed=tuple(str(item) for item in payload.get("completed") or ()),
            fingerprints=dict(payload.get("fingerprints") or {}),
            artifacts=dict(payload.get("artifacts") or {}),
            tasks=tasks,
            rejections=tuple(
                item for item in (payload.get("rejections") or ()) if isinstance(item, Mapping)
            ),
            error=payload.get("error") or None,
            warnings=tuple(str(item) for item in payload.get("warnings") or ()),
            updated_at=str(payload.get("updated_at", "")),
        )

    def reached(self, stage: str, fingerprint: str | None = None) -> bool:
        """True when ``stage`` completed with the same input fingerprint."""
        if stage not in self.completed:
            return False
        if fingerprint is None:
            return True
        return self.fingerprints.get(stage) == fingerprint

    def advance(
        self,
        stage: str,
        fingerprint: str,
        *,
        artifacts: Mapping[str, str] | None = None,
        warnings: Sequence[str] = (),
    ) -> SourceStageState:
        if self.reached(stage, fingerprint):
            # Re-entering a stage whose inputs are unchanged must not discard the
            # downstream progress recorded by an earlier build.
            keep = self.completed
            fingerprints = dict(self.fingerprints)
        else:
            keep = tuple(name for name in self.completed if STAGE_INDEX[name] < STAGE_INDEX[stage])
            fingerprints = {
                key: value for key, value in self.fingerprints.items() if key in keep
            }
            keep = keep + (stage,)
        fingerprints[stage] = fingerprint
        merged_artifacts = dict(self.artifacts)
        merged_artifacts.update(artifacts or {})
        return replace(
            self,
            stage=keep[-1] if keep else stage,
            status="ok",
            completed=keep,
            fingerprints=fingerprints,
            artifacts=merged_artifacts,
            error=None,
            warnings=tuple(dict.fromkeys(self.warnings + tuple(warnings))),
            updated_at=utc_now(),
        )

    def fail(self, stage: str, code: str, detail: str) -> SourceStageState:
        return replace(
            self,
            status="error",
            error={"stage": stage, "code": code, "detail": detail, "at": utc_now()},
            updated_at=utc_now(),
        )

    def with_rejection(self, rejection: Rejection) -> SourceStageState:
        return replace(
            self,
            rejections=self.rejections + (rejection.to_dict(),),
            updated_at=utc_now(),
        )

    def with_task(self, task: TaskStageState) -> SourceStageState:
        tasks = dict(self.tasks)
        tasks[task.task_id] = task
        return replace(self, tasks=tasks, updated_at=utc_now())


class StageStore:
    """Atomic reader/writer for one source's ``stage.json``."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, source_id: str) -> SourceStageState:
        if not self.path.exists():
            return SourceStageState(source_id=source_id)
        payload = read_json(self.path, label=f"stage state {self.path}")
        if not isinstance(payload, Mapping):
            raise BuildConfigurationError(f"stage state {self.path} is not a JSON object")
        if payload.get("schema_version") != STAGE_SCHEMA:
            raise BuildConfigurationError(
                f"stage state {self.path} declares schema_version="
                f"{payload.get('schema_version')!r}, expected {STAGE_SCHEMA!r}"
            )
        state = SourceStageState.from_dict(payload)
        return state if state.source_id == source_id else replace(state, source_id=source_id)

    def save(self, state: SourceStageState) -> SourceStageState:
        stamped = replace(state, updated_at=utc_now())
        write_json(self.path, stamped.to_dict())
        return stamped


@dataclass(frozen=True)
class LintFinding:
    code: str
    detail: str
    candidate_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "candidate_id": self.candidate_id}


@dataclass(frozen=True)
class LintRequest:
    """Input contract handed to ``task_lint`` (spec section 8.1)."""

    source_id: str
    source_dir: Path
    manifest: Mapping[str, Any]
    task_definitions: Mapping[str, Any]
    adjacent_per_replay: int
    max_replay_tasks: int


@dataclass(frozen=True)
class LintOutcome:
    accepted: tuple[Mapping[str, Any], ...] = ()
    findings: tuple[LintFinding, ...] = ()


LintFn = Callable[[LintRequest], "LintOutcome"]

_LINT_ENTRYPOINTS = (
    "lint_task_definitions",
    "lint_task_candidates",
    "lint_tasks",
    "lint_candidates",
    "lint",
)


def _coerce_findings(value: Any) -> tuple[LintFinding, ...]:
    findings: list[LintFinding] = []
    for item in value or ():
        if isinstance(item, LintFinding):
            findings.append(item)
        elif isinstance(item, Mapping):
            findings.append(
                LintFinding(
                    code=str(item.get("code", "PROMPT_ORACLE_LEAKAGE")),
                    detail=str(item.get("detail", "")),
                    candidate_id=(
                        str(item["candidate_id"]) if item.get("candidate_id") is not None else None
                    ),
                )
            )
        else:
            code = getattr(item, "code", None)
            findings.append(
                LintFinding(
                    code=str(code) if code else "PROMPT_ORACLE_LEAKAGE",
                    detail=str(getattr(item, "detail", item)),
                    candidate_id=getattr(item, "candidate_id", None),
                )
            )
    return tuple(findings)


def coerce_lint_outcome(value: Any) -> LintOutcome:
    """Accept several plausible ``task_lint`` return shapes without guessing semantics."""
    if isinstance(value, LintOutcome):
        return value
    if isinstance(value, Mapping):
        accepted = value.get("accepted", value.get("tasks", ()))
        findings = value.get("findings", value.get("rejections", ()))
    elif isinstance(value, tuple) and len(value) == 2:
        accepted, findings = value
    else:
        accepted = getattr(value, "accepted", getattr(value, "tasks", None))
        findings = getattr(value, "findings", getattr(value, "rejections", ()))
        if accepted is None:
            raise BuildConfigurationError(
                "task lint returned an unsupported shape; expected LintOutcome, a mapping with "
                "'accepted'/'findings', or a (accepted, findings) tuple"
            )
    accepted_tasks = tuple(item for item in accepted or () if isinstance(item, Mapping))
    return LintOutcome(accepted=accepted_tasks, findings=_coerce_findings(findings))


def resolve_lint_fn(module_name: str = "retro.benchmarks.task_scorer.task_lint") -> LintFn:
    """Locate the foundation task lint, or explain exactly how to supply one."""
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BuildConfigurationError(
            f"{module_name} is unavailable ({exc}); pass BuildConfig(lint=...) with a callable "
            "accepting a LintRequest and returning a LintOutcome"
        ) from exc
    for name in _LINT_ENTRYPOINTS:
        candidate = getattr(module, name, None)
        if callable(candidate):
            def _call(request: LintRequest, _fn: Any = candidate) -> LintOutcome:
                return coerce_lint_outcome(_fn(request))

            return _call
    raise BuildConfigurationError(
        f"{module_name} exposes none of {_LINT_ENTRYPOINTS}; pass BuildConfig(lint=...)"
    )


def _stable_callable_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _stable_callable_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_callable_value(item) for item in value]
    if callable(value):
        return {
            "callable": (
                f"{getattr(value, '__module__', type(value).__module__)}:"
                f"{getattr(value, '__qualname__', type(value).__qualname__)}"
            )
        }
    return {"type": f"{type(value).__module__}:{type(value).__qualname__}"}


def _callable_fingerprint(value: Callable[..., Any] | None) -> dict[str, Any]:
    if value is None:
        module_object = importlib.import_module("retro.benchmarks.task_scorer.task_lint")
        target = next(
            (
                candidate
                for name in _LINT_ENTRYPOINTS
                if callable(candidate := getattr(module_object, name, None))
            ),
            None,
        )
        if target is None:
            raise BuildConfigurationError(
                "retro.benchmarks.task_scorer.task_lint exposes no supported lint callable"
            )
    else:
        target = value
    target = inspect.unwrap(target)
    module = getattr(target, "__module__", type(target).__module__)
    qualname = getattr(target, "__qualname__", type(target).__qualname__)
    try:
        source_path = inspect.getsourcefile(target)
    except TypeError:
        source_path = None
    source_sha256 = None
    if source_path is not None and Path(source_path).is_file():
        source_sha256 = sha256_file(Path(source_path))
    code = getattr(target, "__code__", None)
    implementation_sha256 = (
        sha256_json(
            {
                "bytecode": code.co_code.hex(),
                "constants": repr(code.co_consts),
                "names": list(code.co_names),
                "defaults": _stable_callable_value(getattr(target, "__defaults__", None)),
                "kwdefaults": _stable_callable_value(getattr(target, "__kwdefaults__", None)),
                "closure": [
                    _stable_callable_value(cell.cell_contents)
                    for cell in (getattr(target, "__closure__", None) or ())
                ],
            }
        )
        if code is not None
        else sha256_text(f"{module}:{qualname}")
    )
    try:
        state = vars(target)
    except TypeError:
        state = {}
    return {
        "callable": f"{module}:{qualname}",
        "source_sha256": source_sha256,
        "implementation_sha256": implementation_sha256,
        "state": _stable_callable_value(state),
    }


@dataclass(frozen=True)
class BuildConfig:
    """Everything the build state machine needs that is not on disk yet."""

    name: str
    ghostlab: GhostlabCli
    task_definer_agent: Path
    scorer_builder_agent: Path
    scorer_auditor_agent: Path | None = None
    #: ``None`` uses the packaged ``schemas/task-definitions.schema.json``.
    task_definitions_schema: Path | None = None
    #: ``None`` uses the packaged ``schemas/scorer-audit.schema.json``.
    scorer_audit_schema: Path | None = None
    scorer_sdk: Path | None = None
    adjacent_per_replay: int = 0
    max_replay_tasks: int = 3
    repeatability_runs: int = 3
    require_audit: bool = True
    lint: LintFn | None = None
    forbidden_public_substrings: tuple[str, ...] = ()
    forbidden_scorer_substrings: tuple[str, ...] = DEFAULT_FORBIDDEN_SCORER_SUBSTRINGS
    definer_timeout_seconds: float | None = None
    builder_timeout_seconds: float | None = None
    auditor_timeout_seconds: float | None = None
    scorer_timeout_seconds: float | None = None

    def lint_fn(self) -> LintFn:
        """Return a lint callable that normalizes whatever shape the linter returns."""
        if self.lint is None:
            return resolve_lint_fn()
        supplied = self.lint

        def _call(request: LintRequest) -> LintOutcome:
            return coerce_lint_outcome(supplied(request))

        return _call

    def definitions_contract(self) -> Path:
        """Output contract for ``task-definitions.json``."""
        return self.task_definitions_schema or schema_path(TASK_DEFINITIONS_CONTRACT)

    def audit_contract(self) -> Path:
        """Output contract for ``audit.json``."""
        return self.scorer_audit_schema or schema_path(SCORER_AUDIT_CONTRACT)

    def fingerprint(self) -> dict[str, Any]:
        return {
            "adjacent_per_replay": self.adjacent_per_replay,
            "max_replay_tasks": self.max_replay_tasks,
            "repeatability_runs": self.repeatability_runs,
            "require_audit": self.require_audit,
            "forbidden_public_substrings": list(self.forbidden_public_substrings),
            "forbidden_scorer_substrings": list(self.forbidden_scorer_substrings),
            "timeouts": {
                "definer": self.definer_timeout_seconds,
                "builder": self.builder_timeout_seconds,
                "auditor": self.auditor_timeout_seconds,
                "scorer": self.scorer_timeout_seconds,
                "ghostlab_default": self.ghostlab.default_timeout_seconds,
            },
            "ghostlab": self.ghostlab.version().fingerprint(),
            "ghostlab_invocation": {
                "cwd": str(self.ghostlab.cwd) if self.ghostlab.cwd else None,
                "environment_sha256": sha256_json(dict(sorted(self.ghostlab.env.items()))),
                "runner": _callable_fingerprint(self.ghostlab._runner),
            },
            "instructions": packaged_instruction_index(),
            "contracts": {
                "task_definitions": sha256_file(self.definitions_contract()),
                "scorer_audit": sha256_file(self.audit_contract()),
                "score_report": sha256_file(schema_path(SCORE_REPORT_CONTRACT)),
            },
            "scorer_sdk": (
                sha256_path(self.scorer_sdk, excludes=()) if self.scorer_sdk else None
            ),
            "lint": _callable_fingerprint(self.lint),
            "builder_implementation_sha256": sha256_file(Path(__file__)),
        }


@dataclass(frozen=True)
class SourceBuildResult:
    source_id: str
    state: SourceStageState
    published_task_ids: tuple[str, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    reused_stages: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.state.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "stage": self.state.stage,
            "status": self.state.status,
            "published_task_ids": list(self.published_task_ids),
            "rejections": [item.to_dict() for item in self.rejections],
            "reused_stages": list(self.reused_stages),
            "warnings": list(self.state.warnings),
            "error": dict(self.state.error) if self.state.error else None,
        }


@dataclass(frozen=True)
class BuildResult:
    name: str
    build_id: str
    sources: tuple[SourceBuildResult, ...]

    @property
    def published_task_ids(self) -> tuple[str, ...]:
        return tuple(
            task_id for source in self.sources for task_id in source.published_task_ids
        )

    @property
    def rejections(self) -> tuple[Rejection, ...]:
        return tuple(item for source in self.sources for item in source.rejections)

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.code] = counts.get(rejection.code, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BUILD_REPORT_SCHEMA,
            "name": self.name,
            "build_id": self.build_id,
            "published_task_ids": list(self.published_task_ids),
            "rejection_counts": self.rejection_counts(),
            "rejections": [item.to_dict() for item in self.rejections],
            "sources": [item.to_dict() for item in self.sources],
        }


def pack_directory(source: Path, destination: Path, *, excludes: Sequence[str] = (".git",)) -> Path:
    """Write a byte-identical tar for identical trees (no mtime/uid/gid entropy)."""
    blocked = set(excludes)
    entries: list[tuple[str, Path]] = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in blocked for part in relative.parts):
            continue
        entries.append((relative.as_posix(), path))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w", format=tarfile.GNU_FORMAT) as archive:
        for name, path in entries:
            info = archive.gettarinfo(str(path), arcname=name)
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o755 if (info.isdir() or info.mode & 0o111) else 0o644
            if info.isfile():
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
            else:
                archive.addfile(info)
    return destination


def is_git_bundle(path: Path) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as handle:
        head = handle.read(32)
    return any(head.startswith(magic) for magic in _GIT_BUNDLE_MAGICS)


def _tar_member_path(root: Path, name: str, *, label: str = "member") -> tuple[PurePosixPath, Path]:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise StageFailure("HARNESS_ERROR", f"bundle archive {label} {name!r} escapes its destination")
    parts = tuple(part for part in relative.parts if part not in ("", "."))
    normalized = PurePosixPath(*parts)
    return normalized, root.joinpath(*parts)


def _assert_no_symlink_parent(root: Path, target: Path, *, member_name: str) -> None:
    current = root
    for part in target.relative_to(root).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise StageFailure(
                "HARNESS_ERROR",
                f"bundle archive member {member_name!r} traverses symlink {current}",
            )
        if current.exists() and not current.is_dir():
            raise StageFailure(
                "HARNESS_ERROR",
                f"bundle archive member {member_name!r} traverses non-directory {current}",
            )


def _safe_tar_link(
    root: Path,
    member_path: PurePosixPath,
    linkname: str,
    *,
    symbolic: bool,
) -> tuple[PurePosixPath, Path]:
    link = PurePosixPath(linkname)
    if link.is_absolute():
        raise StageFailure(
            "HARNESS_ERROR",
            f"bundle archive contains an escaping link {member_path.as_posix()!r}",
        )
    combined = (member_path.parent / link).parts if symbolic else link.parts
    normalized: list[str] = []
    for part in combined:
        if part in ("", "."):
            continue
        if part == "..":
            if not normalized:
                raise StageFailure(
                    "HARNESS_ERROR",
                    f"bundle archive contains an escaping link {member_path.as_posix()!r}",
                )
            normalized.pop()
        else:
            normalized.append(part)
    relative = PurePosixPath(*normalized)
    return relative, root.joinpath(*normalized)


def _extract_tar_stream(bundle: Path, destination: Path) -> None:
    root = destination.resolve()
    members: dict[str, str] = {}
    deferred_modes: list[tuple[Path, int]] = []
    with bundle.open("rb") as source, tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            relative, target = _tar_member_path(root, member.name)
            name = relative.as_posix()
            if not relative.parts:
                if not member.isdir():
                    raise StageFailure(
                        "HARNESS_ERROR", "bundle archive root entry must be a directory"
                    )
                continue
            if name in members:
                raise StageFailure(
                    "HARNESS_ERROR", f"bundle archive contains duplicate member {name!r}"
                )
            _assert_no_symlink_parent(root, target, member_name=name)
            target.parent.mkdir(parents=True, exist_ok=True)

            if member.isdir():
                if target.is_symlink() or (target.exists() and not target.is_dir()):
                    raise StageFailure(
                        "HARNESS_ERROR",
                        f"bundle archive directory {name!r} conflicts with an existing entry",
                    )
                target.mkdir(exist_ok=True)
                members[name] = "directory"
                deferred_modes.append((target, member.mode & 0o777))
                continue

            if target.exists() or target.is_symlink():
                raise StageFailure(
                    "HARNESS_ERROR",
                    f"bundle archive member {name!r} conflicts with an existing entry",
                )
            if member.isfile():
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise StageFailure(
                        "HARNESS_ERROR", f"bundle archive cannot read regular file {name!r}"
                    )
                with extracted, target.open("xb") as output:
                    shutil.copyfileobj(extracted, output, length=1024 * 1024)
                target.chmod(member.mode & 0o777)
                members[name] = "file"
                continue
            if member.issym():
                _safe_tar_link(root, relative, member.linkname, symbolic=True)
                os.symlink(member.linkname, target)
                members[name] = "symlink"
                continue
            if member.islnk():
                link_relative, link_target = _safe_tar_link(
                    root, relative, member.linkname, symbolic=False
                )
                _assert_no_symlink_parent(
                    root, link_target, member_name=link_relative.as_posix()
                )
                if (
                    members.get(link_relative.as_posix()) != "file"
                    or link_target.is_symlink()
                    or not link_target.is_file()
                ):
                    raise StageFailure(
                        "HARNESS_ERROR",
                        f"bundle archive hard link {name!r} must target an earlier regular file",
                    )
                os.link(link_target, target, follow_symlinks=False)
                members[name] = "file"
                continue
            if member.isdev() or member.isfifo():
                raise StageFailure(
                    "HARNESS_ERROR", f"bundle archive contains a special file {name!r}"
                )
            raise StageFailure(
                "HARNESS_ERROR",
                f"bundle archive contains unsupported member type for {name!r}",
            )
    for path, mode in reversed(deferred_modes):
        path.chmod(mode)


def unpack_bundle(bundle: Path, destination: Path) -> Path:
    """Materialize a base/oracle bundle into a fresh directory."""
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if bundle.is_dir():
        shutil.copytree(bundle, destination, dirs_exist_ok=True)
        return destination
    if is_git_bundle(bundle):
        result = subprocess.run(
            ["git", "clone", "--quiet", str(bundle), str(destination)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise StageFailure(
                "HARNESS_ERROR",
                f"git clone of {bundle} failed: {(result.stderr or '').strip()}",
            )
        return destination
    try:
        _extract_tar_stream(bundle, destination)
    except (OSError, tarfile.TarError) as error:
        shutil.rmtree(destination, ignore_errors=True)
        raise StageFailure(
            "HARNESS_ERROR", f"cannot unpack bundle {bundle}: {error}"
        ) from error
    except StageFailure:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def materialize_repo_bundle(source_dir: Path, state: str, destination: Path) -> Path:
    """Produce ``base.bundle``/``oracle.bundle`` from a SourceBundle repository state."""
    prebuilt = source_dir / "repo" / f"{state}.bundle"
    if prebuilt.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prebuilt, destination)
        return destination
    checkout = source_dir / "repo" / state
    if not checkout.is_dir():
        raise StageFailure(
            "HARNESS_ERROR", f"source bundle is missing repo/{state} and repo/{state}.bundle"
        )
    if (checkout / ".git").exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["git", "-C", str(checkout), "bundle", "create", str(destination), "--all"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and destination.exists():
            return destination
    return pack_directory(checkout, destination)


def publish_directory(staging: Path, target: Path) -> Path:
    """Replace ``target`` with ``staging`` without ever exposing a partial tree."""
    target.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if target.exists():
        backup = target.parent / f".{target.name}.replaced-{os.getpid()}"
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except OSError:
        if backup is not None:
            os.replace(backup, target)
        raise
    if backup is not None:
        shutil.rmtree(backup, ignore_errors=True)
    return target


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)
    return path


def task_definer_prompt(source_id: str, adjacent_per_replay: int) -> str:
    return (
        f"Build task definitions for SourceBundle {source_id}.\n"
        "Read manifest.json first. Inspect the complete rollout and both repository states\n"
        f"with retro-context. Adjacent generation limit: {adjacent_per_replay}.\n"
        "Write only /sandbox/output/task-definitions.json.\n"
    )


def scorer_builder_prompt(task_id: str) -> str:
    return (
        f"Build and self-test the scorer for TaskDefinition {task_id}.\n"
        "The canonical task is task.json. The complete SourceBundle is in source/.\n"
        "Write the scorer, reference solution, and validation cases only under\n"
        "/sandbox/output. Execute your self-tests before finishing.\n"
    )


def scorer_auditor_prompt(task_id: str) -> str:
    return (
        f"Audit scorer {task_id} without editing it. Verify that it measures the task,\n"
        "accepts behaviorally valid alternatives, rejects the unchanged base and targeted\n"
        "mutants, protects regressions, and leaks no oracle information. Add mutants under\n"
        "/sandbox/output/mutants and write /sandbox/output/audit.json.\n"
    )


def check_builder_contract(
    result: ArtifactRunResult,
    *,
    stage: str,
    immutable_workspace: bool = True,
) -> None:
    """Fail the stage unless the agent respected the export and mutation contract."""
    if result.status != "completed":
        code = ARTIFACT_STATUS_CODES.get(result.status, "HARNESS_ERROR")
        raise StageFailure(
            code,
            f"{stage}: ghostlab artifact-run reported status={result.status} "
            f"(exit={result.exit_code}, timed_out={result.timed_out}); "
            f"stderr: {result.stderr_tail or '<empty>'}",
        )
    if immutable_workspace and result.workspace_mutated:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"{stage}: the agent mutated its input workspace "
            f"({result.workspace_input_sha256} -> {result.workspace_output_sha256}); "
            "only /sandbox/output may change",
        )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def load_source_manifest(source_dir: Path) -> dict[str, Any]:
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        raise StageFailure("HARNESS_ERROR", f"source bundle manifest missing at {manifest_path}")
    payload = read_json(manifest_path, label="source bundle manifest")
    if not isinstance(payload, dict):
        raise StageFailure("HARNESS_ERROR", "source bundle manifest is not a JSON object")
    if payload.get("schema_version") != SOURCE_BUNDLE_SCHEMA:
        raise StageFailure(
            "HARNESS_ERROR",
            f"source bundle manifest declares schema_version={payload.get('schema_version')!r}, "
            f"expected {SOURCE_BUNDLE_SCHEMA!r}",
        )
    return payload


def load_environment(source_dir: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Resolve ``retro-project-environment-v1`` for a source bundle."""
    for candidate in (
        source_dir / "context" / "environment.json",
        source_dir / "environment.json",
    ):
        if candidate.is_file():
            payload = read_json(candidate, label="project environment")
            try:
                environment = ProjectEnvironment.from_dict(payload)
            except SchemaError as error:
                raise StageFailure(
                    "ENVIRONMENT_UNAVAILABLE",
                    f"invalid project environment at {candidate}: {error}",
                ) from error
            repo = _mapping(manifest.get("repo"))
            if environment.environment_id != repo.get("environment_id"):
                raise StageFailure(
                    "ENVIRONMENT_UNAVAILABLE",
                    "project environment id does not match the source bundle manifest",
                )
            if environment.base_sha != repo.get("base_sha"):
                raise StageFailure(
                    "ENVIRONMENT_UNAVAILABLE",
                    "project environment base_sha does not match the source bundle",
                )
            return environment.to_dict(), []
    raise StageFailure(
        "ENVIRONMENT_UNAVAILABLE",
        "source bundle has no validated context/environment.json",
    )


def build_public_task(
    task: Mapping[str, Any],
    manifest: Mapping[str, Any],
    environment: Mapping[str, Any],
    scorer_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    repo: Mapping[str, Any] = _mapping(manifest.get("repo"))
    setup = environment.get("setup") or []
    setup_command = setup[0] if isinstance(setup, list) and setup else []
    limits: Mapping[str, Any] = _mapping(environment.get("limits"))
    threshold = scorer_manifest.get("pass_threshold")
    return {
        "schema_version": BENCHMARK_TASK_SCHEMA,
        "task_id": task["task_id"],
        "kind": task.get("kind", "replay"),
        "prompt": task.get("prompt", ""),
        "repository": {
            "repo_id": repo.get("repo_id"),
            "base_sha": repo.get("base_sha"),
            "base_tree": repo.get("base_tree"),
            "subdir": repo.get("subdir", "."),
        },
        "environment": {
            "image": environment.get("image"),
            "setup_command": setup_command,
            "network": environment.get("network_during_run", "disabled"),
        },
        "limits": {
            "wall_time_seconds": int(limits.get("wall_time_seconds", 1800)),
            "max_output_chars": int(limits.get("max_output_chars", 20000)),
        },
        "scoring": {
            "score_range": [0.0, 1.0],
            "pass_threshold": float(threshold) if isinstance(threshold, (int, float)) else 0.8,
        },
    }


def _scorer_package_file(scorer_dir: Path, declared: str, label: str) -> Path:
    path = Path(declared)
    if path.is_absolute():
        try:
            relative = path.relative_to("/scorer")
        except ValueError:
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"scorer {label}={declared!r} must resolve under /scorer",
            ) from None
    else:
        relative = path
    root = scorer_dir.resolve()
    resolved = (root / relative).resolve()
    if resolved == root or root not in resolved.parents:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer {label}={declared!r} escapes the scorer package",
        )
    if not resolved.is_file():
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer {label}={declared!r} is not present in the package",
        )
    return resolved


def _validate_judge_agent(
    scorer_dir: Path,
    agent_path: Path,
    prompt_path: Path,
    *,
    mode: str,
) -> None:
    try:
        payload = read_json(agent_path, label="judge agent config")
    except GhostlabError as error:
        raise StageFailure("BUILDER_CONTRACT_ERROR", str(error)) from error
    if not isinstance(payload, Mapping):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", "judge.agent_config must contain a JSON object"
        )
    runtime = payload.get("runtime")
    if not isinstance(runtime, Mapping):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", "judge.agent_config must declare runtime"
        )
    expected_runtime_keys = {
        "backend",
        "model",
        "instructions",
        "tools",
        "permission",
    }
    if set(runtime) != expected_runtime_keys:
        raise StageFailure(
            "SCORER_UNSAFE",
            "judge.agent_config runtime keys must exactly equal "
            f"{sorted(expected_runtime_keys)!r}",
        )
    if runtime.get("backend") != "opencode":
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "judge.agent_config runtime.backend must be 'opencode'",
        )
    model = runtime.get("model")
    if (
        not isinstance(model, str)
        or not model.strip()
        or "$" in model
    ):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "judge.agent_config runtime.model must be pinned to an explicit model",
        )
    tools = runtime.get("tools")
    if not isinstance(tools, Mapping):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", "judge.agent_config must declare runtime.tools"
        )
    expected_tools = JUDGE_AGENT_TOOL_POLICIES[mode]
    if set(tools) != set(expected_tools):
        raise StageFailure(
            "SCORER_UNSAFE",
            f"judge.agent_config runtime.tools keys must exactly equal "
            f"{sorted(expected_tools)!r} for mode {mode!r}",
        )
    for tool, expected_tool_value in expected_tools.items():
        if tools.get(tool) is not expected_tool_value:
            raise StageFailure(
                "SCORER_UNSAFE",
                f"judge.agent_config runtime.tools.{tool} must be {expected_tool_value!r} "
                f"for mode {mode!r}",
            )
    permission = runtime.get("permission")
    if not isinstance(permission, Mapping):
        raise StageFailure(
            "SCORER_UNSAFE", "judge.agent_config must declare runtime.permission"
        )
    expected_permissions = JUDGE_AGENT_PERMISSION_POLICIES[mode]
    if set(permission) != set(expected_permissions):
        raise StageFailure(
            "SCORER_UNSAFE",
            f"judge.agent_config runtime.permission keys must exactly equal "
            f"{sorted(expected_permissions)!r} for mode {mode!r}",
        )
    for capability, expected_permission in expected_permissions.items():
        if permission.get(capability) != expected_permission:
            raise StageFailure(
                "SCORER_UNSAFE",
                f"judge.agent_config runtime.permission.{capability} must be "
                f"{expected_permission!r} for mode {mode!r}",
            )

    instructions = runtime.get("instructions")
    if (
        not isinstance(instructions, list)
        or len(instructions) != 1
        or not isinstance(instructions[0], str)
        or not instructions[0]
    ):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "judge.agent_config runtime.instructions must contain exactly judge.prompt",
        )
    instruction_path = _scorer_package_file(
        scorer_dir,
        instructions[0],
        "judge.agent_config runtime.instructions[0]",
    )
    if instruction_path != prompt_path:
        raise StageFailure(
            "SCORER_UNSAFE",
            "judge.agent_config runtime.instructions must reference the packaged "
            "judge.prompt declared by scorer.json",
        )

    inputs = payload.get("inputs")
    if not isinstance(inputs, Mapping):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", "judge.agent_config inputs must be an object"
        )
    expected_input_keys = {"skills", "mcps", "assets"}
    if set(inputs) != expected_input_keys:
        raise StageFailure(
            "SCORER_UNSAFE",
            "judge.agent_config inputs keys must exactly equal "
            f"{sorted(expected_input_keys)!r}",
        )
    for input_kind in ("skills", "mcps", "assets"):
        declared = inputs.get(input_kind, [])
        if declared != []:
            raise StageFailure(
                "SCORER_UNSAFE",
                f"judge.agent_config inputs.{input_kind} must be empty",
            )


def _validate_scorer_security(
    scorer_dir: Path, manifest: Mapping[str, Any], mode: Any
) -> None:
    if not isinstance(mode, str) or mode not in SCORER_MODES:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer.json mode={mode!r} is not one of {sorted(SCORER_MODES)}",
        )
    runtime = manifest.get("runtime")
    if not isinstance(runtime, Mapping):
        raise StageFailure("BUILDER_CONTRACT_ERROR", "scorer.json must declare runtime")
    image = runtime.get("image")
    if not isinstance(image, str) or not _PINNED_IMAGE_RE.fullmatch(image):
        raise StageFailure(
            "SCORER_UNSAFE",
            "scorer runtime.image must be pinned by a sha256 digest",
        )
    if runtime.get("network") != "disabled":
        raise StageFailure(
            "SCORER_UNSAFE", "scorer runtime.network must be 'disabled'"
        )
    if runtime.get("candidate_mount") != "read_only":
        raise StageFailure(
            "SCORER_UNSAFE", "scorer runtime.candidate_mount must be 'read_only'"
        )

    judge = manifest.get("judge")
    if mode in JUDGE_MODES and not isinstance(judge, Mapping):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer mode={mode!r} requires a pinned judge.agent_config",
        )
    if judge is None:
        return
    if not isinstance(judge, Mapping) or judge.get("enabled") is not True:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "scorer judge must be an enabled object when present",
        )
    resolved: dict[str, Path] = {}
    for key in ("agent_config", "prompt", "output_schema"):
        declared = judge.get(key)
        if not isinstance(declared, str) or not declared:
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"scorer judge.{key} must name a packaged file",
            )
        resolved[key] = _scorer_package_file(scorer_dir, declared, f"judge.{key}")
    _validate_judge_agent(
        scorer_dir,
        resolved["agent_config"],
        resolved["prompt"],
        mode=str(mode),
    )


def validate_scorer_package(
    scorer_dir: Path, task_id: str, *, forbidden_substrings: Sequence[str] = ()
) -> tuple[dict[str, Any], str, list[str]]:
    """Structurally validate ``retro-scorer-v1`` and hash the package."""
    manifest_path = scorer_dir / "scorer.json"
    if not manifest_path.is_file():
        raise StageFailure("BUILDER_CONTRACT_ERROR", "scorer package has no scorer.json")
    raw_manifest = read_json(manifest_path, label="scorer.json")
    if not isinstance(raw_manifest, dict):
        raise StageFailure("BUILDER_CONTRACT_ERROR", "scorer.json is not a JSON object")
    _validate_scorer_security(scorer_dir, raw_manifest, raw_manifest.get("mode"))
    try:
        parsed = ScorerManifest.from_dict(raw_manifest, where="scorer.json")
    except SchemaError as error:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer.json is not a valid {SCORER_SCHEMA} manifest: {error}",
        ) from error
    if parsed.task_id != task_id:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer.json task_id={parsed.task_id!r} does not match {task_id!r}",
        )
    manifest = parsed.to_dict()
    warnings: list[str] = []
    try:
        computed = compute_scorer_package_hash(scorer_dir)
    except ValueError as error:
        raise StageFailure("SCORER_UNSAFE", str(error)) from error
    declared = parsed.package_sha256
    if isinstance(declared, str) and declared and declared != computed:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"scorer.json package_sha256={declared} differs from Retro's computed {computed}",
        )

    hits = _scan_for_substrings(scorer_dir, forbidden_substrings)
    if hits:
        raise StageFailure(
            "SCORER_UNSAFE",
            "scorer package references forbidden oracle material: " + ", ".join(hits),
        )
    return manifest, computed, warnings


def compute_scorer_package_hash(scorer_dir: Path) -> str:
    manifest_path = scorer_dir / "scorer.json"
    manifest = read_json(manifest_path, label="scorer.json")
    if not isinstance(manifest, Mapping):
        raise ValueError("scorer.json is not a JSON object")
    entries: list[dict[str, str]] = []
    for path in sorted(scorer_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(
                f"scorer package contains unsupported symlink: "
                f"{path.relative_to(scorer_dir).as_posix()}"
            )
        if not path.is_file():
            continue
        relative = path.relative_to(scorer_dir).as_posix()
        if relative == "scorer.json":
            digest = sha256_text(
                json.dumps(
                    {
                        key: value
                        for key, value in manifest.items()
                        if key != "package_sha256"
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            digest = sha256_file(path)
        entries.append({"path": relative, "kind": "file", "sha256": digest})
    return sha256_text(json.dumps(entries, sort_keys=True, separators=(",", ":")))


def _scan_for_substrings(root: Path, needles: Sequence[str]) -> list[str]:
    active = [needle for needle in needles if needle]
    if not active:
        return []
    hits: list[str] = []
    targets = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    for path in targets:
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        text = blob.decode("utf-8", errors="ignore")
        for needle in active:
            if needle in text:
                hits.append(f"{path.name}:{needle}")
    return sorted(set(hits))


@dataclass(frozen=True)
class ValidationCaseResult:
    case_id: str
    kind: str
    status: str
    ok: bool
    score_total: float | None
    passed: bool | None
    hard_gate_failures: tuple[str, ...]
    component_values: Mapping[str, float | None]
    runs: int
    code: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "status": self.status,
            "ok": self.ok,
            "score_total": self.score_total,
            "passed": self.passed,
            "hard_gate_failures": list(self.hard_gate_failures),
            "component_values": dict(sorted(self.component_values.items())),
            "runs": self.runs,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RepeatabilityResult:
    ok: bool
    deterministic_mismatches: tuple[str, ...] = ()
    performance_spread: float | None = None
    judge_stdev: Mapping[str, float] = field(default_factory=dict)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "deterministic_mismatches": list(self.deterministic_mismatches),
            "performance_spread": self.performance_spread,
            "judge_stdev": dict(sorted(self.judge_stdev.items())),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScorerValidation:
    task_id: str
    passed: bool
    cases: tuple[ValidationCaseResult, ...]
    repeatability: RepeatabilityResult
    codes: tuple[str, ...] = ()
    scorer_package_sha256: str = ""
    isolation_attestation: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCORER_VALIDATION_SCHEMA,
            "task_id": self.task_id,
            "passed": self.passed,
            "scorer_package_sha256": self.scorer_package_sha256,
            "codes": list(self.codes),
            "cases": [case.to_dict() for case in self.cases],
            "repeatability": self.repeatability.to_dict(),
            "isolation_attestation": dict(self.isolation_attestation),
        }


def _component_values(report: Mapping[str, Any]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for component in report.get("components") or ():
        if not isinstance(component, Mapping):
            continue
        component_id = component.get("id")
        if not isinstance(component_id, str):
            continue
        raw = component.get("value")
        values[component_id] = (
            float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        )
    return values


def _component_kinds(scorer_manifest: Mapping[str, Any]) -> dict[str, str]:
    kinds: dict[str, str] = {}
    for component in scorer_manifest.get("components") or ():
        if isinstance(component, Mapping) and isinstance(component.get("id"), str):
            kind = component.get("kind")
            kinds[component["id"]] = kind if isinstance(kind, str) else "deterministic"
    return kinds


def _hard_gate_ids(scorer_manifest: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        component["id"]
        for component in scorer_manifest.get("components") or ()
        if isinstance(component, Mapping)
        and isinstance(component.get("id"), str)
        and component.get("hard_gate")
    )


def _gate_failures(report: Mapping[str, Any]) -> tuple[str, ...]:
    declared = report.get("hard_gate_failures")
    failures: list[str] = []
    if isinstance(declared, list):
        failures.extend(str(item) for item in declared)
    for component in report.get("components") or ():
        if (
            isinstance(component, Mapping)
            and component.get("hard_gate")
            and component.get("gate_passed") is False
            and isinstance(component.get("id"), str)
        ):
            failures.append(component["id"])
    return tuple(dict.fromkeys(failures))


def evaluate_validation_case(
    kind: str,
    case_id: str,
    reports: Sequence[Mapping[str, Any]],
    *,
    oracle_total: float | None,
    targeted_component: str | None,
    oracle_components: Mapping[str, float | None],
) -> ValidationCaseResult:
    """Apply the spec section 11.1 requirement table to one mandatory case."""
    primary = reports[0]
    status = str(primary.get("status"))
    values = _component_values(primary)
    failures = _gate_failures(primary)
    raw_total = primary.get("score_total")
    total = (
        float(raw_total) if isinstance(raw_total, (int, float)) and not isinstance(raw_total, bool)
        else None
    )
    passed = primary.get("passed") if isinstance(primary.get("passed"), bool) else None

    def fail(detail: str) -> ValidationCaseResult:
        return ValidationCaseResult(
            case_id=case_id,
            kind=kind,
            status=status,
            ok=False,
            score_total=total,
            passed=passed,
            hard_gate_failures=failures,
            component_values=values,
            runs=len(reports),
            code=VALIDATION_CASE_CODES.get(kind, "SCORER_OVERFIT"),
            detail=detail,
        )

    if status != "scored":
        return ValidationCaseResult(
            case_id=case_id,
            kind=kind,
            status=status,
            ok=False,
            score_total=None,
            passed=None,
            hard_gate_failures=failures,
            component_values=values,
            runs=len(reports),
            code="BUILDER_CONTRACT_ERROR",
            detail=f"validation case {case_id!r} returned status={status!r} instead of 'scored'",
        )
    if total is None:
        return fail(f"validation case {case_id!r} produced no numeric score_total")

    if kind == "base":
        if total > BASE_MAX_TOTAL:
            return fail(f"base scored {total} > {BASE_MAX_TOTAL}")
        if not failures:
            return fail("base scored low but no requested-behavior hard gate failed")
        if targeted_component is not None and targeted_component not in failures:
            return fail(
                f"base did not fail the declared requested-behavior gate {targeted_component!r}"
            )
    elif kind == "oracle":
        if total < ORACLE_MIN_TOTAL:
            return fail(f"oracle scored {total} < {ORACLE_MIN_TOTAL}")
        if failures:
            return fail(f"oracle failed hard gates {list(failures)}")
    elif kind == "no_op":
        if passed:
            return fail("a no-op candidate passed the task")
    elif kind == "construct_changing":
        component = targeted_component
        if component is None:
            return fail("construct-changing case declares no targeted component")
        before = oracle_components.get(component)
        after = values.get(component)
        if before is None or after is None:
            return fail(f"targeted component {component!r} is missing from oracle or mutant report")
        if before - after < CONSTRUCT_CHANGING_MIN_DROP:
            return fail(
                f"targeted component {component!r} dropped {before - after} "
                f"< {CONSTRUCT_CHANGING_MIN_DROP}"
            )
    elif kind == "construct_preserving":
        if oracle_total is None:
            return fail("construct-preserving case cannot be compared without an oracle total")
        if abs(total - oracle_total) > CONSTRUCT_PRESERVING_MAX_DELTA:
            return fail(
                f"construct-preserving total moved {abs(total - oracle_total)} "
                f"> {CONSTRUCT_PRESERVING_MAX_DELTA}"
            )
    elif kind == "regression":
        if not failures:
            return fail("regression mutant did not fail any hard gate")
        if targeted_component is not None and targeted_component not in failures:
            return fail(
                f"regression mutant did not fail the declared gate {targeted_component!r}"
            )
        if total != 0.0:
            return fail(f"regression mutant total {total} is not 0")
    else:
        return fail(f"unknown validation case kind {kind!r}")

    return ValidationCaseResult(
        case_id=case_id,
        kind=kind,
        status=status,
        ok=True,
        score_total=total,
        passed=passed,
        hard_gate_failures=failures,
        component_values=values,
        runs=len(reports),
    )


def evaluate_repeatability(
    reports_by_case: Mapping[str, Sequence[Mapping[str, Any]]],
    component_kinds: Mapping[str, str],
    hard_gates: Sequence[str],
    *,
    expected_repeats: int | None = None,
) -> RepeatabilityResult:
    """Apply spec section 11.2 across the repeated runs of every case."""
    import statistics

    mismatches: list[str] = []
    spreads: list[float] = []
    judge_stdev: dict[str, float] = {}
    details: list[str] = []
    declared_components = set(component_kinds)

    for case_id, reports in sorted(reports_by_case.items()):
        if expected_repeats is not None and len(reports) != expected_repeats:
            details.append(
                f"{case_id} produced {len(reports)} repeats, expected {expected_repeats}"
            )
        if not reports:
            details.append(f"{case_id} produced no repeat reports")
            continue
        complete_reports = True
        for index, report in enumerate(reports):
            status = report.get("status")
            if status != "scored":
                complete_reports = False
                details.append(
                    f"{case_id} repeat {index} returned status={status!r} instead of 'scored'"
                )
            component_items = report.get("components")
            component_ids: list[str] = []
            if isinstance(component_items, list):
                for item in component_items:
                    if isinstance(item, Mapping):
                        component_id = item.get("id")
                        if isinstance(component_id, str):
                            component_ids.append(component_id)
            reported_components = set(component_ids)
            missing = sorted(declared_components - reported_components)
            unexpected = sorted(reported_components - declared_components)
            duplicates = sorted(
                component_id
                for component_id in reported_components
                if component_ids.count(component_id) > 1
            )
            if missing or unexpected or duplicates:
                complete_reports = False
                problems: list[str] = []
                if missing:
                    problems.append(f"missing {missing}")
                if unexpected:
                    problems.append(f"unexpected {unexpected}")
                if duplicates:
                    problems.append(f"duplicated {duplicates}")
                details.append(
                    f"{case_id} repeat {index} has an incomplete declared component set "
                    f"({', '.join(problems)})"
                )
        totals = [
            float(report["score_total"])
            for report in reports
            if isinstance(report.get("score_total"), (int, float))
            and not isinstance(report.get("score_total"), bool)
        ]
        if len(totals) == len(reports):
            spreads.append(max(totals) - min(totals))
        else:
            complete_reports = False
            details.append(f"{case_id} has a repeat without a numeric score_total")
        if len(reports) < 2 or not complete_reports:
            continue
        per_component: dict[str, list[float | None]] = {
            component_id: [] for component_id in declared_components
        }
        for report in reports:
            report_values = _component_values(report)
            for component_id in declared_components:
                per_component[component_id].append(report_values[component_id])
        for component_id, component_values in sorted(per_component.items()):
            kind = component_kinds.get(component_id, "deterministic")
            if kind == "deterministic":
                if len(set(component_values)) > 1:
                    mismatches.append(f"{case_id}:{component_id}")
            elif kind == "judge":
                numeric: list[float] = [
                    value for value in component_values if value is not None
                ]
                if len(numeric) > 1:
                    stdev = statistics.pstdev(numeric)
                    judge_stdev[f"{case_id}:{component_id}"] = stdev
                    if stdev > JUDGE_MAX_STDEV and component_id in hard_gates:
                        details.append(
                            f"judge component {component_id!r} stdev {stdev} > {JUDGE_MAX_STDEV} "
                            "while acting as a hard gate"
                        )

    spread = max(spreads) if spreads else None
    if spread is not None and spread > PERFORMANCE_MAX_SPREAD:
        details.append(f"total-score spread {spread} > {PERFORMANCE_MAX_SPREAD}")
    if mismatches:
        details.append("deterministic components varied across repeats: " + ", ".join(mismatches))
    return RepeatabilityResult(
        ok=not details,
        deterministic_mismatches=tuple(mismatches),
        performance_spread=spread,
        judge_stdev=judge_stdev,
        detail="; ".join(details),
    )


@dataclass(frozen=True)
class _SourceContext:
    paths: TasksetPaths
    config: BuildConfig
    build_id: str
    source_id: str
    source_dir: Path
    work_dir: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    bundle_sha256: str = ""
    environment: dict[str, Any] = field(default_factory=dict)


def _stage_selected(source_dir: Path, source_id: str) -> tuple[str, list[str]]:
    """Confirm the source survived ``retro benchmark taskset select``."""
    warnings: list[str] = []
    record_path = source_dir / "selection.json"
    if not record_path.is_file():
        warnings.append(
            f"{source_id}: no selection.json in the source bundle; treating an existing bundle "
            "as selected"
        )
        return sha256_text(f"selection:absent:{source_id}"), warnings
    payload = read_json(record_path, label="selection record")
    if not isinstance(payload, Mapping):
        raise StageFailure("HARNESS_ERROR", "selection.json is not a JSON object")
    status = payload.get("status")
    selected = payload.get("selected")
    rejected = status in {"rejected", "excluded"} or selected is False
    if rejected:
        code = payload.get("code") or payload.get("rejection_code") or "NO_STABLE_GOAL"
        raise StageFailure(
            str(code), f"selection rejected {source_id}: {payload.get('detail') or status}"
        )
    return sha256_file(record_path), warnings


def _stage_bundled(source_dir: Path) -> tuple[dict[str, Any], str, list[str]]:
    """Verify the immutable SourceBundle contract before spending model time."""
    manifest = load_source_manifest(source_dir)
    warnings: list[str] = []
    repo = manifest.get("repo")
    if not isinstance(repo, Mapping):
        raise StageFailure("HARNESS_ERROR", "source bundle manifest has no repo section")
    for key in ("base_sha", "base_tree", "outcome_sha"):
        if not isinstance(repo.get(key), str) or not repo[key]:
            raise StageFailure("HARNESS_ERROR", f"source bundle manifest repo.{key} is missing")
    if repo.get("base_tree") == repo.get("outcome_tree"):
        raise StageFailure(
            "OUTCOME_NOT_DURABLE", "base and outcome trees are identical; nothing was accepted"
        )
    events = source_dir / "rollout" / "events.jsonl"
    if not events.is_file():
        raise StageFailure("NO_NORMALIZED_ROLLOUT", f"source bundle is missing {events}")
    if not (source_dir / "repo" / "base").is_dir() and not (
        source_dir / "repo" / "base.bundle"
    ).is_file():
        raise StageFailure("HARNESS_ERROR", "source bundle is missing repo/base")
    try:
        verified = verify_bundle(source_dir)
    except (BundleError, OSError, ValueError) as error:
        raise StageFailure("HARNESS_ERROR", f"source bundle verification failed: {error}") from error
    if not verified:
        raise StageFailure("HARNESS_ERROR", "source bundle content hash does not match manifest")
    declared = manifest.get("rollout_events_sha256")
    if isinstance(declared, str) and declared:
        actual = sha256_file(events)
        if actual != declared:
            raise StageFailure(
                "HARNESS_ERROR",
                f"rollout events hash {actual} does not match manifest {declared}",
            )
    else:
        warnings.append("source bundle manifest declares no rollout_events_sha256")
    bundle_sha256 = manifest.get("content_sha256")
    if not isinstance(bundle_sha256, str) or not bundle_sha256:
        raise StageFailure("HARNESS_ERROR", "source bundle manifest has no content_sha256")
    return manifest, bundle_sha256, warnings


def _stage_task_generated(
    ctx: _SourceContext, previous: str | None = None
) -> tuple[dict[str, Any], ArtifactRunResult | None, str, list[str]]:
    """Run the TaskDefiner and enforce its export and mutation contract."""
    config = ctx.config
    run_dir = ctx.work_dir / "task-generated"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = _write_text(
        run_dir / "prompt.md", task_definer_prompt(ctx.source_id, config.adjacent_per_replay)
    )
    export = ExportSpec("/sandbox/output/task-definitions.json", "task-definitions.json")
    contract = config.definitions_contract()
    instructions, instruction_warnings = agent_instruction_hashes(config.task_definer_agent)
    request = ArtifactRunRequest(
        agent_config=config.task_definer_agent,
        workspace=ctx.source_dir,
        prompt_file=prompt_path,
        run_dir=run_dir,
        exports=(export,),
        output_contract=contract,
        timeout_seconds=config.definer_timeout_seconds,
        label="task-definer",
    )
    fingerprint = sha256_json(
        {
            "stage": "task_generated",
            "bundle": ctx.bundle_sha256,
            "agent": sha256_file(config.task_definer_agent),
            "instructions": instructions,
            "prompt": sha256_file(prompt_path),
            "contract": sha256_file(contract),
            "config": config.fingerprint(),
        }
    )
    cached = run_dir / "task-definitions.json"
    if previous == fingerprint and cached.is_file():
        result = None
        exported: Path | None = cached
    else:
        result = config.ghostlab.artifact_run(request)
        check_builder_contract(result, stage="task_generated")
        exported = result.export_path("task-definitions.json")
    if exported is None:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", "TaskDefiner did not export task-definitions.json"
        )
    payload = read_json(exported, label="task-definitions.json")
    if not isinstance(payload, dict):
        raise StageFailure("BUILDER_CONTRACT_ERROR", "task-definitions.json is not a JSON object")
    if payload.get("schema_version") != TASK_DEFINITIONS_SCHEMA:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"task-definitions.json declares schema_version={payload.get('schema_version')!r}, "
            f"expected {TASK_DEFINITIONS_SCHEMA!r}",
        )
    if payload.get("source_id") not in (None, ctx.source_id):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"task-definitions.json source_id={payload.get('source_id')!r} does not match "
            f"{ctx.source_id!r}",
        )
    contract_errors = packaged_contract_errors(
        payload, TASK_DEFINITIONS_CONTRACT, where="task-definitions.json"
    )
    if contract_errors:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"task-definitions.json violates {contract}: " + "; ".join(contract_errors[:6]),
        )
    return payload, result, fingerprint, instruction_warnings


def _stage_task_linted(
    ctx: _SourceContext, definitions: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[Rejection], str]:
    """Run the deterministic lint and assign content-addressed task ids."""
    config = ctx.config
    repo = _mapping(ctx.manifest.get("repo"))
    base_tree = str(repo.get("base_tree", ""))
    request = LintRequest(
        source_id=ctx.source_id,
        source_dir=ctx.source_dir,
        manifest=ctx.manifest,
        task_definitions=definitions,
        adjacent_per_replay=config.adjacent_per_replay,
        max_replay_tasks=config.max_replay_tasks,
    )
    outcome = config.lint_fn()(request)
    rejections = [
        Rejection(
            source_id=ctx.source_id,
            stage="task_linted",
            code=finding.code,
            detail=finding.detail,
            candidate_id=finding.candidate_id,
        )
        for finding in outcome.findings
    ]
    accepted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in outcome.accepted:
        task = dict(candidate)
        kind = str(task.get("kind", "replay"))
        prompt = str(task.get("prompt", ""))
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            try:
                task_id = compute_task_id(ctx.source_id, base_tree, kind, prompt)
            except SchemaError as exc:
                raise StageFailure(
                    "BUILDER_CONTRACT_ERROR",
                    f"candidate {task.get('candidate_id')!r} cannot be canonicalized: {exc}",
                ) from exc
        if task_id in seen:
            rejections.append(
                Rejection(
                    source_id=ctx.source_id,
                    stage="task_linted",
                    code="MULTI_GOAL_NOT_SEPARABLE",
                    detail=f"duplicate task id {task_id} from candidate "
                    f"{task.get('candidate_id')!r}",
                    task_id=task_id,
                    candidate_id=task.get("candidate_id"),
                )
            )
            continue
        seen.add(task_id)
        task["task_id"] = task_id
        task["source_id"] = ctx.source_id
        task["prompt"] = prompt
        task.setdefault("candidate_id", task_id)
        accepted.append(task)

    replay_count = sum(1 for task in accepted if task.get("kind") == "replay")
    if replay_count > config.max_replay_tasks:
        raise StageFailure(
            "MULTI_GOAL_NOT_SEPARABLE",
            f"{replay_count} replay tasks exceed the limit of {config.max_replay_tasks}",
        )
    adjacent_count = sum(1 for task in accepted if task.get("kind") == "adjacent")
    if adjacent_count > config.adjacent_per_replay * max(replay_count, 0):
        raise StageFailure(
            "MULTI_GOAL_NOT_SEPARABLE",
            f"{adjacent_count} adjacent tasks exceed adjacent-per-replay="
            f"{config.adjacent_per_replay}",
        )

    for task in accepted:
        write_json(ctx.paths.build_task_dir(ctx.build_id, task["task_id"]) / "task.json", task)

    fingerprint = sha256_json(
        {
            "stage": "task_linted",
            "definitions": sha256_json(definitions),
            "bundle": ctx.bundle_sha256,
            "accepted": [task["task_id"] for task in accepted],
        }
    )
    return accepted, rejections, fingerprint


def _copy_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def _stage_scorer_built(
    ctx: _SourceContext, task: Mapping[str, Any], previous: str | None = None
) -> tuple[Path, dict[str, Any], str, ArtifactRunResult | None, Path, str, list[str]]:
    """Run the ScorerBuilder and validate the emitted package."""
    config = ctx.config
    task_id = str(task["task_id"])
    task_dir = ctx.paths.build_task_dir(ctx.build_id, task_id)
    input_dir = task_dir / "scorer-build-input"
    run_dir = task_dir / "scorer-built"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = _write_text(run_dir / "prompt.md", scorer_builder_prompt(task_id))
    exports = (
        ExportSpec("/sandbox/output/scorer", "scorer", required=False),
        ExportSpec("/sandbox/output/reference", "reference", required=False),
        ExportSpec("/sandbox/output/validation-cases.json", "validation-cases.json", required=False),
        ExportSpec("/sandbox/output/cases", "cases", required=False),
        ExportSpec("/sandbox/output/scorer-rejection.json", "scorer-rejection.json", required=False),
    )
    request = ArtifactRunRequest(
        agent_config=config.scorer_builder_agent,
        workspace=input_dir,
        prompt_file=prompt_path,
        run_dir=run_dir,
        exports=exports,
        timeout_seconds=config.builder_timeout_seconds,
        label="scorer-builder",
    )
    instructions, instruction_warnings = agent_instruction_hashes(config.scorer_builder_agent)
    fingerprint = sha256_json(
        {
            "stage": "scorer_built",
            "task": sha256_json(dict(task)),
            "bundle": ctx.bundle_sha256,
            "agent": sha256_file(config.scorer_builder_agent),
            "instructions": instructions,
            "sdk": (
                sha256_path(config.scorer_sdk, excludes=())
                if config.scorer_sdk
                else None
            ),
            "prompt": sha256_file(prompt_path),
            "config": config.fingerprint(),
        }
    )
    forbidden = tuple(config.forbidden_scorer_substrings) + (str(ctx.source_dir),)
    cached_scorer = run_dir / "scorer"
    cached_cases = run_dir / "validation-cases.json"
    if previous == fingerprint and cached_scorer.is_dir() and cached_cases.is_file():
        manifest, package_sha256, warnings = validate_scorer_package(
            cached_scorer, task_id, forbidden_substrings=forbidden
        )
        return (
            cached_scorer,
            manifest,
            package_sha256,
            None,
            cached_cases,
            fingerprint,
            instruction_warnings + warnings,
        )

    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    write_json(input_dir / "task.json", dict(task))
    _copy_tree(ctx.source_dir, input_dir / "source")
    if config.scorer_sdk is not None:
        _copy_tree(config.scorer_sdk, input_dir / "sdk")

    result = config.ghostlab.artifact_run(request)
    check_builder_contract(result, stage="scorer_built")

    rejection_path = result.export_path("scorer-rejection.json")
    scorer_dir = result.export_path("scorer")
    if rejection_path is not None and scorer_dir is not None:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "ScorerBuilder exported both a scorer package and a scorer rejection",
            task_id=task_id,
        )
    if rejection_path is not None:
        payload = read_json(rejection_path, label="scorer-rejection.json")
        detail = ""
        code = "NO_OBSERVABLE_OUTCOME"
        if isinstance(payload, Mapping):
            detail = str(payload.get("detail") or payload.get("reason") or "")
            declared = payload.get("code")
            if isinstance(declared, str) and declared:
                code = declared
        raise StageFailure(code, f"ScorerBuilder rejected {task_id}: {detail}", task_id=task_id)
    if scorer_dir is None or not scorer_dir.is_dir():
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "ScorerBuilder exported neither a scorer package nor a rejection",
            task_id=task_id,
        )

    manifest, package_sha256, warnings = validate_scorer_package(
        scorer_dir, task_id, forbidden_substrings=forbidden
    )
    cases_path = result.export_path("validation-cases.json")
    if cases_path is None:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            "ScorerBuilder exported no validation-cases.json",
            task_id=task_id,
        )
    return (
        scorer_dir,
        manifest,
        package_sha256,
        result,
        cases_path,
        fingerprint,
        instruction_warnings + warnings,
    )


def _apply_patch(work: Path, patch: Path) -> None:
    attempts = (
        ["git", "apply", "--whitespace=nowarn", str(patch)],
        ["patch", "-p1", "-i", str(patch)],
    )
    errors: list[str] = []
    for argv in attempts:
        try:
            result = subprocess.run(
                argv, cwd=str(work), capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            errors.append(f"{argv[0]} is unavailable")
            continue
        if result.returncode == 0:
            return
        errors.append(f"{argv[0]}: {(result.stderr or result.stdout or '').strip()}")
    raise StageFailure(
        "BUILDER_CONTRACT_ERROR", f"could not apply {patch.name}: {'; '.join(errors)}"
    )


def _resolve_export_reference(
    export_root: Path, declared: str, *, case_id: str, field: str
) -> Path:
    relative = Path(declared)
    if relative.is_absolute():
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation case {case_id!r} {field} path must be relative to the export directory",
        )
    root = export_root.resolve()
    resolved = (root / relative).resolve()
    if resolved == root or root not in resolved.parents:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation case {case_id!r} {field} path escapes the export directory",
        )
    return resolved


def _materialize_case_candidate(
    ctx: _SourceContext,
    case: Mapping[str, Any],
    export_root: Path,
    scratch: Path,
    case_id: str,
    kind: str,
) -> Path:
    declared = case.get("candidate")
    if isinstance(declared, str) and declared:
        candidate = _resolve_export_reference(
            export_root, declared, case_id=case_id, field="candidate"
        )
        if candidate.exists():
            return candidate
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation case {case_id!r} declares candidate {declared!r} which was not exported",
        )

    base_state = case.get("base_state")
    if not isinstance(base_state, str) or base_state not in {"base", "outcome"}:
        base_state = "outcome" if kind == "oracle" else "base"
    checkout = ctx.source_dir / "repo" / base_state
    if not checkout.is_dir():
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation case {case_id!r} needs repo/{base_state}, which the bundle lacks",
        )
    work = _contained_child(scratch, case_id, "validation case id")
    if work.exists():
        if not work.is_dir():
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"validation scratch path for {case_id!r} is not a directory",
            )
        shutil.rmtree(work)
    shutil.copytree(checkout, work)
    shutil.rmtree(work / ".git", ignore_errors=True)

    patch_name = case.get("patch")
    if not patch_name and kind == "oracle" and base_state == "base":
        reference_patch = export_root / "reference" / "reference.patch"
        if reference_patch.is_file():
            patch_name = "reference/reference.patch"
    if isinstance(patch_name, str) and patch_name:
        patch_path = _resolve_export_reference(
            export_root, patch_name, case_id=case_id, field="patch"
        )
        if not patch_path.is_file():
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"validation case {case_id!r} declares patch {patch_name!r} which was not exported",
            )
        _apply_patch(work, patch_path)
    archive = _contained_child(scratch, f"{case_id}.tar", "validation archive name")
    return pack_directory(work, archive)


def _load_validation_cases(cases_path: Path, task_id: str) -> list[dict[str, Any]]:
    payload = read_json(cases_path, label="validation-cases.json")
    if not isinstance(payload, Mapping):
        raise StageFailure("BUILDER_CONTRACT_ERROR", "validation-cases.json is not a JSON object")
    if payload.get("schema_version") not in (None, VALIDATION_CASES_SCHEMA):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation-cases.json declares schema_version={payload.get('schema_version')!r}, "
            f"expected {VALIDATION_CASES_SCHEMA!r}",
        )
    if payload.get("task_id") not in (None, task_id):
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation-cases.json task_id={payload.get('task_id')!r} does not match {task_id!r}",
        )
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise StageFailure("BUILDER_CONTRACT_ERROR", "validation-cases.json declares no cases")

    cases: list[dict[str, Any]] = []
    case_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        if not isinstance(item, Mapping):
            raise StageFailure("BUILDER_CONTRACT_ERROR", "each validation case must be an object")
        kind = item.get("kind")
        if kind not in REQUIRED_VALIDATION_KINDS:
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"validation case kind={kind!r} is not one of {list(REQUIRED_VALIDATION_KINDS)}",
            )
        raw_id = item.get("id")
        case_id = _safe_generated_basename(
            f"{kind}-{index}" if raw_id is None else raw_id,
            "validation case id",
        )
        if case_id in case_ids:
            raise StageFailure(
                "BUILDER_CONTRACT_ERROR",
                f"validation case id {case_id!r} is duplicated",
            )
        case_ids.add(case_id)
        entry = dict(item)
        entry["id"] = case_id
        for field_name in ("candidate", "patch"):
            value = entry.get(field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise StageFailure(
                    "BUILDER_CONTRACT_ERROR",
                    f"validation case {case_id!r} {field_name} must be a non-empty string",
                )
        cases.append(entry)

    missing = [
        kind for kind in REQUIRED_VALIDATION_KINDS if not any(c["kind"] == kind for c in cases)
    ]
    if missing:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"validation-cases.json is missing mandatory cases: {', '.join(missing)}",
        )
    order = {kind: index for index, kind in enumerate(REQUIRED_VALIDATION_KINDS)}
    cases.sort(key=lambda case: order.get(str(case["kind"]), len(order)))
    return cases


def _hash_validation_path(path: Path) -> str | None:
    return sha256_path(path, excludes=()) if path.exists() else None


def _validation_input_hashes(
    ctx: _SourceContext,
    cases: Sequence[Mapping[str, Any]],
    export_root: Path,
) -> dict[str, Any]:
    references: dict[str, str | None] = {}
    for case in cases:
        case_id = str(case["id"])
        for field_name in ("candidate", "patch"):
            declared = case.get(field_name)
            if isinstance(declared, str) and declared:
                referenced = _resolve_export_reference(
                    export_root,
                    declared,
                    case_id=case_id,
                    field=field_name,
                )
                references[f"{case_id}:{field_name}:{declared}"] = _hash_validation_path(
                    referenced
                )

    reference_root = _contained_child(
        export_root, "reference", "validation reference directory"
    )
    source_states: dict[str, str | None] = {}
    for state in ("base", "outcome"):
        tree = ctx.source_dir / "repo" / state
        archive = ctx.source_dir / "repo" / f"{state}.bundle"
        source_states[f"{state}_tree"] = _hash_validation_path(tree)
        source_states[f"{state}_bundle"] = _hash_validation_path(archive)
    return {
        "references": dict(sorted(references.items())),
        "reference_files": _hash_validation_path(reference_root),
        "source_states": source_states,
    }


def _load_isolation_attestation(
    run_dir: Path,
    *,
    task_id: str,
    attempt_id: str,
    status: str,
    task_sha256: str,
    package_sha256: str,
    mode: str,
) -> dict[str, Any]:
    try:
        return validate_scorer_run_attestation(
            run_dir / SCORER_RUN_REPORT_NAME,
            task_id=task_id,
            attempt_id=attempt_id,
            status=status,
            task_sha256=task_sha256,
            scorer_package_sha256=package_sha256,
            mode=mode,
            run_dir=run_dir,
        )
    except GhostlabError as error:
        raise StageFailure("SCORER_UNSAFE", str(error), task_id=task_id) from error


def _verify_recorded_isolation_attestations(
    validation_dir: Path,
    validation: ScorerValidation,
    *,
    task_sha256: str,
    package_sha256: str,
    mode: str,
) -> dict[str, Any]:
    attestations: list[dict[str, Any]] = []
    for case in validation.cases:
        for index in range(case.runs):
            report_path = _contained_child(
                validation_dir / "reports",
                f"{case.case_id}.run{index}.json",
                "validation report name",
            )
            try:
                report = read_json(report_path, label="cached validation score report")
            except GhostlabError as error:
                raise StageFailure(
                    "SCORER_UNSAFE", str(error), task_id=validation.task_id
                ) from error
            if not isinstance(report, Mapping):
                raise StageFailure(
                    "SCORER_UNSAFE",
                    f"cached validation score report {report_path} must be an object",
                    task_id=validation.task_id,
                )
            run_dir = _contained_child(
                validation_dir / "runs",
                f"{case.case_id}-{index}",
                "validation run name",
            )
            observed = _load_isolation_attestation(
                run_dir,
                task_id=validation.task_id,
                attempt_id=str(report.get("attempt_id", "")),
                status=str(report.get("status", "")),
                task_sha256=task_sha256,
                package_sha256=package_sha256,
                mode=mode,
            )
            attestations.append(observed)
    expected = attestations[0] if attestations else None
    if (
        expected is None
        or any(item != expected for item in attestations[1:])
        or expected != dict(validation.isolation_attestation)
    ):
        raise StageFailure(
            "SCORER_UNSAFE",
            "recorded validation runs do not prove the published isolation attestation",
            task_id=validation.task_id,
        )
    return expected


def _stage_scorer_validated(
    ctx: _SourceContext,
    task: Mapping[str, Any],
    scorer_dir: Path,
    scorer_manifest: Mapping[str, Any],
    package_sha256: str,
    cases_path: Path,
    public_task_path: Path,
    previous: str | None = None,
) -> tuple[ScorerValidation, str]:
    """Execute the six mandatory cases plus the repeatability protocol."""
    config = ctx.config
    task_id = str(task["task_id"])
    mode = str(scorer_manifest["mode"])
    validation_dir = ctx.paths.build_task_dir(ctx.build_id, task_id) / "scorer-validation"
    cases = _load_validation_cases(cases_path, task_id)
    export_root = cases_path.parent
    fingerprint = sha256_json(
        {
            "stage": "scorer_validated",
            "package": package_sha256,
            "cases": sha256_file(cases_path),
            "case_inputs": _validation_input_hashes(ctx, cases, export_root),
            "public_task": sha256_file(public_task_path),
            "repeats": max(1, config.repeatability_runs),
            "config": config.fingerprint(),
        }
    )
    cached = validation_dir / "scorer-validation.json"
    if previous == fingerprint and cached.is_file():
        stored = read_json(cached, label="scorer-validation.json")
        if isinstance(stored, Mapping):
            validation = _load_validation(stored)
            _verify_recorded_isolation_attestations(
                validation_dir,
                validation,
                task_sha256=sha256_file(public_task_path),
                package_sha256=package_sha256,
                mode=mode,
            )
            return validation, fingerprint

    scratch = _contained_child(validation_dir, "candidates", "validation scratch directory")
    scratch.mkdir(parents=True, exist_ok=True)

    scorer_json = scorer_dir / "scorer.json"
    repeats = max(1, config.repeatability_runs)
    reports_dir = _contained_child(validation_dir, "reports", "validation reports directory")
    runs_dir = _contained_child(validation_dir, "runs", "validation runs directory")
    reports_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    reports_by_case: dict[str, list[dict[str, Any]]] = {}
    isolation_attestations: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["id"])
        candidate = _materialize_case_candidate(
            ctx, case, export_root, scratch, case_id, str(case["kind"])
        )
        runs: list[dict[str, Any]] = []
        for index in range(repeats):
            report_path = _contained_child(
                reports_dir,
                f"{case_id}.run{index}.json",
                "validation report name",
            )
            run_path = _contained_child(
                runs_dir,
                f"{case_id}-{index}",
                "validation run name",
            )
            private_run_report = run_path / "scorer-run.json"
            if private_run_report.is_symlink() or private_run_report.is_file():
                private_run_report.unlink()
            elif private_run_report.exists():
                shutil.rmtree(private_run_report)
            result = config.ghostlab.scorer_run(
                ScorerRunRequest(
                    task_path=public_task_path,
                    scorer_path=scorer_json,
                    candidate_path=candidate,
                    output_path=report_path,
                    attempt_id=f"validation-{task_id}-{case_id}-{index}",
                    seed=0,
                    run_dir=run_path,
                    timeout_seconds=config.scorer_timeout_seconds,
                    label=f"scorer-{case_id}-{index}",
                )
            )
            isolation_attestations.append(
                _load_isolation_attestation(
                    run_path,
                    task_id=task_id,
                    attempt_id=str(result.report.get("attempt_id", "")),
                    status=result.status,
                    task_sha256=sha256_file(public_task_path),
                    package_sha256=package_sha256,
                    mode=mode,
                )
            )
            runs.append(dict(result.report))
            if result.status == "scored":
                report_check = validate_score_report(
                    result.report,
                    pass_threshold=float(scorer_manifest["pass_threshold"]),
                    scorer_manifest=scorer_manifest,
                    expected_task_id=task_id,
                    expected_scorer_package_sha256=package_sha256,
                )
                if not report_check.valid:
                    raise StageFailure(
                        "BUILDER_CONTRACT_ERROR",
                        f"validation case {case_id!r} produced an invalid score report: "
                        + "; ".join(report_check.errors),
                        task_id=task_id,
                    )
        reports_by_case[case_id] = runs

    oracle_case = next(case for case in cases if case["kind"] == "oracle")
    oracle_reports = reports_by_case[str(oracle_case["id"])]
    oracle_total_raw = oracle_reports[0].get("score_total")
    oracle_total = (
        float(oracle_total_raw)
        if isinstance(oracle_total_raw, (int, float)) and not isinstance(oracle_total_raw, bool)
        else None
    )
    oracle_components = _component_values(oracle_reports[0])
    hard_gates = _hard_gate_ids(scorer_manifest)

    results: list[ValidationCaseResult] = []
    for case in cases:
        case_id = str(case["id"])
        targeted = case.get("component")
        if not isinstance(targeted, str):
            targeted = (
                hard_gates[0]
                if hard_gates and case["kind"] in ("construct_changing", "regression")
                else None
            )
        results.append(
            evaluate_validation_case(
                str(case["kind"]),
                case_id,
                reports_by_case[case_id],
                oracle_total=oracle_total,
                targeted_component=targeted,
                oracle_components=oracle_components,
            )
        )

    repeatability = evaluate_repeatability(
        reports_by_case,
        _component_kinds(scorer_manifest),
        hard_gates,
        expected_repeats=repeats,
    )
    codes = tuple(
        dict.fromkeys(
            [case.code for case in results if case.code]
            + ([] if repeatability.ok else ["SCORER_NONDETERMINISTIC"])
        )
    )
    validation = ScorerValidation(
        task_id=task_id,
        passed=all(case.ok for case in results) and repeatability.ok,
        cases=tuple(results),
        repeatability=repeatability,
        codes=codes,
        scorer_package_sha256=package_sha256,
        isolation_attestation=(
            isolation_attestations[0] if isolation_attestations else {}
        ),
    )
    _verify_recorded_isolation_attestations(
        validation_dir,
        validation,
        task_sha256=sha256_file(public_task_path),
        package_sha256=package_sha256,
        mode=mode,
    )
    write_json(validation_dir / "scorer-validation.json", validation.to_dict())
    return validation, fingerprint


def _agent_model(config_path: Path) -> str | None:
    try:
        payload = read_json(config_path, label="agent config")
    except GhostlabError:
        return None
    if isinstance(payload, Mapping):
        runtime = payload.get("runtime")
        if isinstance(runtime, Mapping) and isinstance(runtime.get("model"), str):
            return runtime["model"]
    return None


def _stage_audited(
    ctx: _SourceContext,
    task: Mapping[str, Any],
    scorer_dir: Path,
    validation: ScorerValidation,
    public_task_path: Path,
    previous: str | None = None,
) -> tuple[dict[str, Any], str, list[str]]:
    """Run the independent scorer auditor with a hashed, immutable input tree."""
    config = ctx.config
    task_id = str(task["task_id"])
    if config.scorer_auditor_agent is None:
        if config.require_audit:
            raise BuildConfigurationError(
                "require_audit=True but BuildConfig.scorer_auditor_agent is None; pass "
                "--scorer-auditor-agent or set require_audit=False"
            )
        return (
            {"decision": "accept", "skipped": True, "reason": "no auditor agent configured"},
            sha256_text(f"audit:skipped:{task_id}"),
            [f"{task_id}: scorer audit skipped; no auditor agent configured"],
        )

    task_dir = ctx.paths.build_task_dir(ctx.build_id, task_id)
    input_dir = task_dir / "scorer-audit-input"
    run_dir = task_dir / "audited"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = _write_text(run_dir / "prompt.md", scorer_auditor_prompt(task_id))
    contract = config.audit_contract()
    instructions, warnings = agent_instruction_hashes(config.scorer_auditor_agent)
    request = ArtifactRunRequest(
        agent_config=config.scorer_auditor_agent,
        workspace=input_dir,
        prompt_file=prompt_path,
        run_dir=run_dir,
        exports=(
            ExportSpec("/sandbox/output/audit.json", "audit.json"),
            ExportSpec("/sandbox/output/mutants", "mutants", required=False),
        ),
        output_contract=contract,
        timeout_seconds=config.auditor_timeout_seconds,
        label="scorer-auditor",
    )
    fingerprint = sha256_json(
        {
            "stage": "audited",
            "package": validation.scorer_package_sha256,
            "validation": sha256_json(validation.to_dict()),
            "agent": sha256_file(config.scorer_auditor_agent),
            "instructions": instructions,
            "prompt": sha256_file(prompt_path),
            "contract": sha256_file(contract),
            "config": config.fingerprint(),
        }
    )
    builder_model = _agent_model(config.scorer_builder_agent)
    auditor_model = _agent_model(config.scorer_auditor_agent)
    if builder_model and auditor_model and builder_model == auditor_model:
        warnings.append(
            f"{task_id}: scorer builder and auditor share model {builder_model!r}; the spec asks "
            "for disjoint model families when one is available"
        )

    cached_audit = run_dir / "audit.json"
    if previous == fingerprint and cached_audit.is_file():
        audit_path: Path | None = cached_audit
    else:
        if input_dir.exists():
            shutil.rmtree(input_dir)
        input_dir.mkdir(parents=True, exist_ok=True)
        write_json(input_dir / "task.json", dict(task))
        shutil.copyfile(public_task_path, input_dir / "public-task.json")
        _copy_tree(scorer_dir, input_dir / "scorer")
        write_json(input_dir / "scorer-validation.json", validation.to_dict())
        _copy_tree(ctx.source_dir, input_dir / "source")
        result = config.ghostlab.artifact_run(request)
        check_builder_contract(result, stage="audited")
        audit_path = result.export_path("audit.json")
    if audit_path is None:
        raise StageFailure("BUILDER_CONTRACT_ERROR", "ScorerAuditor exported no audit.json")
    audit = read_json(audit_path, label="audit.json")
    if not isinstance(audit, dict):
        raise StageFailure("BUILDER_CONTRACT_ERROR", "audit.json is not a JSON object")
    contract_errors = packaged_contract_errors(audit, SCORER_AUDIT_CONTRACT, where="audit.json")
    if contract_errors:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR",
            f"audit.json violates {contract}: " + "; ".join(contract_errors[:6]),
            task_id=task_id,
        )
    decision = audit.get("decision")
    if decision not in {"accept", "revise", "reject"}:
        raise StageFailure(
            "BUILDER_CONTRACT_ERROR", f"audit.json decision={decision!r} is not accept|revise|reject"
        )
    if decision == "accept":
        leakage = audit.get("leakage")
        missing_observables = audit.get("missing_observables")
        audit_failures: list[str] = []
        if isinstance(leakage, list) and leakage:
            audit_failures.append(f"leakage={leakage!r}")
        if isinstance(missing_observables, list) and missing_observables:
            audit_failures.append(f"missing_observables={missing_observables!r}")
        if audit_failures:
            raise StageFailure(
                "SCORER_UNSAFE" if leakage else "NO_OBSERVABLE_OUTCOME",
                "scorer audit cannot accept with " + "; ".join(audit_failures),
                task_id=task_id,
            )
    if decision != "accept":
        code = audit.get("code")
        raise StageFailure(
            str(code) if isinstance(code, str) and code else "SCORER_OVERFIT",
            f"scorer audit returned decision={decision!r}: "
            f"{audit.get('detail') or audit.get('evidence') or ''}",
            task_id=task_id,
        )
    return audit, fingerprint, warnings


def _assert_public_clean(public_dir: Path, forbidden: Sequence[str]) -> None:
    present = sorted(path.name for path in public_dir.iterdir())
    if present != sorted(PUBLIC_TASK_FILES):
        raise StageFailure(
            "HARNESS_ERROR",
            f"published public/ contains {present}, expected exactly {sorted(PUBLIC_TASK_FILES)}",
        )
    hits = sorted(
        {
            hit
            for name in PUBLIC_TASK_FILES
            if name != "base.bundle"
            for hit in _scan_for_substrings(public_dir / name, forbidden)
        }
    )
    if hits:
        raise StageFailure(
            "HARNESS_ERROR",
            "published public/ leaks private oracle material: " + ", ".join(hits),
        )


def _agent_dependency_hashes(agent_config: Path) -> dict[str, str]:
    payload = read_json(agent_config, label="agent config")
    if not isinstance(payload, Mapping):
        return {}
    references: dict[str, tuple[bool, bool]] = {}

    def record(reference: str, *, allow_packaged: bool, include_parent: bool) -> None:
        prior = references.get(reference, (False, False))
        references[reference] = (
            prior[0] or allow_packaged,
            prior[1] or include_parent,
        )

    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        instructions = runtime.get("instructions")
        if isinstance(instructions, list):
            for item in instructions:
                if isinstance(item, str):
                    record(item, allow_packaged=True, include_parent=False)
    inputs = payload.get("inputs")
    if isinstance(inputs, Mapping):
        for kind in ("skills", "mcps", "assets"):
            declared = inputs.get(kind)
            if not isinstance(declared, list):
                continue
            for item in declared:
                if isinstance(item, str):
                    record(
                        item,
                        allow_packaged=False,
                        include_parent=kind == "skills",
                    )
                elif isinstance(item, Mapping):
                    for key in ("path", "source", "config_ref"):
                        value = item.get(key)
                        if isinstance(value, str):
                            record(
                                value,
                                allow_packaged=False,
                                include_parent=kind == "skills" and key == "path",
                            )
    sandbox = payload.get("sandbox")
    if isinstance(sandbox, Mapping) and isinstance(sandbox.get("policy"), str):
        record(sandbox["policy"], allow_packaged=False, include_parent=False)

    dependencies: dict[str, str] = {}
    for reference, (allow_packaged, include_parent) in sorted(references.items()):
        declared = Path(reference)
        if declared.is_absolute():
            resolved = declared
        else:
            local = agent_config.parent / declared
            packaged = INSTRUCTION_ASSET_DIR / declared.name
            resolved = local if local.exists() or not allow_packaged else packaged
        if include_parent and resolved.is_file() and resolved.name == "SKILL.md":
            resolved = resolved.parent
        dependencies[reference] = (
            sha256_path(resolved, excludes=()) if resolved.exists() else ""
        )
    return dependencies


def _agent_asset_record(agent_config: Path | None) -> dict[str, Any] | None:
    if agent_config is None:
        return None
    instructions, _ = agent_instruction_hashes(agent_config)
    return {
        "config": agent_config.name,
        "config_sha256": sha256_file(agent_config),
        "instructions": dict(sorted(instructions.items())),
        "dependencies": _agent_dependency_hashes(agent_config),
    }


def build_asset_record(config: BuildConfig) -> dict[str, Any]:
    """Exact instruction and JSON Schema assets used by one build."""
    return {
        "packaged_instructions": packaged_instruction_index(),
        "contracts": {
            TASK_DEFINITIONS_CONTRACT: sha256_file(config.definitions_contract()),
            SCORER_AUDIT_CONTRACT: sha256_file(config.audit_contract()),
            SCORE_REPORT_CONTRACT: sha256_file(schema_path(SCORE_REPORT_CONTRACT)),
        },
        "agents": {
            "task_definer": _agent_asset_record(config.task_definer_agent),
            "scorer_builder": _agent_asset_record(config.scorer_builder_agent),
            "scorer_auditor": _agent_asset_record(config.scorer_auditor_agent),
        },
    }


def _stage_published(
    ctx: _SourceContext,
    task: Mapping[str, Any],
    scorer_dir: Path,
    package_sha256: str,
    validation: ScorerValidation,
    audit: Mapping[str, Any],
    public_task: Mapping[str, Any],
    stage_fingerprints: Mapping[str, str],
) -> tuple[Path, str]:
    """Publish public/private task material atomically with a leak check."""
    task_id = str(task["task_id"])
    repo = _mapping(ctx.manifest.get("repo"))
    scorer_forbidden = tuple(ctx.config.forbidden_scorer_substrings) + (
        str(ctx.source_dir),
    )
    current_manifest, current_package_sha256, _ = validate_scorer_package(
        scorer_dir,
        task_id,
        forbidden_substrings=scorer_forbidden,
    )
    if current_package_sha256 != package_sha256:
        raise StageFailure(
            "SCORER_UNSAFE",
            "scorer package changed after validation and before publication",
            task_id=task_id,
        )
    if validation.scorer_package_sha256 != package_sha256:
        raise StageFailure(
            "SCORER_UNSAFE",
            "scorer validation is not bound to the package being published",
            task_id=task_id,
        )
    _verify_recorded_isolation_attestations(
        ctx.paths.build_task_dir(ctx.build_id, task_id) / "scorer-validation",
        validation,
        task_sha256=sha256_file(
            ctx.paths.build_task_dir(ctx.build_id, task_id) / "public-task.json"
        ),
        package_sha256=package_sha256,
        mode=str(current_manifest["mode"]),
    )
    target = ctx.paths.task_dir(task_id)
    fingerprint = sha256_json(
        {
            "stage": "published",
            "task": sha256_json(dict(task)),
            "public_task": sha256_json(dict(public_task)),
            "package": package_sha256,
            "validation": sha256_json(validation.to_dict()),
            "audit": sha256_json(dict(audit)),
            "bundle": ctx.bundle_sha256,
        }
    )
    existing = target / "private" / "provenance.json"
    if existing.is_file():
        payload = read_json(existing, label="published provenance")
        if isinstance(payload, Mapping) and payload.get("publication_sha256") == fingerprint:
            return target, fingerprint

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{task_id}.publish.", dir=target.parent))
    try:
        public = staging / "public"
        private = staging / "private"
        public.mkdir(parents=True, exist_ok=True)
        private.mkdir(parents=True, exist_ok=True)

        write_json(public / "task.json", dict(public_task))
        _write_text(public / "prompt.txt", str(task.get("prompt", "")))
        environment = dict(ctx.environment)
        environment.pop("outcome_sha", None)
        write_json(public / "environment.json", environment)
        materialize_repo_bundle(ctx.source_dir, "base", public / "base.bundle")

        forbidden = [
            str(repo.get("outcome_sha") or ""),
            str(repo.get("outcome_tree") or ""),
            str(repo.get("root_at_capture") or ""),
            str(ctx.source_dir),
            *ctx.config.forbidden_public_substrings,
        ]
        _assert_public_clean(public, [item for item in forbidden if item])

        _copy_tree(scorer_dir, private / "scorer")
        write_json(private / "scorer-validation.json", validation.to_dict())
        materialize_repo_bundle(ctx.source_dir, "outcome", private / "oracle.bundle")
        write_json(
            private / "source-link.json",
            {
                "source_id": ctx.source_id,
                "source_dir": str(ctx.source_dir),
                "manifest_sha256": sha256_file(ctx.source_dir / "manifest.json"),
                "repo": dict(repo),
            },
        )
        write_json(
            private / "provenance.json",
            {
                "schema_version": PROVENANCE_SCHEMA,
                "task_id": task_id,
                "source_id": ctx.source_id,
                "candidate_id": task.get("candidate_id"),
                "build_id": ctx.build_id,
                "kind": task.get("kind", "replay"),
                "task_definition": dict(task),
                "scorer_package_sha256": package_sha256,
                "base_bundle_sha256": sha256_file(public / "base.bundle"),
                "oracle_bundle_sha256": sha256_file(private / "oracle.bundle"),
                "public_task_sha256": sha256_file(public / "task.json"),
                "validation": validation.to_dict(),
                "audit": dict(audit),
                "stage_fingerprints": dict(sorted(stage_fingerprints.items())),
                "assets": build_asset_record(ctx.config),
                "ghostlab": ctx.config.ghostlab.version().fingerprint(),
                "publication_sha256": fingerprint,
                "published_at": utc_now(),
            },
        )
        publish_directory(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target, fingerprint


def _load_validation(payload: Mapping[str, Any]) -> ScorerValidation:
    cases = tuple(
        ValidationCaseResult(
            case_id=str(item.get("case_id", "")),
            kind=str(item.get("kind", "")),
            status=str(item.get("status", "")),
            ok=bool(item.get("ok")),
            score_total=item.get("score_total"),
            passed=item.get("passed") if isinstance(item.get("passed"), bool) else None,
            hard_gate_failures=tuple(str(x) for x in item.get("hard_gate_failures") or ()),
            component_values=dict(item.get("component_values") or {}),
            runs=int(item.get("runs") or 0),
            code=item.get("code") if isinstance(item.get("code"), str) else None,
            detail=str(item.get("detail", "")),
        )
        for item in payload.get("cases") or ()
        if isinstance(item, Mapping)
    )
    repeat_raw = payload.get("repeatability") or {}
    repeatability = RepeatabilityResult(
        ok=bool(repeat_raw.get("ok")),
        deterministic_mismatches=tuple(
            str(x) for x in repeat_raw.get("deterministic_mismatches") or ()
        ),
        performance_spread=repeat_raw.get("performance_spread"),
        judge_stdev=dict(repeat_raw.get("judge_stdev") or {}),
        detail=str(repeat_raw.get("detail", "")),
    )
    isolation = payload.get("isolation_attestation")
    return ScorerValidation(
        task_id=str(payload.get("task_id", "")),
        passed=bool(payload.get("passed")),
        cases=cases,
        repeatability=repeatability,
        codes=tuple(str(x) for x in payload.get("codes") or ()),
        scorer_package_sha256=str(payload.get("scorer_package_sha256", "")),
        isolation_attestation=dict(isolation) if isinstance(isolation, Mapping) else {},
    )


def _definer_rejections(source_id: str, definitions: Mapping[str, Any]) -> list[Rejection]:
    rejections: list[Rejection] = []
    for item in definitions.get("rejections") or ():
        if not isinstance(item, Mapping):
            continue
        rejections.append(
            Rejection(
                source_id=source_id,
                stage="task_generated",
                code=str(item.get("code", "NO_STABLE_GOAL")),
                detail=str(item.get("detail", "")),
            )
        )
    return rejections


def build_source(
    paths: TasksetPaths,
    config: BuildConfig,
    source_id: str,
    *,
    build_id: str,
    source_dir: Path | None = None,
    expected_bundle_sha256: str | None = None,
) -> SourceBuildResult:
    """Drive one source through the resumable stage machine."""
    resolved_source = source_dir or paths.source_dir(source_id)
    work_dir = paths.build_source_dir(build_id, source_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    store = StageStore(paths.stage_path(build_id, source_id))
    state = store.load(source_id)
    rejections: list[Rejection] = []
    reused: list[str] = []
    published: list[str] = []

    stage = "selected"
    try:
        fingerprint, warnings = _stage_selected(resolved_source, source_id)
        if state.reached("selected", fingerprint):
            reused.append("selected")
        state = store.save(state.advance("selected", fingerprint, warnings=warnings))

        stage = "bundled"
        manifest, bundle_sha256, warnings = _stage_bundled(resolved_source)
        if (
            expected_bundle_sha256 is not None
            and bundle_sha256 != expected_bundle_sha256
        ):
            raise StageFailure(
                "HARNESS_ERROR",
                f"source bundle {source_id!r} checksum {bundle_sha256} does not match "
                f"the active bundle report checksum {expected_bundle_sha256}",
            )
        if state.reached("bundled", bundle_sha256):
            reused.append("bundled")
        state = store.save(state.advance("bundled", bundle_sha256, warnings=warnings))

        environment, env_warnings = load_environment(resolved_source, manifest)
        ctx = _SourceContext(
            paths=paths,
            config=config,
            build_id=build_id,
            source_id=source_id,
            source_dir=resolved_source,
            work_dir=work_dir,
            manifest=manifest,
            bundle_sha256=bundle_sha256,
            environment=environment,
        )

        stage = "task_generated"
        definitions, run, fingerprint, definer_warnings = _stage_task_generated(
            ctx, state.fingerprints.get("task_generated")
        )
        if run is None:
            reused.append("task_generated")
        state = state.advance(
            "task_generated",
            fingerprint,
            artifacts={"task_definitions": "task-generated/task-definitions.json"},
            warnings=tuple(env_warnings) + tuple(definer_warnings),
        )
        for rejection in _definer_rejections(source_id, definitions):
            rejections.append(rejection)
            state = state.with_rejection(rejection)
        state = store.save(state)

        stage = "task_linted"
        accepted, lint_rejections, fingerprint = _stage_task_linted(ctx, definitions)
        state = state.advance("task_linted", fingerprint)
        for rejection in lint_rejections:
            rejections.append(rejection)
            state = state.with_rejection(rejection)
        state = store.save(state)
    except StageFailure as exc:
        rejection = Rejection(
            source_id=source_id, stage=stage, code=exc.code, detail=exc.detail
        )
        rejections.append(rejection)
        state = store.save(state.with_rejection(rejection).fail(stage, exc.code, exc.detail))
        return SourceBuildResult(source_id, state, (), tuple(rejections), tuple(reused))
    except GhostlabError as exc:
        rejection = Rejection(
            source_id=source_id, stage=stage, code="HARNESS_ERROR", detail=str(exc)
        )
        rejections.append(rejection)
        state = store.save(state.with_rejection(rejection).fail(stage, "HARNESS_ERROR", str(exc)))
        return SourceBuildResult(source_id, state, (), tuple(rejections), tuple(reused))

    for task in accepted:
        task_id = str(task["task_id"])
        candidate_id = str(task.get("candidate_id", task_id))
        prior = state.tasks.get(task_id)
        fingerprints: dict[str, str] = dict(prior.fingerprints) if prior else {}
        task_stage = "scorer_built"
        try:
            scorer_dir, scorer_manifest, package_sha256, built_run, cases_path, fingerprint, warns = (
                _stage_scorer_built(ctx, task, fingerprints.get("scorer_built"))
            )
            if built_run is None:
                reused.append(f"{task_id}:scorer_built")
            fingerprints["scorer_built"] = fingerprint
            if warns:
                state = replace(
                    state, warnings=tuple(dict.fromkeys(state.warnings + tuple(warns)))
                )

            public_task = build_public_task(task, manifest, environment, scorer_manifest)
            public_task_path = paths.build_task_dir(build_id, task_id) / "public-task.json"
            write_json(public_task_path, public_task)

            task_stage = "scorer_validated"
            validation, fingerprint = _stage_scorer_validated(
                ctx,
                task,
                scorer_dir,
                scorer_manifest,
                package_sha256,
                cases_path,
                public_task_path,
                fingerprints.get("scorer_validated"),
            )
            fingerprints["scorer_validated"] = fingerprint
            if not validation.passed:
                code = validation.codes[0] if validation.codes else "SCORER_OVERFIT"
                detail = "; ".join(
                    f"{case.kind}: {case.detail}" for case in validation.cases if not case.ok
                ) or validation.repeatability.detail
                raise StageFailure(code, detail or "scorer validation failed", task_id=task_id)

            task_stage = "audited"
            audit, fingerprint, warns = _stage_audited(
                ctx,
                task,
                scorer_dir,
                validation,
                public_task_path,
                fingerprints.get("audited"),
            )
            fingerprints["audited"] = fingerprint
            if warns:
                state = replace(
                    state, warnings=tuple(dict.fromkeys(state.warnings + tuple(warns)))
                )

            task_stage = "published"
            target, fingerprint = _stage_published(
                ctx,
                task,
                scorer_dir,
                package_sha256,
                validation,
                audit,
                public_task,
                {**state.fingerprints, **fingerprints},
            )
            fingerprints["published"] = fingerprint
            published.append(task_id)
            state = state.with_task(
                TaskStageState(
                    task_id=task_id,
                    candidate_id=candidate_id,
                    stage="published",
                    status="ok",
                    fingerprints=fingerprints,
                    artifacts={"task_dir": str(target), "scorer_package_sha256": package_sha256},
                )
            )
            state = store.save(state)
        except (StageFailure, GhostlabError) as exc:
            code = exc.code if isinstance(exc, StageFailure) else "HARNESS_ERROR"
            detail = exc.detail if isinstance(exc, StageFailure) else str(exc)
            rejection = Rejection(
                source_id=source_id,
                stage=task_stage,
                code=code,
                detail=detail,
                task_id=task_id,
                candidate_id=candidate_id,
            )
            rejections.append(rejection)
            state = state.with_rejection(rejection).with_task(
                TaskStageState(
                    task_id=task_id,
                    candidate_id=candidate_id,
                    stage=task_stage,
                    status="rejected",
                    fingerprints=fingerprints,
                    rejection=rejection.to_dict(),
                )
            )
            state = store.save(state)

    if published:
        state = store.save(
            state.advance(
                "published",
                sha256_json({"tasks": sorted(published), "bundle": bundle_sha256}),
                artifacts={"published_tasks": ",".join(sorted(published))},
            )
        )
    return SourceBuildResult(
        source_id=source_id,
        state=state,
        published_task_ids=tuple(published),
        rejections=tuple(rejections),
        reused_stages=tuple(reused),
    )


@dataclass(frozen=True)
class _BuildSource:
    source_id: str
    path: Path
    content_sha256: str


def _verified_build_source(
    paths: TasksetPaths,
    source_id: str,
    *,
    expected_sha256: str | None = None,
) -> _BuildSource:
    _validate_identifier(source_id, "source id")
    source_path = paths.source_dir(source_id)
    if source_path.is_symlink():
        raise BuildConfigurationError(
            f"source bundle {source_id!r} must not be a symbolic link"
        )
    try:
        bundle = load_bundle(source_path)
        verified = verify_bundle(source_path)
    except (BundleError, GhostlabError, OSError, SchemaError, ValueError) as error:
        raise BuildConfigurationError(
            f"source bundle {source_id!r} cannot be verified: {error}"
        ) from error
    if bundle.source_id != source_id:
        raise BuildConfigurationError(
            f"source bundle directory {source_id!r} contains manifest for "
            f"{bundle.source_id!r}"
        )
    if not verified:
        raise BuildConfigurationError(
            f"source bundle {source_id!r} failed checksum verification"
        )
    digest = bundle.content_sha256
    if expected_sha256 is not None and digest != expected_sha256:
        raise BuildConfigurationError(
            f"source bundle {source_id!r} checksum {digest} does not match "
            f"the active bundle report checksum {expected_sha256}"
        )
    return _BuildSource(source_id=source_id, path=source_path, content_sha256=digest)


def _explicit_build_source(paths: TasksetPaths, source_id: str) -> _BuildSource:
    _validate_identifier(source_id, "source id")
    source_path = paths.source_dir(source_id)
    if source_path.is_symlink() or not source_path.is_dir():
        raise BuildConfigurationError(f"source bundle does not exist: {source_path}")
    try:
        digest = compute_content_hash(source_path)
    except (BundleError, OSError, ValueError) as error:
        raise BuildConfigurationError(
            f"source bundle {source_id!r} cannot be hashed: {error}"
        ) from error
    return _BuildSource(source_id=source_id, path=source_path, content_sha256=digest)


def _active_build_sources(paths: TasksetPaths) -> list[_BuildSource]:
    report_path = paths.bundle_report_path()
    if report_path.is_symlink() or not report_path.is_file():
        raise BuildConfigurationError(
            f"active bundle report is missing at {report_path}; run "
            f"'retro benchmark taskset bundle --name {paths.name}' first or pass source_ids"
        )
    try:
        payload = read_json(report_path, label="active bundle report")
    except GhostlabError as error:
        raise BuildConfigurationError(str(error)) from error
    if not isinstance(payload, Mapping):
        raise BuildConfigurationError("active bundle report is not a JSON object")
    if payload.get("schema_version") != BUNDLE_REPORT_SCHEMA:
        raise BuildConfigurationError(
            "active bundle report declares "
            f"schema_version={payload.get('schema_version')!r}, "
            f"expected {BUNDLE_REPORT_SCHEMA!r}"
        )
    if payload.get("name") != paths.name:
        raise BuildConfigurationError(
            f"active bundle report is for {payload.get('name')!r}, not {paths.name!r}"
        )
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise BuildConfigurationError("active bundle report sources must be an array")

    active: list[_BuildSource] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sources):
        where = f"active bundle report sources[{index}]"
        if not isinstance(item, Mapping):
            raise BuildConfigurationError(f"{where} must be an object")
        source_id = item.get("source_id")
        status = item.get("status")
        if not isinstance(source_id, str):
            raise BuildConfigurationError(f"{where}.source_id must be a string")
        _validate_identifier(source_id, "source id")
        if source_id in seen:
            raise BuildConfigurationError(
                f"active bundle report contains duplicate source id {source_id!r}"
            )
        seen.add(source_id)
        if status == "skipped":
            continue
        if status not in ("bundled", "reused"):
            raise BuildConfigurationError(
                f"{where}.status={status!r} is not bundled, reused, or skipped"
            )
        expected = item.get("content_sha256")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BuildConfigurationError(
                f"{where}.content_sha256 must be a lowercase SHA-256 digest"
            )
        reported_path = item.get("path")
        source_path = paths.source_dir(source_id)
        if not isinstance(reported_path, str) or not reported_path:
            raise BuildConfigurationError(f"{where}.path must identify the source bundle")
        if Path(reported_path).resolve() != source_path.resolve():
            raise BuildConfigurationError(
                f"{where}.path={reported_path!r} does not identify the current source "
                f"bundle {source_path}"
            )
        active.append(
            _verified_build_source(
                paths, source_id, expected_sha256=expected
            )
        )
    return sorted(active, key=lambda item: item.source_id)


def default_build_id(
    name: str,
    config: BuildConfig,
    source_ids: Sequence[str],
    *,
    source_digests: Mapping[str, str] | None = None,
) -> str:
    """Return an id covering every configured behavior and verified source digest."""
    if source_digests is None:
        raise BuildConfigurationError(
            "default build ids require verified source bundle digests"
        )
    ids = sorted(source_ids)
    if set(source_digests) != set(ids):
        raise BuildConfigurationError(
            "source_digests must contain exactly the source ids used by the build"
        )
    for source_id in ids:
        digest = source_digests[source_id]
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise BuildConfigurationError(
                f"source bundle {source_id!r} has invalid digest {digest!r}"
            )
    digest = sha256_json(
        {
            "name": name,
            "config": config.fingerprint(),
            "agents": {
                "task_definer": _agent_asset_record(config.task_definer_agent),
                "scorer_builder": _agent_asset_record(config.scorer_builder_agent),
                "scorer_auditor": _agent_asset_record(config.scorer_auditor_agent),
            },
            "sources": [
                {"source_id": source_id, "content_sha256": source_digests[source_id]}
                for source_id in ids
            ],
        }
    )
    return f"build-{digest[:16]}"


def build_sources(
    paths: TasksetPaths,
    config: BuildConfig,
    source_ids: Iterable[str] | None = None,
    *,
    build_id: str | None = None,
) -> BuildResult:
    """Build every selected source into published tasks (spec section 14.3).

    Without an explicit source list, the latest ``bundles.json`` is authoritative;
    stale directories left under ``sources/`` are never rediscovered implicitly.
    ``build_taskset`` is the CLI-facing wrapper.
    """
    if source_ids is None:
        resolved_sources = _active_build_sources(paths)
    else:
        resolved_ids = sorted(set(source_ids))
        for source_id in resolved_ids:
            _validate_identifier(source_id, "source id")
        resolved_sources = [
            _explicit_build_source(paths, source_id) for source_id in resolved_ids
        ]
    resolved_ids = [source.source_id for source in resolved_sources]
    for source_id in resolved_ids:
        _validate_identifier(source_id, "source id")

    resolved_build_id = build_id or default_build_id(
        paths.name,
        config,
        resolved_ids,
        source_digests={
            source.source_id: source.content_sha256 for source in resolved_sources
        },
    )
    _validate_identifier(resolved_build_id, "build id")
    results = [
        build_source(
            paths,
            config,
            source.source_id,
            build_id=resolved_build_id,
            source_dir=source.path,
            expected_bundle_sha256=source.content_sha256 or None,
        )
        for source in resolved_sources
    ]
    result = BuildResult(name=paths.name, build_id=resolved_build_id, sources=tuple(results))
    write_json(paths.build_run_dir(resolved_build_id) / "build.json", result.to_dict())
    write_json(
        paths.active_tasks_path(),
        {
            "schema_version": ACTIVE_TASKS_SCHEMA,
            "name": paths.name,
            "build_id": resolved_build_id,
            "task_ids": sorted(set(result.published_task_ids)),
        },
    )
    return result


def load_published_task(paths: TasksetPaths, task_id: str) -> dict[str, Any]:
    """Read one published ``public/task.json``."""
    payload = read_json(
        paths.task_dir(task_id) / "public" / "task.json", label=f"published task {task_id}"
    )
    if not isinstance(payload, dict):
        raise BuildConfigurationError(f"published task {task_id} is not a JSON object")
    return payload


def list_published_tasks(paths: TasksetPaths) -> list[str]:
    active_path = paths.active_tasks_path()
    if active_path.is_file():
        try:
            payload = read_json(active_path, label="active task manifest")
        except GhostlabError as error:
            raise BuildConfigurationError(str(error)) from error
        if not isinstance(payload, Mapping):
            raise BuildConfigurationError("active task manifest is not a JSON object")
        if payload.get("schema_version") != ACTIVE_TASKS_SCHEMA:
            raise BuildConfigurationError(
                "active task manifest declares "
                f"schema_version={payload.get('schema_version')!r}, "
                f"expected {ACTIVE_TASKS_SCHEMA!r}"
            )
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not all(isinstance(item, str) for item in task_ids):
            raise BuildConfigurationError("active task manifest task_ids must be an array of strings")
        resolved: list[str] = []
        for task_id in task_ids:
            _validate_identifier(task_id, "active task id")
            if task_id in resolved:
                raise BuildConfigurationError(
                    f"active task manifest contains duplicate task id {task_id!r}"
                )
            if not (paths.task_dir(task_id) / "public" / "task.json").is_file():
                raise BuildConfigurationError(
                    f"active task manifest references unpublished task {task_id!r}"
                )
            resolved.append(task_id)
        return sorted(resolved)

    tasks_dir = paths.tasks_dir()
    if not tasks_dir.is_dir():
        return []
    return sorted(
        path.name for path in tasks_dir.iterdir() if (path / "public" / "task.json").is_file()
    )


__all__ = [
    "ACTIVE_TASKS_SCHEMA",
    "BENCHMARK_TASK_SCHEMA",
    "BUILD_REPORT_SCHEMA",
    "INSTRUCTION_ASSET_DIR",
    "PACKAGED_INSTRUCTIONS",
    "PUBLIC_TASK_FILES",
    "REQUIRED_VALIDATION_KINDS",
    "SCORER_SCHEMA",
    "SCORER_VALIDATION_SCHEMA",
    "SOURCE_BUNDLE_SCHEMA",
    "STAGES",
    "STAGE_SCHEMA",
    "TASK_DEFINITIONS_SCHEMA",
    "VALIDATION_CASES_SCHEMA",
    "VALIDATION_CASE_CODES",
    "BuildConfig",
    "BuildConfigurationError",
    "BuildResult",
    "LintFinding",
    "LintOutcome",
    "LintRequest",
    "RepeatabilityResult",
    "Rejection",
    "ScorerValidation",
    "SourceBuildResult",
    "SourceStageState",
    "StageFailure",
    "StageStore",
    "TaskStageState",
    "TasksetBuildSummary",
    "TasksetPaths",
    "ValidationCaseResult",
    "agent_instruction_hashes",
    "build_asset_record",
    "build_public_task",
    "build_source",
    "build_sources",
    "build_taskset",
    "check_builder_contract",
    "coerce_lint_outcome",
    "compute_scorer_package_hash",
    "compute_task_id",
    "default_build_id",
    "evaluate_repeatability",
    "evaluate_validation_case",
    "instruction_path",
    "instruction_sha256",
    "instruction_text",
    "is_git_bundle",
    "list_published_tasks",
    "load_environment",
    "load_published_task",
    "load_source_manifest",
    "materialize_repo_bundle",
    "normalize_prompt",
    "pack_directory",
    "packaged_instruction_index",
    "publish_directory",
    "resolve_lint_fn",
    "resolve_taskset_paths",
    "summarize_build",
    "unpack_bundle",
    "utc_now",
    "validate_scorer_package",
]


# ---------------------------------------------------------------------------
# CLI-facing entry point: retro benchmark taskset build
# ---------------------------------------------------------------------------


def resolve_taskset_paths(layout: Any, name: str) -> TasksetPaths:
    """Accept a ``Layout``, an archive root, or a prepared ``TasksetPaths``."""
    _validate_identifier(name, "taskset name")
    if isinstance(layout, TasksetPaths):
        if layout.name != name:
            raise BuildConfigurationError(
                f"TasksetPaths is for {layout.name!r}, not {name!r}"
            )
        return layout
    if layout is None:
        from ...storage import default_layout

        return TasksetPaths.from_layout(default_layout(), name)
    if isinstance(layout, (str, Path)):
        from ...storage import Layout

        return TasksetPaths.from_layout(Layout(Path(layout)), name)
    return TasksetPaths.from_layout(layout, name)


def _require_agent(value: str | Path | None, flag: str) -> Path:
    if value is None:
        raise BuildConfigurationError(f"{flag} is required")
    path = Path(value)
    if not path.is_file():
        raise BuildConfigurationError(f"{flag} does not exist: {path}")
    return path


@dataclass(frozen=True)
class TasksetBuildSummary:
    """Rich-renderable outcome of ``retro benchmark taskset build``."""

    name: str
    build_id: str
    build_dir: Path
    tasks_dir: Path
    report_path: Path
    published_task_ids: tuple[str, ...]
    sources_total: int
    sources_ok: int
    sources_failed: int
    reused_stages: tuple[str, ...]
    rejection_counts: Mapping[str, int]
    rejections: tuple[Rejection, ...]
    warnings: tuple[str, ...]
    result: BuildResult

    @property
    def published(self) -> int:
        return len(self.published_task_ids)

    @property
    def rejected(self) -> int:
        return len(self.rejections)

    def source_rows(self) -> list[dict[str, Any]]:
        """One row per source, ready for a Rich table."""
        rows: list[dict[str, Any]] = []
        for source in self.result.sources:
            error = source.state.error or {}
            rows.append(
                {
                    "source_id": source.source_id,
                    "stage": source.state.stage,
                    "status": source.state.status,
                    "published": len(source.published_task_ids),
                    "rejections": len(source.rejections),
                    "reused": len(source.reused_stages),
                    "code": error.get("code") or "",
                    "detail": error.get("detail") or "",
                }
            )
        return rows

    def task_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for source in self.result.sources:
            for task_id in source.published_task_ids:
                task = source.state.tasks.get(task_id)
                artifacts = dict(task.artifacts) if task else {}
                rows.append(
                    {
                        "task_id": task_id,
                        "source_id": source.source_id,
                        "candidate_id": task.candidate_id if task else "",
                        "scorer_package_sha256": artifacts.get("scorer_package_sha256", ""),
                        "task_dir": artifacts.get("task_dir", ""),
                    }
                )
        return rows

    def rejection_rows(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.rejections]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BUILD_REPORT_SCHEMA,
            "name": self.name,
            "build_id": self.build_id,
            "build_dir": str(self.build_dir),
            "tasks_dir": str(self.tasks_dir),
            "report_path": str(self.report_path),
            "published": self.published,
            "published_task_ids": list(self.published_task_ids),
            "sources_total": self.sources_total,
            "sources_ok": self.sources_ok,
            "sources_failed": self.sources_failed,
            "reused_stages": list(self.reused_stages),
            "rejection_counts": dict(self.rejection_counts),
            "rejections": self.rejection_rows(),
            "warnings": list(self.warnings),
            "sources": self.source_rows(),
            "tasks": self.task_rows(),
        }


def summarize_build(paths: TasksetPaths, result: BuildResult) -> TasksetBuildSummary:
    warnings: list[str] = []
    for source in result.sources:
        warnings.extend(source.state.warnings)
    return TasksetBuildSummary(
        name=result.name,
        build_id=result.build_id,
        build_dir=paths.build_run_dir(result.build_id),
        tasks_dir=paths.tasks_dir(),
        report_path=paths.build_run_dir(result.build_id) / "build.json",
        published_task_ids=result.published_task_ids,
        sources_total=len(result.sources),
        sources_ok=sum(1 for source in result.sources if source.ok),
        sources_failed=sum(1 for source in result.sources if not source.ok),
        reused_stages=tuple(
            stage for source in result.sources for stage in source.reused_stages
        ),
        rejection_counts=result.rejection_counts(),
        rejections=result.rejections,
        warnings=tuple(dict.fromkeys(warnings)),
        result=result,
    )


def build_taskset(
    layout: Any,
    name: str,
    ghostlab_bin: str | Path | None = None,
    task_definer_agent: str | Path | None = None,
    scorer_builder_agent: str | Path | None = None,
    scorer_auditor_agent: str | Path | None = None,
    adjacent_per_replay: int = 0,
    *,
    source_ids: Iterable[str] | None = None,
    build_id: str | None = None,
    max_replay_tasks: int = 3,
    repeatability_runs: int = 3,
    require_audit: bool = True,
    lint: LintFn | None = None,
    scorer_sdk: str | Path | None = None,
    task_definitions_schema: str | Path | None = None,
    scorer_audit_schema: str | Path | None = None,
    ghostlab: GhostlabCli | None = None,
    ghostlab_env: Mapping[str, str] | None = None,
    definer_timeout_seconds: float | None = None,
    builder_timeout_seconds: float | None = None,
    auditor_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
) -> TasksetBuildSummary:
    """Run ``retro benchmark taskset build`` (spec sections 14.3 and 20).

    ``layout`` accepts a :class:`retro.storage.Layout`, an archive root path, or a
    prepared :class:`TasksetPaths`. The build id defaults to a content-addressed
    value, so re-running with unchanged agents, Ghostlab version, instruction
    assets, and bundles resumes the previous build instead of forking a new one
    and never republishes an identical task.
    """
    paths = resolve_taskset_paths(layout, name)
    definer = _require_agent(task_definer_agent, "--task-definer-agent")
    builder = _require_agent(scorer_builder_agent, "--scorer-builder-agent")
    auditor = (
        _require_agent(scorer_auditor_agent, "--scorer-auditor-agent")
        if scorer_auditor_agent is not None
        else None
    )
    if auditor is None and require_audit:
        raise BuildConfigurationError(
            "--scorer-auditor-agent is required; pass require_audit=False to publish "
            "without the independent scorer audit"
        )
    if adjacent_per_replay not in (0, 1):
        raise BuildConfigurationError(
            f"--adjacent-per-replay must be 0 or 1, got {adjacent_per_replay}"
        )

    client = ghostlab or GhostlabCli(ghostlab_bin, env=ghostlab_env)
    config = BuildConfig(
        name=name,
        ghostlab=client,
        task_definer_agent=definer,
        scorer_builder_agent=builder,
        scorer_auditor_agent=auditor,
        task_definitions_schema=Path(task_definitions_schema) if task_definitions_schema else None,
        scorer_audit_schema=Path(scorer_audit_schema) if scorer_audit_schema else None,
        scorer_sdk=Path(scorer_sdk) if scorer_sdk else None,
        adjacent_per_replay=adjacent_per_replay,
        max_replay_tasks=max_replay_tasks,
        repeatability_runs=repeatability_runs,
        require_audit=require_audit,
        lint=lint,
        definer_timeout_seconds=definer_timeout_seconds,
        builder_timeout_seconds=builder_timeout_seconds,
        auditor_timeout_seconds=auditor_timeout_seconds,
        scorer_timeout_seconds=scorer_timeout_seconds,
    )
    result = build_sources(paths, config, source_ids, build_id=build_id)
    return summarize_build(paths, result)
