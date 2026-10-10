"""Terminal view over the same catalog/signal inputs as the static dashboard."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..storage import Layout, default_layout
from .build import DashboardInputs, input_warnings, load_inputs
from .metrics import (
    Filters,
    Kpis,
    cost_basis,
    cost_usd,
    filter_rows,
    has_flag,
    kpis,
    project_rollup,
    success_rate,
    trend,
    value,
)

RANGES = {"7d": 7, "30d": 30, "90d": 90, "365d": 365, "all": None}
OUTCOME_STYLE = {
    "success": "green",
    "partial": "yellow",
    "failed": "red",
    "abandoned": "magenta",
    "unknown": "dim",
}


@dataclass
class ViewState:
    range_key: str = "30d"
    include_excluded: bool = False
    query: str = ""
    agent: str | None = None
    project: str | None = None


def run_terminal_dashboard(
    layout: Layout | None = None,
    *,
    root: Path | None = None,
    interactive: bool | None = None,
    console: Console | None = None,
    limit: int = 25,
) -> None:
    console = console or Console()
    resolved = layout or default_layout(root)
    inputs = load_inputs(resolved)
    state = ViewState()
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    show_overview(console, inputs, state)
    if not interactive:
        show_projects(console, inputs, state, limit=10)
        show_sessions(console, inputs, state, limit=limit)
        return

    while True:
        console.print(
            "[dim]commands:[/dim] [bold]o[/bold]verview  [bold]p[/bold]rojects  "
            "[bold]s[/bold]essions  [bold]d[/bold]etail <n>  [bold]f[/bold]ind <text>  "
            "[bold]r[/bold]ange <7d|30d|90d|365d|all>  [bold]x[/bold] toggle excluded  [bold]q[/bold]uit"
        )
        try:
            command = console.input("[bold cyan]retro>[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if not command:
            continue
        head, _, rest = command.partition(" ")
        head = head.lower()
        rest = rest.strip()
        if head in {"q", "quit", "exit"}:
            return
        if head in {"o", "overview"}:
            show_overview(console, inputs, state)
        elif head in {"p", "projects"}:
            show_projects(console, inputs, state, limit=20)
        elif head in {"s", "sessions"}:
            show_sessions(console, inputs, state, limit=limit)
        elif head in {"d", "detail"}:
            show_detail(console, inputs, state, rest)
        elif head in {"f", "find"}:
            state.query = rest
            show_sessions(console, inputs, state, limit=limit)
        elif head in {"r", "range"}:
            if rest in RANGES:
                state.range_key = rest
                show_overview(console, inputs, state)
            else:
                console.print(f"[yellow]range must be one of: {', '.join(RANGES)}[/yellow]")
        elif head in {"x", "excluded"}:
            state.include_excluded = not state.include_excluded
            console.print(f"[dim]excluded sessions: {'shown' if state.include_excluded else 'hidden'}[/dim]")
        else:
            console.print("[yellow]unknown command[/yellow]")


def selected_rows(inputs: DashboardInputs, state: ViewState) -> list:
    since = _range_start(state.range_key)
    filters = Filters(
        include_excluded=state.include_excluded,
        since=since,
        query=state.query or None,
        agents={state.agent} if state.agent else None,
        projects={state.project} if state.project else None,
    )
    return filter_rows(inputs.model.rows, filters, inputs.model.dictionaries)


def show_overview(console: Console, inputs: DashboardInputs, state: ViewState) -> None:
    model = inputs.model
    rows = selected_rows(inputs, state)
    current = kpis(rows, model.dictionaries)
    since = _range_start(state.range_key)
    if since is not None:
        span = _range_days(state.range_key) or 0
        previous_rows = filter_rows(
            model.rows,
            Filters(
                include_excluded=state.include_excluded,
                since=since - span * 86400,
                until=since,
                query=state.query or None,
            ),
            model.dictionaries,
        )
    else:
        previous_rows = []
    deltas = trend(current, kpis(previous_rows, model.dictionaries))

    header = Table.grid(padding=(0, 2))
    header.add_column(justify="left")
    header.add_column(justify="right")
    header.add_row("archive", str(inputs.layout.root))
    header.add_row("range", state.range_key)
    header.add_row("sessions shown", f"{current.sessions:,} of {len(model.rows):,}")
    header.add_row("excluded", f"{model.quality['excluded']:,}")
    console.print(Panel(header, title="retro dashboard", border_style="cyan"))

    kpi_table = Table(box=None, pad_edge=False)
    for name in ("sessions", "active", "cost", "tokens", "turns", "tools", "edits", "success"):
        kpi_table.add_column(name, justify="right")
    kpi_table.add_row(
        _delta(current.sessions, deltas.get("sessions"), fmt=_int),
        _delta(current.active_s, deltas.get("active_s"), fmt=_duration),
        _delta(_total_cost(current), deltas.get("cost_usd"), fmt=_money),
        _delta(current.tokens, deltas.get("tokens"), fmt=_compact),
        _delta(current.user_turns, deltas.get("user_turns"), fmt=_int),
        _delta(current.tool_calls, deltas.get("tool_calls"), fmt=_int),
        _delta(current.edits, deltas.get("edits"), fmt=_int),
        _rate(success_rate(current)),
    )
    console.print(kpi_table)
    if current.unpriced:
        console.print(
            f"[dim]cost covers {current.sessions_with_cost:,} of {current.sessions:,} sessions; "
            f"{current.unpriced:,} unpriced (shown as —)[/dim]"
        )

    mix = Table(title="outcome / work mix", box=None)
    mix.add_column("outcome")
    mix.add_column("n", justify="right")
    mix.add_column("work")
    mix.add_column("n", justify="right")
    outcomes = sorted(current.outcomes.items(), key=lambda item: -item[1])
    work = sorted(current.work.items(), key=lambda item: -item[1])
    for position in range(max(len(outcomes), len(work))):
        left = outcomes[position] if position < len(outcomes) else ("", "")
        right = work[position] if position < len(work) else ("", "")
        mix.add_row(
            Text(str(left[0]), style=OUTCOME_STYLE.get(str(left[0]), "")),
            _int(left[1]) if left[1] != "" else "",
            str(right[0]),
            _int(right[1]) if right[1] != "" else "",
        )
    console.print(mix)

    agents = Table(title="agents", box=None)
    agents.add_column("agent")
    agents.add_column("sessions", justify="right")
    for name, count in sorted(current.agents.items(), key=lambda item: -item[1]):
        agents.add_row(name, _int(count))
    console.print(agents)

    for warning in input_warnings(inputs.catalog, inputs.signals):
        console.print(f"[yellow]! {warning}[/yellow]")
    latest = model.sync.get("latest")
    if latest:
        status = latest.get("status")
        style = "green" if status == "ok" else "red"
        console.print(
            f"[{style}]sync {status}[/{style}] "
            f"started {latest.get('started_at')} imported={latest.get('imported')} "
            f"failures={latest.get('failure_count')}"
        )


def show_projects(console: Console, inputs: DashboardInputs, state: ViewState, *, limit: int = 20) -> None:
    rows = selected_rows(inputs, state)
    rollup = project_rollup(rows, inputs.model.dictionaries)
    table = Table(title=f"projects ({len(rollup)})", box=None)
    table.add_column("project")
    table.add_column("sessions", justify="right")
    table.add_column("active", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("success", justify="right")
    table.add_column("agents")
    table.add_column("last")
    for entry in rollup[:limit]:
        agents = ", ".join(f"{name}×{count}" for name, count in sorted(entry["agents"].items()))
        judged = sum(count for name, count in entry["outcomes"].items() if name != "unknown")
        rate = entry["outcomes"].get("success", 0) / judged if judged else None
        table.add_row(
            str(entry["name"]),
            _int(entry["sessions"]),
            _duration(entry["active_s"]),
            _money(entry["cost_usd"] if entry["sessions_with_cost"] else None),
            _rate(rate),
            agents,
            _date(entry["last"]),
        )
    console.print(table)


def show_sessions(console: Console, inputs: DashboardInputs, state: ViewState, *, limit: int = 25) -> None:
    rows = selected_rows(inputs, state)
    model = inputs.model
    table = Table(title=f"sessions ({len(rows)})", box=None)
    table.add_column("#", justify="right")
    table.add_column("when")
    table.add_column("agent")
    table.add_column("project")
    table.add_column("title", max_width=46)
    table.add_column("dur", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("outcome")
    for position, row in enumerate(rows[:limit], start=1):
        outcome = _lookup(model.dictionaries.outcomes, value(row, "outcome"))
        project = _project_name(model.dictionaries, value(row, "project"))
        marks = ""
        if has_flag(row, "excluded"):
            marks += "×"
        if has_flag(row, "subagent"):
            marks += "↳"
        table.add_row(
            str(position),
            _date(value(row, "started")),
            _lookup(model.dictionaries.agents, value(row, "agent")),
            project,
            (marks + " " if marks else "") + str(value(row, "title") or ""),
            _duration(value(row, "duration")),
            _money(cost_usd(row)),
            Text(outcome, style=OUTCOME_STYLE.get(outcome, "")),
        )
    console.print(table)
    if len(rows) > limit:
        console.print(f"[dim]showing {limit} of {len(rows)}; use `f <text>` to filter[/dim]")


def show_detail(
    console: Console,
    inputs: DashboardInputs,
    state: ViewState,
    selector: str,
) -> None:
    rows = selected_rows(inputs, state)
    row = None
    if selector.isdigit():
        position = int(selector) - 1
        if 0 <= position < len(rows):
            row = rows[position]
    elif selector:
        for candidate in inputs.model.rows:
            if value(candidate, "key") == selector or value(candidate, "key").endswith("/" + selector):
                row = candidate
                break
    if row is None:
        console.print("[yellow]no such session; run `s` first and pick a row number[/yellow]")
        return

    key = value(row, "key")
    record = inputs.model.records[key]
    signal_record = inputs.model.signals.records.get(key)
    header = Table.grid(padding=(0, 2))
    header.add_column()
    header.add_column()
    header.add_row("key", key)
    header.add_row("project", f"{record.project.name} [dim]({record.project.id})[/dim]")
    header.add_row("agent / client", f"{record.agent} / {record.client}")
    header.add_row("models", ", ".join(record.models) or "-")
    header.add_row("branch", str((record.git or {}).get("branch") or "-"))
    header.add_row("started", str(record.started_at or "-"))
    cost = cost_usd(row)
    basis = cost_basis(row, inputs.model.cost_basis)
    header.add_row("cost", _money(cost) + ("" if cost is not None else f" [dim]({basis})[/dim]"))
    header.add_row("duration", _duration(record.duration_s))
    header.add_row("active", _duration(record.active_s))
    header.add_row("tags", ", ".join(record.tags) or "-")
    if record.parent_key:
        header.add_row("parent", record.parent_key)
    children = inputs.model.children.get(key) or []
    if children:
        header.add_row("subagents", ", ".join(children[:6]))
    console.print(Panel(header, title=str(record.title or key), border_style="cyan"))

    if record.first_prompt:
        console.print(Panel(record.first_prompt[:1200], title="first prompt", border_style="dim"))

    if signal_record:
        definitions = {definition.id: definition for definition in inputs.model.definitions}
        grouped: dict[str, list[tuple[str, Any]]] = {}
        for signal_id, signal_value in sorted(signal_record.values.items()):
            definition = definitions.get(signal_id)
            category = definition.category if definition else "other"
            grouped.setdefault(category, []).append((signal_id, signal_value))
        for category, entries in sorted(grouped.items()):
            table = Table(title=category, box=None)
            table.add_column("signal")
            table.add_column("value", justify="right")
            table.add_column("evidence", max_width=42)
            for signal_id, signal_value in entries:
                evidence = ", ".join(signal_record.evidence.get(signal_id, [])[:3])
                table.add_row(signal_id, _signal_text(signal_value), evidence or "")
            console.print(table)
    else:
        console.print("[yellow]no signals recorded for this session[/yellow]")

    console.print(f"[dim]transcript:[/dim] {inputs.layout.rendered_path(record.host, record.session_id)}")


def _signal_text(value_: Any) -> str:
    if isinstance(value_, bool):
        return "yes" if value_ else "no"
    if isinstance(value_, float):
        return f"{value_:,.3f}".rstrip("0").rstrip(".")
    if isinstance(value_, int):
        return f"{value_:,}"
    return str(value_)


def _range_days(range_key: str) -> int | None:
    return RANGES.get(range_key)


def _range_start(range_key: str) -> float | None:
    days = _range_days(range_key)
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()


def _lookup(names: list, index: Any) -> str:
    if isinstance(index, int) and 0 <= index < len(names):
        return str(names[index])
    return "unknown"


def _project_name(dictionaries, index: Any) -> str:
    if isinstance(index, int) and 0 <= index < len(dictionaries.projects):
        return str(dictionaries.projects[index].get("name") or dictionaries.projects[index].get("id"))
    return "unknown"


def _int(number: Any) -> str:
    try:
        return f"{int(number):,}"
    except (TypeError, ValueError):
        return "-"


def _compact(number: Any) -> str:
    try:
        count = float(number)
    except (TypeError, ValueError):
        return "-"
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(count) >= threshold:
            return f"{count / threshold:.1f}{suffix}"
    return f"{count:.0f}"


def _total_cost(stats: Kpis) -> float | None:
    """A group with nothing priced has no total; it is not zero."""
    return stats.cost_usd if stats.sessions_with_cost else None


def _money(number: Any) -> str:
    if number is None:
        return "—"
    try:
        return f"${float(number):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _duration(seconds: Any) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if total <= 0:
        return "-"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _rate(fraction: float | None) -> str:
    if fraction is None:
        return "-"
    return f"{fraction * 100:.0f}%"


def _date(epoch: Any) -> str:
    if not epoch:
        return "-"
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "-"


def _delta(current: Any, fraction: float | None, *, fmt) -> Text:
    text = Text(fmt(current))
    if fraction is None:
        return text
    arrow = "▲" if fraction > 0 else ("▼" if fraction < 0 else "=")
    style = "green" if fraction > 0 else ("red" if fraction < 0 else "dim")
    text.append(f" {arrow}{abs(fraction) * 100:.0f}%", style=style)
    return text
