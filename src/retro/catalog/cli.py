"""Typer command groups for sessions, rules, and projects."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from ..config import (
    load_config,
    update_config,
    user_state_dir,
    validate_rule_spec,
)
from ..locking import exclusive_lock
from ..storage import Layout, default_layout
from . import Catalog, SessionRecord, load_catalog, update_catalog
from .annotations import append_annotation, load_annotations
from .rules import CatalogRule, catalog_rules, rule_matches

sessions_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect, explain, and annotate the archived-session catalog.",
)
rules_app = typer.Typer(
    no_args_is_help=True,
    help="Manage catalog classification and exclusion rules.",
)
projects_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect and consolidate projects across hosts and worktrees.",
)
console = Console()


@sessions_app.command("list")
def sessions_list(
    host: Optional[str] = typer.Option(None, "--host"),
    agent: Optional[str] = typer.Option(None, "--agent"),
    client: Optional[str] = typer.Option(None, "--client"),
    project: Optional[str] = typer.Option(None, "--project"),
    tags: Optional[list[str]] = typer.Option(None, "--tag"),
    excluded: Optional[bool] = typer.Option(None, "--excluded/--included"),
    automated: Optional[bool] = typer.Option(None, "--automated/--interactive"),
    since: Optional[str] = typer.Option(None, "--since"),
    search: Optional[str] = typer.Option(None, "--search"),
    limit: int = typer.Option(100, "--limit", min=0),
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """List catalog records with composable filters."""
    layout, catalog = _refresh(root)
    records = list(catalog.values())
    if host:
        records = [record for record in records if _same(record.host, host)]
    if agent:
        records = [record for record in records if _same(record.agent, agent)]
    if client:
        records = [record for record in records if _same(record.client, client)]
    if project:
        records = [
            record
            for record in records
            if _same(str(record.project.get("id") or ""), project)
            or _same(str(record.project.get("name") or ""), project)
        ]
    for tag in tags or []:
        records = [record for record in records if tag in record.tags]
    if excluded is not None:
        records = [record for record in records if record.excluded is excluded]
    if automated is not None:
        target = False if automated else True
        records = [record for record in records if record.interactive is target]
    if since:
        boundary = _parse_datetime(since, "--since")
        records = [
            record
            for record in records
            if (started := _record_datetime(record.started_at)) is not None
            and started >= boundary
        ]
    if search:
        needle = search.casefold()
        records = [
            record
            for record in records
            if needle
            in "\n".join(
                [
                    record.key,
                    record.title or "",
                    record.first_prompt or "",
                    record.cwd or "",
                    str(record.project.get("id") or ""),
                    str(record.project.get("name") or ""),
                ]
            ).casefold()
        ]
    records.sort(key=lambda record: (record.started_at or "", record.key), reverse=True)
    if limit:
        records = records[:limit]
    elif limit == 0:
        records = []
    if json_output:
        _emit_json([record.to_dict() for record in records])
        return
    table = Table(title=f"Sessions ({len(records)} shown, {len(catalog)} cataloged)")
    table.add_column("key", overflow="fold")
    table.add_column("client")
    table.add_column("project")
    table.add_column("started")
    table.add_column("tags", overflow="fold")
    table.add_column("excluded")
    for record in records:
        table.add_row(
            record.key,
            record.client,
            str(record.project.get("name") or record.project.get("id") or "unknown"),
            record.started_at or "-",
            ", ".join(record.tags) or "-",
            "yes" if record.excluded else "no",
        )
    console.print(table)
    console.print(f"catalog: {layout.root / 'catalog' / 'sessions.jsonl'}")


@sessions_app.command("show")
def sessions_show(
    selector: str = typer.Argument(..., help="host/session-id or host"),
    session_id: Optional[str] = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Show one complete session record."""
    _, catalog = _refresh(root)
    record = _require_record(catalog, _selector_key(selector, session_id))
    if json_output:
        _emit_json(record.to_dict())
    else:
        _render_record(record)


