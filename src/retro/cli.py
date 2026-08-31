"""Typer CLI for retro.

Commands:
  retro list                       -> show discoverable sessions per host
  retro import claude|codex|copilot [...]  -> capture + normalize a session
  retro import all                 -> capture + normalize all discoverable sessions
  retro render <host> <id>         -> re-render markdown from normalized
  retro show   <host> <id>         -> show artifact paths + counts
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

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
)
from .benchmarks.task_scorer.bundle import BundleError, bundle_taskset
from .benchmarks.task_scorer.environment import DockerContainerRuntime, resolve_environment
from .benchmarks.task_scorer.ghostlab_cli import GhostlabError
from .benchmarks.task_scorer.git_state import (
    CaptureExistsError,
    GitError,
    capture_repository_state,
)
from .benchmarks.task_scorer.run import (
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
from .config import (
    RetroConfig,
    config_path,
    load_config,
    resolve_dashboard_dir,
    save_config,
)
from .importers.claude import ClaudeImporter
from .importers.codex import CodexImporter
from .importers.copilot import CopilotImporter
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
from .schema import HOSTS, Host, read_events
from .signals import REGISTRY as SIGNAL_REGISTRY
from .signals import run_signals, write_signal_artifacts
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

config_app = typer.Typer(no_args_is_help=True, help="Inspect and update per-user configuration.")
app.add_typer(config_app, name="config")

archive_app = typer.Typer(no_args_is_help=True, help="Plan and execute archive migrations.")
app.add_typer(archive_app, name="archive")

schedule_app = typer.Typer(no_args_is_help=True, help="Manage periodic local capture.")
app.add_typer(schedule_app, name="schedule")

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
    host: Optional[str] = typer.Option(None, help="Filter to one host: claude|codex|copilot"),
    limit: int = typer.Option(20, help="Max rows per host"),
    root: Optional[Path] = typer.Option(
        None,
        help="rollout-memory root (default ./rollout-memory)",
    ),
):
    """List sessions discoverable on this machine."""
    lay = _layout(root)
    host_full = _expand_host(host) if host else None
    if host_full in (None, "claude-code"):
        _print_claude_table(ClaudeImporter(lay), limit, lay)
    if host_full in (None, "codex"):
        _print_codex_table(CodexImporter(lay), limit, lay)
    if host_full in (None, "vscode-copilot"):
        _print_copilot_table(CopilotImporter(lay), limit, lay)


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


def _print_copilot_table(imp: CopilotImporter, limit: int, lay: Layout) -> None:
    all_sessions = imp.discover()
    sessions = all_sessions[:limit]
    imported = set(lay.list_imported("vscode-copilot"))
    table = Table(title=f"VS Code Copilot  ({len(sessions)}/{len(all_sessions)} shown)")
    table.add_column("imported", justify="center")
    table.add_column("session_id")
    table.add_column("source")
    table.add_column("workspace")
    table.add_column("model")
    table.add_column("title")
    for session in sessions:
        mark = "✓" if session.session_id in imported else ""
        table.add_row(
            mark,
            session.session_id,
            session.source_kind + (" (active)" if getattr(session, "active", False) else ""),
            session.workspace_name,
            session.display_model,
            session.display_title,
        )
    console.print(table)


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
    imp = CopilotImporter(
        lay,
        user_data_dir=user_data_dir,
        session_state_dir=session_state_dir,
    )
    if all_sessions:
        _import_many(
            imp,
            [(session.session_id, session.display_title) for session in imp.discover()[:limit]],
            force=force,
            lay=lay,
            render=not no_render,
        )
        return
    if not session_id and not latest:
        raise typer.BadParameter("Pass --session-id <id>, --latest, or --all")
    if latest:
        session = imp.latest()
        if session is None:
            console.print("[red]No local VS Code Copilot sessions found.[/red]")
            raise typer.Exit(1)
        session_id = session.session_id
    assert session_id is not None
    _do_import(imp, session_id, force=force, lay=lay, render=not no_render)


@import_app.command("all")
def import_all(
    force: bool = typer.Option(False, "--force", help="Overwrite existing raw captures"),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
    no_render: bool = typer.Option(False, "--no-render", help="Skip markdown render"),
    limit_per_host: Optional[int] = typer.Option(
        None, "--limit-per-host", help="Optional max sessions per host"
    ),
):
    """Import every discoverable session from all supported hosts."""
    lay = _layout(root)
    claude = ClaudeImporter(lay)
    codex = CodexImporter(lay)
    copilot = CopilotImporter(lay)
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
    failures.extend(
        _import_many(
            copilot,
            [
                (session.session_id, session.display_title)
                for session in copilot.discover()[:limit_per_host]
            ],
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
    host: str = typer.Argument(..., help="claude|codex|copilot|*"),
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


# ---- benchmark ---------------------------------------------------------------


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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
) -> None:
    """Select exact, clean Git-backed rollouts and record every rejection."""
    lay = _layout(root)
    resolved_host = None if host is None or host in ("*", "all") else _expand_host(host)
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
        )
    except (SelectionError, SchemaError) as exc:
        console.print(f"[red]taskset selection failed: {exc}[/red]")
        raise typer.Exit(1) from exc
    counts = result.result.to_dict()["counts"]
    console.print(
        f"[green]selected {counts['selected']} source(s); "
        f"rejected {counts['rejected']}[/green]"
    )
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
    resolved_host = None if host is None or host in ("*", "all") else _expand_host(host)
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
        )
    except (TaskVerificationError, BuildConfigurationError, GhostlabError) as exc:
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
    except (TaskVerificationError, BuildConfigurationError) as exc:
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
        hosts=_expand_hosts(host),
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
    host: str = typer.Argument(..., help="claude|codex|copilot"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    host: str = typer.Argument(..., help="claude|codex|copilot"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    if h in ("copilot", "vscode", "vscode-copilot", "gh-copilot"):
        return "vscode-copilot"
    raise typer.BadParameter(f"unknown host {host!r}; use claude|codex|copilot")


def _expand_hosts(host: str) -> list[Host]:
    h = host.lower()
    if h in ("*", "all"):
        return list(HOSTS)
    return [_expand_host(host)]


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
    current = load_config()
    values = current.to_dict()
    normalized = key.lower().replace("_", "-")
    if normalized == "archive-root":
        values["archive_root"] = str(Path(value).expanduser().resolve())
    elif normalized == "dashboard-dir":
        values["dashboard_dir"] = str(Path(value).expanduser().resolve())
    elif normalized == "sync-interval":
        from .schedule import parse_interval

        values["sync_interval_seconds"] = parse_interval(value)
    elif normalized == "derived-interval":
        from .schedule import parse_interval

        values["derived_interval_seconds"] = parse_interval(value)
    elif normalized == "sync-on-login":
        lowered = value.lower()
        if lowered not in {"true", "false", "yes", "no", "1", "0"}:
            raise typer.BadParameter("sync-on-login must be true or false")
        values["sync_on_login"] = lowered in {"true", "yes", "1"}
    else:
        raise typer.BadParameter(
            "key must be archive-root, dashboard-dir, sync-interval, "
            "derived-interval, or sync-on-login"
        )
    updated = RetroConfig(**values)
    path = save_config(updated)
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
    from .schedule import install_schedule, parse_interval

    current = load_config()
    interval = parse_interval(periodic)
    derived_interval = parse_interval(derived_every)
    config = RetroConfig(
        archive_root=str(
            (archive_root or Path(current.archive_root)).expanduser().resolve()
        ),
        dashboard_dir=str(
            (dashboard_dir or Path(current.dashboard_dir)).expanduser().resolve()
        ),
        sync_interval_seconds=interval,
        derived_interval_seconds=derived_interval,
        sync_on_login=True,
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
        _layout(root),
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
    if report.status not in {"success"}:
        raise typer.Exit(1)


@schedule_app.command("install")
def schedule_install(
    every: str = typer.Option("15m", "--every"),
) -> None:
    """Install or replace the current user's macOS LaunchAgent."""
    from .schedule import install_schedule, parse_interval

    interval = parse_interval(every)
    config = load_config()
    save_config(
        RetroConfig(
            archive_root=config.archive_root,
            dashboard_dir=config.dashboard_dir,
            sync_interval_seconds=interval,
            derived_interval_seconds=config.derived_interval_seconds,
            sync_on_login=config.sync_on_login,
        )
    )
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
    from .importers.copilot import CopilotImporter
    from .schedule import schedule_status

    layout = default_layout()
    layout.ensure()
    free = shutil.disk_usage(layout.root.parent).free
    table = Table(title="Retro global archive")
    table.add_column("check")
    table.add_column("value")
    table.add_row("config", str(config_path()))
    table.add_row("archive", str(layout.root))
    table.add_row("dashboard", str(resolve_dashboard_dir()))
    table.add_row("free space", f"{free / 1024**3:.1f} GiB")
    table.add_row(
        "normalized sessions",
        str(sum(len(layout.list_normalized(host)) for host in HOSTS)),
    )
    table.add_row("discoverable Copilot", str(len(CopilotImporter(layout).discover())))
    status = schedule_status()
    table.add_row("schedule installed", str(status.get("installed")))
    table.add_row("schedule loaded", str(status.get("loaded")))
    console.print(table)


