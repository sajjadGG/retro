"""Experimental trajectory signals over inferred action/result steps."""
from __future__ import annotations

import re
import statistics
from collections import Counter

from ..trajectory import (
    ALL_CATEGORIES,
    CORE_CATEGORIES,
    TrajectoryStep,
    build_trajectory,
    ngrams,
    result_is_failed_text,
    shannon_entropy,
)
from .base import REGISTRY, SessionContext, reading, register


def _steps(ctx: SessionContext) -> list[TrajectoryStep]:
    return build_trajectory(ctx.events)


def _evidence(steps: list[TrajectoryStep], *, limit: int = 8) -> list[str]:
    refs: list[str] = []
    for step in steps:
        refs.append(step.action_event_id)
        refs.extend(step.result_event_ids[:1])
        if len(refs) >= limit:
            break
    return refs[:limit]


def _step_meta(steps: list[TrajectoryStep], *, limit: int = 8) -> dict:
    return {
        "step_indices": [s.index for s in steps[:limit]],
        "categories": [s.action_category for s in steps[:limit]],
        "fingerprints": [s.action_fingerprint for s in steps[:limit]],
        "fingerprint_keys": [s.action_fingerprint_key for s in steps[:limit]],
    }


@register(
    "trajectory_step_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of inferred action-centered trajectory steps.",
)
def _trajectory_step_count(ctx: SessionContext):
    steps = _steps(ctx)
    return reading(ctx, _trajectory_step_count, len(steps), evidence=_evidence(steps))


@register(
    "trajectory_unknown_action_ratio",
    group="risk",
    kind="numeric",
    unit="ratio",
    description="Fraction of trajectory steps categorized as other or error.",
)
def _trajectory_unknown_action_ratio(ctx: SessionContext):
    steps = _steps(ctx)
    if not steps:
        return None
    unknown = [s for s in steps if s.action_category in {"other", "error"}]
    return reading(
        ctx,
        _trajectory_unknown_action_ratio,
        round(len(unknown) / len(steps), 4),
        evidence=_evidence(unknown),
        metadata={"unknown_steps": [s.index for s in unknown[:8]], "total_steps": len(steps)},
    )


@register(
    "trajectory_phase_coverage",
    group="activity",
    kind="numeric",
    unit="ratio",
    description="Unique core action categories observed divided by the eight core trajectory categories.",
)
def _trajectory_phase_coverage(ctx: SessionContext):
    steps = _steps(ctx)
    seen = {s.action_category for s in steps if s.action_category in CORE_CATEGORIES}
    return reading(
        ctx,
        _trajectory_phase_coverage,
        round(len(seen) / len(CORE_CATEGORIES), 4),
        metadata={"seen_categories": sorted(seen), "core_categories": list(CORE_CATEGORIES)},
    )


@register(
    "trajectory_validation_presence",
    group="outcome",
    kind="boolean",
    unit="flag",
    description="True when a validation/test step appears after the first generated fix.",
)
def _trajectory_validation_presence(ctx: SessionContext):
    steps = _steps(ctx)
    first_edit = next((s.index for s in steps if s.action_category == "generate_fix"), None)
    if first_edit is None:
        return reading(ctx, _trajectory_validation_presence, False, metadata={"reason": "no_fix_step"})
    validations = [s for s in steps if s.index > first_edit and s.action_category == "run_tests"]
    return reading(
        ctx,
        _trajectory_validation_presence,
        bool(validations),
        evidence=_evidence(validations[:1]),
        metadata={"first_fix_step": first_edit, "validation_steps": [s.index for s in validations[:8]]},
    )


def _register_category_signals() -> None:
    for category in ALL_CATEGORIES:
        count_name = f"trajectory_action_count_{category}"
        ratio_name = f"trajectory_action_ratio_{category}"

        @register(
            count_name,
            group="activity",
            kind="numeric",
            unit="count",
            description=f"Number of inferred trajectory steps categorized as {category}.",
        )
        def _count(ctx: SessionContext, category=category, signal_name=count_name):
            steps = _steps(ctx)
            hits = [s for s in steps if s.action_category == category]
            return reading(
                ctx,
                REGISTRY[signal_name],
                len(hits),
                evidence=_evidence(hits),
                metadata={"category": category, "total_steps": len(steps), "first_step_indices": [s.index for s in hits[:8]]},
            )

        @register(
            ratio_name,
            group="activity",
            kind="numeric",
            unit="ratio",
            description=f"Fraction of inferred trajectory steps categorized as {category}.",
        )
        def _ratio(ctx: SessionContext, category=category, signal_name=ratio_name):
            steps = _steps(ctx)
            hits = [s for s in steps if s.action_category == category]
            value = round(len(hits) / len(steps), 4) if steps else 0.0
            return reading(
                ctx,
                REGISTRY[signal_name],
                value,
                evidence=_evidence(hits),
                metadata={"category": category, "total_steps": len(steps), "first_step_indices": [s.index for s in hits[:8]]},
            )


