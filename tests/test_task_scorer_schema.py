"""Contract tests for the task/scorer versioned schemas."""
from __future__ import annotations

import json

import pytest

from retro.benchmarks.task_scorer import schema as ts


def _repo_anchor(**overrides):
    payload = {
        "root_at_capture": "/Users/dev/project",
        "repo_id": "sha256:" + "1" * 64,
        "base_sha": "a" * 40,
        "base_tree": "b" * 40,
        "outcome_sha": "c" * 40,
        "outcome_tree": "d" * 40,
        "base_resolution": "captured_start",
        "state_confidence": "exact_clean_commit",
    }
    payload.update(overrides)
    return ts.RepoAnchor(**payload)


def _manifest(**overrides) -> ts.SourceBundleManifest:
    payload = {
        "source_id": "codex__019abc",
        "host": "codex",
        "session_id": "019abc",
        "started_at": "2026-08-01T18:12:03Z",
        "ended_at": "2026-08-01T19:05:47Z",
        "rollout_events_sha256": "e" * 64,
        "repo": _repo_anchor(),
    }
    payload.update(overrides)
    return ts.SourceBundleManifest(**payload)


def test_manifest_round_trip_is_strict():
    manifest = _manifest().with_content_hash("f" * 64)
    restored = ts.SourceBundleManifest.from_dict(json.loads(json.dumps(manifest.to_dict())))
    assert restored == manifest


def test_manifest_rejects_unknown_keys_and_wrong_version():
    payload = _manifest().to_dict()
    payload["extra"] = 1
    with pytest.raises(ts.SchemaError, match="unknown keys"):
        ts.SourceBundleManifest.from_dict(payload)

    payload = _manifest().to_dict()
    payload["schema_version"] = "retro-source-bundle-v2"
    with pytest.raises(ts.SchemaError, match="schema_version"):
        ts.SourceBundleManifest.from_dict(payload)


def test_repo_anchor_rejects_unusable_resolution_and_identical_trees():
    with pytest.raises(ts.SchemaError, match="base_resolution"):
        _repo_anchor(base_resolution="unresolved")
    with pytest.raises(ts.SchemaError, match="must differ"):
        _repo_anchor(outcome_tree="b" * 40)
    with pytest.raises(ts.SchemaError, match="approximate"):
        _repo_anchor(base_resolution="first_commit_parent", state_confidence="exact_clean_commit")
    with pytest.raises(ts.SchemaError, match="40-character"):
        _repo_anchor(base_sha="abc")


def test_task_limits_are_bounded():
    assert ts.TaskLimits.from_dict({"max_replay_tasks": 3, "adjacent_per_replay": 1}) == ts.TaskLimits(
        max_replay_tasks=3, adjacent_per_replay=1
    )
    with pytest.raises(ts.SchemaError):
        ts.TaskLimits.from_dict({"max_replay_tasks": 4, "adjacent_per_replay": 0})
    with pytest.raises(ts.SchemaError):
        ts.TaskLimits.from_dict({"max_replay_tasks": 3, "adjacent_per_replay": 2})


def test_task_id_is_stable_and_prompt_normalized():
    first = ts.compute_task_id("codex__1", "b" * 40, "replay", "Add a  guard\nfor zero")
    second = ts.compute_task_id("codex__1", "b" * 40, "replay", " Add a guard for zero ")
    assert first == second
    assert len(first) == 20
    assert first != ts.compute_task_id("codex__1", "b" * 40, "adjacent", "Add a guard for zero")
    with pytest.raises(ts.SchemaError):
        ts.compute_task_id("codex__1", "b" * 40, "unknown", "x")


def _definitions_payload() -> dict:
    return {
        "schema_version": ts.TASK_DEFINITIONS_SCHEMA,
        "source_id": "codex__019abc",
        "tasks": [
            {
                "candidate_id": "goal-1-replay",
                "kind": "replay",
                "prompt": "Guard division against zero.",
                "prompt_provenance": {"user_event_ids": ["019abc:2"], "mode": "resolved_user_messages"},
                "goal_segment": {
                    "introduced_event_id": "019abc:2",
                    "closed_event_id": "019abc:9",
                    "summary": "zero guard",
                },
                "repo_evidence": [{"state": "base", "path": "src/calc.py", "reason": "target"}],
                "scorer_brief": {
                    "observables": [
                        {
                            "id": "requested-behavior",
                            "description": "divide raises on zero",
                            "importance": "gate",
                            "evidence": ["019abc:2"],
                        }
                    ],
                    "regressions_to_protect": ["existing test suite"],
                    "performance": [],
                    "residual_judgment": [],
                    "forbidden_shortcuts": ["reference patch equality"],
                },
                "base_failure_claim": "base divides by zero",
                "outcome_success_claim": "outcome raises ValueError",
                "adjacency": None,
                "confidence": {"goal": 0.9, "state": 1.0, "scorability": 0.9},
            }
        ],
        "rejections": [
            {"goal_event_ids": ["019abc:20"], "code": "NO_OBSERVABLE_OUTCOME", "detail": "n/a"}
        ],
    }


