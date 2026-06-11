"""Typer CLI for retro.

Commands:
  retro list                       -> show discoverable sessions per host
  retro import claude|codex [...]  -> capture + normalize a session
  retro import all                 -> capture + normalize all discoverable sessions
  retro render <host> <id>         -> re-render markdown from normalized
  retro show   <host> <id>         -> show artifact paths + counts
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .importers.claude import ClaudeImporter
from .importers.codex import CodexImporter
from .mining import (
    FILTER_REGISTRY as MINING_FILTERS,
)
from .mining import (
    METHOD_REGISTRY as MINING_METHODS,
)
from .mining import (
    mine_with_method,
    write_mining_artifacts,
)
from .renderer import render_file
from .schema import Host, read_events
from .signals import REGISTRY as SIGNAL_REGISTRY
from .signals import run_signals, write_signal_artifacts
from .storage import Layout, default_layout

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Capture Codex / Claude Code rollouts and store them as durable local artifacts.",
)
import_app = typer.Typer(no_args_is_help=True, help="Import a session from a host.")
app.add_typer(import_app, name="import")

signal_app = typer.Typer(
    no_args_is_help=True,
    help="Compute, list, and inspect signal readings over captured sessions.",
)
app.add_typer(signal_app, name="signal")

dashboard_app = typer.Typer(
    no_args_is_help=True,
    help="Build and inspect the local static dashboard.",
)
app.add_typer(dashboard_app, name="dashboard")

memory_app = typer.Typer(
    no_args_is_help=True,
    help="Build and query the local memory index.",
)
app.add_typer(memory_app, name="memory")

quest_app = typer.Typer(
    no_args_is_help=True,
    help="Manage daily quests and streaks.",
)
app.add_typer(quest_app, name="quest")

console = Console()


def _layout(root: Path | None) -> Layout:
    lay = default_layout(root or Path.cwd() / "rollout-memory")
    lay.ensure()
    return lay


# ---- list -------------------------------------------------------------------


@app.command("list")
def list_cmd(
    host: str | None = typer.Option(None, help="Filter to one host: claude|codex"),
    limit: int = typer.Option(20, help="Max rows per host"),
    root: Path | None = typer.Option(None, help="rollout-memory root (default ./rollout-memory)"),
):
    """List sessions discoverable on this machine."""
    lay = _layout(root)
    if host in (None, "claude", "claude-code"):
        _print_claude_table(ClaudeImporter(lay), limit, lay)
    if host in (None, "codex"):
        _print_codex_table(CodexImporter(lay), limit, lay)


def _print_claude_table(imp: ClaudeImporter, limit: int, lay: Layout) -> None:
    all_sessions = imp.discover()
    sessions = all_sessions[:limit]
    imported = set(lay.list_imported("claude-code"))
    table = Table(title=f"Claude Code  ({len(sessions)} shown)")
    table.add_column("imported", justify="center")
    table.add_column("session_id")
    table.add_column("project")
    table.add_column("size")
    for s in sessions:
        mark = "✓" if s.session_id in imported else ""
        table.add_row(mark, s.session_id, s.project_slug, f"{s.size_bytes:,}")
    console.print(table)
    _print_claude_retention_note(all_sessions)


def _print_claude_retention_note(sessions) -> None:
    """Surface Claude's ~30-day log retention if logs are aging out.

    Claude Code retains transcripts for ~30 days by default (`cleanupPeriodDays`
    in Claude settings). Warn so users know to capture before logs disappear.
    """
    if not sessions:
        return
    import time

    oldest = min(s.mtime for s in sessions)
    age_days = (time.time() - oldest) / 86400
    if age_days >= 25:
        console.print(
            f"[yellow]⚠  Oldest discoverable Claude transcript is "
            f"{age_days:.1f} days old. Claude Code retains logs for ~30 days "
            f"by default — capture older sessions before they age out, or "
            f"raise `cleanupPeriodDays` in Claude settings.[/yellow]"
        )


def _print_codex_table(imp: CodexImporter, limit: int, lay: Layout) -> None:
    threads = imp.discover()[:limit]
    imported = set(lay.list_imported("codex"))
    table = Table(title=f"Codex  ({len(threads)} shown)")
    table.add_column("imported", justify="center")
    table.add_column("thread_id")
    table.add_column("cwd")
    table.add_column("title")
    for t in threads:
        mark = "✓" if t.thread_id in imported else ""
        table.add_row(mark, t.thread_id, t.cwd, t.display_title)
    console.print(table)


# ---- import claude / codex --------------------------------------------------


@import_app.command("claude")
def import_claude(
    session_id: str | None = typer.Option(None, "--session-id", help="Specific session id"),
    latest: bool = typer.Option(False, "--latest", help="Import the most-recent session"),
    all_sessions: bool = typer.Option(False, "--all", help="Import every discoverable Claude Code session"),
    limit: int | None = typer.Option(None, "--limit", help="Optional max sessions to import with --all"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raw capture"),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
):
    """Import a Claude Code session."""
    lay = _layout(root)
    imp = ClaudeImporter(lay)
    if all_sessions:
        _import_many(
            imp,
            [(s.session_id, s.session_id) for s in imp.discover()[:limit]],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not session_id and not latest:
        raise typer.BadParameter("Pass --session-id <id>, --latest, or --all")
    if latest:
        s = imp.latest()
        if s is None:
            console.print("[red]No Claude Code sessions found.[/red]")
            raise typer.Exit(1)
        session_id = s.session_id
    assert session_id is not None  # guaranteed by the --session-id/--latest check above
    _do_import(imp, session_id, force=force, lay=lay, render=not no_render)


@import_app.command("codex")
def import_codex(
    thread_id: str | None = typer.Option(None, "--thread-id", help="Specific thread id"),
    latest: bool = typer.Option(False, "--latest", help="Import the most-recent thread"),
    all_sessions: bool = typer.Option(False, "--all", help="Import every discoverable Codex thread"),
    limit: int | None = typer.Option(None, "--limit", help="Optional max threads to import with --all"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raw capture"),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
):
    """Import a Codex thread."""
    lay = _layout(root)
    imp = CodexImporter(lay)
    if all_sessions:
        _import_many(
            imp,
            [(t.thread_id, t.display_title) for t in imp.discover()[:limit]],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not thread_id and not latest:
        raise typer.BadParameter("Pass --thread-id <id>, --latest, or --all")
    if latest:
        t = imp.latest()
        if t is None:
            console.print("[red]No Codex threads found.[/red]")
            raise typer.Exit(1)
        thread_id = t.thread_id
    assert thread_id is not None  # guaranteed by the --thread-id/--latest check above
    _do_import(imp, thread_id, force=force, lay=lay, render=not no_render)


@import_app.command("all")
def import_all(
    force: bool = typer.Option(False, "--force", help="Overwrite existing raw captures"),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
    limit_per_host: int | None = typer.Option(
        None, "--limit-per-host", help="Optional max sessions per host"
    ),
):
    """Import every discoverable Claude Code session and Codex thread."""
    lay = _layout(root)
    claude = ClaudeImporter(lay)
    codex = CodexImporter(lay)
    failures = []
    failures.extend(
        _import_many(
            claude,
            [(s.session_id, s.session_id) for s in claude.discover()[:limit_per_host]],
            force=force,
            lay=lay,
            render=not no_render,
            exit_on_failure=False,
        )
    )
    failures.extend(
        _import_many(
            codex,
            [(t.thread_id, t.display_title) for t in codex.discover()[:limit_per_host]],
            force=force,
            lay=lay,
            render=not no_render,
            exit_on_failure=False,
        )
    )
    if failures:
        raise typer.Exit(1)


def _do_import(imp, identifier: str, *, force: bool, lay: Layout, render: bool) -> None:
    try:
        result = imp.import_session(identifier=identifier, force=force)
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(2) from None
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[green]captured {result.host}/{result.session_id}[/green]")
    console.print(f"  raw:        {result.raw_dir}")
    console.print(f"  normalized: {result.normalized_path}  ({result.event_count} events)")
    if result.unknown_event_count:
        console.print(
            f"  [yellow]gaps:[/yellow] {result.unknown_event_count} unknown events "
            f"(types: {', '.join(result.gaps)})"
        )
    if render:
        dest = lay.rendered_path(result.host, result.session_id)
        n = render_file(result.normalized_path, dest)
        console.print(f"  rendered:   {dest}  ({n:,} bytes)")

    try:
        from .analyzer import check_operator_diagnostics
        events = list(read_events(result.normalized_path))
        tips = check_operator_diagnostics(events)
        for tip in tips:
            console.print(f"[yellow]{tip}[/yellow]")
    except Exception:
        pass


def _import_many(
    imp,
    targets: list[tuple[str, str]],
    *,
    force: bool,
    lay: Layout,
    render: bool,
    exit_on_failure: bool = True,
) -> list[str]:
    if not targets:
        console.print("[yellow]No sessions found.[/yellow]")
        return []
    imported = 0
    skipped = 0
    failures: list[str] = []
    for identifier, label in targets:
        try:
            _do_import(imp, identifier, force=force, lay=lay, render=render)
            imported += 1
        except typer.Exit as e:
            if e.exit_code == 2 and not force:
                skipped += 1
                console.print(f"[dim]skipped existing {identifier}[/dim]")
                continue
            failures.append(f"{identifier}: exit {e.exit_code}")
            console.print(f"[red]failed {identifier} ({label}): exit {e.exit_code}[/red]")
            if exit_on_failure:
                raise
        except Exception as e:
            failures.append(f"{identifier}: {e}")
            console.print(f"[red]failed {identifier} ({label}): {e}[/red]")
            if exit_on_failure:
                raise

    console.print(
        f"[bold]imported {imported}/{len(targets)} sessions[/bold]"
        + (f"  [dim]({skipped} already existed)[/dim]" if skipped else "")
    )
    if failures:
        console.print(f"[red]{len(failures)} failures[/red]")
    return failures


# ---- render / show ----------------------------------------------------------


@app.command("mine")
def mine_cmd(
    host: str = typer.Argument(..., help="claude|codex|*"),
    session_id: str = typer.Argument(..., help="session id, thread id, or *"),
    method: str = typer.Option(
        "reme_refine_poc",
        "--method",
        help="Mining method name, or `all` to run every registered method.",
    ),
    filter_names: str | None = typer.Option(
        None,
        "--filter",
        help="Comma-separated list of filters to apply after mining (e.g. risk_aware).",
    ),
    all_sessions: bool = typer.Option(
        False, "--all", help="Mine all imported normalized sessions for the host"
    ),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Mine prompt-time memory from an imported normalized rollout."""
    # Resolve method choices.
    if method == "all":
        method_list = sorted(MINING_METHODS)
    else:
        if method not in MINING_METHODS:
            raise typer.BadParameter(
                f"unknown method {method!r}; registered: {sorted(MINING_METHODS)}"
            )
        method_list = [method]

    filter_list = [f.strip() for f in (filter_names or "").split(",") if f.strip()]
    for f in filter_list:
        if f not in MINING_FILTERS:
            raise typer.BadParameter(
                f"unknown filter {f!r}; registered: {sorted(MINING_FILTERS)}"
            )

    lay = _layout(root)
    targets = _mine_targets(lay, host, session_id, all_sessions=all_sessions)
    if not targets:
        console.print("[yellow]No normalized sessions found to mine.[/yellow]")
        raise typer.Exit(0)

    failures: list[str] = []
    for host_full, target_session_id in targets:
        for m in method_list:
            try:
                _mine_one(lay, host_full, target_session_id, m, filter_list)
            except Exception as e:  # keep bulk mining moving across sessions
                failures.append(f"{host_full}/{target_session_id} [{m}]: {e}")
                console.print(f"[red]failed {host_full}/{target_session_id} [{m}]: {e}[/red]")

    runs = len(targets) * len(method_list)
    if runs > 1:
        console.print(f"[bold]mined {runs - len(failures)}/{runs} (session × method) runs[/bold]")
    if failures:
        raise typer.Exit(1)


