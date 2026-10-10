"""Aggregation helpers shared by the terminal view (mirrored by the browser)."""
from __future__ import annotations

import pytest

from retro.dashboard.metrics import (
    Filters,
    cost_basis,
    cost_usd,
    error_rate,
    filter_rows,
    has_flag,
    kpis,
    project_rollup,
    success_rate,
    trend,
    value,
)
from retro.storage import Layout
from tests.dashboard_helpers import archive, inputs  # noqa: F401

KEY_MAIN = "codex/019d0001-0000-7000-8000-000000000001"


def test_excluded_sessions_are_hidden_by_default(inputs):  # noqa: F811
    model = inputs.model
    visible = filter_rows(model.rows, Filters(), model.dictionaries)
    assert len(visible) == 6
    everything = filter_rows(model.rows, Filters(include_excluded=True), model.dictionaries)
    assert len(everything) == 8


def test_filters_combine_with_and_semantics(inputs):  # noqa: F811
    model = inputs.model
    rows = filter_rows(
        model.rows,
        Filters(agents={"codex"}, projects={"github.com/sajjadgg/retro"}),
        model.dictionaries,
    )
    assert {value(row, "key") for row in rows} == {
        KEY_MAIN,
        "codex/019d0002-0000-7000-8000-000000000002",
        "codex/019d0006-0000-7000-8000-000000000006",
    }
    assert filter_rows(model.rows, Filters(agents={"nope"}), model.dictionaries) == []


def test_query_matches_title_and_first_prompt(inputs):  # noqa: F811
    model = inputs.model
    assert len(filter_rows(model.rows, Filters(query="shard writer"), model.dictionaries)) == 1
    assert len(filter_rows(model.rows, Filters(query="release notes"), model.dictionaries)) == 1
    assert filter_rows(model.rows, Filters(query="zzz"), model.dictionaries) == []


def test_interactive_filter_separates_automation(inputs):  # noqa: F811
    model = inputs.model
    automated = filter_rows(model.rows, Filters(interactive=False, include_excluded=True), model.dictionaries)
    assert {value(row, "key") for row in automated} == {
        "codex/019d0002-0000-7000-8000-000000000002",
        "vscode-copilot/f15d510d-e6aa-4a6b-a674-de5d65900004",
        "claude-code/2f7f1a10-4c3a-4a0e-9d61-aaaa00000007",
    }
    interactive = filter_rows(model.rows, Filters(interactive=True), model.dictionaries)
    assert len(interactive) == 5


def test_date_window_filters_on_start(inputs):  # noqa: F811
    model = inputs.model
    rows = filter_rows(model.rows, Filters(since=1791417600.0), model.dictionaries)
    assert {value(row, "key") for row in rows} == {KEY_MAIN, "codex/019d0002-0000-7000-8000-000000000002"}


def test_kpis_sum_costs_and_mixes(inputs):  # noqa: F811
    model = inputs.model
    stats = kpis(filter_rows(model.rows, Filters(), model.dictionaries), model.dictionaries)
    assert stats.sessions == 6
    assert stats.cost_usd == pytest.approx(7.14)
    assert stats.sessions_with_cost == 4
    assert stats.unpriced == 2
    assert stats.to_dict()["cost_per_priced_session"] == pytest.approx(7.14 / 4)
    assert stats.tokens == 1_203_000
    assert stats.outcomes["success"] == 2
    assert stats.outcomes["unknown"] == 2
    assert stats.work["build"] == 2
    assert stats.agents["codex"] == 4
    assert len(stats.days) == 5
    assert success_rate(stats) == pytest.approx(0.5)


def test_project_rollup_groups_by_consolidated_id(inputs):  # noqa: F811
    model = inputs.model
    rollup = project_rollup(filter_rows(model.rows, Filters(), model.dictionaries), model.dictionaries)
    by_id = {entry["id"]: entry for entry in rollup}
    assert set(by_id) == {"github.com/sajjadgg/retro", "github.com/sajjadgg/cortex"}
    retro = by_id["github.com/sajjadgg/retro"]
    assert retro["sessions"] == 4
    assert retro["agents"] == {"codex": 3, "copilot": 1}
    assert retro["clients"]["codex-cli"] == 1
    cortex = by_id["github.com/sajjadgg/cortex"]
    assert cortex["cost_usd"] == pytest.approx(4.87)
    assert rollup[0]["id"] == "github.com/sajjadgg/cortex"


def test_trend_compares_periods(inputs):  # noqa: F811
    model = inputs.model
    now = kpis(filter_rows(model.rows, Filters(since=1791417600.0), model.dictionaries), model.dictionaries)
    before = kpis(
        filter_rows(model.rows, Filters(since=1791331200.0, until=1791417600.0), model.dictionaries),
        model.dictionaries,
    )
    deltas = trend(now, before)
    assert deltas["sessions"] == pytest.approx(1.0)
    assert deltas["cost_usd"] == pytest.approx((1.63 - 4.87) / 4.87, rel=1e-3)


def test_missing_cost_is_never_zero(inputs):  # noqa: F811
    model = inputs.model
    unpriced = model.row("codex/019d0006-0000-7000-8000-000000000006")
    assert cost_usd(unpriced) is None
    assert cost_basis(unpriced, model.cost_basis) == "none"
    unscored = model.row("codex/019d0008-0000-7000-8000-000000000008")
    assert cost_usd(unscored) is None
    assert cost_basis(unscored, model.cost_basis) == "unscored"
    priced = model.row(KEY_MAIN)
    assert cost_usd(priced) == pytest.approx(1.42)
    assert cost_basis(priced, model.cost_basis) == "exact"


def test_row_accessors(inputs):  # noqa: F811
    row = inputs.model.row(KEY_MAIN)
    assert cost_usd(row) == pytest.approx(1.42)
    assert error_rate(row) == pytest.approx(0.08)
    assert has_flag(row, "committed")
    assert error_rate(inputs.model.row("codex/019d0008-0000-7000-8000-000000000008")) is None


def test_archive_fixture_is_untouched(archive: Layout):  # noqa: F811
    assert (archive.root / "catalog" / "sessions.jsonl").exists()
