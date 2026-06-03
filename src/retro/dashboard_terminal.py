"""Interactive terminal dashboard for retro rollout-memory.

Provides a feature-parity terminal view of the static HTML dashboard.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


def load_dashboard_data(mode: str = "auto") -> dict[str, Any]:
    """Load dashboard data from rollouts.json, building it if missing."""
    repo_root = Path(__file__).resolve().parents[2]
    json_path = repo_root / "dashboard" / "data" / "rollouts.json"

    if not json_path.exists():
        console.print(
            "[yellow]dashboard/data/rollouts.json not found. Building dashboard data first...[/yellow]"
        )
        builder = repo_root / "dashboard" / "build_dashboard.py"
        if not builder.exists():
            console.print(f"[red]Dashboard builder not found at {builder}[/red]")
            sys.exit(1)

        cmd = [sys.executable, str(builder), "--mode", mode]
        proc = subprocess.run(
            cmd,
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            console.print(f"[red]Failed to build dashboard: {proc.stderr}[/red]")
            sys.exit(proc.returncode)

    try:
        with json_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as e:
        console.print(f"[red]Failed to load rollouts.json: {e}[/red]")
        sys.exit(1)


def is_interactive() -> bool:
    """Check if the session is interactive (tty)."""
    return sys.stdin.isatty()


def run_non_interactive(data: dict[str, Any]) -> None:
    """Print a quick overview of imported sessions and exit (useful for tests/pipes)."""
    console.print("[bold green]Retro Rollout Dashboard (Non-interactive Mode)[/bold green]")
    console.print(f"Generated: {data.get('generated_at')}")
    console.print(f"Cost mode: {data.get('cost_mode')} ({data.get('rate_note')})")
    console.print()

    # Summary table
    table = Table(title="Imported Sessions Summary")
    table.add_column("Host", style="cyan")
    table.add_column("Session ID", style="magenta")
    table.add_column("Date", style="green")
    table.add_column("Events", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Est. Cost", justify="right")

    for s in data.get("sessions", []):
        cost = s.get("estimated_cost_usd")
        cost_str = f"${cost:.4f}" if cost is not None else "n/a"
        table.add_row(
            s.get("host", ""),
            s.get("session_id", ""),
            s.get("date", ""),
            f"{s.get('event_count', 0):,}",
            f"{s.get('tokens', {}).get('total_tokens', 0):,}",
            cost_str,
        )
    console.print(table)


def clear_screen() -> None:
    """Clear terminal screen."""
    console.print("\033[H\033[J", end="")


def format_money(val: float | None) -> str:
    """Format float as USD currency."""
    if val is None:
        return "n/a"
    return f"${val:.4f}"


def format_duration(seconds: int | None) -> str:
    """Format duration in seconds to human readable string."""
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def render_activity_histogram(by_day: dict[str, Any]) -> None:
    """Draw a clean ASCII stacked-bar histogram for daily session activity."""
    if not by_day:
        console.print("[dim]No activity logged.[/dim]")
        return

    days = sorted(by_day.keys())
    max_sessions = max((by_day[day].get("sessions", 0) for day in days), default=1)
    max_width = 30  # Max bar length in terminal chars

    console.print("[bold]ACTIVITY BY DAY[/bold] (Claude: [orange3]█[/orange3], Codex: [blue]█[/blue])")
    console.print("─" * 60)

    for day in days:
        d = by_day[day]
        claude = d.get("sessions_claude", 0)
        codex = d.get("sessions_codex", 0)
        total = claude + codex

        if max_sessions > 0:
            claude_len = int((claude / max_sessions) * max_width)
            codex_len = int((codex / max_sessions) * max_width)
        else:
            claude_len = codex_len = 0

        # Guarantee at least 1 block if count > 0 but rounded to 0
        if claude > 0 and claude_len == 0:
            claude_len = 1
        if codex > 0 and codex_len == 0:
            codex_len = 1

        claude_bar = "[orange3]█[/orange3]" * claude_len
        codex_bar = "[blue]█[/blue]" * codex_len
        bar = claude_bar + codex_bar

        console.print(f"  {day:<10}  {bar:<{max_width}}  {claude}+{codex} (total {total})")
    console.print()


def show_portfolio_kpis(data: dict[str, Any]) -> None:
    """Print the portfolio KPI summary."""
    summary = data.get("summary", {})
    totals = summary.get("totals", {})
    by_host = summary.get("by_host", {})
    mem = data.get("memory", {})
    secret_agg = (
        (data.get("signals") or {})
        .get("by_signal", {})
        .get("secret_exposure_signal", {})
    )

    claude_count = by_host.get("claude-code", 0)
    codex_count = by_host.get("codex", 0)

    # We build a grid of stats
    kpi_table = Table.grid(expand=True, padding=1)
    kpi_table.add_column(ratio=1)
    kpi_table.add_column(ratio=1)
    kpi_table.add_column(ratio=1)

    s_count = summary.get("session_count", 0)
    sessions_panel = Panel(
        f"[bold]{s_count}[/bold] Total Sessions\n"
        f"[orange3]{claude_count} Claude[/orange3] / [blue]{codex_count} Codex[/blue]",
        title="Sessions",
    )
    c_count = mem.get("candidate_count", 0)
    mem_sessions = mem.get("sessions_with_memory", 0)
    candidates_panel = Panel(
        f"[bold]{c_count}[/bold] Candidates\n[dim]{mem_sessions} mined sessions[/dim]",
        title="Memory Candidates",
    )
    a_days = summary.get("active_days", 0)
    scope_panel = Panel(
        f"[bold]{a_days}[/bold] Active Days\n[dim]Secret-risk: {secret_agg.get('true_count', 0)}[/dim]",
        title="Portfolio Scope",
    )
    kpi_table.add_row(sessions_panel, candidates_panel, scope_panel)

    events_count = totals.get("events", 0)
    tools_count = totals.get("tool_calls", 0)
    activity_panel = Panel(
        f"Events: [bold]{events_count:,}[/bold]\nTool Calls: [bold]{tools_count:,}[/bold]",
        title="Activity Totals",
    )
    edits_count = totals.get("file_edits", 0)
    tokens_count = totals.get("tokens", 0)
    asset_panel = Panel(
        f"File Edits: [bold]{edits_count:,}[/bold]\nTokens: [bold]{tokens_count:,}[/bold]",
        title="Asset Totals",
    )
    est_cost = format_money(summary.get("estimated_cost_usd"))
    cost_panel = Panel(
        f"Est Cost: [bold]{est_cost}[/bold]\n[dim]Rates: {data.get('rate_note')}[/dim]",
        title="Cost Summary",
    )
    kpi_table.add_row(activity_panel, asset_panel, cost_panel)

    console.print(
        Panel(
            kpi_table,
            title="[bold green]RETRO TERMINAL DASHBOARD[/bold green]",
            subtitle=f"Generated: {data.get('generated_at')} | Cost Mode: {data.get('cost_mode')}",
        )
    )
    console.print()


def show_session_list(data: dict[str, Any]) -> None:
    """Interactive loop for browsing sessions list."""
    sessions = data.get("sessions", [])

    page = 0
    page_size = 10
    host_filter = "all"
    search_query = ""

    while True:
        clear_screen()
        # Filter sessions
        q = search_query.lower()
        filtered = []
        for s in sessions:
            if host_filter != "all" and s.get("host") != host_filter:
                continue
            if q:
                files_str = " ".join(s.get("files_touched", []))
                haystack = (
                    f"{s.get('host', '')} {s.get('session_id', '')} {s.get('title', '')} {files_str}".lower()
                )
                if q not in haystack:
                    continue
            filtered.append(s)

        total_sessions = len(filtered)
        max_page = max(0, (total_sessions - 1) // page_size)
        page = min(page, max_page)

        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total_sessions)
        page_sessions = filtered[start_idx:end_idx]

        # Draw Table
        table = Table(
            title=f"Sessions · showing {len(filtered)} of {len(sessions)} (Page {page + 1}/{max_page + 1})"
        )
        table.add_column("#", justify="center")
        table.add_column("Host", justify="center")
        table.add_column("Date", justify="center")
        table.add_column("Title")
        table.add_column("Events", justify="right")
        table.add_column("Tools", justify="right")
        table.add_column("Edits", justify="right")
        table.add_column("Tokens", justify="right")
        table.add_column("Cost", justify="right")
        table.add_column("Memory", justify="center")
        table.add_column("Risk", justify="center")

        for idx, s in enumerate(page_sessions, start=1):
            host = s.get("host", "")
            host_disp = f"[orange3]{host}[/orange3]" if host == "claude-code" else f"[blue]{host}[/blue]"

            # Truncate title
            title = s.get("title", s.get("session_id", "untitled"))
            if len(title) > 40:
                title = title[:37] + "..."

            # Memory summary
            mined = s.get("mined", [])
            if not mined:
                mem_str = "—"
            else:
                total_cands = sum(m.get("candidate_count", 0) for m in mined)
                methods = len(mined)
                mem_str = f"{total_cands}c/{methods}m"

            # Risk summary
            risk_val = s.get("signals_index", {}).get("secret_exposure_signal")
            risk_str = "[red]secret[/red]" if risk_val else "—"

            table.add_row(
                str(idx),
                host_disp,
                s.get("date", ""),
                title,
                f"{s.get('event_count', 0):,}",
                f"{s.get('tool_call_events', 0) + s.get('tool_result_events', 0):,}",
                f"{s.get('file_edit_events', 0):,}",
                f"{s.get('tokens', {}).get('total_tokens', 0):,}",
                format_money(s.get("estimated_cost_usd")),
                mem_str,
                risk_str,
            )

        console.print(table)
        console.print(f"[bold]Filters:[/bold] host={host_filter} | search={search_query or 'None'}")
        console.print()
        console.print("Options:")
        console.print("  [bold]1-10[/bold]  View session detail")
        console.print("  [bold]n[/bold]     Next page      |  [bold]p[/bold]     Prev page")
        console.print("  [bold]f[/bold]     Filter host    |  [bold]s[/bold]     Search text")
        console.print("  [bold]b[/bold]     Back to main menu")
        console.print()

        choice = input("Choice: ").strip().lower()

        if choice == "b":
            break
        elif choice == "n":
            page = min(page + 1, max_page)
        elif choice == "p":
            page = max(page - 1, 0)
        elif choice == "f":
            if host_filter == "all":
                host_filter = "claude-code"
            elif host_filter == "claude-code":
                host_filter = "codex"
            else:
                host_filter = "all"
            page = 0
        elif choice == "s":
            search_query = input("Search text (press Enter to clear): ").strip()
            page = 0
        elif choice.isdigit():
            val = int(choice)
            if 1 <= val <= len(page_sessions):
                show_session_detail(page_sessions[val - 1])
            else:
                input("Invalid index. Press Enter to retry.")


def show_session_detail(s: dict[str, Any]) -> None:
    """Display session detail screen with tabs."""
    tab = "summary"

    while True:
        clear_screen()
        # Top metadata banner
        meta_table = Table.grid(expand=True, padding=1)
        meta_table.add_column()
        meta_table.add_column()

        host = s.get("host", "")
        host_disp = f"[orange3]{host}[/orange3]" if host == "claude-code" else f"[blue]{host}[/blue]"

        duration_str = format_duration(s.get("duration_seconds"))
        events_str = f"{s.get('event_count', 0):,}"
        cmds_str = f"{s.get('command_events', 0):,}"
        edits_str = f"{s.get('file_edit_events', 0):,}"
        files_str = f"{s.get('unique_files_touched', 0):,}"
        tokens_str = f"{s.get('tokens', {}).get('total_tokens', 0):,}"
        cost_str = format_money(s.get("estimated_cost_usd"))
        c_source = s.get("cost_source", "n/a")
        c_model = s.get("cost_model", "unknown")
        secret_risk = "Yes" if s.get("signals_index", {}).get("secret_exposure_signal") else "No"
        risk_disp = f"[red]{secret_risk}[/red]" if secret_risk == "Yes" else secret_risk

        # Details grid
        details = (
            f"Host: {host_disp} | Duration: {duration_str}\n"
            f"Events: {events_str} | Commands: {cmds_str}\n"
            f"File Edits: {edits_str} | Files Touched: {files_str}\n"
            f"Tokens: {tokens_str} | Est Cost: {cost_str}\n"
            f"Cost Source: {c_source} | Cost Model: {c_model}\n"
            f"Secret Risk: {risk_disp}"
        )

        console.print(Panel(details, title=f"[bold green]{s.get('title', s.get('session_id'))}[/bold green]"))
        console.print()

        # Tabs bar
        tabs = [
            "[u] Summary",
            "[o] Models",
            "[i] Signals",
            "[t] Transcript",
            "[y] Memory",
            "[b] Back to List",
        ]
        tabs_str = "   ".join(
            [f"[bold reverse]{t}[/bold reverse]" if t.lower().startswith(f"[{tab[0]}]") else t for t in tabs]
        )
        console.print(tabs_str)
        console.print("─" * 80)
        console.print()

        if tab == "summary":
            # Just print the JSON representation
            # We filter out large blocks like rendered_markdown for clean summary view
            clean_s = {k: v for k, v in s.items() if k not in ("rendered_markdown", "mined")}
            clean_s["mined_summary"] = [
                {
                    "method": m.get("method"),
                    "candidate_count": m.get("candidate_count"),
                    "filters_applied": m.get("filters_applied"),
                }
                for m in s.get("mined", [])
            ]
            console.print(clean_s)

        elif tab == "models":
            tokens_by_model = s.get("tokens_by_model", {})
            cost_by_model = s.get("cost_by_model", {})

            if not tokens_by_model:
                console.print("[dim]No per-model token data for this session.[/dim]")
            else:
                table = Table(title="Tokens & Cost by Model")
                table.add_column("Model")
                table.add_column("Input", justify="right")
                table.add_column("Cache Create", justify="right")
                table.add_column("Cache Read", justify="right")
                table.add_column("Output", justify="right")
                table.add_column("Total", justify="right")
                table.add_column("Cost", justify="right")

                for model in sorted(tokens_by_model.keys()):
                    t = tokens_by_model[model]
                    cost = cost_by_model.get(model)
                    table.add_row(
                        model,
                        f"{t.get('input_tokens', 0):,}",
                        f"{t.get('cache_creation_tokens', 0):,}",
                        f"{t.get('cached_input_tokens', 0):,}",
                        f"{t.get('output_tokens', 0):,}",
                        f"{t.get('total_tokens', 0):,}",
                        format_money(cost),
                    )
                console.print(table)
                cost_mode = s.get("cost_mode_used")
                cost_src = s.get("cost_source")
                console.print(f"\n[dim]Cost Mode: {cost_mode} | Source: {cost_src}[/dim]")
                console.print(f"[dim]Pricing Rates: {s.get('cost_note')}[/dim]")

        elif tab == "signals":
            readings = s.get("signals", [])
            if not readings:
                console.print("[dim]No signals computed for this session.[/dim]")
            else:
                by_group = defaultdict(list)
                for r in readings:
                    by_group[r.get("group")].append(r)

                group_colors = {"activity": "blue", "outcome": "teal", "cost": "orange3", "risk": "red"}

                for g in ("activity", "outcome", "cost", "risk"):
                    if g not in by_group:
                        continue
                    color = group_colors.get(g, "white")
                    console.print(f"[{color}][bold]# {g.upper()}[/bold][/{color}]")
                    for r in sorted(by_group[g], key=lambda x: x.get("signal", "")):
                        val = r.get("value")
                        val_str = "∅ (missing)" if val is None else str(val)
                        unit = f" {r.get('unit')}" if r.get("unit") else ""
                        conf = (
                            f" conf={r.get('confidence')}" if r.get("confidence") not in (None, 1.0) else ""
                        )
                        meta = f"  {json.dumps(r.get('metadata'))}" if r.get("metadata") else ""
                        console.print(f"  {r.get('signal'):<28} [bold]{val_str}[/bold]{unit}{conf}{meta}")
                    console.print()

        elif tab == "transcript":
            rendered_md = s.get("rendered_markdown", "")
            if not rendered_md:
                console.print("[yellow]No rendered transcript found for this session.[/yellow]")
            else:
                console.print("Opening scrollable transcript in pager...")
                input("Press Enter to launch pager (use Arrow keys to scroll, 'q' to exit).")
                with console.pager(styles=True):
                    console.print(Markdown(rendered_md))
                # Reset to summary after returning from pager to avoid infinite loop
                tab = "summary"
                continue

        elif tab == "memory":
            mined = s.get("mined", [])
            if not mined:
                console.print("[dim]No mined memory found for this session.[/dim]")
            else:
                for entry in mined:
                    method = entry.get("method", "unknown")
                    cands = entry.get("candidates", [])
                    filters = (
                        f" (filters: {', '.join(entry.get('filters_applied', []))})"
                        if entry.get("filters_applied")
                        else ""
                    )
                    cand_count = entry.get("candidate_count", 0)
                    panel_text = (
                        f"Method: [bold]{method}[/bold]{filters} | "
                        f"[bold]{cand_count}[/bold] candidates"
                    )
                    console.print(Panel(panel_text, style="orange3"))

                    for c in cands:
                        kind = c.get("kind", "unknown")
                        scope = c.get("scope", "repo")
                        risk = c.get("risk", "medium")
                        risk_col = "red" if risk == "high" else "green" if risk == "low" else "yellow"

                        priority = c.get("priority")
                        conf = c.get("confidence", 0.0)
                        header = (
                            f"[bold reverse]{kind.upper()}[/bold reverse]  "
                            f"scope:{scope}  "
                            f"[bold {risk_col}]risk:{risk}[/bold {risk_col}]  "
                            f"priority:{priority}  "
                            f"conf:{conf:.2f}"
                        )
                        body = f"[bold]{c.get('text')}[/bold]"
                        if c.get("when_to_use"):
                            body += f"\n[dim]When to use: {c.get('when_to_use')}[/dim]"

                        # Structured steps
                        struct = c.get("structured")
                        if struct:
                            lines = []
                            if kind == "skill":
                                if struct.get("activation"):
                                    lines.append(f"  Activation: {struct['activation']}")
                                if struct.get("steps"):
                                    lines.append("  Steps:")
                                    for idx, step in enumerate(struct["steps"], start=1):
                                        lines.append(f"    {idx}. {step}")
                                if struct.get("termination"):
                                    lines.append(f"  Termination: {struct['termination']}")
                                if struct.get("verification"):
                                    lines.append(f"  Verification: {struct['verification']}")
                            elif kind == "procedure":
                                if struct.get("goal"):
                                    lines.append(f"  Goal: {struct['goal']}")
                                if struct.get("preconditions"):
                                    preconds = ", ".join(struct["preconditions"])
                                    lines.append(f"  Preconditions: {preconds}")
                                if struct.get("steps"):
                                    lines.append("  Steps:")
                                    for idx, step in enumerate(struct["steps"], start=1):
                                        lines.append(f"    {idx}. {step}")
                                if struct.get("warnings"):
                                    lines.append(f"  Warnings: {'; '.join(struct['warnings'])}")
                                if struct.get("outcome"):
                                    lines.append(f"  Outcome: {struct['outcome']}")
                            if lines:
                                body += "\n" + "\n".join(lines)

                        evidence = " ".join([f"`{e[:12]}...`" for e in c.get("evidence_refs", [])])
                        if evidence:
                            body += f"\n[dim]Evidence: {evidence}[/dim]"

                        console.print(Panel(body, title=header))

                    # Prompt text option
                    if entry.get("prompt_text"):
                        console.print()
                        prompt_msg = (
                            f"Press [p] to view raw prompt block for "
                            f"method '{method}' (or Enter to skip): "
                        )
                        sub_choice = input(prompt_msg).strip().lower()
                        if sub_choice == "p":
                            with console.pager(styles=True):
                                console.print(Markdown(entry.get("prompt_text")))
                            break

        console.print()
        console.print("Navigate:")
        console.print("  [bold]u[/bold] Summary   [bold]o[/bold] Models   [bold]i[/bold] Signals")
        console.print("  [bold]t[/bold] Transcript pager")
        console.print("  [bold]y[/bold] Memory details")
        console.print("  [bold]b[/bold] Back to sessions list")
        console.print()

        choice = input("Choice: ").strip().lower()
        if choice == "b":
            break
        elif choice in ("u", "summary"):
            tab = "summary"
        elif choice in ("o", "models"):
            tab = "models"
        elif choice in ("i", "signals"):
            tab = "signals"
        elif choice in ("t", "transcript"):
            tab = "transcript"
        elif choice in ("y", "memory"):
            tab = "memory"


def show_memory_aggregates(data: dict[str, Any]) -> None:
    """Display portfolio-level memory aggregates."""
    while True:
        clear_screen()
        mem = data.get("memory", {})
        if not mem:
            console.print("[yellow]No memory aggregates found.[/yellow]")
            input("\nPress Enter to return.")
            break

        console.print("[bold green]MEMORY · MINED ACROSS PORTFOLIO[/bold green]")
        console.print("─" * 80)
        console.print()

        # Overview Grid
        console.print(f"Sessions with mined memory: [bold]{mem.get('sessions_with_memory', 0)}[/bold]")
        console.print(f"Total candidates:           [bold]{mem.get('candidate_count', 0)}[/bold]")
        console.print()

        # By Method Table
        by_method = mem.get("by_method", {})
        method_sessions = mem.get("method_session_counts", {})
        method_table = Table(title="By Method")
        method_table.add_column("Method")
        method_table.add_column("Sessions Mined", justify="right")
        method_table.add_column("Candidates Count", justify="right")
        for method, count in by_method.items():
            method_table.add_row(method, str(method_sessions.get(method, 0)), f"{count:,}")
        console.print(method_table)
        console.print()

        # By Kind Table
        by_kind = mem.get("by_kind", {})
        kind_max = max(by_kind.values()) if by_kind else 1
        kind_table = Table(title="By Kind")
        kind_table.add_column("Kind")
        kind_table.add_column("Candidates", justify="right")
        kind_table.add_column("Distribution Bar")
        for kind, count in by_kind.items():
            bar_len = int((count / kind_max) * 20)
            bar = "█" * bar_len
            kind_table.add_row(kind, f"{count:,}", f"[teal]{bar}[/teal]")
        console.print(kind_table)
        console.print()

        # Top Candidates preview
        top_candidates = mem.get("top_candidates", [])
        console.print("[bold]Top Candidates Preview[/bold] (top 6 shown)")
        for c in top_candidates[:6]:
            host = c.get("host", "")
            host_badge = f"[orange3]{host}[/orange3]" if host == "claude-code" else f"[blue]{host}[/blue]"
            c_kind = c.get("kind", "unknown")
            c_priority = c.get("priority")
            c_conf = c.get("confidence", 0.0)
            c_method = c.get("method")
            panel_title = f"{c_kind} | priority:{c_priority} | conf:{c_conf:.2f} | method:{c_method}"
            console.print(
                Panel(
                    f"[bold]{c.get('text')}[/bold]\n"
                    f"[dim]When to use: {c.get('when_to_use') or 'n/a'}[/dim]\n"
                    f"[dim]From session ({host_badge}): {c.get('title')}[/dim]",
                    title=panel_title,
                )
            )

        console.print()
        console.print("Options:")
        console.print("  [bold]b[/bold] Back to main menu")
        console.print()
        choice = input("Choice: ").strip().lower()
        if choice == "b":
            break


def show_all_memories_browser(data: dict[str, Any]) -> None:
    """Interactive all-memories browser with pagination and filters."""
    mem = data.get("memory", {})
    all_candidates = mem.get("all_candidates", [])

    page = 0
    page_size = 5
    host_filter = "all"
    scope_filter = "all"
    kind_filter = "all"
    method_filter = "all"
    risk_filter = "all"
    search_query = ""

    # Unique values for filter list
    kinds = sorted(list(set(c.get("kind") for c in all_candidates if c.get("kind"))))
    methods = sorted(list(set(c.get("method") for c in all_candidates if c.get("method"))))

    while True:
        clear_screen()
        # Filter candidates
        q = search_query.lower()
        filtered = []
        for c in all_candidates:
            if host_filter != "all" and c.get("host") != host_filter:
                continue
            if scope_filter != "all" and c.get("scope") != scope_filter:
                continue
            if kind_filter != "all" and c.get("kind") != kind_filter:
                continue
            if method_filter != "all" and c.get("method") != method_filter:
                continue
            if risk_filter != "all" and c.get("risk") != risk_filter:
                continue
            if q:
                parts = [
                    c.get("text", ""),
                    c.get("when_to_use", ""),
                    c.get("title", ""),
                    c.get("origin_repo", ""),
                ]
                haystack = " ".join(parts).lower()
                if q not in haystack:
                    continue
            filtered.append(c)

        # Sort filtered stable
        scope_order = {"user": 0, "global": 1, "task": 2, "repo": 3}
        filtered.sort(
            key=lambda x: (
                -(x.get("priority") or 0),
                -(x.get("confidence") or 0.0),
                scope_order.get(x.get("scope", "repo"), 9),
            )
        )

        total_mems = len(filtered)
        max_page = max(0, (total_mems - 1) // page_size)
        page = min(page, max_page)

        start_idx = page * page_size
        end_idx = min(start_idx + page_size, total_mems)
        page_candidates = filtered[start_idx:end_idx]

        # Scope counts header line
        scope_counts = mem.get("by_scope", {})
        scope_summary_parts = []
        for s in ("user", "repo", "task", "global"):
            scope_summary_parts.append(f"{s}: [bold]{scope_counts.get(s, 0)}[/bold]")
        scope_summary = "  |  ".join(scope_summary_parts)

        console.print(Panel(scope_summary, title="Scope Counts (Total Corpus)"))
        all_mems_title = (
            f"[bold green]ALL MEMORIES · FULL DETAIL[/bold green] "
            f"(showing {len(filtered)} of {len(all_candidates)})"
        )
        console.print(all_mems_title)
        console.print(f"Page {page + 1}/{max_page + 1}")
        console.print("─" * 80)
        console.print()

        for c in page_candidates:
            host = c.get("host", "")
            host_badge = f"[orange3]{host}[/orange3]" if host == "claude-code" else f"[blue]{host}[/blue]"

            c_scope = c.get("scope")
            c_reason = c.get("scope_reason", "default")
            c_risk = c.get("risk", "medium")
            title_str = (
                f"From {host_badge} ({c.get('session_id', '')[:12]}...) · {c.get('title')}\n"
                f"Scope: {c_scope} (reason: {c_reason}) | risk: {c_risk}"
            )

            body = f"[bold]{c.get('text')}[/bold]"
            if c.get("when_to_use"):
                body += f"\n[dim]When to use: {c.get('when_to_use')}[/dim]"

            struct = c.get("structured")
            if struct:
                lines = []
                kind = c.get("kind")
                if kind == "skill":
                    if struct.get("activation"):
                        lines.append(f"  Activation: {struct['activation']}")
                    if struct.get("steps"):
                        lines.append("  Steps:")
                        for idx, step in enumerate(struct["steps"], start=1):
                            lines.append(f"    {idx}. {step}")
                    if struct.get("termination"):
                        lines.append(f"  Termination: {struct['termination']}")
                    if struct.get("verification"):
                        lines.append(f"  Verification: {struct['verification']}")
                elif kind == "procedure":
                    if struct.get("goal"):
                        lines.append(f"  Goal: {struct['goal']}")
                    if struct.get("preconditions"):
                        lines.append(f"  Preconditions: {', '.join(struct['preconditions'])}")
                    if struct.get("steps"):
                        lines.append("  Steps:")
                        for idx, step in enumerate(struct["steps"], start=1):
                            lines.append(f"    {idx}. {step}")
                    if struct.get("warnings"):
                        lines.append(f"  Warnings: {'; '.join(struct['warnings'])}")
                    if struct.get("outcome"):
                        lines.append(f"  Outcome: {struct['outcome']}")
                if lines:
                    body += "\n" + "\n".join(lines)

            evidence = " ".join([f"`{e[:12]}...`" for e in c.get("evidence_refs", [])])
            if evidence:
                body += f"\n[dim]Evidence: {evidence}[/dim]"

            body += f"\n\n[dim]{title_str}[/dim]"

            c_kind = c.get("kind", "unknown").upper()
            c_priority = c.get("priority")
            c_conf = c.get("confidence", 0.0)
            c_method = c.get("method")
            header = (
                f"{c_kind} | priority:{c_priority} | "
                f"conf:{c_conf:.2f} | method:{c_method}"
            )
            console.print(Panel(body, title=header))

        if not page_candidates:
            console.print("[dim]No memories match current filters.[/dim]")
            console.print()

        # Filters status line
        filt_str = (
            f"host={host_filter} | scope={scope_filter} | "
            f"kind={kind_filter} | method={method_filter} | "
            f"risk={risk_filter} | search={search_query or 'None'}"
        )
        console.print(f"[bold]Active Filters:[/bold] {filt_str}")
        console.print()
        console.print("Options:")
        console.print("  [bold]n[/bold] Next page   |  [bold]p[/bold] Prev page")
        console.print(
            "  [bold]h[/bold] Filter host |  [bold]o[/bold] Filter scope |  [bold]k[/bold] Filter kind"
        )
        console.print(
            "  [bold]m[/bold] Filter meth |  [bold]r[/bold] Filter risk  |  [bold]s[/bold] Search text"
        )
        console.print("  [bold]b[/bold] Back to main menu")
        console.print()

        choice = input("Choice: ").strip().lower()
        if choice == "b":
            break
        elif choice == "n":
            page = min(page + 1, max_page)
        elif choice == "p":
            page = max(page - 1, 0)
        elif choice == "h":
            host_filter = (
                "claude-code" if host_filter == "all" else "codex" if host_filter == "claude-code" else "all"
            )
            page = 0
        elif choice == "o":
            scopes = ("all", "user", "repo", "task", "global")
            scope_filter = scopes[(scopes.index(scope_filter) + 1) % len(scopes)]
            page = 0
        elif choice == "k":
            if kind_filter in kinds:
                idx = (kinds.index(kind_filter) + 1) % len(kinds)
                kind_filter = kinds[idx]
            else:
                kind_filter = kinds[0] if kinds else "all"
            page = 0
        elif choice == "m":
            if method_filter in methods:
                idx = (methods.index(method_filter) + 1) % len(methods)
                method_filter = methods[idx]
            else:
                method_filter = methods[0] if methods else "all"
            page = 0
        elif choice == "r":
            r_options = ("all", "low", "medium", "high")
            risk_filter = r_options[(r_options.index(risk_filter) + 1) % len(r_options)]
            page = 0
        elif choice == "s":
            search_query = input("Search text (press Enter to clear): ").strip()
            page = 0


def show_signal_aggregates(data: dict[str, Any]) -> None:
    """Display portfolio-level signal aggregates."""
    while True:
        clear_screen()
        signals = data.get("signals", {})
        by_signal = signals.get("by_signal", {})
        if not by_signal:
            console.print("[yellow]No signals aggregates data found.[/yellow]")
            input("\nPress Enter to return.")
            break

        console.print("[bold green]SIGNALS · PORTFOLIO AGGREGATES[/bold green]")
        console.print("─" * 80)
        console.print()

        # Table of aggregates
        table = Table(title="Signal Aggregates across Portfolio")
        table.add_column("Group", justify="center")
        table.add_column("Signal Name")
        table.add_column("Summary Value")
        table.add_column("Coverage Details")

        group_colors = {"activity": "blue", "outcome": "teal", "cost": "orange3", "risk": "red"}

        for name in sorted(by_signal.keys()):
            a = by_signal[name]
            group = a.get("group", "activity")
            g_color = group_colors.get(group, "white")
            group_disp = f"[{g_color}]{group.upper()}[/{g_color}]"

            value = "—"
            details = f"n={a.get('sessions_with_reading', 0)} sessions"
            kind = a.get("kind")

            if kind == "numeric" and a.get("mean") is not None:
                mean = a["mean"]
                mean_str = f"{mean}" if isinstance(mean, int) else f"{mean:.2f}"
                value = f"mean {mean_str} {a.get('unit', '')}"
                read_count = a.get("sessions_with_reading")
                details = (
                    f"median {a.get('median')} · max {a.get('max')} · n={read_count}"
                )
            elif kind == "boolean":
                value = f"{a.get('true_count')}/{a.get('true_count', 0) + a.get('false_count', 0)} true"
                details = f"ratio {a.get('true_ratio', 0):.2f}"
            elif kind == "categorical" and a.get("histogram"):
                hist = a.get("histogram") or {}
                top_3 = sorted(hist.items(), key=lambda x: x[1], reverse=True)[:3]
                value = ", ".join(f"{k}={v}" for k, v in top_3)

            table.add_row(group_disp, name, value, details)

        console.print(table)
        console.print()
        console.print("Options:")
        console.print("  [bold]b[/bold] Back to main menu")
        console.print()
        choice = input("Choice: ").strip().lower()
        if choice == "b":
            break


def show_accounting_costs(data: dict[str, Any]) -> None:
    """Render the Accounting & Costs TUI view."""
    while True:
        clear_screen()
        summary = data.get("summary", {})
        cats = summary.get("cost_categories") or {
            "input": 0.0,
            "cache_create": 0.0,
            "cache_read": 0.0,
            "output": 0.0,
        }

        # KPIs grid
        kpi_table = Table.grid(expand=True, padding=1)
        kpi_table.add_column(ratio=1)
        kpi_table.add_column(ratio=1)
        kpi_table.add_column(ratio=1)
        kpi_table.add_column(ratio=1)

        input_panel = Panel(
            f"[bold]{format_money(cats.get('input'))}[/bold]", title="Input Cost"
        )
        cc_panel = Panel(
            f"[bold]{format_money(cats.get('cache_create'))}[/bold]",
            title="Cache Create Cost",
        )
        cr_panel = Panel(
            f"[bold]{format_money(cats.get('cache_read'))}[/bold]",
            title="Cache Read Cost",
        )
        output_panel = Panel(
            f"[bold]{format_money(cats.get('output'))}[/bold]", title="Output Cost"
        )

        kpi_table.add_row(input_panel, cc_panel, cr_panel, output_panel)

        console.print(
            Panel(
                kpi_table,
                title="[bold green]Accounting & Costs Summary[/bold green]",
                subtitle=f"Total Cost: [bold]{format_money(summary.get('estimated_cost_usd'))}[/bold]",
            )
        )
        console.print()

        # Legend
        console.print(
            "Legend: [blue]█[/blue] Input  "
            "[orange3]█[/orange3] Cache Create  "
            "[yellow]█[/yellow] Cache Read  "
            "[green]█[/green] Output"
        )
        console.print()

        # Daily Table
        table = Table(title="Daily Cost Breakdown")
        table.add_column("Date", style="cyan", width=12)
        table.add_column("Breakdown", width=35)
        table.add_column("Input", justify="right", style="blue")
        table.add_column("Cache Create", justify="right", style="orange3")
        table.add_column("Cache Read", justify="right", style="yellow")
        table.add_column("Output", justify="right", style="green")
        table.add_column("Total Cost", justify="right", style="bold white")

        by_day = summary.get("by_day", {})
        for day, d in sorted(by_day.items()):
            ci = d.get("cost_input", 0.0)
            ccc = d.get("cost_cache_create", 0.0)
            ccr = d.get("cost_cache_read", 0.0)
            co = d.get("cost_output", 0.0)
            total = d.get("cost", 0.0)

            sum_cats = ci + ccc + ccr + co
            width = 30
            if sum_cats > 0:
                ci_len = int(round((ci / sum_cats) * width))
                ccc_len = int(round((ccc / sum_cats) * width))
                ccr_len = int(round((ccr / sum_cats) * width))
                # Adjust output length to keep total length constant
                co_len = max(0, width - (ci_len + ccc_len + ccr_len))

                # Handle minimal representations if cost is positive
                if ci > 0 and ci_len == 0:
                    ci_len = 1
                if ccc > 0 and ccc_len == 0:
                    ccc_len = 1
                if ccr > 0 and ccr_len == 0:
                    ccr_len = 1
                if co > 0 and co_len == 0:
                    co_len = 1

                bar_str = (
                    "[blue]" + "█" * ci_len + "[/blue]"
                    + "[orange3]" + "█" * ccc_len + "[/orange3]"
                    + "[yellow]" + "█" * ccr_len + "[/yellow]"
                    + "[green]" + "█" * co_len + "[/green]"
                )
            else:
                bar_str = "[dim]░" * width + "[/dim]"

            table.add_row(
                day,
                bar_str,
                format_money(ci),
                format_money(ccc),
                format_money(ccr),
                format_money(co),
                format_money(total),
            )

        console.print(table)
        console.print()
        console.print("  [bold]b[/bold] Back to main menu")
        console.print()
        choice = input("Choice: ").strip().lower()
        if choice == "b":
            break


def run_terminal_dashboard(mode: str = "auto") -> None:
    """Main runner for the terminal dashboard."""
    data = load_dashboard_data(mode)

    if not is_interactive():
        run_non_interactive(data)
        return

    while True:
        clear_screen()
        # 1. KPIs
        show_portfolio_kpis(data)

        # 2. Activity By Day Histogram
        summary = data.get("summary", {})
        by_day = summary.get("by_day", {})
        render_activity_histogram(by_day)

        # Main menu options
        console.print("Main Menu:")
        console.print("  [bold]s[/bold]  Browse Sessions List")
        console.print("  [bold]c[/bold]  Accounting & Costs")
        console.print("  [bold]m[/bold]  Mined Memory Aggregates")
        console.print("  [bold]a[/bold]  All Memories Browser")
        console.print("  [bold]g[/bold]  Signal Aggregates")
        console.print("  [bold]q[/bold]  Quit")
        console.print()

        choice = input("Choice: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            console.print("[green]Goodbye![/green]")
            break
        elif choice in ("s", "sessions"):
            show_session_list(data)
        elif choice in ("c", "costs", "accounting"):
            show_accounting_costs(data)
        elif choice in ("m", "memory", "aggregates"):
            show_memory_aggregates(data)
        elif choice in ("a", "all-memories"):
            show_all_memories_browser(data)
        elif choice in ("g", "signals"):
            show_signal_aggregates(data)
