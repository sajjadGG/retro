from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import retro.catalog.rules as catalog_rules_module
import retro.catalog.store as catalog_store
from retro.benchmarks.task_scorer.selection import select_source
from retro.catalog import SessionRecord, load_catalog, update_catalog
from retro.catalog.annotations import append_annotation
from retro.catalog.markers import parse_markers
from retro.cli import _mine_targets, app
from retro.config import RetroConfig, save_config
from retro.experiment_traces import (
    EXPERIMENT_METADATA_HEADER,
    EXPERIMENT_RULE_ID,
    EXPERIMENT_TAG,
    EXPERIMENT_TAG_PREFIX,
    MAX_EXPERIMENT_ID_LENGTH,
    is_experiment_tag,
    tag_experiment_prompt,
)
from retro.importers.base import GHOSTLAB_COPILOT_SESSION_ID_PREFIX
from retro.importers.claude import ClaudeImporter
from retro.importers.codex import CodexImporter
from retro.importers.copilot_cli import CopilotCliImporter
from retro.importers.vscode_copilot import VscodeCopilotImporter
from retro.memory_store import reindex
from retro.mining import mine_with_method
from retro.schema import BUILTIN_HOSTS, NormalizedEvent, RawRef, read_events, write_events
from retro.signals.analyze import analyze_sessions
from retro.signals.store import load_signal_store, update_signals
from retro.storage import Layout
from retro.sync import update_rendered
from retro.utils import event_text

FIXTURES = Path(__file__).parent / "fixtures"


def _layout(tmp_path: Path, **config_overrides) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    save_config(
        RetroConfig(
            archive_root=str(layout.root),
            dashboard_dir=str(tmp_path / "dashboard"),
            **config_overrides,
        )
    )
    return layout


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _fixture_records(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    ]


def _write_session(layout: Layout, host: str, session_id: str, prompt: str) -> None:
    write_events(
        layout.normalized_path(host, session_id),
        [
            NormalizedEvent(
                event_id=f"{session_id}:{sequence}",
                session_id=session_id,
                host=host,
                sequence=sequence,
                actor=actor,
                event_type="message",
                summary=text[:200],
                raw_ref=RawRef(
                    path=f"raw/{host}/{session_id}/source.jsonl",
                    line=sequence,
                ),
                timestamp=f"2026-10-10T10:00:0{sequence}Z",
                payload={"text": text},
            )
            for sequence, actor, text in (
                (1, "user", prompt),
                (2, "assistant", "I fixed the regression and verified the tests."),
            )
        ],
    )


def _write_parent(layout: Layout, host: str, session_id: str, parent_id: str) -> None:
    raw = layout.raw_dir(host, session_id)
    raw.mkdir(parents=True, exist_ok=True)
    if host == "codex":
        _write_jsonl(
            raw / "rollout.jsonl",
            [
                {
                    "type": "session_meta",
                    "payload": {
                        "source": {
                            "subagent": {"thread_spawn": {"parent_thread_id": parent_id}}
                        }
                    },
                }
            ],
        )
    elif host == "claude-code":
        _write_jsonl(
            raw / "transcript.jsonl",
            [{"type": "user", "isSidechain": True, "parentSessionId": parent_id}],
        )
    else:
        (raw / "import_meta.json").write_text(
            json.dumps({"parent_session_id": parent_id}),
            encoding="utf-8",
        )


