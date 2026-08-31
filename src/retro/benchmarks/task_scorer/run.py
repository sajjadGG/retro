"""Candidate-agent evaluation for published benchmark tasks.

Implements spec section 15: verify the published task, materialize a fresh base
repository for every attempt, run the agent under test once through
``ghostlab artifact-run``, score the exported state through
``ghostlab scorer-run``, and write one immutable, hash-addressed ``attempt.json``.

Agent, harness, and scorer failures receive distinct statuses and are never
converted into a numeric zero.
"""
from __future__ import annotations

import math
import re
import shutil
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aggregate import (
    ATTEMPT_SCHEMA,
    SCORED_STATUS,
    AttemptRecord,
    BenchmarkAggregate,
    aggregate_attempts,
    iter_status_rows,
    load_attempts,
    validate_attempt_record,
    validate_score_report,
    write_aggregate,
)
from .build import (
    BENCHMARK_TASK_SCHEMA,
    TasksetPaths,
    compute_scorer_package_hash,
    list_published_tasks,
    resolve_taskset_paths,
    unpack_bundle,
    utc_now,
)
from .ghostlab_cli import (
    ArtifactRunRequest,
    GhostlabBinaryError,
    GhostlabCli,
    GhostlabContractError,
    GhostlabError,
    GhostlabInvocationError,
    GhostlabTimeoutError,
    ScorerRunRequest,
    read_json,
    sha256_file,
    sha256_json,
    sha256_path,
    write_json,
)
from .schema import BenchmarkTask, ProjectEnvironment, SchemaError, ScorerManifest

CANDIDATE_EXPORT_NAME = "candidate-state.tar.zst"
RESOURCE_USAGE_SCHEMA = "retro-attempt-resources-v1"

#: ``artifact-run`` status -> attempt status. ``None`` means "keep going".
ARTIFACT_STATUS_TO_ATTEMPT: Mapping[str, str | None] = {
    "completed": None,
    "timed_out": "agent_timeout",
    "timeout": "agent_timeout",
    "agent_error": "agent_error",
    "model_unavailable": "model_unavailable",
    "export_failed": "harness_error",
    "output_contract_failed": "harness_error",
    "contract_violation": "harness_error",
    "sandbox_error": "harness_error",
    "harness_error": "harness_error",
}


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class TaskVerificationError(RuntimeError):
    """A published task failed its integrity checks before any model ran."""


def _validate_safe_id(value: str, label: str) -> str:
    if not _SAFE_ID_RE.fullmatch(value):
        raise TaskVerificationError(f"{label} {value!r} contains unsupported characters")
    return value


def _referenced_agent_paths(payload: Mapping[str, Any]) -> tuple[str, ...]:
    references: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str) and value:
            references.append(value)
        elif isinstance(value, Mapping):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        collect(runtime.get("instructions"))
    inputs = payload.get("inputs")
    if isinstance(inputs, Mapping):
        for key in ("skills", "mcps", "assets"):
            collect(inputs.get(key))
    return tuple(dict.fromkeys(references))


def _agent_asset_hashes(path: Path) -> dict[str, str]:
    payload = read_json(path, label="candidate agent config")
    if not isinstance(payload, Mapping):
        raise TaskVerificationError(f"agent config {path} is not a JSON object")
    hashes: dict[str, str] = {}
    packaged = Path(__file__).resolve().parent / "instructions"
    for reference in _referenced_agent_paths(payload):
        candidate = Path(reference).expanduser()
        candidates = (
            (candidate,)
            if candidate.is_absolute()
            else (
                path.parent / candidate,
                Path.cwd() / candidate,
                packaged / candidate.name,
            )
        )
        resolved = next((item for item in candidates if item.exists()), None)
        hashes[reference] = sha256_path(resolved, excludes=()) if resolved is not None else "<missing>"
    return hashes


def parse_seeds(value: str | Sequence[int]) -> tuple[int, ...]:
    """Parse ``--seeds 0,1,2`` into a de-duplicated, ordered tuple."""
    if not isinstance(value, str):
        return tuple(dict.fromkeys(int(item) for item in value))
    seeds: list[int] = []
    for chunk in value.split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            seeds.append(int(text))
        except ValueError as exc:
            raise TaskVerificationError(f"seed {text!r} is not an integer") from exc
    if not seeds:
        raise TaskVerificationError("at least one seed is required")
    return tuple(dict.fromkeys(seeds))


def default_eval_id(now: datetime | None = None) -> str:
    """Timestamped eval id used when the caller does not supply one."""
    moment = now or datetime.now(timezone.utc)
    return "eval-" + moment.strftime("%Y%m%dT%H%M%SZ")


def unique_eval_id(paths: TasksetPaths) -> str:
    """A timestamped eval id that does not collide with an existing directory."""
    base = default_eval_id()
    existing = set(list_evals(paths))
    if base not in existing:
        return base
    for index in range(2, 1000):
        candidate = f"{base}-{index}"
        if candidate not in existing:
            return candidate
    raise TaskVerificationError(f"could not allocate a fresh eval id beside {base!r}")


def list_evals(paths: TasksetPaths) -> list[str]:
    root = paths.eval_dir("probe").parent
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if path.is_dir())


def resolve_eval_id(paths: TasksetPaths, requested: str | None) -> str:
    """Resolve ``--eval latest`` (or a missing value) against existing evals."""
    if requested and requested != "latest":
        if not _SAFE_ID_RE.fullmatch(requested):
            raise TaskVerificationError(f"eval id {requested!r} contains unsupported characters")
        return requested
    existing = list_evals(paths)
    if not existing:
        raise TaskVerificationError(
            f"no evals exist under {paths.eval_dir('').parent}; run the taskset first"
        )
    return existing[-1]