_register_category_signals()


@register(
    "trajectory_action_redundancy",
    group="risk",
    kind="numeric",
    unit="count",
    description="Repeated action fingerprints beyond the first occurrence.",
)
def _trajectory_action_redundancy(ctx: SessionContext):
    steps = _steps(ctx)
    counts = Counter(s.action_fingerprint_key for s in steps)
    value = sum(c - 1 for c in counts.values() if c > 1)
    repeated = [s for s in steps if counts[s.action_fingerprint_key] > 1]
    return reading(
        ctx,
        _trajectory_action_redundancy,
        value,
        evidence=_evidence(repeated),
        metadata=_step_meta(repeated),
    )


@register(
    "trajectory_consecutive_repetition_count",
    group="risk",
    kind="numeric",
    unit="count",
    description="Adjacent trajectory steps with identical category or identical action fingerprint.",
)
def _trajectory_consecutive_repetition_count(ctx: SessionContext):
    steps = _steps(ctx)
    hits = []
    for prev, cur in zip(steps, steps[1:]):
        if (
            prev.action_fingerprint_key == cur.action_fingerprint_key
            or prev.action_category == cur.action_category
        ):
            hits.append(cur)
    return reading(
        ctx,
        _trajectory_consecutive_repetition_count,
        len(hits),
        evidence=_evidence(hits),
        metadata=_step_meta(hits),
    )


@register(
    "trajectory_max_repetition_run",
    group="risk",
    kind="numeric",
    unit="count",
    description="Longest adjacent run with the same action category or fingerprint.",
)
def _trajectory_max_repetition_run(ctx: SessionContext):
    steps = _steps(ctx)
    best = 0
    cur = 0
    last_cat = None
    last_fp = None
    for step in steps:
        if step.action_category == last_cat or step.action_fingerprint_key == last_fp:
            cur += 1
        else:
            cur = 1
        best = max(best, cur)
        last_cat = step.action_category
        last_fp = step.action_fingerprint_key
    return reading(ctx, _trajectory_max_repetition_run, best)


@register(
    "trajectory_4gram_repetition_ratio",
    group="risk",
    kind="numeric",
    unit="ratio",
    description="Fraction of 4-gram action-category windows that repeat within the session.",
)
def _trajectory_4gram_repetition_ratio(ctx: SessionContext):
    grams = ngrams([s.action_category for s in _steps(ctx)], 4)
    if not grams:
        return reading(ctx, _trajectory_4gram_repetition_ratio, 0.0, metadata={"windows": 0})
    counts = Counter(grams)
    repeated_windows = sum(c for c in counts.values() if c > 1)
    return reading(
        ctx,
        _trajectory_4gram_repetition_ratio,
        round(repeated_windows / len(grams), 4),
        metadata={"windows": len(grams), "unique_windows": len(counts)},
    )


@register(
    "trajectory_4gram_top",
    group="activity",
    kind="text",
    unit="label",
    description="Most frequent 4-gram action-category sequence.",
)
def _trajectory_4gram_top(ctx: SessionContext):
    grams = ngrams([s.action_category for s in _steps(ctx)], 4)
    if not grams:
        return None
    gram, count = Counter(grams).most_common(1)[0]
    return reading(
        ctx,
        _trajectory_4gram_top,
        " -> ".join(gram),
        metadata={"ngram": list(gram), "count": count, "windows": len(grams)},
    )


@register(
    "trajectory_sequence_entropy",
    group="activity",
    kind="numeric",
    unit="score",
    description="Shannon entropy over action categories.",
)
def _trajectory_sequence_entropy(ctx: SessionContext):
    return reading(
        ctx,
        _trajectory_sequence_entropy,
        shannon_entropy([s.action_category for s in _steps(ctx)]),
    )


@register(
    "trajectory_phase_transition_count",
    group="activity",
    kind="numeric",
    unit="count",
    description="Number of adjacent category changes in the inferred trajectory.",
)
def _trajectory_phase_transition_count(ctx: SessionContext):
    steps = _steps(ctx)
    value = sum(1 for a, b in zip(steps, steps[1:]) if a.action_category != b.action_category)
    return reading(ctx, _trajectory_phase_transition_count, value)


