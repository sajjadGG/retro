"""Signal registry: definitions carry their own metadata and version.

The store is keyed on a digest of the registered set *and of the inputs that set
depends on*, so changing a signal's algorithm means bumping its `version` and
every stored session is recomputed automatically. Only semantic fields feed the
digest — editing a description must not trigger a full archive recompute — but
the bundled pricing snapshot does, because every money signal is a function of
it and stale costs would otherwise survive a rate update forever.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from .pricing import snapshot_digest
from .profile import SessionProfile

Category = str  # usage|activity|outcome|friction|efficiency|collaboration|risk|quality|work
Kind = str  # number|count|ratio|duration|money|boolean|category
Polarity = str  # higher_better|lower_better|neutral
Method = str  # exact|heuristic|regex|llm

CATEGORIES: tuple[str, ...] = (
    "usage",
    "activity",
    "outcome",
    "friction",
    "efficiency",
    "collaboration",
    "risk",
    "quality",
    "work",
)
KINDS: tuple[str, ...] = ("number", "count", "ratio", "duration", "money", "boolean", "category")
POLARITIES: tuple[str, ...] = ("higher_better", "lower_better", "neutral")
METHODS: tuple[str, ...] = ("exact", "heuristic", "regex", "llm")


@dataclass(frozen=True)
class SignalValue:
    """A value plus the evidence and confidence that back it."""

    value: Any
    evidence: tuple[str, ...] = ()
    confidence: float = 1.0


@dataclass(frozen=True)
class SignalDef:
    id: str
    title: str
    category: Category
    kind: Kind
    unit: str | None
    polarity: Polarity
    method: Method
    version: int
    description: str
    applicability: str
    categories: tuple[str, ...] | None
    fn: Callable[[SessionProfile], Any] = field(compare=False, repr=False, default=lambda _p: None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "kind": self.kind,
            "unit": self.unit,
            "polarity": self.polarity,
            "method": self.method,
            "version": self.version,
            "description": self.description,
            "applicability": self.applicability,
            "categories": list(self.categories) if self.categories else None,
        }

    def semantic_key(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "kind": self.kind,
            "unit": self.unit,
            "polarity": self.polarity,
            "method": self.method,
            "version": self.version,
            "categories": list(self.categories) if self.categories else None,
        }


REGISTRY: dict[str, SignalDef] = {}


def register(
    signal_id: str,
    *,
    title: str,
    category: Category,
    kind: Kind,
    polarity: Polarity = "neutral",
    method: Method = "heuristic",
    unit: str | None = None,
    version: int = 1,
    description: str = "",
    applicability: str = "every session",
    categories: Sequence[str] | None = None,
) -> Callable[[Callable[[SessionProfile], Any]], SignalDef]:
    """Register a signal computed from a `SessionProfile`."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if polarity not in POLARITIES:
        raise ValueError(f"unknown polarity {polarity!r}")
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}")

    def wrap(fn: Callable[[SessionProfile], Any]) -> SignalDef:
        if signal_id in REGISTRY:
            raise ValueError(f"signal {signal_id!r} already registered")
        definition = SignalDef(
            id=signal_id,
            title=title,
            category=category,
            kind=kind,
            unit=unit,
            polarity=polarity,
            method=method,
            version=version,
            description=" ".join(description.split()),
            applicability=" ".join(applicability.split()),
            categories=tuple(categories) if categories else None,
            fn=fn,
        )
        REGISTRY[signal_id] = definition
        return definition

    return wrap


def definitions() -> list[SignalDef]:
    return [REGISTRY[key] for key in sorted(REGISTRY)]


def definitions_payload() -> list[dict[str, Any]]:
    return [definition.to_dict() for definition in definitions()]


def definitions_version() -> str:
    """Digest of the registered signal set and of the inputs it depends on."""
    blob = json.dumps(
        {
            "signals": [definition.semantic_key() for definition in definitions()],
            "pricing": snapshot_digest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def evaluate(
    profile: SessionProfile, selected: Iterable[str] | None = None
) -> tuple[dict[str, Any], dict[str, list[str]], dict[str, float]]:
    """Run every registered signal over one profile."""
    chosen = definitions() if selected is None else [REGISTRY[key] for key in sorted(set(selected))]
    values: dict[str, Any] = {}
    evidence: dict[str, list[str]] = {}
    confidence: dict[str, float] = {}
    for definition in chosen:
        try:
            result = definition.fn(profile)
        except Exception as exc:  # a broken signal must not lose the session
            values[definition.id] = None
            confidence[definition.id] = 0.0
            evidence[definition.id] = [f"error:{type(exc).__name__}"]
            continue
        if result is None:
            continue
        if isinstance(result, SignalValue):
            if result.value is None:
                continue
            values[definition.id] = result.value
            if result.evidence:
                evidence[definition.id] = list(result.evidence)[:5]
            if result.confidence != 1.0:
                confidence[definition.id] = round(result.confidence, 3)
        else:
            values[definition.id] = result
    return values, evidence, confidence


def lookup(signal_ids: Iterable[str]) -> list[SignalDef]:
    missing = sorted(set(signal_ids) - set(REGISTRY))
    if missing:
        raise KeyError(f"unknown signal(s): {missing}")
    return [REGISTRY[key] for key in sorted(set(signal_ids))]


def definitions_by_id(payload: Sequence[Mapping[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    source = payload if payload is not None else definitions_payload()
    return {str(item["id"]): dict(item) for item in source if "id" in item}