@dataclass(frozen=True)
class AgentSpec:
    """One candidate agent configuration under evaluation."""

    agent_id: str
    config_path: Path
    config_sha256: str
    expected_sha256: str | None = None

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        agent_id: str | None = None,
        expected_sha256: str | None = None,
    ) -> AgentSpec:
        if not path.is_file():
            raise TaskVerificationError(f"agent config {path} does not exist")
        payload = read_json(path, label="candidate agent config")
        if not isinstance(payload, Mapping):
            raise TaskVerificationError(f"agent config {path} is not a JSON object")
        resolved = agent_id
        if resolved is None:
            declared = payload.get("id")
            if isinstance(declared, str) and declared:
                resolved = declared
        if resolved is None:
            resolved = path.stem
        resolved = _validate_safe_id(resolved, "agent id")
        return cls(
            agent_id=resolved,
            config_path=path,
            config_sha256=sha256_file(path),
            expected_sha256=expected_sha256,
        )

    def verify(self) -> None:
        _validate_safe_id(self.agent_id, "agent id")
        actual = sha256_file(self.config_path)
        if actual != self.config_sha256:
            raise TaskVerificationError(
                f"agent config {self.config_path} changed after it was loaded: "
                f"{self.config_sha256} -> {actual}"
            )
        if self.expected_sha256 and self.expected_sha256 != self.config_sha256:
            raise TaskVerificationError(
                f"agent config {self.config_path} hashes to {self.config_sha256}, "
                f"expected {self.expected_sha256}"
            )

    def referenced_asset_hashes(self) -> dict[str, str]:
        return _agent_asset_hashes(self.config_path)


@dataclass(frozen=True)
class PublishedTask:
    """The verified public/private material for one benchmark task."""

    task_id: str
    task_dir: Path
    public_task: Mapping[str, Any]
    public_task_path: Path
    prompt_path: Path
    public_environment: Mapping[str, Any]
    public_environment_path: Path
    base_bundle: Path
    scorer_manifest: Mapping[str, Any]
    scorer_manifest_path: Path
    provenance: Mapping[str, Any]
    public_task_sha256: str
    prompt_sha256: str
    public_environment_sha256: str
    base_bundle_sha256: str
    scorer_package_sha256: str

    @property
    def source_id(self) -> str | None:
        value = self.provenance.get("source_id")
        return value if isinstance(value, str) else None

    @property
    def pass_threshold(self) -> float:
        scoring = self.public_task.get("scoring")
        if isinstance(scoring, Mapping):
            value = scoring.get("pass_threshold")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
        return 0.8

    @property
    def wall_time_seconds(self) -> int:
        limits = self.public_task.get("limits")
        if isinstance(limits, Mapping):
            value = limits.get("wall_time_seconds")
            if isinstance(value, int):
                return value
        return 1800

    @property
    def environment_image(self) -> str:
        image = self.public_environment.get("image")
        if isinstance(image, str) and image:
            return image
        raise TaskVerificationError(f"task {self.task_id} has no pinned environment image")

    @property
    def setup_commands(self) -> tuple[tuple[str, ...], ...]:
        commands = self.public_environment.get("setup")
        if not isinstance(commands, list) or not commands:
            raise TaskVerificationError(f"task {self.task_id} has no environment setup command")
        normalized: list[tuple[str, ...]] = []
        for command in commands:
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(part, str) or not part for part in command)
            ):
                raise TaskVerificationError(
                    f"task {self.task_id} has an invalid environment setup command"
                )
            normalized.append(tuple(command))
        return tuple(normalized)

    @property
    def scorer_timeout_seconds(self) -> float:
        runtime = self.scorer_manifest.get("runtime")
        if isinstance(runtime, Mapping):
            value = runtime.get("timeout_seconds")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return float(value)
        raise TaskVerificationError(f"task {self.task_id} has no valid scorer timeout")


