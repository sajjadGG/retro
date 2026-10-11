"""Outcome labelling and deterministic stratified source sampling.

Selection used to emit every eligible source in lexicographic ``source_id``
order, which makes a taskset a biased sample of whichever repository happened to
produce the most capture-complete sessions. This module adds two things the rest
of the pipeline consumes:

* ``outcome_labels`` — what actually happened to a source, derived only from
  evidence Retro already holds (commit survival in branch history, user pushback
  proxies from the signal store and the rollout itself, and the heuristic
  ``outcome.status`` signal). Every label carries evidence references.
* a deterministic, seeded stratified sampler over (repo, work kind, size bucket)
  with a per-repo cap, so a taskset spreads across projects and task shapes
  instead of over-representing one repository.

Both are pure functions of their inputs: the same archive, seed and limits always
produce the same ordering.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...schema import NormalizedEvent
from ...utils import event_text
from . import git_state

OUTCOME_LABELS_SCHEMA = "retro-source-outcome-labels-v1"
SAMPLING_SCHEMA = "retro-taskset-sampling-v1"
SAMPLING_STRATEGIES: tuple[str, ...] = ("stratified", "lexicographic")
OUTCOME_LABEL_KINDS: tuple[str, ...] = (
    "successful_surviving",
    "corrected",
    "failed_then_fixed",
    "unlabeled",
)

DEFAULT_REPO_CAP_SHARE = 0.25
SMALL_MAX_FILES = 2
SMALL_MAX_LINES = 50
MEDIUM_MAX_FILES = 10
MEDIUM_MAX_LINES = 400

# Informative first: a source where the user pushed back, or where the agent
# failed and the work was still finished, teaches more than an uneventful run.
# Clean surviving successes are kept at the next tier so a taskset keeps both
# ends of the outcome distribution; unlabeled sources fill the remainder.
LABEL_PRIORITY: dict[str, int] = {
    "corrected": 0,
    "failed_then_fixed": 0,
    "successful_surviving": 1,
    "unlabeled": 2,
}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


ACCEPTANCE_RE = re.compile(
    r"^\s*(?:lgtm|looks good(?: to me)?|approved|ship it|merge it|"
    r"that works|accepted|i accept(?: this)?)(?:[\s,.!]|$)",
    re.IGNORECASE,
)
ACCEPTANCE_RETRACTION_RE = re.compile(
    r"\b(?:but|however|except|not|don't|do not)\b", re.IGNORECASE
)


def is_acceptance(text: str) -> bool:
    """True for an unqualified approval message (not a correction)."""
    stripped = text.strip().lower()
    return bool(ACCEPTANCE_RE.search(stripped)) and not bool(
        ACCEPTANCE_RETRACTION_RE.search(stripped)
    )


def user_accepted(events: Sequence[NormalizedEvent]) -> bool:
    """True when the rollout's final user message is an explicit acceptance."""
    for event in reversed(list(events)):
        if event.event_type != "message" or event.actor != "user":
            continue
        return is_acceptance(event_text(event))
    return False


def size_bucket(files_changed: int, lines_changed: int) -> str:
    """Bucket a diff by breadth and volume; used as a stratification axis."""
    if files_changed <= SMALL_MAX_FILES and lines_changed <= SMALL_MAX_LINES:
        return "small"
    if files_changed <= MEDIUM_MAX_FILES and lines_changed <= MEDIUM_MAX_LINES:
        return "medium"
    return "large"


def diff_size(root: Path, base_sha: str, outcome_sha: str) -> dict[str, int]:
    """Files and added/deleted line counts between two commits (read-only)."""
    output = git_state.run_git(
        root, "diff", "--numstat", "--no-renames", base_sha, outcome_sha, check=False
    )
    files = 0
    insertions = 0
    deletions = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        added, removed = parts[0], parts[1]
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return {
        "files_changed": files,
        "insertions": insertions,
        "deletions": deletions,
        "lines_changed": insertions + deletions,
    }


def _human_turns_after_first_edit(events: Sequence[NormalizedEvent]) -> tuple[int, list[str]]:
    """User messages after the first mutating action, excluding plain approvals."""
    index = git_state.first_mutating_index(events)
    if index is None:
        return 0, []
    event_ids: list[str] = []
    for event in list(events)[index + 1 :]:
        if event.event_type != "message" or event.actor != "user":
            continue
        if is_acceptance(event_text(event)):
            continue
        event_ids.append(event.event_id)
    return len(event_ids), event_ids[:5]