def _mine_one(
    lay: Layout,
    host_full: Host,
    session_id: str,
    method: str,
    filters: list[str],
) -> None:
    normalized = lay.normalized_path(host_full, session_id)
    if not normalized.exists():
        raise FileNotFoundError(f"No normalized events at {normalized}")

    result = mine_with_method(normalized, method=method, filters=filters)
    json_path = lay.mined_json_path(host_full, session_id, result.method)
    prompt_path = lay.mined_prompt_path(host_full, session_id, result.method)
    write_mining_artifacts(result, json_path, prompt_path)

    flt = f"  filters: {', '.join(filters)}" if filters else ""
    console.print(f"[green]mined {result.host}/{result.session_id} with {result.method}[/green]")
    console.print(f"  json:   {json_path}")
    console.print(f"  prompt: {prompt_path}")
    console.print(f"  candidates: {len(result.candidates)}{flt}")


@app.command("methods")
def methods_cmd():
    """List registered mining methods and filters."""
    m_table = Table(title=f"Mining methods ({len(MINING_METHODS)})")
    m_table.add_column("name")
    m_table.add_column("description")
    for name in sorted(MINING_METHODS):
        m_table.add_row(name, MINING_METHODS[name].description)
    console.print(m_table)

    f_table = Table(title=f"Mining filters ({len(MINING_FILTERS)})")
    f_table.add_column("name")
    f_table.add_column("description")
    for name in sorted(MINING_FILTERS):
        f_table.add_row(name, MINING_FILTERS[name].description)
    console.print(f_table)


