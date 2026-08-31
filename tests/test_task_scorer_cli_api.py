"""CLI-facing entry points: capture, taskset selection, and taskset bundling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle, git_state, selection
from retro.benchmarks.task_scorer.schema import ProjectEnvironment
from retro.storage import Layout
from tests.task_scorer_helpers import (
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


def _install(layout: Layout, root: Path, shas: dict[str, str], session_id: str = "session-1") -> None:
    install_session(
        layout,
        session_id=session_id,
        events=rollout_events(
            session_id=session_id,
            base_sha=shas["base_sha"],
            commit_short=shas["outcome_sha"][:7],
        ),
        cwd=str(root),
        raw_files={
            "repo_start.json": repo_state_json(root, shas["base_sha"], shas["base_tree"])
        },
    )


def _environment_file(tmp_path: Path, base_sha: str, *, wrapped: bool = False) -> Path:
    payload = project_environment(base_sha)
    document = (
        {
            "schema_version": selection.ENVIRONMENT_CONTRACT_SCHEMA,
            "environments": [payload],
        }
        if wrapped
        else payload
    )
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_capture_repository_state_writes_under_layout(archive: Layout, repo):
    root, shas = repo
    record = git_state.capture_repository_state(
        layout=archive, host="codex", session_id="session-1", cwd=root, phase="start"
    )
    assert record.path == archive.raw_dir("codex", "session-1") / "repo_start.json"
    assert record.to_dict()["host"] == "codex"
    assert record.to_dict()["session_id"] == "session-1"
    assert record.state.head_sha == shas["outcome_sha"]

    end = git_state.capture_repository_state(
        layout=archive, host="codex", session_id="session-1", cwd=root, phase="end"
    )
    assert end.path.name == "repo_end.json"

    with pytest.raises(git_state.CaptureExistsError):
        git_state.capture_repository_state(
            layout=archive, host="codex", session_id="session-1", cwd=root, phase="start"
        )
    with pytest.raises(git_state.GitError):
        git_state.capture_repository_state(
            layout=archive, host="codex", session_id="s2", cwd=root, phase="middle"
        )
    with pytest.raises(git_state.GitError, match="session id"):
        git_state.capture_repository_state(
            layout=archive,
            host="codex",
            session_id="../escape",
            cwd=root,
            phase="start",
        )


def test_session_selectors_and_discovery(archive: Layout, repo, tmp_path: Path):
    root, shas = repo
    _install(archive, root, shas, session_id="session-1")
    _install(archive, root, shas, session_id="session-2")

    assert selection.discover_sessions(archive) == [
        ("codex", "session-1"),
        ("codex", "session-2"),
    ]
    assert selection.discover_sessions(archive, host="claude") == []
    with pytest.raises(selection.SelectionError):
        selection.discover_sessions(archive, host="nope")

    assert selection.parse_session_selector("codex/session-1") == ("codex", "session-1")
    assert selection.parse_session_selector("codex:session-1") == ("codex", "session-1")
    assert selection.parse_session_selector("session-1", host="codex") == ("codex", "session-1")
    assert selection.parse_session_selector("claude/session-1") == (
        "claude-code",
        "session-1",
    )
    assert selection.parse_session_selector("vscode_copilot/session-1") == (
        "vscode-copilot",
        "session-1",
    )
    assert selection.normalize_host("claude-code") == "claude-code"
    assert selection.normalize_host("copilot") == "vscode-copilot"
    with pytest.raises(selection.SelectionError):
        selection.parse_session_selector("session-1")
    with pytest.raises(selection.SelectionError):
        selection.parse_session_selector("claude/session-1", host="codex")

    session_file = tmp_path / "sessions.txt"
    session_file.write_text(
        "# taskset sources\ncodex/session-2\n\ncodex/session-1\ncodex/session-1\n",
        encoding="utf-8",
    )
    assert selection.load_session_file(session_file) == [
        ("codex", "session-2"),
        ("codex", "session-1"),
    ]
    assert selection.resolve_sessions(layout=archive, session_file=session_file) == [
        ("codex", "session-1"),
        ("codex", "session-2"),
    ]

    bad = tmp_path / "bad.txt"
    bad.write_text("not-a-host/\n", encoding="utf-8")
    with pytest.raises(selection.SelectionError, match="bad.txt:1"):
        selection.load_session_file(bad)
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(selection.SelectionError):
        selection.load_session_file(empty)
    with pytest.raises(selection.SelectionError, match="unsupported characters"):
        selection.parse_session_selector("../escape", host="codex")
    with pytest.raises(selection.SelectionError, match="unsupported characters"):
        selection.selection_path(archive, "../escape")


def test_select_taskset_requires_an_explicit_environment_contract(archive: Layout, repo, tmp_path):
    root, shas = repo
    _install(archive, root, shas)

    with pytest.raises(selection.SelectionError, match="Ambient developer setup"):
        selection.select_taskset(layout=archive, name="pilot", host="codex")

    result = selection.select_taskset(
        layout=archive,
        name="pilot",
        host="codex",
        environment_file=_environment_file(tmp_path, shas["base_sha"]),
    )
    assert [candidate.source_id for candidate in result.selected] == ["codex__session-1"]
    assert result.path == selection.selection_path(archive, "pilot")
    payload = selection.load_selection(result.path)
    assert payload["environment"]["validated"] is True
    assert payload["environment"]["required"] is True
    assert payload["environment"]["contract"]["contract_path"] == str(
        _environment_file(tmp_path, shas["base_sha"])
    )
    assert payload["selected"][0]["environment_id"] == "sha256:" + "a" * 64
    assert result.to_dict()["sessions"] == ["codex/session-1"]


def test_unvalidated_selection_is_recorded_as_unvalidated(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    result = selection.select_taskset(
        layout=archive, name="pilot", host="codex", require_environment=False
    )
    payload = selection.load_selection(result.path)
    assert payload["environment"] == {
        "required": False,
        "contract": None,
        "resolver": None,
        "validated": False,
    }
    assert payload["selected"][0]["environment_id"] is None


def test_automatic_environment_is_embedded_for_later_bundling(archive: Layout, repo):
    root, shas = repo
    _install(archive, root, shas)
    environment = ProjectEnvironment.from_dict(project_environment(shas["base_sha"]))

    selected = selection.select_taskset(
        layout=archive,
        name="pilot",
        host="codex",
        environment_resolver=lambda _candidate: environment,
    )

    assert selected.path is not None
    payload = selection.load_selection(selected.path)
    assert payload["environment"]["resolver"] == "automatic"
    assert payload["selected"][0]["environment"]["environment_id"] == environment.environment_id

    bundled = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)
    assert len(bundled.bundled) == 1
    assert bundled.bundled[0].path is not None
    environment_path = bundled.bundled[0].path / "context" / "environment.json"
    assert json.loads(environment_path.read_text(encoding="utf-8"))["environment_id"] == (
        environment.environment_id
    )


def test_environment_contract_forms_and_validation(archive: Layout, repo, tmp_path: Path):
    root, shas = repo
    _install(archive, root, shas)

    wrapped = selection.load_environment_contract(
        _environment_file(tmp_path, shas["base_sha"], wrapped=True)
    )
    assert list(wrapped.environments) == [shas["base_sha"]]
    assert wrapped.for_base(shas["base_sha"]) is not None
    assert wrapped.for_base("0" * 40) is None
    assert wrapped.to_dict()["base_shas"] == [shas["base_sha"]]

    listed = tmp_path / "list.json"
    listed.write_text(json.dumps([project_environment(shas["base_sha"])]), encoding="utf-8")
    assert list(selection.load_environment_contract(listed).environments) == [shas["base_sha"]]

    duplicate = tmp_path / "dupe.json"
    duplicate.write_text(
        json.dumps([project_environment(shas["base_sha"])] * 2), encoding="utf-8"
    )
    with pytest.raises(selection.SelectionError, match="duplicate environment"):
        selection.load_environment_contract(duplicate)

    unvalidated = project_environment(shas["base_sha"])
    unvalidated["validated"] = {"base": True, "outcome": False}
    ambient = tmp_path / "ambient.json"
    ambient.write_text(json.dumps(unvalidated), encoding="utf-8")
    with pytest.raises(selection.SelectionError, match="validated"):
        selection.load_environment_contract(ambient)

    with pytest.raises(selection.SelectionError, match="does not exist"):
        selection.load_environment_contract(tmp_path / "missing.json")

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(selection.SelectionError, match="invalid JSON"):
        selection.load_environment_contract(broken)


def test_environment_contract_for_another_base_is_a_rejection(archive: Layout, repo, tmp_path):
    root, shas = repo
    _install(archive, root, shas)
    other = tmp_path / "other.json"
    other.write_text(json.dumps(project_environment("0" * 40)), encoding="utf-8")
    result = selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=other
    )
    assert result.selected == []
    assert [item.code for item in result.rejections] == ["ENVIRONMENT_UNAVAILABLE"]


def test_bundle_taskset_selected_only_round_trip(archive: Layout, repo, tmp_path: Path):
    root, shas = repo
    _install(archive, root, shas)
    environment_file = _environment_file(tmp_path, shas["base_sha"])
    selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=environment_file
    )

    report = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)
    assert [outcome.status for outcome in report.outcomes] == ["bundled"]
    outcome = report.outcomes[0]
    assert outcome.source_id == "codex__session-1"
    assert outcome.path is not None and outcome.path.is_dir()
    assert bundle.verify_bundle(outcome.path)
    assert report.path == bundle.bundle_report_path(archive, "pilot")
    payload = json.loads(report.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == bundle.BUNDLE_REPORT_SCHEMA
    assert payload["counts"] == {"bundled": 1, "skipped": 0, "by_status": {"bundled": 1}}

    record = json.loads((outcome.path / "selection.json").read_text(encoding="utf-8"))
    assert record["selected"] is True
    assert record["status"] == "selected"
    assert record["base_sha"] == shas["base_sha"]
    assert record["environment_validated"] is True

    again = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)
    assert [item.status for item in again.outcomes] == ["reused"]
    assert again.outcomes[0].content_sha256 == outcome.content_sha256


def test_bundle_taskset_without_selected_only_selects_first(archive: Layout, repo, tmp_path: Path):
    root, shas = repo
    _install(archive, root, shas)
    install_session(
        archive,
        session_id="session-empty",
        events=[],
        cwd=str(root),
    )
    report = bundle.bundle_taskset(
        layout=archive,
        name="pilot",
        selected_only=False,
        host="codex",
        environment_file=_environment_file(tmp_path, shas["base_sha"]),
    )
    statuses = {item.source_id: item.status for item in report.outcomes}
    assert statuses["codex__session-1"] == "bundled"
    assert statuses["codex__session-empty"] == "skipped"
    skipped = [item for item in report.outcomes if item.status == "skipped"][0]
    assert skipped.code in {"NO_REPO_CWD", "NO_EXACT_BASE_SHA", "NO_NORMALIZED_ROLLOUT"}
    assert selection.selection_path(archive, "pilot").is_file()


def test_bundle_taskset_requires_a_selection_and_detects_drift(archive: Layout, repo, tmp_path):
    root, shas = repo
    _install(archive, root, shas)
    with pytest.raises(bundle.BundleError, match="run 'retro benchmark taskset select"):
        bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)

    environment_file = _environment_file(tmp_path, shas["base_sha"])
    result = selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=environment_file
    )
    path = result.path
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["selected"][0]["outcome"]["sha"] = "0" * 40
    path.write_text(json.dumps(payload), encoding="utf-8")

    report = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)
    assert [item.status for item in report.outcomes] == ["skipped"]
    assert report.outcomes[0].code == "HARNESS_ERROR"
    assert "selection is stale" in report.outcomes[0].detail
    assert not archive.benchmark_taskset_source_dir("pilot", "codex__session-1").exists()


def test_bundle_taskset_skips_sources_that_no_longer_select(archive: Layout, repo, tmp_path):
    root, shas = repo
    _install(archive, root, shas)
    environment_file = _environment_file(tmp_path, shas["base_sha"])
    selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=environment_file
    )
    (archive.raw_dir("codex", "session-1") / "repo_start.json").unlink()
    archive.normalized_path("codex", "session-1").unlink()

    report = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)
    assert [item.status for item in report.outcomes] == ["skipped"]
    assert report.outcomes[0].code == "NO_NORMALIZED_ROLLOUT"


def test_bundle_taskset_reuses_embedded_environment_if_contract_moves(
    archive: Layout, repo, tmp_path
):
    root, shas = repo
    _install(archive, root, shas)
    environment_file = _environment_file(tmp_path, shas["base_sha"])
    selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=environment_file
    )
    environment_file.unlink()

    report = bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)

    assert len(report.bundled) == 1


def test_bundle_taskset_refuses_to_drop_a_required_environment(archive: Layout, repo, tmp_path):
    root, shas = repo
    _install(archive, root, shas)
    environment_file = _environment_file(tmp_path, shas["base_sha"])
    selected = selection.select_taskset(
        layout=archive, name="pilot", host="codex", environment_file=environment_file
    )
    assert selected.path is not None
    payload = selection.load_selection(selected.path)
    for entry in payload["selected"]:
        entry["environment"] = None
    selected.path.write_text(json.dumps(payload), encoding="utf-8")
    environment_file.unlink()

    with pytest.raises(bundle.BundleError, match="no longer readable"):
        bundle.bundle_taskset(layout=archive, name="pilot", selected_only=True)

    report = bundle.bundle_taskset(
        layout=archive, name="pilot", selected_only=True, require_environment=False
    )
    assert [item.status for item in report.outcomes] == ["bundled"]
    record = json.loads(
        (report.outcomes[0].path / "selection.json").read_text(encoding="utf-8")
    )
    assert record["environment_validated"] is False
