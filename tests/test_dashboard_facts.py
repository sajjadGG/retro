"""Fact-table construction, signal matrix encoding and quality aggregates."""
from __future__ import annotations

import json

import pytest

from retro.dashboard.build import load_inputs
from retro.dashboard.facts import (
    COLUMN_INDEX,
    COLUMNS,
    FLAGS,
    _bucket_count,
    detail_payload,
    excluded_detail_payload,
)
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive, inputs  # noqa: F401

KEY_MAIN = "codex/019d0001-0000-7000-8000-000000000001"
KEY_SUB = "codex/019d0002-0000-7000-8000-000000000002"
KEY_EVAL = "vscode-copilot/f15d510d-e6aa-4a6b-a674-de5d65900004"
KEY_EMPTY = "claude-code/2f7f1a10-4c3a-4a0e-9d61-aaaa00000007"


def column(row, name):
    return row[COLUMN_INDEX[name]]


def has_flag(row, name):
    return bool(row[COLUMN_INDEX["flags"]] & FLAGS[name])


def test_rows_are_included_first_then_newest_first(inputs):  # noqa: F811
    model = inputs.model
    assert len(model.rows) == 8
    assert model.included_count == 6
    assert all(not has_flag(row, "excluded") for row in model.included_rows)
    assert all(has_flag(row, "excluded") for row in model.excluded_rows)
    for block in (model.included_rows, model.excluded_rows):
        started = [column(row, "started") for row in block if column(row, "started")]
        assert started == sorted(started, reverse=True)
    assert column(model.rows[-1], "started") is None


def test_dictionary_split_covers_included_rows(inputs):  # noqa: F811
    model = inputs.model
    for name, accessor in (
        ("projects", "project"),
        ("agents", "agent"),
        ("clients", "client"),
    ):
        split = model.dictionary_split[name]
        for row in model.included_rows:
            assert column(row, accessor) < split
    tag_split = model.dictionary_split["tags"]
    for row in model.included_rows:
        assert all(index < tag_split for index in column(row, "tags"))
    excluded_only = [
        index
        for row in model.excluded_rows
        for index in [column(row, "project")]
        if index >= model.dictionary_split["projects"]
    ]
    assert excluded_only, "the fixture has an excluded-only project"


def test_excluded_rows_drop_the_prompt_excerpt(inputs):  # noqa: F811
    for row in inputs.model.excluded_rows:
        assert column(row, "prompt") == ""
    assert any(column(row, "prompt") for row in inputs.model.included_rows)


def test_cost_basis_column(inputs):  # noqa: F811
    model = inputs.model
    assert model.cost_basis[:3] == ["exact", "family", "mixed"]
    main = model.row(KEY_MAIN)
    assert model.cost_basis[column(main, "basis")] == "exact"
    unscored = model.row("codex/019d0008-0000-7000-8000-000000000008")
    assert column(unscored, "basis") == -1


def test_quality_reports_cost_coverage(inputs):  # noqa: F811
    cost = inputs.model.quality["cost"]
    assert cost["priced"] == 4
    assert cost["unpriced"] == 2
    basis = {entry["basis"]: entry for entry in cost["basis"]}
    assert basis["none"]["count"] == 1
    assert basis["unscored"]["count"] == 1
    assert basis["none"]["examples"] == ["codex/019d0006-0000-7000-8000-000000000006"]


def test_row_columns_merge_catalog_and_signals(inputs):  # noqa: F811
    row = inputs.model.row(KEY_MAIN)
    assert len(row) == len(COLUMNS)
    assert column(row, "title") == "Fix dashboard shard writer"
    assert column(row, "cost") == 1_420_000
    assert column(row, "tokens") == 184000
    assert column(row, "turns") == 9
    assert column(row, "tools") == 120
    assert column(row, "edits") == 14
    assert column(row, "events") == 420
    assert column(row, "unknown") == 4
    assert column(row, "errors") == 80
    assert inputs.model.dictionaries.outcomes[column(row, "outcome")] == "success"
    assert inputs.model.dictionaries.work[column(row, "work")] == "build"


def test_counts_fall_back_when_signals_are_absent(inputs):  # noqa: F811
    row = inputs.model.row("codex/019d0008-0000-7000-8000-000000000008")
    assert column(row, "turns") == 3
    assert column(row, "tools") == 2
    assert column(row, "cost") is None
    assert column(row, "outcome") == -1
    assert not column(row, "flags") & FLAGS["signals"]


def test_flags_encode_catalog_and_signal_facts(inputs):  # noqa: F811
    main = column(inputs.model.row(KEY_MAIN), "flags")
    assert main & FLAGS["interactive"]
    assert main & FLAGS["committed"]
    assert main & FLAGS["signals"]
    assert main & FLAGS["transcript"]
    assert main & FLAGS["parent"]
    assert not main & FLAGS["excluded"]

    sub = column(inputs.model.row(KEY_SUB), "flags")
    assert sub & FLAGS["subagent"] and sub & FLAGS["child"] and sub & FLAGS["automated"]
    assert sub & FLAGS["worktree"]
    assert not sub & FLAGS["transcript"]

    evaluation = column(inputs.model.row(KEY_EVAL), "flags")
    assert evaluation & FLAGS["excluded"] and evaluation & FLAGS["automated"]


