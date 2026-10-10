"""Model pricing: the single source of truth for turning tokens into dollars.

Rates come from the bundled LiteLLM snapshot (`retro/pricing/litellm-pricing.json`,
USD per token). Lookups normalize vendor-specific model spellings
(`copilot/claude-sonnet-4.6` -> `claude-sonnet-4-6`) and fall back to the model
family when an exact entry is missing, always reporting which happened so
callers can label an estimate as an estimate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRICING_SNAPSHOT = Path(__file__).resolve().parent.parent / "pricing" / "litellm-pricing.json"

RateBasis = str  # "exact" | "family" | "unknown"

_SUFFIX_RE = re.compile(r"-(?:20\d{6}|\d{8}|latest|preview|beta|stable|v\d+)$")
_SEPARATOR_RE = re.compile(r"[.\s_]+")


def normalize_model_id(model: str | None) -> str:
    """Canonicalize a provider model string for rate lookup."""
    if not model:
        return ""
    name = model.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    name = _SEPARATOR_RE.sub("-", name)
    name = _SUFFIX_RE.sub("", name)
    return name.strip("-")


@dataclass(frozen=True)
class Rates:
    """USD per million tokens, plus how confident the lookup was."""

    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_create: float = 0.0
    basis: RateBasis = "unknown"
    matched: str = ""

    @property
    def priced(self) -> bool:
        return bool(self.input or self.output or self.cache_read or self.cache_create)


UNKNOWN_RATES = Rates()


@dataclass(frozen=True)
class CostBreakdown:
    total_usd: float
    by_model: Mapping[str, float]
    by_category: Mapping[str, float]
    basis: str  # "exact" | "family" | "mixed" | "unpriced" | "none"
    unpriced_models: tuple[str, ...]


class PricingMap:
    """Resolve per-model rates from the bundled snapshot."""

    def __init__(self, snapshot: Mapping[str, Any]):
        self.meta = dict(snapshot.get("_meta") or {}) if snapshot else {}
        self._entries = {
            normalize_model_id(key): value
            for key, value in (snapshot or {}).items()
            if key != "_meta" and isinstance(value, dict)
        }
        self._by_length = sorted(self._entries, key=len, reverse=True)
        self._cache: dict[str, Rates] = {}

    @classmethod
    def load(cls, snapshot_path: Path | None = None) -> PricingMap:
        path = snapshot_path or PRICING_SNAPSHOT
        snapshot: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                loaded = {}
            if isinstance(loaded, dict):
                snapshot = loaded
        return cls(snapshot)

    def rates_for(self, model: str | None) -> Rates:
        name = normalize_model_id(model)
        if not name:
            return UNKNOWN_RATES
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        entry = self._entries.get(name)
        basis: RateBasis = "exact"
        matched = name
        if entry is None:
            basis = "family"
            matched = ""
            for known in self._by_length:
                if name.startswith(known + "-") or known.startswith(name + "-"):
                    entry = self._entries[known]
                    matched = known
                    break
        if entry is not None and entry.get("rate_basis") == "family-estimate":
            basis = "family"
        rates = _rates_from_entry(entry, basis, matched) if entry else UNKNOWN_RATES
        self._cache[name] = rates
        return rates

    def source_note(self) -> str:
        taken = self.meta.get("snapshot_taken")
        if self._entries and taken:
            return f"LiteLLM snapshot taken {taken}; family fallback for unlisted models"
        return "no pricing snapshot available"

    def price(
        self,
        by_model: Mapping[str, Mapping[str, int]],
        *,
        embedded_usd: float | None = None,
    ) -> CostBreakdown:
        """Price normalized usage buckets.

        `by_model` values must already be normalized by `retro.signals.usage`:
        `input` is fresh non-cached input, `cache_read` and `cache_create` are
        disjoint from it. That keeps provider-specific accounting out of here.
        """
        if embedded_usd is not None:
            return CostBreakdown(round(float(embedded_usd), 6), {}, {}, "embedded", ())
        if not by_model:
            return CostBreakdown(0.0, {}, {}, "none", ())

        categories = {"input": 0.0, "cache_read": 0.0, "cache_create": 0.0, "output": 0.0}
        per_model: dict[str, float] = {}
        unpriced: list[str] = []
        bases: set[str] = set()
        for model, bucket in by_model.items():
            rates = self.rates_for(model)
            if not rates.priced:
                if any(int(bucket.get(k, 0) or 0) for k in categories):
                    unpriced.append(model)
                per_model[model] = 0.0
                continue
            bases.add(rates.basis)
            model_total = 0.0
            for key in categories:
                amount = int(bucket.get(key, 0) or 0) * getattr(rates, key) / 1_000_000
                categories[key] += amount
                model_total += amount
            per_model[model] = round(model_total, 6)

        total = round(sum(per_model.values()), 6)
        if not bases:
            basis = "unpriced"
        elif unpriced or len(bases) > 1:
            basis = "mixed"
        else:
            basis = bases.pop()
        return CostBreakdown(
            total_usd=total,
            by_model=per_model,
            by_category={k: round(v, 6) for k, v in categories.items()},
            basis=basis,
            unpriced_models=tuple(sorted(set(unpriced))),
        )


def _rates_from_entry(entry: Mapping[str, Any], basis: RateBasis, matched: str) -> Rates:
    def per_million(key: str, fallback: str | None = None) -> float:
        value = entry.get(key)
        if value is None and fallback is not None:
            value = entry.get(fallback)
        try:
            return float(value) * 1_000_000 if value is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    return Rates(
        input=per_million("input_cost_per_token"),
        output=per_million("output_cost_per_token"),
        cache_read=per_million("cache_read_input_token_cost", "input_cost_per_token"),
        cache_create=per_million("cache_creation_input_token_cost", "input_cost_per_token"),
        basis=basis,
        matched=matched,
    )


_DEFAULT_MAP: PricingMap | None = None


def default_pricing() -> PricingMap:
    global _DEFAULT_MAP
    if _DEFAULT_MAP is None:
        _DEFAULT_MAP = PricingMap.load()
    return _DEFAULT_MAP