def _capture(
    tmp_path: Path,
    layout: Layout,
    source: str,
    session_id: str,
    prompt: str,
) -> tuple[str, str]:
    if source == "codex":
        home = tmp_path / "codex-home"
        records = _fixture_records("codex_rollout.jsonl")
        records[0]["payload"]["id"] = session_id
        records[0]["payload"]["cwd"] = "/workspace"
        records[2]["payload"]["message"] = prompt
        _write_jsonl(home / "sessions" / f"{session_id}.jsonl", records)
        result = CodexImporter(layout, codex_home=home).import_session(identifier=session_id)
    elif source == "claude-code":
        home = tmp_path / "claude-home"
        records = _fixture_records("claude_transcript.jsonl")
        records[1]["message"]["content"] = prompt
        _write_jsonl(home / "projects" / "project" / f"{session_id}.jsonl", records)
        result = ClaudeImporter(layout, claude_home=home).import_session(identifier=session_id)
    elif source in {"copilot-cli", "vscode-agent"}:
        state_root = tmp_path / "copilot-home" / "session-state"
        state_dir = state_root / session_id
        records = _fixture_records("copilot_cli_events.jsonl")
        records[0]["data"]["sessionId"] = session_id
        records[1]["data"]["content"] = prompt
        _write_jsonl(state_dir / "events.jsonl", records)
        client = "vscode-agent-host" if source == "vscode-agent" else "copilot-cli"
        (state_dir / "workspace.yaml").write_text(
            f"cwd: /workspace\nclient_name: {client}\n",
            encoding="utf-8",
        )
        result = CopilotCliImporter(layout, session_state_dir=state_root).import_session(
            identifier=session_id
        )
    else:
        user_dir = tmp_path / "vscode-user"
        workspace = user_dir / "workspaceStorage" / "workspace-001"
        core = _fixture_records("vscode_copilot_session.jsonl")
        core[0]["v"]["sessionId"] = session_id
        core[1]["v"][0]["message"]["text"] = prompt
        _write_jsonl(workspace / "chatSessions" / f"{session_id}.jsonl", core)
        transcript = _fixture_records("vscode_copilot_transcript.jsonl")
        transcript[0]["data"]["sessionId"] = session_id
        transcript[1]["data"]["content"] = prompt
        _write_jsonl(
            workspace / "GitHub.copilot-chat" / "transcripts" / f"{session_id}.jsonl",
            transcript,
        )
        (workspace / "workspace.json").write_text(
            '{"folder":"file:///workspace"}',
            encoding="utf-8",
        )
        result = VscodeCopilotImporter(layout, user_data_dir=user_dir).import_session(
            identifier=session_id
        )
    return result.host, result.session_id


@pytest.mark.parametrize("experiment_id", ["run-001", "A_b.c-123", "x" * MAX_EXPERIMENT_ID_LENGTH])
def test_tag_experiment_prompt_preserves_task_and_is_deterministic(experiment_id: str):
    prompt = "Fix the regression.\n\nKeep this whitespace intact.  \n"
    tagged = tag_experiment_prompt(prompt, experiment_id)

    assert tagged.startswith(prompt + "\n\n")
    assert tagged == tag_experiment_prompt(prompt, experiment_id)
    assert tag_experiment_prompt(tagged, experiment_id) == tagged
    assert tagged.count(EXPERIMENT_METADATA_HEADER) == 1
    markers = parse_markers(tagged, event_id="user:1")
    assert markers == [
        {"directive": "ignore", "value": None, "event_id": "user:1"},
        {"directive": "tag", "value": EXPERIMENT_TAG, "event_id": "user:1"},
        {
            "directive": "tag",
            "value": f"{EXPERIMENT_TAG_PREFIX}{experiment_id}",
            "event_id": "user:1",
        },
    ]
    assert all(len(marker["value"]) <= 64 for marker in markers if marker["value"])


@pytest.mark.parametrize(
    "experiment_id",
    [
        "",
        ".",
        "..",
        "../escape",
        "nested/id",
        "nested\\id",
        "-leading",
        "_leading",
        "has spaces",
        "id\n",
        "id\x00",
        "éval",
        "id#retro:keep",
        "x" * (MAX_EXPERIMENT_ID_LENGTH + 1),
    ],
)
def test_tag_experiment_prompt_rejects_invalid_ids(experiment_id: str):
    with pytest.raises(ValueError, match="experiment ID"):
        tag_experiment_prompt("Task", experiment_id)


@pytest.mark.parametrize("fence", ["```", "````", "~~~", "~~~~"])
def test_tag_experiment_prompt_keeps_metadata_visible_after_unclosed_fence(fence: str):
    prompt = f"Fix this code:\n{fence}text\n#retro:tag=not-real\n"
    tagged = tag_experiment_prompt(prompt, "unclosed")

    assert tagged.startswith(prompt)
    assert tag_experiment_prompt(tagged, "unclosed") == tagged
    assert [marker["value"] for marker in parse_markers(tagged, event_id="u1")] == [
        None,
        "experiment",
        "experiment/unclosed",
    ]