# ---- signals ----------------------------------------------------------------


@signal_app.command("list")
def signal_list(
    group: Optional[str] = typer.Option(None, help="Filter by group: activity|outcome|cost|risk"),
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
    host: Optional[str] = typer.Option(None, help="Restrict to one host: claude|codex|copilot"),
    session_id: Optional[str] = typer.Option(
        None, "--session-id", help="Restrict to one session id (repeatable via comma)"
    ),
    signal: Optional[str] = typer.Option(
        None, "--signal", help="Restrict to one signal name (repeatable via comma)"
    ),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    host: str = typer.Argument(..., help="claude|codex|copilot"),
    session_id: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    root: Optional[Path] = typer.Option(
        None,
        help="rollout-memory root (default ./rollout-memory)",
    ),
    out: Optional[Path] = typer.Option(None, help="output directory (default ./dashboard)"),
):
    """Build dashboard/data/rollouts.json and dashboard/index.html."""
    if mode not in {"auto", "calculate", "display"}:
        raise typer.BadParameter("mode must be one of: auto, calculate, display")

    from .dashboard_build import build as build_dashboard_html

    index_path = build_dashboard_html(
        mode=mode,
        artifact_root=root,
        out_dir=resolve_dashboard_dir(out),
    )
    console.print(f"[green]dashboard ready:[/green] {index_path}")


@dashboard_app.command("experiments")
def dashboard_experiments(
    root: Optional[Path] = typer.Option(
        None,
        help="rollout-memory root (default ./rollout-memory)",
    ),
    out: Optional[Path] = typer.Option(None, help="output directory (default ./dashboard)"),
):
    """Build the experimental trajectory-signals page (trajectory_experiments.html)."""
    from .dashboard_experiments import build as build_experiments_html

    html_path = build_experiments_html(
        artifact_root=root,
        out_dir=resolve_dashboard_dir(out),
    )
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    # Print streak message on execution if in a TTY
    if sys.stdout.isatty():
        try:
            from .quest import ensure_daily_quests, load_quest_state, save_quest_state
            lay = default_layout(root)
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
    root: Optional[Path] = typer.Option(None, help="rollout-memory root"),
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