@sessions_app.command("explain")
def sessions_explain(
    selector: str = typer.Argument(..., help="host/session-id or host"),
    session_id: Optional[str] = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Explain every tag, exclusion decision, and winning project evidence."""
    layout, catalog = _refresh(root)
    record = _require_record(catalog, _selector_key(selector, session_id))
    config = load_config()
    annotations = load_annotations(layout.root).get(record.key)
    excluded_by = sorted(set(record.tags) & set(config.exclude_tags))
    explanation = {
        "key": record.key,
        "excluded": record.excluded,
        "excluded_by_tags": excluded_by,
        "exclude_tags": config.exclude_tags,
        "tags": record.tags,
        "tag_sources": record.tag_sources,
        "project": record.project,
        "project_decision": (
            f"{record.project.get('source')}: {record.project.get('evidence')}"
        ),
        "markers": record.markers,
        "annotations": annotations.entries if annotations else [],
        "notes": annotations.notes if annotations else [],
    }
    if json_output:
        _emit_json(explanation)
        return
    console.print(f"[bold]{record.key}[/bold]")
    if record.excluded:
        console.print(
            f"excluded: yes ({', '.join(excluded_by)} intersects configured exclude_tags)"
        )
    else:
        console.print("excluded: no (no session tag intersects configured exclude_tags)")
    for tag in record.tags:
        console.print(f"tag {tag}: {', '.join(record.tag_sources.get(tag, []))}")
    console.print(
        f"project {record.project.get('id')}: "
        f"{record.project.get('source')} — {record.project.get('evidence') or 'no evidence'}"
    )


@sessions_app.command("tag")
def sessions_tag(
    selector: str = typer.Argument(...),
    tag: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Add a canonical manual tag annotation."""
    _annotate(selector, "tag", tag, root)


@sessions_app.command("untag")
def sessions_untag(
    selector: str = typer.Argument(...),
    tag: str = typer.Argument(...),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Suppress a tag regardless of lower-precedence sources."""
    _annotate(selector, "untag", tag, root)


@sessions_app.command("set-project")
def sessions_set_project(
    selector: str = typer.Argument(...),
    project: Optional[str] = typer.Argument(None),
    clear: bool = typer.Option(False, "--clear"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Set or clear a canonical per-session project annotation."""
    if clear:
        if project is not None:
            raise typer.BadParameter("omit PROJECT when using --clear")
        _annotate(selector, "clear-project", None, root)
        return
    if not project:
        raise typer.BadParameter("PROJECT is required unless --clear is used")
    _annotate(selector, "set-project", project, root)


@rules_app.command("list")
def rules_list(
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """List built-in and configured catalog rules."""
    rules = catalog_rules(load_config())
    if json_output:
        _emit_json([rule.to_dict() for rule in rules])
        return
    table = Table(title="Catalog rules")
    table.add_column("id")
    table.add_column("enabled")
    table.add_column("tags")
    table.add_column("description", overflow="fold")
    for rule in rules:
        table.add_row(
            rule.id,
            "yes" if rule.enabled else "no",
            ", ".join(rule.tags),
            rule.description,
        )
    console.print(table)


@rules_app.command("add")
def rules_add(
    spec: str = typer.Argument(..., help="JSON object, @file, or JSON file path"),
) -> None:
    """Add one validated user rule."""
    rule = _validated_rule(_load_json_argument(spec))
    if rule["id"].startswith("builtin."):
        raise typer.BadParameter("custom rule ids must not use the builtin. prefix")
    with exclusive_lock(user_state_dir() / "archive.lock"):
        config = load_config()
        if any(item["id"] == rule["id"] for item in config.rules):
            raise typer.BadParameter(f"rule {rule['id']!r} already exists")
        update_config(rules=[*config.rules, rule])
    console.print(f"[green]added rule[/green] {rule['id']}")


@rules_app.command("remove")
def rules_remove(rule_id: str = typer.Argument(...)) -> None:
    """Remove one configured user rule."""
    if rule_id.startswith("builtin."):
        raise typer.BadParameter("built-in rules can be disabled but not removed")
    with exclusive_lock(user_state_dir() / "archive.lock"):
        config = load_config()
        retained = [rule for rule in config.rules if rule["id"] != rule_id]
        if len(retained) == len(config.rules):
            raise typer.BadParameter(f"unknown configured rule {rule_id!r}")
        update_config(
            rules=retained,
            disabled_rules=[
                item for item in config.disabled_rules if item != rule_id
            ],
        )
    console.print(f"[green]removed rule[/green] {rule_id}")


@rules_app.command("enable")
def rules_enable(rule_id: str = typer.Argument(...)) -> None:
    """Enable a built-in or configured rule."""
    _set_rule_enabled(rule_id, True)


@rules_app.command("disable")
def rules_disable(rule_id: str = typer.Argument(...)) -> None:
    """Disable a built-in or configured rule."""
    _set_rule_enabled(rule_id, False)


@rules_app.command("test")
def rules_test(
    spec: Optional[str] = typer.Argument(
        None,
        help="Optional JSON rule object or file; omit to test all configured rules.",
    ),
    samples: int = typer.Option(5, "--samples", min=0),
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Dry-run rules against the existing catalog without changing any files."""
    layout = default_layout(root)
    catalog = load_catalog(layout)
    if spec is not None:
        validated = _validated_rule(_load_json_argument(spec))
        rules = [
            CatalogRule(
                id=validated["id"],
                description=validated["description"],
                enabled=validated["enabled"],
                match=validated["match"],
                tags=validated["tags"],
            )
        ]
    else:
        rules = catalog_rules(load_config())
    results: list[dict[str, Any]] = []
    for rule in rules:
        matching = [record.key for record in catalog.values() if rule_matches(record, rule)]
        results.append(
            {
                "id": rule.id,
                "enabled": rule.enabled,
                "tags": rule.tags,
                "matches": len(matching),
                "sample_sessions": matching[:samples],
            }
        )
    if json_output:
        _emit_json(results)
        return
    table = Table(title=f"Rule dry run ({len(catalog)} sessions, no writes)")
    table.add_column("id")
    table.add_column("enabled")
    table.add_column("matches", justify="right")
    table.add_column("sample sessions", overflow="fold")
    for result in results:
        table.add_row(
            result["id"],
            "yes" if result["enabled"] else "no",
            str(result["matches"]),
            ", ".join(result["sample_sessions"]) or "-",
        )
    console.print(table)


@projects_app.command("list")
def projects_list(
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """List consolidated projects and per-agent session counts."""
    _, catalog = _refresh(root)
    grouped: dict[str, list[SessionRecord]] = defaultdict(list)
    for record in catalog.values():
        grouped[str(record.project.get("id") or "unknown")].append(record)
    rows: list[dict[str, Any]] = [
        {
            "id": project_id,
            "name": records[0].project.get("name"),
            "sessions": len(records),
            "agents": dict(sorted(Counter(record.agent for record in records).items())),
            "excluded": sum(record.excluded for record in records),
        }
        for project_id, records in grouped.items()
    ]
    rows.sort(key=lambda item: (-item["sessions"], item["id"]))
    if json_output:
        _emit_json(rows)
        return
    table = Table(title=f"Projects ({len(rows)})")
    table.add_column("project")
    table.add_column("sessions", justify="right")
    table.add_column("agents")
    table.add_column("excluded", justify="right")
    for row in rows:
        table.add_row(
            f"{row['name']} ({row['id']})",
            str(row["sessions"]),
            ", ".join(f"{name}:{count}" for name, count in row["agents"].items()),
            str(row["excluded"]),
        )
    console.print(table)


@projects_app.command("show")
def projects_show(
    project_id: str = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json"),
    root: Optional[Path] = typer.Option(None, "--root"),
) -> None:
    """Show one consolidated project and its sessions."""
    _, catalog = _refresh(root)
    records = [
        record
        for record in catalog.values()
        if _same(str(record.project.get("id") or ""), project_id)
        or _same(str(record.project.get("name") or ""), project_id)
    ]
    if not records:
        raise typer.BadParameter(f"unknown project {project_id!r}")
    payload: dict[str, Any] = {
        "project": records[0].project,
        "sessions": len(records),
        "agents": dict(sorted(Counter(record.agent for record in records).items())),
        "keys": sorted(record.key for record in records),
    }
    if json_output:
        _emit_json(payload)
    else:
        console.print(f"[bold]{payload['project']['name']}[/bold] {payload['project']['id']}")
        console.print(f"sessions: {payload['sessions']}")
        console.print(
            "agents: "
            + ", ".join(f"{name}:{count}" for name, count in payload["agents"].items())
        )
        for key in payload["keys"]:
            console.print(f"  {key}")


@projects_app.command("merge")
def projects_merge(
    source: str = typer.Argument(...),
    target: str = typer.Argument(...),
) -> None:
    """Persist a project-id alias from SOURCE into TARGET."""
    if source == target:
        raise typer.BadParameter("SOURCE and TARGET must differ")
    with exclusive_lock(user_state_dir() / "archive.lock"):
        config = load_config()
        projects = dict(config.projects)
        aliases = dict(projects.get("aliases", {}))
        aliases[source] = target
        projects["aliases"] = aliases
        update_config(projects=projects)
    console.print(f"[green]merged[/green] {source} -> {target}")


@projects_app.command("rename")
def projects_rename(
    project_id: str = typer.Argument(...),
    name: str = typer.Argument(...),
) -> None:
    """Persist a display name for a canonical project id."""
    if not name.strip():
        raise typer.BadParameter("NAME must be non-empty")
    with exclusive_lock(user_state_dir() / "archive.lock"):
        config = load_config()
        projects = dict(config.projects)
        names = dict(projects.get("names", {}))
        names[project_id] = name.strip()
        projects["names"] = names
        update_config(projects=projects)
    console.print(f"[green]renamed[/green] {project_id} -> {name.strip()}")


def _refresh(root: Optional[Path]) -> tuple[Layout, Catalog]:
    layout = default_layout(root)
    with exclusive_lock(user_state_dir() / "archive.lock"):
        update_catalog(layout)
    return layout, load_catalog(layout)


def _annotate(
    selector: str,
    op: str,
    value: str | None,
    root: Optional[Path],
) -> None:
    layout = default_layout(root)
    key = _selector_key(selector, None)
    with exclusive_lock(user_state_dir() / "archive.lock"):
        update_catalog(layout)
        catalog = load_catalog(layout)
        _require_record(catalog, key)
        append_annotation(layout.root, key=key, op=op, value=value)
        update_catalog(layout, keys=[key])
    console.print(f"[green]{op}[/green] {key}" + (f" {value}" if value else ""))


def _set_rule_enabled(rule_id: str, enabled: bool) -> None:
    with exclusive_lock(user_state_dir() / "archive.lock"):
        config = load_config()
        known = {rule.id for rule in catalog_rules(config)}
        if rule_id not in known:
            raise typer.BadParameter(f"unknown rule {rule_id!r}")
        disabled = set(config.disabled_rules)
        if enabled:
            disabled.discard(rule_id)
        else:
            disabled.add(rule_id)
        rules = []
        for rule in config.rules:
            updated = dict(rule)
            if updated["id"] == rule_id:
                updated["enabled"] = enabled
            rules.append(updated)
        update_config(disabled_rules=sorted(disabled), rules=rules)
    console.print(f"[green]{'enabled' if enabled else 'disabled'}[/green] {rule_id}")


def _selector_key(selector: str, session_id: str | None) -> str:
    key = f"{selector}/{session_id}" if session_id else selector
    host, separator, identifier = key.partition("/")
    if not separator or not host or not identifier:
        raise typer.BadParameter("session selector must be host/session-id")
    return f"{host}/{identifier}"


def _require_record(catalog: Catalog, key: str) -> SessionRecord:
    record = catalog.get(key)
    if record is None:
        raise typer.BadParameter(f"unknown catalog session {key!r}")
    return record


def _render_record(record: SessionRecord) -> None:
    table = Table(title=record.key)
    table.add_column("field")
    table.add_column("value", overflow="fold")
    for key, value in record.to_dict().items():
        if isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            rendered = str(value)
        table.add_row(key, rendered)
    console.print(table)


def _load_json_argument(value: str) -> dict[str, Any]:
    candidate = value[1:] if value.startswith("@") else value
    path = Path(candidate).expanduser()
    try:
        is_file = path.is_file()
    except OSError:
        is_file = False
    if value.startswith("@") or is_file:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"cannot read rule spec {path}: {exc}") from exc
    else:
        raw = value
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"rule spec is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise typer.BadParameter("rule spec must be a JSON object")
    return parsed


def _validated_rule(value: dict[str, Any]) -> dict[str, Any]:
    try:
        return validate_rule_spec(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_datetime(value: str, label: str) -> datetime:
    parsed = _record_datetime(value)
    if parsed is None:
        raise typer.BadParameter(f"{label} must be an ISO-8601 date or timestamp")
    return parsed


def _record_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _same(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def _emit_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
