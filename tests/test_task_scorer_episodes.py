"""Fresh repository resets, sparse terminal rewards and actor/verifier separation."""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer.episodes import (
    EpisodeError,
    export_episodes,
    materialize_episode,
    score_episode,
    source_balanced_weights,
    verify_export,
)
from retro.benchmarks.task_scorer.ghostlab_cli import GhostlabTimeoutError
from retro.benchmarks.task_scorer.git_state import snapshot_tree_sha
from retro.benchmarks.task_scorer.suite import CurationPolicy, SuiteError, curate_taskset
from retro.utils import iter_jsonl
from tests.task_scorer_harness import CHANGING_FEATURE, PRESERVING_FEATURE
from tests.test_task_scorer_suite import PublishedFixture, make_published_fixture


@pytest.fixture
def published(tmp_path: Path) -> PublishedFixture:
    return make_published_fixture(tmp_path)


def _train_suite(published: PublishedFixture):
    return curate_taskset(
        published.layout, "pilot",
        policy=CurationPolicy(split_ratios={"train": 1, "validation": 0, "test": 0}),
    )


def test_sampling_is_source_balanced_and_user_priority_is_not_a_score():
    tasks = [
        {"task_id": "1" * 20, "source_id": "a", "priority": 2},
        {"task_id": "2" * 20, "source_id": "a", "priority": 1},
        {"task_id": "3" * 20, "source_id": "b", "priority": 999},
    ]
    weights = source_balanced_weights(tasks)
    assert weights["1" * 20] == pytest.approx(1 / 3)
    assert weights["2" * 20] == pytest.approx(1 / 6)
    assert weights["3" * 20] == pytest.approx(1 / 2)
    assert sum(weights.values()) == pytest.approx(1)
    with pytest.raises(EpisodeError, match="empty"):
        source_balanced_weights([])


def test_reset_is_fresh_exact_and_never_runs_host_setup(published: PublishedFixture, tmp_path: Path):
    suite = _train_suite(published)
    first = materialize_episode(suite, published.task_id, tmp_path / "episode-1")
    workspace = Path(first["workspace"])
    assert snapshot_tree_sha(workspace) == published.shas["base_tree"]
    assert not (workspace / ".git").exists()
    assert not (workspace / ".venv").exists()
    assert not (workspace / "src" / "feature.py").exists()
    assert "outcome_sha" not in first["repository"]
    assert "source_id" not in first
    (workspace / "src" / "feature.py").write_text("modified workspace")
    second = materialize_episode(suite, published.task_id, tmp_path / "episode-2", seed=1)
    assert not (Path(second["workspace"]) / "src" / "feature.py").exists()
    assert snapshot_tree_sha(Path(second["workspace"])) == published.shas["base_tree"]
    with pytest.raises(EpisodeError, match="already exists"):
        materialize_episode(suite, published.task_id, tmp_path / "episode-1")


def test_split_boundary_is_enforced_before_reset_or_grading(published: PublishedFixture, tmp_path: Path):
    suite = curate_taskset(published.layout, "pilot")
    with pytest.raises(SuiteError, match="requested suite split"):
        materialize_episode(suite, published.task_id, tmp_path / "train-episode")
    assert not (tmp_path / "train-episode").exists()
    with pytest.raises(EpisodeError, match="no train"):
        export_episodes(suite, tmp_path / "train-export")


def test_correct_and_incorrect_implementations_have_real_terminal_rewards(
    published: PublishedFixture, tmp_path: Path
):
    suite = _train_suite(published)
    for name, implementation, expected in (
        ("correct", PRESERVING_FEATURE, 1),
        ("incorrect", CHANGING_FEATURE, 0),
    ):
        observation = materialize_episode(suite, published.task_id, tmp_path / name)
        candidate = Path(observation["workspace"])
        (candidate / "src" / "feature.py").write_text(implementation)
        reward = score_episode(
            suite, published.task_id, candidate, tmp_path / f"{name}-reward", ghostlab=published.client()
        )
        assert reward["status"] == "scored"
        assert reward["reward"] == pytest.approx(expected)
        assert reward["terminated"] is True
        assert reward["truncated"] is False
        assert reward["candidate_sha256"]
        saved = json.loads((tmp_path / f"{name}-reward" / "episode-reward.json").read_text())
        assert saved == reward


