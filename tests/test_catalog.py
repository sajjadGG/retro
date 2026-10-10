from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import retro.catalog.store as catalog_store
from retro.catalog import SessionRecord, load_catalog, update_catalog
from retro.catalog.annotations import append_annotation
from retro.catalog.markers import parse_markers
from retro.catalog.metadata import parse_simple_yaml
from retro.catalog.projects import (
    discover_git,
    normalize_remote,
    path_glob_matches,
    resolve_cwd,
)
from retro.catalog.rules import (
    CatalogRule,
    catalog_rules,
    is_temp_path,
    rule_matches,
)
from retro.config import RetroConfig, save_config
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout


def _event(
    host: str,
    session_id: str,
    sequence: int,
    *,
    actor: str = "user",
    event_type: str = "message",
    text: str = "",
    timestamp: str | None = None,
    payload: dict | None = None,
) -> NormalizedEvent:
    body = dict(payload or {})
    if text:
        body["text"] = text
    return NormalizedEvent(
        event_id=f"{session_id}:{sequence}",
        session_id=session_id,
        host=host,
        sequence=sequence,
        actor=actor,  # type: ignore[arg-type]
        event_type=event_type,  # type: ignore[arg-type]
        summary=text[:200],
        raw_ref=RawRef(path=f"raw/{host}/{session_id}/source.jsonl", line=sequence),
        timestamp=timestamp or f"2026-10-01T10:00:{sequence:02d}Z",
        payload=body,
    )


def _write_normalized(
    layout: Layout,
    host: str,
    session_id: str,
    events: list[NormalizedEvent] | None = None,
) -> None:
    write_events(
        layout.normalized_path(host, session_id),
        events
        or [
            _event(host, session_id, 1, text=f"prompt for {session_id}"),
            _event(
                host,
                session_id,
                2,
                actor="assistant",
                text="done",
            ),
        ],
    )


def _config(tmp_path: Path, **overrides) -> RetroConfig:
    values = {
        "archive_root": str(tmp_path / "archive"),
        "dashboard_dir": str(tmp_path / "dashboard"),
    }
    values.update(overrides)
    config = RetroConfig(**values)
    save_config(config)
    return config