def test_task_definitions_round_trip_and_duplicate_detection():
    definitions = ts.TaskDefinitions.from_dict(_definitions_payload())
    assert definitions.tasks[0].scorer_brief.observables[0].importance == "gate"
    assert definitions.rejections[0].code == "NO_OBSERVABLE_OUTCOME"
    assert ts.TaskDefinitions.from_dict(definitions.to_dict()) == definitions

    payload = _definitions_payload()
    payload["tasks"].append(dict(payload["tasks"][0]))
    with pytest.raises(ts.SchemaError, match="duplicate candidate_id"):
        ts.TaskDefinitions.from_dict(payload)


def test_scorer_brief_preserves_structured_residual_criteria():
    payload = _definitions_payload()
    payload["tasks"][0]["scorer_brief"]["residual_judgment"] = [
        {"id": "content_quality", "anchors": {"met": 1.0, "unmet": 0.0}}
    ]

    definitions = ts.TaskDefinitions.from_dict(payload)

    assert definitions.to_dict()["tasks"][0]["scorer_brief"]["residual_judgment"] == [
        {"id": "content_quality", "anchors": {"met": 1.0, "unmet": 0.0}}
    ]


def test_task_definitions_reject_unknown_rejection_code_and_operator():
    payload = _definitions_payload()
    payload["rejections"][0]["code"] = "NOT_A_CODE"
    with pytest.raises(ts.SchemaError, match="rejection code"):
        ts.TaskDefinitions.from_dict(payload)

    payload = _definitions_payload()
    payload["tasks"][0]["kind"] = "adjacent"
    payload["tasks"][0]["adjacency"] = {
        "operator": "make_it_better",
        "parent_candidate_id": "goal-1-replay",
        "transformed_object": "src/calc.py",
        "base_failure_reason": "missing",
    }
    with pytest.raises(ts.SchemaError, match="adjacency.operator"):
        ts.TaskDefinitions.from_dict(payload)


def _scorer_payload(**overrides) -> dict:
    payload = {
        "schema_version": ts.SCORER_SCHEMA,
        "task_id": "2d493d" + "0" * 14,
        "mode": "deterministic",
        "entrypoint": ["python3", "/scorer/score.py"],
        "runtime": {"image": "sha256:" + "c" * 64},
        "components": [
            {"id": "requested_behavior", "kind": "deterministic", "weight": 0.8, "hard_gate": True},
            {"id": "regression_suite", "kind": "deterministic", "weight": 0.2, "hard_gate": True},
        ],
        "pass_threshold": 0.8,
    }
    payload.update(overrides)
    return payload


def test_scorer_manifest_requires_unit_weights_and_pinned_judge():
    manifest = ts.ScorerManifest.from_dict(_scorer_payload())
    assert manifest.mode == "deterministic"

    bad_weights = _scorer_payload()
    bad_weights["components"][0]["weight"] = 0.9
    with pytest.raises(ts.SchemaError, match="sum to 1.0"):
        ts.ScorerManifest.from_dict(bad_weights)

    hybrid = _scorer_payload(mode="hybrid")
    with pytest.raises(ts.SchemaError, match="requires a pinned judge"):
        ts.ScorerManifest.from_dict(hybrid)

    hybrid["judge"] = {
        "enabled": True,
        "agent_config": "/scorer/judge-agent.json",
        "prompt": "/scorer/judge.prompt.md",
        "output_schema": "/scorer/judge.schema.json",
        "criteria": ["project_fit"],
    }
    with pytest.raises(ts.SchemaError, match="judge criteria"):
        ts.ScorerManifest.from_dict(hybrid)

    hybrid["judge"]["criteria"] = ["regression_suite"]
    assert ts.ScorerManifest.from_dict(hybrid).judge is not None

    agentic = _scorer_payload(mode="agentic")
    with pytest.raises(ts.SchemaError, match="requires a pinned judge"):
        ts.ScorerManifest.from_dict(agentic)


