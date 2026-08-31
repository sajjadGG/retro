"""Deterministic static lint for TaskDefiner candidates (§8.1).

LLM confidence never overrides a failed deterministic check. Every rejection
carries a stable code from ``schema.REJECTION_CODES``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ...schema import read_events
from . import git_state
from .schema import (
    ADJACENCY_OPERATORS,
    MAX_ADJACENT_PER_REPLAY,
    MAX_PROMPT_CHARS,
    MAX_REPLAY_TASKS,
    MAX_TASKS_PER_SOURCE,
    REJECTION_CODES,
    SchemaError,
    SourceBundleManifest,
    TaskCandidate,
    TaskDefinitions,
    TaskRejection,
    compute_task_id,
    json_schema_errors,
    load_packaged_schema,
    normalize_prompt,
    packaged_schema_errors,
)

DEFAULT_NGRAM_SIZE = 8
MIN_STATE_CONFIDENCE = 0.8
_MULTI_MESSAGE_RE = re.compile(
    r"^\s*(?:user|human|assistant|system)\s*[:>]", re.IGNORECASE | re.MULTILINE
)
_MESSAGE_TAG_RE = re.compile(r"<\s*/?\s*(?:user|human)_?message\s*>", re.IGNORECASE)
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_EVIDENCE_REPO_RE = re.compile(r"^repo/(?P<state>base|outcome):(?P<path>.+)$")


@dataclass(frozen=True)
class LintedTask:
    task_id: str
    candidate: TaskCandidate

    @property
    def kind(self) -> str:
        return self.candidate.kind


@dataclass(frozen=True)
class LintReport:
    source_id: str
    accepted: list[LintedTask] = field(default_factory=list)
    rejections: list[TaskRejection] = field(default_factory=list)

    @property
    def accepted_ids(self) -> list[str]:
        return [task.task_id for task in self.accepted]

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rejection in self.rejections:
            counts[rejection.code] = counts.get(rejection.code, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "retro-task-lint-v1",
            "source_id": self.source_id,
            "accepted": [
                {"task_id": task.task_id, "candidate_id": task.candidate.candidate_id,
                 "kind": task.candidate.kind}
                for task in self.accepted
            ],
            "rejections": [
                {**rejection.to_dict(), "candidate_id": rejection.candidate_id}
                for rejection in self.rejections
            ],
            "counts": {
                "accepted": len(self.accepted),
                "rejected": len(self.rejections),
                "by_code": self.rejection_counts(),
            },
        }


@dataclass(frozen=True)
class BundleFacts:
    """Everything the lint needs from a materialized SourceBundle."""

    source_id: str
    base_tree: str
    event_ids: frozenset[str]
    user_event_ids: frozenset[str]
    base_paths: frozenset[str]
    outcome_paths: frozenset[str]
    oracle_ngrams: frozenset[str]
    oracle_lines: frozenset[str]
    max_replay_tasks: int = MAX_REPLAY_TASKS
    adjacent_per_replay: int = 0

    @classmethod
    def from_bundle(cls, bundle_dir: Path, *, ngram_size: int = DEFAULT_NGRAM_SIZE) -> BundleFacts:
        manifest = SourceBundleManifest.from_dict(
            json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
        )
        events_path = bundle_dir / "rollout" / "events.jsonl"
        events = list(read_events(events_path))
        event_ids = frozenset(event.event_id for event in events)
        patch_path = bundle_dir / "repo" / "change.patch"
        patch = patch_path.read_text(encoding="utf-8") if patch_path.is_file() else ""
        added = git_state.added_lines(patch)
        return cls(
            source_id=manifest.source_id,
            base_tree=manifest.repo.base_tree,
            event_ids=event_ids,
            user_event_ids=frozenset(
                event.event_id
                for event in events
                if event.actor == "user" and event.event_type == "message"
            ),
            base_paths=_tree_paths(bundle_dir / "repo" / "base"),
            outcome_paths=_tree_paths(bundle_dir / "repo" / "outcome"),
            oracle_ngrams=oracle_ngrams(added, ngram_size=ngram_size),
            oracle_lines=frozenset(
                fragment
                for line in added
                for fragment in _oracle_fragments(line)
            ),
            max_replay_tasks=manifest.task_limits.max_replay_tasks,
            adjacent_per_replay=manifest.task_limits.adjacent_per_replay,
        )


def _tree_paths(root: Path) -> frozenset[str]:
    if not root.is_dir():
        return frozenset()
    paths: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        paths.add(relative)
        if path.is_dir():
            paths.add(relative + "/")
    return frozenset(paths)


def _normalize_text(value: str) -> str:
    lowered = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_TOKEN_RE.findall(lowered))


def _ngrams(text: str, size: int) -> set[str]:
    tokens = text.split()
    if len(tokens) < size:
        return set()
    return {" ".join(tokens[index : index + size]) for index in range(len(tokens) - size + 1)}


def oracle_ngrams(added: list[str], *, ngram_size: int = DEFAULT_NGRAM_SIZE) -> frozenset[str]:
    """Normalized token n-grams over oracle added lines."""
    grams: set[str] = set()
    for line in added:
        grams |= _ngrams(_normalize_text(line), ngram_size)
    return frozenset(grams)


def _distinctive_short_identifier(token: str, *, line_token_count: int) -> bool:
    value = token.strip("_")
    if not value:
        return False
    if "_" in value and len(value) >= 5:
        return True
    if any(character.isalpha() for character in value) and any(
        character.isdigit() for character in value
    ):
        return len(value) >= 4
    if any(ord(character) > 127 for character in value):
        return len(value) >= 2
    return line_token_count <= 2 and len(value) >= 8


def _oracle_fragments(line: str) -> set[str]:
    normalized = _normalize_text(line)
    tokens = normalized.split()
    if not tokens:
        return set()
    fragments = {
        token
        for token in tokens
        if _distinctive_short_identifier(token, line_token_count=len(tokens))
    }
    if len(tokens) >= 5 or (len(tokens) >= 2 and fragments):
        fragments.add(normalized)
    return fragments


def prompt_leakage(
    prompt: str,
    facts: BundleFacts,
    *,
    ngram_size: int = DEFAULT_NGRAM_SIZE,
) -> list[str]:
    """Return oracle fragments that appear in the prompt."""
    normalized = _normalize_text(prompt)
    hits = sorted(_ngrams(normalized, ngram_size) & set(facts.oracle_ngrams))
    padded_prompt = f" {normalized} "
    for fragment in sorted(facts.oracle_lines):
        if fragment and f" {fragment} " in padded_prompt and fragment not in hits:
            hits.append(fragment)
    return hits


def _path_known(path: str, facts: BundleFacts, state: str | None = None) -> bool:
    cleaned = path.strip()
    if not cleaned:
        return False
    pools = {
        "base": facts.base_paths,
        "outcome": facts.outcome_paths,
    }
    candidates = [pools[state]] if state in pools else [facts.base_paths, facts.outcome_paths]
    for pool in candidates:
        if cleaned in pool:
            return True
    return False


def _evidence_missing(reference: str, facts: BundleFacts) -> str | None:
    reference = reference.strip()
    if not reference:
        return "empty evidence reference"
    match = _EVIDENCE_REPO_RE.match(reference)
    if match:
        if not _path_known(match.group("path"), facts, match.group("state")):
            return f"unknown repository path: {reference}"
        return None
    if reference in facts.event_ids:
        return None
    if _path_known(reference, facts):
        return None
    return f"unknown evidence reference: {reference}"


def _reject(candidate: TaskCandidate, code: str, detail: str) -> TaskRejection:
    return TaskRejection(
        code=code,
        detail=detail,
        goal_event_ids=[candidate.goal_segment.introduced_event_id]
        if candidate.goal_segment.introduced_event_id
        else [],
        candidate_id=candidate.candidate_id,
    )


def lint_candidate(
    candidate: TaskCandidate,
    facts: BundleFacts,
    *,
    ngram_size: int = DEFAULT_NGRAM_SIZE,
) -> TaskRejection | None:
    """Return a rejection for the first failed deterministic check, else None."""
    prompt = candidate.prompt or ""
    if not prompt.strip():
        return _reject(candidate, "BUILDER_CONTRACT_ERROR", "prompt is empty")
    if len(prompt) > MAX_PROMPT_CHARS:
        return _reject(
            candidate,
            "BUILDER_CONTRACT_ERROR",
            f"prompt exceeds {MAX_PROMPT_CHARS} UTF-8 characters ({len(prompt)})",
        )
    if len(_MULTI_MESSAGE_RE.findall(prompt)) > 1 or len(_MESSAGE_TAG_RE.findall(prompt)) > 1:
        return _reject(
            candidate, "BUILDER_CONTRACT_ERROR", "prompt contains more than one user message"
        )

    if candidate.kind == "replay" and not candidate.prompt_provenance.user_event_ids:
        return _reject(candidate, "NO_STABLE_GOAL", "replay task has no user-event provenance")
    if candidate.kind == "replay":
        non_user_ids = sorted(
            (
                set(candidate.prompt_provenance.user_event_ids)
                & set(facts.event_ids)
            )
            - set(facts.user_event_ids)
        )
        if non_user_ids:
            return _reject(
                candidate,
                "BUILDER_CONTRACT_ERROR",
                f"prompt provenance cites non-user message event: {non_user_ids[0]}",
            )
        if (
            candidate.goal_segment.introduced_event_id in facts.event_ids
            and candidate.goal_segment.introduced_event_id not in facts.user_event_ids
        ):
            return _reject(
                candidate,
                "BUILDER_CONTRACT_ERROR",
                "replay goal was not introduced by a user message event",
            )

    references: list[str] = list(candidate.prompt_provenance.user_event_ids)
    if candidate.goal_segment.introduced_event_id:
        references.append(candidate.goal_segment.introduced_event_id)
    if candidate.goal_segment.closed_event_id:
        references.append(candidate.goal_segment.closed_event_id)
    for reference in references:
        problem = _evidence_missing(reference, facts)
        if problem:
            return _reject(candidate, "BUILDER_CONTRACT_ERROR", problem)
    for evidence in candidate.repo_evidence:
        if not _path_known(evidence.path, facts, evidence.state):
            return _reject(
                candidate,
                "BUILDER_CONTRACT_ERROR",
                f"unknown repository path: repo/{evidence.state}:{evidence.path}",
            )

    if not candidate.base_failure_claim.strip():
        return _reject(candidate, "NO_OBSERVABLE_OUTCOME", "base_failure_claim is empty")
    if not candidate.outcome_success_claim.strip():
        return _reject(candidate, "NO_OBSERVABLE_OUTCOME", "outcome_success_claim is empty")
    if not candidate.scorer_brief.observables:
        return _reject(candidate, "NO_OBSERVABLE_OUTCOME", "scorer brief declares no observables")
    for observable in candidate.scorer_brief.observables:
        if not observable.evidence:
            return _reject(
                candidate,
                "NO_OBSERVABLE_OUTCOME",
                f"observable {observable.id!r} has no evidence source",
            )
        problems = [
            problem
            for reference in observable.evidence
            if (problem := _evidence_missing(reference, facts)) is not None
        ]
        if problems:
            return _reject(
                candidate,
                "NO_OBSERVABLE_OUTCOME",
                f"observable {observable.id!r} has invalid evidence: {problems[0]}",
            )

    if candidate.confidence.state < MIN_STATE_CONFIDENCE:
        return _reject(
            candidate,
            "NO_EXACT_BASE_SHA",
            f"state confidence {candidate.confidence.state} is below {MIN_STATE_CONFIDENCE}",
        )

    leaks = prompt_leakage(prompt, facts, ngram_size=ngram_size)
    if leaks:
        return _reject(
            candidate,
            "PROMPT_ORACLE_LEAKAGE",
            f"prompt repeats oracle added-line material: {leaks[0]!r}",
        )

    if candidate.kind == "adjacent":
        if candidate.adjacency is None:
            return _reject(
                candidate, "BUILDER_CONTRACT_ERROR", "adjacent task has no adjacency record"
            )
        if candidate.adjacency.operator not in ADJACENCY_OPERATORS:
            return _reject(
                candidate,
                "BUILDER_CONTRACT_ERROR",
                f"adjacency operator {candidate.adjacency.operator!r} is not allowlisted",
            )
    elif candidate.adjacency is not None:
        return _reject(
            candidate, "BUILDER_CONTRACT_ERROR", "replay task must not declare adjacency"
        )
    return None


@dataclass(frozen=True)
class RequestLintOutcome:
    """Adapter shape for callers that hand the lint a single request object.

    ``retro.benchmarks.task_scorer.build`` resolves this module's first
    ``lint_task_definitions`` entry point and calls it with a request describing
    the bundle and the raw TaskDefiner document, so accepted tasks are returned
    as plain mappings carrying their canonical ``task_id``.
    """

    accepted: tuple[dict[str, Any], ...]
    rejections: tuple[TaskRejection, ...]
    report: LintReport

    @property
    def findings(self) -> tuple[TaskRejection, ...]:
        return self.rejections


def _is_lint_request(value: Any) -> bool:
    return hasattr(value, "task_definitions") and hasattr(value, "source_dir")


def _lint_request(request: Any, *, ngram_size: int) -> RequestLintOutcome:
    facts = BundleFacts.from_bundle(Path(request.source_dir), ngram_size=ngram_size)
    limits: dict[str, Any] = {}
    max_replay = getattr(request, "max_replay_tasks", None)
    if isinstance(max_replay, int):
        limits["max_replay_tasks"] = max_replay
    adjacent = getattr(request, "adjacent_per_replay", None)
    if isinstance(adjacent, int):
        limits["adjacent_per_replay"] = adjacent
    if limits:
        facts = replace(facts, **limits)
    report = lint_definitions_document(
        dict(request.task_definitions), facts, ngram_size=ngram_size
    )
    accepted = tuple(
        {**task.candidate.to_dict(), "task_id": task.task_id, "source_id": report.source_id}
        for task in report.accepted
    )
    return RequestLintOutcome(
        accepted=accepted, rejections=tuple(report.rejections), report=report
    )


def lint_task_definitions(
    definitions: TaskDefinitions | Any,
    facts: BundleFacts | None = None,
    *,
    ngram_size: int = DEFAULT_NGRAM_SIZE,
) -> Any:
    """Apply every §8.1 check plus task-count and adjacency limits.

    Called as ``lint_task_definitions(definitions, facts)`` this returns a
    :class:`LintReport`. Called with a single request object exposing
    ``task_definitions`` and ``source_dir`` it returns a
    :class:`RequestLintOutcome`.
    """
    if facts is None:
        if not _is_lint_request(definitions):
            raise TypeError("lint_task_definitions requires BundleFacts or a lint request")
        return _lint_request(definitions, ngram_size=ngram_size)
    if not isinstance(definitions, TaskDefinitions):
        raise TypeError("definitions must be a TaskDefinitions instance")
    return _lint_definitions(definitions, facts, ngram_size=ngram_size)


def _lint_definitions(
    definitions: TaskDefinitions,
    facts: BundleFacts,
    *,
    ngram_size: int = DEFAULT_NGRAM_SIZE,
) -> LintReport:
    rejections: list[TaskRejection] = list(definitions.rejections)
    accepted: list[LintedTask] = []
    replay_count = 0
    adjacent_by_parent: dict[str, int] = {}
    accepted_candidate_ids: set[str] = set()

    if definitions.source_id != facts.source_id:
        rejections.append(
            TaskRejection(
                code="BUILDER_CONTRACT_ERROR",
                detail=(
                    f"task definitions source_id {definitions.source_id!r} does not match "
                    f"bundle {facts.source_id!r}"
                ),
            )
        )
        return LintReport(source_id=facts.source_id, accepted=[], rejections=rejections)

    replay_candidates = [task for task in definitions.tasks if task.kind == "replay"]
    adjacent_candidates = [task for task in definitions.tasks if task.kind == "adjacent"]
    max_adjacent = min(facts.adjacent_per_replay, MAX_ADJACENT_PER_REPLAY)

    for candidate in replay_candidates + adjacent_candidates:
        if candidate.kind == "replay" and replay_count >= facts.max_replay_tasks:
            rejections.append(
                _reject(
                    candidate,
                    "BUILDER_CONTRACT_ERROR",
                    f"more than {facts.max_replay_tasks} replay tasks emitted",
                )
            )
            continue
        if candidate.kind == "adjacent":
            if max_adjacent == 0:
                rejections.append(
                    _reject(
                        candidate,
                        "BUILDER_CONTRACT_ERROR",
                        "adjacent generation is disabled for this source",
                    )
                )
                continue
            parent = candidate.adjacency.parent_candidate_id if candidate.adjacency else ""
            if parent not in accepted_candidate_ids:
                rejections.append(
                    _reject(
                        candidate,
                        "BUILDER_CONTRACT_ERROR",
                        f"adjacent task names unknown parent replay task {parent!r}",
                    )
                )
                continue
            if adjacent_by_parent.get(parent, 0) >= max_adjacent:
                rejections.append(
                    _reject(
                        candidate,
                        "BUILDER_CONTRACT_ERROR",
                        f"more than {max_adjacent} adjacent tasks for parent {parent!r}",
                    )
                )
                continue

        rejection = lint_candidate(candidate, facts, ngram_size=ngram_size)
        if rejection is not None:
            rejections.append(rejection)
            continue

        task_id = compute_task_id(
            facts.source_id, facts.base_tree, candidate.kind, candidate.prompt
        )
        if task_id in {task.task_id for task in accepted}:
            rejections.append(
                _reject(
                    candidate,
                    "BUILDER_CONTRACT_ERROR",
                    f"duplicate task after canonicalization: {task_id}",
                )
            )
            continue
        accepted.append(LintedTask(task_id=task_id, candidate=candidate))
        accepted_candidate_ids.add(candidate.candidate_id)
        if candidate.kind == "replay":
            replay_count += 1
        elif candidate.adjacency is not None:
            parent = candidate.adjacency.parent_candidate_id
            adjacent_by_parent[parent] = adjacent_by_parent.get(parent, 0) + 1

    return LintReport(source_id=facts.source_id, accepted=accepted, rejections=rejections)


def lint_definitions_document(
    payload: Any,
    facts: BundleFacts,
    *,
    ngram_size: int = DEFAULT_NGRAM_SIZE,
) -> LintReport:
    """Lint a raw TaskDefiner document against the packaged JSON Schema first.

    Contract violations become per-candidate rejections instead of exceptions so
    one malformed task never discards the rest of a source.
    """
    envelope_errors = _envelope_errors(payload)
    if envelope_errors:
        return LintReport(
            source_id=facts.source_id,
            accepted=[],
            rejections=[
                TaskRejection(
                    code="BUILDER_CONTRACT_ERROR",
                    detail="task-definitions contract violation: " + "; ".join(envelope_errors),
                )
            ],
        )

    document = dict(payload)
    raw_tasks = list(document.get("tasks", []))
    raw_rejections = list(document.get("rejections", []))
    conformant: list[Any] = []
    rejections: list[TaskRejection] = []
    task_schema = load_packaged_schema("task-definitions")
    for index, raw_task in enumerate(raw_tasks):
        errors = json_schema_errors(
            raw_task,
            {"$ref": "#/$defs/task"},
            where=f"$.tasks[{index}]",
            root=task_schema,
        )
        if not errors:
            conformant.append(raw_task)
            continue
        rejections.append(
            TaskRejection(
                code=_conformance_code(errors),
                detail="; ".join(errors),
                goal_event_ids=_goal_event_ids(raw_task),
                candidate_id=_candidate_id(raw_task),
            )
        )

    normalized_rejections: list[dict[str, Any]] = []
    for raw_rejection in raw_rejections:
        if not isinstance(raw_rejection, Mapping):
            continue
        code = raw_rejection.get("code")
        if code not in REJECTION_CODES:
            code = "NO_OBSERVABLE_OUTCOME"
        normalized_rejections.append(
            {
                "goal_event_ids": [
                    str(item)
                    for item in raw_rejection.get("goal_event_ids", [])
                    if isinstance(item, str)
                ],
                "code": code,
                "detail": str(raw_rejection.get("detail") or "TaskDefiner rejected this goal."),
            }
        )
    document["tasks"] = conformant
    document["rejections"] = normalized_rejections
    try:
        definitions = TaskDefinitions.from_dict(document)
    except SchemaError as error:
        rejections.append(TaskRejection(code="BUILDER_CONTRACT_ERROR", detail=str(error)))
        return LintReport(source_id=facts.source_id, accepted=[], rejections=rejections)

    report = _lint_definitions(definitions, facts, ngram_size=ngram_size)
    return LintReport(
        source_id=report.source_id,
        accepted=report.accepted,
        rejections=rejections + report.rejections,
    )


def _envelope_errors(payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["$ must be of type object"]
    envelope = dict(payload)
    task_count = len(envelope["tasks"]) if isinstance(envelope.get("tasks"), list) else 0
    envelope["tasks"] = []
    errors = packaged_schema_errors(envelope, "task-definitions")
    if task_count > MAX_TASKS_PER_SOURCE:
        errors.append(f"$.tasks must contain at most {MAX_TASKS_PER_SOURCE} items")
    return errors


def _conformance_code(errors: list[str]) -> str:
    if any("scorer_brief.observables" in error for error in errors):
        return "NO_OBSERVABLE_OUTCOME"
    return "BUILDER_CONTRACT_ERROR"


def _goal_event_ids(raw_task: Any) -> list[str]:
    if not isinstance(raw_task, Mapping):
        return []
    segment = raw_task.get("goal_segment")
    if isinstance(segment, Mapping) and isinstance(segment.get("introduced_event_id"), str):
        return [segment["introduced_event_id"]]
    return []


def _candidate_id(raw_task: Any) -> str | None:
    if isinstance(raw_task, Mapping) and isinstance(raw_task.get("candidate_id"), str):
        return raw_task["candidate_id"]
    return None


__all__ = [
    "DEFAULT_NGRAM_SIZE",
    "MIN_STATE_CONFIDENCE",
    "BundleFacts",
    "LintReport",
    "LintedTask",
    "RequestLintOutcome",
    "compute_task_id",
    "lint_candidate",
    "lint_definitions_document",
    "lint_task_definitions",
    "normalize_prompt",
    "oracle_ngrams",
    "prompt_leakage",
]