def test_catalog_extracts_all_builtin_hosts_subagents_and_unknown(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)

    codex = layout.raw_dir("codex", "codex-child")
    codex.mkdir(parents=True)
    (codex / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "originator": "codex_exec",
                    "source": {
                        "subagent": {
                            "thread_spawn": {"parent_thread_id": "codex-parent"}
                        }
                    },
                    "cwd": str(tmp_path / "repo"),
                    "git": {
                        "repository_url": "git@github.com:Owner/Repo.git",
                        "branch": "feature",
                        "commit_hash": "abc123",
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (codex / "thread.json").write_text(
        json.dumps({"title": "Child task", "spawn_edges": {"parents": [], "children": []}}),
        encoding="utf-8",
    )
    _write_normalized(layout, "codex", "codex-child")

    claude = layout.raw_dir("claude-code", "claude-child")
    claude.mkdir(parents=True)
    (claude / "import_meta.json").write_text(
        json.dumps({"project_slug": "-Users-test-Dev-Repo"}),
        encoding="utf-8",
    )
    (claude / "transcript.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "entrypoint": "sdk-cli",
                "cwd": str(tmp_path / "claude-repo"),
                "gitBranch": "main",
                "isSidechain": True,
                "parentSessionId": "claude-parent",
                "aiTitle": "Sidechain analysis",
                "message": {"model": "claude-opus-4-1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "claude-code", "claude-child")

    copilot = layout.raw_dir("vscode-copilot", "copilot-cli")
    (copilot / "sidecars").mkdir(parents=True)
    (copilot / "import_meta.json").write_text(
        json.dumps(
            {
                "source_kind": "copilot-cli",
                "title": "Automated check",
                "cwd": str(tmp_path / "copilot-repo"),
                "repository": "Owner/Repo",
                "models": ["gpt-5.4"],
            }
        ),
        encoding="utf-8",
    )
    (copilot / "sidecars" / "workspace.yaml").write_text(
        'client_name: "github/cli"\ncommand: |-\n  copilot -p "check this"\n',
        encoding="utf-8",
    )
    _write_normalized(layout, "vscode-copilot", "copilot-cli")

    vscode = layout.raw_dir("vscode-copilot", "vscode-chat")
    vscode.mkdir(parents=True)
    (vscode / "import_meta.json").write_text(
        json.dumps(
            {
                "source_kind": "vscode-chat",
                "title": "Editor chat",
                "cwd": str(tmp_path / "vscode-repo"),
            }
        ),
        encoding="utf-8",
    )
    _write_normalized(layout, "vscode-copilot", "vscode-chat")

    unknown = layout.raw_dir("opencode", "external-1")
    unknown.mkdir(parents=True)
    (unknown / "import_meta.json").write_text(
        json.dumps({"title": "Extension", "cwd": str(tmp_path / "external")}),
        encoding="utf-8",
    )
    _write_normalized(layout, "opencode", "external-1")

    result = update_catalog(layout)
    catalog = load_catalog(layout)

    assert result.total == 5
    codex_record = catalog["codex/codex-child"]
    assert codex_record.client == "codex-exec"
    assert codex_record.interactive is False
    assert codex_record.is_subagent is True
    assert codex_record.parent_key == "codex/codex-parent"
    assert codex_record.git["remote"] == "github.com/owner/repo"
    assert codex_record.project["id"] == "github.com/owner/repo"

    claude_record = catalog["claude-code/claude-child"]
    assert claude_record.client == "claude-sdk"
    assert claude_record.is_subagent is True
    assert claude_record.parent_key == "claude-code/claude-parent"
    assert claude_record.title == "Sidechain analysis"
    assert "claude-opus-4-1" in claude_record.models

    copilot_record = catalog["vscode-copilot/copilot-cli"]
    assert copilot_record.client == "copilot-cli"
    assert copilot_record.interactive is False
    assert "automated" in copilot_record.tags
    assert copilot_record.project["id"] == "github.com/owner/repo"
    assert catalog["vscode-copilot/vscode-chat"].client == "copilot-vscode-chat"
    assert catalog["opencode/external-1"].agent == "opencode"
    assert catalog["opencode/external-1"].client == "unknown"


def test_codex_guardian_and_copilot_agent_host_classification(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    guardian = layout.raw_dir("codex", "guardian")
    guardian.mkdir(parents=True)
    (guardian / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "originator": "Codex Desktop",
                    "source": {"subagent": {"other": "guardian"}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "codex", "guardian")
    agent_host = layout.raw_dir("vscode-copilot", "agent-host")
    (agent_host / "sidecars").mkdir(parents=True)
    (agent_host / "import_meta.json").write_text(
        json.dumps({"source_kind": "copilot-cli"}),
        encoding="utf-8",
    )
    (agent_host / "sidecars" / "workspace.yaml").write_text(
        "client_name: vscode-agent-host\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "vscode-copilot", "agent-host")
    prompt_mode = layout.raw_dir("vscode-copilot", "prompt-mode")
    (prompt_mode / "sidecars").mkdir(parents=True)
    (prompt_mode / "import_meta.json").write_text(
        json.dumps({"source_kind": "copilot-cli", "cwd": str(tmp_path / "repo")}),
        encoding="utf-8",
    )
    (prompt_mode / "sidecars" / "workspace.yaml").write_text(
        "client_name: github/cli\n",
        encoding="utf-8",
    )
    (prompt_mode / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "user.message",
                "data": {
                    "content": "one-shot prompt",
                    "delivery": "idle",
                    "parentAgentTaskId": "task-1",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "vscode-copilot", "prompt-mode")

    update_catalog(layout)
    catalog = load_catalog(layout)

    assert catalog["codex/guardian"].client == "codex-desktop"
    assert catalog["codex/guardian"].is_subagent is True
    assert catalog["codex/guardian"].interactive is False
    assert catalog["codex/guardian"].parent_key is None
    assert catalog["vscode-copilot/agent-host"].client == "copilot-vscode-agent"
    assert catalog["vscode-copilot/prompt-mode"].interactive is False


def test_event_summary_accepts_normalization_v1_and_v2_types(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    session_id = "mixed-types"
    _write_normalized(
        layout,
        "codex",
        session_id,
        [
            _event("codex", session_id, 1, text="real prompt"),
            _event(
                "codex",
                session_id,
                2,
                actor="system",
                event_type="usage",
                payload={"usage": {"model": "gpt-5.4", "input_tokens": 100}},
            ),
            _event(
                "codex",
                session_id,
                3,
                actor="system",
                event_type="lifecycle",
                payload={"lifecycle": "turn_end"},
            ),
            _event(
                "codex",
                session_id,
                4,
                actor="system",
                event_type="metadata",
                payload={"selectedModel": "gpt-5.4-mini"},
            ),
            _event(
                "codex",
                session_id,
                5,
                actor="assistant",
                event_type="tool_call",
                payload={"name": "rg"},
            ),
        ],
    )

    update_catalog(layout)
    record = load_catalog(layout)[f"codex/{session_id}"]

    assert record.counts == {
        "events": 5,
        "user_messages": 1,
        "assistant_messages": 0,
        "tool_calls": 1,
        "unknown_events": 0,
    }
    assert record.first_prompt == "real prompt"
    assert record.models == ["gpt-5.4", "gpt-5.4-mini"]


def test_metadata_timestamps_supply_duration_without_event_timestamps(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("vscode-copilot", "metadata-time")
    raw.mkdir(parents=True)
    (raw / "import_meta.json").write_text(
        json.dumps(
            {
                "source_kind": "copilot-cli",
                "created_at": "2026-10-01T10:00:00Z",
                "updated_at": "2026-10-01T10:00:12.500Z",
            }
        ),
        encoding="utf-8",
    )
    _write_normalized(
        layout,
        "vscode-copilot",
        "metadata-time",
        [
            _event(
                "vscode-copilot",
                "metadata-time",
                1,
                text="prompt",
                timestamp=None,
            )
        ],
    )
    events_path = layout.normalized_path("vscode-copilot", "metadata-time")
    payload = json.loads(events_path.read_text(encoding="utf-8"))
    payload["timestamp"] = None
    events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    update_catalog(layout)
    record = load_catalog(layout)["vscode-copilot/metadata-time"]

    assert record.started_at == "2026-10-01T10:00:00Z"
    assert record.ended_at == "2026-10-01T10:00:12.500000Z"
    assert record.duration_s == 12.5
    assert record.active_s is None


def test_markers_ignore_code_fences_spans_quotes_and_assistant_text(tmp_path: Path):
    visible = """#retro:ignore
`#retro:tag=inline`
```text
#retro:tag=fenced
```
> #retro:tag=quoted
plain #ReTrO:tag=useful and #retro:project=Owner/Repo
"""
    assert parse_markers(visible, event_id="e1") == [
        {"directive": "ignore", "value": None, "event_id": "e1"},
        {"directive": "tag", "value": "useful", "event_id": "e1"},
        {"directive": "project", "value": "Owner/Repo", "event_id": "e1"},
    ]

    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("codex", "markers")
    raw.mkdir(parents=True)
    (raw / "thread.json").write_text(
        json.dumps({"title": "x" * 220 + " #retro:tag=title-tag"}),
        encoding="utf-8",
    )
    _write_normalized(
        layout,
        "codex",
        "markers",
        [
            _event("codex", "markers", 1, text=visible),
            _event(
                "codex",
                "markers",
                2,
                actor="assistant",
                text="#retro:tag=assistant-must-not-count",
            ),
        ],
    )

    update_catalog(layout)
    record = load_catalog(layout)["codex/markers"]

    assert {"ignore", "useful", "title-tag"} <= set(record.tags)
    assert "inline" not in record.tags
    assert "fenced" not in record.tags
    assert "quoted" not in record.tags
    assert "assistant-must-not-count" not in record.tags
    assert record.project["id"] == "github.com/owner/repo"
    assert len(record.title or "") == 200


def test_retroignore_keep_and_annotation_precedence(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("TMPDIR", "/not-this-test-workspace")
    workspace = tmp_path / "workspace"
    nested = workspace / "subdir"
    nested.mkdir(parents=True)
    (workspace / ".retroignore").write_text("evaluation\nignore\n# comment\n", encoding="utf-8")
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("codex", "ignored")
    raw.mkdir(parents=True)
    (raw / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(nested)}}) + "\n",
        encoding="utf-8",
    )
    _write_normalized(
        layout,
        "codex",
        "ignored",
        [_event("codex", "ignored", 1, text="#retro:keep useful work")],
    )

    update_catalog(layout)
    record = load_catalog(layout)["codex/ignored"]
    assert "evaluation" in record.tags
    assert "ignore" not in record.tags
    assert record.tag_sources["evaluation"] == [
        f"dotfile:{workspace / '.retroignore'}"
    ]
    before_fingerprint = record.fingerprint

    append_annotation(layout.root, key=record.key, op="untag", value="evaluation")
    append_annotation(layout.root, key=record.key, op="tag", value="curated")
    append_annotation(
        layout.root,
        key=record.key,
        op="set-project",
        value="github.com/example/manual",
    )
    monkeypatch.setattr(
        catalog_store,
        "_scan_events",
        lambda path: pytest.fail(f"unexpected event scan: {path}"),
    )
    update_catalog(layout, keys=[record.key])
    updated = load_catalog(layout)[record.key]
    assert updated.fingerprint == before_fingerprint
    assert "evaluation" not in updated.tags
    assert updated.tag_sources["curated"] == ["manual"]
    assert updated.project == {
        "id": "github.com/example/manual",
        "name": "manual",
        "source": "annotation",
        "evidence": "manual project github.com/example/manual",
    }


def test_empty_retroignore_means_ignore(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TMPDIR", "/not-this-test-workspace")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".retroignore").write_text("", encoding="utf-8")
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("codex", "empty-dotfile")
    raw.mkdir(parents=True)
    (raw / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(workspace)}})
        + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "codex", "empty-dotfile")

    update_catalog(layout)
    record = load_catalog(layout)["codex/empty-dotfile"]

    assert record.excluded is True
    assert record.tag_sources["ignore"] == [
        f"dotfile:{workspace / '.retroignore'}"
    ]


def _rule_record() -> SessionRecord:
    return SessionRecord(
        schema=1,
        key="codex/s1",
        host="codex",
        session_id="s1",
        agent="codex",
        client="codex-exec",
        interactive=False,
        is_subagent=False,
        parent_key=None,
        title="AIME evaluator",
        first_prompt="Below is a math problem",
        started_at="2026-09-15T12:00:00Z",
        ended_at="2026-09-15T12:01:00Z",
        duration_s=60,
        active_s=60,
        cwd="~/Dev/sandbox/run",
        cwd_resolved=str(Path.home() / "Dev/sandbox/run"),
        git={
            "remote": "github.com/example/eval",
            "remote_url": "git@github.com:example/eval.git",
            "branch": "main",
            "commit": "abc",
            "root": None,
            "worktree": None,
        },
        project={
            "id": "github.com/example/eval",
            "name": "eval",
            "source": "git-remote",
            "evidence": "remote",
        },
        models=["gpt-5.4"],
        counts={
            "events": 3,
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_calls": 0,
            "unknown_events": 0,
        },
        markers=[],
        tags=[],
        tag_sources={},
        excluded=False,
        fingerprint="x",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", ["codex"]),
        ("agent", ["codex"]),
        ("client", ["codex-exec"]),
        ("interactive", False),
        ("is_subagent", False),
        ("cwd", ["~/Dev/sandbox/**"]),
        ("project", ["github.com/example/eval"]),
        ("remote", ["github.com/example/eval"]),
        ("branch", ["main"]),
        ("model", ["gpt-5.4"]),
        ("title", "(?i)aime"),
        ("prompt", "(?i)^below is a math problem"),
        ("session_id", "^s1$"),
        ("min_user_messages", 1),
        ("max_user_messages", 1),
        ("started_after", "2026-09-01"),
        ("started_before", "2026-10-01"),
    ],
)
def test_every_rule_match_field(field: str, value):
    rule = CatalogRule(
        id=f"test-{field}",
        description="",
        enabled=True,
        match={field: value},
        tags=["ignore"],
    )
    assert rule_matches(_rule_record(), rule)


def test_rule_fields_are_anded_and_empty_lists_are_unconstrained():
    record = _rule_record()
    matching = CatalogRule(
        id="all",
        description="",
        enabled=True,
        match={
            "host": [],
            "client": ["codex-exec"],
            "interactive": False,
            "prompt": "(?i)math",
            "min_user_messages": 1,
            "max_user_messages": 1,
        },
        tags=["ignore"],
    )
    failing = CatalogRule(
        id="fail",
        description="",
        enabled=True,
        match={"client": ["claude-cli"], "prompt": "math"},
        tags=["ignore"],
    )
    assert rule_matches(record, matching)
    assert not rule_matches(record, failing)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp",
        "/tmp/session",
        "/private/tmp",
        "/private/tmp/copilot-probe2",
        "/var/tmp",
        "/var/tmp/session",
        "/private/var/tmp",
        "/private/var/tmp/session",
        "/var/folders/aa/bb/T",
        "/var/folders/aa/bb/T/session",
        "/private/var/folders/aa/bb/T",
        "/private/var/folders/aa/bb/T/session",
        "$TMPDIR",
        "$TMPDIR/session",
    ],
)
def test_temp_dir_rule_covers_platform_roots(
    monkeypatch,
    path: str,
):
    monkeypatch.setenv("TMPDIR", "/Volumes/External/custom-tmp/")
    assert is_temp_path(path)


def test_temp_dir_rule_rejects_neighboring_paths(monkeypatch):
    monkeypatch.setenv("TMPDIR", "/Volumes/External/custom-tmp")
    assert not is_temp_path("/var/folders/aa/bb/not-T/session")
    assert not is_temp_path("/Volumes/External/custom-tmp-other")
    assert not is_temp_path("/workspace/project")


@pytest.mark.parametrize(
    ("first_prompt", "user_messages", "assistant_messages", "tool_calls", "expected"),
    [
        (None, 0, 1, 0, True),
        (None, 1, 1, 0, True),
        ("prompt", 1, 0, 0, True),
        ("prompt", 1, 1, 0, False),
        ("prompt", 1, 0, 1, False),
    ],
)
def test_empty_rule_requires_prompt_and_activity(
    tmp_path: Path,
    first_prompt: str | None,
    user_messages: int,
    assistant_messages: int,
    tool_calls: int,
    expected: bool,
):
    record = _rule_record()
    record.first_prompt = first_prompt
    record.counts["user_messages"] = user_messages
    record.counts["assistant_messages"] = assistant_messages
    record.counts["tool_calls"] = tool_calls
    config = RetroConfig(
        archive_root=str(tmp_path / "archive"),
        dashboard_dir=str(tmp_path / "dashboard"),
    )
    empty_rule = next(
        rule
        for rule in catalog_rules(config)
        if rule.id == "builtin.empty"
    )

    assert rule_matches(record, empty_rule) is expected


def test_codex_v1_raw_messages_prevent_false_empty_tag(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("codex", "legacy-exec")
    raw.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {
                "originator": "codex_exec",
                "source": "exec",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Fix the parser"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Implemented"}],
            },
        },
    ]
    (raw / "rollout.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    _write_normalized(
        layout,
        "codex",
        "legacy-exec",
        [
            _event(
                "codex",
                "legacy-exec",
                1,
                actor="system",
                event_type="session_start",
            ),
            _event(
                "codex",
                "legacy-exec",
                2,
                actor="system",
                event_type="session_end",
            ),
        ],
    )

    update_catalog(layout)
    record = load_catalog(layout)["codex/legacy-exec"]

    assert record.first_prompt == "Fix the parser"
    assert record.counts["user_messages"] == 1
    assert record.counts["assistant_messages"] == 1
    assert "empty" not in record.tags