def _count(values: Mapping[str, Any], key: str) -> int | None:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


@dataclass(frozen=True)
class OutcomeLabels:
    """What happened to one source, with the evidence that proves it."""

    label: str
    survived: bool | None
    in_branch_history: bool
    pushback_turns: int
    signal_outcome: str | None
    committed: bool | None
    evidence: dict[str, Any] = field(default_factory=dict)
    detail: str = ""

    @property
    def priority(self) -> int:
        return LABEL_PRIORITY.get(self.label, LABEL_PRIORITY["unlabeled"])

    @property
    def informative(self) -> bool:
        return self.priority == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OUTCOME_LABELS_SCHEMA,
            "label": self.label,
            "informative": self.informative,
            "priority": self.priority,
            "survived": self.survived,
            "in_branch_history": self.in_branch_history,
            "pushback_turns": self.pushback_turns,
            "signal_outcome": self.signal_outcome,
            "committed": self.committed,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


def compute_outcome_labels(
    *,
    repo_root: Path,
    outcome_sha: str,
    branch: str = "HEAD",
    events: Sequence[NormalizedEvent] = (),
    signal_values: Mapping[str, Any] | None = None,
    signal_evidence: Mapping[str, Any] | None = None,
    stability_horizon_days: int = 7,
) -> OutcomeLabels:
    """Label one source from commit survival, pushback proxies and signals.

    ``survived`` is true when the outcome commit is still reachable from the
    project branch and no revert of it exists; a revert found after the stability
    horizon keeps the source but records the revert as evidence.
    """
    values = dict(signal_values or {})
    evidence: dict[str, Any] = {}

    in_branch_history = git_state.is_ancestor(repo_root, outcome_sha, branch)
    revert = git_state.find_revert(repo_root, outcome_sha, branch=branch)
    survived: bool | None = in_branch_history and revert is None
    if revert is not None:
        evidence["revert"] = revert
        reverted_at = _parse_timestamp(str(revert.get("committed_at")))
        outcome_at = _parse_timestamp(
            git_state.run_git(repo_root, "show", "-s", "--format=%cI", outcome_sha, check=False)
        )
        if reverted_at is None or outcome_at is None:
            survived = None
        else:
            survived = (reverted_at - outcome_at) >= timedelta(days=stability_horizon_days)
            evidence["revert_after_days"] = round(
                (reverted_at - outcome_at).total_seconds() / 86400.0, 3
            )
    evidence["branch"] = branch
    evidence["outcome_sha"] = outcome_sha

    corrections = _count(values, "friction.user_corrections") or 0
    interruptions = _count(values, "friction.interruptions") or 0
    churn = _count(values, "friction.edit_churn") or 0
    turns_after_edit, turn_event_ids = _human_turns_after_first_edit(events)
    pushback_turns = max(corrections, interruptions, turns_after_edit)
    if turn_event_ids:
        evidence["human_turns_after_first_edit"] = turn_event_ids
    for signal_id in (
        "friction.user_corrections",
        "friction.interruptions",
        "friction.edit_churn",
        "outcome.status",
        "outcome.committed",
    ):
        references = (signal_evidence or {}).get(signal_id)
        if isinstance(references, list) and references:
            evidence.setdefault("signals", {})[signal_id] = list(references)[:5]

    raw_outcome = values.get("outcome.status")
    signal_outcome = raw_outcome if isinstance(raw_outcome, str) else None
    raw_committed = values.get("outcome.committed")
    committed = raw_committed if isinstance(raw_committed, bool) else None

    label = "unlabeled"
    detail = "outcome survival could not be established"
    if survived is False:
        detail = "outcome commit did not survive in the project branch"
    elif survived is None:
        detail = "outcome was reverted and revert timing could not be established"
    elif signal_outcome in ("failed", "partial", "abandoned"):
        label = "failed_then_fixed"
        detail = f"session outcome was {signal_outcome} yet the work landed durably"
    elif pushback_turns > 0 or churn > 0:
        label = "corrected"
        detail = (
            f"user corrected the work ({pushback_turns} pushback turn(s), "
            f"edit churn {churn})"
        )
    else:
        label = "successful_surviving"
        detail = "durable outcome with no recorded pushback"
    return OutcomeLabels(
        label=label,
        survived=survived,
        in_branch_history=in_branch_history,
        pushback_turns=pushback_turns,
        signal_outcome=signal_outcome,
        committed=committed,
        evidence=evidence,
        detail=detail,
    )


