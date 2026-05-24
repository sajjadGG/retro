"""risk_aware: safety pass over any miner's output.

Inspired by "Learning When to Remember" (https://arxiv.org/abs/2604.27283).
Treats memory injection as a risk-sensitive decision: not every mined
candidate should land in the next prompt. This filter:

  1. Drops candidates flagged `risk=high`.
  2. Drops candidates whose confidence is below a floor.
  3. Drops candidates whose text overlaps near-duplicately with another.
  4. Sorts by priority + confidence, then caps the result to keep prompt
     blocks small.

It does not invent new candidates — only re-ranks and prunes. Apply via
`--filter risk_aware` on the CLI.
"""
from __future__ import annotations

import re

from ..base import MemoryCandidate, MiningResult, register_filter

_DEFAULT_MIN_CONFIDENCE = 0.40
_DEFAULT_KEEP = 8
_OVERLAP_THRESHOLD = 0.75  # Jaccard on word sets


@register_filter(
    "risk_aware",
    description=(
        "Drop high-risk and low-confidence candidates, dedupe near-duplicates, "
        "and cap the result to a prompt-friendly size."
    ),
)
def filter_risk_aware(result: MiningResult) -> MiningResult:
    kept: list[MemoryCandidate] = []
    notes: list[str] = list(result.notes)

    dropped_high_risk = 0
    dropped_low_conf = 0
    dropped_duplicate = 0

    # Stable ordering: priority desc, confidence desc, kind asc, id asc.
    ranked = sorted(
        result.candidates,
        key=lambda c: (-c.priority, -c.confidence, c.kind, c.id),
    )

    for c in ranked:
        if c.risk == "high":
            dropped_high_risk += 1
            continue
        if c.confidence < _DEFAULT_MIN_CONFIDENCE:
            dropped_low_conf += 1
            continue
        if any(_near_duplicate(c, prior) for prior in kept):
            dropped_duplicate += 1
            continue
        kept.append(c)
        if len(kept) >= _DEFAULT_KEEP:
            break

    notes.append(
        f"risk_aware: kept {len(kept)} of {len(result.candidates)} "
        f"(dropped high_risk={dropped_high_risk}, "
        f"low_confidence={dropped_low_conf}, "
        f"duplicate={dropped_duplicate})."
    )
    return MiningResult(
        session_id=result.session_id,
        host=result.host,
        method=result.method,
        task_summary=result.task_summary,
        candidates=kept,
        notes=notes,
        filters_applied=[*result.filters_applied, "risk_aware"],
    )


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _word_set(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text)}


def _near_duplicate(a: MemoryCandidate, b: MemoryCandidate) -> bool:
    """Jaccard similarity on the combined text + when_to_use of each candidate."""
    if a.kind != b.kind:
        return False
    a_words = _word_set(a.text + " " + a.when_to_use)
    b_words = _word_set(b.text + " " + b.when_to_use)
    if not a_words or not b_words:
        return False
    inter = len(a_words & b_words)
    union = len(a_words | b_words)
    return (inter / union) >= _OVERLAP_THRESHOLD
