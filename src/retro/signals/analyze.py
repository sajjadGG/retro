"""Cross-session command and tool analysis (`retro analyze`).

This replaces the old top-level `analyzer.py`, which walked every normalized
file a second time with its own copies of command parsing and failure
detection. It now reuses the profile builder, so there is exactly one
definition of "what command ran" and "did it fail".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from ..schema import read_events
from ..storage import Layout
from ..utils import atomic_write_text
from . import commands as cmd
from .profile import SessionProfile, build_profile
from .sources import record_attr, resolve_source

console = Console()


def _empty_host() -> dict[str, Any]:
    return {
        "sessions": 0,
        "commands": 0,
        "command_failures": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "tool_failures": 0,
        "base_commands": Counter(),
        "base_failures": Counter(),
        "tools": Counter(),
        "tool_errors": Counter(),
        "categories": Counter(),
    }


def analyze_sessions(
    layout: Layout,
    catalog: Any = None,
    *,
    host: str | None = None,
    session_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Aggregate command and tool behavior over the selected sessions."""
    source = resolve_source(layout, catalog, host=host, session_ids=session_ids)
    hosts: dict[str, dict[str, Any]] = {}
    total = _empty_host()
    sessions = 0

    for record in source.included():
        record_host = str(record_attr(record, "host", ""))
        session_id = str(record_attr(record, "session_id", ""))
        if not record_host or not session_id:
            continue
        path = layout.normalized_path(record_host, session_id)
        if not path.exists():
            continue
        profile = build_profile(
            key=f"{record_host}/{session_id}",
            host=record_host,
            session_id=session_id,
            events=list(read_events(path)),
            raw_dir=layout.raw_dir(record_host, session_id),
            record=record,
        )
        sessions += 1
        bucket = hosts.setdefault(record_host, _empty_host())
        for target in (bucket, total):
            _accumulate(target, profile)

    return {"sessions": sessions, "hosts": hosts, "total": total}


def _accumulate(bucket: dict[str, Any], profile: SessionProfile) -> None:
    bucket["sessions"] += 1
    bucket["commands"] += profile.command_calls
    bucket["command_failures"] += profile.command_errors
    bucket["tool_calls"] += profile.tool_calls
    bucket["tool_results"] += profile.tool_results
    bucket["tool_failures"] += profile.tool_errors
    bucket["base_commands"].update(profile.base_commands)
    bucket["base_failures"].update(profile.base_command_failures)
    bucket["tools"].update(profile.tool_names)
    bucket["tool_errors"].update(profile.tool_failures)
    bucket["categories"].update(profile.command_categories)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def render_console_report(stats: dict[str, Any]) -> None:
    hosts = sorted(stats.get("hosts") or {})
    summary = Table(title=f"Command & tool behavior ({stats.get('sessions', 0)} sessions)")
    summary.add_column("host", style="cyan")
    summary.add_column("sessions", justify="right")
    summary.add_column("commands", justify="right")
    summary.add_column("cmd fail", justify="right")
    summary.add_column("tool calls", justify="right")
    summary.add_column("tool fail", justify="right")
    for name in (*hosts, "total"):
        bucket = stats["hosts"][name] if name != "total" else stats["total"]
        summary.add_row(
            "all hosts" if name == "total" else name,
            f"{bucket['sessions']:,}",
            f"{bucket['commands']:,}",
            f"{_rate(bucket['command_failures'], bucket['commands']):.1%}",
            f"{bucket['tool_calls']:,}",
            f"{_rate(bucket['tool_failures'], bucket['tool_results']):.1%}",
        )
    console.print(summary)
    console.print()

    totals = stats["total"]
    commands = Table(title="Top commands")
    commands.add_column("command", style="green")
    commands.add_column("runs", justify="right")
    commands.add_column("failure rate", justify="right")
    for base, count in totals["base_commands"].most_common(15):
        commands.add_row(base, f"{count:,}", f"{_rate(totals['base_failures'][base], count):.1%}")
    console.print(commands)
    console.print()

    tools = Table(title="Top tools")
    tools.add_column("tool", style="magenta")
    tools.add_column("calls", justify="right")
    tools.add_column("failure rate", justify="right")
    for name, count in totals["tools"].most_common(15):
        tools.add_row(name, f"{count:,}", f"{_rate(totals['tool_errors'][name], count):.1%}")
    console.print(tools)


def generate_report(stats: dict[str, Any], output_path: Path) -> Path:
    lines: list[str] = ["# Command & tool analysis", ""]
    lines.append(f"- Sessions analyzed: {stats.get('sessions', 0):,}")
    lines.append("")
    lines.append("| host | sessions | commands | cmd failure rate | tool calls | tool failure rate |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    hosts = sorted(stats.get("hosts") or {})
    for name in (*hosts, "total"):
        bucket = stats["hosts"][name] if name != "total" else stats["total"]
        lines.append(
            f"| {'all hosts' if name == 'total' else name} | {bucket['sessions']:,} | "
            f"{bucket['commands']:,} | {_rate(bucket['command_failures'], bucket['commands']):.1%} | "
            f"{bucket['tool_calls']:,} | {_rate(bucket['tool_failures'], bucket['tool_results']):.1%} |"
        )
    totals = stats["total"]
    lines.extend(["", "## Top commands", "", "| command | runs | failure rate |", "| --- | ---: | ---: |"])
    for base, count in totals["base_commands"].most_common(25):
        lines.append(f"| `{base}` | {count:,} | {_rate(totals['base_failures'][base], count):.1%} |")
    lines.extend(["", "## Top tools", "", "| tool | calls | failure rate |", "| --- | ---: | ---: |"])
    for name, count in totals["tools"].most_common(25):
        lines.append(f"| `{name}` | {count:,} | {_rate(totals['tool_errors'][name], count):.1%} |")
    lines.extend(["", "## Command mix", "", "| category | runs |", "| --- | ---: |"])
    for category, count in totals["categories"].most_common():
        lines.append(f"| {category} | {count:,} |")
    lines.append("")
    atomic_write_text(output_path, "\n".join(lines))
    return output_path


def session_tips(profile: SessionProfile) -> list[str]:
    """Actionable, evidence-backed notes about a single session."""
    tips: list[str] = []
    if profile.user_turns > 35:
        tips.append(
            f"{profile.user_turns} human turns in one session. Long sessions pay the context "
            "tax on every turn; splitting the task into fresh sessions is usually cheaper."
        )
    if profile.searches > 10 and profile.file_edits:
        tips.append(
            f"{profile.searches} search commands before {profile.file_edits} edits. An AGENTS.md "
            "describing entry points removes most of that rediscovery."
        )
    rate = profile.tool_error_rate
    if rate is not None and profile.tool_results > 5 and rate > 0.3:
        tips.append(
            f"{rate:.0%} of tool results failed. Agreeing on a plan before editing avoids most retry loops."
        )
    if profile.file_edits and not profile.validation_runs:
        tips.append(
            f"{profile.file_edits} edits and no test, typecheck, lint or build command ran. "
            "Ask for a validation step before accepting the work."
        )
    if profile.compactions:
        tips.append(
            f"{profile.compactions} context compaction(s): the session outgrew the window and "
            "paid to re-summarize itself."
        )
    if profile.secret_kinds:
        tips.append(
            "Credential-shaped strings appear in this rollout ("
            + ", ".join(sorted(profile.secret_kinds))
            + "). Rotate them if they are real."
        )
    return tips


__all__ = [
    "analyze_sessions",
    "generate_report",
    "render_console_report",
    "session_tips",
    "cmd",
]
