"""Conformance with the JSON Schema assets packaged under ``task_scorer/schemas``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle as bundle_mod
from retro.benchmarks.task_scorer import schema as ts
from retro.benchmarks.task_scorer import selection, task_lint
from retro.benchmarks.task_scorer.schema import ProjectEnvironment
from retro.storage import Layout
from tests.task_scorer_helpers import (
    definitions_payload,
    install_session,
    make_repo,
    project_environment,
    repo_state_json,
    rollout_events,
    task_payload,
)

SCHEMA_DIR = Path(ts.__file__).resolve().parent / "schemas"


@pytest.fixture
def facts(tmp_path: Path) -> task_lint.BundleFacts:
    root = tmp_path / "project"
    shas = make_repo(root)
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    install_session(
        layout,
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
        cwd=str(root),
        raw_files={"repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])},
    )
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))
    candidate, rejection = selection.select_source(
        layout=layout,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=lambda _candidate: environment,
    )
    assert rejection is None and candidate is not None
    built = bundle_mod.build_source_bundle(candidate, layout=layout, name="pilot")
    return task_lint.BundleFacts.from_bundle(built.path)


def test_every_packaged_schema_loads_and_is_supported():
    for name in ts.PACKAGED_SCHEMAS:
        document = ts.load_packaged_schema(name)
        assert document["$schema"].endswith("2020-12/schema")
        ts.json_schema_errors({}, document)
    assert {path.name for path in SCHEMA_DIR.glob("*.json")} == set(ts.PACKAGED_SCHEMAS.values())
    with pytest.raises(ts.SchemaError, match="unknown packaged schema"):
        ts.load_packaged_schema("nope")


def test_unsupported_keywords_fail_loudly():
    with pytest.raises(ts.SchemaError, match="unsupported JSON Schema keywords"):
        ts.json_schema_errors({}, {"patternProperties": {}})
    with pytest.raises(ts.SchemaError, match="unresolvable JSON Schema"):
        ts.json_schema_errors({}, {"$ref": "#/$defs/missing"}, root={"$defs": {}})


def test_validator_enforces_the_keywords_the_assets_rely_on():
    schema = ts.load_packaged_schema("task-definitions")

    assert ts.json_schema_errors(definitions_payload([]), schema) == []

    wrong_version = definitions_payload([])
    wrong_version["schema_version"] = "retro-task-definitions-v2"
    assert ts.json_schema_errors(wrong_version, schema)

    extra_key = definitions_payload([])
    extra_key["extra"] = 1
    assert "$.extra is not an allowed property" in ts.json_schema_errors(extra_key, schema)

    missing_key = definitions_payload([])
    del missing_key["tasks"]
    assert "$.tasks is required" in ts.json_schema_errors(missing_key, schema)

    too_many = definitions_payload([task_payload(candidate_id=f"c{index}") for index in range(7)])
    assert any("at most 6 items" in error for error in ts.json_schema_errors(too_many, schema))

    duplicate_events = definitions_payload(
        [
            task_payload(
                prompt_provenance={
                    "user_event_ids": ["session-1:2", "session-1:2"],
                    "mode": "resolved_user_messages",
                }
            )
        ]
    )
    assert any("unique items" in error for error in ts.json_schema_errors(duplicate_events, schema))

    bad_confidence = definitions_payload(
        [task_payload(confidence={"goal": 1.4, "state": 1.0, "scorability": 0.9})]
    )
    assert any("<= 1" in error for error in ts.json_schema_errors(bad_confidence, schema))

    # booleans are not integers, and integers are acceptable numbers
    assert ts.json_schema_errors(True, {"type": "integer"})
    assert ts.json_schema_errors(1, {"type": "number"}) == []


def test_task_definitions_round_trip_conforms_to_the_packaged_schema():
    payload = definitions_payload(
        [
            task_payload(),
            task_payload(
                candidate_id="goal-1-adjacent",
                kind="adjacent",
                prompt="Apply the same guard to the modulo helper.",
                adjacency={
                    "parent_candidate_id": "goal-1-replay",
                    "operator": "sibling_transfer",
                    "transformed_object": "src/calc.py",
                    "base_failure_reason": "the modulo helper has no guard at base",
                },
            ),
        ],
        rejections=[
            {
                "goal_event_ids": ["session-1:8"],
                "code": "NO_OBSERVABLE_OUTCOME",
                "detail": "no scorable outcome",
            }
        ],
    )
    ts.validate_packaged(payload, "task-definitions")

    definitions = ts.TaskDefinitions.from_dict(payload)
    emitted = definitions.to_dict()
    ts.validate_packaged(emitted, "task-definitions")
    assert ts.TaskDefinitions.from_dict(emitted) == definitions


def test_observable_importance_matches_the_packaged_enum():
    assert ts.OBSERVABLE_IMPORTANCES == ("gate", "soft")
    brief = task_payload()["scorer_brief"]
    brief["observables"][0]["importance"] = "soft"
    payload = definitions_payload([task_payload(scorer_brief=brief)])
    ts.validate_packaged(payload, "task-definitions")
    assert ts.TaskDefinitions.from_dict(payload).tasks[0].scorer_brief.observables[0].importance == (
        "soft"
    )

    brief["observables"][0]["importance"] = "primary"
    payload = definitions_payload([task_payload(scorer_brief=brief)])
    assert ts.packaged_schema_errors(payload, "task-definitions")
    with pytest.raises(ts.SchemaError, match="importance"):
        ts.TaskDefinitions.from_dict(payload)


def _report(**overrides) -> ts.ScoreReport:
    payload = {
        "task_id": "2d493d" + "0" * 14,
        "attempt_id": "attempt-1",
        "status": "scored",
        "scorer_package_sha256": "d" * 64,
        "valid": True,
        "score_total": 0.87,
        "passed": True,
        "pass_threshold": 0.8,
        "unscored_weight": 0.0,
        "components": [
            ts.ScoreComponentResult(
                id="requested_behavior",
                weight=1.0,
                hard_gate=True,
                value=1.0,
                gate_passed=True,
                evidence=[
                    ts.ScoreEvidence(kind="command", ref="pytest -q", summary="4 passed")
                ],
            )
        ],
        "commands": [
            ts.CommandRecord(argv=["pytest", "-q"], exit_code=0, duration_ms=1832),
        ],
    }
    payload.update(overrides)
    return ts.ScoreReport(**payload)


def test_score_reports_emit_packaged_conformant_documents():
    ts.validate_packaged(_report().to_dict(), "score-report")

    non_scored = _report(
        status="scorer_timeout",
        valid=False,
        score_total=None,
        passed=None,
        pass_threshold=None,
        unscored_weight=None,
        components=[],
        commands=[],
        warnings=["scorer exceeded its wall clock"],
    ).to_dict()
    ts.validate_packaged(non_scored, "score-report")
    assert "score_total" not in non_scored and "passed" not in non_scored

    nullable = {**non_scored, "score_total": None, "passed": None}
    ts.validate_packaged(nullable, "score-report")
    assert ts.ScoreReport.from_dict(nullable) == ts.ScoreReport.from_dict(non_scored)


def test_packaged_schema_rejects_scores_on_failed_reports():
    payload = _report().to_dict()
    payload["status"] = "scorer_error"
    payload["valid"] = False
    assert ts.packaged_schema_errors(payload, "score-report")
    with pytest.raises(ts.SchemaError, match="must not carry a score_total"):
        ts.ScoreReport.from_dict(payload)

    payload = _report().to_dict()
    del payload["scorer_package_sha256"]
    assert "$.scorer_package_sha256 is required" in ts.packaged_schema_errors(
        payload, "score-report"
    )


def test_score_report_loader_tolerates_free_form_evidence_and_commands():
    payload = _report().to_dict()
    payload["components"][0]["evidence"] = [{"kind": "log", "ref": "x", "note": "extra"}]
    payload["commands"] = [
        {"argv": ["pytest"], "exit_code": 0, "duration_ms": 3, "stdout_sha256": "a" * 64}
    ]
    ts.validate_packaged(payload, "score-report")
    report = ts.ScoreReport.from_dict(payload)
    assert report.components[0].evidence[0].extra == {"note": "extra"}
    assert report.commands[0].extra == {"stdout_sha256": "a" * 64}
    assert report.to_dict() == payload


def test_packaged_score_report_matches_ghostlab_repeatability_contract():
    payload = _report(
        repeatability=ts.ScoreRepeatability(
            runs=3,
            deterministic_stable=False,
            unstable_components=["requested_behavior"],
            max_total_spread=0.25,
            totals=[0.5, 0.75, 0.5],
        )
    ).to_dict()

    ts.validate_packaged(payload, "score-report")
    assert ts.ScoreReport.from_dict(payload).repeatability is not None

    payload["repeatability"]["unexpected"] = True
    assert "$.repeatability.unexpected is not an allowed property" in ts.packaged_schema_errors(
        payload, "score-report"
    )


@pytest.mark.parametrize("field", ["score_total", "pass_threshold", "unscored_weight"])
def test_packaged_score_report_rejects_non_finite_numbers(field):
    payload = _report().to_dict()
    payload[field] = float("nan")
    assert ts.packaged_schema_errors(payload, "score-report")


def test_lint_document_accepts_a_conformant_definition(facts: task_lint.BundleFacts):
    payload = definitions_payload([task_payload()])
    report = task_lint.lint_definitions_document(payload, facts)
    assert report.rejections == []
    assert report.accepted_ids == [
        ts.compute_task_id(facts.source_id, facts.base_tree, "replay", task_payload()["prompt"])
    ]


def test_lint_document_isolates_non_conformant_tasks(facts: task_lint.BundleFacts):
    broken = task_payload(candidate_id="broken")
    broken["unexpected"] = True
    empty_observables = task_payload(candidate_id="empty")
    empty_observables["scorer_brief"]["observables"] = []
    payload = definitions_payload([task_payload(), broken, empty_observables])

    report = task_lint.lint_definitions_document(payload, facts)
    assert len(report.accepted) == 1
    assert report.rejection_counts() == {
        "BUILDER_CONTRACT_ERROR": 1,
        "NO_OBSERVABLE_OUTCOME": 1,
    }
    detail = next(item for item in report.rejections if item.candidate_id == "broken").detail
    assert "unexpected is not an allowed property" in detail


def test_lint_document_reports_envelope_violations_once(facts: task_lint.BundleFacts):
    payload = definitions_payload([task_payload()])
    payload["schema_version"] = "retro-task-definitions-v0"
    report = task_lint.lint_definitions_document(payload, facts)
    assert report.accepted == []
    assert [item.code for item in report.rejections] == ["BUILDER_CONTRACT_ERROR"]
    assert "schema_version" in report.rejections[0].detail

    too_many = definitions_payload(
        [task_payload(candidate_id=f"c{index}", prompt=f"Do thing {index}.") for index in range(7)]
    )
    report = task_lint.lint_definitions_document(too_many, facts)
    assert report.accepted == []
    assert "at most 6" in report.rejections[0].detail

    report = task_lint.lint_definitions_document(["not-an-object"], facts)
    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"


def test_task_definitions_loader_enforces_the_packaged_task_ceiling():
    payload = definitions_payload(
        [task_payload(candidate_id=f"c{index}", prompt=f"Do thing {index}.") for index in range(7)]
    )
    with pytest.raises(ts.SchemaError, match="at most 6"):
        ts.TaskDefinitions.from_dict(payload)


def test_instruction_assets_are_readable_and_unmodified():
    instructions = Path(ts.__file__).resolve().parent / "instructions"
    names = {path.name for path in instructions.glob("*.md")}
    assert names == {
        "task-definer.md",
        "scorer-builder.md",
        "scorer-auditor.md",
        "residual-judge.md",
    }
    text = (instructions / "task-definer.md").read_text(encoding="utf-8")
    assert "task-definitions.json" in text


def test_scorer_audit_documents_validate():
    audit = {
        "decision": "accept",
        "leakage": [],
        "overfit_checks": ["mutation: revert guard"],
        "missing_observables": [],
        "mutants": [{"id": "m1", "expected": "fail"}],
        "evidence": ["repo/base:src/calc.py"],
    }
    ts.validate_packaged(audit, "scorer-audit")
    audit["decision"] = "maybe"
    assert ts.packaged_schema_errors(audit, "scorer-audit")


def test_packaged_schema_files_are_not_modified_by_the_loaders():
    before = {path.name: path.read_bytes() for path in SCHEMA_DIR.glob("*.json")}
    ts.load_packaged_schema.cache_clear()
    for name in ts.PACKAGED_SCHEMAS:
        document = ts.load_packaged_schema(name)
        document["mutated"] = True
    ts.load_packaged_schema.cache_clear()
    assert {path.name: path.read_bytes() for path in SCHEMA_DIR.glob("*.json")} == before
    assert "mutated" not in json.loads(
        (SCHEMA_DIR / "task-definitions.schema.json").read_text(encoding="utf-8")
    )


class _LintRequest:
    """Mirror of the request shape ``build.py`` hands to the lint entry point."""

    def __init__(self, bundle_dir: Path, definitions: dict, **limits: int) -> None:
        self.source_id = "codex__session-1"
        self.source_dir = bundle_dir
        self.manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        self.task_definitions = definitions
        self.max_replay_tasks = limits.get("max_replay_tasks", 3)
        self.adjacent_per_replay = limits.get("adjacent_per_replay", 0)


def test_lint_entrypoint_accepts_a_request_object(facts: task_lint.BundleFacts, tmp_path: Path):
    bundle_dir = Layout(tmp_path / "archive").benchmark_taskset_source_dir(
        "pilot", "codex__session-1"
    )
    request = _LintRequest(bundle_dir, definitions_payload([task_payload()]))
    outcome = task_lint.lint_task_definitions(request)

    assert [task["task_id"] for task in outcome.accepted] == [
        ts.compute_task_id(facts.source_id, facts.base_tree, "replay", task_payload()["prompt"])
    ]
    assert all(isinstance(task, dict) for task in outcome.accepted)
    assert outcome.accepted[0]["candidate_id"] == "goal-1-replay"
    assert outcome.findings == outcome.rejections == ()


def test_lint_request_limits_override_bundle_limits(facts: task_lint.BundleFacts, tmp_path: Path):
    bundle_dir = Layout(tmp_path / "archive").benchmark_taskset_source_dir(
        "pilot", "codex__session-1"
    )
    tasks = [
        task_payload(candidate_id=f"c{index}", prompt=f"Guard helper number {index} against zero.")
        for index in range(3)
    ]
    request = _LintRequest(bundle_dir, definitions_payload(tasks), max_replay_tasks=1)
    outcome = task_lint.lint_task_definitions(request)
    assert len(outcome.accepted) == 1
    assert [item.code for item in outcome.rejections] == [
        "BUILDER_CONTRACT_ERROR",
        "BUILDER_CONTRACT_ERROR",
    ]


def test_lint_entrypoint_rejects_ambiguous_calls():
    with pytest.raises(TypeError):
        task_lint.lint_task_definitions(object())
    with pytest.raises(TypeError):
        task_lint.lint_task_definitions({"schema_version": "x"}, object())