def _mine_targets(
    lay: Layout,
    host: str,
    session_id: str,
    *,
    all_sessions: bool,
) -> list[tuple[Host, str]]:
    hosts = _expand_hosts(host)
    if all_sessions or session_id == "*":
        targets: list[tuple[Host, str]] = []
        for h in hosts:
            targets.extend((h, sid) for sid in lay.list_normalized(h))
        return targets
    if len(hosts) != 1:
        raise typer.BadParameter("When host is *, session_id must be * or --all must be used")
    return [(hosts[0], session_id)]


@app.command("render")
def render_cmd(
    host: str = typer.Argument(..., help="claude|codex"),
    session_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Re-render markdown from already-imported normalized events."""
    lay = _layout(root)
    host_full = _expand_host(host)
    normalized = lay.normalized_path(host_full, session_id)
    if not normalized.exists():
        console.print(f"[red]No normalized events at {normalized}[/red]")
        raise typer.Exit(1)
    dest = lay.rendered_path(host_full, session_id)
    n = render_file(normalized, dest)
    console.print(f"rendered {dest}  ({n:,} bytes)")


@app.command("show")
def show_cmd(
    host: str = typer.Argument(..., help="claude|codex"),
    session_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Show artifact paths and basic stats for an imported session."""
    lay = _layout(root)
    host_full = _expand_host(host)
    raw_dir = lay.raw_dir(host_full, session_id)
    normalized = lay.normalized_path(host_full, session_id)
    rendered = lay.rendered_path(host_full, session_id)

    table = Table(title=f"{host_full}/{session_id}")
    table.add_column("artifact")
    table.add_column("path")
    table.add_column("status")
    table.add_row("raw/", str(raw_dir), "✓" if raw_dir.exists() else "missing")
    table.add_row("normalized", str(normalized), "✓" if normalized.exists() else "missing")
    table.add_row("rendered", str(rendered), "✓" if rendered.exists() else "missing")
    console.print(table)

    if normalized.exists():
        counts: dict[str, int] = {}
        total = 0
        for ev in read_events(normalized):
            counts[ev.event_type] = counts.get(ev.event_type, 0) + 1
            total += 1
        console.print(f"\n[bold]Event counts[/bold] (total {total}):")
        for k, v in sorted(counts.items()):
            console.print(f"  {k:<14} {v}")


def _expand_host(host: str) -> Host:
    h = host.lower()
    if h in ("claude", "claude-code", "cc"):
        return "claude-code"
    if h in ("codex", "cx"):
        return "codex"
    raise typer.BadParameter(f"unknown host {host!r}; use claude|codex")


def _expand_hosts(host: str) -> list[Host]:
    h = host.lower()
    if h in ("*", "all"):
        return ["claude-code", "codex"]
    return [_expand_host(host)]


# ---- signals ----------------------------------------------------------------


@signal_app.command("list")
def signal_list(
    group: str | None = typer.Option(None, help="Filter by group: activity|outcome|cost|risk"),
):
    """List registered signals grouped by intent."""
    table = Table(title=f"Signals ({len(SIGNAL_REGISTRY)} registered)")
    table.add_column("name")
    table.add_column("group")
    table.add_column("kind")
    table.add_column("method")
    table.add_column("unit")
    table.add_column("description")
    for name in sorted(SIGNAL_REGISTRY):
        s = SIGNAL_REGISTRY[name]
        if group and s.group != group:
            continue
        table.add_row(s.name, s.group, s.kind, s.method, s.unit or "", s.description)
    console.print(table)


@signal_app.command("run")
def signal_run(
    host: str | None = typer.Option(None, help="Restrict to one host: claude|codex"),
    session_id: str | None = typer.Option(
        None, "--session-id", help="Restrict to one session id (repeatable via comma)"
    ),
    signal: str | None = typer.Option(
        None, "--signal", help="Restrict to one signal name (repeatable via comma)"
    ),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Compute signals over imported sessions and write readings + aggregates."""
    lay = _layout(root)
    host_full = _expand_host(host) if host else None
    sids = [s.strip() for s in session_id.split(",")] if session_id else None
    sigs = [s.strip() for s in signal.split(",")] if signal else None
    try:
        readings = run_signals(lay, host=host_full, session_ids=sids, signal_names=sigs)
    except KeyError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if not readings:
        console.print("[yellow]No readings produced (no matching sessions or signals).[/yellow]")
        raise typer.Exit(0)
    paths = write_signal_artifacts(lay, readings)
    console.print(f"[green]wrote {len(readings)} readings[/green]")
    for label, p in paths.items():
        console.print(f"  {label:<10} {p}")


@signal_app.command("show")
def signal_show(
    host: str = typer.Argument(..., help="claude|codex"),
    session_id: str = typer.Argument(...),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Show all signal readings stored for one session."""
    lay = _layout(root)
    host_full = _expand_host(host)
    readings_path = lay.root / "signals" / "readings.jsonl"
    if not readings_path.exists():
        console.print(f"[red]No readings found at {readings_path}. Run `retro signal run` first.[/red]")
        raise typer.Exit(1)
    import json as _json

    table = Table(title=f"Signals for {host_full}/{session_id}")
    table.add_column("signal")
    table.add_column("group")
    table.add_column("kind")
    table.add_column("value")
    table.add_column("unit")
    table.add_column("notes")
    found = 0
    with readings_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = _json.loads(line)
            if r["host"] != host_full or r["session_id"] != session_id:
                continue
            found += 1
            note_bits = []
            if r.get("confidence") not in (None, 1.0):
                note_bits.append(f"conf={r['confidence']}")
            meta = r.get("metadata") or {}
            if "reason" in meta:
                note_bits.append(f"reason={meta['reason']}")
            if "error" in meta:
                note_bits.append(f"err={meta['error'][:40]}")
            table.add_row(
                r["signal"],
                r["group"],
                r["kind"],
                str(r["value"]),
                r.get("unit") or "",
                ", ".join(note_bits),
            )
    if found == 0:
        console.print(f"[yellow]No readings for {host_full}/{session_id}[/yellow]")
        raise typer.Exit(1)
    console.print(table)


# ---- dashboard --------------------------------------------------------------


@dashboard_app.command("build")
def dashboard_build(
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="Cost mode: auto, calculate, or display.",
    ),
    root: Path | None = typer.Option(None, help="rollout-memory root (default ./rollout-memory)"),
    out: Path | None = typer.Option(None, help="output directory (default ./dashboard)"),
):
    """Build dashboard/data/rollouts.json and dashboard/index.html."""
    if mode not in {"auto", "calculate", "display"}:
        raise typer.BadParameter("mode must be one of: auto, calculate, display")

    from .dashboard_build import build as build_dashboard_html

    index_path = build_dashboard_html(mode=mode, artifact_root=root, out_dir=out)
    console.print(f"[green]dashboard ready:[/green] {index_path}")


@dashboard_app.command("experiments")
def dashboard_experiments(
    root: Path | None = typer.Option(None, help="rollout-memory root (default ./rollout-memory)"),
    out: Path | None = typer.Option(None, help="output directory (default ./dashboard)"),
):
    """Build the experimental trajectory-signals page (trajectory_experiments.html)."""
    from .dashboard_experiments import build as build_experiments_html

    html_path = build_experiments_html(artifact_root=root, out_dir=out)
    console.print(f"[green]experiments page ready:[/green] {html_path}")


@dashboard_app.command("view")
def dashboard_view(
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="Cost mode: auto, calculate, or display.",
    ),
):
    """View the rollout dashboard interactively in the terminal."""
    if mode not in {"auto", "calculate", "display"}:
        raise typer.BadParameter("mode must be one of: auto, calculate, display")

    from .dashboard_terminal import run_terminal_dashboard

    run_terminal_dashboard(mode=mode)