def commit_outcome_labels(
    *,
    repo_root: Path,
    outcome_sha: str,
    branch: str = "HEAD",
    stability_horizon_days: int = 7,
    linked_session_key: str | None = None,
    signal_values: Mapping[str, Any] | None = None,
    signal_evidence: Mapping[str, Any] | None = None,
    events: Sequence[NormalizedEvent] = (),
) -> OutcomeLabels:
    """Labels for a mined commit; a linked session contributes pushback proxies."""
    labels = compute_outcome_labels(
        repo_root=repo_root,
        outcome_sha=outcome_sha,
        branch=branch,
        events=events,
        signal_values=signal_values,
        signal_evidence=signal_evidence,
        stability_horizon_days=stability_horizon_days,
    )
    evidence = dict(labels.evidence)
    evidence["source_kind"] = "commit"
    if linked_session_key:
        evidence["linked_session"] = linked_session_key
    return OutcomeLabels(
        label=labels.label,
        survived=labels.survived,
        in_branch_history=labels.in_branch_history,
        pushback_turns=labels.pushback_turns,
        signal_outcome=labels.signal_outcome,
        committed=True if labels.survived else labels.committed,
        evidence=evidence,
        detail=labels.detail,
    )


@dataclass(frozen=True)
class SamplingUnit:
    """One candidate reduced to the fields the sampler orders by."""

    source_id: str
    repo_key: str
    repo_label: str
    work_kind: str
    size: str
    priority: int
    label: str

    @property
    def stratum(self) -> tuple[str, str, str]:
        return (self.repo_key, self.work_kind, self.size)

    @property
    def stratum_id(self) -> str:
        return "|".join(self.stratum)


@dataclass(frozen=True)
class SamplingDecision:
    source_id: str
    stratum_id: str
    repo_label: str
    label: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "stratum": self.stratum_id,
            "repo": self.repo_label,
            "label": self.label,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SamplingPlan:
    strategy: str
    seed: int
    max_per_repo: int | None
    limit: int | None
    kept: list[str] = field(default_factory=list)
    dropped: list[SamplingDecision] = field(default_factory=list)
    strata: dict[str, int] = field(default_factory=dict)
    repos: dict[str, int] = field(default_factory=dict)
    labels: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SAMPLING_SCHEMA,
            "strategy": self.strategy,
            "seed": self.seed,
            "max_per_repo": self.max_per_repo,
            "limit": self.limit,
            "kept": list(self.kept),
            "dropped": [item.to_dict() for item in self.dropped],
            "strata": dict(sorted(self.strata.items())),
            "repos": dict(sorted(self.repos.items())),
            "labels": dict(sorted(self.labels.items())),
        }


def _seeded_rank(seed: int, *parts: str) -> str:
    material = "\x1f".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def resolve_repo_cap(
    *, max_per_repo: int | None, limit: int | None, share: float = DEFAULT_REPO_CAP_SHARE
) -> int | None:
    """Explicit cap wins; a cap is derived only when a selection limit is set."""
    if max_per_repo is not None:
        return max(1, max_per_repo)
    if limit is None:
        return None
    return max(1, math.ceil(limit * share))


