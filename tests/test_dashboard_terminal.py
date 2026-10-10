"""Terminal dashboard view."""
from __future__ import annotations

import io

from rich.console import Console

from retro.dashboard.terminal import ViewState, run_terminal_dashboard, selected_rows, show_detail
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive, inputs  # noqa: F401


def render(layout: Layout, *, interactive=False, input_text: str = "") -> str:
    buffer = io.StringIO()
    console = Console(file=buffer, width=160, force_terminal=False, legacy_windows=False)
    if interactive:
        console.input = _scripted(iter(input_text.splitlines()))
    run_terminal_dashboard(layout, console=console, interactive=interactive)
    return buffer.getvalue()


def _scripted(lines):
    def reader(_prompt=""):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    return reader


def test_non_interactive_view_prints_overview_projects_and_sessions(archive: Layout):  # noqa: F811
    output = render(archive)
    assert "retro dashboard" in output
    assert "sessions" in output
    assert "retro" in output and "Cortex" in output
    assert "Fix dashboard shard writer" in output
    assert "sync failed" in output


def test_view_reports_missing_inputs(empty_archive: Layout):  # noqa: F811
    output = render(empty_archive)
    assert "catalog missing" in output


def test_interactive_loop_supports_navigation(archive: Layout):  # noqa: F811
    output = render(archive, interactive=True, input_text="r all\np\ns\nd 1\nx\nf shard\nq\n")
    assert "projects" in output
    assert "first prompt" in output
    assert "usage.cost_usd" in output
    assert "ev-80" in output
    assert "excluded sessions: shown" in output


def test_interactive_rejects_unknown_commands(archive: Layout):  # noqa: F811
    output = render(archive, interactive=True, input_text="zzz\nr 99d\nq\n")
    assert "unknown command" in output
    assert "range must be one of" in output


def test_selected_rows_follow_state(inputs):  # noqa: F811
    state = ViewState(range_key="all")
    assert len(selected_rows(inputs, state)) == 6
    state.include_excluded = True
    assert len(selected_rows(inputs, state)) == 8
    state.query = "AIME"
    assert len(selected_rows(inputs, state)) == 1


def test_detail_accepts_a_session_key(inputs):  # noqa: F811
    buffer = io.StringIO()
    console = Console(file=buffer, width=160)
    state = ViewState(range_key="all")
    show_detail(console, inputs, state, "codex/019d0001-0000-7000-8000-000000000001")
    text = buffer.getvalue()
    assert "Fix dashboard shard writer" in text
    assert "rendered/codex" in text.replace("\n", "")
    show_detail(console, inputs, state, "missing-session")
    assert "no such session" in buffer.getvalue()


def test_unpriced_groups_show_a_dash_not_zero(archive: Layout):  # noqa: F811
    output = render(archive, interactive=True, input_text="r all\np\nq\n")
    assert "unpriced (shown as —)" in output
    # the Cortex project in the fixture has one priced and one unscored session
    projects = output[output.index("projects ("):]
    assert "$0.00" not in projects


def test_cost_column_is_dashed_for_sessions_without_an_estimate(archive: Layout):  # noqa: F811
    output = render(archive, interactive=True, input_text="r all\ns\nq\n")
    sessions = output[output.index("sessions ("):]
    assert "—" in sessions
    assert "$0.00" not in sessions
