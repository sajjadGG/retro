from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from retro.archive import (
    create_migration_plan,
    cutover_compatibility_link,
    execute_migration,
    rebuild_derived_artifacts,
    repair_archive_refs,
    sha256_file,
    verify_migration,
)
from retro.locking import LockUnavailableError, archive_lock
from retro.storage import Layout


def _source_archive(tmp_path: Path) -> Path:
    root = tmp_path / "source" / "rollout-memory"
    raw = root / "raw" / "codex" / "session-1" / "rollout.jsonl"
    raw.parent.mkdir(parents=True)
    raw.write_text('{"type":"session_meta","payload":{}}\n', encoding="utf-8")
    normalized = root / "normalized" / "codex" / "session-1.events.jsonl"
    normalized.parent.mkdir(parents=True)
    normalized.write_text(
        json.dumps(
            {
                "event_id": "event-1",
                "session_id": "session-1",
                "host": "codex",
                "sequence": 1,
                "actor": "system",
                "event_type": "session_start",
                "summary": "start",
                "raw_ref": {"path": str(raw), "line": 1},
                "timestamp": None,
                "parent_event_id": None,
                "payload": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    signals = root / "signals" / "readings.jsonl"
    signals.parent.mkdir(parents=True)
    signals.write_text('{"stale":true}\n', encoding="utf-8")
    memory_db = root / "memories" / "index.sqlite"
    memory_db.parent.mkdir(parents=True)
    con = sqlite3.connect(memory_db)
    try:
        con.execute("CREATE TABLE sample(value TEXT)")
        con.execute("INSERT INTO sample VALUES ('preserved')")
        con.commit()
    finally:
        con.close()
    return root


def test_migration_copies_raw_and_rebases_normalized_refs(tmp_path: Path):
    source = _source_archive(tmp_path)
    destination = tmp_path / "canonical"

    plan_path = create_migration_plan([source], destination)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))

    assert plan["status"] == "planned"
    assert plan["summary"]["actions"]["rewrite-normalized"] == 1
    assert source.exists()
    execute_migration(plan_path)

    source_raw = source / "raw" / "codex" / "session-1" / "rollout.jsonl"
    target_raw = destination / "raw" / "codex" / "session-1" / "rollout.jsonl"
    assert sha256_file(target_raw) == sha256_file(source_raw)
    event = json.loads(
        (
            destination / "normalized" / "codex" / "session-1.events.jsonl"
        ).read_text(encoding="utf-8")
    )
    assert event["raw_ref"]["path"] == "raw/codex/session-1/rollout.jsonl"
    assert not (destination / "signals" / "readings.jsonl").exists()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    memory_entry = next(
        entry
        for entry in plan["entries"]
        if entry["relative_path"] == "memories/index.sqlite"
    )
    assert memory_entry["backup_method"] == "sqlite-online-backup"
    backup = Path(memory_entry["target"])
    con = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT value FROM sample").fetchone()[0] == "preserved"
    finally:
        con.close()
    rendered = destination / "rendered" / "codex" / "session-1.md"
    rendered.parent.mkdir(parents=True)
    rendered.write_text("# session\n", encoding="utf-8")

    report = verify_migration(plan_path)
    assert report["ok"] is True
    assert report["session_counts"] == {"codex": 1}
    assert report["normalized_events"] == 1


def test_migration_refuses_conflicting_existing_target(tmp_path: Path):
    source = _source_archive(tmp_path)
    destination = tmp_path / "canonical"
    plan_path = create_migration_plan([source], destination)
    target = destination / "raw" / "codex" / "session-1" / "rollout.jsonl"
    target.parent.mkdir(parents=True)
    target.write_text('{"different":true}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        execute_migration(plan_path)

    assert target.read_text(encoding="utf-8") == '{"different":true}\n'
    conflicts = list((plan_path.parent / "conflicts").rglob("rollout.jsonl"))
    assert len(conflicts) == 1


def test_cutover_requires_verification_and_preserves_source(tmp_path: Path):
    source = _source_archive(tmp_path)
    destination = tmp_path / "canonical"
    plan_path = create_migration_plan([source], destination)
    execute_migration(plan_path)

    with pytest.raises(RuntimeError, match="pass verification"):
        cutover_compatibility_link(plan_path, source)

    dashboard = tmp_path / "dashboard"
    rebuild_derived_artifacts(plan_path, dashboard_dir=dashboard)
    assert (dashboard / "index.html").is_symlink()
    assert (dashboard / "data").is_symlink()
    assert verify_migration(plan_path)["ok"] is True
    link = cutover_compatibility_link(plan_path, source)

    assert link.is_symlink()
    assert link.resolve() == destination.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    backup = Path(plan["cutover"]["source_backup"])
    assert backup.is_dir()
    assert (backup / "raw" / "codex" / "session-1" / "rollout.jsonl").exists()


def test_repair_refs_is_atomic_dry_run_and_idempotent(tmp_path: Path):
    layout = Layout(tmp_path / "relocated")
    path = layout.normalized_path("vscode-copilot", "session-1")
    path.parent.mkdir(parents=True)
    old_root = "/Users/old/Library/Application Support/retro/rollout-memory"
    original = {
        "event_id": "event-1",
        "session_id": "session-1",
        "host": "vscode-copilot",
        "sequence": 1,
        "actor": "assistant",
        "event_type": "tool_call",
        "summary": "read",
        "raw_ref": {
            "path": f"{old_root}/raw/vscode-copilot/session-1/events.jsonl",
            "line": 4,
        },
        "timestamp": None,
        "parent_event_id": None,
        "payload": {
            "name": "read",
            "execution_raw_ref": {
                "path": (
                    "/another/archive/raw/vscode-copilot/"
                    "session-1/events.jsonl"
                ),
                "line": 7,
            },
            "external_path": "/Users/old/project/file.py",
        },
    }
    path.write_text(json.dumps(original) + "\n", encoding="utf-8")
    before = path.read_bytes()

    dry_run = repair_archive_refs(layout, dry_run=True)

    assert dry_run["references_changed"] == 2
    assert path.read_bytes() == before

    repaired = repair_archive_refs(layout)
    event = json.loads(path.read_text(encoding="utf-8"))

    assert repaired["ok"] is True
    assert repaired["files_changed"] == 1
    assert repaired["references_changed"] == 2
    assert event["raw_ref"]["path"] == (
        "raw/vscode-copilot/session-1/events.jsonl"
    )
    assert event["payload"]["execution_raw_ref"]["path"] == (
        "raw/vscode-copilot/session-1/events.jsonl"
    )
    assert event["payload"]["external_path"] == "/Users/old/project/file.py"
    assert event["event_id"] == original["event_id"]
    assert repair_archive_refs(layout)["references_changed"] == 0


def test_archive_lock_is_shared_across_user_data_dirs(
    monkeypatch,
    tmp_path: Path,
):
    layout = Layout(tmp_path / "archive")
    first_lock = layout.lock_path()

    with archive_lock(layout):
        monkeypatch.setenv("RETRO_DATA_DIR", str(tmp_path / "other-user-data"))
        second_layout = Layout(layout.root)
        assert second_layout.lock_path() == first_lock
        with pytest.raises(LockUnavailableError):
            with archive_lock(second_layout):
                pass

    assert first_lock.is_file()
