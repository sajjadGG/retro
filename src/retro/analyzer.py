"""Deprecated shim: command/tool analysis moved into `retro.signals.analyze`.

Kept only so the legacy `dashboard_build.py` keeps importing; it is scheduled
for deletion with that module.
"""

from __future__ import annotations

from typing import Any

from .signals.analyze import analyze_sessions, generate_report, render_console_report, session_tips
from .signals.commands import get_base_command

__all__ = [
    "analyze_operator_portfolio",
    "analyze_sessions",
    "generate_report",
    "get_base_command",
    "render_console_report",
    "session_tips",
]


def analyze_operator_portfolio(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Portfolio-level operator hygiene, computed from signal values."""
    if not sessions:
        return {
            "sessions": 0,
            "avg_turns": 0.0,
            "tool_error_rate": 0.0,
            "avg_cost": 0.0,
            "recommendations": [],
        }

    def mean(key: str) -> float:
        values = [
            float(session.get("signals_index", {}).get(key))
            for session in sessions
            if isinstance(session.get("signals_index", {}).get(key), (int, float))
        ]
        return sum(values) / len(values) if values else 0.0

    avg_turns = mean("activity.user_turns") or (
        sum(session.get("user_messages", 0) for session in sessions) / len(sessions)
    )
    tool_error_rate = mean("friction.tool_error_rate")
    avg_cost = mean("usage.cost_usd") or (
        sum(session.get("estimated_cost_usd") or 0.0 for session in sessions) / len(sessions)
    )
    corrections = mean("friction.user_corrections")

    recommendations: list[str] = []
    if avg_turns > 25:
        recommendations.append(
            "Sessions average over 25 human turns. Splitting work into fresh sessions avoids "
            "paying the context tax on every turn."
        )
    if tool_error_rate > 0.20:
        recommendations.append(
            "More than 20% of tool results fail. Agreeing on a plan before editing removes most retry loops."
        )
    if corrections > 2:
        recommendations.append(
            "More than two corrections per session on average. Front-loading constraints into the "
            "opening prompt is cheaper than steering mid-flight."
        )
    if not recommendations:
        recommendations.append("No portfolio-level anti-patterns detected.")

    return {
        "sessions": len(sessions),
        "avg_turns": round(avg_turns, 1),
        "tool_error_rate": round(tool_error_rate, 3),
        "avg_cost": round(avg_cost, 4),
        "avg_corrections": round(corrections, 2),
        "recommendations": recommendations,
    }