def test_parent_and_children_links(inputs):  # noqa: F811
    assert column(inputs.model.row(KEY_SUB), "parent") == KEY_MAIN
    assert inputs.model.children[KEY_MAIN] == [KEY_SUB]
    assert column(inputs.model.row(KEY_MAIN), "parent") is None


def test_project_dictionary_is_consolidated(inputs):  # noqa: F811
    projects = inputs.model.dictionaries.projects
    ids = [entry["id"] for entry in projects]
    assert ids.count("github.com/sajjadgg/retro") == 1
    entry = projects[ids.index("github.com/sajjadgg/retro")]
    assert entry["name"] == "retro"
    assert entry["remote"] == "github.com/sajjadgg/retro"
    worktree_row = inputs.model.row("vscode-copilot/aa2d510d-e6aa-4a6b-a674-de5d65900005")
    clone_row = inputs.model.row("codex/019d0006-0000-7000-8000-000000000006")
    expected = ids.index("github.com/sajjadgg/retro")
    assert column(worktree_row, "project") == expected
    assert column(clone_row, "project") == expected


def test_signal_matrix_encodes_by_kind(inputs):  # noqa: F811
    model = inputs.model
    position = model.index[KEY_MAIN]
    encoded = model.matrix[position]
    assert encoded["usage.cost_usd"] == pytest.approx(1.42)
    assert encoded["outcome.committed"] == 1
    assert encoded["risk.secret_exposure"] == 0
    categories = model.encodings["outcome.status"].categories
    assert categories[encoded["outcome.status"]] == "success"
    assert model.encodings["outcome.status"].encoding == "cat"
    assert model.encodings["usage.cost_usd"].encoding == "num"
    assert model.encodings["outcome.committed"].encoding == "bool"


def test_signal_summaries_cover_every_kind(inputs):  # noqa: F811
    summaries = inputs.model.summaries
    cost = summaries["usage.cost_usd"]
    assert cost["n"] == 4
    assert cost["sum"] == pytest.approx(7.14)
    assert cost["max"] == pytest.approx(4.87)
    assert summaries["outcome.committed"]["true_rate"] == pytest.approx(0.5)
    assert summaries["outcome.status"]["counts"]["success"] == 2
    assert summaries["activity.files_touched"]["n"] == 0


def test_quality_explains_exclusions_and_capture(inputs):  # noqa: F811
    quality = inputs.model.quality
    assert quality["sessions"] == 8
    assert quality["included"] == 6
    assert quality["excluded"] == 2
    assert quality["with_signals"] == 5
    assert quality["stale_signals"] == 1
    assert quality["missing_signals"] == 1
    assert quality["interactive"] == 5
    assert quality["automated"] == 3
    assert quality["unknown_rate"] == pytest.approx(93 / 1986, rel=1e-3)

    by_tag = {entry["tag"]: entry for entry in quality["excluded_by_tag"]}
    assert by_tag["ignore"]["count"] == 2
    assert by_tag["scratch"]["sources"] == {"rule:builtin.temp-dir": 1}
    assert KEY_EVAL in by_tag["ignore"]["examples"]
    hosts = {entry["host"]: entry for entry in quality["hosts"]}
    assert hosts["codex"]["sessions"] == 4
    assert hosts["codex"]["missing_transcript"] == 3
    assert quality["markers"] == {"keep": 1}


def test_sync_summary_is_compact(inputs):  # noqa: F811
    latest = inputs.model.sync["latest"]
    assert latest["status"] == "failed"
    assert latest["imported"] == 1
    assert latest["failure_count"] == 1
    assert latest["failures"][0]["error"] == "transcript vanished"
    assert len(inputs.model.sync["history"]) == 2


def test_detail_payload_carries_evidence_and_paths(inputs):  # noqa: F811
    payload = detail_payload(inputs.model, KEY_MAIN)
    assert payload["project"]["id"] == "github.com/sajjadgg/retro"
    assert payload["tag_sources"] == {"keep": ["marker"]}
    assert payload["children"] == [KEY_SUB]
    assert payload["signals"]["values"]["usage.cost_usd"] == pytest.approx(1.42)
    assert payload["signals"]["evidence"]["outcome.status"] == ["ev-401", "ev-402"]
    assert payload["paths"]["transcript"] == "rendered/codex/019d0001-0000-7000-8000-000000000001.md"
    assert payload["paths"]["raw"] == "raw/codex/019d0001-0000-7000-8000-000000000001"


def test_detail_payload_of_unscored_session_has_no_signals(inputs):  # noqa: F811
    payload = detail_payload(inputs.model, KEY_EMPTY)
    assert "signals" not in payload
    assert payload["excluded"] is True