def test_tagging_a_fenced_metadata_example_adds_real_visible_provenance():
    example = "```text\n" + tag_experiment_prompt("Example task", "example")

    tagged = tag_experiment_prompt(example, "example")

    assert tagged.startswith(example)
    assert tag_experiment_prompt(tagged, "example") == tagged
    assert [marker["value"] for marker in parse_markers(tagged, event_id="u1")] == [
        None,
        "experiment",
        "experiment/example",
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("experiment", True),
        ("EXPERIMENT/Run-001", True),
        ("experiment/" + "x" * MAX_EXPERIMENT_ID_LENGTH, True),
        ("experiments", False),
        ("experimental", False),
        ("experiment/", False),
        ("experiment/../other", False),
        ("experiment/nested/id", False),
        ("experiment/" + "x" * (MAX_EXPERIMENT_ID_LENGTH + 1), False),
        (None, False),
    ],
)
def test_is_experiment_tag_uses_reserved_namespace_only(value: object, expected: bool):
    assert is_experiment_tag(value) is expected


@pytest.mark.parametrize(
    "source",
    ["codex", "claude-code", "copilot-cli", "vscode-agent", "vscode-chat"],
)
def test_captured_experiment_prompts_are_excluded_and_evidence_linked(
    tmp_path: Path,
    source: str,
):
    layout = _layout(tmp_path)
    prompt = "Fix the regression while preserving these details. " + "context " * 400
    tagged = tag_experiment_prompt(prompt, "capture-001")
    host, session_id = _capture(tmp_path, layout, source, "experiment", tagged)
    _capture(tmp_path, layout, source, "normal", prompt)
    raw_files = {
        path: path.read_bytes()
        for path in (layout.root / "raw").rglob("*")
        if path.is_file()
    }
    events = list(read_events(layout.normalized_path(host, session_id)))
    user = next(event for event in events if event.actor == "user" and event_text(event) == tagged)

    update_catalog(layout)
    catalog = load_catalog(layout)
    record = catalog[f"{host}/{session_id}"]

    assert record.excluded is True
    assert "experiment/capture-001" not in (record.first_prompt or "")
    assert {"ignore", "experiment", "experiment/capture-001"} <= set(record.tags)
    assert record.tag_sources["experiment/capture-001"] == ["marker"]
    assert f"rule:{EXPERIMENT_RULE_ID}" in record.tag_sources["ignore"]
    assert {
        marker["value"]
        for marker in record.markers
        if marker["event_id"] == user.event_id
    } == {None, "experiment", "experiment/capture-001"}
    assert event_text(user).startswith(prompt)
    assert user.raw_ref.path.startswith(f"raw/{host}/{session_id}/")
    assert catalog[f"{host}/normal"].excluded is False
    assert {item.key for item in catalog.included()} == {f"{host}/normal"}
    assert {path: path.read_bytes() for path in raw_files} == raw_files


@pytest.mark.parametrize("host", [*BUILTIN_HOSTS, "opencode", "custom-source"])
@pytest.mark.parametrize("marker", ["#retro:tag=experiment", "#ReTrO:tag=EXPERIMENT/Run-001"])
def test_experiment_tag_alone_is_excluded_for_any_captured_host(
    tmp_path: Path,
    host: str,
    marker: str,
):
    layout = _layout(tmp_path)
    _write_session(layout, host, "tag-only", f"Fix the regression.\n{marker}")

    update_catalog(layout)
    record = load_catalog(layout)[f"{host}/tag-only"]

    assert record.excluded is True
    assert record.tag_sources["ignore"] == [f"rule:{EXPERIMENT_RULE_ID}"]
    assert any(marker["event_id"] == "tag-only:1" for marker in record.markers)


