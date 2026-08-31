"""Score-report validation and source-normalized benchmark aggregation.

Implements spec sections 10.5 and 16: a scored attempt contributes a number,
every other status contributes only to error accounting, each rollout source
carries equal weight regardless of how many tasks it produced, and resource and
budget-conditional views are reported next to the score rather than folded into
it.
"""
from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ghostlab_cli import SCORE_REPORT_STATUSES, read_json, sha256_json, write_json
from .schema import ATTEMPT_STATUSES as ATTEMPT_STATUS_VALUES
from .schema import BenchmarkAttempt, SchemaError

ATTEMPT_SCHEMA = "retro-benchmark-attempt-v1"
AGGREGATE_SCHEMA = "retro-benchmark-aggregate-v1"

WEIGHT_SUM_TOLERANCE = 1e-9
TOTAL_MATCH_TOLERANCE = 1e-6
MAX_UNSCORED_WEIGHT = 0.20
DEFAULT_PASS_THRESHOLD = 0.8

SCORED_STATUS = "scored"

#: The evaluated agent (or its model provider) failed; not a task failure.
AGENT_ERROR_STATUSES = frozenset({"agent_error", "agent_timeout", "model_unavailable"})
#: The scorer failed; never converted into a numeric zero.
SCORER_ERROR_STATUSES = frozenset(
    {"scorer_error", "scorer_timeout", "judge_unavailable", "invalid_candidate_artifact"}
)
#: Retro's own orchestration failed.
HARNESS_ERROR_STATUSES = frozenset({"harness_error", "invalid_result"})

ATTEMPT_STATUSES = frozenset(ATTEMPT_STATUS_VALUES)

CANNOT_ASSESS = "CANNOT_ASSESS"
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEED_DIR_RE = re.compile(r"^seed-(0|[1-9][0-9]*)$")


def classify_status(status: str) -> str:
    if status == SCORED_STATUS:
        return "scored"
    if status in AGENT_ERROR_STATUSES:
        return "agent_error"
    if status in SCORER_ERROR_STATUSES:
        return "scorer_error"
    if status in HARNESS_ERROR_STATUSES:
        return "harness_error"
    return "unknown"


@dataclass(frozen=True)
class ComponentOutcome:
    id: str
    weight: float
    hard_gate: bool
    value: float | None
    gate_passed: bool | None
    scored: bool
    kind: str = "deterministic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "weight": self.weight,
            "hard_gate": self.hard_gate,
            "value": self.value,
            "gate_passed": self.gate_passed,
            "scored": self.scored,
        }