@register(
    "trajectory_repeated_action_without_progress",
    group="risk",
    kind="boolean",
    unit="flag",
    description="True when the same action fingerprint repeats in a short window without a category shift.",
)
def _trajectory_repeated_action_without_progress(ctx: SessionContext):
    steps = _steps(ctx)
    hits = []
    for i, step in enumerate(steps):
        for prior in steps[max(0, i - 3) : i]:
            if (
                step.action_fingerprint_key == prior.action_fingerprint_key
                and step.action_category == prior.action_category
            ):
                hits.append(step)
                break
    return reading(
        ctx,
        _trajectory_repeated_action_without_progress,
        bool(hits),
        evidence=_evidence(hits),
        confidence=0.75 if hits else 1.0,
        metadata=_step_meta(hits),
    )


@register(
    "trajectory_fix_without_validation_count",
    group="risk",
    kind="numeric",
    unit="count",
    description="Generate-fix steps not followed by validation within the next four steps.",
)
def _trajectory_fix_without_validation_count(ctx: SessionContext):
    steps = _steps(ctx)
    hits = []
    for i, step in enumerate(steps):
        if step.action_category != "generate_fix":
            continue
        window = steps[i + 1 : i + 5]
        if not any(s.action_category == "run_tests" for s in window):
            hits.append(step)
    return reading(
        ctx,
        _trajectory_fix_without_validation_count,
        len(hits),
        evidence=_evidence(hits),
        metadata=_step_meta(hits),
    )


@register(
    "trajectory_fix_validation_latency_steps",
    group="cost",
    kind="numeric",
    unit="count",
    description="Median number of steps from a generated fix to the next validation step.",
)
def _trajectory_fix_validation_latency_steps(ctx: SessionContext):
    steps = _steps(ctx)
    latencies = []
    for i, step in enumerate(steps):
        if step.action_category != "generate_fix":
            continue
        for later in steps[i + 1 :]:
            if later.action_category == "run_tests":
                latencies.append(later.index - step.index)
                break
    if not latencies:
        return None
    return reading(
        ctx,
        _trajectory_fix_validation_latency_steps,
        statistics.median(latencies),
        metadata={"latencies": latencies[:20]},
    )


@register(
    "trajectory_premature_finish_without_validation",
    group="risk",
    kind="boolean",
    unit="flag",
    description="True when edits occurred but no validation step followed the last edit.",
)
def _trajectory_premature_finish_without_validation(ctx: SessionContext):
    steps = _steps(ctx)
    last_fix = max((s.index for s in steps if s.action_category == "generate_fix"), default=None)
    if last_fix is None:
        return reading(ctx, _trajectory_premature_finish_without_validation, False)
    has_validation = any(s.index > last_fix and s.action_category == "run_tests" for s in steps)
    return reading(
        ctx,
        _trajectory_premature_finish_without_validation,
        not has_validation,
        evidence=_evidence([s for s in steps if s.index == last_fix]),
        metadata={"last_fix_step": last_fix},
    )


@register(
    "trajectory_test_fix_loop_count",
    group="risk",
    kind="numeric",
    unit="count",
    description="Repeated fix/test cycles where validation appears to keep failing without exploration between cycles.",
)
def _trajectory_test_fix_loop_count(ctx: SessionContext):
    steps = _steps(ctx)
    count = 0
    evidence_steps = []
    for a, b, c, d in zip(steps, steps[1:], steps[2:], steps[3:]):
        if (
            a.action_category == "generate_fix"
            and b.action_category == "run_tests"
            and c.action_category == "generate_fix"
            and d.action_category == "run_tests"
            and (result_is_failed_text(b.result_text) or result_is_failed_text(d.result_text))
        ):
            count += 1
            evidence_steps.extend([b, d])
    return reading(
        ctx,
        _trajectory_test_fix_loop_count,
        count,
        evidence=_evidence(evidence_steps),
        metadata=_step_meta(evidence_steps),
    )


@register(
    "trajectory_failed_result_recovery_steps",
    group="cost",
    kind="numeric",
    unit="count",
    description="Median steps from a failed result to a changed category or fingerprint.",
)
def _trajectory_failed_result_recovery_steps(ctx: SessionContext):
    steps = _steps(ctx)
    latencies = []
    for i, step in enumerate(steps):
        if not result_is_failed_text(step.result_text):
            continue
        for later in steps[i + 1 :]:
            if (
                later.action_category != step.action_category
                or later.action_fingerprint_key != step.action_fingerprint_key
            ):
                latencies.append(later.index - step.index)
                break
    if not latencies:
        return None
    return reading(
        ctx,
        _trajectory_failed_result_recovery_steps,
        statistics.median(latencies),
        metadata={"latencies": latencies[:20]},
    )