@pytest.mark.parametrize(
    "discussion",
    [
        "Discuss whether experiment or evaluation runs improve our workflows.",
        "Explain '#retro:tag=experiment' and '#retro:ignore' as examples.",
        'Explain "#retro:tag=experiment/demo" as an example.',
        'text = f"#retro:tag=experiment"',
        "text = r'#retro:tag=experiment/demo'",
        "Explain “#retro:tag=experiment” and ‘#retro:ignore’ as examples.",
        "`#retro:ignore` and ``#retro:tag=experiment/demo`` are directives.",
        "```text\n#retro:ignore\n#retro:tag=experiment\n```",
        "```text\n" + tag_experiment_prompt("Example task", "example") + "\n```",
        "~~~text\n#retro:ignore\n#retro:tag=experiment/demo\n~~~",
        "````text\n```\n#retro:ignore\n#retro:tag=experiment/demo\n````",
        "> #retro:ignore\n> #retro:tag=experiment/demo",
        "    #retro:ignore\n    #retro:tag=experiment/demo",
        '{"prompt": "#retro:ignore #retro:tag=experiment"}',
        "#retro:tag=experimental #retro:tag=experiments",
    ],
)
def test_experiment_discussion_and_code_examples_do_not_exclude(
    tmp_path: Path,
    discussion: str,
):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "discussion", f"Document this behavior:\n{discussion}")

    update_catalog(layout)
    record = load_catalog(layout)["codex/discussion"]

    assert record.excluded is False
    assert "experiment" not in record.tags
    assert "ignore" not in record.tags


def test_assistant_markers_do_not_classify_human_sessions(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "human", "Explain experiment provenance.")
    events = list(read_events(layout.normalized_path("codex", "human")))
    events[1].payload["text"] = tag_experiment_prompt("Here is an example.", "example")
    write_events(layout.normalized_path("codex", "human"), events)

    update_catalog(layout)

    assert load_catalog(layout)["codex/human"].excluded is False


def test_run_provenance_survives_synthetic_leading_prompt_without_human_evidence(tmp_path: Path):
    layout = _layout(tmp_path)
    prompt = "<environment_context>\nTask context\n</environment_context>"
    _write_session(layout, "codex", "synthetic", tag_experiment_prompt(prompt, "synthetic"))

    update_catalog(layout)
    record = load_catalog(layout)["codex/synthetic"]

    assert record.excluded is True
    assert record.first_prompt is None
    assert record.counts["user_messages"] == 0
    assert "experiment/synthetic" in record.tags
    assert all(marker["event_id"] == "synthetic:1" for marker in record.markers)


@pytest.mark.parametrize(
    ("prompt", "config", "excluded"),
    [
        ("#retro:tag=experiment #retro:keep", {}, False),
        ("#retro:tag=experiment", {"disabled_rules": [EXPERIMENT_RULE_ID]}, False),
        ("#retro:tag=experiment", {"exclude_tags": []}, False),
        ("#retro:tag=experiment #retro:keep", {"exclude_tags": ["experiment"]}, True),
        (tag_experiment_prompt("Task #retro:keep", "explicit"), {}, True),
    ],
)
def test_experiment_rule_respects_existing_explicit_policy_overrides(
    tmp_path: Path,
    prompt: str,
    config: dict,
    excluded: bool,
):
    layout = _layout(tmp_path, **config)
    _write_session(layout, "codex", "override", f"Do this task.\n{prompt}")

    update_catalog(layout)

    assert load_catalog(layout)["codex/override"].excluded is excluded


def test_manual_untag_can_override_explicit_experiment_ignore(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "curated", tag_experiment_prompt("Task", "curated"))
    update_catalog(layout)
    append_annotation(layout.root, key="codex/curated", op="untag", value="ignore")

    update_catalog(layout)
    record = load_catalog(layout)["codex/curated"]

    assert record.excluded is False
    assert {"experiment", "experiment/curated"} <= set(record.tags)