@dataclass(frozen=True)
class ScoreReportValidation:
    """Result of applying the spec's total-score rules to one score report."""

    status: str
    valid: bool
    score_total: float | None
    passed: bool | None
    computed_total: float | None
    unscored_weight: float
    hard_gate_failures: tuple[str, ...]
    components: tuple[ComponentOutcome, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "valid": self.valid,
            "score_total": self.score_total,
            "passed": self.passed,
            "computed_total": self.computed_total,
            "unscored_weight": self.unscored_weight,
            "hard_gate_failures": list(self.hard_gate_failures),
            "components": [component.to_dict() for component in self.components],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _component_outcome(raw: Mapping[str, Any]) -> tuple[ComponentOutcome, list[str]]:
    errors: list[str] = []
    component_id = raw.get("id")
    if not isinstance(component_id, str) or not component_id:
        errors.append("component is missing a string id")
        component_id = "<unknown>"
    raw_weight = raw.get("weight")
    weight = _as_float(raw_weight)
    if weight is None:
        errors.append(f"component {component_id!r} has a non-finite or non-numeric weight")
        weight = 0.0
    elif not 0.0 <= weight <= 1.0:
        errors.append(f"component {component_id!r} weight {weight} is outside [0, 1]")
    raw_value = raw.get("value")
    value = _as_float(raw_value)
    verdict = raw.get("verdict")
    if raw_value is not None and value is None:
        errors.append(f"component {component_id!r} has a non-finite or non-numeric value")
    scored = value is not None and verdict != CANNOT_ASSESS
    if scored and not (
        0.0 - TOTAL_MATCH_TOLERANCE <= (value or 0.0) <= 1.0 + TOTAL_MATCH_TOLERANCE
    ):
        errors.append(f"component {component_id!r} value {value} is outside [0, 1]")
    hard_gate_raw = raw.get("hard_gate")
    if not isinstance(hard_gate_raw, bool):
        errors.append(f"component {component_id!r} hard_gate must be a boolean")
        hard_gate = False
    else:
        hard_gate = hard_gate_raw
    gate_raw = raw.get("gate_passed")
    gate_passed = bool(gate_raw) if isinstance(gate_raw, bool) else None
    if gate_raw is not None and not isinstance(gate_raw, bool):
        errors.append(f"component {component_id!r} gate_passed must be a boolean or null")
    raw_kind = raw.get("kind")
    kind: str = raw_kind if isinstance(raw_kind, str) else "deterministic"
    return (
        ComponentOutcome(
            id=component_id,
            weight=weight,
            hard_gate=hard_gate,
            value=value if scored else None,
            gate_passed=gate_passed,
            scored=scored,
            kind=kind,
        ),
        errors,
    )


def validate_score_report(
    report: Mapping[str, Any],
    *,
    pass_threshold: float | None = None,
    scorer_manifest: Mapping[str, Any] | None = None,
    expected_task_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_scorer_package_sha256: str | None = None,
) -> ScoreReportValidation:
    """Validate report identity, scorer declarations, and host-recomputed totals."""
    errors: list[str] = []
    warnings: list[str] = []

    status = report.get("status")
    if not isinstance(status, str) or status not in SCORE_REPORT_STATUSES:
        return ScoreReportValidation(
            status=str(status),
            valid=False,
            score_total=None,
            passed=None,
            computed_total=None,
            unscored_weight=0.0,
            hard_gate_failures=(),
            components=(),
            errors=(f"unsupported score-report status {status!r}",),
            warnings=(),
        )

    for key, expected in (
        ("task_id", expected_task_id),
        ("attempt_id", expected_attempt_id),
        ("scorer_package_sha256", expected_scorer_package_sha256),
    ):
        if expected is None:
            continue
        actual = report.get(key)
        if key == "scorer_package_sha256" and isinstance(actual, str):
            actual = actual.removeprefix("sha256:")
            expected = expected.removeprefix("sha256:")
        if actual != expected:
            errors.append(f"{key} {actual!r} does not match expected {expected!r}")

    raw_components = report.get("components")
    components: list[ComponentOutcome] = []
    if isinstance(raw_components, list):
        for item in raw_components:
            if not isinstance(item, Mapping):
                errors.append("component entries must be JSON objects")
                continue
            component, component_errors = _component_outcome(item)
            components.append(component)
            errors.extend(component_errors)
    elif status == SCORED_STATUS:
        errors.append("a scored report must list components")

    if status != SCORED_STATUS:
        return ScoreReportValidation(
            status=status,
            valid=False,
            score_total=None,
            passed=None,
            computed_total=None,
            unscored_weight=0.0,
            hard_gate_failures=(),
            components=tuple(components),
            errors=tuple(errors),
            warnings=(f"status={status} produces no numeric task result",),
        )

    if not components:
        errors.append("a scored report must contain at least one component")
    component_ids = [component.id for component in components]
    if len(component_ids) != len(set(component_ids)):
        errors.append("score report component ids must be unique")

    declared_components: dict[str, Mapping[str, Any]] = {}
    if scorer_manifest is not None:
        raw_declared = scorer_manifest.get("components")
        if not isinstance(raw_declared, list) or not raw_declared:
            errors.append("published scorer manifest must declare nonempty components")
        else:
            for item in raw_declared:
                if not isinstance(item, Mapping):
                    errors.append("published scorer component entries must be objects")
                    continue
                declared_id = item.get("id")
                if not isinstance(declared_id, str) or not declared_id:
                    errors.append("published scorer component is missing a string id")
                    continue
                if declared_id in declared_components:
                    errors.append(f"published scorer component id {declared_id!r} is duplicated")
                declared_components[declared_id] = item

        declared_ids = set(declared_components)
        reported_ids = set(component_ids)
        if reported_ids != declared_ids:
            missing = sorted(declared_ids - reported_ids)
            extra = sorted(reported_ids - declared_ids)
            errors.append(
                "score report component ids differ from the published scorer manifest"
                f" (missing={missing}, extra={extra})"
            )
        for component in components:
            declared = declared_components.get(component.id)
            if declared is None:
                continue
            declared_weight = _as_float(declared.get("weight"))
            if declared_weight is None:
                errors.append(
                    f"published scorer component {component.id!r} has a non-finite weight"
                )
            elif abs(component.weight - declared_weight) > WEIGHT_SUM_TOLERANCE:
                errors.append(
                    f"component {component.id!r} weight {component.weight} does not match "
                    f"published weight {declared_weight}"
                )
            declared_gate = declared.get("hard_gate")
            if not isinstance(declared_gate, bool):
                errors.append(
                    f"published scorer component {component.id!r} hard_gate must be a boolean"
                )
            elif component.hard_gate != declared_gate:
                errors.append(
                    f"component {component.id!r} hard_gate={component.hard_gate} does not "
                    f"match published hard_gate={declared_gate}"
                )

    weight_sum = sum(component.weight for component in components)
    if not math.isfinite(weight_sum):
        errors.append("component weights produce a non-finite sum")
    elif components and abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
        errors.append(f"component weights sum to {weight_sum!r}, expected 1.0")

    for component in components:
        if component.hard_gate and component.gate_passed is None:
            errors.append(
                f"hard-gate component {component.id!r} must explicitly assess gate_passed"
            )
    gate_failures = tuple(
        component.id
        for component in components
        if component.hard_gate and component.gate_passed is False
    )
    declared_failures = report.get("hard_gate_failures")
    if not isinstance(declared_failures, list) or any(
        not isinstance(entry, str) or not entry for entry in declared_failures
    ):
        errors.append("hard_gate_failures must be an array of non-empty component ids")
    else:
        normalized_failures = tuple(dict.fromkeys(declared_failures))
        if len(normalized_failures) != len(declared_failures):
            errors.append("hard_gate_failures must not contain duplicates")
        if set(normalized_failures) != set(gate_failures):
            errors.append(
                f"hard_gate_failures {list(normalized_failures)!r} do not match host "
                f"assessment {list(gate_failures)!r}"
            )

    unscored_weight = sum(component.weight for component in components if not component.scored)

    if gate_failures:
        computed_total: float | None = 0.0
    else:
        computed_total = sum(
            (component.value or 0.0) * component.weight for component in components if component.scored
        )

    reported_total = _as_float(report.get("score_total"))
    if reported_total is None:
        errors.append("a scored report must carry a finite numeric score_total")
    elif not 0.0 <= reported_total <= 1.0:
        errors.append(f"score_total {reported_total} is outside [0, 1]")
    elif computed_total is not None and abs(reported_total - computed_total) > TOTAL_MATCH_TOLERANCE:
        errors.append(
            f"score_total {reported_total} does not match the component total {computed_total}"
        )

    if unscored_weight > MAX_UNSCORED_WEIGHT + WEIGHT_SUM_TOLERANCE:
        errors.append(
            f"unscored component weight {unscored_weight} exceeds the {MAX_UNSCORED_WEIGHT} limit"
        )
    elif unscored_weight > 0:
        warnings.append(
            f"{unscored_weight} of component weight is unscored and is not renormalized"
        )

    manifest_threshold = (
        _as_float(scorer_manifest.get("pass_threshold")) if scorer_manifest is not None else None
    )
    if (
        scorer_manifest is not None
        and "pass_threshold" in scorer_manifest
        and manifest_threshold is None
    ):
        errors.append("published scorer pass_threshold must be finite and numeric")
    threshold = pass_threshold if pass_threshold is not None else manifest_threshold
    if threshold is not None and (not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0):
        errors.append(f"pass threshold {threshold!r} must be finite and within [0, 1]")
        threshold = None
    if (
        pass_threshold is not None
        and manifest_threshold is not None
        and abs(pass_threshold - manifest_threshold) > TOTAL_MATCH_TOLERANCE
    ):
        errors.append(
            f"pass threshold {pass_threshold} does not match published threshold "
            f"{manifest_threshold}"
        )
    reported_passed = report.get("passed")
    if not isinstance(reported_passed, bool):
        errors.append("a scored report must carry a boolean passed value")
        reported_passed = None
    expected_pass: bool | None = None
    if threshold is not None and computed_total is not None:
        expected_pass = (
            False
            if gate_failures
            else computed_total + TOTAL_MATCH_TOLERANCE >= threshold
        )
        if reported_passed is not None and reported_passed != expected_pass:
            errors.append(
                f"passed={reported_passed} disagrees with host-computed total "
                f"{computed_total} at threshold {threshold}"
            )

    return ScoreReportValidation(
        status=status,
        valid=not errors,
        score_total=computed_total,
        passed=expected_pass,
        computed_total=computed_total,
        unscored_weight=unscored_weight,
        hard_gate_failures=tuple(dict.fromkeys(gate_failures)),
        components=tuple(components),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class AttemptRecord:
    """One immutable ``retro-benchmark-attempt-v1`` record."""

    attempt_id: str
    task_id: str
    agent_id: str
    seed: int
    status: str
    source_id: str | None = None
    score: float | None = None
    passed: bool | None = None
    pass_threshold: float | None = None
    components: tuple[ComponentOutcome, ...] = ()
    tokens: Mapping[str, int] = field(default_factory=dict)
    wall_time_ms: int = 0
    cost_usd: float | None = None
    agent_config_sha256: str | None = None
    base_bundle_sha256: str | None = None
    scorer_package_sha256: str | None = None
    input_sha256: str | None = None
    path: Path | None = None

    @property
    def valid(self) -> bool:
        return self.status == SCORED_STATUS and self.score is not None

    @property
    def status_class(self) -> str:
        return classify_status(self.status)

    @property
    def total_tokens(self) -> int:
        return sum(int(value) for value in self.tokens.values() if isinstance(value, (int, float)))

    @property
    def evaluation_fingerprint(self) -> str:
        """Stable compatibility key for one agent/task evaluation configuration."""
        if self.input_sha256:
            return self.input_sha256
        return sha256_json(
            {
                "agent_id": self.agent_id,
                "task_id": self.task_id,
                "agent_config_sha256": self.agent_config_sha256,
                "base_bundle_sha256": self.base_bundle_sha256,
                "scorer_package_sha256": self.scorer_package_sha256,
                "pass_threshold": self.pass_threshold,
            }
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any], *, path: Path | None = None) -> AttemptRecord:
        status = payload.get("status")
        if not isinstance(status, str):
            raise ValueError(f"attempt record is missing status: {path or payload}")
        score = _as_float(payload.get("score"))
        if status != SCORED_STATUS:
            score = None
        tokens_raw = payload.get("tokens")
        tokens = (
            {key: int(value) for key, value in tokens_raw.items() if isinstance(value, (int, float))}
            if isinstance(tokens_raw, Mapping)
            else {}
        )
        components: list[ComponentOutcome] = []
        raw_components = payload.get("components")
        if isinstance(raw_components, list):
            for item in raw_components:
                if isinstance(item, Mapping):
                    component, _ = _component_outcome(item)
                    components.append(component)
        threshold = _as_float(payload.get("pass_threshold"))
        seed_raw = payload.get("seed")
        return cls(
            attempt_id=str(payload.get("attempt_id", "")),
            task_id=str(payload.get("task_id", "")),
            agent_id=str(payload.get("agent_id", "")),
            seed=int(seed_raw) if isinstance(seed_raw, int) else 0,
            status=status,
            source_id=payload.get("source_id") if isinstance(payload.get("source_id"), str) else None,
            score=score,
            passed=payload.get("passed") if isinstance(payload.get("passed"), bool) else None,
            pass_threshold=threshold,
            components=tuple(components),
            tokens=tokens,
            wall_time_ms=int(payload.get("wall_time_ms") or 0),
            cost_usd=_as_float(payload.get("cost_usd")),
            agent_config_sha256=(
                payload.get("agent_config_sha256")
                if isinstance(payload.get("agent_config_sha256"), str)
                else None
            ),
            base_bundle_sha256=(
                payload.get("base_bundle_sha256")
                if isinstance(payload.get("base_bundle_sha256"), str)
                else None
            ),
            scorer_package_sha256=(
                payload.get("scorer_package_sha256")
                if isinstance(payload.get("scorer_package_sha256"), str)
                else None
            ),
            input_sha256=(
                payload.get("input_sha256")
                if isinstance(payload.get("input_sha256"), str)
                else None
            ),
            path=path,
        )


def validate_attempt_record(
    payload: Mapping[str, Any], *, path: Path | None = None
) -> AttemptRecord:
    """Strictly parse a persisted attempt before it can enter aggregation."""
    where = f"attempt record {path}" if path else "attempt record"
    try:
        parsed = BenchmarkAttempt.from_dict(payload, where=where)
    except SchemaError as exc:
        raise ValueError(str(exc)) from exc
    if "pass_threshold" not in payload:
        raise ValueError(
            f"{where}.pass_threshold is missing; legacy attempts without immutable "
            "threshold provenance must be rerun"
        )

    if not _SAFE_ID_RE.fullmatch(parsed.agent_id):
        raise ValueError(f"{where} has unsafe agent_id {parsed.agent_id!r}")
    if not _SAFE_ID_RE.fullmatch(parsed.attempt_id):
        raise ValueError(f"{where} has unsafe attempt_id {parsed.attempt_id!r}")
    for key in (
        "agent_config_sha256",
        "base_bundle_sha256",
        "scorer_package_sha256",
        "input_sha256",
    ):
        value = getattr(parsed, key)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ValueError(f"{where}.{key} must be a lowercase sha256")
    if parsed.candidate_state_sha256 is not None and not _SHA256_RE.fullmatch(
        parsed.candidate_state_sha256
    ):
        raise ValueError(f"{where}.candidate_state_sha256 must be a lowercase sha256 or null")
    for key, value in (
        ("score", parsed.score),
        ("pass_threshold", parsed.pass_threshold),
        ("cost_usd", parsed.cost_usd),
    ):
        if value is not None and not math.isfinite(float(value)):
            raise ValueError(f"{where}.{key} must be finite")
    if any(value < 0 for value in parsed.tokens.values()):
        raise ValueError(f"{where}.tokens values must be non-negative")
    if parsed.status == SCORED_STATUS:
        if not parsed.components:
            raise ValueError(f"{where}.components must not be empty for a scored attempt")
        outcomes: list[ComponentOutcome] = []
        for component in parsed.components:
            outcome, component_errors = _component_outcome(component)
            if component_errors:
                raise ValueError(f"{where} has invalid components: {'; '.join(component_errors)}")
            if outcome.hard_gate and outcome.gate_passed is None:
                raise ValueError(
                    f"{where} hard-gate component {outcome.id!r} has no gate assessment"
                )
            outcomes.append(outcome)
        assert parsed.score is not None
        assert parsed.passed is not None
        weight_sum = sum(component.weight for component in outcomes)
        if abs(weight_sum - 1.0) > WEIGHT_SUM_TOLERANCE:
            raise ValueError(f"{where} component weights do not sum to 1.0")
        unscored_weight = sum(
            component.weight for component in outcomes if not component.scored
        )
        if unscored_weight > MAX_UNSCORED_WEIGHT + WEIGHT_SUM_TOLERANCE:
            raise ValueError(f"{where} has too much unscored component weight")
        gate_failed = any(
            component.hard_gate and component.gate_passed is False for component in outcomes
        )
        computed_score = (
            0.0
            if gate_failed
            else sum(
                (component.value or 0.0) * component.weight
                for component in outcomes
                if component.scored
            )
        )
        if abs(parsed.score - computed_score) > TOTAL_MATCH_TOLERANCE:
            raise ValueError(f"{where}.score does not match its component total")
        expected_pass = (
            False
            if gate_failed
            else parsed.score + TOTAL_MATCH_TOLERANCE >= parsed.pass_threshold
        )
        if parsed.passed != expected_pass:
            raise ValueError(
                f"{where}.passed disagrees with score at pass_threshold"
            )
    return AttemptRecord.from_mapping(parsed.to_dict(), path=path)


def load_attempts(root: Path) -> list[AttemptRecord]:
    """Read only ``attempts/<task>/<agent>/seed-N/attempt.json`` records."""
    attempts_root = root if root.name == "attempts" else root / "attempts"
    if not attempts_root.is_dir():
        return []
    records: list[AttemptRecord] = []
    for path in sorted(attempts_root.glob("*/*/seed-*/attempt.json")):
        task_id = path.parents[2].name
        agent_id = path.parents[1].name
        seed_match = _SEED_DIR_RE.fullmatch(path.parent.name)
        if (
            not _SAFE_ID_RE.fullmatch(task_id)
            or not _SAFE_ID_RE.fullmatch(agent_id)
            or seed_match is None
        ):
            raise ValueError(f"attempt record has a non-canonical path: {path}")
        payload = read_json(path, label=f"attempt record {path}")
        if not isinstance(payload, Mapping):
            raise ValueError(f"attempt record {path} must be a JSON object")
        record = validate_attempt_record(payload, path=path)
        seed = int(seed_match.group(1))
        if (record.task_id, record.agent_id, record.seed) != (task_id, agent_id, seed):
            raise ValueError(
                f"attempt record {path} identity does not match its canonical path"
            )
        records.append(record)
    return records


@dataclass(frozen=True)
class BudgetConditional:
    dimension: str
    budget: float
    score: float | None
    within_budget: int
    over_budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "budget": self.budget,
            "score": self.score,
            "within_budget_attempts": self.within_budget,
            "over_budget_attempts": self.over_budget,
        }


