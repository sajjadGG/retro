from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from retro.cli import app
from retro.config import config_path
from retro.schema import NormalizedEvent, RawRef, write_events
from retro.storage import Layout

runner = CliRunner()


def _archive(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    raw = layout.raw_dir("codex", "session-1")
    raw.mkdir(parents=True)
    (raw / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "originator": "codex_exec",
                    "source": "exec",
                    "cwd": str(tmp_path / "repo"),
                    "git": {"repository_url": "github.com/example/old"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_events(
        layout.normalized_path("codex", "session-1"),
        [
            NormalizedEvent(
                event_id="session-1:1",
                session_id="session-1",
                host="codex",
                sequence=1,
                actor="user",
                event_type="message",
                summary="Implement catalog filtering",
                raw_ref=RawRef(path="raw/codex/session-1/rollout.jsonl", line=1),
                timestamp="2026-10-01T12:00:00Z",
                payload={"text": "Implement catalog filtering"},
            )
        ],
    )
    return layout


def test_sessions_cli_list_show_explain_tag_and_untag(tmp_path: Path):
    layout = _archive(tmp_path)
    root = str(layout.root)

    listed = runner.invoke(
        app,
        ["sessions", "list", "--root", root, "--host", "codex", "--json"],
    )
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert [row["key"] for row in rows] == ["codex/session-1"]
    filtered = runner.invoke(
        app,
        [
            "sessions",
            "list",
            "--root",
            root,
            "--agent",
            "codex",
            "--client",
            "codex-exec",
            "--project",
            "github.com/example/old",
            "--included",
            "--automated",
            "--since",
            "2026-09-01",
            "--search",
            "catalog filtering",
            "--json",
        ],
    )
    assert filtered.exit_code == 0, filtered.output
    assert len(json.loads(filtered.output)) == 1

    tagged = runner.invoke(
        app,
        ["sessions", "tag", "codex/session-1", "ignore", "--root", root],
    )
    assert tagged.exit_code == 0, tagged.output
    explained = runner.invoke(
        app,
        ["sessions", "explain", "codex/session-1", "--root", root, "--json"],
    )
    explanation = json.loads(explained.output)
    assert explanation["excluded"] is True
    assert explanation["excluded_by_tags"] == ["ignore"]
    assert explanation["tag_sources"]["ignore"] == ["manual"]
    assert explanation["project"]["id"] == "github.com/example/old"

    untagged = runner.invoke(
        app,
        ["sessions", "untag", "codex/session-1", "ignore", "--root", root],
    )
    assert untagged.exit_code == 0, untagged.output
    shown = runner.invoke(
        app,
        ["sessions", "show", "codex", "session-1", "--root", root, "--json"],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["excluded"] is False


def test_rules_cli_add_disable_enable_remove_and_dry_run(tmp_path: Path):
    layout = _archive(tmp_path)
    root = str(layout.root)
    runner.invoke(app, ["sessions", "list", "--root", root, "--json"])
    spec = json.dumps(
        {
            "id": "codex-exec",
            "description": "headless codex",
            "enabled": True,
            "match": {"client": ["codex-exec"], "interactive": False},
            "tags": ["evaluation"],
        }
    )

    added = runner.invoke(app, ["rules", "add", spec])
    assert added.exit_code == 0, added.output
    tested = runner.invoke(
        app,
        ["rules", "test", spec, "--root", root, "--json"],
    )
    assert tested.exit_code == 0, tested.output
    assert json.loads(tested.output)[0]["matches"] == 1

    disabled = runner.invoke(app, ["rules", "disable", "codex-exec"])
    assert disabled.exit_code == 0, disabled.output
    listed = runner.invoke(app, ["rules", "list", "--json"])
    rule = next(item for item in json.loads(listed.output) if item["id"] == "codex-exec")
    assert rule["enabled"] is False

    enabled = runner.invoke(app, ["rules", "enable", "codex-exec"])
    assert enabled.exit_code == 0, enabled.output
    removed = runner.invoke(app, ["rules", "remove", "codex-exec"])
    assert removed.exit_code == 0, removed.output


def test_projects_cli_merge_rename_and_show(tmp_path: Path):
    layout = _archive(tmp_path)
    root = str(layout.root)
    runner.invoke(app, ["sessions", "list", "--root", root, "--json"])

    merged = runner.invoke(
        app,
        [
            "projects",
            "merge",
            "github.com/example/old",
            "github.com/example/canonical",
        ],
    )
    assert merged.exit_code == 0, merged.output
    renamed = runner.invoke(
        app,
        ["projects", "rename", "github.com/example/canonical", "Canonical"],
    )
    assert renamed.exit_code == 0, renamed.output
    shown = runner.invoke(
        app,
        ["projects", "show", "github.com/example/canonical", "--root", root, "--json"],
    )

    assert shown.exit_code == 0, shown.output
    payload = json.loads(shown.output)
    assert payload["project"]["name"] == "Canonical"
    assert payload["sessions"] == 1


def test_retro_config_set_preserves_catalog_and_unknown_keys(tmp_path: Path):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "archive_root": str(tmp_path / "archive"),
                "dashboard_dir": str(tmp_path / "dashboard"),
                "exclude_tags": ["ignore", "private"],
                "rules": [],
                "projects": {"aliases": {}, "paths": [], "names": {}},
                "future": {"value": 1},
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config", "set", "sync-interval", "20m"])

    assert result.exit_code == 0, result.output
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sync_interval_seconds"] == 1200
    assert raw["exclude_tags"] == ["ignore", "private"]
    assert raw["future"] == {"value": 1}
