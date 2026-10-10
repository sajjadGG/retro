"""Aggregations over the fact table, shared by the terminal view and tests.

The browser implements the same reductions in JavaScript; this module keeps the
Python side (terminal view, tests, CLI summaries) honest about the semantics.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .facts import COLUMN_INDEX, COST_SCALE, FLAGS, RATIO_SCALE, Dictionaries

Row = list


def value(row: Row, column: str) -> Any:
    return row[COLUMN_INDEX[column]]


def has_flag(row: Row, name: str) -> bool:
    return bool(int(row[COLUMN_INDEX["flags"]] or 0) & FLAGS[name])


def cost_usd(row: Row) -> float:
    raw = row[COLUMN_INDEX["cost"]]
    return (raw / COST_SCALE) if raw is not None else 0.0


def error_rate(row: Row) -> float | None:
    raw = row[COLUMN_INDEX["errors"]]
    return (raw / RATIO_SCALE) if raw is not None else None


@dataclass
class Filters:
    include_excluded: bool = False
    since: float | None = None
    until: float | None = None
    agents: set | None = None
    clients: set | None = None
    projects: set | None = None
    tags: set | None = None
    outcomes: set | None = None
    work: set | None = None
    interactive: bool | None = None
    query: str | None = None


def filter_rows(rows: Iterable[Row], filters: Filters, dictionaries: Dictionaries) -> list[Row]:
    agents = _indexes(filters.agents, dictionaries.agents)
    clients = _indexes(filters.clients, dictionaries.clients)
    projects = _indexes(filters.projects, [entry["id"] for entry in dictionaries.projects])
    tags = _indexes(filters.tags, dictionaries.tags)
    outcomes = _indexes(filters.outcomes, dictionaries.outcomes)
    work = _indexes(filters.work, dictionaries.work)
    query = (filters.query or "").strip().lower()

    selected: list[Row] = []
    for row in rows:
        if not filters.include_excluded and has_flag(row, "excluded"):
            continue
        started = value(row, "started")
        if filters.since is not None and (started is None or started < filters.since):
            continue
        if filters.until is not None and (started is None or started > filters.until):
            continue
        if agents is not None and value(row, "agent") not in agents:
            continue
        if clients is not None and value(row, "client") not in clients:
            continue
        if projects is not None and value(row, "project") not in projects:
            continue
        if outcomes is not None and value(row, "outcome") not in outcomes:
            continue
        if work is not None and value(row, "work") not in work:
            continue
        if tags is not None and not (tags & set(value(row, "tags") or ())):
            continue
        if filters.interactive is True and not has_flag(row, "interactive"):
            continue
        if filters.interactive is False and not has_flag(row, "automated"):
            continue
        if query:
            haystack = " ".join(
                str(part or "") for part in (value(row, "key"), value(row, "title"), value(row, "prompt"))
            ).lower()
            if query not in haystack:
                continue
        selected.append(row)
    return selected


@dataclass
class Kpis:
    sessions: int = 0
    active_s: int = 0
    duration_s: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    user_turns: int = 0
    tool_calls: int = 0
    edits: int = 0
    corrections: int = 0
    sessions_with_cost: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    work: dict[str, int] = field(default_factory=dict)
    agents: dict[str, int] = field(default_factory=dict)
    projects: dict[str, int] = field(default_factory=dict)
    days: set = field(default_factory=set)
    first: int | None = None
    last: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "active_s": self.active_s,
            "duration_s": self.duration_s,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": self.tokens,
            "user_turns": self.user_turns,
            "tool_calls": self.tool_calls,
            "edits": self.edits,
            "corrections": self.corrections,
            "sessions_with_cost": self.sessions_with_cost,
            "outcomes": self.outcomes,
            "work": self.work,
            "agents": self.agents,
            "projects": len(self.projects),
            "active_days": len(self.days),
            "first": self.first,
            "last": self.last,
        }


def kpis(rows: Iterable[Row], dictionaries: Dictionaries) -> Kpis:
    result = Kpis()
    for row in rows:
        result.sessions += 1
        result.active_s += int(value(row, "active") or 0)
        result.duration_s += int(value(row, "duration") or 0)
        cost = value(row, "cost")
        if cost is not None:
            result.cost_usd += cost / COST_SCALE
            result.sessions_with_cost += 1
        result.tokens += int(value(row, "tokens") or 0)
        result.user_turns += int(value(row, "turns") or 0)
        result.tool_calls += int(value(row, "tools") or 0)
        result.edits += int(value(row, "edits") or 0)
        result.corrections += int(value(row, "corrections") or 0)
        outcome = _name(dictionaries.outcomes, value(row, "outcome"))
        result.outcomes[outcome] = result.outcomes.get(outcome, 0) + 1
        work = _name(dictionaries.work, value(row, "work"))
        result.work[work] = result.work.get(work, 0) + 1
        agent = _name(dictionaries.agents, value(row, "agent"))
        result.agents[agent] = result.agents.get(agent, 0) + 1
        project = _name([entry["id"] for entry in dictionaries.projects], value(row, "project"))
        result.projects[project] = result.projects.get(project, 0) + 1
        started = value(row, "started")
        if started is not None:
            result.days.add(int(started) // 86400)
            result.first = started if result.first is None else min(result.first, started)
            result.last = started if result.last is None else max(result.last, started)
    return result


def group_rows(rows: Iterable[Row], column: str) -> dict[Any, list[Row]]:
    groups: dict[Any, list[Row]] = {}
    for row in rows:
        groups.setdefault(value(row, column), []).append(row)
    return groups


def project_rollup(rows: Iterable[Row], dictionaries: Dictionaries) -> list[dict[str, Any]]:
    groups = group_rows(rows, "project")
    rollup: list[dict[str, Any]] = []
    for project_index, project_rows in groups.items():
        stats = kpis(project_rows, dictionaries)
        meta = (
            dictionaries.projects[project_index]
            if isinstance(project_index, int) and 0 <= project_index < len(dictionaries.projects)
            else {"id": "unknown", "name": "unknown", "source": "unknown"}
        )
        agents: dict[str, int] = {}
        clients: dict[str, int] = {}
        for row in project_rows:
            agent = _name(dictionaries.agents, value(row, "agent"))
            agents[agent] = agents.get(agent, 0) + 1
            client = _name(dictionaries.clients, value(row, "client"))
            clients[client] = clients.get(client, 0) + 1
        entry = stats.to_dict()
        entry.update(
            {
                "id": meta.get("id"),
                "name": meta.get("name"),
                "source": meta.get("source"),
                "remote": meta.get("remote"),
                "agents": agents,
                "clients": clients,
            }
        )
        rollup.append(entry)
    rollup.sort(key=lambda item: (-float(item["cost_usd"]), -int(item["sessions"])))
    return rollup


def trend(current: Kpis, previous: Kpis) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for name in ("sessions", "active_s", "cost_usd", "tokens", "tool_calls", "edits", "user_turns"):
        now = getattr(current, name)
        before = getattr(previous, name)
        if not before:
            result[name] = None if not now else 1.0
        else:
            result[name] = (now - before) / before
    return result


def success_rate(stats: Kpis) -> float | None:
    judged = sum(count for name, count in stats.outcomes.items() if name != "unknown")
    if not judged:
        return None
    return stats.outcomes.get("success", 0) / judged


def _name(names: list, index: Any) -> str:
    if isinstance(index, int) and 0 <= index < len(names):
        return str(names[index])
    return "unknown"


def _indexes(selected: set | None, names: list) -> set | None:
    if not selected:
        return None
    lookup = {name: position for position, name in enumerate(names)}
    return {lookup[name] for name in selected if name in lookup}