@dataclass(frozen=True)
class TaskAggregate:
    task_id: str
    source_id: str
    agent_id: str
    pass_threshold: float
    scores: tuple[float, ...]
    mean_score: float | None
    std_score: float | None
    pass_rate: float | None
    requested_attempts: int
    scored_attempts: int
    status_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "source_id": self.source_id,
            "agent_id": self.agent_id,
            "pass_threshold": self.pass_threshold,
            "scores": list(self.scores),
            "mean_score": self.mean_score,
            "std_score": self.std_score,
            "std_kind": "population",
            "pass_rate": self.pass_rate,
            "requested_attempts": self.requested_attempts,
            "scored_attempts": self.scored_attempts,
            "status_counts": dict(sorted(self.status_counts.items())),
        }


@dataclass(frozen=True)
class SourceAggregate:
    source_id: str
    agent_id: str
    task_ids: tuple[str, ...]
    scored_task_ids: tuple[str, ...]
    mean_score: float | None
    pass_rate: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "agent_id": self.agent_id,
            "task_ids": list(self.task_ids),
            "scored_task_ids": list(self.scored_task_ids),
            "mean_score": self.mean_score,
            "pass_rate": self.pass_rate,
        }


@dataclass(frozen=True)
class AgentAggregate:
    agent_id: str
    benchmark_score: float | None
    pass_rate: float | None
    coverage: float
    requested_attempts: int
    scored_attempts: int
    status_counts: Mapping[str, int]
    error_counts: Mapping[str, int]
    tasks: tuple[TaskAggregate, ...]
    sources: tuple[SourceAggregate, ...]
    component_means: Mapping[str, float]
    resources: Mapping[str, Any]
    budget_conditionals: tuple[BudgetConditional, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "benchmark_score": self.benchmark_score,
            "pass_rate": self.pass_rate,
            "valid_coverage": self.coverage,
            "requested_attempts": self.requested_attempts,
            "scored_attempts": self.scored_attempts,
            "status_counts": dict(sorted(self.status_counts.items())),
            "error_counts": dict(sorted(self.error_counts.items())),
            "tasks": [task.to_dict() for task in self.tasks],
            "sources": [source.to_dict() for source in self.sources],
            "component_means": dict(sorted(self.component_means.items())),
            "resources": dict(sorted(self.resources.items())),
            "budget_conditionals": [budget.to_dict() for budget in self.budget_conditionals],
        }