def test_git_resolution_worktrees_symlinks_and_remote_forms(tmp_path: Path):
    main = tmp_path / "actual" / "retro"
    git_dir = main / ".git"
    worktree = tmp_path / "actual" / "retro.worktrees" / "feature"
    worktree_git = git_dir / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    worktree_git.mkdir(parents=True)
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = git@github.com:SajjadGG/Retro.git\n',
        encoding="utf-8",
    )
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "refs" / "heads" / "main").write_text("a" * 40 + "\n", encoding="utf-8")
    (worktree / ".git").write_text(f"gitdir: {worktree_git}\n", encoding="utf-8")
    (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
    (worktree_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    link = tmp_path / "linked"
    os.symlink(tmp_path / "actual", link)

    resolved = resolve_cwd(str(link / "retro.worktrees" / "feature"))
    git = discover_git(resolved)

    assert resolved == str(worktree.resolve())
    assert git.root == str(main.resolve())
    assert git.worktree is True
    assert normalize_remote(git.remote_url) == "github.com/sajjadgg/retro"
    assert normalize_remote("https://github.com/SajjadGG/Retro.git") == (
        "github.com/sajjadgg/retro"
    )
    assert normalize_remote("ssh://git@github.com/SajjadGG/Retro.git") == (
        "github.com/sajjadgg/retro"
    )
    assert normalize_remote("SajjadGG/Retro") == "github.com/sajjadgg/retro"
    assert path_glob_matches(str(main) + "/**", str(main))
    assert path_glob_matches(str(main) + "/**", str(main / "src" / "retro"))


def test_project_config_aliases_paths_and_missing_claude_cwd(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(
        tmp_path,
        projects={
            "aliases": {
                "path:/Users/test/Dev/repos/Mem": "github.com/sajjadgg/retro",
                "path:/Users/test/Dev/repos/mcp-ghostlab": (
                    "github.com/sajjadgg/ghostlab"
                ),
            },
            "paths": [
                {
                    "glob": "/Users/test/Dev/repos/Mem/**",
                    "project": "path:/Users/test/Dev/repos/Mem",
                }
            ],
            "names": {"github.com/sajjadgg/retro": "Retro"},
        },
    )
    raw = layout.raw_dir("claude-code", "missing")
    raw.mkdir(parents=True)
    (raw / "import_meta.json").write_text(
        json.dumps({"project_slug": "-Users-test-Dev-repos-Mem"}),
        encoding="utf-8",
    )
    (raw / "transcript.jsonl").write_text(
        json.dumps({"type": "user", "entrypoint": "cli", "isSidechain": False}) + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "claude-code", "missing")
    hyphenated = layout.raw_dir("claude-code", "hyphenated")
    hyphenated.mkdir(parents=True)
    (hyphenated / "import_meta.json").write_text(
        json.dumps({"project_slug": "-Users-test-Dev-repos-mcp-ghostlab"}),
        encoding="utf-8",
    )
    (hyphenated / "transcript.jsonl").write_text(
        json.dumps({"type": "user", "entrypoint": "cli", "isSidechain": False}) + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "claude-code", "hyphenated")

    update_catalog(layout)
    record = load_catalog(layout)["claude-code/missing"]

    assert record.project["id"] == "github.com/sajjadgg/retro"
    assert record.project["name"] == "Retro"
    assert record.project["source"] == "config"
    assert record.cwd is None
    assert record.cwd_resolved == "/Users/test/Dev/repos/Mem"
    assert load_catalog(layout)["claude-code/hyphenated"].project["id"] == (
        "github.com/sajjadgg/ghostlab"
    )


def test_incremental_fingerprinting_noop_change_and_removal(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    _write_normalized(layout, "codex", "incremental")

    first = update_catalog(layout)
    first_fingerprint = load_catalog(layout)["codex/incremental"].fingerprint
    catalog_file = layout.root / "catalog" / "sessions.jsonl"
    first_stat = catalog_file.stat().st_mtime_ns
    second = update_catalog(layout)

    assert first.added_keys == ["codex/incremental"]
    assert second.changed_keys == []
    assert catalog_file.stat().st_mtime_ns == first_stat

    _write_normalized(
        layout,
        "codex",
        "incremental",
        [_event("codex", "incremental", 1, text="changed prompt with more bytes")],
    )
    third = update_catalog(layout)
    assert third.changed_keys == ["codex/incremental"]
    assert load_catalog(layout)["codex/incremental"].fingerprint != first_fingerprint

    layout.normalized_path("codex", "incremental").unlink()
    fourth = update_catalog(layout)
    assert fourth.removed_keys == ["codex/incremental"]
    assert len(load_catalog(layout)) == 0


def test_rule_edit_reevaluates_without_scanning_events(
    monkeypatch,
    tmp_path: Path,
):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    config = _config(tmp_path)
    _write_normalized(layout, "codex", "policy")
    update_catalog(layout)
    before = load_catalog(layout)["codex/policy"]

    def fail_scan(path: Path):
        raise AssertionError(f"unexpected event scan: {path}")

    monkeypatch.setattr(catalog_store, "_scan_events", fail_scan)
    save_config(
        RetroConfig(
            archive_root=config.archive_root,
            dashboard_dir=config.dashboard_dir,
            rules=[
                {
                    "id": "ignore-codex",
                    "description": "test",
                    "enabled": True,
                    "match": {"host": ["codex"]},
                    "tags": ["ignore"],
                }
            ],
        )
    )

    result = update_catalog(layout)
    after = load_catalog(layout)["codex/policy"]

    assert result.changed_keys == ["codex/policy"]
    assert after.fingerprint == before.fingerprint
    assert after.excluded is True
    assert after.tag_sources["ignore"] == ["rule:ignore-codex"]
    assert catalog_store.catalog_state_path(layout).is_file()


def test_annotation_changes_exclusion_without_changing_fingerprint(
    monkeypatch,
    tmp_path: Path,
):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    _write_normalized(layout, "codex", "annotation-policy")
    update_catalog(layout)
    before = load_catalog(layout)["codex/annotation-policy"]
    append_annotation(
        layout.root,
        key=before.key,
        op="tag",
        value="ignore",
    )

    def fail_scan(path: Path):
        raise AssertionError(f"unexpected event scan: {path}")

    monkeypatch.setattr(catalog_store, "_scan_events", fail_scan)
    result = update_catalog(layout, keys=[before.key])
    after = load_catalog(layout)[before.key]

    assert result.changed_keys == [before.key]
    assert after.fingerprint == before.fingerprint
    assert after.excluded is True
    assert after.tag_sources["ignore"] == ["manual"]


def test_incremental_fingerprint_tracks_git_config_input(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("TMPDIR", "/not-this-test-workspace")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = repo / ".git" / "config"
    config_path.write_text(
        '[remote "origin"]\nurl = https://github.com/example/old.git\n',
        encoding="utf-8",
    )
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    _config(tmp_path)
    raw = layout.raw_dir("codex", "git-change")
    raw.mkdir(parents=True)
    (raw / "rollout.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": str(repo)}}) + "\n",
        encoding="utf-8",
    )
    _write_normalized(layout, "codex", "git-change")

    update_catalog(layout)
    before = load_catalog(layout)["codex/git-change"]
    assert before.project["id"] == (
        "github.com/example/old"
    )
    config_path.write_text(
        '[remote "origin"]\nurl = git@github.com:example/new.git\n',
        encoding="utf-8",
    )
    result = update_catalog(layout)

    assert result.changed_keys == ["codex/git-change"]
    after = load_catalog(layout)["codex/git-change"]
    assert after.project["id"] == (
        "github.com/example/new"
    )
    assert after.fingerprint == before.fingerprint


def test_simple_yaml_block_scalars_and_scalars(tmp_path: Path):
    path = tmp_path / "workspace.yaml"
    path.write_text(
        "client_name: github/cli\n"
        "active: true\n"
        "name: |-\n"
        "  First line\n"
        "  Second: line\n"
        "quoted: 'owner/repo'\n",
        encoding="utf-8",
    )
    assert parse_simple_yaml(path) == {
        "client_name": "github/cli",
        "active": True,
        "name": "First line\nSecond: line",
        "quoted": "owner/repo",
    }