def test_scorer_runtime_locks_network_and_mount():
    payload = _scorer_payload()
    payload["runtime"] = {"image": "sha256:" + "c" * 64, "network": "enabled"}
    with pytest.raises(ts.SchemaError, match="network"):
        ts.ScorerManifest.from_dict(payload)
    payload["runtime"] = {"image": "sha256:" + "c" * 64, "candidate_mount": "read_write"}
    with pytest.raises(ts.SchemaError, match="candidate_mount"):
        ts.ScorerManifest.from_dict(payload)


def test_score_input_round_trip():
    payload = {
        "schema_version": ts.SCORE_INPUT_SCHEMA,
        "task_id": "2d493d" + "0" * 14,
        "attempt_id": "attempt-1",
        "repo_path": "/candidate/repo",
        "task_path": "/input/task.json",
        "trace_path": "/input/aut-events.jsonl",
        "resource_usage_path": "/input/resources.json",
        "seed": 0,
    }
    score_input = ts.ScoreInput.from_dict(payload)
    assert score_input.to_dict() == payload


def _component(**overrides) -> ts.ScoreComponentResult:
    payload = {
        "id": "requested_behavior",
        "weight": 1.0,
        "hard_gate": True,
        "value": 1.0,
        "gate_passed": True,
    }
    payload.update(overrides)
    return ts.ScoreComponentResult(**payload)


def test_hard_gate_failure_forces_zero():
    components = [
        _component(weight=0.7, value=0.0, gate_passed=False),
        _component(id="regression_suite", weight=0.3, value=1.0, gate_passed=True),
    ]
    total = ts.compute_score_total(components, pass_threshold=0.8)
    assert total.score_total == 0.0
    assert total.passed is False
    assert total.hard_gate_failures == ["requested_behavior"]
    assert total.valid is True


def test_weighted_total_and_unscored_weight_rules():
    components = [
        _component(weight=0.7, value=1.0, gate_passed=True),
        ts.ScoreComponentResult(id="project_fit", weight=0.3, hard_gate=False, value=None),
    ]
    total = ts.compute_score_total(components, pass_threshold=0.8)
    assert total.unscored_weight == pytest.approx(0.3)
    assert total.valid is False
    assert total.score_total == pytest.approx(0.7)

    components[1] = ts.ScoreComponentResult(id="project_fit", weight=0.1, hard_gate=False, value=None)
    components[0] = _component(weight=0.9, value=1.0, gate_passed=True)
    total = ts.compute_score_total(components, pass_threshold=0.8)
    assert total.valid is True
    assert total.score_total == pytest.approx(0.9)
    assert total.passed is True


def test_compute_score_total_requires_unit_weights():
    with pytest.raises(ts.SchemaError, match="sum to 1.0"):
        ts.compute_score_total([_component(weight=0.5)], pass_threshold=0.8)


def _report(**overrides) -> ts.ScoreReport:
    payload = {
        "task_id": "2d493d" + "0" * 14,
        "attempt_id": "attempt-1",
        "status": "scored",
        "scorer_package_sha256": "d" * 64,
        "score_total": 0.87,
        "passed": True,
        "components": [_component()],
    }
    payload.update(overrides)
    return ts.ScoreReport(**payload)


def test_score_report_status_rules():
    scored = _report()
    assert scored.is_numeric
    assert ts.ScoreReport.from_dict(scored.to_dict()) == scored

    errored = _report(
        status="scorer_error",
        score_total=None,
        passed=None,
        components=[],
        warnings=["scorer crashed"],
    )
    assert errored.is_numeric is False
    assert "score_total" not in errored.to_dict()
    assert "passed" not in errored.to_dict()
    assert ts.ScoreReport.from_dict(errored.to_dict()) == errored

    with pytest.raises(ts.SchemaError, match="must not carry a score_total"):
        _report(status="scorer_error", passed=None, components=[])
    with pytest.raises(ts.SchemaError, match="must carry score_total"):
        _report(score_total=None)
    with pytest.raises(ts.SchemaError, match="must carry passed"):
        _report(passed=None)


def test_score_report_accepts_prefixed_package_hash_and_rejects_junk():
    report = _report(scorer_package_sha256="sha256:" + "d" * 64)
    assert report.scorer_package_sha256 == "d" * 64
    with pytest.raises(ts.SchemaError, match="scorer_package_sha256"):
        _report(scorer_package_sha256="not-a-hash")


def test_unscored_hard_gate_is_a_gate_failure():
    components = [
        ts.ScoreComponentResult(id="gate", weight=0.6, hard_gate=True, value=None),
        _component(id="soft", weight=0.4, hard_gate=False, value=1.0, gate_passed=None),
    ]
    total = ts.compute_score_total(components, pass_threshold=0.5)
    assert total.hard_gate_failures == ["gate"]
    assert total.score_total == 0.0
    with pytest.raises(ts.SchemaError, match="within \\[0, 1\\]"):
        ts.ScoreComponentResult(id="gate", weight=1.0, hard_gate=True, value=1.5)