@pytest.mark.parametrize("host", BUILTIN_HOSTS)
def test_experiment_provenance_excludes_only_evidenced_descendants(tmp_path: Path, host: str):
    layout = _layout(tmp_path)
    _write_session(layout, host, "z-root", tag_experiment_prompt("Replay a task", "family"))
    _write_session(layout, host, "a-child", "Investigate the regression")
    _write_session(layout, host, "grandchild", "Check one implementation detail")
    _write_session(layout, host, "normal", "Normal human task")
    _write_session(layout, host, "normal-child", "Useful delegated human work")
    _write_parent(layout, host, "a-child", "z-root")
    _write_parent(layout, host, "grandchild", "a-child")
    _write_parent(layout, host, "normal-child", "normal")

    update_catalog(layout)
    catalog = load_catalog(layout)

    assert {record.key for record in catalog.excluded()} == {
        f"{host}/z-root",
        f"{host}/a-child",
        f"{host}/grandchild",
    }
    child = catalog[f"{host}/a-child"]
    assert child.experiment_parent_key == f"{host}/z-root"
    assert f"rule:{EXPERIMENT_RULE_ID}:parent:{host}/z-root" in child.tag_sources["ignore"]
    assert catalog[f"{host}/grandchild"].experiment_parent_key == child.key
    assert catalog[f"{host}/normal-child"].is_subagent is True
    assert catalog[f"{host}/normal-child"].excluded is False
    assert update_catalog(layout).changed_keys == []


def test_partial_parent_updates_refresh_child_policy_without_rescanning_children(
    monkeypatch,
    tmp_path: Path,
):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "parent", "Normal task")
    _write_session(layout, "codex", "child", "Useful delegated human work")
    _write_parent(layout, "codex", "child", "parent")
    update_catalog(layout)
    original = load_catalog(layout)["codex/child"].fingerprint
    scan = catalog_store._scan_events

    def scan_parent_only(path: Path):
        assert path == layout.normalized_path("codex", "parent")
        return scan(path)

    monkeypatch.setattr(catalog_store, "_scan_events", scan_parent_only)
    _write_session(layout, "codex", "parent", "Replay task #retro:tag=experiment")

    updated = update_catalog(layout, keys=["codex/parent"])
    catalog = load_catalog(layout)

    assert updated.changed_keys == ["codex/child", "codex/parent"]
    assert catalog["codex/child"].excluded is True
    assert catalog["codex/child"].fingerprint == original
    _write_session(layout, "codex", "parent", "Normal task again")
    updated = update_catalog(layout, keys=["codex/parent"])
    child = load_catalog(layout)["codex/child"]
    assert updated.changed_keys == ["codex/child", "codex/parent"]
    assert child.excluded is False
    assert child.experiment_parent_key is None
    assert "experiment" not in child.tags
    assert child.fingerprint == original


def test_child_keep_override_does_not_lose_descendant_provenance(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "parent", "Replay task #retro:tag=experiment")
    _write_session(layout, "codex", "child", "Curated delegated work #retro:keep")
    _write_session(layout, "codex", "grandchild", "Uncurated generated work")
    _write_parent(layout, "codex", "child", "parent")
    _write_parent(layout, "codex", "grandchild", "child")

    update_catalog(layout)
    catalog = load_catalog(layout)

    assert catalog["codex/child"].excluded is False
    assert catalog["codex/child"].experiment_parent_key == "codex/parent"
    assert catalog["codex/grandchild"].excluded is True


def test_uncaptured_native_copilot_parent_is_exact_machine_evidence(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "vscode-copilot", "child", "Generated delegated work")
    _write_parent(
        layout,
        "vscode-copilot",
        "child",
        GHOSTLAB_COPILOT_SESSION_ID_PREFIX + "4abc-8abc-0123456789ab",
    )

    update_catalog(layout)
    child = load_catalog(layout)["vscode-copilot/child"]

    assert child.excluded is True
    assert child.parent_key == (
        "vscode-copilot/" + GHOSTLAB_COPILOT_SESSION_ID_PREFIX + "4abc-8abc-0123456789ab"
    )
    assert "experiment" in child.tags


def test_cyclic_non_experiment_parentage_does_not_exclude_normal_traces(tmp_path: Path):
    layout = _layout(tmp_path)
    for session_id, parent_id in (("a", "b"), ("b", "a")):
        _write_session(layout, "codex", session_id, "Useful human work")
        _write_parent(layout, "codex", session_id, parent_id)

    update_catalog(layout)

    assert {record.key for record in load_catalog(layout).included()} == {"codex/a", "codex/b"}


