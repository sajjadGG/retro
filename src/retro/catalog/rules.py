"""Catalog rule matching and built-in quality filters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import RetroConfig, validate_rule_spec
from .models import SessionRecord
from .projects import path_glob_matches


@dataclass(frozen=True)
class CatalogRule:
    id: str
    description: str
    enabled: bool
    match: dict[str, Any]
    tags: list[str]
    builtin: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "enabled": self.enabled,
            "match": dict(self.match),
            "tags": list(self.tags),
            "builtin": self.builtin,
        }


_BUILTIN_RULES = (
    CatalogRule(
        id="builtin.temp-dir",
        description="Scratch sessions launched from an operating-system temporary directory.",
        enabled=True,
        match={},
        tags=["scratch", "ignore"],
        builtin=True,
    ),
    CatalogRule(
        id="builtin.empty",
        description="Sessions with no normalized user-authored messages.",
        enabled=True,
        match={},
        tags=["empty", "ignore"],
        builtin=True,
    ),
    CatalogRule(
        id="builtin.automated",
        description="Sessions classified as non-interactive.",
        enabled=True,
        match={},
        tags=["automated"],
        builtin=True,
    ),
    CatalogRule(
        id="builtin.subagent",
        description="Sessions spawned as child agents.",
        enabled=True,
        match={},
        tags=["subagent"],
        builtin=True,
    ),
)


def catalog_rules(config: RetroConfig, *, include_disabled: bool = True) -> list[CatalogRule]:
    disabled = set(config.disabled_rules)
    rules: list[CatalogRule] = []
    for rule in _BUILTIN_RULES:
        enabled = rule.id not in disabled
        if include_disabled or enabled:
            rules.append(
                CatalogRule(
                    id=rule.id,
                    description=rule.description,
                    enabled=enabled,
                    match=rule.match,
                    tags=rule.tags,
                    builtin=True,
                )
            )
    for index, raw in enumerate(config.rules):
        spec = validate_rule_spec(raw, where=f"rules[{index}]")
        enabled = bool(spec["enabled"]) and spec["id"] not in disabled
        if include_disabled or enabled:
            rules.append(
                CatalogRule(
                    id=spec["id"],
                    description=spec["description"],
                    enabled=enabled,
                    match=spec["match"],
                    tags=spec["tags"],
                )
            )
    return rules


def evaluate_rules(record: SessionRecord, config: RetroConfig) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    for rule in catalog_rules(config, include_disabled=False):
        if not rule_matches(record, rule):
            continue
        for tag in rule.tags:
            sources.setdefault(tag, []).append(f"rule:{rule.id}")
    return sources


def rule_matches(record: SessionRecord, rule: CatalogRule | dict[str, Any]) -> bool:
    if isinstance(rule, CatalogRule):
        if rule.builtin:
            return _builtin_matches(record, rule.id)
        match = rule.match
    else:
        match = validate_rule_spec(rule)["match"]
    scalar_lists = {
        "host": record.host,
        "agent": record.agent,
        "client": record.client,
        "project": record.project.get("id"),
        "remote": record.git.get("remote"),
        "branch": record.git.get("branch"),
    }
    for field, actual in scalar_lists.items():
        expected = match.get(field)
        if expected and not _one_of(actual, expected):
            return False
    expected_models = match.get("model")
    if expected_models and not any(
        _one_of(model, expected_models) for model in record.models
    ):
        return False
    for field in ("interactive", "is_subagent"):
        if field in match and getattr(record, field) is not match[field]:
            return False
    cwd_patterns = match.get("cwd")
    if cwd_patterns:
        values = [value for value in (record.cwd, record.cwd_resolved) if value]
        if not any(
            path_glob_matches(pattern, value)
            for pattern in cwd_patterns
            for value in values
        ):
            return False
    regex_fields = {
        "title": record.title or "",
        "prompt": record.first_prompt or "",
        "session_id": record.session_id,
    }
    for field, actual in regex_fields.items():
        expression = match.get(field)
        if expression is not None and re.search(expression, actual) is None:
            return False
    user_messages = record.counts.get("user_messages", 0)
    minimum = match.get("min_user_messages")
    maximum = match.get("max_user_messages")
    if minimum is not None and user_messages < minimum:
        return False
    if maximum is not None and user_messages > maximum:
        return False
    if "started_after" in match or "started_before" in match:
        started = _parse_datetime(record.started_at)
        if started is None:
            return False
        after = _parse_datetime(match.get("started_after"))
        before = _parse_datetime(match.get("started_before"))
        if after is not None and started < after:
            return False
        if before is not None and started >= before:
            return False
    return True


def _builtin_matches(record: SessionRecord, rule_id: str) -> bool:
    if rule_id == "builtin.empty":
        return record.counts.get("user_messages", 0) == 0
    if rule_id == "builtin.automated":
        return record.interactive is False
    if rule_id == "builtin.subagent":
        return record.is_subagent
    if rule_id == "builtin.temp-dir":
        return any(is_temp_path(value) for value in (record.cwd, record.cwd_resolved) if value)
    return False


def is_temp_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return bool(
        re.match(r"^/(?:private/)?var/folders/.+/T/tmp[^/]*(?:/|$)", normalized)
        or re.match(r"^/(?:private/)?tmp(?:/|$)", normalized)
        or re.search(r"/AppData/Local/Temp(?:/|$)", normalized, re.IGNORECASE)
    )


def _one_of(actual: Any, expected: list[str]) -> bool:
    if actual is None:
        return False
    folded = str(actual).casefold()
    return any(folded == item.casefold() for item in expected)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
