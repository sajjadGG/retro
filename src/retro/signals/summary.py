"""Aggregation: per-signal statistics and guarded group comparisons.

The per-session store is the raw material; everything a human reads is an
aggregate. Ratios are aggregated as rate-of-sums where the components exist
(not a mean of per-session ratios), because a mean of ratios over-weights tiny
sessions.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Callable

NUMERIC_KINDS = frozenset({"number", "count", "ratio", "duration", "money"})
MIN_GROUP_SESSIONS = 5


def summarize(
    values: Iterable[Mapping[str, Any]],
    definitions: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """Per-signal statistics across sessions.

    `values` is an iterable of per-session value dicts (`store.all_values()`).
    `definitions` may be the store's definition dicts or registered `SignalDef`s;
    when omitted, kinds are inferred from the data.
    """
    meta = _definition_meta(definitions)
    numeric: dict[str, list[float]] = {}
    booleans: dict[str, list[bool]] = {}
    categories: dict[str, Counter[str]] = {}
    missing: dict[str, int] = {}
    sessions = 0

    for row in values:
        sessions += 1
        for signal_id, value in row.items():
            if value is None:
                missing[signal_id] = missing.get(signal_id, 0) + 1
                continue
            kind = meta.get(signal_id, {}).get("kind") or _infer_kind(value)
            if kind == "boolean" or isinstance(value, bool):
                booleans.setdefault(signal_id, []).append(bool(value))
            elif kind == "category" or isinstance(value, str):
                categories.setdefault(signal_id, Counter())[str(value)] += 1
            elif isinstance(value, (int, float)) and not math.isnan(float(value)):
                numeric.setdefault(signal_id, []).append(float(value))

    signals: dict[str, dict[str, Any]] = {}
    for signal_id, data in numeric.items():
        signals[signal_id] = _numeric_block(data)
    for signal_id, flags in booleans.items():
        true_count = sum(1 for flag in flags if flag)
        signals[signal_id] = {
            "n": len(flags),
            "true": true_count,
            "false": len(flags) - true_count,
            "true_rate": round(true_count / len(flags), 4) if flags else None,
        }
    for signal_id, counter in categories.items():
        signals[signal_id] = {
            "n": sum(counter.values()),
            "counts": dict(counter.most_common()),
            "top": counter.most_common(1)[0][0] if counter else None,
        }

    for signal_id, block in signals.items():
        info = meta.get(signal_id)
        if info:
            for field in ("kind", "unit", "category", "polarity", "title"):
                if info.get(field) is not None:
                    block[field] = info[field]
        if missing.get(signal_id):
            block["null"] = missing[signal_id]
        block["coverage"] = round(block["n"] / sessions, 4) if sessions else 0.0

    return {"sessions": sessions, "signals": signals}


def _numeric_block(data: list[float]) -> dict[str, Any]:
    data.sort()
    return {
        "n": len(data),
        "sum": round(sum(data), 6),
        "mean": round(sum(data) / len(data), 6),
        "median": round(_percentile(data, 0.5), 6),
        "p90": round(_percentile(data, 0.9), 6),
        "min": round(data[0], 6),
        "max": round(data[-1], 6),
    }


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = (len(sorted_values) - 1) * fraction
    low = int(math.floor(position))
    high = min(low + 1, len(sorted_values) - 1)
    if low == high:
        return float(sorted_values[low])
    return float(sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low))


def _infer_kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "category"
    return "number"


def _definition_meta(definitions: Sequence[Any] | None) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    for item in definitions or ():
        if isinstance(item, Mapping):
            signal_id = item.get("id")
            info = dict(item)
        else:
            signal_id = getattr(item, "id", None)
            info = {
                "kind": getattr(item, "kind", None),
                "unit": getattr(item, "unit", None),
                "category": getattr(item, "category", None),
                "polarity": getattr(item, "polarity", None),
                "title": getattr(item, "title", None),
            }
        if isinstance(signal_id, str):
            meta[signal_id] = info
    return meta


def group_by(
    records: Iterable[Mapping[str, Any]],
    key: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, list[Mapping[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        label = key(record)
        if label is None:
            continue
        groups.setdefault(label, []).append(record)
    return groups


def compare(
    groups: Mapping[str, Iterable[Mapping[str, Any]]],
    definitions: Sequence[Any] | None = None,
    *,
    min_sessions: int = MIN_GROUP_SESSIONS,
) -> dict[str, Any]:
    """Summarize several groups side by side, flagging under-powered ones.

    Groups below `min_sessions` are summarized but marked `sufficient: false`;
    they must never be rendered as a winner.
    """
    out: dict[str, Any] = {}
    for label, values in groups.items():
        rows = [dict(row) for row in values]
        summary = summarize(rows, definitions)
        summary["sufficient"] = summary["sessions"] >= min_sessions
        out[label] = summary
    return {"min_sessions": min_sessions, "groups": out}


def rate(
    values: Iterable[Mapping[str, Any]],
    numerator: str,
    denominator: str,
) -> float | None:
    """Rate of sums (not mean of ratios) for two count signals."""
    top = 0.0
    bottom = 0.0
    for row in values:
        numerator_value = row.get(numerator)
        denominator_value = row.get(denominator)
        if isinstance(numerator_value, (int, float)) and not isinstance(numerator_value, bool):
            top += float(numerator_value)
        if isinstance(denominator_value, (int, float)) and not isinstance(denominator_value, bool):
            bottom += float(denominator_value)
    if bottom <= 0:
        return None
    return round(top / bottom, 6)


def category_share(
    values: Iterable[Mapping[str, Any]], signal_id: str, wanted: Sequence[str]
) -> float | None:
    """Share of sessions whose category signal is in `wanted` (e.g. success rate)."""
    total = 0
    hits = 0
    for row in values:
        value = row.get(signal_id)
        if not isinstance(value, str):
            continue
        total += 1
        if value in wanted:
            hits += 1
    if not total:
        return None
    return round(hits / total, 4)


def weekly_trend(rows: Iterable[tuple[str, Mapping[str, Any]]], signal_id: str) -> dict[str, dict[str, Any]]:
    """Bucket per-session values by ISO week of `started_at` for trend lines."""
    buckets: dict[str, list[float]] = {}
    for started_at, row in rows:
        week = _iso_week(started_at)
        value = row.get(signal_id)
        if week is None or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        buckets.setdefault(week, []).append(float(value))
    return {week: _numeric_block(sorted(data)) for week, data in sorted(buckets.items())}


def _iso_week(started_at: str | None) -> str | None:
    if not started_at or len(started_at) < 10:
        return None
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"