def verify_published_task(paths: TasksetPaths, task_id: str) -> PublishedTask:
    """Check every published artifact hash before an attempt starts."""
    _validate_safe_id(task_id, "task id")
    task_dir = paths.task_dir(task_id)
    public_task_path = task_dir / "public" / "task.json"
    prompt_path = task_dir / "public" / "prompt.txt"
    public_environment_path = task_dir / "public" / "environment.json"
    base_bundle = task_dir / "public" / "base.bundle"
    scorer_manifest = task_dir / "private" / "scorer" / "scorer.json"
    provenance_path = task_dir / "private" / "provenance.json"
    for path, label in (
        (public_task_path, "public/task.json"),
        (prompt_path, "public/prompt.txt"),
        (public_environment_path, "public/environment.json"),
        (base_bundle, "public/base.bundle"),
        (scorer_manifest, "private/scorer/scorer.json"),
        (provenance_path, "private/provenance.json"),
    ):
        if not path.is_file():
            raise TaskVerificationError(f"published task {task_id} is missing {label}")

    public_task = read_json(public_task_path, label="public task")
    if not isinstance(public_task, Mapping):
        raise TaskVerificationError(f"published task {task_id} public/task.json is not an object")
    try:
        parsed_task = BenchmarkTask.from_dict(public_task, where="public/task.json")
    except SchemaError as exc:
        raise TaskVerificationError(f"published task {task_id} is invalid: {exc}") from exc
    if public_task.get("schema_version") != BENCHMARK_TASK_SCHEMA:
        raise TaskVerificationError(
            f"published task {task_id} declares schema_version="
            f"{public_task.get('schema_version')!r}, expected {BENCHMARK_TASK_SCHEMA!r}"
        )
    if public_task.get("task_id") != task_id:
        raise TaskVerificationError(
            f"published task directory {task_id} holds task_id={public_task.get('task_id')!r}"
        )

    public_environment = read_json(public_environment_path, label="public environment")
    if not isinstance(public_environment, Mapping):
        raise TaskVerificationError(
            f"published task {task_id} public/environment.json is not an object"
        )
    try:
        parsed_environment = ProjectEnvironment.from_dict(
            public_environment, where="public/environment.json"
        )
    except SchemaError as exc:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid public environment: {exc}"
        ) from exc
    if not parsed_environment.setup or any(
        not token for command in parsed_environment.setup for token in command
    ):
        raise TaskVerificationError(
            f"published task {task_id} public environment has no valid setup command"
        )
    task_environment = parsed_task.environment
    expected_projection = {
        "image": parsed_environment.image,
        "setup_command": parsed_environment.setup[0],
        "network": parsed_environment.network_during_run,
    }
    for key, expected in expected_projection.items():
        if task_environment.get(key) != expected:
            raise TaskVerificationError(
                f"published task {task_id} environment.{key} does not match "
                f"public/environment.json"
            )
    if parsed_task.repository.get("base_sha") != parsed_environment.base_sha:
        raise TaskVerificationError(
            f"published task {task_id} environment base_sha does not match its repository"
        )
    wall_time = parsed_task.limits.get("wall_time_seconds")
    if isinstance(wall_time, bool) or not isinstance(wall_time, int) or wall_time <= 0:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid wall_time_seconds limit"
        )

    scorer_payload = read_json(scorer_manifest, label="scorer.json")
    if not isinstance(scorer_payload, Mapping):
        raise TaskVerificationError(
            f"published task {task_id} private/scorer/scorer.json is not an object"
        )
    try:
        parsed_scorer = ScorerManifest.from_dict(scorer_payload, where="private/scorer/scorer.json")
    except SchemaError as exc:
        raise TaskVerificationError(
            f"published task {task_id} has an invalid scorer manifest: {exc}"
        ) from exc
    if parsed_scorer.task_id != task_id:
        raise TaskVerificationError(
            f"published task {task_id} scorer manifest targets {parsed_scorer.task_id!r}"
        )
    if not math.isfinite(parsed_scorer.pass_threshold) or any(
        not math.isfinite(component.weight) for component in parsed_scorer.components
    ):
        raise TaskVerificationError(
            f"published task {task_id} scorer manifest contains non-finite numbers"
        )
    public_threshold = parsed_task.scoring.get("pass_threshold")
    if (
        isinstance(public_threshold, bool)
        or not isinstance(public_threshold, (int, float))
        or not math.isfinite(float(public_threshold))
        or not 0.0 <= float(public_threshold) <= 1.0
    ):
        raise TaskVerificationError(
            f"published task {task_id} has an invalid public pass threshold"
        )
    if abs(parsed_scorer.pass_threshold - float(public_threshold)) > 1e-9:
        raise TaskVerificationError(
            f"published task {task_id} scorer and public pass thresholds disagree"
        )

    provenance = read_json(provenance_path, label="task provenance")
    if not isinstance(provenance, Mapping):
        raise TaskVerificationError(f"published task {task_id} provenance is not an object")

    public_task_sha256 = sha256_file(public_task_path)
    prompt_sha256 = sha256_file(prompt_path)
    public_environment_sha256 = sha256_file(public_environment_path)
    base_bundle_sha256 = sha256_file(base_bundle)

    for label, actual, declared in (
        ("public/task.json", public_task_sha256, provenance.get("public_task_sha256")),
        ("public/base.bundle", base_bundle_sha256, provenance.get("base_bundle_sha256")),
    ):
        if isinstance(declared, str) and declared and declared != actual:
            raise TaskVerificationError(
                f"published task {task_id} {label} hashes to {actual}, provenance says {declared}"
            )

    resolved_scorer_sha = _verify_scorer_package(task_id, scorer_manifest, provenance)
    return PublishedTask(
        task_id=task_id,
        task_dir=task_dir,
        public_task=public_task,
        public_task_path=public_task_path,
        prompt_path=prompt_path,
        public_environment=public_environment,
        public_environment_path=public_environment_path,
        base_bundle=base_bundle,
        scorer_manifest=scorer_payload,
        scorer_manifest_path=scorer_manifest,
        provenance=provenance,
        public_task_sha256=public_task_sha256,
        prompt_sha256=prompt_sha256,
        public_environment_sha256=public_environment_sha256,
        base_bundle_sha256=base_bundle_sha256,
        scorer_package_sha256=resolved_scorer_sha,
    )


def _scorer_package_hashes(scorer_manifest: Path) -> tuple[str, str | None]:
    """Return Retro's computed package hash and the package's own declared hash."""
    manifest = read_json(scorer_manifest, label="scorer.json")
    payload: Mapping[str, Any] = manifest if isinstance(manifest, Mapping) else {}
    try:
        computed = compute_scorer_package_hash(scorer_manifest.parent)
    except ValueError as error:
        raise TaskVerificationError(str(error)) from error
    declared = payload.get("package_sha256")
    return computed, declared if isinstance(declared, str) and declared else None


def _verify_scorer_package(
    task_id: str, scorer_manifest: Path, provenance: Mapping[str, Any]
) -> str:
    computed, declared = _scorer_package_hashes(scorer_manifest)
    recorded = provenance.get("scorer_package_sha256")
    if declared is not None and declared != computed:
        raise TaskVerificationError(
            f"published task {task_id} scorer.json declares {declared}, but the package "
            f"hash is {computed}"
        )
    if not isinstance(recorded, str) or not recorded:
        raise TaskVerificationError(
            f"published task {task_id} provenance has no scorer_package_sha256"
        )
    if recorded != computed:
        raise TaskVerificationError(
            f"published task {task_id} private/scorer hashes to {computed}, provenance says "
            f"{recorded}; the scorer package changed after publication"
        )
    return computed


@dataclass(frozen=True)
class RunConfig:
    """Everything one ``retro benchmark taskset run`` invocation needs."""

    ghostlab: GhostlabCli
    eval_id: str
    seeds: tuple[int, ...] = (0,)
    aut_timeout_seconds: float | None = None
    scorer_timeout_seconds: float | None = None
    candidate_export_name: str = CANDIDATE_EXPORT_NAME
    force: bool = False