@dataclass(frozen=True)
class BenchmarkAggregate:
    name: str
    eval_id: str
    agents: tuple[AgentAggregate, ...]
    generated_at: str | None = None

    def agent(self, agent_id: str) -> AgentAggregate | None:
        for candidate in self.agents:
            if candidate.agent_id == agent_id:
                return candidate
        return None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": AGGREGATE_SCHEMA,
            "name": self.name,
            "eval_id": self.eval_id,
            "agents": [agent.to_dict() for agent in self.agents],
        }
        if self.generated_at:
            payload["generated_at"] = self.generated_at
        return payload


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _pstdev(values: Sequence[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return 0.0
    return statistics.pstdev(values)


def _resolve_source(
    record: AttemptRecord, task_sources: Mapping[str, str] | None
) -> str:
    if task_sources and record.task_id in task_sources:
        return task_sources[record.task_id]
    if record.source_id:
        return record.source_id
    return f"task:{record.task_id}"


def _budget_score(
    task_groups: Mapping[tuple[str, str], list[AttemptRecord]],
    dimension: str,
    budget: float,
) -> BudgetConditional:
    within = 0
    over = 0
    per_task: dict[tuple[str, str], list[float]] = {}
    for key, records in task_groups.items():
        scores: list[float] = []
        for record in records:
            if not record.valid or record.score is None:
                continue
            usage = record.total_tokens if dimension == "tokens" else float(record.wall_time_ms)
            if usage > budget:
                over += 1
                scores.append(0.0)
            else:
                within += 1
                scores.append(record.score)
        if scores:
            per_task[key] = scores
    source_scores: dict[str, list[float]] = {}
    for (source_id, _task_id), scores in per_task.items():
        mean = _mean(scores)
        if mean is not None:
            source_scores.setdefault(source_id, []).append(mean)
    means = [value for value in (_mean(scores) for scores in source_scores.values()) if value is not None]
    return BudgetConditional(
        dimension=dimension,
        budget=budget,
        score=_mean(means),
        within_budget=within,
        over_budget=over,
    )


def aggregate_attempts(
    attempts: Iterable[AttemptRecord],
    *,
    name: str = "",
    eval_id: str = "",
    task_sources: Mapping[str, str] | None = None,
    task_thresholds: Mapping[str, float] | None = None,
    requested_attempts: Mapping[str, int] | None = None,
    token_budgets: Sequence[float] = (),
    wall_time_budgets_ms: Sequence[float] = (),
    generated_at: str | None = None,
) -> BenchmarkAggregate:
    """Compute source-normalized benchmark scores (spec section 16).

    ``task_thresholds`` is retained as a compatibility assertion for callers
    that already supply it. Persisted attempt thresholds remain authoritative
    and an inconsistent assertion is rejected rather than used as an override.
    """
    materialized = list(attempts)
    _validate_evaluation_compatibility(
        materialized,
        asserted_task_thresholds=task_thresholds,
    )
    by_agent: dict[str, list[AttemptRecord]] = {}
    for record in materialized:
        by_agent.setdefault(record.agent_id, []).append(record)

    agents: list[AgentAggregate] = []
    for agent_id in sorted(by_agent):
        records = by_agent[agent_id]
        agents.append(
            _aggregate_agent(
                agent_id,
                records,
                task_sources=task_sources,
                requested=(requested_attempts or {}).get(agent_id),
                token_budgets=token_budgets,
                wall_time_budgets_ms=wall_time_budgets_ms,
            )
        )
    return BenchmarkAggregate(
        name=name, eval_id=eval_id, agents=tuple(agents), generated_at=generated_at
    )


def _validate_evaluation_compatibility(
    records: Sequence[AttemptRecord],
    *,
    asserted_task_thresholds: Mapping[str, float] | None = None,
) -> None:
    """Reject provenance drift that would otherwise be averaged as one evaluation."""

    def check(
        grouped: Mapping[Any, set[Any]],
        label: str,
    ) -> None:
        for key, values in grouped.items():
            if len(values) > 1:
                raise ValueError(
                    f"incompatible {label} for {key!r}; refusing to mix evaluation fingerprints"
                )

    agent_configs: dict[str, set[str | None]] = {}
    task_bases: dict[str, set[str | None]] = {}
    task_scorers: dict[str, set[str | None]] = {}
    task_thresholds: dict[str, set[float]] = {}
    evaluation_inputs: dict[tuple[str, str], set[str | None]] = {}
    for record in records:
        for label, value in (
            ("agent configuration", record.agent_config_sha256),
            ("base bundle", record.base_bundle_sha256),
            ("scorer package", record.scorer_package_sha256),
            ("input", record.input_sha256),
        ):
            if value is None or not _SHA256_RE.fullmatch(value):
                raise ValueError(
                    f"attempt {record.attempt_id!r} has missing or invalid {label} provenance"
                )
        threshold = record.pass_threshold
        if (
            threshold is None
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError(
                f"attempt {record.attempt_id!r} has missing or invalid pass threshold provenance"
            )
        agent_configs.setdefault(record.agent_id, set()).add(record.agent_config_sha256)
        task_bases.setdefault(record.task_id, set()).add(record.base_bundle_sha256)
        task_scorers.setdefault(record.task_id, set()).add(record.scorer_package_sha256)
        task_thresholds.setdefault(record.task_id, set()).add(threshold)
        evaluation_inputs.setdefault((record.agent_id, record.task_id), set()).add(
            record.input_sha256
        )
    check(agent_configs, "agent configuration provenance")
    check(task_bases, "base bundle provenance")
    check(task_scorers, "scorer package provenance")
    check(task_thresholds, "pass threshold provenance")
    check(evaluation_inputs, "input provenance")

    for task_id, asserted in (asserted_task_thresholds or {}).items():
        if (
            isinstance(asserted, bool)
            or not isinstance(asserted, (int, float))
            or not math.isfinite(float(asserted))
            or not 0.0 <= float(asserted) <= 1.0
        ):
            raise ValueError(
                f"asserted pass threshold provenance for task {task_id!r} is invalid"
            )
        persisted = task_thresholds.get(task_id)
        if persisted and any(
            abs(value - float(asserted)) > TOTAL_MATCH_TOLERANCE for value in persisted
        ):
            raise ValueError(
                f"asserted pass threshold for task {task_id!r} does not match immutable "
                "attempt provenance"
            )


def _aggregate_agent(
    agent_id: str,
    records: Sequence[AttemptRecord],
    *,
    task_sources: Mapping[str, str] | None,
    requested: int | None,
    token_budgets: Sequence[float],
    wall_time_budgets_ms: Sequence[float],
) -> AgentAggregate:
    task_groups: dict[tuple[str, str], list[AttemptRecord]] = {}
    for record in records:
        source_id = _resolve_source(record, task_sources)
        task_groups.setdefault((source_id, record.task_id), []).append(record)

    status_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {"agent_error": 0, "scorer_error": 0, "harness_error": 0}
    for record in records:
        status_counts[record.status] = status_counts.get(record.status, 0) + 1
        bucket = record.status_class
        if bucket in error_counts:
            error_counts[bucket] += 1

    tasks: list[TaskAggregate] = []
    for (source_id, task_id) in sorted(task_groups):
        group = task_groups[(source_id, task_id)]
        threshold = group[0].pass_threshold
        if threshold is None:
            raise ValueError(
                f"task {task_id!r} has no immutable pass threshold provenance"
            )
        scores = [record.score for record in group if record.valid and record.score is not None]
        passes = [
            1.0 if (record.score or 0.0) + TOTAL_MATCH_TOLERANCE >= threshold else 0.0
            for record in group
            if record.valid and record.score is not None
        ]
        task_status_counts: dict[str, int] = {}
        for record in group:
            task_status_counts[record.status] = task_status_counts.get(record.status, 0) + 1
        tasks.append(
            TaskAggregate(
                task_id=task_id,
                source_id=source_id,
                agent_id=agent_id,
                pass_threshold=threshold,
                scores=tuple(scores),
                mean_score=_mean(scores),
                std_score=_pstdev(scores),
                pass_rate=_mean(passes),
                requested_attempts=len(group),
                scored_attempts=len(scores),
                status_counts=task_status_counts,
            )
        )

    sources: list[SourceAggregate] = []
    by_source: dict[str, list[TaskAggregate]] = {}
    for task in tasks:
        by_source.setdefault(task.source_id, []).append(task)
    for source_id in sorted(by_source):
        source_tasks = by_source[source_id]
        task_means = [task.mean_score for task in source_tasks if task.mean_score is not None]
        task_pass_rates = [task.pass_rate for task in source_tasks if task.pass_rate is not None]
        sources.append(
            SourceAggregate(
                source_id=source_id,
                agent_id=agent_id,
                task_ids=tuple(task.task_id for task in source_tasks),
                scored_task_ids=tuple(
                    task.task_id for task in source_tasks if task.mean_score is not None
                ),
                mean_score=_mean(task_means),
                pass_rate=_mean(task_pass_rates),
            )
        )

    source_means = [source.mean_score for source in sources if source.mean_score is not None]
    source_pass_rates = [source.pass_rate for source in sources if source.pass_rate is not None]

    scored = [record for record in records if record.valid]
    requested_total = requested if requested is not None else len(records)
    coverage = (len(scored) / requested_total) if requested_total else 0.0

    component_totals: dict[str, list[float]] = {}
    for record in scored:
        for component in record.components:
            if component.scored and component.value is not None:
                component_totals.setdefault(component.id, []).append(component.value)
    component_means = {
        key: value
        for key, value in ((key, _mean(values)) for key, values in component_totals.items())
        if value is not None
    }

    resources = _resource_summary(scored)
    budgets = [
        _budget_score(task_groups, "tokens", float(budget)) for budget in token_budgets
    ] + [
        _budget_score(task_groups, "wall_time_ms", float(budget)) for budget in wall_time_budgets_ms
    ]

    return AgentAggregate(
        agent_id=agent_id,
        benchmark_score=_mean(source_means),
        pass_rate=_mean(source_pass_rates),
        coverage=coverage,
        requested_attempts=requested_total,
        scored_attempts=len(scored),
        status_counts=status_counts,
        error_counts=error_counts,
        tasks=tuple(tasks),
        sources=tuple(sources),
        component_means=component_means,
        resources=resources,
        budget_conditionals=tuple(budgets),
    )


def _resource_summary(scored: Sequence[AttemptRecord]) -> dict[str, Any]:
    token_keys: set[str] = set()
    for record in scored:
        token_keys.update(record.tokens)
    tokens_mean: dict[str, float] = {}
    for key in sorted(token_keys):
        values = [float(record.tokens.get(key, 0)) for record in scored]
        mean = _mean(values)
        if mean is not None:
            tokens_mean[key] = mean
    wall_values = [float(record.wall_time_ms) for record in scored]
    cost_values = [record.cost_usd for record in scored if record.cost_usd is not None]
    return {
        "scored_attempts": len(scored),
        "tokens_mean": tokens_mean,
        "tokens_total": {
            key: sum(int(record.tokens.get(key, 0)) for record in scored) for key in sorted(token_keys)
        },
        "wall_time_ms_mean": _mean(wall_values),
        "wall_time_ms_total": sum(int(value) for value in wall_values),
        "cost_usd_mean": _mean([float(value) for value in cost_values]),
        "cost_usd_total": sum(float(value) for value in cost_values) if cost_values else None,
    }


def write_aggregate(path: Path, aggregate: BenchmarkAggregate) -> Path:
    write_json(path, aggregate.to_dict())
    return path


def iter_status_rows(aggregate: BenchmarkAggregate) -> Iterator[dict[str, Any]]:
    """Flat rows convenient for CLI tables."""
    for agent in aggregate.agents:
        for task in agent.tasks:
            yield {
                "agent_id": agent.agent_id,
                "source_id": task.source_id,
                "task_id": task.task_id,
                "mean_score": task.mean_score,
                "std_score": task.std_score,
                "pass_rate": task.pass_rate,
                "scored_attempts": task.scored_attempts,
                "requested_attempts": task.requested_attempts,
            }


__all__ = [
    "AGENT_ERROR_STATUSES",
    "AGGREGATE_SCHEMA",
    "ATTEMPT_SCHEMA",
    "ATTEMPT_STATUSES",
    "CANNOT_ASSESS",
    "DEFAULT_PASS_THRESHOLD",
    "HARNESS_ERROR_STATUSES",
    "MAX_UNSCORED_WEIGHT",
    "SCORER_ERROR_STATUSES",
    "SCORED_STATUS",
    "AgentAggregate",
    "AttemptRecord",
    "BenchmarkAggregate",
    "BudgetConditional",
    "ComponentOutcome",
    "ScoreReportValidation",
    "SourceAggregate",
    "TaskAggregate",
    "aggregate_attempts",
    "classify_status",
    "iter_status_rows",
    "load_attempts",
    "validate_attempt_record",
    "validate_score_report",
    "write_aggregate",
]