@pytest.mark.parametrize(
    ("host", "session_id", "originator", "excluded"),
    [
        ("codex", "native", "ghostlab", True),
        ("codex", "native", " GhOsTlAb ", True),
        ("codex", "native", "ghostlab-evaluation", False),
        ("codex", "native", "codex_exec", False),
        ("codex", "native", None, False),
        ("vscode-copilot", GHOSTLAB_COPILOT_SESSION_ID_PREFIX + "4abc-8abc-0123456789ab", None, True),
        ("vscode-copilot", "67686f73-746d-4abc-8abc-0123456789ab", None, False),
        ("vscode-copilot", "x67686f73-746c-4abc-8abc-0123456789ab", None, False),
        ("codex", GHOSTLAB_COPILOT_SESSION_ID_PREFIX + "4abc-8abc-0123456789ab", None, False),
    ],
)
def test_historical_native_runs_require_verified_machine_provenance(
    tmp_path: Path,
    host: str,
    session_id: str,
    originator: str | None,
    excluded: bool,
):
    layout = _layout(tmp_path)
    _write_session(layout, host, session_id, "Improve our experiment tooling in Ghostlab.")
    raw = layout.raw_dir(host, session_id)
    raw.mkdir(parents=True)
    if host == "codex":
        _write_jsonl(
            raw / "rollout.jsonl",
            [{"type": "session_meta", "payload": {"originator": originator, "cwd": "/workspace"}}],
        )
    else:
        (raw / "import_meta.json").write_text(
            '{"source_kind":"copilot-cli","cwd":"/workspace"}',
            encoding="utf-8",
        )
    raw_files = {path: path.read_bytes() for path in raw.rglob("*") if path.is_file()}

    update_catalog(layout)
    record = load_catalog(layout)[f"{host}/{session_id}"]

    assert record.excluded is excluded
    assert ("experiment" in record.tags) is excluded
    if excluded:
        assert record.tag_sources["ignore"] == [f"rule:{EXPERIMENT_RULE_ID}"]
    assert record.originator == (originator.strip() if originator else None)
    assert SessionRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()
    assert {path: path.read_bytes() for path in raw_files} == raw_files


def test_new_experiment_policy_reevaluates_cached_catalog_without_event_scan(
    monkeypatch,
    tmp_path: Path,
):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "experiment", "Task #retro:tag=experiment")
    _write_session(layout, "codex", "normal", "Normal task")
    assert catalog_store.TAG_EVALUATOR_VERSION != "catalog-tags-v3"
    with monkeypatch.context() as previous_policy:
        previous_policy.setattr(catalog_store, "TAG_EVALUATOR_VERSION", "catalog-tags-v3")
        previous_policy.setattr(
            catalog_rules_module,
            "_BUILTIN_RULES",
            tuple(
                rule
                for rule in catalog_rules_module._BUILTIN_RULES
                if rule.id != EXPERIMENT_RULE_ID
            ),
        )
        update_catalog(layout)
    before = load_catalog(layout)["codex/experiment"]
    old_state = catalog_store.catalog_state_path(layout).read_bytes()
    assert before.excluded is False
    monkeypatch.setattr(
        catalog_store,
        "_scan_events",
        lambda path: pytest.fail(f"unexpected event scan: {path}"),
    )

    update = update_catalog(layout)
    after = load_catalog(layout)["codex/experiment"]

    assert update.changed_keys == ["codex/experiment"]
    assert after.fingerprint == before.fingerprint
    assert after.excluded is True
    assert after.tag_sources["ignore"] == [f"rule:{EXPERIMENT_RULE_ID}"]
    assert catalog_store.catalog_state_path(layout).read_bytes() != old_state
    assert update_catalog(layout).changed_keys == []


