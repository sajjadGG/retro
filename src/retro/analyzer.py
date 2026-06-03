"""Command and Tool Call Analyzer for retro rollout-memory."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from .schema import Host, NormalizedEvent, read_events
from .storage import Layout

console = Console()


def get_base_command(cmd_line: str) -> str:
    """Extract the base executable command from a full command line."""
    cmd = cmd_line.strip()
    if not cmd:
        return ""

    while True:
        m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)=([^\s]+)\s+(.*)$", cmd)
        if m:
            cmd = m.group(3).strip()
        else:
            break

    parts = cmd.split()
    if not parts:
        return ""

    exe = parts[0]
    exe = exe.replace("\\", "/")
    if "/" in exe:
        exe = exe.split("/")[-1]

    if exe in ("python", "python3", "pythonw", "py", "python.exe") or exe.startswith("python3."):
        if len(parts) > 2 and parts[1] == "-m":
            base = parts[2]
            if "/" in base:
                base = base.split("/")[-1]
            return base
        elif len(parts) > 1:
            script = parts[1]
            script = script.replace("\\", "/")
            if "/" in script:
                script = script.split("/")[-1]
            return script

    if exe in ("npx", "bunx", "npx.cmd"):
        if len(parts) > 1:
            return parts[1]

    if exe == "poetry" and len(parts) > 2 and parts[1] == "run":
        return parts[2]

    return exe


def extract_command_line(ev: NormalizedEvent) -> str | None:
    """Extract command line string from a normalized event's payload."""
    if ev.event_type != "command":
        return None
    payload = ev.payload or {}

    val = payload.get("input")
    if isinstance(val, dict):
        cmd = val.get("command") or val.get("cmd")
        if isinstance(cmd, str):
            return cmd
    elif isinstance(val, str):
        return val

    args = payload.get("arguments")
    if isinstance(args, dict):
        cmd = args.get("cmd") or args.get("command") or args.get("command_line")
        if isinstance(cmd, str):
            return cmd
    elif isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                cmd = parsed.get("cmd") or parsed.get("command") or parsed.get("command_line")
                if isinstance(cmd, str):
                    return cmd
        except json.JSONDecodeError:
            return args

    cmd_direct = payload.get("command") or payload.get("cmd")
    if isinstance(cmd_direct, str):
        return cmd_direct

    return None


def _is_failed(ev: NormalizedEvent) -> bool:
    """Determine if a tool result or command event indicates a failure."""
    payload = ev.payload or {}
    if payload.get("is_error") is True or payload.get("success") is False:
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.lower() in {"failed", "error"}:
        return True
    output = payload.get("output")
    if isinstance(output, str):
        m = re.search(r'"exit_code"\s*:\s*(\d+)', output)
        if m and int(m.group(1)) != 0:
            return True
        if "Process exited with code " in output:
            m2 = re.search(r"Process exited with code (\d+)", output)
            if m2 and int(m2.group(1)) != 0:
                return True
    return ev.event_type == "error"


def analyze_sessions(layout: Layout) -> dict[str, Any]:
    """Scan all normalized rollout events and extract statistics."""
    stats: dict[str, Any] = {
        "claude-code": {
            "sessions": 0,
            "commands": [],
            "tools": [],
            "transitions": Counter(),
        },
        "codex": {
            "sessions": 0,
            "commands": [],
            "tools": [],
            "transitions": Counter(),
        },
        "total": {
            "sessions": 0,
            "commands": [],
            "tools": [],
            "transitions": Counter(),
        },
    }

    hosts: list[Host] = ["claude-code", "codex"]
    for host in hosts:
        session_ids = layout.list_normalized(host)
        stats[host]["sessions"] = len(session_ids)
        stats["total"]["sessions"] += len(session_ids)

        for sid in session_ids:
            path = layout.normalized_path(host, sid)
            if not path.exists():
                continue

            events = list(read_events(path))

            results_by_call_id = {}
            results_by_parent_id = {}
            unmatched_results = []

            for ev in events:
                if ev.actor == "tool":
                    payload = ev.payload or {}
                    call_id = payload.get("call_id")
                    if call_id:
                        results_by_call_id[call_id] = ev
                    if ev.parent_event_id:
                        results_by_parent_id[ev.parent_event_id] = ev
                    if not call_id and not ev.parent_event_id:
                        unmatched_results.append(ev)

            last_etype = None
            for ev in events:
                etype = ev.event_type
                if last_etype is not None:
                    stats[host]["transitions"][(last_etype, etype)] += 1
                    stats["total"]["transitions"][(last_etype, etype)] += 1
                last_etype = etype

            unmatched_idx = 0
            for ev in events:
                if ev.actor == "assistant" and ev.event_type in (
                    "tool_call",
                    "command",
                    "file_read",
                    "file_edit",
                ):
                    payload = ev.payload or {}
                    tool_name = payload.get("name") or ev.summary.split("(")[0]
                    if not tool_name or tool_name == "?":
                        continue

                    call_id = payload.get("call_id")
                    res_ev = None
                    if call_id and call_id in results_by_call_id:
                        res_ev = results_by_call_id[call_id]
                    elif ev.event_id in results_by_parent_id:
                        res_ev = results_by_parent_id[ev.event_id]
                    elif unmatched_idx < len(unmatched_results):
                        res_ev = unmatched_results[unmatched_idx]
                        unmatched_idx += 1

                    failed = _is_failed(res_ev) if res_ev else _is_failed(ev)

                    is_cmd = tool_name in ("Bash", "exec_command", "shell") or ev.event_type == "command"
                    if is_cmd:
                        cmd_line = extract_command_line(ev)
                        if cmd_line:
                            base = get_base_command(cmd_line)
                            cmd_record = {
                                "cmd_line": cmd_line,
                                "base_cmd": base,
                                "failed": failed,
                            }
                            stats[host]["commands"].append(cmd_record)
                            stats["total"]["commands"].append(cmd_record)

                    tool_record = {
                        "name": tool_name,
                        "failed": failed,
                    }
                    stats[host]["tools"].append(tool_record)
                    stats["total"]["tools"].append(tool_record)

    return stats


