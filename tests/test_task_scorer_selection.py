"""Source eligibility, rejection codes, and Layout helpers for the taskset pipeline."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import selection
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


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "project"
    return root, make_repo(root)


def _environment_resolver(base_sha: str):
    environment = ProjectEnvironment.from_dict(project_environment(base_sha))

    def resolver(candidate: selection.SourceCandidate) -> ProjectEnvironment:
        return environment

    return resolver


def _install(layout: Layout, root: Path, shas: dict[str, str], **kwargs) -> None:
    install_session(
        layout,
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"]),
            **kwargs.pop("raw_files", {}),
        },
        **kwargs,
    )


def test_user_acceptance_requires_an_unqualified_final_user_message():
    assert selection._user_accepted(
        [event(1, actor="user", payload={"text": "Looks good, thanks."})]
    )
    assert not selection._user_accepted(
        [event(1, actor="user", payload={"text": "Please accept JSON input."})]
    )
    assert not selection._user_accepted(
        [event(1, actor="user", payload={"text": "Looks good, but fix the timeout."})]
    )


def test_selects_eligible_source(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=_environment_resolver(shas["base_sha"]),
    )
    assert rejection is None
    assert candidate is not None
    assert candidate.source_id == "codex__session-1"
    assert candidate.base_sha == shas["base_sha"]
    assert candidate.outcome_sha == shas["outcome_sha"]
    assert candidate.base.resolution == "captured_start"
    anchor = candidate.repo_anchor()
    assert anchor.state_confidence == "exact_clean_commit"
    assert anchor.environment_id == "sha256:" + "a" * 64
    assert anchor.repo_id.startswith("sha256:")


def test_rejects_missing_normalized_rollout(archive: Layout):
    candidate, rejection = selection.select_source(
        layout=archive, host="codex", session_id="ghost", require_environment=False
    )
    assert candidate is None
    assert rejection is not None
    assert rejection.code == "NO_NORMALIZED_ROLLOUT"


def test_rejects_missing_cwd_and_non_git_cwd(archive: Layout, tmp_path: Path, repo):
    root, shas = repo
    install_session(
        archive,
        session_id="no-cwd",
        events=[event(1, actor="user", payload={"text": "hello"}, session_id="no-cwd")],
    )
    _, rejection = selection.select_source(
        layout=archive, host="codex", session_id="no-cwd", require_environment=False
    )
    assert rejection is not None and rejection.code == "NO_REPO_CWD"

    plain = tmp_path / "plain"
    plain.mkdir()
    install_session(
        archive,
        session_id="plain",
        events=[event(1, actor="user", payload={"text": "hello"}, session_id="plain")],
        cwd=str(plain),
    )
    _, rejection = selection.select_source(
        layout=archive, host="codex", session_id="plain", require_environment=False
    )
    assert rejection is not None and rejection.code == "NOT_GIT_REPOSITORY"


def test_rejects_dirty_start_capture(archive: Layout, repo):
    root, shas = repo
    install_session(
        archive,
        events=rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7]),
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(
                root,
                shas["base_sha"],
                shas["base_tree"],
                porcelain="1 .M N... 100644 100644 100644 aaa bbb src/calc.py\x00",
            )
        },
    )
    _, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )
    assert rejection is not None and rejection.code == "DIRTY_START_STATE"


def test_rejects_start_capture_taken_after_the_rollout_started(archive: Layout, repo):
    root, shas = repo
    _install(
        archive,
        root,
        shas,
        raw_files={
            "repo_start.json": repo_state_json(
                root,
                shas["base_sha"],
                shas["base_tree"],
                captured_at="2026-08-01T18:02:00Z",
            )
        },
    )

    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "NO_EXACT_BASE_SHA"
    assert "after the rollout started" in rejection.detail


def test_legitimate_start_and_end_sidecars_bracket_the_rollout(archive: Layout, repo):
    root, shas = repo
    events = rollout_events(base_sha=shas["base_sha"], include_commit=False)
    install_session(
        archive,
        events=events,
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"]),
            "repo_end.json": repo_state_json(
                root,
                shas["outcome_sha"],
                shas["outcome_tree"],
                captured_at="2026-08-01T19:00:00Z",
            ),
        },
    )

    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )

    assert rejection is None
    assert candidate is not None
    assert candidate.outcome.resolution == "captured_end"


def test_rejects_end_capture_taken_before_rollout_completion(archive: Layout, repo):
    root, shas = repo
    events = rollout_events(base_sha=shas["base_sha"], include_commit=False)
    install_session(
        archive,
        events=events,
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"]),
            "repo_end.json": repo_state_json(
                root,
                shas["outcome_sha"],
                shas["outcome_tree"],
                captured_at="2026-08-01T18:02:00Z",
            ),
        },
    )

    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )

    assert candidate is None
    assert rejection is not None
    assert rejection.code == "NO_OUTCOME_SHA"
    assert "before the rollout ended" in rejection.detail


def test_rejects_source_without_outcome(archive: Layout, repo):
    root, shas = repo
    install_session(
        archive,
        events=[
            event(1, actor="system", event_type="session_start", payload={"cwd": str(root)}),
            event(2, actor="user", payload={"text": "please look into this"}),
        ],
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])
        },
    )
    _, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )
    assert rejection is not None and rejection.code == "NO_OUTCOME_SHA"


def test_rejects_reverted_outcome(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    git(root, "revert", "--no-edit", shas["outcome_sha"])
    _, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )
    assert rejection is not None and rejection.code == "OUTCOME_NOT_DURABLE"


def test_rejects_when_environment_missing_or_mismatched(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    _, rejection = selection.select_source(
        layout=archive, host="codex", session_id="session-1", branch="main"
    )
    assert rejection is not None and rejection.code == "ENVIRONMENT_UNAVAILABLE"

    _, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=_environment_resolver(shas["outcome_sha"]),
    )
    assert rejection is not None and rejection.code == "ENVIRONMENT_UNAVAILABLE"
    assert rejection.evidence["environment_base_sha"] == shas["outcome_sha"]


def test_environment_resolver_failure_is_a_rejection(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)

    def failing(candidate):
        raise RuntimeError("docker build failed")

    _, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=failing,
    )
    assert rejection is not None and rejection.code == "ENVIRONMENT_UNAVAILABLE"
    assert "docker build failed" in rejection.detail


def test_select_sources_reports_counts_and_writes_atomically(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    result = selection.select_sources(
        layout=archive,
        sessions=[("codex", "session-1"), ("codex", "missing")],
        branch="main",
        environment_resolver=_environment_resolver(shas["base_sha"]),
    )
    assert [candidate.source_id for candidate in result.selected] == ["codex__session-1"]
    assert result.rejection_counts() == {"NO_NORMALIZED_ROLLOUT": 1}

    path = selection.write_selection(archive, "pilot", result)
    assert path == archive.benchmark_taskset_dir("pilot") / "selection.json"
    payload = selection.load_selection(path)
    assert payload["counts"] == {
        "selected": 1,
        "rejected": 1,
        "by_code": {"NO_NORMALIZED_ROLLOUT": 1},
    }
    assert not list(path.parent.glob(".selection.json.*"))


def test_selection_does_not_mutate_raw_or_normalized(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    raw_dir = archive.raw_dir("codex", "session-1")
    normalized = archive.normalized_path("codex", "session-1")
    before = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in list(raw_dir.rglob("*")) + [normalized]
        if path.is_file()
    }
    selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        environment_resolver=_environment_resolver(shas["base_sha"]),
    )
    after = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in list(raw_dir.rglob("*")) + [normalized]
        if path.is_file()
    }
    assert before == after


def test_layout_taskset_helpers_are_namespaced(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    taskset = layout.benchmark_taskset_dir("pilot")
    assert taskset == layout.benchmark_dir("pilot") / "task-scorer"
    assert layout.benchmark_taskset_sources_dir("pilot") == taskset / "sources"
    assert layout.benchmark_taskset_source_dir("pilot", "codex__1") == taskset / "sources" / "codex__1"
    assert layout.benchmark_taskset_tasks_dir("pilot") == taskset / "tasks"
    assert layout.benchmark_taskset_task_dir("pilot", "abc") == taskset / "tasks" / "abc"
    assert layout.benchmark_taskset_build_run_dir("pilot", "b1") == taskset / "builds" / "b1"
    assert layout.benchmark_taskset_eval_dir("pilot", "e1") == taskset / "evals" / "e1"
    assert (
        layout.benchmark_taskset_attempt_dir("pilot", "e1", "t1", "codex", 2)
        == taskset / "evals" / "e1" / "attempts" / "t1" / "codex" / "seed-2"
    )
    assert layout.benchmark_taskset_results_path("pilot", "e1") == taskset / "evals" / "e1" / "results.json"
    # the time-consistent benchmark contract is untouched
    assert layout.benchmark_dir("pilot") == layout.benchmarks_dir() / "pilot"


def test_selection_report_json_is_serializable(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    result = selection.select_sources(
        layout=archive,
        sessions=[("codex", "session-1")],
        branch="main",
        environment_resolver=_environment_resolver(shas["base_sha"]),
    )
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["selected"][0]["base"]["resolution"] == "captured_start"
    assert payload["selected"][0]["environment_id"] == "sha256:" + "a" * 64


def test_base_is_never_inferred_from_session_timestamps(archive: Layout, repo):
    """Commits that merely fall inside the session window are not evidence of a base."""
    root, shas = repo
    git(
        root,
        "-c",
        "user.email=retro@example.invalid",
        "-c",
        "user.name=Retro Tests",
        "commit",
        "--allow-empty",
        "--date=2026-08-01T18:03:00Z",
        "-m",
        "unrelated commit inside the session window",
    )
    install_session(
        archive,
        events=[
            event(
                1,
                actor="system",
                event_type="session_start",
                payload={"cwd": str(root)},
                timestamp="2026-08-01T18:00:00Z",
            ),
            event(
                2,
                actor="user",
                payload={"text": "Guard divide against zero denominators."},
                timestamp="2026-08-01T18:01:00Z",
            ),
            event(
                3,
                actor="system",
                event_type="session_end",
                summary="session end",
                timestamp="2026-08-01T18:30:00Z",
            ),
        ],
        cwd=str(root),
    )
    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )
    assert candidate is None
    assert rejection is not None and rejection.code == "NO_EXACT_BASE_SHA"


def test_outcome_is_never_inferred_from_session_timestamps(archive: Layout, repo):
    """A captured start proves the base, but an unattributed later commit is not an outcome."""
    root, shas = repo
    git(root, "reset", "--hard", shas["base_sha"])
    install_session(
        archive,
        events=[
            event(
                1,
                actor="system",
                event_type="session_start",
                payload={"cwd": str(root)},
                timestamp="2026-08-01T18:00:00Z",
            ),
            event(2, actor="user", payload={"text": "please look into this"}),
        ],
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])
        },
    )
    git(
        root,
        "-c",
        "user.email=retro@example.invalid",
        "-c",
        "user.name=Retro Tests",
        "commit",
        "--allow-empty",
        "--date=2026-08-01T18:10:00Z",
        "-m",
        "human commit made during the session window",
    )
    candidate, rejection = selection.select_source(
        layout=archive,
        host="codex",
        session_id="session-1",
        branch="main",
        require_environment=False,
    )
    assert candidate is None
    assert rejection is not None and rejection.code == "NO_OUTCOME_SHA"
