"""Outcome labels and the deterministic stratified source sampler."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import sampling, selection
from retro.benchmarks.task_scorer.schema import ProjectEnvironment
from retro.storage import Layout
from tests.task_scorer_helpers import (
    event,
    git,
    install_session,
    make_repo,
    project_environment,
    repo_state_json,
    rollout_events,
)


@pytest.fixture
def archive(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    return layout


def _unit(
    source_id: str,
    *,
    repo: str = "repo-a",
    work_kind: str = "build",
    files: int = 1,
    lines: int = 10,
    label: str = "unlabeled",
) -> sampling.SamplingUnit:
    return sampling.sampling_unit(
        source_id=source_id,
        repo_key=repo,
        repo_label=repo,
        work_kind=work_kind,
        files_changed=files,
        lines_changed=lines,
        labels={"label": label},
    )


def test_size_buckets_split_small_medium_large():
    assert sampling.size_bucket(1, 10) == "small"
    assert sampling.size_bucket(2, 50) == "small"
    assert sampling.size_bucket(3, 50) == "medium"
    assert sampling.size_bucket(2, 120) == "medium"
    assert sampling.size_bucket(11, 120) == "large"
    assert sampling.size_bucket(2, 500) == "large"


def test_stratified_sampling_is_deterministic_for_a_seed():
    units = [
        _unit(f"codex__s{index}", repo=f"repo-{index % 3}", work_kind="build" if index % 2 else "debug")
        for index in range(12)
    ]
    first = sampling.plan_sample(units, seed=7)
    second = sampling.plan_sample(list(reversed(units)), seed=7)
    assert first.kept == second.kept
    assert sampling.plan_sample(units, seed=8).kept != first.kept
    assert sorted(first.kept) == sorted(unit.source_id for unit in units)
    assert first.dropped == []


def test_stratified_sampling_spreads_across_repositories_before_repeating():
    units = [_unit(f"codex__a{index}", repo="repo-a") for index in range(4)]
    units += [_unit("codex__b1", repo="repo-b"), _unit("codex__c1", repo="repo-c")]
    plan = sampling.plan_sample(units, seed=3)
    assert set(plan.kept[:3]) == {"codex__b1", "codex__c1"} | {
        source_id for source_id in plan.kept[:3] if source_id.startswith("codex__a")
    }
    assert len([item for item in plan.kept[:3] if item.startswith("codex__a")]) == 1


def test_per_repo_cap_drops_overflow_with_a_reason():
    units = [_unit(f"codex__a{index}", repo="repo-a") for index in range(5)]
    units += [_unit("codex__b1", repo="repo-b")]
    plan = sampling.plan_sample(units, seed=1, max_per_repo=2)
    assert len([item for item in plan.kept if item.startswith("codex__a")]) == 2
    assert "codex__b1" in plan.kept
    assert {decision.reason for decision in plan.dropped} == {"repo_cap"}
    assert len(plan.dropped) == 3


def test_limit_derives_a_default_repo_cap():
    assert sampling.resolve_repo_cap(max_per_repo=None, limit=None) is None
    assert sampling.resolve_repo_cap(max_per_repo=None, limit=8) == 2
    assert sampling.resolve_repo_cap(max_per_repo=None, limit=2) == 1
    assert sampling.resolve_repo_cap(max_per_repo=5, limit=8) == 5

    units = [_unit(f"codex__a{index}", repo=f"repo-{index}") for index in range(6)]
    plan = sampling.plan_sample(units, seed=2, limit=3)
    assert len(plan.kept) == 3
    assert [decision.reason for decision in plan.dropped] == ["limit"] * 3


def test_informative_outcomes_are_sampled_before_unlabeled_ones():
    units = [
        _unit("codex__plain", label="unlabeled"),
        _unit("codex__ok", label="successful_surviving"),
        _unit("codex__fixed", label="failed_then_fixed"),
        _unit("codex__pushback", label="corrected"),
    ]
    plan = sampling.plan_sample(units, seed=5)
    assert set(plan.kept[:2]) == {"codex__fixed", "codex__pushback"}
    assert plan.kept[2] == "codex__ok"
    assert plan.kept[3] == "codex__plain"


def test_lexicographic_strategy_preserves_legacy_order():
    units = [_unit("codex__b"), _unit("codex__a"), _unit("codex__c")]
    plan = sampling.plan_sample(units, strategy="lexicographic", seed=11)
    assert plan.kept == ["codex__a", "codex__b", "codex__c"]
    with pytest.raises(ValueError, match="unknown sampling strategy"):
        sampling.plan_sample(units, strategy="random")


def test_outcome_labels_record_survival_and_pushback(tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    labels = sampling.compute_outcome_labels(
        repo_root=root,
        outcome_sha=shas["outcome_sha"],
        branch="main",
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
    )
    assert labels.label == "successful_surviving"
    assert labels.survived is True
    assert labels.in_branch_history is True
    assert labels.pushback_turns == 0
    payload = json.loads(json.dumps(labels.to_dict()))
    assert payload["schema_version"] == sampling.OUTCOME_LABELS_SCHEMA
    assert payload["evidence"]["outcome_sha"] == shas["outcome_sha"]


def test_outcome_labels_mark_pushback_as_corrected(tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    events = rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7])
    events.append(
        event(90, actor="user", payload={"text": "That broke the import, fix it."})
    )
    labels = sampling.compute_outcome_labels(
        repo_root=root,
        outcome_sha=shas["outcome_sha"],
        branch="main",
        events=events,
        signal_values={"friction.interruptions": 2, "outcome.status": "success"},
        signal_evidence={"friction.interruptions": ["session-1:4"]},
    )
    assert labels.label == "corrected"
    assert labels.pushback_turns == 2
    assert labels.evidence["signals"]["friction.interruptions"] == ["session-1:4"]
    assert labels.priority == 0


def test_outcome_labels_mark_failed_sessions_that_still_landed(tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    labels = sampling.compute_outcome_labels(
        repo_root=root,
        outcome_sha=shas["outcome_sha"],
        branch="main",
        signal_values={"outcome.status": "failed", "outcome.committed": True},
    )
    assert labels.label == "failed_then_fixed"
    assert labels.informative is True


def test_outcome_labels_detect_a_revert(tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    git(root, "revert", "--no-edit", shas["outcome_sha"])
    labels = sampling.compute_outcome_labels(
        repo_root=root, outcome_sha=shas["outcome_sha"], branch="main"
    )
    assert labels.label == "unlabeled"
    assert labels.survived is False
    assert labels.evidence["revert"]["sha"]


def test_selection_records_outcome_labels_and_sampling(archive: Layout, tmp_path: Path):
    root = tmp_path / "project"
    shas = make_repo(root)
    install_session(
        archive,
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
        cwd=str(root),
        raw_files={"repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])},
    )
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))
    result = selection.select_sources(
        layout=archive,
        sessions=[("codex", "session-1")],
        branch="main",
        environment_resolver=lambda _candidate: environment,
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["counts"]["by_kind"] == {"session": 1}
    assert payload["counts"]["by_label"] == {"successful_surviving": 1}
    assert payload["selected"][0]["source_kind"] == "session"
    labels = payload["selected"][0]["outcome_labels"]
    assert labels["label"] == "successful_surviving"
    assert labels["evidence"]["branch"] == "main"
    assert payload["sampling"]["strategy"] == "stratified"
    assert payload["sampling"]["kept"] == ["codex__session-1"]
    assert payload["sampling"]["strata"]