def plan_sample(
    units: Sequence[SamplingUnit],
    *,
    strategy: str = "stratified",
    seed: int = 0,
    max_per_repo: int | None = None,
    limit: int | None = None,
) -> SamplingPlan:
    """Order (and optionally cap) candidates deterministically.

    ``lexicographic`` preserves the historical ``source_id`` ordering. The default
    ``stratified`` strategy walks strata round-robin — one source per
    (repo, work kind, size) stratum per round — with a seeded order inside each
    stratum that places informative outcomes first. Without ``limit`` or
    ``max_per_repo`` nothing is dropped: the sampler only reorders.
    """
    if strategy not in SAMPLING_STRATEGIES:
        raise ValueError(f"unknown sampling strategy {strategy!r}; expected {SAMPLING_STRATEGIES}")
    cap = resolve_repo_cap(max_per_repo=max_per_repo, limit=limit)
    strata_counts: dict[str, int] = {}
    repo_counts: dict[str, int] = {}
    label_counts: dict[str, int] = {}
    for unit in units:
        strata_counts[unit.stratum_id] = strata_counts.get(unit.stratum_id, 0) + 1
        repo_counts[unit.repo_label] = repo_counts.get(unit.repo_label, 0) + 1
        label_counts[unit.label] = label_counts.get(unit.label, 0) + 1

    if strategy == "lexicographic":
        ordered = sorted(units, key=lambda unit: unit.source_id)
    else:
        buckets: dict[str, list[SamplingUnit]] = {}
        for unit in units:
            buckets.setdefault(unit.stratum_id, []).append(unit)
        for bucket in buckets.values():
            bucket.sort(
                key=lambda unit: (
                    unit.priority,
                    _seeded_rank(seed, unit.stratum_id, unit.source_id),
                    unit.source_id,
                )
            )
        stratum_order = sorted(
            buckets, key=lambda stratum_id: (_seeded_rank(seed, stratum_id), stratum_id)
        )
        ordered = []
        depth = 0
        while True:
            round_units = [
                buckets[stratum_id][depth]
                for stratum_id in stratum_order
                if depth < len(buckets[stratum_id])
            ]
            if not round_units:
                break
            ordered.extend(round_units)
            depth += 1

    kept: list[str] = []
    dropped: list[SamplingDecision] = []
    per_repo: dict[str, int] = {}
    for unit in ordered:
        if cap is not None and per_repo.get(unit.repo_key, 0) >= cap:
            dropped.append(
                SamplingDecision(
                    source_id=unit.source_id,
                    stratum_id=unit.stratum_id,
                    repo_label=unit.repo_label,
                    label=unit.label,
                    reason="repo_cap",
                )
            )
            continue
        if limit is not None and len(kept) >= limit:
            dropped.append(
                SamplingDecision(
                    source_id=unit.source_id,
                    stratum_id=unit.stratum_id,
                    repo_label=unit.repo_label,
                    label=unit.label,
                    reason="limit",
                )
            )
            continue
        per_repo[unit.repo_key] = per_repo.get(unit.repo_key, 0) + 1
        kept.append(unit.source_id)

    return SamplingPlan(
        strategy=strategy,
        seed=seed,
        max_per_repo=cap,
        limit=limit,
        kept=kept,
        dropped=dropped,
        strata=strata_counts,
        repos=repo_counts,
        labels=label_counts,
    )


def sampling_unit(
    *,
    source_id: str,
    repo_key: str,
    repo_label: str,
    work_kind: str | None,
    files_changed: int,
    lines_changed: int,
    labels: OutcomeLabels | Mapping[str, Any] | None,
) -> SamplingUnit:
    if isinstance(labels, OutcomeLabels):
        label = labels.label
        priority = labels.priority
    elif isinstance(labels, Mapping):
        label = str(labels.get("label") or "unlabeled")
        priority = LABEL_PRIORITY.get(label, LABEL_PRIORITY["unlabeled"])
    else:
        label = "unlabeled"
        priority = LABEL_PRIORITY["unlabeled"]
    return SamplingUnit(
        source_id=source_id,
        repo_key=repo_key,
        repo_label=repo_label,
        work_kind=work_kind or "unknown",
        size=size_bucket(files_changed, lines_changed),
        priority=priority,
        label=label,
    )


__all__ = [
    "ACCEPTANCE_RE",
    "ACCEPTANCE_RETRACTION_RE",
    "DEFAULT_REPO_CAP_SHARE",
    "LABEL_PRIORITY",
    "OUTCOME_LABELS_SCHEMA",
    "OUTCOME_LABEL_KINDS",
    "SAMPLING_SCHEMA",
    "SAMPLING_STRATEGIES",
    "OutcomeLabels",
    "SamplingDecision",
    "SamplingPlan",
    "SamplingUnit",
    "commit_outcome_labels",
    "compute_outcome_labels",
    "diff_size",
    "is_acceptance",
    "plan_sample",
    "resolve_repo_cap",
    "sampling_unit",
    "size_bucket",
    "user_accepted",
]