@register(
    "trajectory_failure_ignored_count",
    group="risk",
    kind="numeric",
    unit="count",
    description="Failed result followed by the same action or an explanation/final step.",
)
def _trajectory_failure_ignored_count(ctx: SessionContext):
    steps = _steps(ctx)
    hits = []
    for step, later in zip(steps, steps[1:]):
        if not result_is_failed_text(step.result_text):
            continue
        if (
            later.action_fingerprint_key == step.action_fingerprint_key
            or later.action_category in {"explain", "other"}
        ):
            hits.append(later)
    return reading(
        ctx,
        _trajectory_failure_ignored_count,
        len(hits),
        evidence=_evidence(hits),
        metadata=_step_meta(hits),
    )


@register(
    "trajectory_result_token_reuse_ratio",
    group="activity",
    kind="numeric",
    unit="ratio",
    description="Fraction of steps whose next action/thought reuses salient terms from the prior result.",
)
def _trajectory_result_token_reuse_ratio(ctx: SessionContext):
    steps = _steps(ctx)
    eligible = 0
    reused = 0
    examples = []
    for step, later in zip(steps, steps[1:]):
        terms = _salient_terms(step.result_text)
        if not terms:
            continue
        eligible += 1
        next_text = (later.thought_text + " " + later.action_text).lower()
        matched = sorted(t for t in terms if t.lower() in next_text)
        if matched:
            reused += 1
            examples.append({"from_step": step.index, "to_step": later.index, "terms": matched[:6]})
    value = round(reused / eligible, 4) if eligible else 0.0
    return reading(
        ctx,
        _trajectory_result_token_reuse_ratio,
        value,
        metadata={"eligible_pairs": eligible, "reused_pairs": reused, "examples": examples[:8]},
    )


@register(
    "trajectory_error_to_search_or_edit_ratio",
    group="outcome",
    kind="numeric",
    unit="ratio",
    description="Fraction of failed results followed by search, exploration, edit, test, or locate behavior.",
)
def _trajectory_error_to_search_or_edit_ratio(ctx: SessionContext):
    steps = _steps(ctx)
    failures = 0
    recovered = 0
    for step, later in zip(steps, steps[1:]):
        if not result_is_failed_text(step.result_text):
            continue
        failures += 1
        if later.action_category in {"search", "explore", "locate", "generate_fix", "run_tests"}:
            recovered += 1
    value = round(recovered / failures, 4) if failures else 0.0
    return reading(
        ctx,
        _trajectory_error_to_search_or_edit_ratio,
        value,
        metadata={"failed_results": failures, "recovery_followups": recovered},
    )


@register(
    "trajectory_exploration_ratio",
    group="activity",
    kind="numeric",
    unit="ratio",
    description="Fraction of steps spent exploring, searching, locating, or explaining.",
)
def _trajectory_exploration_ratio(ctx: SessionContext):
    steps = _steps(ctx)
    cats = {"explore", "search", "locate", "explain"}
    value = round(sum(1 for s in steps if s.action_category in cats) / len(steps), 4) if steps else 0.0
    return reading(ctx, _trajectory_exploration_ratio, value, metadata={"total_steps": len(steps)})


@register(
    "trajectory_exploitation_ratio",
    group="activity",
    kind="numeric",
    unit="ratio",
    description="Fraction of steps spent reproducing, editing, validating, or refactoring.",
)
def _trajectory_exploitation_ratio(ctx: SessionContext):
    steps = _steps(ctx)
    cats = {"reproduce", "generate_fix", "run_tests", "refactor"}
    value = round(sum(1 for s in steps if s.action_category in cats) / len(steps), 4) if steps else 0.0
    return reading(ctx, _trajectory_exploitation_ratio, value, metadata={"total_steps": len(steps)})


@register(
    "trajectory_balance_score",
    group="activity",
    kind="numeric",
    unit="score",
    description="Balance between exploration and exploitation ratios; 1.0 is evenly balanced.",
)
def _trajectory_balance_score(ctx: SessionContext):
    steps = _steps(ctx)
    if not steps:
        return reading(ctx, _trajectory_balance_score, 0.0)
    explore = sum(1 for s in steps if s.action_category in {"explore", "search", "locate", "explain"}) / len(steps)
    exploit = sum(1 for s in steps if s.action_category in {"reproduce", "generate_fix", "run_tests", "refactor"}) / len(steps)
    return reading(ctx, _trajectory_balance_score, round(1 - abs(explore - exploit), 4))


def _salient_terms(text: str) -> set[str]:
    if not text:
        return set()
    terms: set[str] = set()
    for match in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]{3,}", text):
        low = match.lower().strip("./")
        if low in _STOPWORDS or len(low) < 4:
            continue
        if any(ch in match for ch in "./_") or match[:1].isupper() or low.endswith(("error", "exception")):
            terms.add(match.strip(".,:;()[]{}'\""))
        if len(terms) >= 20:
            break
    return terms


_STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "have",
    "there",
    "their",
    "would",
    "could",
    "should",
    "process",
    "exited",
    "code",
    "output",
    "error",
    "failed",
}
