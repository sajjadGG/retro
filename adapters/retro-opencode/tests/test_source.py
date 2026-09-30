from __future__ import annotations

import shutil
from importlib import metadata
from pathlib import Path

from retro.cli import app
from retro.sdk import read_events
from retro.storage import Layout
from typer.testing import CliRunner

from retro_opencode import OpenCodeSource

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "opencode-home"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    shutil.copy2(
        FIXTURES / "opencode_session.jsonl",
        sessions / "oc-session-001.jsonl",
    )
    return home


def test_discover_and_import_preserves_unknown_records(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    source = OpenCodeSource(layout, opencode_home=_home(tmp_path))

    descriptors = source.discover()
    result = source.import_session(identifier="oc-session-001")
    events = list(read_events(result.normalized_path))

    assert [descriptor.session_id for descriptor in descriptors] == ["oc-session-001"]
    assert descriptors[0].metadata["raw_filename"] == "session.jsonl"
    assert result.event_count == 6
    assert result.unknown_event_count == 2
    assert [event.summary for event in events[-2:]] == [
        "unknown type=future_record",
        "malformed_json",
    ]
    assert all(
        event.raw_ref.path == "raw/opencode/oc-session-001/session.jsonl"
        for event in events
    )


def test_installed_entry_point_routes_cli(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENCODE_HOME", str(_home(tmp_path)))
    root = tmp_path / "archive"

    result = runner.invoke(
        app,
        [
            "import",
            "opencode",
            "--latest",
            "--no-render",
            "--root",
            str(root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "captured opencode/oc-session-001" in result.output
    assert (root / "normalized/opencode/oc-session-001.events.jsonl").is_file()


def test_distribution_registers_source_entry_point():
    entry_points = metadata.entry_points()
    if hasattr(entry_points, "select"):
        sources = list(entry_points.select(group="retro.sources"))
    else:
        sources = list(entry_points.get("retro.sources", ()))

    assert any(entry.name == "opencode" for entry in sources)
