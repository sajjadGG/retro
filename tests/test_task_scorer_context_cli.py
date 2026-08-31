"""Bounded, read-only ``retro-context`` operations over a SourceBundle."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.benchmarks.task_scorer import bundle as bundle_mod
from retro.benchmarks.task_scorer import selection
from retro.benchmarks.task_scorer.context_cli import (
    MAX_LIMIT,
    ContextError,
    RetroContext,
    main,
)
from retro.benchmarks.task_scorer.schema import ProjectEnvironment
from retro.storage import Layout
from tests.task_scorer_helpers import (
    event,
    install_session,
    make_repo,
    project_environment,
    repo_state_json,
    rollout_events,
)


@pytest.fixture
def context(tmp_path: Path) -> RetroContext:
    root = tmp_path / "project"
    shas = make_repo(root)
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    events = rollout_events(base_sha=shas["base_sha"], commit_short=shas["outcome_sha"][:7])
    events.extend(
        event(
            index,
            actor="tool",
            event_type="command",
            payload={"command": f"pytest -q tests/test_{index}.py", "exit_code": index % 2},
        )
        for index in range(10, 40)
    )
    install_session(
        layout,
        events=events,
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
    return RetroContext.open(built.path)


def test_manifest_command_returns_versioned_manifest(context: RetroContext):
    manifest = context.manifest()
    assert manifest["schema_version"] == "retro-source-bundle-v1"
    assert manifest["repo"]["base_resolution"] == "captured_start"


def test_rollout_pagination_covers_every_event_exactly_once(context: RetroContext):
    total = context.rollout_list(limit=1)["total"]
    seen: list[str] = []
    cursor = 0
    while True:
        page = context.rollout_list(cursor=cursor, limit=7)
        seen.extend(item["event_id"] for item in page["events"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert len(seen) == total
    assert len(set(seen)) == total
    assert seen == [item["event_id"] for item in context.rollout_list(limit=MAX_LIMIT)["events"]]


def test_rollout_filters_and_limit_clamping(context: RetroContext):
    users = context.rollout_list(actor="user")
    assert users["total"] >= 2
    assert {item["actor"] for item in users["events"]} == {"user"}

    commands = context.rollout_list(event_type="command")
    assert {item["event_type"] for item in commands["events"]} == {"command"}

    clamped = context.rollout_list(limit=10_000)
    assert clamped["limit"] == MAX_LIMIT
    with pytest.raises(ContextError):
        context.rollout_list(limit=0)
    with pytest.raises(ContextError):
        context.rollout_list(cursor=-1)


def test_rollout_show_and_search(context: RetroContext):
    shown = context.rollout_show("session-1:2")
    assert shown["actor"] == "user"
    assert "Guard divide" in shown["text"]
    with pytest.raises(ContextError, match="unknown event_id"):
        context.rollout_show("session-1:99999")

    found = context.rollout_search("guard divide", actor="user")
    assert found["total"] >= 1
    assert found["events"][0]["event_id"] == "session-1:2"
    with pytest.raises(ContextError):
        context.rollout_search("")


def test_commands_listing_supports_failed_only(context: RetroContext):
    every = context.commands(limit=MAX_LIMIT)
    failed = context.commands(failed_only=True, limit=MAX_LIMIT)
    assert failed["total"] < every["total"]
    assert all(item["exit_code"] not in (0, None) for item in failed["commands"])


def test_repo_tree_read_and_grep_are_state_scoped(context: RetroContext):
    tree = context.repo_tree(state="base", depth=2)
    paths = {entry["path"] for entry in tree["entries"]}
    assert "src/calc.py" in paths
    assert "tests/test_divide.py" not in paths

    outcome_tree = context.repo_tree(state="outcome", path="tests", depth=1)
    assert {entry["path"] for entry in outcome_tree["entries"]} >= {"tests/test_divide.py"}

    read = context.repo_read(state="base", path="src/calc.py", start=1, end=2)
    assert read["lines"] == ["def add(a, b):", "    return a + b"]
    assert read["total_lines"] == 2

    grep = context.repo_grep(state="outcome", query="division by zero", glob="src/*.py")
    assert grep["total"] == 1
    assert grep["matches"][0]["path"] == "src/calc.py"
    assert context.repo_grep(state="base", query="division by zero")["total"] == 0


def test_repo_access_is_confined_to_the_bundle(context: RetroContext):
    with pytest.raises(ContextError, match="escapes the bundle"):
        context.repo_read(state="base", path="../../manifest.json")
    with pytest.raises(ContextError, match="state must be"):
        context.repo_tree(state="oracle")
    with pytest.raises(ContextError, match="file not found"):
        context.repo_read(state="base", path="src/missing.py")


def test_repo_diff_sections_and_git_log(context: RetroContext):
    diff = context.repo_diff()
    assert "src/calc.py" in diff["paths"]
    assert any("def divide" in section["patch"] for section in diff["sections"])

    scoped = context.repo_diff(path="src/calc.py")
    assert scoped["paths"] == ["src/calc.py"]
    with pytest.raises(ContextError, match="no diff section"):
        context.repo_diff(path="src/nope.py")

    log = context.git_log(max_count=5)
    assert log["total"] == 1
    assert log["commits"][0]["subject"] == "add divide with zero guard"


def test_cli_emits_json_and_reports_errors(context: RetroContext, capsys):
    assert main(["--bundle", str(context.root), "manifest"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_id"] == "codex__session-1"

    assert main(["--bundle", str(context.root), "rollout", "list", "--limit", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["events"]) == 2
    assert payload["next_cursor"] == 2

    assert main(["--bundle", str(context.root), "rollout", "show", "nope"]) == 2
    assert "unknown event_id" in json.loads(capsys.readouterr().out)["error"]

    assert main(["--bundle", str(context.root), "repo", "grep", "--state", "base", "--query", "add"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] >= 1


def test_open_rejects_non_bundle_directory(tmp_path: Path):
    with pytest.raises(ContextError, match="not a source bundle"):
        RetroContext.open(tmp_path)


def test_open_rejects_tampered_bundle(context: RetroContext):
    (context.root / "repo" / "base" / "README.md").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ContextError, match="checksum mismatch"):
        RetroContext.open(context.root)