@app.command("analyze")
def analyze(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Analyze command and tool call patterns across imported sessions."""
    from .analyzer import analyze_sessions, generate_report, render_console_report

    lay = _layout(root)
    stats = analyze_sessions(lay)
    render_console_report(stats)

    report_path = lay.root / "analysis_report.md"
    generate_report(stats, report_path)
    console.print()
    console.print(f"[green]Wrote analysis report to:[/green] [bold]{report_path}[/bold]")


# ---- memory -----------------------------------------------------------------


@memory_app.command("init")
def memory_init(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Create the memory directory and empty SQLite index."""
    from .memory_store import init

    lay = _layout(root)
    init(lay)
    console.print(f"[green]memory index ready:[/green] {lay.memory_index_path()}")


@memory_app.command("reindex")
def memory_reindex(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Rebuild index.sqlite from flat-file memory sources."""
    from .memory_store import reindex

    lay = _layout(root)
    report = reindex(lay)
    console.print(f"[green]indexed {report.indexed} memories[/green]")
    console.print(f"  items.jsonl records: {report.source_records}")
    console.print(f"  mined artifacts:     {report.mined_records}")
    console.print(f"  evidence refs:       {report.evidence_refs}")
    console.print(f"  wiki links:          {report.links}")
    console.print(f"  sqlite:              {lay.memory_index_path()}")


@memory_app.command("doctor")
def memory_doctor(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Report memory index health and counts."""
    from .memory_store import doctor

    lay = _layout(root)
    report = doctor(lay)
    console.print(f"[bold]Memory index[/bold] {lay.memory_index_path()}")
    console.print(f"  memories:       {report.memory_count}")
    console.print(f"  statuses:       {_fmt_counts(report.counts_by_status)}")
    console.print(f"  scopes:         {_fmt_counts(report.counts_by_scope)}")
    console.print(f"  kinds:          {_fmt_counts(report.counts_by_kind)}")
    console.print(f"  dangling links: {report.dangling_links}")
    console.print(f"  sqlite-vec:     {'available' if report.sqlite_vec else 'not loaded'}")


@memory_app.command("import-authored")
def memory_import_authored(
    directory: Path = typer.Argument(..., help="Directory of markdown memory files"),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Import authored markdown memories into the flat-file memory log."""
    from .memory_store import import_authored

    lay = _layout(root)
    report = import_authored(lay, directory)
    console.print(f"[green]imported {report.imported} authored memories[/green]")
    if report.skipped:
        console.print(f"  skipped: {report.skipped}")
    console.print(f"  source:  {lay.memory_items_path()}")
    console.print(f"  sqlite:  {lay.memory_index_path()}")


@memory_app.command("retrieve")
def memory_retrieve(
    query: str = typer.Option(..., "--query", "-q", help="Search query"),
    cwd: Path | None = typer.Option(None, "--cwd", help="Repo/cwd for repo-scoped recall"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum memories to return"),
    include_candidates: bool = typer.Option(
        True,
        "--include-candidates/--accepted-only",
        help="Include candidate memories while the promotion workflow is being built.",
    ),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Retrieve ranked memories from FTS5 keyword recall."""
    from .memory_store import retrieve

    lay = _layout(root)
    rows = retrieve(
        lay,
        query,
        cwd=str(cwd.resolve()) if cwd else None,
        limit=limit,
        include_candidates=include_candidates,
    )
    if not rows:
        console.print("[yellow]No matching memories.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Memory recall: {query!r}")
    table.add_column("#", justify="right")
    table.add_column("score")
    table.add_column("kind")
    table.add_column("scope")
    table.add_column("status")
    table.add_column("memory")
    for row in rows:
        table.add_row(
            str(row.rank),
            f"{row.score:.3f}",
            row.kind,
            row.scope,
            row.status,
            row.text,
        )
    console.print(table)


@memory_app.command("weave")
def memory_weave(
    query: str = typer.Option(..., "--query", "-q", help="Search query"),
    cwd: Path | None = typer.Option(None, "--cwd", help="Repo/cwd for repo-scoped recall"),
    limit: int = typer.Option(6, "--limit", "-n", help="Maximum memories to include"),
    include_candidates: bool = typer.Option(
        True,
        "--include-candidates/--accepted-only",
        help="Include candidate memories while the promotion workflow is being built.",
    ),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Emit a compact prompt-time memory block."""
    from .memory_store import weave

    lay = _layout(root)
    result = weave(
        lay,
        query,
        cwd=str(cwd.resolve()) if cwd else None,
        limit=limit,
        include_candidates=include_candidates,
    )
    block = result.to_markdown()
    if not block:
        console.print("[yellow]No matching memories.[/yellow]")
        raise typer.Exit(0)
    console.print(block)


@memory_app.command("update-utility")
def memory_update_utility(
    memory_id: str = typer.Option(..., "--memory-id", help="Memory id to update"),
    reward: float = typer.Option(..., "--reward", help="Reward in [0, 1]"),
    session_id: str | None = typer.Option(None, "--session-id", help="Session that used the memory"),
    reason: str | None = typer.Option(None, "--reason", help="Optional update reason"),
    root: Path | None = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Append a utility event and update q_value."""
    from .memory_store import update_utility

    lay = _layout(root)
    try:
        report = update_utility(
            lay,
            memory_id,
            reward,
            session_id=session_id,
            reason=reason,
        )
    except KeyError:
        console.print(f"[red]No memory found with id {memory_id!r}[/red]")
        raise typer.Exit(1) from None
    console.print(
        f"[green]updated {report.memory_id}[/green] "
        f"q={report.old_q_value:.3f} → {report.new_q_value:.3f}"
    )
    console.print(
        f"  hits={report.hits} successes={report.successes} failures={report.failures}"
    )


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


@app.callback()
def callback(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """retro: local-first coding agent session capture and memory tool."""
    # Print streak message on execution if in a TTY
    if sys.stdout.isatty():
        try:
            from .quest import ensure_daily_quests, load_quest_state, save_quest_state
            lay = default_layout(root or Path.cwd() / "rollout-memory")
            state = load_quest_state(lay)
            gen_new = ensure_daily_quests(lay, state)
            if gen_new:
                save_quest_state(lay, state)
            
            streak = state.get("streak_count", 0)
            active_quests_count = sum(1 for q in state.get("daily_quests", []) if q["status"] == "active")
            if active_quests_count > 0:
                console.print(
                    f"[bold teal]\\[retro][/bold teal] Streak: {streak} Days. "
                    f"{active_quests_count} New Quests Available."
                )
        except Exception:
            pass


@quest_app.command("list")
def quest_list(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """List active daily quests, progression, and streak count."""
    from .quest import ensure_daily_quests, load_quest_state, save_quest_state
    lay = _layout(root)
    state = load_quest_state(lay)
    ensure_daily_quests(lay, state)
    save_quest_state(lay, state)

    console.print()
    console.print("[bold teal]Operator Ranks & Progression[/bold teal]")
    console.print(f"  Current Rank:   [bold green]{state.get('user_level')}[/bold green]")
    console.print(f"  Streak Count:   [bold yellow]{state.get('streak_count')} Days[/bold yellow]")
    console.print(f"  Streak Freezes: [bold cyan]{state.get('streak_freezes', 0)}[/bold cyan]")
    console.print(f"  Experience:     [bold]{state.get('experience_points')} XP[/bold]")
    console.print()

    console.print("[bold]Active Daily Quests:[/bold]")
    quests = state.get("daily_quests", [])
    if not quests:
        console.print("  No quests available.")
    else:
        for q in quests:
            is_comp = q["status"] == "completed"
            status_str = "[green]Completed[/green]" if is_comp else "[yellow]Active[/yellow]"
            console.print(f"  • [bold]{q['name']}[/bold] ({status_str})")
            console.print(f"    Objective: {q['objective']}")
            console.print(f"    Rationale: {q['rationale']}")
            console.print()


@quest_app.command("verify")
def quest_verify(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Run local metrics checks to verify active daily quests."""
    from .quest import load_quest_state, save_quest_state, verify_quests
    lay = _layout(root)
    state = load_quest_state(lay)

    report = verify_quests(lay, state)
    save_quest_state(lay, state)

    console.print()
    console.print("[bold teal]Quest Verification Summary[/bold teal]")
    console.print()

    for q, verified, reason in report["results"]:
        status_str = "[green]✓ Verified[/green]" if verified else "[red]✗ Not Verified[/red]"
        console.print(f"  • [bold]{q['name']}[/bold]: {status_str}")
        console.print(f"    {reason}")
        console.print()

    if report["xp_gained"] > 0:
        console.print(f"[bold green]+{report['xp_gained']} XP Gained![/bold green]")
        console.print(
            f"New Experience: [bold]{report['new_xp']} XP[/bold] "
            f"(Rank: [bold]{report['new_level']}[/bold])"
        )
        for q_id in report["now_completed_ids"]:
            console.print(f"Completed Quest: [bold]{q_id}[/bold]")
    else:
        console.print("No quests verified this run. Keep working on them!")
    console.print()


@quest_app.command("buy-freeze")
def quest_buy_freeze(
    root: Path | None = typer.Option(None, help="rollout-memory root"),
):
    """Purchase a Streak Freeze to protect your streak on inactive days (costs 200 XP)."""
    from .quest import buy_streak_freeze, load_quest_state, save_quest_state
    lay = _layout(root)
    state = load_quest_state(lay)

    msg = buy_streak_freeze(state)
    save_quest_state(lay, state)
    console.print(msg)


if __name__ == "__main__":
    app()