def generate_report(stats: dict[str, Any], output_path: Path) -> None:
    """Generate the markdown report at rollout-memory/analysis_report.md."""
    lines = []
    lines.append("# Command & Tool Use Analysis Report")
    lines.append("")
    lines.append("This report analyzes command execution, tool usage, and transition patterns ")
    lines.append("across all capture agent sessions.")
    lines.append("")

    lines.append("## Overview Summary")
    lines.append("")
    lines.append(
        "| Host | Sessions | Total Commands | Command Failure Rate | Total Tool Calls | Tool Failure Rate |"
    )
    lines.append("|---|---|---|---|---|---|")
    for host in ("claude-code", "codex", "total"):
        h_data = stats[host]
        cmds = h_data["commands"]
        tools = h_data["tools"]

        cmd_fail_rate = sum(1 for c in cmds if c["failed"]) / len(cmds) if cmds else 0.0
        tool_fail_rate = sum(1 for t in tools if t["failed"]) / len(tools) if tools else 0.0

        label = "Total (All Hosts)" if host == "total" else host
        lines.append(
            f"| {label} | {h_data['sessions']} | {len(cmds)} | "
            f"{cmd_fail_rate:.1%} | {len(tools)} | {tool_fail_rate:.1%} |"
        )
    lines.append("")

    lines.append("## Top Base Commands")
    lines.append("")
    lines.append("| Base Command | Host | Executions | Failure Rate |")
    lines.append("|---|---|---|---|")

    base_counts: dict[tuple[str, str], int] = defaultdict(int)
    base_fails: dict[tuple[str, str], int] = defaultdict(int)
    for host in ("claude-code", "codex"):
        h_cmds = stats[host]["commands"]
        for c in h_cmds:
            key = (c["base_cmd"], host)
            base_counts[key] += 1
            if c["failed"]:
                base_fails[key] += 1

    sorted_base = sorted(base_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    for (base, host), count in sorted_base:
        fail_rate = base_fails[(base, host)] / count if count else 0.0
        lines.append(f"| `{base}` | {host} | {count} | {fail_rate:.1%} |")
    lines.append("")

    lines.append("## Top Full Command Lines")
    lines.append("")
    lines.append("| Command Line | Host | Executions | Failure Rate |")
    lines.append("|---|---|---|---|")

    cmd_counts: dict[tuple[str, str], int] = defaultdict(int)
    cmd_fails: dict[tuple[str, str], int] = defaultdict(int)
    for host in ("claude-code", "codex"):
        h_cmds = stats[host]["commands"]
        for c in h_cmds:
            key = (c["cmd_line"], host)
            cmd_counts[key] += 1
            if c["failed"]:
                cmd_fails[key] += 1

    sorted_cmds = sorted(cmd_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    for (cmd_line, host), count in sorted_cmds:
        fail_rate = cmd_fails[(cmd_line, host)] / count if count else 0.0
        trunc_cmd = cmd_line if len(cmd_line) <= 60 else cmd_line[:57] + "..."
        lines.append(f"| `{trunc_cmd}` | {host} | {count} | {fail_rate:.1%} |")
    lines.append("")

    lines.append("## Top Tool Calls")
    lines.append("")
    lines.append("| Tool Name | Host | Executions | Failure Rate |")
    lines.append("|---|---|---|---|")

    tool_counts: dict[tuple[str, str], int] = defaultdict(int)
    tool_fails: dict[tuple[str, str], int] = defaultdict(int)
    for host in ("claude-code", "codex"):
        h_tools = stats[host]["tools"]
        for t in h_tools:
            key = (t["name"], host)
            tool_counts[key] += 1
            if t["failed"]:
                tool_fails[key] += 1

    sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    for (name, host), count in sorted_tools:
        fail_rate = tool_fails[(name, host)] / count if count else 0.0
        lines.append(f"| `{name}` | {host} | {count} | {fail_rate:.1%} |")
    lines.append("")

    lines.append("## Action Transition Patterns")
    lines.append("")
    lines.append("| From Event Type | To Event Type | Occurrences |")
    lines.append("|---|---|---|")

    sorted_trans = sorted(stats["total"]["transitions"].items(), key=lambda x: x[1], reverse=True)[:15]
    for (from_type, to_type), count in sorted_trans:
        lines.append(f"| {from_type} | {to_type} | {count} |")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")


def render_console_report(stats: dict[str, Any]) -> None:
    """Print the Rich command line report to console."""
    console.print("[bold green]Retro Command & Tool Call Analysis[/bold green]")
    console.print()

    summary_table = Table(title="Overview Summary")
    summary_table.add_column("Host", style="cyan")
    summary_table.add_column("Sessions", justify="right")
    summary_table.add_column("Total Commands", justify="right")
    summary_table.add_column("Cmd Failure Rate", justify="right")
    summary_table.add_column("Total Tool Calls", justify="right")
    summary_table.add_column("Tool Failure Rate", justify="right")

    for host in ("claude-code", "codex", "total"):
        h_data = stats[host]
        cmds = h_data["commands"]
        tools = h_data["tools"]

        cmd_fail_rate = sum(1 for c in cmds if c["failed"]) / len(cmds) if cmds else 0.0
        tool_fail_rate = sum(1 for t in tools if t["failed"]) / len(tools) if tools else 0.0

        label = "Total (All)" if host == "total" else host
        summary_table.add_row(
            label,
            str(h_data["sessions"]),
            f"{len(cmds):,}",
            f"{cmd_fail_rate:.1%}",
            f"{len(tools):,}",
            f"{tool_fail_rate:.1%}",
        )

    console.print(summary_table)
    console.print()

    base_table = Table(title="Top Base Commands")
    base_table.add_column("Base Command", style="green")
    base_table.add_column("Host", style="cyan")
    base_table.add_column("Executions", justify="right")
    base_table.add_column("Failure Rate", justify="right")

    base_counts: dict[tuple[str, str], int] = defaultdict(int)
    base_fails: dict[tuple[str, str], int] = defaultdict(int)
    for host in ("claude-code", "codex"):
        for c in stats[host]["commands"]:
            key = (c["base_cmd"], host)
            base_counts[key] += 1
            if c["failed"]:
                base_fails[key] += 1

    sorted_base = sorted(base_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    for (base, host), count in sorted_base:
        fail_rate = base_fails[(base, host)] / count if count else 0.0
        base_table.add_row(base, host, f"{count:,}", f"{fail_rate:.1%}")

    console.print(base_table)
    console.print()

    tool_table = Table(title="Top Tool Calls")
    tool_table.add_column("Tool Name", style="magenta")
    tool_table.add_column("Host", style="cyan")
    tool_table.add_column("Executions", justify="right")
    tool_table.add_column("Failure Rate", justify="right")

    tool_counts: dict[tuple[str, str], int] = defaultdict(int)
    tool_fails: dict[tuple[str, str], int] = defaultdict(int)
    for host in ("claude-code", "codex"):
        for t in stats[host]["tools"]:
            key = (t["name"], host)
            tool_counts[key] += 1
            if t["failed"]:
                tool_fails[key] += 1

    sorted_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    for (name, host), count in sorted_tools:
        fail_rate = tool_fails[(name, host)] / count if count else 0.0
        tool_table.add_row(name, host, f"{count:,}", f"{fail_rate:.1%}")

    console.print(tool_table)
    console.print()

    trans_table = Table(title="Action Transition Patterns")
    trans_table.add_column("From Event Type", style="yellow")
    trans_table.add_column("To Event Type", style="cyan")
    trans_table.add_column("Occurrences", justify="right")

    sorted_trans = sorted(stats["total"]["transitions"].items(), key=lambda x: x[1], reverse=True)[:10]
    for (from_type, to_type), count in sorted_trans:
        trans_table.add_row(from_type, to_type, f"{count:,}")

    console.print(trans_table)