def test_scorer_timeout_is_not_a_zero_reward(published: PublishedFixture, tmp_path: Path, monkeypatch):
    suite = _train_suite(published)
    observation = materialize_episode(suite, published.task_id, tmp_path / "episode")
    client = published.client()

    def timeout(_request):
        raise GhostlabTimeoutError("isolated scorer timed out")

    monkeypatch.setattr(client, "scorer_run", timeout)
    reward = score_episode(
        suite, published.task_id, Path(observation["workspace"]), tmp_path / "reward", ghostlab=client
    )
    assert reward["status"] == "scorer_timeout"
    assert reward["reward"] is None
    assert reward["passed"] is None
    assert reward["terminated"] is False
    assert reward["truncated"] is True
    assert "timed out" in reward["error"]


def test_missing_ghostlab_is_persisted_as_infrastructure_failure(
    published: PublishedFixture, tmp_path: Path
):
    suite = _train_suite(published)
    observation = materialize_episode(suite, published.task_id, tmp_path / "episode")
    reward = score_episode(
        suite, published.task_id, Path(observation["workspace"]), tmp_path / "reward",
        ghostlab_bin=tmp_path / "missing-ghostlab",
    )
    assert reward["status"] == "harness_error"
    assert reward["reward"] is None
    assert reward["error"]
    assert (tmp_path / "reward" / "episode-reward.json").is_file()


def test_sandbox_failure_preserves_the_runtime_diagnostic(published: PublishedFixture, tmp_path: Path):
    suite = _train_suite(published)
    observation = materialize_episode(suite, published.task_id, tmp_path / "episode")
    plan = json.loads(published.plan_path.read_text())
    plan["scorer"] = {
        "force_status": "scorer_error",
        "run_report_overrides": {
            "error": {"status": "scorer_error", "detail": "sandbox_gateway_unavailable: connection refused"},
        },
    }
    published.plan_path.write_text(json.dumps(plan))
    reward = score_episode(
        suite, published.task_id, Path(observation["workspace"]), tmp_path / "reward",
        ghostlab=published.client(),
    )
    assert reward["status"] == "scorer_error"
    assert reward["reward"] is None
    assert "sandbox_gateway_unavailable" in reward["error"]


@pytest.mark.parametrize("include_rewards", [False, True])
def test_export_has_portable_actor_inputs_and_separate_optional_verifiers(
    published: PublishedFixture, tmp_path: Path, include_rewards: bool
):
    suite = _train_suite(published)
    out = tmp_path / "export"
    manifest = export_episodes(suite, out, include_rewards=include_rewards)
    assert manifest["task_count"] == 1
    assert manifest["images_bundled"] is False
    assert manifest["required_images"] == ["demo@sha256:" + "b" * 64]
    assert manifest["include_rewards"] is include_rewards
    assert verify_export(out) == manifest
    assert export_episodes(suite, out, include_rewards=include_rewards) == manifest
    episodes = [row for _line, row in iter_jsonl(out / "episodes.jsonl", strict=True)]
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["sampling_weight"] == 1
    assert episode["curation"]["provenance_grade"] == "exact_replay"
    assert "outcome_sha" not in episode["repository"]
    public = out / "tasks" / published.task_id / "public"
    assert {file.name for file in public.iterdir()} == {
        "task.json", "prompt.txt", "environment.json", "base.bundle",
    }
    with tarfile.open(public / "base.bundle", "r:*") as bundle:
        assert not any(".git" in Path(name).parts for name in bundle.getnames())
        assert "src/feature.py" not in bundle.getnames()
    assert (out / "private").exists() is include_rewards
    if include_rewards:
        assert (out / "private" / "rewards" / published.task_id / "scorer.json").is_file()
        assert not list(out.rglob("oracle.bundle"))
        assert not list(out.rglob("provenance.json"))
        assert not list(out.rglob("reference.patch"))
    with pytest.raises(EpisodeError, match="immutable"):
        export_episodes(suite, out, include_rewards=not include_rewards)


def test_tampered_export_is_not_reused(published: PublishedFixture, tmp_path: Path):
    suite = _train_suite(published)
    out = tmp_path / "export"
    export_episodes(suite, out)
    (out / "episodes.jsonl").write_text("corrupted")
    with pytest.raises(EpisodeError, match="integrity"):
        export_episodes(suite, out)
