"""Typer CLI for retro.

Commands:
  retro list                       -> show discoverable sessions per host
  retro import claude|codex|copilot [...]  -> capture + normalize a session
  retro import all                 -> capture + normalize all discoverable sessions
  retro render <host> <id>         -> re-render markdown from normalized
  retro show   <host> <id>         -> show artifact paths + counts
"""
from __future__ import annotations

import dataclasses
import json
import re
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from .benchmarks import (
    BenchmarkEvaluationResult,
    build_time_consistent_benchmark,
    evaluate_time_consistent_benchmark,
    run_ghostlab_benchmark,
)
from .benchmarks.task_scorer.build import (
    BuildConfigurationError,
    TasksetBuildSummary,
    build_taskset,
    resolve_taskset_paths,
)
from .benchmarks.task_scorer.bundle import BundleError, bundle_taskset
from .benchmarks.task_scorer.commit_sources import CommitLimits, CommitSourceError
from .benchmarks.task_scorer.environment import DockerContainerRuntime, resolve_environment
from .benchmarks.task_scorer.episodes import (
    EpisodeError,
    export_episodes,
    materialize_episode,
    score_episode,
)
from .benchmarks.task_scorer.ghostlab_cli import GhostlabError
from .benchmarks.task_scorer.git_state import (
    CaptureExistsError,
    GitError,
    capture_repository_state,
)
from .benchmarks.task_scorer.run import (
    AgentSpec,
    TasksetReportSummary,
    TasksetRunSummary,
    TaskVerificationError,
    report_taskset,
    run_taskset,
)
from .benchmarks.task_scorer.schema import ProjectEnvironment, SchemaError
from .benchmarks.task_scorer.selection import (
    EnvironmentResolver,
    SelectionError,
    SourceCandidate,
    select_taskset,
)
from .benchmarks.task_scorer.suite import SuiteError, curate_taskset, load_policy, load_suite
from .catalog import Catalog, SessionRecord, load_catalog, update_catalog
from .catalog.cli import projects_app, rules_app, sessions_app
from .config import (
    config_path,
    load_config,
    resolve_dashboard_dir,
    save_config,
    update_config,
    user_data_dir,
    user_log_dir,
)
from .extensions import (
    SourceHandle,
    canonical_source_host,
    create_source,
    doctor_sources,
    extension_records,
    source_host_names,
)
from .locking import archive_lock
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
from .renormalize import parse_since, renormalize_archive
from .schema import BUILTIN_HOSTS, Host, read_events
from .sdk import SessionDescriptor
from .signals import REGISTRY as SIGNAL_REGISTRY
from .signals import (
    definitions_version,
    load_signal_store,
    summarize,
    update_signals,
)
from .storage import Layout, default_layout

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Capture Codex, Claude Code, and VS Code Copilot rollouts as durable local artifacts.",
)
import_app = typer.Typer(no_args_is_help=True, help="Import a session from a host.")
app.add_typer(import_app, name="import")

capture_app = typer.Typer(
    no_args_is_help=True,
    help="Capture immutable repository state at coding-session boundaries.",
)
app.add_typer(capture_app, name="capture")

signal_app = typer.Typer(
    no_args_is_help=True,
    help="Compute, list, and inspect signal readings over captured sessions.",
)
app.add_typer(signal_app, name="signal")

benchmark_app = typer.Typer(
    no_args_is_help=True,
    help="Build and evaluate time-consistent private benchmarks from rollouts.",
)
app.add_typer(benchmark_app, name="benchmark")
taskset_app = typer.Typer(
    no_args_is_help=True,
    help="Build and evaluate Git-backed implementation tasks with hidden scorers.",
)
benchmark_app.add_typer(taskset_app, name="taskset")

experiment_app = typer.Typer(
    no_args_is_help=True,
    help="Freeze experiment setup, explicitly run candidate agents and clean owned artifacts.",
)
app.add_typer(experiment_app, name="experiment")

dashboard_app = typer.Typer(
    no_args_is_help=True,
    help="Build the static dashboard or enable opt-in localhost experiment controls.",
)
app.add_typer(dashboard_app, name="dashboard")

memory_app = typer.Typer(
    no_args_is_help=True,
    help="Build and query the local memory index.",
)
app.add_typer(memory_app, name="memory")

config_app = typer.Typer(no_args_is_help=True, help="Inspect and update per-user configuration.")
app.add_typer(config_app, name="config")

archive_app = typer.Typer(no_args_is_help=True, help="Plan and execute archive migrations.")
app.add_typer(archive_app, name="archive")

schedule_app = typer.Typer(no_args_is_help=True, help="Manage periodic local capture.")
app.add_typer(schedule_app, name="schedule")

extensions_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect built-in and installed source providers.",
)
app.add_typer(extensions_app, name="extensions")
app.add_typer(sessions_app, name="sessions")
app.add_typer(rules_app, name="rules")
app.add_typer(projects_app, name="projects")

console = Console()
_SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


def _layout(root: Optional[Path]) -> Layout:
    lay = default_layout(root)
    lay.ensure()
    return lay


# ---- repository-state capture ------------------------------------------------