def test_excluded_detail_payload_only_adds_what_rows_lack(inputs):  # noqa: F811
    payload = excluded_detail_payload(inputs.model, KEY_EVAL)
    assert payload["minimal"] is True
    assert payload["counts"]["events"] == 6
    assert payload["tag_sources"]["ignore"] == ["rule:builtin.temp-dir"]
    assert payload["cwd"] == "/private/var/folders/kw/T/tmpuxz1xexv"
    for duplicated in ("title", "agent", "client", "project", "paths", "models", "tags", "prompt"):
        assert duplicated not in payload


def test_bucket_count_grows_in_powers_of_two():
    assert _bucket_count(0) == 1
    assert _bucket_count(64) == 1
    assert _bucket_count(65) == 2
    assert _bucket_count(2000) == 32
    assert _bucket_count(35000) == 1024
    assert _bucket_count(31169, target=320) == 128


def test_buckets_are_stable_when_sessions_are_added(inputs, archive: Layout):  # noqa: F811
    before = {column(row, "key"): column(row, "bucket") for row in inputs.model.rows}
    path = archive.root / "catalog" / "sessions.jsonl"
    extra = path.read_text(encoding="utf-8").splitlines()[0].replace("019d0001", "019d9999")
    path.write_text(path.read_text(encoding="utf-8") + extra + "\n", encoding="utf-8")
    after_model = load_inputs(archive).model
    after = {column(row, "key"): column(row, "bucket") for row in after_model.rows}
    assert after_model.bucket_count == inputs.model.bucket_count
    for key, bucket in before.items():
        assert after[key] == bucket


def test_empty_archive_builds_an_empty_model(empty_archive: Layout):  # noqa: F811
    model = load_inputs(empty_archive).model
    assert model.rows == []
    assert model.quality["sessions"] == 0
    assert model.sync["latest"] is None


def test_sync_summary_counts_lists_or_integers(archive: Layout):  # noqa: F811
    path = archive.root / "sync" / "runs" / "20261010T030000.json"
    path.write_text(
        json.dumps(
            {
                "started_at": "2026-10-10T03:00:00+00:00",
                "completed_at": None,
                "status": "running",
                "unchanged": 31169,
                "imported": ["codex/a", "codex/b"],
                "rendered": 4,
                "failures": [],
                "stages": {
                    "capture": {"status": "success", "duration_seconds": 44.1, "changed_keys": 111},
                    "catalog": {"status": "success", "total": 31901, "excluded": 31169},
                    "dashboard": {"status": "running"},
                },
            }
        ),
        encoding="utf-8",
    )
    model = load_inputs(archive).model
    latest = model.sync["latest"]
    assert latest["status"] == "running"
    assert latest["unchanged"] == 31169
    assert latest["imported"] == 2
    assert latest["rendered"] == 4
    assert latest["health"] == "ok"
    assert latest["built_during_sync"] is True
    assert latest["stages_unfinished"] == ["dashboard"]
    assert latest["stages_ok"] == ["capture", "catalog"]
    assert latest["observed_at"] == "2026-10-10T03:00:00+00:00"
    assert latest["stages"]["catalog"]["excluded"] == 31169


def test_sync_summary_reports_failed_stages(archive: Layout):  # noqa: F811
    path = archive.root / "sync" / "runs" / "20261010T040000.json"
    path.write_text(
        json.dumps(
            {
                "started_at": "2026-10-10T04:00:00+00:00",
                "status": "running",
                "unchanged": 0,
                "failures": [],
                "stages": {
                    "capture": {"status": "success"},
                    "signals": {"status": "failed", "error": "boom"},
                },
            }
        ),
        encoding="utf-8",
    )
    latest = load_inputs(archive).model.sync["latest"]
    assert latest["health"] == "failed"
    assert latest["built_during_sync"] is True
    assert latest["stages_failed"] == ["signals"]
    assert latest["stages"]["signals"]["error"] == "boom"


def test_sync_state_file_supplies_stages(archive: Layout):  # noqa: F811
    state = archive.root / "state" / "sync.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "updated_at": "2026-10-11T00:10:00+00:00",
                "last_run": {
                    "started_at": "2026-10-11T00:00:00+00:00",
                    "completed_at": "2026-10-11T00:09:00+00:00",
                    "status": "success",
                },
                "stages": {
                    "capture": {"status": "success", "duration_seconds": 12.0},
                    "dashboard": {"status": "success"},
                },
            }
        ),
        encoding="utf-8",
    )
    sync = load_inputs(archive).model.sync
    assert sync["state_path"].endswith("state/sync.json")
    latest = sync["latest"]
    assert latest["health"] == "ok"
    assert latest["built_during_sync"] is False
    assert latest["started_at"] == "2026-10-11T00:00:00+00:00"
    assert sorted(latest["stages"]) == ["capture", "dashboard"]
    # the newest run report is still the one on disk for history
    assert len(sync["history"]) == 2