def test_new_extractor_recovers_historical_originator_without_raw_mutation(
    monkeypatch,
    tmp_path: Path,
):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "legacy", "Normal-looking task")
    raw_path = layout.raw_dir("codex", "legacy") / "rollout.jsonl"
    _write_jsonl(raw_path, [{"type": "session_meta", "payload": {"originator": "ghostlab"}}])
    original = raw_path.read_bytes()
    extract = catalog_store.extract_host_metadata
    assert catalog_store.EXTRACTOR_VERSION != "catalog-v1-v3-compatible-16"

    def legacy_metadata(host: str, session_id: str, raw_dir: Path):
        metadata = extract(host, session_id, raw_dir)
        metadata.originator = None
        return metadata

    with monkeypatch.context() as previous_extractor:
        previous_extractor.setattr(
            catalog_store,
            "EXTRACTOR_VERSION",
            "catalog-v1-v3-compatible-16",
        )
        previous_extractor.setattr(catalog_store, "extract_host_metadata", legacy_metadata)
        update_catalog(layout)
    before = load_catalog(layout)["codex/legacy"]
    assert before.excluded is False

    update = update_catalog(layout)
    after = load_catalog(layout)["codex/legacy"]

    assert update.changed_keys == ["codex/legacy"]
    assert after.fingerprint != before.fingerprint
    assert after.originator == "ghostlab"
    assert after.excluded is True
    assert raw_path.read_bytes() == original


def test_new_experiment_exclusions_reach_mining_memory_signals_and_rendering(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "normal", "Fix the normal regression")
    _write_session(layout, "codex", "experiment", "Replay a task")
    update_catalog(layout)
    update_signals(layout, load_catalog(layout))
    assert set(load_signal_store(layout).keys) == {"codex/normal", "codex/experiment"}
    normal_path = layout.normalized_path("codex", "normal")
    normal_mining = mine_with_method(normal_path).to_dict()
    _write_session(layout, "codex", "experiment", tag_experiment_prompt("Replay a task", "run-001"))

    update_catalog(layout)
    catalog = load_catalog(layout)
    targets = _mine_targets(layout, "*", "*", all_sessions=True)

    assert targets == [("codex", "normal")]
    assert _mine_targets(
        layout, "codex", "*", all_sessions=True, include_excluded=True
    ) == [("codex", "experiment"), ("codex", "normal")]
    assert mine_with_method(normal_path).to_dict() == normal_mining
    update = update_signals(layout, catalog)
    assert update.removed_keys == ["codex/experiment"]
    assert set(load_signal_store(layout).keys) == {"codex/normal"}
    assert analyze_sessions(layout, catalog)["sessions"] == 1
    assert update_rendered(layout, keys=set(catalog), catalog=catalog) == {"codex/normal"}
    assert layout.rendered_path("codex", "normal").is_file()
    assert not layout.rendered_path("codex", "experiment").exists()

    for session_id in ("normal", "experiment"):
        path = layout.mined_json_path("codex", session_id, "test")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "host": "codex",
                    "session_id": session_id,
                    "method": "test",
                    "candidates": [
                        {
                            "id": session_id,
                            "kind": "case",
                            "scope": "global",
                            "text": f"memory for {session_id}",
                            "when_to_use": "always",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    report = reindex(layout)
    assert report.mined_records == 1
    assert report.indexed == 1
    candidate, rejection = select_source(
        layout=layout,
        host="codex",
        session_id="experiment",
        require_environment=False,
    )
    assert candidate is None
    assert rejection is not None
    assert rejection.code == "EXCLUDED_BY_CATALOG"
    assert "experiment" in rejection.evidence["tags"]


def test_builtin_experiment_rule_supports_cli_dry_run(tmp_path: Path):
    layout = _layout(tmp_path)
    _write_session(layout, "codex", "experiment", "Task #retro:tag=experiment")
    _write_session(layout, "codex", "normal", "Normal task")
    update_catalog(layout)

    result = CliRunner().invoke(
        app,
        ["rules", "test", "--root", str(layout.root), "--json"],
    )

    assert result.exit_code == 0, result.output
    rule = next(item for item in json.loads(result.output) if item["id"] == EXPERIMENT_RULE_ID)
    assert rule["matches"] == 1
