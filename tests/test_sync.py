from __future__ import annotations

import json
import os
import time
from pathlib import Path
from shutil import _ntuple_diskusage

from retro.importers.copilot_cli import CopilotCliSession
from retro.storage import Layout
from retro.sync import _preserve_rewritten_raw, run_sync


class _EmptyImporter:
    host = "codex"

    def __init__(self, layout):
        self.layout = layout

    def discover(self):
        return []


def test_noop_sync_does_not_rebuild_dashboard(monkeypatch, tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text("existing", encoding="utf-8")
    monkeypatch.setattr("retro.sync.ClaudeImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CodexImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CopilotImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.run_signals", lambda layout: [])

    def write_signals(layout, readings):
        path = layout.root / "signals" / "readings.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return {"readings": path}

    def reindex(layout):
        path = layout.memory_index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"index")

    monkeypatch.setattr("retro.sync.write_signal_artifacts", write_signals)
    monkeypatch.setattr("retro.sync.reindex_memory", reindex)
    rebuilt = []
    monkeypatch.setattr(
        "retro.sync._rebuild_dashboard_atomically",
        lambda layout, output: rebuilt.append(output),
    )

    first = run_sync(layout, dashboard_dir=dashboard, force_derived=True)
    assert first.status == "success"
    assert first.dashboard_rebuilt is True
    assert len(rebuilt) == 1

    second = run_sync(layout, dashboard_dir=dashboard)
    assert second.status == "success"
    assert second.imported == []
    assert second.dashboard_rebuilt is False
    assert len(rebuilt) == 1


def test_rewritten_raw_capture_creates_revision(tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    raw_dir = layout.raw_dir("vscode-copilot", "session-1")
    raw_dir.mkdir(parents=True)
    captured = raw_dir / "events.jsonl"
    captured.write_text('{"old":true}\n', encoding="utf-8")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source = source_dir / "events.jsonl"
    source.write_text('{"rewritten":true}\n', encoding="utf-8")
    changed = time.time() + 10
    os.utime(source, (changed, changed))
    session = CopilotCliSession(
        session_id="session-1",
        state_dir=source_dir,
        events_path=source,
        session_store_db=None,
        cwd="",
        repository="",
        branch="",
        title="test",
        models=(),
        request_count=0,
        size_bytes=source.stat().st_size,
        mtime=source.stat().st_mtime,
        created_at=None,
        updated_at=None,
        active=True,
        workspace_name="test",
    )

    revision = _preserve_rewritten_raw(
        layout,
        "vscode-copilot",
        session,
        raw_dir,
    )

    assert revision is not None
    assert (revision / "events.jsonl").read_text(encoding="utf-8") == '{"old":true}\n'


def test_sync_report_is_structured(monkeypatch, tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    dashboard = tmp_path / "dashboard"
    monkeypatch.setattr("retro.sync.ClaudeImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CodexImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CopilotImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.run_signals", lambda layout: [])
    monkeypatch.setattr("retro.sync.write_signal_artifacts", lambda layout, readings: {})
    monkeypatch.setattr("retro.sync.reindex_memory", lambda layout: None)
    monkeypatch.setattr("retro.sync._rebuild_dashboard_atomically", lambda layout, output: None)

    report = run_sync(layout, dashboard_dir=dashboard, force_derived=True)

    reports = list((layout.root / "sync" / "runs").glob("*.json"))
    assert len(reports) == 1
    saved = json.loads(reports[0].read_text(encoding="utf-8"))
    assert saved["status"] == report.status == "success"
    assert saved["archive_root"] == str(layout.root)


def test_low_space_only_blocks_scheduled_sync(monkeypatch, tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    dashboard = tmp_path / "dashboard"
    monkeypatch.setattr("retro.sync.ClaudeImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CodexImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CopilotImporter", _EmptyImporter)
    monkeypatch.setattr(
        "retro.sync.shutil.disk_usage",
        lambda path: _ntuple_diskusage(10 * 1024**3, 9 * 1024**3, 1024**3),
    )
    monkeypatch.setattr("retro.sync.run_signals", lambda layout: [])
    monkeypatch.setattr("retro.sync.write_signal_artifacts", lambda layout, readings: {})
    monkeypatch.setattr("retro.sync.reindex_memory", lambda layout: None)
    monkeypatch.setattr("retro.sync._rebuild_dashboard_atomically", lambda layout, output: None)

    manual = run_sync(
        layout,
        dashboard_dir=dashboard,
        scheduled=False,
        force_derived=True,
    )
    scheduled = run_sync(
        layout,
        dashboard_dir=dashboard,
        scheduled=True,
        force_derived=True,
    )

    assert manual.status == "success"
    assert manual.warning is not None
    assert scheduled.status == "failed"
    assert "requires 2 GiB free" in scheduled.failures[0]["error"]


def test_signal_schema_change_rebuilds_dashboard(monkeypatch, tmp_path: Path):
    layout = Layout(tmp_path / "archive")
    layout.ensure()
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (dashboard / "index.html").write_text("existing", encoding="utf-8")
    (layout.root / "signals").mkdir()
    (layout.root / "signals" / "readings.jsonl").write_text("", encoding="utf-8")
    state = Path(os.environ["RETRO_DATA_DIR"]) / "state" / "last-sync.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps(
            {
                "signal_signature": "old",
                "memory_signature": "",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("retro.sync.ClaudeImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CodexImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.CopilotImporter", _EmptyImporter)
    monkeypatch.setattr("retro.sync.run_signals", lambda layout: [])
    monkeypatch.setattr("retro.sync.write_signal_artifacts", lambda layout, readings: {})
    monkeypatch.setattr("retro.sync.reindex_memory", lambda layout: None)
    monkeypatch.setattr("retro.sync._memory_source_signature", lambda layout: "")
    rebuilt = []
    monkeypatch.setattr(
        "retro.sync._rebuild_dashboard_atomically",
        lambda layout, output: rebuilt.append(output),
    )

    report = run_sync(layout, dashboard_dir=dashboard)

    assert report.signals_recomputed is True
    assert report.dashboard_rebuilt is True
    assert rebuilt == [dashboard.resolve()]
