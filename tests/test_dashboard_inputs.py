"""Readers for the catalog, signal store and sync reports."""
from __future__ import annotations

import json

import pytest

from retro.dashboard.inputs import (
    mark_stale_signals,
    parse_iso,
    read_catalog,
    read_signals,
    read_sync_runs,
)
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive  # noqa: F401


def test_read_catalog_decodes_every_contract_field(archive: Layout):  # noqa: F811
    catalog = read_catalog(archive)
    assert not catalog.missing
    assert catalog.malformed == 0
    assert len(catalog.records) == 8

    record = catalog.by_key()["codex/019d0001-0000-7000-8000-000000000001"]
    assert record.agent == "codex"
    assert record.client == "codex-cli"
    assert record.interactive is True
    assert record.project.id == "github.com/sajjadgg/retro"
    assert record.project.source == "git-remote"
    assert record.git["branch"] == "main"
    assert record.counts["tool_calls"] == 120
    assert record.markers[0]["directive"] == "keep"
    assert record.tag_sources["keep"] == ["marker"]
    assert record.fingerprint == "fp-codex-1"
    assert record.started_epoch == pytest.approx(1791450000.0)


def test_read_catalog_handles_null_fields(archive: Layout):  # noqa: F811
    record = read_catalog(archive).by_key()["claude-code/2f7f1a10-4c3a-4a0e-9d61-aaaa00000007"]
    assert record.title is None
    assert record.started_at is None
    assert record.started_epoch is None
    assert record.excluded is True
    assert record.project.id == "unknown"


def test_missing_inputs_are_reported(empty_archive: Layout):  # noqa: F811
    catalog = read_catalog(empty_archive)
    signals = read_signals(empty_archive)
    assert catalog.missing and not catalog.records
    assert signals.missing_definitions and signals.missing_sessions
    assert read_sync_runs(empty_archive).latest is None


def test_read_signals_decodes_definitions_and_records(archive: Layout):  # noqa: F811
    signals = read_signals(archive)
    assert signals.version == "sig-v2-0001"
    assert len(signals.definitions) == 15
    definition = signals.definition("outcome.status")
    assert definition is not None
    assert definition.kind == "category"
    assert definition.categories[0] == "success"

    record = signals.records["codex/019d0001-0000-7000-8000-000000000001"]
    assert record.values["usage.cost_usd"] == pytest.approx(1.42)
    assert record.evidence["outcome.status"] == ["ev-401", "ev-402"]
    assert record.confidence["outcome.status"] == pytest.approx(0.72)


def test_stale_signals_are_flagged(archive: Layout):  # noqa: F811
    catalog = read_catalog(archive)
    signals = read_signals(archive)
    mark_stale_signals(catalog, signals)
    assert signals.stale == ["codex/019d0006-0000-7000-8000-000000000006"]


def test_read_sync_runs_orders_newest_first(archive: Layout):  # noqa: F811
    sync = read_sync_runs(archive)
    assert sync.latest["status"] == "failed"
    assert [entry["status"] for entry in sync.history] == ["failed", "ok"]


def test_unusable_records_are_skipped_and_counted(archive: Layout):  # noqa: F811
    path = archive.root / "signals" / "sessions.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "{oops\n{}\n", encoding="utf-8")
    signals = read_signals(archive)
    assert len(signals.records) == 5
    assert signals.malformed == 1


def test_parse_iso_accepts_contract_formats():
    assert parse_iso("2026-10-08T09:00:00Z") == pytest.approx(1791450000.0)
    assert parse_iso("2026-10-08T09:00:00+00:00") == pytest.approx(1791450000.0)
    assert parse_iso("2026-10-08") == pytest.approx(1791417600.0)
    assert parse_iso("") is None
    assert parse_iso(None) is None
    assert parse_iso("not a date") is None


def test_reader_tolerates_wrong_types(archive: Layout):  # noqa: F811
    path = archive.root / "catalog" / "sessions.jsonl"
    path.write_text(
        json.dumps(
            {
                "key": "codex/x",
                "host": "codex",
                "session_id": "x",
                "tags": "not-a-list",
                "counts": {"events": "12"},
                "models": [1, "gpt"],
                "git": "nope",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record = read_catalog(archive).records[0]
    assert record.tags == []
    assert record.counts == {"events": 12}
    assert record.models == ["gpt"]
    assert record.git == {}
