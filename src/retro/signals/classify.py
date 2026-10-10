"""Judgements derived from a `SessionProfile`: outcome, work kind, language.

These are the only genuinely inferential signals. They are kept here, apart
from fact extraction, so their rules can be read, argued with, and re-tuned
against hand labels without touching the scan.
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from . import commands as cmd
from . import patterns as pat
from .profile import SessionProfile

OUTCOME_CATEGORIES: tuple[str, ...] = ("success", "partial", "failed", "abandoned", "unknown")
WORK_KINDS: tuple[str, ...] = (
    "build",
    "debug",
    "refactor",
    "test",
    "explore",
    "review",
    "docs",
    "ops",
    "research",
    "chat",
    "other",
)
# Ties go to the more specific kind: "build" and "other" are what everything
# falls back to, so they must never win a tie against a sharper label.
_KIND_SPECIFICITY: tuple[str, ...] = (
    "test",
    "docs",
    "review",
    "refactor",
    "debug",
    "ops",
    "research",
    "explore",
    "chat",
    "build",
    "other",
)
_KIND_PRECEDENCE = {kind: index for index, kind in enumerate(_KIND_SPECIFICITY)}
CUE_WINDOW = 600
IDLE_CUE_WINDOW = 300
_CUE_WEIGHTS: dict[str, int] = {
    "debug": 3,
    "test": 3,
    "refactor": 3,
    "review": 3,
    "docs": 3,
    "ops": 3,
    "research": 3,
    "explore": 2,
    "build": 2,
}


@dataclass(frozen=True)
class Judgement:
    value: str
    confidence: float
    evidence: tuple[str, ...] = ()
    reason: str = ""


def classify_outcome(profile: SessionProfile) -> Judgement:
    """Decide how a session ended. First matching rule wins.

    Rule order encodes what dominates what: a provider limit or a user abort
    beats any amount of completed work, an explicit refusal beats an edit
    count, and a final message that is still describing the next step means
    the session stopped mid-stride no matter what came before it.
    """
    if not profile.has_activity:
        return Judgement("unknown", 0.9, (), "no conversational activity captured")

    final = profile.final_assistant_text
    # A one-turn session has no "closing" message: its last human text is the
    # opening request, which must not be read as a verdict on the work.
    closing = profile.last_human_text[:600] if profile.user_turns >= 2 else ""
    negative_close = bool(pat.CLOSING_NEGATIVE_RE.search(closing))
    positive_close = bool(pat.CLOSING_POSITIVE_RE.search(closing)) and not negative_close

    if final and pat.LIMIT_REACHED_RE.search(final[:600]):
        return Judgement("abandoned", 0.8, (), "the provider cut the session off at a usage limit")
    if profile.interruption_events and _aborted_at_end(profile):
        return Judgement(
            "abandoned", 0.7, tuple(profile.interruption_events[:5]), "session ends on a user abort"
        )
    if negative_close:
        return Judgement("failed", 0.65, (), "last human message reports it still does not work")
    if final and pat.INABILITY_RE.search(final[:600]):
        return Judgement("failed", 0.7, (), "the agent states it could not complete the request")
    if _ends_mid_step(final):
        return Judgement("partial", 0.6, (), "the final message is still announcing the next step")

    validated = profile.validated
    if profile.committed:
        evidence = tuple(event_id for event_id, ok in profile.commit_events if ok)[:5]
        confidence = 0.9 if validated is not False else 0.7
        return Judgement("success", confidence, evidence, "a commit succeeded during the session")
    if validated is True and profile.file_edits > 0:
        evidence = tuple(run.event_id for run in profile.validation_runs if run.ok)[-5:]
        confidence = 0.8 if profile.strong_validation else 0.65
        return Judgement("success", confidence, evidence, "edits made and the last validation run passed")
    if positive_close:
        return Judgement("success", 0.6, (), "last human message closes positively")
    if validated is False:
        evidence = tuple(run.event_id for run in profile.validation_runs if not run.ok)[-5:]
        if profile.file_edits:
            return Judgement("partial", 0.65, evidence, "edits made but the last validation run failed")
        return Judgement("failed", 0.6, evidence, "validation ran and failed with nothing delivered")
    if profile.ended_in_error:
        evidence = tuple(profile.error_events[-5:])
        if profile.file_edits:
            return Judgement("partial", 0.6, evidence, "edits made but the session ends on a tool error")
        return Judgement("failed", 0.55, evidence, "no edits and the session ends on a tool error")
    if profile.file_edits > 0:
        return Judgement("success", 0.5, (), "work delivered and nothing contradicts it")
    if profile.assistant_messages and final:
        return Judgement("success", 0.45, (), "answer-only session that ended with a reply")
    return Judgement("unknown", 0.5, (), "not enough evidence")


def _ends_mid_step(final: str) -> bool:
    """A short, forward-looking last message means the turn never finished."""
    if not final or len(final) > 600:
        return False
    return bool(pat.CONTINUATION_RE.search(final[:200]) or pat.CONTINUATION_RE.search(final[-200:]))


def _aborted_at_end(profile: SessionProfile) -> bool:
    """True when nothing of substance happened after the last interruption."""
    if profile.committed:
        return False
    if profile.validated is True:
        return False
    return profile.trailing_failures > 0 or profile.file_edits == 0 or len(profile.interruption_events) >= 2


def classify_work_kind(profile: SessionProfile) -> Judgement:
    prompt = (profile.first_prompt or profile.title)[:1500]
    opaque = not profile.first_prompt and not profile.assistant_messages and not profile.file_edits
    if not prompt or opaque:
        return Judgement("other", 0.3, (), "opaque run: no human prompt or assistant output")
    # Nothing was realized: no reply, no tools, no edits. Only an intent stated
    # up front counts; a keyword buried in a long prompt is not evidence.
    idle = not profile.assistant_messages and not profile.tool_calls and not profile.file_edits
    cue_window = IDLE_CUE_WINDOW if idle else CUE_WINDOW
    scores: Counter[str] = Counter()
    cue_hits = []
    positions = {}
    for kind, regex in pat.WORK_KIND_CUES:
        match = regex.search(prompt)
        if match:
            scores[kind] += _CUE_WEIGHTS.get(kind, 2)
            cue_hits.append(kind)
            positions[kind] = match.start()
    early = {kind: at for kind, at in positions.items() if at < cue_window}
    if early:
        # The intent stated first is usually the real one.
        scores[min(early, key=lambda kind: early[kind])] += 2
    elif idle:
        return Judgement("other", 0.3, (), "no realized work and no up-front intent")

    edits = profile.file_edits
    edited = profile.files_edited
    categories = profile.command_categories

    if not profile.tool_calls and profile.assistant_messages:
        scores["chat"] += 4
    if edits == 0 and (profile.file_reads or profile.searches):
        scores["explore"] += 3
    if edits:
        scores["build"] += 1
        test_files = sum(1 for path in edited if pat.TEST_PATH_RE.search(path))
        doc_files = sum(1 for path in edited if pat.DOC_PATH_RE.search(path))
        if test_files * 2 >= len(edited) and test_files:
            scores["test"] += 3
        if doc_files >= 0.7 * len(edited) and doc_files:
            scores["docs"] += 3
        if profile.tool_errors and "debug" in cue_hits:
            scores["debug"] += 2
        if profile.edit_churn is not None and profile.edit_churn > 0.5 and "refactor" in cue_hits:
            scores["refactor"] += 1
    if categories.get(cmd.OPS, 0) >= 3 and edits <= 1:
        scores["ops"] += 2
    if profile.web_searches >= 3 and edits == 0:
        scores["research"] += 2
    if categories.get(cmd.TEST, 0) >= 2 and edits == 0:
        scores["test"] += 2

    if not scores:
        return Judgement("other", 0.3, (), "no distinguishing cues")
    best = max(scores.items(), key=lambda item: (item[1], -_KIND_PRECEDENCE.get(item[0], 99)))
    runner_up = max((value for kind, value in scores.items() if kind != best[0]), default=0)
    margin = best[1] - runner_up
    confidence = min(0.85, 0.4 + 0.1 * margin + (0.1 if best[0] in cue_hits else 0.0))
    return Judgement(best[0], round(confidence, 2), (), f"score {best[1]} vs {runner_up}")


def primary_language(profile: SessionProfile) -> str | None:
    counts = _language_counts(sorted(profile.files_edited))
    if not counts:
        counts = _language_counts(sorted(profile.files_read))
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _language_counts(paths: Sequence[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for path in paths:
        extension = os.path.splitext(path)[1].lower()
        language = pat.LANGUAGE_BY_EXTENSION.get(extension)
        if language:
            counts[language] += 1
    return counts