@dataclass(frozen=True)
class AttemptResult:
    """One immutable ``retro-benchmark-attempt-v1`` outcome."""

    attempt_id: str
    task_id: str
    agent_id: str
    seed: int
    status: str
    attempt_dir: Path
    input_sha256: str
    agent_config_sha256: str
    base_bundle_sha256: str
    scorer_package_sha256: str
    source_id: str | None = None
    candidate_state_sha256: str | None = None
    score: float | None = None
    passed: bool | None = None
    pass_threshold: float = 0.8
    components: tuple[Mapping[str, Any], ...] = ()
    tokens: Mapping[str, int] = field(default_factory=dict)
    wall_time_ms: int = 0
    cost_usd: float | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    reused: bool = False
    artifact_run: str | None = None
    score_report: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ATTEMPT_SCHEMA,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "source_id": self.source_id,
            "agent_id": self.agent_id,
            "seed": self.seed,
            "status": self.status,
            "agent_config_sha256": self.agent_config_sha256,
            "base_bundle_sha256": self.base_bundle_sha256,
            "candidate_state_sha256": self.candidate_state_sha256,
            "scorer_package_sha256": self.scorer_package_sha256,
            "input_sha256": self.input_sha256,
            "score": self.score,
            "passed": self.passed,
            "pass_threshold": self.pass_threshold,
            "components": [dict(component) for component in self.components],
            "tokens": dict(sorted(self.tokens.items())),
            "wall_time_ms": self.wall_time_ms,
            "cost_usd": self.cost_usd,
            "error": self.error,
            "warnings": list(self.warnings),
            "artifact_run": self.artifact_run,
            "score_report": self.score_report,
            "created_at": self.created_at,
        }

    def record(self) -> AttemptRecord:
        return AttemptRecord.from_mapping(self.to_dict(), path=self.attempt_dir / "attempt.json")


def _normalize_tokens(report: Mapping[str, Any]) -> dict[str, int]:
    for key in ("tokens", "usage"):
        raw = report.get(key)
        if isinstance(raw, Mapping):
            tokens: dict[str, int] = {}
            for name, value in raw.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                normalized = str(name).replace("_tokens", "")
                tokens[normalized] = int(value)
            if tokens:
                return tokens
    return {}


def _cost_usd(report: Mapping[str, Any]) -> float | None:
    for container in (report, report.get("usage") if isinstance(report.get("usage"), Mapping) else {}):
        if isinstance(container, Mapping):
            value = container.get("cost_usd")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def compute_attempt_id(
    task: PublishedTask,
    agent: AgentSpec,
    seed: int,
    ghostlab_version: Mapping[str, Any],
    *,
    aut_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
    candidate_export_name: str = CANDIDATE_EXPORT_NAME,
) -> tuple[str, str]:
    """Hash-address one attempt over every input that can change its result."""
    effective_aut_timeout = (
        float(aut_timeout_seconds)
        if aut_timeout_seconds is not None
        else float(task.wall_time_seconds)
    )
    effective_scorer_timeout = (
        float(scorer_timeout_seconds)
        if scorer_timeout_seconds is not None
        else task.scorer_timeout_seconds
    )
    for label, value in (
        ("agent timeout", effective_aut_timeout),
        ("scorer timeout", effective_scorer_timeout),
    ):
        if not math.isfinite(value) or value <= 0:
            raise TaskVerificationError(f"{label} must be a positive finite number")
    if Path(candidate_export_name).name != candidate_export_name or candidate_export_name in {
        "",
        ".",
        "..",
    }:
        raise TaskVerificationError(
            f"candidate export name {candidate_export_name!r} must be a plain file name"
        )

    evaluation_fingerprint = sha256_json(
        {
            "task_id": task.task_id,
            "public_task_sha256": task.public_task_sha256,
            "prompt_sha256": task.prompt_sha256,
            "public_environment_sha256": task.public_environment_sha256,
            "public_environment": dict(task.public_environment),
            "base_bundle_sha256": task.base_bundle_sha256,
            "scorer_package_sha256": task.scorer_package_sha256,
            "agent_id": agent.agent_id,
            "agent_config_sha256": agent.config_sha256,
            "agent_referenced_assets": agent.referenced_asset_hashes(),
            "ghostlab": dict(ghostlab_version),
            "artifact_run": {
                "timeout_seconds": effective_aut_timeout,
                "export_workspace": candidate_export_name,
                "sandbox_image": task.environment_image,
                "setup_commands": [list(command) for command in task.setup_commands],
            },
            "scorer_run": {"timeout_seconds": effective_scorer_timeout},
        }
    )
    attempt_id = sha256_json(
        {"evaluation_fingerprint": evaluation_fingerprint, "seed": seed}
    )[:20]
    return attempt_id, evaluation_fingerprint


def _write_attempt(attempt: AttemptResult) -> AttemptResult:
    write_json(attempt.attempt_dir / "attempt.json", attempt.to_dict())
    return attempt


