"""Tests for score-report validation and source-normalized aggregation."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from retro.benchmarks.task_scorer.aggregate import (
    AttemptRecord,
    aggregate_attempts,
    classify_status,
    iter_status_rows,
    load_attempts,
    validate_score_report,
    write_aggregate,
)


def _report(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "retro-score-report-v1",
        "task_id": "t1",
        "attempt_id": "a1",
        "scorer_package_sha256": "c" * 64,
        "status": "scored",
        "score_total": 0.86,
        "passed": True,
        "components": [
            {
                "id": "requested_behavior",
                "kind": "deterministic",
                "value": 1.0,
                "weight": 0.7,
                "hard_gate": True,
                "gate_passed": True,
            },
            {
                "id": "regression_suite",
                "kind": "deterministic",
                "value": 0.8,
                "weight": 0.2,
                "hard_gate": True,
                "gate_passed": True,
            },
            {
                "id": "project_fit",
                "kind": "judge",
                "value": 0.0,
                "weight": 0.1,
                "hard_gate": False,
                "gate_passed": None,
            },
        ],
        "hard_gate_failures": [],
    }
    payload.update(overrides)
    return payload


def _manifest() -> dict[str, Any]:
    return {
        "components": [
            {
                "id": "requested_behavior",
                "kind": "deterministic",
                "weight": 0.7,
                "hard_gate": True,
            },
            {
                "id": "regression_suite",
                "kind": "deterministic",
                "weight": 0.2,
                "hard_gate": True,
            },
            {
                "id": "project_fit",
                "kind": "judge",
                "weight": 0.1,
                "hard_gate": False,
            },
        ],
        "pass_threshold": 0.8,
    }


def _attempt(
    task_id: str,
    source_id: str,
    agent_id: str,
    seed: int,
    status: str = "scored",
    score: float | None = 1.0,
    **extra: Any,
) -> AttemptRecord:
    threshold = extra.get("pass_threshold", 0.8)
    payload: dict[str, Any] = {
        "attempt_id": f"{task_id}-{agent_id}-{seed}",
        "task_id": task_id,
        "source_id": source_id,
        "agent_id": agent_id,
        "seed": seed,
        "status": status,
        "score": score,
        "passed": (
            bool(score >= threshold)
            if status == "scored" and score is not None
            else None
        ),
        "pass_threshold": threshold,
        "tokens": {"input": 1000, "output": 200},
        "wall_time_ms": 60000,
        "cost_usd": 0.05,
        "agent_config_sha256": "a" * 64,
        "base_bundle_sha256": "b" * 64,
        "scorer_package_sha256": "c" * 64,
        "input_sha256": "d" * 64,
        "components": [
            {
                "id": "requested_behavior",
                "kind": "deterministic",
                "value": score if score is not None else None,
                "weight": 1.0,
                "hard_gate": True,
                "gate_passed": True,
            }
        ],
    }
    payload.update(extra)
    return AttemptRecord.from_mapping(payload)


def test_valid_report_totals_match_components() -> None:
    result = validate_score_report(_report(), pass_threshold=0.8)
    assert result.valid is True
    assert result.score_total == pytest.approx(0.86)
    assert result.computed_total == pytest.approx(0.86)
    assert result.passed is True
    assert result.unscored_weight == pytest.approx(0.0)
    assert result.errors == ()


def test_hard_gate_failure_forces_a_zero_total() -> None:
    report = _report(score_total=0.0, passed=False)
    report["components"][0]["gate_passed"] = False
    report["hard_gate_failures"] = ["requested_behavior"]
    result = validate_score_report(report, pass_threshold=0.8)
    assert result.valid is True
    assert result.computed_total == pytest.approx(0.0)
    assert result.hard_gate_failures == ("requested_behavior",)
    assert result.passed is False


def test_weights_must_sum_to_one() -> None:
    report = _report()
    report["components"][2]["weight"] = 0.4
    result = validate_score_report(report)
    assert result.valid is False
    assert any("weights sum to" in error for error in result.errors)


def test_score_total_must_match_the_components() -> None:
    result = validate_score_report(_report(score_total=0.99))
    assert result.valid is False
    assert any("does not match the component total" in error for error in result.errors)


def test_cannot_assess_weight_is_reported_not_renormalized() -> None:
    report = _report(score_total=0.7 * 1.0 + 0.2 * 0.8)
    report["components"][2]["value"] = None
    report["components"][2]["verdict"] = "CANNOT_ASSESS"
    result = validate_score_report(report)
    assert result.valid is True
    assert result.unscored_weight == pytest.approx(0.1)
    assert result.computed_total == pytest.approx(0.86)
    assert any("not renormalized" in warning for warning in result.warnings)


def test_more_than_twenty_percent_unscored_weight_is_invalid() -> None:
    report = _report(score_total=0.7)
    report["components"][1]["value"] = None
    report["components"][1]["verdict"] = "CANNOT_ASSESS"
    report["components"][2]["value"] = None
    report["components"][2]["verdict"] = "CANNOT_ASSESS"
    result = validate_score_report(report)
    assert result.valid is False
    assert any("exceeds the 0.2 limit" in error for error in result.errors)


def test_passed_flag_must_agree_with_the_threshold() -> None:
    result = validate_score_report(_report(passed=False), pass_threshold=0.8)
    assert result.valid is False
    assert any("disagrees with host-computed total" in error for error in result.errors)


def test_component_values_outside_the_range_are_invalid() -> None:
    report = _report()
    report["components"][0]["value"] = 1.5
    result = validate_score_report(report)
    assert result.valid is False
    assert any("outside [0, 1]" in error for error in result.errors)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report["components"].pop(), "component ids differ"),
        (
            lambda report: report["components"][0].update(weight=0.6),
            "does not match published weight",
        ),
        (
            lambda report: report["components"][0].update(hard_gate=False),
            "does not match published hard_gate",
        ),
        (
            lambda report: report["components"][0].update(gate_passed=None),
            "explicitly assess gate_passed",
        ),
    ],
)
def test_report_components_must_match_published_manifest(mutation, message: str) -> None:
    report = copy.deepcopy(_report())
    mutation(report)
    result = validate_score_report(
        report, pass_threshold=0.8, scorer_manifest=_manifest()
    )
    assert result.valid is False
    assert any(message in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "expected", "message"),
    [
        ("task_id", "expected-task", "task_id"),
        ("attempt_id", "expected-attempt", "attempt_id"),
        ("scorer_package_sha256", "e" * 64, "scorer_package_sha256"),
    ],
)
def test_report_identity_must_match_current_attempt(
    field: str, expected: str, message: str
) -> None:
    report = _report()
    result = validate_score_report(
        report,
        expected_task_id=expected if field == "task_id" else None,
        expected_attempt_id=expected if field == "attempt_id" else None,
        expected_scorer_package_sha256=(
            expected if field == "scorer_package_sha256" else None
        ),
    )
    assert result.valid is False
    assert any(message in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("task_id", "expected-task"),
        ("attempt_id", "expected-attempt"),
        ("scorer_package_sha256", "e" * 64),
    ],
)
def test_failure_report_identity_is_validated_before_status_branching(
    field: str, expected: str
) -> None:
    report = _report(
        status="scorer_error",
        score_total=None,
        passed=None,
        components=[],
    )
    result = validate_score_report(
        report,
        expected_task_id=expected if field == "task_id" else "t1",
        expected_attempt_id=expected if field == "attempt_id" else "a1",
        expected_scorer_package_sha256=(
            expected if field == "scorer_package_sha256" else "c" * 64
        ),
    )
    assert result.valid is False
    assert any(field in error for error in result.errors)
    assert result.score_total is None


@pytest.mark.parametrize("field", ["score_total", "component"])
def test_report_rejects_non_finite_numbers(field: str) -> None:
    report = _report()
    if field == "score_total":
        report["score_total"] = float("nan")
    else:
        report["components"][0]["value"] = float("inf")
    result = validate_score_report(report, scorer_manifest=_manifest())
    assert result.valid is False
    assert any("finite" in error for error in result.errors)


def test_hard_gate_failure_list_must_match_explicit_assessments() -> None:
    report = _report()
    report["hard_gate_failures"] = ["requested_behavior"]
    result = validate_score_report(report, scorer_manifest=_manifest())
    assert result.valid is False
    assert any("do not match host assessment" in error for error in result.errors)


def test_scored_report_components_must_not_be_empty() -> None:
    report = _report(components=[], score_total=0.0, passed=False)
    result = validate_score_report(report, scorer_manifest=_manifest())
    assert result.valid is False
    assert any("at least one component" in error for error in result.errors)


@pytest.mark.parametrize(
    "status", ["scorer_error", "scorer_timeout", "judge_unavailable", "invalid_candidate_artifact"]
)
def test_failure_statuses_produce_no_number(status: str) -> None:
    result = validate_score_report(
        {"schema_version": "retro-score-report-v1", "status": status, "components": []}
    )
    assert result.valid is False
    assert result.score_total is None
    assert result.warnings and "no numeric task result" in result.warnings[0]


def test_unknown_status_is_rejected() -> None:
    result = validate_score_report({"status": "made_up"})
    assert result.valid is False
    assert result.errors == ("unsupported score-report status 'made_up'",)


def test_status_classification() -> None:
    assert classify_status("scored") == "scored"
    assert classify_status("agent_timeout") == "agent_error"
    assert classify_status("model_unavailable") == "agent_error"
    assert classify_status("scorer_timeout") == "scorer_error"
    assert classify_status("invalid_result") == "harness_error"
    assert classify_status("nonsense") == "unknown"


def test_source_normalization_gives_each_rollout_equal_weight() -> None:
    attempts = [
        _attempt("t1", "src-a", "agent", 0, score=1.0),
        _attempt("t2", "src-a", "agent", 0, score=1.0),
        _attempt("t3", "src-a", "agent", 0, score=1.0),
        _attempt("t4", "src-b", "agent", 0, score=0.0),
    ]
    aggregate = aggregate_attempts(attempts, name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    # A naive attempt mean would be 0.75; source normalization yields 0.5.
    assert agent.benchmark_score == pytest.approx(0.5)
    assert {source.source_id for source in agent.sources} == {"src-a", "src-b"}
    assert agent.pass_rate == pytest.approx(0.5)


def test_invalid_results_are_excluded_from_numbers_but_counted() -> None:
    attempts = [
        _attempt("t1", "src-a", "agent", 0, score=1.0),
        _attempt("t1", "src-a", "agent", 1, status="scorer_error", score=None),
        _attempt("t1", "src-a", "agent", 2, status="agent_timeout", score=None),
        _attempt("t2", "src-b", "agent", 0, status="harness_error", score=None),
    ]
    aggregate = aggregate_attempts(attempts, name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    # Only the single scored attempt contributes; the empty source drops out.
    assert agent.benchmark_score == pytest.approx(1.0)
    assert agent.scored_attempts == 1
    assert agent.requested_attempts == 4
    assert agent.coverage == pytest.approx(0.25)
    assert agent.error_counts == {"agent_error": 1, "scorer_error": 1, "harness_error": 1}
    assert agent.status_counts["scorer_error"] == 1

    by_task = {task.task_id: task for task in agent.tasks}
    assert by_task["t1"].scored_attempts == 1
    assert by_task["t1"].requested_attempts == 3
    assert by_task["t2"].mean_score is None
    assert "src-b" in {source.source_id for source in agent.sources}


def test_seed_spread_and_pass_rate_use_the_task_threshold() -> None:
    attempts = [
        _attempt("t1", "src-a", "agent", 0, score=1.0, pass_threshold=0.4),
        _attempt("t1", "src-a", "agent", 1, score=0.5, pass_threshold=0.4),
        _attempt("t1", "src-a", "agent", 2, score=0.0, pass_threshold=0.4),
    ]
    aggregate = aggregate_attempts(attempts, name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    task = agent.tasks[0]
    assert task.pass_threshold == pytest.approx(0.4)
    assert task.mean_score == pytest.approx(0.5)
    assert task.std_score == pytest.approx(0.408248, abs=1e-5)
    assert task.pass_rate == pytest.approx(2 / 3)


def test_component_means_and_resource_summary() -> None:
    attempts = [
        _attempt("t1", "src-a", "agent", 0, score=1.0),
        _attempt("t1", "src-a", "agent", 1, score=0.0),
    ]
    aggregate = aggregate_attempts(attempts, name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    assert agent.component_means["requested_behavior"] == pytest.approx(0.5)
    assert agent.resources["tokens_mean"]["input"] == pytest.approx(1000.0)
    assert agent.resources["tokens_total"]["output"] == 400
    assert agent.resources["wall_time_ms_mean"] == pytest.approx(60000.0)
    assert agent.resources["cost_usd_total"] == pytest.approx(0.1)


def test_budget_conditionals_zero_out_over_budget_attempts() -> None:
    attempts = [
        _attempt("t1", "src-a", "agent", 0, score=1.0, tokens={"input": 100, "output": 100}),
        _attempt("t2", "src-b", "agent", 0, score=1.0, tokens={"input": 5000, "output": 5000}),
    ]
    aggregate = aggregate_attempts(
        attempts,
        name="pilot",
        eval_id="e1",
        token_budgets=(1000.0,),
        wall_time_budgets_ms=(120000.0,),
    )
    agent = aggregate.agent("agent")
    assert agent is not None
    budgets = {(item.dimension, item.budget): item for item in agent.budget_conditionals}
    tokens = budgets[("tokens", 1000.0)]
    assert tokens.score == pytest.approx(0.5)
    assert tokens.within_budget == 1
    assert tokens.over_budget == 1
    assert budgets[("wall_time_ms", 120000.0)].score == pytest.approx(1.0)


def test_explicit_requested_attempt_count_drives_coverage() -> None:
    attempts = [_attempt("t1", "src-a", "agent", 0, score=1.0)]
    aggregate = aggregate_attempts(
        attempts, name="pilot", eval_id="e1", requested_attempts={"agent": 4}
    )
    agent = aggregate.agent("agent")
    assert agent is not None
    assert agent.coverage == pytest.approx(0.25)


def test_task_source_override_beats_the_attempt_field() -> None:
    attempts = [
        _attempt("t1", "recorded", "agent", 0, score=1.0),
        _attempt("t2", "recorded", "agent", 0, score=0.0),
    ]
    aggregate = aggregate_attempts(
        attempts, name="pilot", eval_id="e1", task_sources={"t1": "src-x", "t2": "src-y"}
    )
    agent = aggregate.agent("agent")
    assert agent is not None
    assert {source.source_id for source in agent.sources} == {"src-x", "src-y"}


def test_attempts_without_a_source_fall_back_to_the_task() -> None:
    record = _attempt("t1", "src-a", "agent", 0, score=1.0)
    stripped = AttemptRecord.from_mapping(
        {**json.loads(json.dumps(_payload(record))), "source_id": None}
    )
    aggregate = aggregate_attempts([stripped], name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    assert agent.sources[0].source_id == "task:t1"


def _payload(record: AttemptRecord) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "task_id": record.task_id,
        "agent_id": record.agent_id,
        "seed": record.seed,
        "status": record.status,
        "score": record.score,
        "passed": record.passed,
        "pass_threshold": record.pass_threshold,
        "tokens": dict(record.tokens),
        "wall_time_ms": record.wall_time_ms,
        "cost_usd": record.cost_usd,
        "agent_config_sha256": record.agent_config_sha256,
        "base_bundle_sha256": record.base_bundle_sha256,
        "scorer_package_sha256": record.scorer_package_sha256,
        "input_sha256": record.input_sha256,
        "components": [component.to_dict() for component in record.components],
    }


def test_load_attempts_and_write_aggregate_round_trip(tmp_path: Path) -> None:
    task_id = "a" * 20
    for seed in (0, 1):
        directory = tmp_path / "attempts" / task_id / "agent" / f"seed-{seed}"
        directory.mkdir(parents=True)
        payload = _payload(_attempt(task_id, "src-a", "agent", seed, score=1.0))
        payload["schema_version"] = "retro-benchmark-attempt-v1"
        payload["source_id"] = "src-a"
        (directory / "attempt.json").write_text(json.dumps(payload))

    records = load_attempts(tmp_path)
    assert len(records) == 2
    assert all(record.valid for record in records)

    aggregate = aggregate_attempts(records, name="pilot", eval_id="e1")
    path = write_aggregate(tmp_path / "results.json", aggregate)
    stored = json.loads(path.read_text())
    assert stored["schema_version"] == "retro-benchmark-aggregate-v1"
    assert stored["agents"][0]["benchmark_score"] == pytest.approx(1.0)
    assert stored["agents"][0]["tasks"][0]["std_kind"] == "population"

    rows = list(iter_status_rows(aggregate))
    assert rows[0]["task_id"] == task_id
    assert rows[0]["scored_attempts"] == 2


def test_load_attempts_ignores_nested_workspace_records(tmp_path: Path) -> None:
    nested = tmp_path / "attempts" / ("a" * 20) / "agent" / "seed-0" / "workspace"
    nested.mkdir(parents=True)
    (nested / "attempt.json").write_text("{}")
    assert load_attempts(tmp_path) == []


def test_load_attempts_strictly_validates_canonical_records(tmp_path: Path) -> None:
    task_id = "a" * 20
    directory = tmp_path / "attempts" / task_id / "agent" / "seed-0"
    directory.mkdir(parents=True)
    payload = _payload(_attempt(task_id, "src-a", "agent", 0))
    payload.update(schema_version="retro-benchmark-attempt-v1", status="invented")
    (directory / "attempt.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="attempt.status"):
        load_attempts(tmp_path)


@pytest.mark.parametrize(
    ("left_extra", "right_extra", "message"),
    [
        (
            {"agent_config_sha256": "1" * 64},
            {"agent_config_sha256": "2" * 64},
            "agent configuration provenance",
        ),
        (
            {"base_bundle_sha256": "1" * 64},
            {"base_bundle_sha256": "2" * 64},
            "base bundle provenance",
        ),
        (
            {"scorer_package_sha256": "1" * 64},
            {"scorer_package_sha256": "2" * 64},
            "scorer package provenance",
        ),
        (
            {"input_sha256": "1" * 64},
            {"input_sha256": "2" * 64},
            "input provenance",
        ),
    ],
)
def test_aggregation_rejects_incompatible_evaluation_fingerprints(
    left_extra: dict[str, Any], right_extra: dict[str, Any], message: str
) -> None:
    attempts = [
        _attempt("t1", "src", "agent", 0, **left_extra),
        _attempt("t1", "src", "agent", 1, **right_extra),
    ]
    with pytest.raises(ValueError, match=message):
        aggregate_attempts(attempts)


def test_aggregation_rejects_inconsistent_persisted_thresholds() -> None:
    attempts = [
        _attempt("t1", "src", "agent-a", 0, score=0.7, pass_threshold=0.6),
        _attempt("t1", "src", "agent-b", 0, score=0.7, pass_threshold=0.8),
    ]
    with pytest.raises(ValueError, match="pass threshold provenance"):
        aggregate_attempts(attempts)


def test_external_threshold_cannot_override_persisted_provenance() -> None:
    attempts = [_attempt("t1", "src", "agent", 0, score=0.7, pass_threshold=0.8)]
    with pytest.raises(ValueError, match="does not match immutable attempt provenance"):
        aggregate_attempts(attempts, task_thresholds={"t1": 0.6})


def test_aggregation_rejects_missing_persisted_threshold() -> None:
    payload = _payload(_attempt("t1", "src", "agent", 0))
    payload.pop("pass_threshold")
    record = AttemptRecord.from_mapping(payload)
    with pytest.raises(ValueError, match="pass threshold provenance"):
        aggregate_attempts([record])


def test_loading_legacy_attempt_without_threshold_requires_rerun(tmp_path: Path) -> None:
    task_id = "a" * 20
    directory = tmp_path / "attempts" / task_id / "agent" / "seed-0"
    directory.mkdir(parents=True)
    payload = _payload(_attempt(task_id, "src", "agent", 0))
    payload["schema_version"] = "retro-benchmark-attempt-v1"
    payload.pop("pass_threshold")
    (directory / "attempt.json").write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="legacy attempts.*must be rerun"):
        load_attempts(tmp_path)


def test_scored_status_without_score_is_not_valid() -> None:
    record = AttemptRecord.from_mapping(
        {
            "attempt_id": "a",
            "task_id": "t1",
            "agent_id": "agent",
            "seed": 0,
            "status": "scored",
            "pass_threshold": 0.8,
            "agent_config_sha256": "a" * 64,
            "base_bundle_sha256": "b" * 64,
            "scorer_package_sha256": "c" * 64,
            "input_sha256": "d" * 64,
        }
    )
    assert record.valid is False
    aggregate = aggregate_attempts([record], name="pilot", eval_id="e1")
    agent = aggregate.agent("agent")
    assert agent is not None
    assert agent.benchmark_score is None
    assert agent.scored_attempts == 0


def test_aggregation_rejects_missing_evaluation_provenance() -> None:
    record = AttemptRecord.from_mapping(
        {
            "attempt_id": "a",
            "task_id": "t1",
            "agent_id": "agent",
            "seed": 0,
            "status": "agent_error",
        }
    )
    with pytest.raises(ValueError, match="missing or invalid"):
        aggregate_attempts([record])