@capture_app.command("start")
def capture_start_cmd(
    host: str = typer.Option(..., "--host", help="claude|codex|copilot"),
    session_id: str = typer.Option(..., "--session-id", help="Host session identifier"),
    cwd: Path = typer.Option(
        ...,
        "--cwd",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Repository working directory at session start",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Capture exact Git state before the first coding-agent action."""
    _capture_session_repo_state("start", host, session_id, cwd, root)


@capture_app.command("end")
def capture_end_cmd(
    host: str = typer.Option(..., "--host", help="claude|codex|copilot"),
    session_id: str = typer.Option(..., "--session-id", help="Host session identifier"),
    cwd: Path = typer.Option(
        ...,
        "--cwd",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Repository working directory after the final coding-agent action",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Capture exact Git state after the final coding-agent action."""
    _capture_session_repo_state("end", host, session_id, cwd, root)


def _capture_session_repo_state(
    phase: str,
    host: str,
    session_id: str,
    cwd: Path,
    root: Optional[Path],
) -> None:
    if not _SAFE_SESSION_ID_RE.fullmatch(session_id):
        raise typer.BadParameter("session id contains unsupported characters")
    lay = _layout(root)
    try:
        record = capture_repository_state(
            layout=lay,
            host=_expand_host(host),
            session_id=session_id,
            cwd=cwd,
            phase=phase,
        )
    except CaptureExistsError as exc:
        console.print(f"[red]repository state already captured: {exc.path}[/red]")
        raise typer.Exit(2) from exc
    except GitError as exc:
        console.print(f"[red]repository state capture failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))


# ---- list -------------------------------------------------------------------


@app.command("list")
def list_cmd(
    host: Optional[str] = typer.Option(None, help="Filter to one registered source host"),
    limit: int = typer.Option(20, help="Max rows per host"),
    root: Optional[Path] = typer.Option(
        None,
        help="rollout-memory root (default ./rollout-memory)",
    ),
):
    """List sessions discoverable on this machine."""
    lay = _layout(root)
    hosts = [_expand_host(host)] if host else source_host_names()
    failures = 0
    for source_host in hosts:
        try:
            source = create_source(source_host, lay)
            _print_source_table(source, limit, lay)
        except Exception as exc:
            failures += 1
            console.print(
                f"[red]Unable to list {source_host!r}: {type(exc).__name__}: {exc}[/red]"
            )
    if host and failures:
        raise typer.Exit(1)


def _print_source_table(source: SourceHandle, limit: int, lay: Layout) -> None:
    all_sessions = source.discover()
    sessions = all_sessions[:limit]
    imported = set(lay.list_imported(source.host))
    table = Table(
        title=f"{source.display_name}  ({len(sessions)}/{len(all_sessions)} shown)"
    )
    table.add_column("imported", justify="center")
    table.add_column("session_id")
    table.add_column("cwd")
    table.add_column("title")
    for session in sessions:
        mark = "✓" if session.session_id in imported else ""
        table.add_row(
            mark,
            session.session_id,
            session.cwd or "",
            session.title,
        )
    console.print(table)
    if source.host == "claude-code":
        _print_claude_retention_note(all_sessions)


def _print_claude_retention_note(sessions: list[SessionDescriptor]) -> None:
    """Surface Claude's ~30-day log retention if logs are aging out.

    Claude Code retains transcripts for ~30 days by default (`cleanupPeriodDays`
    in Claude settings). Warn so users know to capture before logs disappear.
    """
    timestamps = [
        float(session.updated_at)
        for session in sessions
        if isinstance(session.updated_at, (int, float))
    ]
    if not timestamps:
        return
    import time

    oldest = min(timestamps)
    age_days = (time.time() - oldest) / 86400
    if age_days >= 25:
        console.print(
            f"[yellow]⚠  Oldest discoverable Claude transcript is "
            f"{age_days:.1f} days old. Claude Code retains logs for ~30 days "
            f"by default — capture older sessions before they age out, or "
            f"raise `cleanupPeriodDays` in Claude settings.[/yellow]"
        )


@extensions_app.command("list")
def extensions_list() -> None:
    """List source providers without importing optional extension modules."""
    records = extension_records()
    collisions = {
        name
        for name, count in Counter(record.name for record in records).items()
        if count > 1
    }
    table = Table(title=f"Retro source providers ({len(records)})")
    table.add_column("host")
    table.add_column("source")
    table.add_column("API")
    table.add_column("status")
    for record in sorted(records, key=lambda item: (item.name, item.source)):
        status = "collision" if record.name in collisions else record.status
        table.add_row(
            record.name,
            record.source,
            record.api_version or "—",
            status,
        )
    console.print(table)


@extensions_app.command("doctor")
def extensions_doctor() -> None:
    """Validate provider imports, API versions, names, and required methods."""
    results = doctor_sources()
    table = Table(title="Retro source extension doctor")
    table.add_column("status")
    table.add_column("host")
    table.add_column("source")
    table.add_column("detail")
    for result in sorted(
        results,
        key=lambda item: (item.record.name, item.record.source),
    ):
        table.add_row(
            "✓" if result.ok else "✗",
            result.record.name,
            result.record.source,
            result.message,
        )
    console.print(table)
    failures = sum(not result.ok for result in results)
    if failures:
        console.print(f"[red]{failures} source extension check(s) failed[/red]")
        raise typer.Exit(1)
    console.print(f"[green]All {len(results)} source provider checks passed.[/green]")


# ---- import claude / codex / copilot ----------------------------------------


@import_app.command("claude")
def import_claude(
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Specific session id"),
    latest: bool = typer.Option(False, "--latest", help="Import the most-recent session"),
    all_sessions: bool = typer.Option(False, "--all", help="Import every discoverable Claude Code session"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional max sessions to import with --all"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raw capture"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
):
    """Import a Claude Code session."""
    lay = _layout(root)
    source = create_source("claude-code", lay)
    if all_sessions:
        _import_many(
            source,
            [
                (session.session_id, session.title or session.session_id)
                for session in source.discover()[:limit]
            ],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not session_id and not latest:
        raise typer.BadParameter("Pass --session-id <id>, --latest, or --all")
    if latest:
        sessions = source.discover()
        if not sessions:
            console.print("[red]No Claude Code sessions found.[/red]")
            raise typer.Exit(1)
        session_id = sessions[0].session_id
    assert session_id is not None  # guaranteed by the --session-id/--latest check above
    _do_import(source, session_id, force=force, lay=lay, render=not no_render)


@import_app.command("codex")
def import_codex(
    thread_id: Optional[str] = typer.Option(None, "--thread-id", help="Specific thread id"),
    latest: bool = typer.Option(False, "--latest", help="Import the most-recent thread"),
    all_sessions: bool = typer.Option(False, "--all", help="Import every discoverable Codex thread"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional max threads to import with --all"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raw capture"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
):
    """Import a Codex thread."""
    lay = _layout(root)
    source = create_source("codex", lay)
    if all_sessions:
        _import_many(
            source,
            [
                (session.session_id, session.title or session.session_id)
                for session in source.discover()[:limit]
            ],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not thread_id and not latest:
        raise typer.BadParameter("Pass --thread-id <id>, --latest, or --all")
    if latest:
        sessions = source.discover()
        if not sessions:
            console.print("[red]No Codex threads found.[/red]")
            raise typer.Exit(1)
        thread_id = sessions[0].session_id
    assert thread_id is not None  # guaranteed by the --thread-id/--latest check above
    _do_import(source, thread_id, force=force, lay=lay, render=not no_render)


@import_app.command("copilot")
def import_copilot(
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Specific chat session id"),
    latest: bool = typer.Option(False, "--latest", help="Import the most-recent Copilot chat"),
    all_sessions: bool = typer.Option(
        False,
        "--all",
        help="Import every discoverable VS Code Copilot chat session",
    ),
    limit: Optional[int] = typer.Option(None, "--limit", help="Optional max sessions to import with --all"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing raw capture"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    user_data_dir: Optional[Path] = typer.Option(
        None,
        "--user-data-dir",
        help="Explicit VS Code User data directory",
    ),
    session_state_dir: Optional[Path] = typer.Option(
        None,
        "--session-state-dir",
        help="Explicit Copilot Agent Host session-state directory",
    ),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
):
    """Import a VS Code GitHub Copilot or Agent Host session."""
    lay = _layout(root)
    source = create_source(
        "vscode-copilot",
        lay,
        user_data_dir=user_data_dir,
        session_state_dir=session_state_dir,
    )
    if all_sessions:
        _import_many(
            source,
            [
                (session.session_id, session.title or session.session_id)
                for session in source.discover()[:limit]
            ],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not session_id and not latest:
        raise typer.BadParameter("Pass --session-id <id>, --latest, or --all")
    if latest:
        sessions = source.discover()
        if not sessions:
            console.print("[red]No local VS Code Copilot sessions found.[/red]")
            raise typer.Exit(1)
        session_id = sessions[0].session_id
    assert session_id is not None
    _do_import(source, session_id, force=force, lay=lay, render=not no_render)


@import_app.command("all")
def import_all(
    force: bool = typer.Option(False, "--force", help="Overwrite existing raw captures"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
    limit_per_host: Optional[int] = typer.Option(
        None, "--limit-per-host", help="Optional max sessions per host"
    ),
):
    """Import every discoverable session from all registered source providers."""
    lay = _layout(root)
    failures: list[str] = []
    for host in source_host_names():
        try:
            source = create_source(host, lay)
            sessions = source.discover()[:limit_per_host]
        except Exception as exc:
            message = f"{host}: {type(exc).__name__}: {exc}"
            failures.append(message)
            console.print(f"[red]failed {message}[/red]")
            continue
        failures.extend(
            _import_many(
                source,
                [
                    (session.session_id, session.title or session.session_id)
                    for session in sessions
                ],
                force=force,
                lay=lay,
                render=not no_render,
                exit_on_failure=False,
            )
        )
    if failures:
        raise typer.Exit(1)


@import_app.command("renormalize")
def import_renormalize(
    root: Optional[Path] = typer.Option(None, "--root", help="rollout-memory root"),
    host: Optional[str] = typer.Option(None, "--host", help="Only rebuild this host"),
    session_id: Optional[str] = typer.Option(
        None,
        "--session",
        help="Only rebuild this archived session ID",
    ),
    since: Optional[str] = typer.Option(
        None,
        "--since",
        help="Only consider raw captures updated at or after this ISO-8601 time",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report outdated sessions without writing derived artifacts",
    ),
    no_render: bool = typer.Option(
        False,
        "--no-render",
        help="Rebuild normalized JSONL without rendered markdown",
    ),
) -> None:
    """Rebuild derived events from archived raw captures without touching raw/."""
    lay = _layout(root)
    try:
        host_full = canonical_source_host(host) if host else None
        since_value = parse_since(since)
        report = renormalize_archive(
            lay,
            host=host_full,
            session_id=session_id,
            since=since_value,
            dry_run=dry_run,
            render=not no_render,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    action = "would rebuild" if dry_run else "rebuilt"
    console.print(
        f"[bold]{action} {report.outdated if dry_run else report.rebuilt}/"
        f"{report.scanned} archived sessions[/bold]"
    )
    console.print(
        f"  skipped current: {report.skipped:,}  failures: {len(report.failures):,}"
    )
    if not dry_run:
        console.print(
            f"  events: {report.event_count:,}  unknown: "
            f"{report.unknown_event_count:,}  rendered: {report.rendered:,}"
        )
        rate = report.rebuilt / report.elapsed_seconds if report.elapsed_seconds else 0.0
        console.print(
            f"  normalized: {report.normalized_bytes:,} bytes  "
            f"elapsed: {report.elapsed_seconds:.2f}s  rate: {rate:.2f} sessions/s"
        )
    for failure in report.failures:
        console.print(
            f"[red]failed {failure.host}/{failure.session_id}: {failure.error}[/red]"
        )
    if report.failures:
        raise typer.Exit(1)


def _do_import(
    source: SourceHandle,
    identifier: str,
    *,
    force: bool,
    lay: Layout,
    render: bool,
) -> None:
    try:
        result = source.import_session(identifier=identifier, force=force)
    except FileExistsError as e:
        console.print(f"[yellow]{e}[/yellow]")
        raise typer.Exit(2) from None
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    except Exception as e:
        console.print(f"[red]{type(e).__name__}: {e}[/red]")
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
        from .signals import build_profile, session_tips

        profile = build_profile(
            key=f"{result.host}/{result.session_id}",
            host=result.host,
            session_id=result.session_id,
            events=list(read_events(result.normalized_path)),
            raw_dir=result.raw_dir,
        )
        for tip in session_tips(profile):
            console.print(f"[yellow]retro tip:[/yellow] {tip}")
    except Exception as exc:
        console.print(
            f"[yellow]operator diagnostics unavailable: "
            f"{type(exc).__name__}: {exc}[/yellow]"
        )


def _import_many(
    source: SourceHandle,
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
            _do_import(source, identifier, force=force, lay=lay, render=render)
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


def _make_extension_import_command(host: Host):
    def import_extension(
        session_id: Optional[str] = typer.Option(
            None,
            "--session-id",
            help="Specific session id",
        ),
        latest: bool = typer.Option(False, "--latest", help="Import the most-recent session"),
        all_sessions: bool = typer.Option(
            False,
            "--all",
            help="Import every discoverable session",
        ),
        limit: Optional[int] = typer.Option(
            None,
            "--limit",
            help="Optional max sessions with --all",
        ),
        force: bool = typer.Option(
            False,
            "--force",
            help="Overwrite an existing raw capture",
        ),
        root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
        no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
    ) -> None:
        lay = _layout(root)
        try:
            source = create_source(host, lay)
            sessions = source.discover()
        except Exception as exc:
            console.print(
                f"[red]Unable to load source provider {host!r}: "
                f"{type(exc).__name__}: {exc}[/red]"
            )
            raise typer.Exit(1) from None
        if all_sessions:
            _import_many(
                source,
                [
                    (session.session_id, session.title or session.session_id)
                    for session in sessions[:limit]
                ],
                force=force,
                lay=lay,
                render=not no_render,
            )
            return
        if not session_id and not latest:
            raise typer.BadParameter("Pass --session-id <id>, --latest, or --all")
        if latest:
            if not sessions:
                console.print(f"[red]No {source.display_name} sessions found.[/red]")
                raise typer.Exit(1)
            session_id = sessions[0].session_id
        assert session_id is not None
        _do_import(
            source,
            session_id,
            force=force,
            lay=lay,
            render=not no_render,
        )

    import_extension.__name__ = f"import_{host.replace('-', '_')}"
    import_extension.__doc__ = f"Import a session using the installed {host} provider."
    return import_extension


def _register_extension_import_commands() -> None:
    records = extension_records()
    collisions = {
        name
        for name, count in Counter(record.name for record in records).items()
        if count > 1
    }
    for record in records:
        if record.builtin or record.name in collisions:
            continue
        try:
            if canonical_source_host(record.name) != record.name:
                continue
        except ValueError:
            continue
        import_app.command(record.name)(_make_extension_import_command(record.name))


_register_extension_import_commands()


# ---- render / show ----------------------------------------------------------


@app.command("mine")
def mine_cmd(
    host: str = typer.Argument(..., help="registered or archived host ID, or *"),
    session_id: str = typer.Argument(..., help="session id, thread id, or *"),
    method: str = typer.Option(
        "reme_refine_poc",
        "--method",
        help="Mining method name, or `all` to run every registered method.",
    ),
    filter_names: Optional[str] = typer.Option(
        None,
        "--filter",
        help="Comma-separated list of filters to apply after mining (e.g. risk_aware).",
    ),
    all_sessions: bool = typer.Option(
        False, "--all", help="Mine all imported normalized sessions for the host"
    ),
    include_excluded: bool = typer.Option(
        False,
        "--include-excluded",
        help="Mine sessions excluded by the catalog.",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    with archive_lock(lay):
        update_catalog(lay)
    targets = _mine_targets(
        lay,
        host,
        session_id,
        all_sessions=all_sessions,
        include_excluded=include_excluded,
    )
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
    include_excluded: bool = False,
) -> list[tuple[Host, str]]:
    hosts = (
        lay.list_normalized_hosts()
        if host.lower() in ("*", "all")
        else [_expand_host(host)]
    )
    if all_sessions or session_id == "*":
        targets: list[tuple[Host, str]] = []
        for h in hosts:
            targets.extend((h, sid) for sid in lay.list_normalized(h))
        return _included_mining_targets(lay, targets, include_excluded)
    if len(hosts) != 1:
        raise typer.BadParameter("When host is *, session_id must be * or --all must be used")
    return _included_mining_targets(
        lay,
        [(hosts[0], session_id)],
        include_excluded,
    )


def _included_mining_targets(
    lay: Layout,
    targets: list[tuple[Host, str]],
    include_excluded: bool,
) -> list[tuple[Host, str]]:
    if include_excluded:
        return targets
    catalog = load_catalog(lay)
    return [
        (host, session_id)
        for host, session_id in targets
        if not (
            (record := catalog.get(f"{host}/{session_id}"))
            and record.excluded
        )
    ]


# ---- benchmark ---------------------------------------------------------------


def _print_selection_yield(counts: dict[str, Any]) -> None:
    """Yield is a pipeline metric: report selected kinds, labels and rejections."""
    for title, key in (
        ("by kind", "by_kind"),
        ("by outcome", "by_label"),
        ("rejected", "by_code"),
    ):
        values = counts.get(key) or {}
        if not values:
            continue
        rendered = ", ".join(f"{name}={value}" for name, value in values.items())
        console.print(f"  {title}: {rendered}")


@taskset_app.command("select")
def taskset_select_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    host: Optional[str] = typer.Option(
        None,
        "--host",
        help="claude|codex|copilot; omit to use selectors from the session file",
    ),
    session_file: Optional[Path] = typer.Option(
        None,
        "--session-file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="One host/session-id selector per line",
    ),
    repo_root: Optional[Path] = typer.Option(
        None,
        "--repo-root",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Current checkout for relocated trajectories; exact captured Git proof is still required",
    ),
    environment_file: Optional[Path] = typer.Option(
        None,
        "--environment-file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Validated retro-project-environment-v1 contract",
    ),
    environment_config: Optional[Path] = typer.Option(
        None,
        "--environment-config",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="External retro-environment-config-v1 to build and validate",
    ),
    container_runtime_bin: str = typer.Option(
        "docker",
        "--container-runtime-bin",
        help="Docker-compatible binary used to build and validate project environments",
    ),
    ci_base_image: Optional[str] = typer.Option(
        None,
        "--ci-base-image",
        help="Pinned image digest used for CI-derived commands",
    ),
    repolaunch_bin: Optional[Path] = typer.Option(
        None,
        "--repolaunch-bin",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Explicit RepoLaunch executable used only as the final resolver",
    ),
    build_network_allowlist: str = typer.Option(
        "",
        "--build-network-allowlist",
        help="Comma-separated environment-build egress destinations",
    ),
    build_network_name: Optional[str] = typer.Option(
        None,
        "--build-network-name",
        help="Explicit egress-filtered Docker network enforcing the build allowlist",
    ),
    allow_unvalidated_environment: bool = typer.Option(
        False,
        "--allow-unvalidated-environment",
        help="Record source eligibility without publishing buildable tasks",
    ),
    branch: str = typer.Option("HEAD", "--branch", help="Project branch containing durable outcomes"),
    stability_horizon_days: int = typer.Option(
        7,
        "--stability-horizon-days",
        min=0,
        help="Days an outcome must survive without a revert",
    ),
    sources: str = typer.Option(
        "sessions",
        "--sources",
        help="sessions|commits|both; commits mine your own Git history for task sources",
    ),
    strategy: str = typer.Option(
        "stratified",
        "--strategy",
        help="stratified (repo/work-kind/size strata, outcome-informed) or lexicographic",
    ),
    seed: int = typer.Option(0, "--seed", min=0, help="Seed for deterministic stratified sampling"),
    max_per_repo: Optional[int] = typer.Option(
        None,
        "--max-per-repo",
        min=1,
        help="Cap on selected sources per repository; defaults to 25% of --limit when limiting",
    ),
    limit: Optional[int] = typer.Option(
        None, "--limit", min=1, help="Maximum number of selected sources"
    ),
    commit_repo: Optional[list[Path]] = typer.Option(
        None,
        "--commit-repo",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Repository to mine commits from; repeatable, defaults to catalog repositories",
    ),
    commit_window_days: int = typer.Option(
        180, "--commit-window-days", min=1, help="Only mine commits from the last N days"
    ),
    commit_max_files: int = typer.Option(
        50, "--commit-max-files", min=1, help="Skip commits touching more files than this"
    ),
    commit_max_lines: int = typer.Option(
        2000, "--commit-max-lines", min=1, help="Skip commits changing more lines than this"
    ),
    commit_scan_limit: int = typer.Option(
        200, "--commit-scan-limit", min=1, help="Commits examined per repository"
    ),
    commit_link_window_hours: float = typer.Option(
        6.0,
        "--commit-link-window-hours",
        min=0.0,
        help="Link a captured session to a commit it precedes by at most this many hours",
    ),
    commit_include_merges: bool = typer.Option(
        False,
        "--commit-include-merges",
        help="Also mine merge commits, anchored on their first parent",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Select exact, clean Git-backed sources and record every rejection."""
    lay = _layout(root)
    resolved_host = (
        None
        if host is None or host in ("*", "all")
        else _expand_builtin_host(host)
    )
    if environment_file is not None and environment_config is not None:
        raise typer.BadParameter(
            "--environment-file and --environment-config are mutually exclusive"
        )
    environment_resolver: EnvironmentResolver | None = None
    if environment_file is None and not allow_unvalidated_environment:
        runtime = DockerContainerRuntime(
            binary=container_runtime_bin,
            allowlisted_network=build_network_name,
        )
        allowlist = tuple(
            destination.strip()
            for destination in build_network_allowlist.split(",")
            if destination.strip()
        )

        def automatic_environment_resolver(candidate: SourceCandidate) -> ProjectEnvironment:
            return resolve_environment(
                candidate,
                runtime=runtime,
                explicit_config=environment_config,
                repolaunch_binary=repolaunch_bin,
                ci_base_image=ci_base_image,
                network_allowlist=allowlist,
                logs_dir=(
                    lay.benchmark_taskset_dir(name)
                    / "environment-runs"
                    / candidate.source_id
                ),
            )

        environment_resolver = automatic_environment_resolver

    try:
        result = select_taskset(
            layout=lay,
            name=name,
            host=resolved_host,
            session_file=session_file,
            environment_file=environment_file,
            environment_resolver=environment_resolver,
            require_environment=not allow_unvalidated_environment,
            branch=branch,
            stability_horizon_days=stability_horizon_days,
            repo_root=repo_root,
            sources=sources,
            strategy=strategy,
            seed=seed,
            max_per_repo=max_per_repo,
            limit=limit,
            commit_repos=list(commit_repo or []) or None,
            commit_limits=CommitLimits(
                window_days=commit_window_days,
                max_files=commit_max_files,
                max_lines=commit_max_lines,
                scan_limit=commit_scan_limit,
                link_window_hours=commit_link_window_hours,
                include_merges=commit_include_merges,
            ),
        )
    except (SelectionError, SchemaError, CommitSourceError) as exc:
        console.print(f"[red]taskset selection failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    counts = result.result.to_dict()["counts"]
    console.print(
        f"[green]selected {counts['selected']} source(s); "
        f"rejected {counts['rejected']}[/green]"
    )
    _print_selection_yield(counts)
    if result.path is not None:
        console.print(f"  selection: {result.path}")


@taskset_app.command("bundle")
def taskset_bundle_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    selected_only: bool = typer.Option(
        True,
        "--selected-only/--reselect",
        help="Bundle the recorded selection or select inputs again",
    ),
    host: Optional[str] = typer.Option(None, "--host", help="Host used with --reselect"),
    repo_root: Optional[Path] = typer.Option(
        None, "--repo-root", exists=True, file_okay=False, resolve_path=True,
        help="Current checkout for relocated trajectories; defaults to the selection's explicit checkout",
    ),
    session_file: Optional[Path] = typer.Option(
        None,
        "--session-file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Session selectors used with --reselect",
    ),
    environment_file: Optional[Path] = typer.Option(
        None,
        "--environment-file",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Validated environment contract; defaults to the selection's contract",
    ),
    allow_unvalidated_environment: bool = typer.Option(
        False,
        "--allow-unvalidated-environment",
        help="Allow unvalidated diagnostic bundles that cannot be published",
    ),
    branch: str = typer.Option("HEAD", "--branch", help="Project branch containing outcomes"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Materialize deterministic immutable SourceBundles for selected rollouts."""
    lay = _layout(root)
    resolved_host = (
        None
        if host is None or host in ("*", "all")
        else _expand_builtin_host(host)
    )
    try:
        result = bundle_taskset(
            layout=lay,
            name=name,
            selected_only=selected_only,
            host=resolved_host,
            session_file=session_file,
            environment_file=environment_file,
            require_environment=not allow_unvalidated_environment,
            branch=branch,
            repo_root=repo_root,
        )
    except (BundleError, SelectionError, SchemaError) as exc:
        console.print(f"[red]taskset bundling failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    counts = result.to_dict()["counts"]
    console.print(
        f"[green]bundled or reused {counts['bundled']} source(s); "
        f"skipped {counts['skipped']}[/green]"
    )
    if result.path is not None:
        console.print(f"  report: {result.path}")


@taskset_app.command("build")
def taskset_build_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    ghostlab_bin: Optional[str] = typer.Option(
        None,
        "--ghostlab-bin",
        help="Ghostlab executable name/path; defaults to RETRO_GHOSTLAB_BIN or PATH",
    ),
    task_definer_agent: Path = typer.Option(
        ...,
        "--task-definer-agent",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    scorer_builder_agent: Path = typer.Option(
        ...,
        "--scorer-builder-agent",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    scorer_auditor_agent: Path = typer.Option(
        ...,
        "--scorer-auditor-agent",
        exists=True,
        dir_okay=False,
        resolve_path=True,
    ),
    adjacent_per_replay: int = typer.Option(
        0,
        "--adjacent-per-replay",
        min=0,
        max=1,
        help="Adjacent tasks per accepted replay task",
    ),
    source_id: Optional[list[str]] = typer.Option(
        None,
        "--source-id",
        help="Build only these source IDs; may be repeated",
    ),
    build_id: Optional[str] = typer.Option(None, "--build-id", help="Explicit resumable build ID"),
    repeatability_runs: int = typer.Option(
        3,
        "--repeatability-runs",
        min=3,
        help="Validation executions for deterministic, performance, and judge components",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Generate, lint, validate, audit, and publish hidden-scorer tasks."""
    lay = _layout(root)
    try:
        result = build_taskset(
            lay,
            name,
            ghostlab_bin,
            task_definer_agent,
            scorer_builder_agent,
            scorer_auditor_agent,
            adjacent_per_replay,
            source_ids=source_id,
            build_id=build_id,
            repeatability_runs=repeatability_runs,
        )
    except (BuildConfigurationError, GhostlabError, TaskVerificationError) as exc:
        console.print(f"[red]taskset build failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_taskset_build(result)


@taskset_app.command("curate")
def taskset_curate_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    suite_id: Optional[str] = typer.Option(
        None, "--suite", help="Immutable release ID; defaults to input hash",
    ),
    policy: Optional[Path] = typer.Option(
        None, "--policy", exists=True, dir_okay=False, resolve_path=True,
        help="Curation policy with grouped splits, task families and user priorities; default is test-only",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Freeze validated tasks into a provenance-gated train/evaluation release."""
    try:
        suite = curate_taskset(_layout(root), name, suite_id=suite_id, policy=load_policy(policy))
    except (SuiteError, SchemaError, BuildConfigurationError, GhostlabError, OSError) as exc:
        console.print(f"[red]taskset curation failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(suite.manifest, ensure_ascii=False))
    console.print(f"[green]Frozen suite:[/green] {suite.path}")


@taskset_app.command("export")
def taskset_export_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    suite_id: str = typer.Option(..., "--suite", help="Frozen curated suite"),
    output: Path = typer.Option(..., "--out", resolve_path=True, help="New immutable export directory"),
    split: str = typer.Option(
        "train", "--split", help="train|validation|test; never defaults to held-out data",
    ),
    include_rewards: bool = typer.Option(
        False, "--include-rewards",
        help="Include hidden verifiers in private/ for a trusted training harness",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Export pinned actor inputs and optional separately stored verifier packages."""
    try:
        suite = load_suite(resolve_taskset_paths(_layout(root), name), suite_id)
        manifest = export_episodes(suite, output, split=split, include_rewards=include_rewards)
    except (
        EpisodeError, SuiteError, SchemaError, TaskVerificationError,
        BuildConfigurationError, GhostlabError, OSError,
    ) as exc:
        console.print(f"[red]taskset export failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(manifest, ensure_ascii=False))
    if include_rewards:
        console.print(
            "[yellow]private/ contains hidden reward code; mount it only in the trusted scorer.[/yellow]"
        )
    console.print("[yellow]Pinned container images are referenced, not bundled in this export.[/yellow]")


@taskset_app.command("reset")
def taskset_reset_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    suite_id: str = typer.Option(..., "--suite", help="Frozen curated suite"),
    task_id: str = typer.Option(..., "--task-id", help="Task to materialize"),
    output: Path = typer.Option(..., "--out", resolve_path=True, help="New episode directory"),
    split: str = typer.Option("train", "--split", help="Required split membership: train|validation|test"),
    seed: int = typer.Option(0, "--seed", min=0, help="Episode seed"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Reset a fresh Git-free workspace; setup and commands run only through Ghostlab."""
    try:
        suite = load_suite(resolve_taskset_paths(_layout(root), name), suite_id)
        observation = materialize_episode(suite, task_id, output, split=split, seed=seed)
    except (
        EpisodeError, SuiteError, SchemaError, TaskVerificationError,
        BuildConfigurationError, GhostlabError, OSError,
    ) as exc:
        console.print(f"[red]episode reset failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(observation, ensure_ascii=False))


@taskset_app.command("reward")
def taskset_reward_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    suite_id: str = typer.Option(..., "--suite", help="Frozen curated suite"),
    task_id: str = typer.Option(..., "--task-id", help="Task to grade"),
    candidate: Path = typer.Option(
        ..., "--candidate", exists=True, resolve_path=True,
        help="Terminal workspace or exported candidate archive",
    ),
    output: Path = typer.Option(..., "--out", resolve_path=True, help="New private reward-run directory"),
    split: str = typer.Option("train", "--split", help="Required split membership: train|validation|test"),
    seed: int = typer.Option(0, "--seed", min=0, help="Episode seed"),
    ghostlab_bin: Optional[str] = typer.Option(None, "--ghostlab-bin", help="Ghostlab executable"),
    trace: Optional[Path] = typer.Option(
        None, "--trace", exists=True, dir_okay=False, resolve_path=True,
        help="Optional terminal-agent event trace",
    ),
    resources: Optional[Path] = typer.Option(
        None, "--resources", exists=True, dir_okay=False, resolve_path=True,
        help="Optional recorded resource usage passed to the scorer",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Return an attested terminal reward; infrastructure failure has no numeric reward."""
    try:
        suite = load_suite(resolve_taskset_paths(_layout(root), name), suite_id)
        reward = score_episode(
            suite, task_id, candidate, output, split=split, seed=seed, ghostlab_bin=ghostlab_bin,
            trace_path=trace, resource_usage_path=resources,
        )
    except (
        EpisodeError, SuiteError, SchemaError, TaskVerificationError,
        BuildConfigurationError, GhostlabError, OSError,
    ) as exc:
        console.print(f"[red]episode reward failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print_json(json.dumps(reward, ensure_ascii=False))
    if reward["status"] != "scored":
        raise typer.Exit(1)


@taskset_app.command("run")
def taskset_run_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    agent: Path = typer.Option(
        ...,
        "--agent",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Candidate Ghostlab agent configuration",
    ),
    seeds: str = typer.Option("0", "--seeds", help="Comma-separated non-negative seeds"),
    ghostlab_bin: Optional[str] = typer.Option(
        None,
        "--ghostlab-bin",
        help="Ghostlab executable name/path; defaults to RETRO_GHOSTLAB_BIN or PATH",
    ),
    eval_id: Optional[str] = typer.Option(
        None,
        "--eval",
        help="Evaluation ID; latest continues the latest, new starts a fresh evaluation",
    ),
    task_id: Optional[list[str]] = typer.Option(
        None,
        "--task-id",
        help="Run only these task IDs; may be repeated",
    ),
    suite_id: Optional[str] = typer.Option(
        None, "--suite", help="Evaluate immutable curated inputs rather than the active task pool",
    ),
    split: Optional[str] = typer.Option(
        None, "--split", help="Curated split: train|validation|test; --suite defaults to test",
    ),
    force: bool = typer.Option(False, "--force", help="Re-run existing hash-identical attempts"),
    token_budget: Optional[list[float]] = typer.Option(
        None,
        "--token-budget",
        min=0,
        help="Report score under this token budget; may be repeated",
    ),
    wall_time_budget_ms: Optional[list[float]] = typer.Option(
        None,
        "--wall-time-budget-ms",
        min=0,
        help="Report score under this wall-time budget; may be repeated",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Run one candidate agent on fresh task bases and score every attempt."""
    lay = _layout(root)
    try:
        result = run_taskset(
            lay,
            name,
            agent,
            seeds,
            ghostlab_bin,
            eval_id=eval_id,
            task_ids=task_id,
            force=force,
            token_budgets=tuple(token_budget or ()),
            wall_time_budgets_ms=tuple(wall_time_budget_ms or ()),
            suite_id=suite_id,
            split=split,
        )
    except (TaskVerificationError, BuildConfigurationError, GhostlabError, SuiteError) as exc:
        console.print(f"[red]taskset run failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_taskset_run(result)


@taskset_app.command("report")
def taskset_report_cmd(
    name: str = typer.Option(..., "--name", help="Taskset name"),
    eval_id: str = typer.Option("latest", "--eval", help="Evaluation ID or latest"),
    token_budget: Optional[list[float]] = typer.Option(
        None,
        "--token-budget",
        min=0,
        help="Report score under this token budget; may be repeated",
    ),
    wall_time_budget_ms: Optional[list[float]] = typer.Option(
        None,
        "--wall-time-budget-ms",
        min=0,
        help="Report score under this wall-time budget; may be repeated",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the complete report as JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Render source-normalized scores and invalid-run accounting."""
    lay = _layout(root)
    try:
        result = report_taskset(
            lay,
            name,
            eval_id,
            token_budgets=tuple(token_budget or ()),
            wall_time_budgets_ms=tuple(wall_time_budget_ms or ()),
        )
    except (TaskVerificationError, BuildConfigurationError, SuiteError) as exc:
        console.print(f"[red]taskset report failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    if json_output:
        console.print_json(json.dumps(result.to_dict(), ensure_ascii=False))
    else:
        _print_taskset_report(result)


def _print_rows(title: str, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    table = Table(title=title)
    columns = list(rows[0])
    for column in columns:
        table.add_column(column.replace("_", " "))
    for row in rows:
        table.add_row(*(str(row.get(column, "")) for column in columns))
    console.print(table)


def _print_taskset_build(result: TasksetBuildSummary) -> None:
    console.print(
        f"[green]published {result.published} task(s) from "
        f"{result.sources_ok}/{result.sources_total} source(s)[/green]"
    )
    _print_rows("Taskset build sources", result.source_rows())
    _print_rows("Published tasks", result.task_rows())
    _print_rows("Rejections", result.rejection_rows())
    console.print(f"  build:  {result.build_dir}")
    console.print(f"  report: {result.report_path}")


def _print_taskset_run(result: TasksetRunSummary) -> None:
    score = "n/a" if result.benchmark_score is None else f"{result.benchmark_score:.3f}"
    console.print(
        f"[green]evaluation {result.eval_id}: score {score}, "
        f"coverage {result.coverage:.1%}, reused {result.reused_attempts}[/green]"
    )
    _print_rows("Attempts", result.attempt_rows())
    _print_rows("Attempt errors", result.error_rows())
    console.print(f"  report: {result.results_path}")


def _print_taskset_report(result: TasksetReportSummary) -> None:
    _print_rows(f"Taskset report: {result.eval_id}", result.agent_rows())
    _print_rows("Sources", result.source_rows())
    _print_rows("Tasks", result.task_rows())
    _print_rows("Components", result.component_rows())
    _print_rows("Resources", result.resource_rows())
    _print_rows("Budget conditionals", result.budget_rows())
    _print_rows("Invalid attempts", result.error_rows())
    console.print(f"[green]wrote report to {result.results_path}[/green]")


def _print_experiment(record: dict[str, Any], json_output: bool) -> None:
    from .dashboard.benchmarks import project_experiment

    public = project_experiment(record)
    if json_output:
        console.print_json(json.dumps(public, ensure_ascii=False))
        return
    console.print(f"experiment: {public['experiment_id']} · {public['status']}")
    console.print(f"  {public['title']}")
    console.print(
        f"  datasets={len(public['datasets'])} agents={len(public['agents'])} "
        f"requested trials={public['requested_attempts']} seeds={','.join(map(str, public['seeds']))}"
    )
    if public["error"]:
        console.print(f"[red]  {public['error']}[/red]")
    if public["cleanup"]:
        console.print(f"  cleanup: {json.dumps(public['cleanup'], ensure_ascii=False)}")


@experiment_app.command("create")
def experiment_create_cmd(
    title: str = typer.Option(..., "--title", help="Name of the experiment"),
    dataset: list[str] = typer.Option(
        ..., "--dataset", help="NAME/SUITE or NAME/SUITE/SPLIT; may be repeated",
    ),
    agent: list[Path] = typer.Option(
        ..., "--agent", exists=True, dir_okay=False, resolve_path=True,
        help="Candidate Ghostlab config; may be repeated",
    ),
    description: str = typer.Option("", "--description", help="Question or hypothesis being tested"),
    seeds: str = typer.Option("0,1,2", "--seeds", help="Comma-separated distinct non-negative seeds"),
    experiment_id: Optional[str] = typer.Option(None, "--id", help="Optional path-safe experiment ID"),
    agent_timeout: Optional[float] = typer.Option(None, "--agent-timeout", min=0.001),
    scorer_timeout: Optional[float] = typer.Option(None, "--scorer-timeout", min=0.001),
    json_output: bool = typer.Option(False, "--json", help="Emit safe setup/progress JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Save an immutable plan; this command never starts a model."""
    from .benchmarks.task_scorer.experiments import ExperimentError, create_experiment
    from .benchmarks.task_scorer.run import parse_seeds

    selections = []
    for value in dataset:
        parts = value.split("/")
        if len(parts) not in (2, 3) or not all(parts):
            raise typer.BadParameter("dataset must be NAME/SUITE[/SPLIT]")
        selections.append({
            "name": parts[0], "suite_id": parts[1], "split": parts[2] if len(parts) == 3 else "test",
        })
    try:
        record = create_experiment(
            _layout(root), title=title, description=description, datasets=selections,
            agents=[AgentSpec.from_path(path) for path in agent], seeds=parse_seeds(seeds),
            experiment_id=experiment_id, aut_timeout_seconds=agent_timeout,
            scorer_timeout_seconds=scorer_timeout,
        )
    except (ExperimentError, TaskVerificationError, GhostlabError, OSError) as exc:
        console.print(f"[red]experiment planning failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_experiment(record, json_output)
    if not json_output:
        console.print("Plan saved. No model was called. Use retro experiment run to explicitly start it.")


@experiment_app.command("list")
def experiment_list_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit safe job summaries"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """List experiment jobs separately from task datasets."""
    from .benchmarks.task_scorer.experiments import ExperimentError, list_experiments
    from .dashboard.benchmarks import project_experiment

    try:
        records = list_experiments(_layout(root))
    except (ExperimentError, OSError) as exc:
        console.print(f"[red]experiment listing failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    if json_output:
        console.print_json(json.dumps([project_experiment(record) for record in records], ensure_ascii=False))
    else:
        for record in records:
            _print_experiment(record, False)
        if not records:
            console.print("No experiment jobs.")


@experiment_app.command("show")
def experiment_show_cmd(
    experiment_id: str = typer.Argument(..., help="Saved experiment ID"),
    json_output: bool = typer.Option(False, "--json", help="Emit safe setup/progress JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Inspect a frozen job and its progress without starting agents."""
    from .benchmarks.task_scorer.experiments import ExperimentError, load_experiment

    try:
        record = load_experiment(_layout(root), experiment_id)
    except (ExperimentError, OSError) as exc:
        console.print(f"[red]experiment inspection failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_experiment(record, json_output)


@experiment_app.command("run")
def experiment_run_cmd(
    experiment_id: str = typer.Argument(..., help="Saved experiment ID"),
    ghostlab_bin: Optional[str] = typer.Option(None, "--ghostlab-bin", help="Ghostlab executable name/path"),
    json_output: bool = typer.Option(False, "--json", help="Emit safe setup/progress JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Explicitly start/resume a planned job; model charges may apply."""
    from .benchmarks.task_scorer.experiments import ExperimentError, run_experiment

    try:
        record = run_experiment(_layout(root), experiment_id, ghostlab=ghostlab_bin)
    except (ExperimentError, TaskVerificationError, GhostlabError, OSError) as exc:
        console.print(f"[red]experiment run failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_experiment(record, json_output)
    if record["status"] == "failed" or record.get("summary", {}).get("error_attempts", 0):
        raise typer.Exit(1)


@experiment_app.command("cleanup")
def experiment_cleanup_cmd(
    experiment_id: str = typer.Argument(..., help="Saved finished/interrupted experiment ID"),
    prune_artifacts: bool = typer.Option(
        False, "--prune-artifacts", help="Also delete this job's owned bulky candidate exports",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit safe cleanup/progress JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Clean only owned experiment artifacts; retain task bundles, grades and evidence."""
    from .benchmarks.task_scorer.experiments import ExperimentError, cleanup_experiment

    try:
        record = cleanup_experiment(_layout(root), experiment_id, prune_artifacts=prune_artifacts)
    except (ExperimentError, OSError) as exc:
        console.print(f"[red]experiment cleanup failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    _print_experiment(record, json_output)
    if record.get("cleanup", {}).get("errors"):
        raise typer.Exit(1)


@benchmark_app.command("build")
def benchmark_build_cmd(
    benchmark_id: str = typer.Argument(..., help="Immutable benchmark version identifier"),
    project: Path = typer.Option(
        ...,
        "--project",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Local git repository represented by the rollout tasks",
    ),
    cutoff: str = typer.Option(
        ...,
        "--cutoff",
        help="Timezone-aware T0; knowledge and repository state stop here",
    ),
    end: str = typer.Option(
        ...,
        "--end",
        help="Timezone-aware inclusive T1 for generated task episodes",
    ),
    host: str = typer.Option(
        "*",
        "--host",
        help="claude|codex|copilot|*",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Generate four prompt variants and hidden file truth from future rollout episodes."""
    lay = _layout(root)
    result = build_time_consistent_benchmark(
        lay,
        benchmark_id=benchmark_id,
        project_root=project,
        cutoff_time=cutoff,
        end_time=end,
        hosts=_expand_builtin_hosts(host),
    )
    console.print(
        f"[green]built {result.benchmark_id} with {result.task_count} tasks[/green]"
    )
    console.print(f"  snapshot:    {result.snapshot_commit}")
    console.print(f"  benchmark:   {result.path}")
    console.print(f"  diagnostics: {result.observed_predictions_path}")


@benchmark_app.command("evaluate")
def benchmark_evaluate_cmd(
    benchmark_id: str = typer.Argument(..., help="Benchmark version identifier"),
    predictions: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="JSONL file containing predicted repository-relative file sets",
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Immutable run identifier; defaults to the current UTC timestamp",
    ),
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help="Allow a condition/model/prompt group to omit benchmark tasks",
    ),
    baseline_condition: str = typer.Option(
        "baseline",
        "--baseline-condition",
        help="Condition name used as the matched-comparison baseline",
    ),
    augmented_condition: str = typer.Option(
        "augmented",
        "--augmented-condition",
        help="Condition name used as the matched-comparison treatment",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Score file predictions with exact precision, recall, F1, and matched deltas."""
    lay = _layout(root)
    result = evaluate_time_consistent_benchmark(
        lay,
        benchmark_id=benchmark_id,
        predictions_path=predictions,
        run_id=run_id,
        allow_partial=allow_partial,
        baseline_condition=baseline_condition,
        augmented_condition=augmented_condition,
    )
    _print_benchmark_evaluation(result)


@benchmark_app.command("run")
def benchmark_run_cmd(
    benchmark_id: str = typer.Argument(..., help="Benchmark version identifier"),
    model: str = typer.Option(..., "--model", help="GitHub Copilot model identifier"),
    run_id: str = typer.Option(..., "--run-id", help="Immutable evaluation run identifier"),
    prompt_level: str = typer.Option(
        "contextual",
        "--prompt-level",
        help="minimal|concise|contextual|guided",
    ),
    condition: str = typer.Option(
        "baseline",
        "--condition",
        help="Condition label stored with every prediction",
    ),
    workers: int = typer.Option(
        2,
        "--workers",
        min=1,
        help="Concurrent OpenShell sandboxes",
    ),
    timeout_seconds: int = typer.Option(
        600,
        "--timeout",
        min=1,
        help="Per-task model timeout in seconds",
    ),
    attempts: int = typer.Option(
        1,
        "--attempts",
        min=1,
        help="Maximum independent sandbox attempts per task",
    ),
    reasoning_effort: str = typer.Option(
        "medium",
        "--effort",
        help="Copilot reasoning effort",
    ),
    context: str = typer.Option(
        "default",
        "--context",
        help="Copilot context tier",
    ),
    credential_env: str = typer.Option(
        "COPILOT_GITHUB_TOKEN",
        "--credential-env",
        help="Allowlisted environment variable containing a Copilot-capable token",
    ),
    use_git_credential: bool = typer.Option(
        False,
        "--use-git-credential",
        help="Explicitly request a GitHub token from Git's credential helper",
    ),
    sandbox_image: Optional[Path] = typer.Option(
        None,
        "--sandbox-image",
        exists=True,
        dir_okay=False,
        resolve_path=True,
        help="Override the packaged OpenShell Copilot Dockerfile",
    ),
    cpu: str = typer.Option("2", "--cpu", help="CPU limit for each sandbox"),
    memory: str = typer.Option("4Gi", "--memory", help="Memory limit for each sandbox"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Run benchmark tasks through GitHub Copilot inside GhostLab OpenShell."""
    lay = _layout(root)
    result = run_ghostlab_benchmark(
        lay,
        benchmark_id=benchmark_id,
        run_id=run_id,
        model=model,
        prompt_level=prompt_level,
        condition=condition,
        workers=workers,
        timeout_seconds=timeout_seconds,
        attempts=attempts,
        reasoning_effort=reasoning_effort,
        context=context,
        credential_env=credential_env,
        use_git_credential=use_git_credential,
        sandbox_image=sandbox_image,
        cpu=cpu,
        memory=memory,
    )
    _print_benchmark_evaluation(result.evaluation)
    console.print(
        f"[green]completed {result.task_count} independent GhostLab/OpenShell tasks[/green]"
    )


def _print_benchmark_evaluation(result: BenchmarkEvaluationResult) -> None:
    table = Table(title=f"Benchmark evaluation: {result.run_id}")
    for column in ("condition", "model", "prompt", "tasks", "macro F1", "exact match"):
        table.add_column(column)
    for aggregate in result.aggregate:
        table.add_row(
            aggregate["condition"],
            aggregate["model"],
            aggregate["prompt_level"],
            str(aggregate["task_count"]),
            f"{aggregate['macro_f1']:.3f}",
            f"{aggregate['exact_match_rate']:.3f}",
        )
    console.print(table)
    for comparison in result.paired_comparisons:
        console.print(
            "  matched delta "
            f"{comparison['model']}/{comparison['prompt_level']}: "
            f"{comparison['mean_file_f1_delta']:+.3f} "
            f"({comparison['matched_task_count']} tasks)"
        )
    console.print(f"[green]wrote immutable run to {result.path}[/green]")


@benchmark_app.command("list")
def benchmark_list_cmd(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """List locally generated benchmark versions."""
    lay = _layout(root)
    table = Table(title="Benchmarks")
    table.add_column("id")
    table.add_column("method")
    table.add_column("tasks")
    table.add_column("cutoff")
    for benchmark_dir in sorted(path for path in lay.benchmarks_dir().iterdir() if path.is_dir()):
        manifest_path = benchmark_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            console.print(
                f"[yellow]skipping invalid benchmark manifest {manifest_path}: {exc}[/yellow]"
            )
            continue
        if not isinstance(manifest, dict):
            console.print(
                f"[yellow]skipping non-object benchmark manifest {manifest_path}[/yellow]"
            )
            continue
        source = manifest.get("source")
        source = source if isinstance(source, dict) else {}
        temporal = manifest.get("temporal_contract")
        temporal = temporal if isinstance(temporal, dict) else {}
        legacy_contract = manifest.get("contract")
        legacy_contract = legacy_contract if isinstance(legacy_contract, dict) else {}
        table.add_row(
            str(manifest.get("benchmark_id", benchmark_dir.name)),
            str(manifest.get("method", "unknown")),
            str(source.get("task_count", "?")),
            str(
                temporal.get(
                    "knowledge_and_snapshot_at_or_before",
                    legacy_contract.get("cutoff_time", "?"),
                )
            ),
        )
    console.print(table)


@app.command("render")
def render_cmd(
    host: str = typer.Argument(..., help="registered or archived host ID"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    include_noise: bool = typer.Option(
        False,
        "--include-noise",
        help="Include lifecycle, metadata, and usage telemetry",
    ),
):
    """Re-render markdown from already-imported normalized events."""
    lay = _layout(root)
    host_full = _expand_host(host)
    normalized = lay.normalized_path(host_full, session_id)
    if not normalized.exists():
        console.print(f"[red]No normalized events at {normalized}[/red]")
        raise typer.Exit(1)
    dest = lay.rendered_path(host_full, session_id)
    n = render_file(normalized, dest, include_noise=include_noise)
    console.print(f"rendered {dest}  ({n:,} bytes)")


@app.command("show")
def show_cmd(
    host: str = typer.Argument(..., help="registered or archived host ID"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
):
    """Show artifact paths and basic stats for an imported session."""
    lay = _layout(root)
    host_full = _expand_host(host)
    raw_dir = lay.raw_dir(host_full, session_id)
    normalized = lay.normalized_path(host_full, session_id)
    rendered = lay.rendered_path(host_full, session_id)
    if not any((raw_dir.exists(), normalized.exists(), rendered.exists())):
        console.print(f"[red]No artifacts found for {host_full}/{session_id}[/red]")
        raise typer.Exit(1)

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
    try:
        return canonical_source_host(host)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _expand_builtin_host(host: str) -> Host:
    resolved = _expand_host(host)
    if resolved not in BUILTIN_HOSTS:
        expected = "|".join(BUILTIN_HOSTS)
        raise typer.BadParameter(
            f"unsupported benchmark host {host!r}; use {expected}"
        )
    return resolved


def _expand_builtin_hosts(host: str) -> list[Host]:
    h = host.lower()
    if h in ("*", "all"):
        return list(BUILTIN_HOSTS)
    return [_expand_builtin_host(host)]


# ---- global config / archive / sync -----------------------------------------


@config_app.command("show")
def config_show() -> None:
    """Show effective per-user paths and periodic settings."""
    config = load_config()
    table = Table(title=f"Retro configuration ({config_path()})")
    table.add_column("setting")
    table.add_column("value")
    for key, value in config.to_dict().items():
        table.add_row(key, str(value))
    console.print(table)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ...,
        help="archive-root|dashboard-dir|sync-interval|derived-interval|sync-on-login",
    ),
    value: str = typer.Argument(...),
) -> None:
    """Update one per-user setting."""
    normalized = key.lower().replace("_", "-")
    updates: dict[str, object]
    if normalized == "archive-root":
        updates = {"archive_root": str(Path(value).expanduser().resolve())}
    elif normalized == "dashboard-dir":
        updates = {"dashboard_dir": str(Path(value).expanduser().resolve())}
    elif normalized == "sync-interval":
        from .schedule import parse_interval

        updates = {"sync_interval_seconds": parse_interval(value)}
    elif normalized == "derived-interval":
        from .schedule import parse_interval

        updates = {"derived_interval_seconds": parse_interval(value)}
    elif normalized == "sync-on-login":
        lowered = value.lower()
        if lowered not in {"true", "false", "yes", "no", "1", "0"}:
            raise typer.BadParameter("sync-on-login must be true or false")
        updates = {"sync_on_login": lowered in {"true", "yes", "1"}}
    else:
        raise typer.BadParameter(
            "key must be archive-root, dashboard-dir, sync-interval, "
            "derived-interval, or sync-on-login"
        )
    update_config(**updates)
    path = config_path()
    console.print(f"[green]updated[/green] {path}")


@app.command("setup")
def setup_cmd(
    archive_root: Optional[Path] = typer.Option(
        None,
        "--archive-root",
        help="Global archive root",
    ),
    dashboard_dir: Optional[Path] = typer.Option(
        None,
        "--dashboard-dir",
        help="Generated dashboard directory",
    ),
    periodic: str = typer.Option("15m", "--periodic", help="Capture interval, e.g. 15m or 1h"),
    derived_every: str = typer.Option(
        "6h",
        "--derived-every",
        help="Minimum interval between scheduled signal/dashboard rebuilds",
    ),
    no_schedule: bool = typer.Option(False, "--no-schedule", help="Save config without launchd"),
) -> None:
    """Configure global storage and optionally install periodic capture."""
    from .archive_health import archive_access
    from .schedule import install_schedule, parse_interval

    current = load_config()
    interval = parse_interval(periodic)
    derived_interval = parse_interval(derived_every)
    config = dataclasses.replace(
        current,
        archive_root=str((archive_root or Path(current.archive_root)).expanduser().resolve()),
        dashboard_dir=str((dashboard_dir or Path(current.dashboard_dir)).expanduser().resolve()),
        sync_interval_seconds=interval,
        derived_interval_seconds=derived_interval,
        sync_on_login=True,
    )
    for label, target in (
        ("archive", Path(config.archive_root)),
        ("dashboard", Path(config.dashboard_dir)),
    ):
        access = archive_access(target)
        if not access.mounted or not access.accessible:
            raise typer.BadParameter(
                access.message or f"{label} path is not accessible: {target}"
            )
    path = save_config(config)
    Layout(Path(config.archive_root)).ensure()
    Path(config.dashboard_dir).mkdir(parents=True, exist_ok=True)
    console.print(f"[green]configured Retro:[/green] {path}")
    console.print(f"  archive:   {config.archive_root}")
    console.print(f"  dashboard: {config.dashboard_dir}")
    if not no_schedule:
        plist = install_schedule(interval)
        console.print(f"  schedule:  {plist}  (every {interval}s)")


@archive_app.command("plan")
def archive_plan(
    sources: list[Path] = typer.Option(..., "--from", help="Source rollout-memory root"),
    into: Path = typer.Option(..., "--into", help="Canonical destination root"),
    experiments_from: Optional[list[Path]] = typer.Option(
        None,
        "--experiments-from",
        help="Repository root containing logs/ and root evaluation JSON",
    ),
    dashboard_dir: Optional[Path] = typer.Option(None, "--dashboard-dir"),
) -> None:
    """Create a checksummed migration plan without copying archive data."""
    from .archive import create_migration_plan

    plan = create_migration_plan(
        sources,
        into,
        experiment_roots=experiments_from,
        dashboard_dir=dashboard_dir,
    )
    data = json.loads(plan.read_text(encoding="utf-8"))
    console.print(f"[green]migration plan:[/green] {plan}")
    console.print(json.dumps(data["summary"], indent=2))


@archive_app.command("migrate")
def archive_migrate(
    plan: Path = typer.Option(..., "--plan"),
    rebuild: bool = typer.Option(False, "--rebuild-derived"),
) -> None:
    """Execute a migration plan using atomic, checksummed copies."""
    from .archive import execute_migration, rebuild_derived_artifacts

    result = execute_migration(plan)
    console.print(f"[green]copied migration {result['migration_id']}[/green]")
    console.print(json.dumps(result["summary"], indent=2))
    if rebuild:
        report = rebuild_derived_artifacts(plan)
        console.print(
            f"[green]rebuilt derived artifacts for "
            f"{report['rendered_sessions']} sessions[/green]"
        )


@archive_app.command("verify")
def archive_verify(
    plan: Path = typer.Option(..., "--plan"),
) -> None:
    """Verify migrated checksums, event references, sessions, and memory DB."""
    from .archive import verify_migration

    report = verify_migration(plan)
    console.print(json.dumps(report, indent=2))
    if not report["ok"]:
        raise typer.Exit(1)


@archive_app.command("cutover")
def archive_cutover(
    plan: Path = typer.Option(..., "--plan"),
    link_path: Path = typer.Option(..., "--link-path"),
) -> None:
    """Replace a verified legacy archive directory with a compatibility symlink."""
    from .archive import cutover_compatibility_link

    link = cutover_compatibility_link(plan, link_path)
    console.print(f"[green]compatibility path ready:[/green] {link} -> {link.resolve()}")


@archive_app.command("repair-refs")
def archive_repair_refs(
    root: Optional[Path] = typer.Option(None, help="Override configured archive root"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without rewriting files"),
) -> None:
    """Make archive-internal normalized raw references relocatable."""
    from .archive import repair_archive_refs

    report = repair_archive_refs(default_layout(root), dry_run=dry_run)
    console.print_json(data=report)
    if not report["ok"]:
        raise typer.Exit(1)


@app.command("sync")
def sync_cmd(
    root: Optional[Path] = typer.Option(None, help="Override configured archive root"),
    dashboard_dir: Optional[Path] = typer.Option(None, "--dashboard-dir"),
    scheduled: bool = typer.Option(False, "--scheduled", hidden=True),
    force_derived: bool = typer.Option(
        False,
        "--force-derived",
        help="Recompute all signals, memory, and dashboard data",
    ),
) -> None:
    """Capture changed local sessions and refresh derived portfolio artifacts."""
    from .sync import run_sync

    report = run_sync(
        default_layout(root),
        dashboard_dir=dashboard_dir,
        scheduled=scheduled,
        force_derived=force_derived,
    )
    console.print(
        f"[{'green' if report.status == 'success' else 'yellow'}]"
        f"sync {report.status}[/]"
    )
    console.print(
        f"  imported={len(report.imported)} unchanged={len(report.unchanged)} "
        f"failures={len(report.failures)}"
    )
    if report.warning:
        console.print(f"[yellow]{report.warning}[/yellow]")
    for failure in report.failures:
        console.print(f"[red]{failure['session']}: {failure['error']}[/red]")
    if report.status == "skipped" and report.skip_reason:
        console.print(f"[yellow]{report.skip_reason}[/yellow]")
    if report.status not in {"success"} and not (
        scheduled and report.status == "skipped"
    ):
        raise typer.Exit(1)


@schedule_app.command("install")
def schedule_install(
    every: str = typer.Option("15m", "--every"),
) -> None:
    """Install or replace the current user's macOS LaunchAgent."""
    from .schedule import install_schedule, parse_interval

    interval = parse_interval(every)
    update_config(sync_interval_seconds=interval)
    path = install_schedule(interval)
    console.print(f"[green]schedule installed:[/green] {path}")


@schedule_app.command("status")
def schedule_status_cmd() -> None:
    """Show periodic capture installation and loaded state."""
    from .schedule import schedule_status

    console.print_json(data=schedule_status())


@schedule_app.command("run-now")
def schedule_run_now() -> None:
    """Ask launchd to start the periodic capture job now."""
    from .schedule import run_now

    run_now()
    console.print("[green]scheduled sync started[/green]")


@schedule_app.command("uninstall")
def schedule_uninstall() -> None:
    """Unload and remove Retro's user LaunchAgent."""
    from .schedule import uninstall_schedule

    path = uninstall_schedule()
    console.print(f"[green]schedule removed:[/green] {path}")


@app.command("doctor")
def doctor_cmd() -> None:
    """Report global archive, source, disk, and scheduler health."""
    from .archive_health import (
        archive_access,
        duplicate_volume_mounts,
        scheduled_archive_access,
    )
    from .schedule import schedule_status
    from .sync import STAGE_NAMES

    layout = default_layout()
    dashboard = resolve_dashboard_dir()
    access = archive_access(layout.root)
    scheduled_access = scheduled_archive_access(layout.root)
    errors: list[str] = []
    warnings: list[str] = []
    table = Table(title="Retro global archive")
    table.add_column("check")
    table.add_column("value")
    table.add_row("config", str(config_path()))
    table.add_row("archive", str(layout.root))
    table.add_row("dashboard", str(dashboard))
    table.add_row("archive volume", access.volume or "internal filesystem")
    table.add_row("archive volume mounted", str(access.mounted))
    table.add_row("scheduled archive access", str(scheduled_access["accessible"]))
    table.add_row("scheduled access evidence", str(scheduled_access["evidence"]))
    if access.message:
        table.add_row("archive access detail", access.message)
    if scheduled_access.get("message") and scheduled_access["message"] != access.message:
        table.add_row("scheduled access detail", str(scheduled_access["message"]))
    if not access.mounted or not access.accessible:
        errors.append(access.message or "archive is unavailable")
    elif not scheduled_access["accessible"]:
        errors.append(str(scheduled_access["message"]))
    if access.volume:
        duplicates = duplicate_volume_mounts(Path(access.volume))
        if duplicates:
            message = (
                f"Expected {access.volume}, but duplicate mounted volume(s) exist: "
                + ", ".join(str(path) for path in duplicates)
            )
            table.add_row("duplicate volume mounts", f"ERROR: {message}")
            errors.append(message)
    table.add_row("archive state", str(layout.state_dir()))
    table.add_row("archive lock", str(layout.lock_path()))
    table.add_row("sync state", str(layout.sync_state_path()))
    if access.mounted and access.accessible:
        usage_path = Path(access.volume) if access.volume else layout.root.parent
        while not usage_path.exists() and usage_path.parent != usage_path:
            usage_path = usage_path.parent
        free = shutil.disk_usage(usage_path).free
        table.add_row("free space", f"{free / 1024**3:.1f} GiB")
        normalized_count = (
            sum(
                len(layout.list_normalized(host))
                for host in layout.list_normalized_hosts()
            )
            if layout.root.is_dir()
            else 0
        )
        table.add_row("normalized sessions", str(normalized_count))
        try:
            sync_state = json.loads(
                layout.sync_state_path().read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            sync_state = {}
        except (OSError, json.JSONDecodeError) as exc:
            sync_state = {}
            message = f"cannot read sync state: {type(exc).__name__}: {exc}"
            warnings.append(message)
            table.add_row("sync state detail", f"WARNING: {message}")
        stages = sync_state.get("stages") if isinstance(sync_state, dict) else {}
        for name in STAGE_NAMES:
            record = stages.get(name) if isinstance(stages, dict) else None
            value = (
                record.get("status", "never run")
                if isinstance(record, dict)
                else "never run"
            )
            table.add_row(f"last sync {name}", str(value))
    legacy_root = user_data_dir() / "rollout-memory"
    if legacy_root != layout.root and legacy_root.is_dir():
        try:
            has_legacy_data = next(legacy_root.iterdir(), None) is not None
        except OSError:
            has_legacy_data = True
        if has_legacy_data:
            message = f"legacy archive data remains at {legacy_root}"
            warnings.append(message)
            table.add_row("legacy archive", f"WARNING: {message}")
    stale_dashboard = (
        sorted(dashboard.parent.glob(".retro-dashboard-*"))
        if dashboard.parent.is_dir()
        else []
    )
    if stale_dashboard:
        message = ", ".join(str(path) for path in stale_dashboard)
        warnings.append(f"stale dashboard staging directories: {message}")
        table.add_row("dashboard staging", f"WARNING: {message}")
    for result in doctor_sources():
        table.add_row(
            f"source provider {result.record.name}",
            "ok" if result.ok else f"ERROR: {result.message}",
        )
        if not result.ok:
            errors.append(
                f"{result.record.name} provider failed: {result.message}"
            )
    status = schedule_status()
    table.add_row("schedule installed", str(status.get("installed")))
    table.add_row("schedule loaded", str(status.get("loaded")))
    table.add_row("schedule logs", str(user_log_dir()))
    for warning in warnings:
        table.add_row("warning", warning)
    console.print(table)
    if errors:
        raise typer.Exit(1)


# ---- signals ----------------------------------------------------------------


@signal_app.command("list")
def signal_list(
    category: Optional[str] = typer.Option(
        None,
        "--category",
        help="Filter by category: usage|activity|outcome|friction|efficiency|collaboration|risk|quality|work",
    ),
    describe: bool = typer.Option(
        False, "--describe", help="Include the full definition of each signal"
    ),
):
    """List registered signals with their metadata."""
    table = Table(
        title=f"Signals ({len(SIGNAL_REGISTRY)} registered, set {definitions_version()})"
    )
    table.add_column("id", no_wrap=True, style="cyan")
    table.add_column("category", no_wrap=True)
    table.add_column("kind", no_wrap=True)
    table.add_column("unit", no_wrap=True)
    table.add_column("polarity", no_wrap=True)
    table.add_column("method", no_wrap=True)
    table.add_column("v", justify="right", no_wrap=True)
    if describe:
        table.add_column("description", overflow="fold", ratio=2)
    for signal_id in sorted(SIGNAL_REGISTRY):
        definition = SIGNAL_REGISTRY[signal_id]
        if category and definition.category != category:
            continue
        row = [
            definition.id,
            definition.category,
            definition.kind,
            definition.unit or "",
            definition.polarity,
            definition.method,
            str(definition.version),
        ]
        if describe:
            row.append(definition.description)
        table.add_row(*row)
    console.print(table)


class _CatalogSlice:
    """Included catalog records, optionally narrowed to one host or session list."""

    def __init__(
        self,
        catalog: Catalog,
        *,
        host: str | None,
        session_ids: list[str] | None,
    ) -> None:
        self._catalog = catalog
        self._host = host
        self._session_ids = set(session_ids) if session_ids else None

    def included(self) -> Iterator[SessionRecord]:
        for record in self._catalog.included():
            if self._host is not None and record.host != self._host:
                continue
            if self._session_ids is not None and record.session_id not in self._session_ids:
                continue
            yield record


@signal_app.command("run")
def signal_run(
    host: Optional[str] = typer.Option(None, help="Restrict to one host ID"),
    session_id: Optional[str] = typer.Option(
        None, "--session-id", help="Restrict to session ids (comma separated)"
    ),
    force: bool = typer.Option(False, "--force", help="Recompute every session"),
    include_scratch: bool = typer.Option(
        False,
        "--include-scratch",
        help="Also evaluate sessions whose working directory is a temp directory",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
):
    """Update signals/sessions.jsonl for every included catalog session."""
    lay = _layout(root)
    host_full = _expand_host(host) if host else None
    sids = [s.strip() for s in session_id.split(",") if s.strip()] if session_id else None
    with archive_lock(lay):
        source: Any
        if include_scratch:
            from .signals import NormalizedSource

            source = NormalizedSource(
                layout=lay, host=host_full, session_ids=sids, skip_temp=False
            )
        else:
            update_catalog(lay)
            source = _CatalogSlice(load_catalog(lay), host=host_full, session_ids=sids)
        update = update_signals(
            lay,
            source,
            force=force,
            host=host_full,
            session_ids=sids,
            prune=not (host_full or sids),
        )
    console.print(
        f"[green]signals up to date[/green] set={update.version} sessions={update.total:,} "
        f"added={len(update.added_keys):,} changed={len(update.changed_keys):,} "
        f"removed={len(update.removed_keys):,} reused={update.reused:,} "
        f"in {update.duration_s:.1f}s"
    )
    signals_root = lay.root / "signals"
    console.print(f"  definitions {signals_root / 'definitions.json'}")
    console.print(f"  sessions    {signals_root / 'sessions.jsonl'}")


@signal_app.command("show")
def signal_show(
    host: str = typer.Argument(..., help="registered or archived host ID"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
):
    """Show every stored signal value for one session."""
    lay = _layout(root)
    host_full = _expand_host(host)
    key = f"{host_full}/{session_id}"
    store = load_signal_store(lay)
    record = store.record(key)
    if record is None:
        console.print(
            f"[yellow]No stored signals for {key}. Run `retro signal run` first.[/yellow]"
        )
        raise typer.Exit(1)
    definitions = {item["id"]: item for item in store.definitions}
    evidence = store.evidence(key)
    confidence = store.confidence(key)

    table = Table(title=f"Signals for {key}")
    table.add_column("id")
    table.add_column("category")
    table.add_column("value")
    table.add_column("unit")
    table.add_column("conf", justify="right")
    table.add_column("evidence")
    for signal_id, value in sorted(record.get("values", {}).items()):
        definition = definitions.get(signal_id, {})
        refs = evidence.get(signal_id) or []
        table.add_row(
            signal_id,
            str(definition.get("category") or ""),
            str(value),
            str(definition.get("unit") or ""),
            f"{confidence.get(signal_id, 1.0):.2f}",
            ", ".join(refs[:2]),
        )
    console.print(table)


@signal_app.command("summary")
def signal_summary(
    signal: Optional[str] = typer.Option(
        None, "--signal", help="Restrict to signal ids (comma separated)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the raw summary as JSON"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
):
    """Aggregate stored signal values across every evaluated session."""
    lay = _layout(root)
    store = load_signal_store(lay)
    if not len(store):
        console.print("[yellow]No signal store yet. Run `retro signal run` first.[/yellow]")
        raise typer.Exit(1)
    wanted = {s.strip() for s in signal.split(",") if s.strip()} if signal else None
    summary = summarize(store.all_values(), store.definitions)
    blocks = summary["signals"]
    if wanted:
        blocks = {k: v for k, v in blocks.items() if k in wanted}
    if as_json:
        import json as _json

        console.print_json(_json.dumps({"sessions": summary["sessions"], "signals": blocks}))
        return
    table = Table(title=f"Signal summary ({summary['sessions']:,} sessions)")
    table.add_column("id")
    table.add_column("category")
    table.add_column("n", justify="right")
    table.add_column("summary")
    for signal_id, block in sorted(blocks.items()):
        if "true_rate" in block:
            text = f"true {block['true']}/{block['n']} ({block['true_rate']:.0%})"
        elif "counts" in block:
            text = ", ".join(f"{k}={v}" for k, v in list(block["counts"].items())[:4])
        else:
            text = (
                f"median {block['median']:g}  p90 {block['p90']:g}  "
                f"mean {block['mean']:g}  sum {block['sum']:g}"
            )
        table.add_row(signal_id, str(block.get("category") or ""), f"{block['n']:,}", text)
    console.print(table)


# ---- dashboard --------------------------------------------------------------


@dashboard_app.command("build")
def dashboard_build(
    mode: str = typer.Option(
        "auto",
        "--mode",
        help="auto reuses unchanged session-detail shards; full rebuilds every shard.",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    out: Optional[Path] = typer.Option(None, "--out", help="dashboard directory"),
    publish: bool = typer.Option(
        True,
        "--publish/--no-publish",
        help="Publish a new generation atomically, or write straight into --out.",
    ),
):
    """Build the static dashboard from the session catalog and the signal store."""
    from .dashboard import BUILD_MODES, build_dashboard

    if mode not in BUILD_MODES:
        raise typer.BadParameter(f"mode must be one of: {', '.join(BUILD_MODES)}")

    layout = _layout(root)
    report = build_dashboard(layout, resolve_dashboard_dir(out), mode=mode, publish=publish)
    console.print(f"[green]dashboard ready:[/green] {report.index_path}")
    console.print(
        f"  sessions={report.sessions:,} included={report.included:,} "
        f"scored={report.with_signals:,} signals={report.signal_definitions}"
    )
    console.print(
        f"  shards: included={report.session_shards} excluded={report.excluded_shards} "
        f"signals={report.signal_shards} detail={report.detail_shards}"
        f"+{report.excluded_detail_shards} (reused {report.detail_cached})"
    )
    console.print(
        f"  size={report.bytes_total / 1_048_576:.1f} MiB "
        f"index={report.index_bytes / 1024:.0f} KiB in {report.duration_s:.1f}s"
    )
    for warning in report.warnings:
        console.print(f"[yellow]  ! {warning}[/yellow]")


@dashboard_app.command("view")
def dashboard_view(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    limit: int = typer.Option(25, "--limit", help="rows per table"),
    interactive: Optional[bool] = typer.Option(
        None,
        "--interactive/--no-interactive",
        help="Force or suppress the interactive loop (default: auto-detect a TTY).",
    ),
):
    """Browse the catalog, projects, sessions and signals in the terminal."""
    from .dashboard import run_terminal_dashboard

    run_terminal_dashboard(
        _layout(root),
        interactive=interactive,
        console=console,
        limit=limit,
    )


@dashboard_app.command("serve")
def dashboard_serve(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    out: Optional[Path] = typer.Option(None, "--out", help="dashboard directory"),
    port: int = typer.Option(8877, "--port", min=1, max=65535, help="Loopback-only HTTP port"),
    agent: Optional[list[Path]] = typer.Option(
        None, "--agent", exists=True, dir_okay=False, resolve_path=True,
        help="Register a candidate Ghostlab agent config; may be repeated",
    ),
    ghostlab_bin: Optional[str] = typer.Option(
        None, "--ghostlab-bin", help="Ghostlab executable name/path",
    ),
    max_attempts: int = typer.Option(
        100, "--max-attempts", min=1, help="Maximum requested agent trials in one UI-created experiment",
    ),
) -> None:
    """Serve local experiment planning, explicit job starts and private grader inspection."""
    from .benchmarks.task_scorer.ghostlab_cli import GhostlabCli
    from .dashboard import build_dashboard
    from .dashboard.server import ControlError, DashboardServer, ExperimentController

    layout, directory = _layout(root), resolve_dashboard_dir(out)
    try:
        specs = [AgentSpec.from_path(path) for path in agent or []]
        build_dashboard(layout, directory, mode="auto", publish=True)
        controller = ExperimentController(
            layout, directory, agents=specs, ghostlab=GhostlabCli(ghostlab_bin),
            max_attempts=max_attempts,
        )
        server = DashboardServer(controller, port)
    except (ControlError, TaskVerificationError, GhostlabError, OSError) as exc:
        console.print(f"[red]dashboard controls failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]local dashboard:[/green] http://127.0.0.1:{port}/index.html#/tasks")
    console.print(
        "Static exports stay read-only. Saving a plan does not run a model; starting a job may incur charges."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("Stopping local controls; waiting for any active experiment to finish cleanup.")
    finally:
        server.server_close()
        controller.close()


@app.command("analyze")
def analyze(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    host: Optional[str] = typer.Option(None, help="Restrict to one host ID"),
) -> None:
    """Analyze command and tool call patterns across imported sessions."""
    from .signals import analyze_sessions, generate_report, render_console_report

    lay = _layout(root)
    with archive_lock(lay):
        update_catalog(lay)
        source = _CatalogSlice(
            load_catalog(lay), host=_expand_host(host) if host else None, session_ids=None,
        )
    stats = analyze_sessions(lay, source)
    render_console_report(stats)

    report_path = lay.root / "analysis_report.md"
    generate_report(stats, report_path)
    console.print()
    console.print(f"[green]Wrote analysis report to:[/green] [bold]{report_path}[/bold]")


# ---- memory -----------------------------------------------------------------


@memory_app.command("init")
def memory_init(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Create the memory directory and empty SQLite index."""
    from .memory_store import init

    lay = _layout(root)
    init(lay)
    console.print(f"[green]memory index ready:[/green] {lay.memory_index_path()}")


@memory_app.command("reindex")
def memory_reindex(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Rebuild index.sqlite from flat-file memory sources."""
    from .memory_store import reindex

    lay = _layout(root)
    with archive_lock(lay):
        update_catalog(lay)
        report = reindex(lay)
    console.print(f"[green]indexed {report.indexed} memories[/green]")
    console.print(f"  items.jsonl records: {report.source_records}")
    console.print(f"  mined artifacts:     {report.mined_records}")
    console.print(f"  evidence refs:       {report.evidence_refs}")
    console.print(f"  wiki links:          {report.links}")
    console.print(f"  sqlite:              {lay.memory_index_path()}")


@memory_app.command("doctor")
def memory_doctor(
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    cwd: Optional[Path] = typer.Option(None, "--cwd", help="Repo/cwd for repo-scoped recall"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum memories to return"),
    include_candidates: bool = typer.Option(
        True,
        "--include-candidates/--accepted-only",
        help="Include candidate memories while the promotion workflow is being built.",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    cwd: Optional[Path] = typer.Option(None, "--cwd", help="Repo/cwd for repo-scoped recall"),
    limit: int = typer.Option(6, "--limit", "-n", help="Maximum memories to include"),
    include_candidates: bool = typer.Option(
        True,
        "--include-candidates/--accepted-only",
        help="Include candidate memories while the promotion workflow is being built.",
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Session that used the memory"),
    reason: Optional[str] = typer.Option(None, "--reason", help="Optional update reason"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
):
    """retro: local-first coding agent session capture and memory tool."""


if __name__ == "__main__":
    app()