def run_attempt(
    paths: TasksetPaths,
    config: RunConfig,
    task: PublishedTask,
    agent: AgentSpec,
    seed: int,
) -> AttemptResult:
    """Run one ``(task, agent, seed)`` attempt end to end."""
    agent.verify()
    _validate_safe_id(config.eval_id, "eval id")
    _validate_safe_id(task.task_id, "task id")
    _validate_safe_id(agent.agent_id, "agent id")
    if seed < 0:
        raise TaskVerificationError("seed must be non-negative")

    effective_aut_timeout = (
        config.aut_timeout_seconds
        if config.aut_timeout_seconds is not None
        else float(task.wall_time_seconds)
    )
    effective_scorer_timeout = (
        config.scorer_timeout_seconds
        if config.scorer_timeout_seconds is not None
        else task.scorer_timeout_seconds
    )
    version_error: GhostlabError | None = None
    try:
        version = config.ghostlab.version().fingerprint()
    except GhostlabError as exc:
        version_error = exc
        version = {
            "binary": config.ghostlab.binary,
            "version": None,
            "binary_sha256": None,
        }
    attempt_id, input_sha256 = compute_attempt_id(
        task,
        agent,
        seed,
        version,
        aut_timeout_seconds=effective_aut_timeout,
        scorer_timeout_seconds=effective_scorer_timeout,
        candidate_export_name=config.candidate_export_name,
    )
    attempt_dir = paths.attempt_dir(config.eval_id, task.task_id, agent.agent_id, seed)
    attempt_path = attempt_dir / "attempt.json"

    if attempt_path.is_file() and not config.force:
        payload = read_json(attempt_path, label="attempt record")
        if isinstance(payload, Mapping) and payload.get("input_sha256") == input_sha256:
            try:
                record = validate_attempt_record(payload, path=attempt_path)
            except ValueError:
                record = None
            if (
                record is not None
                and record.attempt_id == attempt_id
                and record.task_id == task.task_id
                and record.agent_id == agent.agent_id
                and record.seed == seed
            ):
                return _reuse(payload, attempt_dir)

    attempt_dir.mkdir(parents=True, exist_ok=True)

    def failed(status: str, detail: str, **extra: Any) -> AttemptResult:
        return _write_attempt(
            AttemptResult(
                attempt_id=attempt_id,
                task_id=task.task_id,
                agent_id=agent.agent_id,
                seed=seed,
                status=status,
                attempt_dir=attempt_dir,
                input_sha256=input_sha256,
                agent_config_sha256=agent.config_sha256,
                base_bundle_sha256=task.base_bundle_sha256,
                scorer_package_sha256=task.scorer_package_sha256,
                source_id=task.source_id,
                pass_threshold=task.pass_threshold,
                error=detail,
                **extra,
            )
        )

    if version_error is not None:
        return failed("harness_error", str(version_error))

    workspace = attempt_dir / "workspace"
    if workspace.exists():
        shutil.rmtree(workspace)
    try:
        unpack_bundle(task.base_bundle, workspace)
    except Exception as exc:  # noqa: BLE001 - materialization failure is a harness failure
        return failed("harness_error", f"could not materialize the base repository: {exc}")

    prompt_path = attempt_dir / "prompt.txt"
    shutil.copyfile(task.prompt_path, prompt_path)

    agent_run_dir = attempt_dir / "agent"
    if agent_run_dir.exists():
        shutil.rmtree(agent_run_dir)
    started = time.monotonic()
    try:
        run = config.ghostlab.artifact_run(
            ArtifactRunRequest(
                agent_config=agent.config_path,
                workspace=workspace,
                prompt_file=prompt_path,
                run_dir=agent_run_dir,
                export_workspace=config.candidate_export_name,
                timeout_seconds=effective_aut_timeout,
                sandbox_image=task.environment_image,
                setup_commands=task.setup_commands,
                label="candidate-agent",
            )
        )
    except GhostlabTimeoutError as exc:
        return failed("agent_timeout", str(exc))
    except (GhostlabBinaryError, GhostlabInvocationError) as exc:
        return failed("harness_error", str(exc))
    except GhostlabContractError as exc:
        return failed("harness_error", str(exc))
    except GhostlabError as exc:
        return failed("agent_error", str(exc))

    wall_time_ms = int((time.monotonic() - started) * 1000)
    tokens = _normalize_tokens(run.report)
    cost = _cost_usd(run.report)
    mapped = ARTIFACT_STATUS_TO_ATTEMPT.get(run.status, "harness_error")
    if mapped is not None:
        return failed(
            mapped,
            f"candidate artifact-run reported status={run.status}: "
            f"{run.stderr_tail or '<no stderr>'}",
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )

    candidate = run.export_path(config.candidate_export_name)
    if candidate is None:
        return failed(
            "harness_error",
            f"candidate artifact-run exported no {config.candidate_export_name}",
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    candidate_sha256 = run.export_sha256.get(config.candidate_export_name) or sha256_path(
        candidate, excludes=()
    )

    resources_path = attempt_dir / "resources.json"
    write_json(
        resources_path,
        {
            "schema_version": RESOURCE_USAGE_SCHEMA,
            "task_id": task.task_id,
            "attempt_id": attempt_id,
            "tokens": dict(sorted(tokens.items())),
            "wall_time_ms": wall_time_ms,
            "cost_usd": cost,
        },
    )

    trace = run.events_path if run.events_path and run.events_path.is_file() else None
    score_report_path = attempt_dir / "score-report.json"
    scorer_run_dir = attempt_dir / "scorer"
    score_report_path.unlink(missing_ok=True)
    if scorer_run_dir.exists():
        shutil.rmtree(scorer_run_dir)
    try:
        scored = config.ghostlab.scorer_run(
            ScorerRunRequest(
                task_path=task.public_task_path,
                scorer_path=task.scorer_manifest_path,
                candidate_path=candidate,
                output_path=score_report_path,
                trace_path=trace,
                resource_usage_path=resources_path,
                seed=seed,
                run_dir=scorer_run_dir,
                timeout_seconds=effective_scorer_timeout,
                label="scorer",
            )
        )
    except GhostlabTimeoutError as exc:
        return failed(
            "scorer_timeout",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except (GhostlabBinaryError, GhostlabInvocationError) as exc:
        return failed(
            "harness_error",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except GhostlabContractError as exc:
        return failed(
            "invalid_result",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )
    except GhostlabError as exc:
        return failed(
            "scorer_error",
            str(exc),
            candidate_state_sha256=candidate_sha256,
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            artifact_run=str(run.report_path),
        )

    validation = validate_score_report(
        scored.report,
        pass_threshold=task.pass_threshold,
        scorer_manifest=task.scorer_manifest,
        expected_task_id=task.task_id,
        expected_attempt_id=attempt_id,
        expected_scorer_package_sha256=task.scorer_package_sha256,
    )
    status = scored.status
    error: str | None = None
    if scored.status == "scored" and not validation.valid:
        status = "invalid_result"
        error = "; ".join(validation.errors)

    return _write_attempt(
        AttemptResult(
            attempt_id=attempt_id,
            task_id=task.task_id,
            agent_id=agent.agent_id,
            seed=seed,
            status=status,
            attempt_dir=attempt_dir,
            input_sha256=input_sha256,
            agent_config_sha256=agent.config_sha256,
            base_bundle_sha256=task.base_bundle_sha256,
            scorer_package_sha256=task.scorer_package_sha256,
            source_id=task.source_id,
            candidate_state_sha256=candidate_sha256,
            score=validation.score_total if status == "scored" else None,
            passed=validation.passed if status == "scored" else None,
            pass_threshold=task.pass_threshold,
            components=tuple(dict(component) for component in scored.components),
            tokens=tokens,
            wall_time_ms=wall_time_ms,
            cost_usd=cost,
            error=error,
            warnings=validation.warnings,
            artifact_run=str(run.report_path),
            score_report=str(scored.report_path),
        )
    )


def _reuse(payload: Mapping[str, Any], attempt_dir: Path) -> AttemptResult:
    tokens_raw = payload.get("tokens")
    tokens = (
        {str(key): int(value) for key, value in tokens_raw.items() if isinstance(value, (int, float))}
        if isinstance(tokens_raw, Mapping)
        else {}
    )
    components = tuple(
        dict(item) for item in payload.get("components") or () if isinstance(item, Mapping)
    )
    score = payload.get("score")
    threshold = payload.get("pass_threshold")
    cost = payload.get("cost_usd")
    created_at = payload.get("created_at")
    return AttemptResult(
        attempt_id=str(payload.get("attempt_id", "")),
        task_id=str(payload.get("task_id", "")),
        agent_id=str(payload.get("agent_id", "")),
        seed=int(payload.get("seed") or 0),
        status=str(payload.get("status", "harness_error")),
        attempt_dir=attempt_dir,
        input_sha256=str(payload.get("input_sha256", "")),
        agent_config_sha256=str(payload.get("agent_config_sha256", "")),
        base_bundle_sha256=str(payload.get("base_bundle_sha256", "")),
        scorer_package_sha256=str(payload.get("scorer_package_sha256", "")),
        source_id=payload.get("source_id") if isinstance(payload.get("source_id"), str) else None,
        candidate_state_sha256=(
            payload.get("candidate_state_sha256")
            if isinstance(payload.get("candidate_state_sha256"), str)
            else None
        ),
        score=float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        passed=payload.get("passed") if isinstance(payload.get("passed"), bool) else None,
        pass_threshold=(
            float(threshold) if isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            else 0.8
        ),
        components=components,
        tokens=tokens,
        wall_time_ms=int(payload.get("wall_time_ms") or 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
        warnings=tuple(str(item) for item in payload.get("warnings") or ()),
        reused=True,
        artifact_run=payload.get("artifact_run") if isinstance(payload.get("artifact_run"), str) else None,
        score_report=payload.get("score_report") if isinstance(payload.get("score_report"), str) else None,
        created_at=created_at if isinstance(created_at, str) else utc_now(),
    )


@dataclass(frozen=True)
class EvalResult:
    name: str
    eval_id: str
    agent_id: str
    attempts: tuple[AttemptResult, ...]
    aggregate: BenchmarkAggregate
    results_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "aggregate": self.aggregate.to_dict(),
            "results_path": str(self.results_path),
        }


def run_agent(
    paths: TasksetPaths,
    config: RunConfig,
    agent: AgentSpec,
    task_ids: Iterable[str] | None = None,
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> EvalResult:
    """Evaluate one agent over the published task set at every requested seed."""
    _validate_safe_id(config.eval_id, "eval id")
    agent.verify()
    resolved = sorted(task_ids) if task_ids is not None else list_published_tasks(paths)
    attempts: list[AttemptResult] = []
    for task_id in resolved:
        task = verify_published_task(paths, task_id)
        for seed in config.seeds:
            attempts.append(run_attempt(paths, config, task, agent, seed))
    aggregate = collect_eval_report(
        paths,
        config.eval_id,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return EvalResult(
        name=paths.name,
        eval_id=config.eval_id,
        agent_id=agent.agent_id,
        attempts=tuple(attempts),
        aggregate=aggregate,
        results_path=paths.results_path(config.eval_id),
    )


def task_source_index(paths: TasksetPaths, task_ids: Sequence[str] | None = None) -> dict[str, str]:
    """Map every published task id to the rollout source it came from."""
    index: dict[str, str] = {}
    for task_id in task_ids if task_ids is not None else list_published_tasks(paths):
        provenance = paths.task_dir(task_id) / "private" / "provenance.json"
        if not provenance.is_file():
            continue
        payload = read_json(provenance, label="task provenance")
        if isinstance(payload, Mapping) and isinstance(payload.get("source_id"), str):
            index[task_id] = payload["source_id"]
    return index


def task_threshold_index(paths: TasksetPaths, task_ids: Sequence[str] | None = None) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for task_id in task_ids if task_ids is not None else list_published_tasks(paths):
        public = paths.task_dir(task_id) / "public" / "task.json"
        if not public.is_file():
            continue
        payload = read_json(public, label="public task")
        if isinstance(payload, Mapping) and isinstance(payload.get("scoring"), Mapping):
            value = payload["scoring"].get("pass_threshold")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                thresholds[task_id] = float(value)
    return thresholds


def collect_eval_report(
    paths: TasksetPaths,
    eval_id: str,
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> BenchmarkAggregate:
    """Aggregate every attempt written under one eval and publish ``results.json``."""
    eval_dir = paths.eval_dir(eval_id)
    records = load_attempts(eval_dir) if eval_dir.is_dir() else []
    aggregate = aggregate_attempts(
        records,
        name=paths.name,
        eval_id=eval_id,
        task_sources=task_source_index(paths),
        task_thresholds=task_threshold_index(paths),
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
        generated_at=utc_now(),
    )
    write_aggregate(paths.results_path(eval_id), aggregate)
    return aggregate


__all__ = [
    "ARTIFACT_STATUS_TO_ATTEMPT",
    "CANDIDATE_EXPORT_NAME",
    "RESOURCE_USAGE_SCHEMA",
    "AgentSpec",
    "AttemptResult",
    "EvalResult",
    "PublishedTask",
    "RunConfig",
    "TaskVerificationError",
    "TasksetReportSummary",
    "TasksetRunSummary",
    "collect_eval_report",
    "compute_attempt_id",
    "default_eval_id",
    "list_evals",
    "parse_seeds",
    "report_taskset",
    "resolve_eval_id",
    "resolve_run_eval_id",
    "run_agent",
    "unique_eval_id",
    "run_attempt",
    "run_taskset",
    "summarize_run",
    "task_source_index",
    "task_threshold_index",
    "verify_published_task",
]


# ---------------------------------------------------------------------------
# CLI-facing entry points: retro benchmark taskset run|report
# ---------------------------------------------------------------------------


def resolve_run_eval_id(paths: TasksetPaths, requested: str | None) -> str:
    """Pick the eval an evaluation run writes into.

    ``None``/``latest`` continues the newest eval so a second ``run`` for another
    agent lands beside the first and ``report --eval latest`` compares both.
    ``new`` always starts a fresh timestamped eval.
    """
    if requested is None or requested == "latest":
        existing = list_evals(paths)
        return existing[-1] if existing else unique_eval_id(paths)
    if requested == "new":
        return unique_eval_id(paths)
    if not _SAFE_ID_RE.fullmatch(requested):
        raise TaskVerificationError(f"eval id {requested!r} contains unsupported characters")
    return requested


def _score_row(attempt: AttemptResult) -> dict[str, Any]:
    return {
        "task_id": attempt.task_id,
        "source_id": attempt.source_id or "",
        "agent_id": attempt.agent_id,
        "seed": attempt.seed,
        "status": attempt.status,
        "score": attempt.score,
        "passed": attempt.passed,
        "tokens": attempt.record().total_tokens,
        "wall_time_ms": attempt.wall_time_ms,
        "cost_usd": attempt.cost_usd,
        "reused": attempt.reused,
        "attempt_id": attempt.attempt_id,
        "error": attempt.error or "",
    }


def _aggregate_task_rows(aggregate: BenchmarkAggregate) -> list[dict[str, Any]]:
    return list(iter_status_rows(aggregate))


def _agent_rows(aggregate: BenchmarkAggregate) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for agent in aggregate.agents:
        rows.append(
            {
                "agent_id": agent.agent_id,
                "benchmark_score": agent.benchmark_score,
                "pass_rate": agent.pass_rate,
                "valid_coverage": agent.coverage,
                "scored_attempts": agent.scored_attempts,
                "requested_attempts": agent.requested_attempts,
                "agent_errors": agent.error_counts.get("agent_error", 0),
                "scorer_errors": agent.error_counts.get("scorer_error", 0),
                "harness_errors": agent.error_counts.get("harness_error", 0),
                "sources": len(agent.sources),
                "tasks": len(agent.tasks),
            }
        )
    return rows


@dataclass(frozen=True)
class TasksetRunSummary:
    """Rich-renderable outcome of ``retro benchmark taskset run``."""

    name: str
    eval_id: str
    agent_id: str
    eval_dir: Path
    results_path: Path
    seeds: tuple[int, ...]
    attempts: tuple[AttemptResult, ...]
    requested_attempts: int
    scored_attempts: int
    reused_attempts: int
    status_counts: Mapping[str, int]
    benchmark_score: float | None
    pass_rate: float | None
    coverage: float
    aggregate: BenchmarkAggregate

    @property
    def failed_attempts(self) -> int:
        return self.requested_attempts - self.scored_attempts

    def attempt_rows(self) -> list[dict[str, Any]]:
        return [_score_row(attempt) for attempt in self.attempts]

    def task_rows(self) -> list[dict[str, Any]]:
        return [
            row for row in _aggregate_task_rows(self.aggregate) if row["agent_id"] == self.agent_id
        ]

    def error_rows(self) -> list[dict[str, Any]]:
        return [
            _score_row(attempt) for attempt in self.attempts if attempt.status != SCORED_STATUS
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "agent_id": self.agent_id,
            "eval_dir": str(self.eval_dir),
            "results_path": str(self.results_path),
            "seeds": list(self.seeds),
            "requested_attempts": self.requested_attempts,
            "scored_attempts": self.scored_attempts,
            "reused_attempts": self.reused_attempts,
            "status_counts": dict(sorted(self.status_counts.items())),
            "benchmark_score": self.benchmark_score,
            "pass_rate": self.pass_rate,
            "valid_coverage": self.coverage,
            "attempts": self.attempt_rows(),
            "tasks": self.task_rows(),
        }


@dataclass(frozen=True)
class TasksetReportSummary:
    """Rich-renderable outcome of ``retro benchmark taskset report``."""

    name: str
    eval_id: str
    eval_dir: Path
    results_path: Path
    aggregate: BenchmarkAggregate

    @property
    def agents(self) -> tuple[Any, ...]:
        return self.aggregate.agents

    def agent_rows(self) -> list[dict[str, Any]]:
        return _agent_rows(self.aggregate)

    def task_rows(self) -> list[dict[str, Any]]:
        return _aggregate_task_rows(self.aggregate)

    def source_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in self.aggregate.agents:
            for source in agent.sources:
                rows.append(
                    {
                        "agent_id": agent.agent_id,
                        "source_id": source.source_id,
                        "mean_score": source.mean_score,
                        "pass_rate": source.pass_rate,
                        "tasks": len(source.task_ids),
                        "scored_tasks": len(source.scored_task_ids),
                    }
                )
        return rows

    def component_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, "component_id": key, "mean_value": value}
            for agent in self.aggregate.agents
            for key, value in sorted(agent.component_means.items())
        ]

    def resource_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for agent in self.aggregate.agents:
            resources = dict(agent.resources)
            tokens = resources.get("tokens_mean") or {}
            rows.append(
                {
                    "agent_id": agent.agent_id,
                    "scored_attempts": resources.get("scored_attempts", 0),
                    "tokens_mean": tokens,
                    "wall_time_ms_mean": resources.get("wall_time_ms_mean"),
                    "cost_usd_mean": resources.get("cost_usd_mean"),
                    "cost_usd_total": resources.get("cost_usd_total"),
                }
            )
        return rows

    def budget_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, **budget.to_dict()}
            for agent in self.aggregate.agents
            for budget in agent.budget_conditionals
        ]

    def error_rows(self) -> list[dict[str, Any]]:
        return [
            {"agent_id": agent.agent_id, **{key: value for key, value in agent.error_counts.items()}}
            for agent in self.aggregate.agents
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "eval_id": self.eval_id,
            "eval_dir": str(self.eval_dir),
            "results_path": str(self.results_path),
            "aggregate": self.aggregate.to_dict(),
            "agents": self.agent_rows(),
            "sources": self.source_rows(),
            "tasks": self.task_rows(),
            "components": self.component_rows(),
            "resources": self.resource_rows(),
            "budgets": self.budget_rows(),
            "errors": self.error_rows(),
        }


def run_taskset(
    layout: Any,
    name: str,
    agent: str | Path | AgentSpec | None = None,
    seeds: str | Sequence[int] = (0,),
    ghostlab_bin: str | Path | None = None,
    *,
    eval_id: str | None = None,
    task_ids: Iterable[str] | None = None,
    force: bool = False,
    agent_id: str | None = None,
    expected_agent_sha256: str | None = None,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
    ghostlab: GhostlabCli | None = None,
    ghostlab_env: Mapping[str, str] | None = None,
    aut_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
    candidate_export_name: str = CANDIDATE_EXPORT_NAME,
) -> TasksetRunSummary:
    """Run ``retro benchmark taskset run`` (spec sections 15 and 20).

    Attempts stay hash-addressed over task, prompt, environment, effective agent
    assets/settings, seed, base, scorer, and Ghostlab version. Only that complete
    fingerprint may reuse ``attempt.json`` unless ``force=True``.
    """
    paths = resolve_taskset_paths(layout, name)
    if agent is None:
        raise TaskVerificationError("--agent is required")
    spec = (
        agent
        if isinstance(agent, AgentSpec)
        else AgentSpec.from_path(
            Path(agent), agent_id=agent_id, expected_sha256=expected_agent_sha256
        )
    )
    resolved_seeds = parse_seeds(seeds)
    config = RunConfig(
        ghostlab=ghostlab or GhostlabCli(ghostlab_bin, env=ghostlab_env),
        eval_id=resolve_run_eval_id(paths, eval_id),
        seeds=resolved_seeds,
        aut_timeout_seconds=aut_timeout_seconds,
        scorer_timeout_seconds=scorer_timeout_seconds,
        candidate_export_name=candidate_export_name,
        force=force,
    )
    result = run_agent(
        paths,
        config,
        spec,
        task_ids,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return summarize_run(paths, result)


def summarize_run(paths: TasksetPaths, result: EvalResult) -> TasksetRunSummary:
    status_counts: dict[str, int] = {}
    for attempt in result.attempts:
        status_counts[attempt.status] = status_counts.get(attempt.status, 0) + 1
    agent = result.aggregate.agent(result.agent_id)
    return TasksetRunSummary(
        name=result.name,
        eval_id=result.eval_id,
        agent_id=result.agent_id,
        eval_dir=paths.eval_dir(result.eval_id),
        results_path=result.results_path,
        seeds=tuple(dict.fromkeys(attempt.seed for attempt in result.attempts)),
        attempts=result.attempts,
        requested_attempts=len(result.attempts),
        scored_attempts=sum(1 for attempt in result.attempts if attempt.status == SCORED_STATUS),
        reused_attempts=sum(1 for attempt in result.attempts if attempt.reused),
        status_counts=status_counts,
        benchmark_score=agent.benchmark_score if agent else None,
        pass_rate=agent.pass_rate if agent else None,
        coverage=agent.coverage if agent else 0.0,
        aggregate=result.aggregate,
    )


def report_taskset(
    layout: Any,
    name: str,
    eval_id: str | None = "latest",
    *,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
) -> TasksetReportSummary:
    """Run ``retro benchmark taskset report`` (spec sections 16 and 20).

    ``eval_id`` accepts ``latest`` (or ``None``) and resolves to the newest eval
    directory; the recomputed aggregate is republished to ``results.json``.
    """
    paths = resolve_taskset_paths(layout, name)
    resolved = resolve_eval_id(paths, eval_id)
    aggregate = collect_eval_report(
        paths,
        resolved,
        token_budgets=token_budgets,
        wall_time_budgets_ms=wall_time_budgets_ms,
    )
    return TasksetReportSummary(
        name=paths.name,
        eval_id=resolved,
        eval_dir=paths.eval_dir(resolved),
        results_path=paths.results_path(resolved),
        aggregate=aggregate,
    )
