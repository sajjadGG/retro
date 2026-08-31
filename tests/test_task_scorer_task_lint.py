"""Deterministic static lint for TaskDefiner candidates."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle as bundle_mod
from retro.benchmarks.task_scorer import selection, task_lint
from retro.benchmarks.task_scorer.schema import (
    ProjectEnvironment,
    SchemaError,
    TaskDefinitions,
    TaskLimits,
    compute_task_id,
)
from retro.storage import Layout
from tests.task_scorer_helpers import (
    TASK_PROMPT as PROMPT,
)
from tests.task_scorer_helpers import (
    definitions_payload,
    install_session,
    make_repo,
    project_environment,
    repo_state_json,
    rollout_events,
    task_payload,
)


def _build_bundle(tmp_path: Path, *, adjacent_per_replay: int = 0):
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
    built = bundle_mod.build_source_bundle(
        candidate,
        layout=layout,
        name="pilot",
        task_limits=TaskLimits(max_replay_tasks=3, adjacent_per_replay=adjacent_per_replay),
    )
    return built, shas


@pytest.fixture
def facts(tmp_path: Path) -> task_lint.BundleFacts:
    built, _shas = _build_bundle(tmp_path)
    return task_lint.BundleFacts.from_bundle(built.path)


def _task(**overrides) -> dict:
    return task_payload(**overrides)


def _definitions(tasks: list[dict], source_id: str = "codex__session-1") -> TaskDefinitions:
    return TaskDefinitions.from_dict(definitions_payload(tasks, source_id=source_id))


def test_accepts_a_well_formed_replay_task(facts: task_lint.BundleFacts):
    report = task_lint.lint_task_definitions(_definitions([_task()]), facts)
    assert report.rejections == []
    assert len(report.accepted) == 1
    assert report.accepted[0].task_id == compute_task_id(
        facts.source_id, facts.base_tree, "replay", PROMPT
    )


def test_unknown_taskdefiner_rejection_code_does_not_discard_valid_task(
    facts: task_lint.BundleFacts,
):
    payload = definitions_payload([_task()])
    payload["rejections"] = [
        {
            "goal_event_ids": ["session-1:2"],
            "code": "external_issue_not_repository_scorable",
            "detail": "The goal exists only as external issue state.",
        }
    ]

    report = task_lint.lint_definitions_document(payload, facts)

    assert len(report.accepted) == 1
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"


def test_task_ids_are_stable_across_rebuilt_bundles(tmp_path: Path):
    first, _ = _build_bundle(tmp_path / "one")
    second, _ = _build_bundle(tmp_path / "two")
    facts_one = task_lint.BundleFacts.from_bundle(first.path)
    facts_two = task_lint.BundleFacts.from_bundle(second.path)
    report_one = task_lint.lint_task_definitions(_definitions([_task()]), facts_one)
    report_two = task_lint.lint_task_definitions(_definitions([_task()]), facts_two)
    assert report_one.accepted_ids == report_two.accepted_ids

    whitespace_variant = _task(prompt="  " + PROMPT.replace(" ", "  ") + "\n")
    report_three = task_lint.lint_task_definitions(_definitions([whitespace_variant]), facts_one)
    assert report_three.accepted_ids == report_one.accepted_ids


@pytest.mark.parametrize(
    ("overrides", "code", "fragment"),
    [
        ({"prompt": "   "}, "BUILDER_CONTRACT_ERROR", "empty"),
        ({"prompt": "x" * 4001}, "BUILDER_CONTRACT_ERROR", "4000"),
        (
            {"prompt": "User: do this\nAssistant: ok\nUser: also that"},
            "BUILDER_CONTRACT_ERROR",
            "more than one user message",
        ),
        (
            {"prompt_provenance": {"user_event_ids": [], "mode": "resolved_user_messages"}},
            "NO_STABLE_GOAL",
            "provenance",
        ),
        ({"base_failure_claim": " "}, "NO_OBSERVABLE_OUTCOME", "base_failure_claim"),
        ({"outcome_success_claim": ""}, "NO_OBSERVABLE_OUTCOME", "outcome_success_claim"),
        (
            {"confidence": {"goal": 0.99, "state": 0.5, "scorability": 0.99}},
            "NO_EXACT_BASE_SHA",
            "state confidence",
        ),
    ],
)
def test_rejects_malformed_candidates(facts, overrides, code, fragment):
    report = task_lint.lint_task_definitions(_definitions([_task(**overrides)]), facts)
    assert report.accepted == []
    assert len(report.rejections) == 1
    assert report.rejections[0].code == code
    assert fragment in report.rejections[0].detail


def test_rejects_empty_observables_and_evidenceless_observables(facts):
    brief = {
        "observables": [],
        "regressions_to_protect": [],
        "performance": [],
        "residual_judgment": [],
        "forbidden_shortcuts": [],
    }
    report = task_lint.lint_task_definitions(_definitions([_task(scorer_brief=brief)]), facts)
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"

    brief = copy.deepcopy(_task()["scorer_brief"])
    brief["observables"][0]["evidence"] = []
    report = task_lint.lint_task_definitions(_definitions([_task(scorer_brief=brief)]), facts)
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"
    assert "evidence source" in report.rejections[0].detail


def test_rejects_nonexistent_event_and_path_references(facts):
    provenance = {"user_event_ids": ["session-1:9999"], "mode": "resolved_user_messages"}
    report = task_lint.lint_task_definitions(
        _definitions([_task(prompt_provenance=provenance)]), facts
    )
    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"
    assert "unknown evidence reference" in report.rejections[0].detail

    report = task_lint.lint_task_definitions(
        _definitions(
            [_task(repo_evidence=[{"state": "base", "path": "src/ghost.py", "reason": "nope"}])]
        ),
        facts,
    )
    assert "unknown repository path" in report.rejections[0].detail

    brief = copy.deepcopy(_task()["scorer_brief"])
    brief["observables"][0]["evidence"] = ["repo/base:tests/test_divide.py"]
    report = task_lint.lint_task_definitions(_definitions([_task(scorer_brief=brief)]), facts)
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"


def test_rejects_descriptive_evidence_that_only_embeds_real_references(facts):
    brief = copy.deepcopy(_task()["scorer_brief"])
    brief["observables"][0]["evidence"] = [
        "User event session-1:2 requests the behavior.",
        "Outcome path tests/test_divide.py demonstrates it.",
    ]

    report = task_lint.lint_task_definitions(
        _definitions([_task(scorer_brief=brief)]),
        facts,
    )
    assert report.accepted == []
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"
    assert "unknown evidence reference" in report.rejections[0].detail


def test_accepts_hidden_repository_paths_and_exact_observable_sources(
    facts: task_lint.BundleFacts,
):
    hidden_facts = replace(
        facts,
        base_paths=facts.base_paths | frozenset({".github/workflows/ci.yml"}),
    )
    brief = copy.deepcopy(_task()["scorer_brief"])
    brief["observables"][0]["evidence"] = [
        "repo/base:tests/test_calc.py",
        "repo/outcome:tests/test_divide.py",
    ]

    report = task_lint.lint_task_definitions(
        _definitions(
            [
                _task(
                    scorer_brief=brief,
                    repo_evidence=[
                        {
                            "state": "base",
                            "path": ".github/workflows/ci.yml",
                            "reason": "CI entry point",
                        }
                    ],
                )
            ]
        ),
        hidden_facts,
    )

    assert len(report.accepted) == 1


def test_every_observable_evidence_reference_must_resolve_exactly(facts):
    brief = copy.deepcopy(_task()["scorer_brief"])
    brief["observables"][0]["evidence"] = [
        "session-1:2",
        "Notes about repo/outcome:tests/test_divide.py",
    ]

    report = task_lint.lint_task_definitions(_definitions([_task(scorer_brief=brief)]), facts)

    assert report.accepted == []
    assert report.rejections[0].code == "NO_OBSERVABLE_OUTCOME"
    assert "unknown evidence reference" in report.rejections[0].detail


def test_replay_provenance_must_reference_user_messages(facts):
    provenance = {"user_event_ids": ["session-1:1"], "mode": "resolved_user_messages"}

    report = task_lint.lint_task_definitions(
        _definitions([_task(prompt_provenance=provenance)]),
        facts,
    )

    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"
    assert "non-user message" in report.rejections[0].detail


def test_rejects_prompts_leaking_oracle_added_lines(facts):
    leaked = (
        "Update divide so that it runs: if b == 0: raise ValueError('division by zero') "
        "before returning a / b."
    )
    report = task_lint.lint_task_definitions(_definitions([_task(prompt=leaked)]), facts)
    assert report.accepted == []
    assert report.rejections[0].code == "PROMPT_ORACLE_LEAKAGE"

    hits = task_lint.prompt_leakage(leaked, facts)
    assert hits
    assert task_lint.prompt_leakage(PROMPT, facts) == []


def test_leakage_check_is_whitespace_and_case_insensitive(facts):
    leaked = "IF   B == 0:\n RAISE VALUEERROR('DIVISION BY ZERO')\n return A / B please implement"
    assert task_lint.prompt_leakage(leaked, facts)


def test_leakage_check_catches_short_identifiers_and_unicode(facts):
    oracle_fragments = frozenset(
        fragment
        for line in ("sentinel_key = 7", "修正_識別子 = True")
        for fragment in task_lint._oracle_fragments(line)
    )
    focused_facts = replace(
        facts,
        oracle_ngrams=frozenset(),
        oracle_lines=oracle_fragments,
    )

    assert task_lint.prompt_leakage("Set SENTINEL_KEY when ready.", focused_facts)
    assert task_lint.prompt_leakage("Use 修正_識別子 for the result.", focused_facts)


def test_enforces_replay_task_ceiling(facts):
    tasks = [
        _task(candidate_id=f"goal-{index}", prompt=f"{PROMPT} Variant {index}.")
        for index in range(4)
    ]
    report = task_lint.lint_task_definitions(_definitions(tasks), facts)
    assert len(report.accepted) == 3
    assert len(report.rejections) == 1
    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"
    assert "more than 3 replay tasks" in report.rejections[0].detail


def test_duplicate_prompts_collapse_to_one_task(facts):
    tasks = [_task(candidate_id="a"), _task(candidate_id="b")]
    report = task_lint.lint_task_definitions(_definitions(tasks), facts)
    assert len(report.accepted) == 1
    assert report.rejections[0].detail.startswith("duplicate task after canonicalization")


def _adjacent(**overrides) -> dict:
    payload = _task(
        candidate_id="goal-1-adjacent",
        kind="adjacent",
        prompt="Apply the same zero-denominator guard to the modulo helper in the same module.",
        adjacency={
            "operator": "sibling_transfer",
            "parent_candidate_id": "goal-1-replay",
            "transformed_object": "src/calc.py",
            "base_failure_reason": "the modulo helper has no guard at base",
        },
    )
    payload.update(overrides)
    return payload


def test_adjacent_tasks_are_opt_in(facts):
    report = task_lint.lint_task_definitions(_definitions([_task(), _adjacent()]), facts)
    assert [task.candidate.kind for task in report.accepted] == ["replay"]
    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"
    assert "adjacent generation is disabled" in report.rejections[0].detail


def test_adjacent_allowlist_parent_and_limit(tmp_path: Path):
    built, _shas = _build_bundle(tmp_path, adjacent_per_replay=1)
    facts = task_lint.BundleFacts.from_bundle(built.path)

    report = task_lint.lint_task_definitions(_definitions([_task(), _adjacent()]), facts)
    assert [task.candidate.kind for task in report.accepted] == ["replay", "adjacent"]

    with pytest.raises(SchemaError, match="adjacency.operator"):
        _definitions(
            [
                _task(),
                _adjacent(
                    adjacency={
                        "operator": "make_it_nicer",
                        "parent_candidate_id": "goal-1-replay",
                        "transformed_object": "src/calc.py",
                        "base_failure_reason": "n/a",
                    }
                ),
            ]
        )

    orphan = _adjacent(
        adjacency={
            "operator": "sibling_transfer",
            "parent_candidate_id": "not-a-task",
            "transformed_object": "src/calc.py",
            "base_failure_reason": "n/a",
        }
    )
    report = task_lint.lint_task_definitions(_definitions([_task(), orphan]), facts)
    assert "unknown parent replay task" in report.rejections[0].detail

    second = _adjacent(
        candidate_id="goal-1-adjacent-2",
        prompt="Also guard the remainder helper in the same module against zero divisors.",
    )
    report = task_lint.lint_task_definitions(
        _definitions([_task(), _adjacent(), second]), facts
    )
    assert len([task for task in report.accepted if task.candidate.kind == "adjacent"]) == 1
    assert any("more than 1 adjacent tasks" in item.detail for item in report.rejections)


def test_replay_task_must_not_declare_adjacency(facts):
    payload = _task(
        adjacency={
            "operator": "sibling_transfer",
            "parent_candidate_id": "goal-1-replay",
            "transformed_object": "src/calc.py",
            "base_failure_reason": "n/a",
        }
    )
    report = task_lint.lint_task_definitions(_definitions([payload]), facts)
    assert "must not declare adjacency" in report.rejections[0].detail


def test_source_id_mismatch_fails_closed(facts):
    report = task_lint.lint_task_definitions(
        _definitions([_task()], source_id="codex__other"), facts
    )
    assert report.accepted == []
    assert report.rejections[0].code == "BUILDER_CONTRACT_ERROR"


def test_report_serialization_reports_counts(facts):
    report = task_lint.lint_task_definitions(
        _definitions([_task(), _task(candidate_id="dupe")]), facts
    )
    payload = report.to_dict()
    assert payload["counts"]["accepted"] == 1
    assert payload["counts"]["rejected"] == 1
    assert payload["counts"]["by_code"] == {"BUILDER_CONTRACT_ERROR": 1}
    assert payload["accepted"][0]["kind"] == "replay"


def test_builder_rejections_are_carried_through(facts):
    definitions = TaskDefinitions.from_dict(
        {
            "schema_version": "retro-task-definitions-v1",
            "source_id": "codex__session-1",
            "tasks": [],
            "rejections": [
                {
                    "goal_event_ids": ["session-1:2"],
                    "code": "NO_OBSERVABLE_OUTCOME",
                    "detail": "no scorable outcome",
                }
            ],
        }
    )
    report = task_lint.lint_task_definitions(definitions, facts)
    assert report.accepted == []
    assert report.rejection_counts() == {"NO_OBSERVABLE_OUTCOME": 1}