def test_benchmark_task_hides_oracle_material():
    repository = {"repo_id": "sha256:" + "1" * 64, "base_sha": "a" * 40, "base_tree": "b" * 40, "subdir": "."}
    task = ts.BenchmarkTask(
        task_id="2d493d" + "0" * 14,
        kind="replay",
        prompt="Guard division against zero.",
        repository=repository,
        environment={"image": "sha256:" + "c" * 64, "network": "disabled"},
    )
    assert ts.BenchmarkTask.from_dict(task.to_dict()) == task

    with pytest.raises(ts.SchemaError, match="oracle field"):
        ts.BenchmarkTask(
            task_id="2d493d" + "0" * 14,
            kind="replay",
            prompt="x",
            repository={**repository, "outcome_sha": "c" * 40},
            environment={"image": "img", "network": "disabled"},
        )
    with pytest.raises(ts.SchemaError, match="network"):
        ts.BenchmarkTask(
            task_id="2d493d" + "0" * 14,
            kind="replay",
            prompt="x",
            repository=repository,
            environment={"image": "img", "network": "enabled"},
        )


def test_attempt_keeps_errors_out_of_numeric_results():
    attempt = ts.BenchmarkAttempt(
        attempt_id="attempt-1",
        task_id="2d493d" + "0" * 14,
        agent_id="codex",
        seed=0,
        status="scored",
        score=0.87,
        passed=True,
    )
    assert attempt.is_numeric
    assert ts.BenchmarkAttempt.from_dict(attempt.to_dict()) == attempt

    failed = ts.BenchmarkAttempt(
        attempt_id="attempt-2",
        task_id="2d493d" + "0" * 14,
        agent_id="codex",
        seed=1,
        status="scorer_error",
        error="scorer crashed",
    )
    assert failed.is_numeric is False
    with pytest.raises(ts.SchemaError, match="must not carry a score"):
        ts.BenchmarkAttempt(
            attempt_id="attempt-3",
            task_id="2d493d" + "0" * 14,
            agent_id="codex",
            seed=0,
            status="scorer_error",
            score=0.0,
        )


def test_project_environment_requires_validation_and_disabled_network():
    payload = {
        "schema_version": ts.PROJECT_ENVIRONMENT_SCHEMA,
        "environment_id": "sha256:" + "a" * 64,
        "source": "explicit",
        "base_sha": "a" * 40,
        "image": "repo@sha256:" + "b" * 64,
        "workdir": "/workspace/repo",
        "setup": [["python3", "-m", "venv", ".venv"]],
        "smoke": [[".venv/bin/pytest", "--collect-only", "-q"]],
        "test": [[".venv/bin/pytest", "-q"]],
        "env": {},
        "network_during_build": "allowlisted",
        "network_during_run": "disabled",
        "workspace_excludes": [".venv"],
        "validated": {"base": True, "outcome": True, "runs": 2},
    }
    environment = ts.ProjectEnvironment.from_dict(payload)
    assert environment.test_commands()["test"] == [[".venv/bin/pytest", "-q"]]

    payload["validated"] = {"base": True, "outcome": False}
    with pytest.raises(ts.SchemaError, match="validated"):
        ts.ProjectEnvironment.from_dict(payload)

    payload["validated"] = {"base": True, "outcome": True, "runs": 2}
    payload["network_during_run"] = "allowlisted"
    with pytest.raises(ts.SchemaError, match="network_during_run"):
        ts.ProjectEnvironment.from_dict(payload)

    payload["network_during_run"] = "disabled"
    payload["setup"] = ["python3 -m venv .venv"]
    with pytest.raises(ts.SchemaError, match="argument array"):
        ts.ProjectEnvironment.from_dict(payload)

    payload["setup"] = []
    payload["image"] = "repo:latest"
    with pytest.raises(ts.SchemaError, match="pinned"):
        ts.ProjectEnvironment.from_dict(payload)

    payload["image"] = "repo@sha256:" + "b" * 64
    payload["validated"]["runs"] = 1
    with pytest.raises(ts.SchemaError, match="runs"):
        ts.ProjectEnvironment.from_dict(payload)


def test_canonical_json_is_stable():
    assert ts.canonical_json({"b": 1, "a": [2, 3]}) == '{"a":[2,3],"b":1}'
    assert ts.content_sha256({"a": 1}) == ts.content_sha256({"a": 1})
