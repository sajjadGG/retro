"""Time-consistent task generation and file-localization evaluation.

The method adapts arXiv:2603.26137 to Retro's normalized rollout archive:
post-cutoff user goal episodes replace pull requests, while the files edited in
each episode provide the hidden file-level ground truth.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from ..locking import exclusive_lock
from ..schema import HOSTS, Host, NormalizedEvent, read_events
from ..storage import Layout
from ..utils import event_command_text, event_file_paths, event_text
from .metrics import (
    FileLocalizationMetrics,
    aggregate_file_localization,
    file_localization_metrics,
)

PromptLevel = Literal["minimal", "concise", "contextual", "guided"]

METHOD_NAME = "time_consistent_file_localization_v1"
PROMPT_LEVELS: tuple[PromptLevel, ...] = (
    "minimal",
    "concise",
    "contextual",
    "guided",
)
SOURCE_PROMPT_LEVEL = "source"
SCHEMA_VERSION = 2
PREDICTION_SCHEMA_VERSION = 1
PAPER_TITLE = "A Time-Consistent Benchmark for Repository-Level Software Engineering Evaluation"
PAPER_URL = "https://arxiv.org/abs/2603.26137"

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_INSPECTION_COMMAND_RE = re.compile(
    r"(?:^|(?:&&|\|\||[;|])\s*)"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:cat|sed|rg|grep|find|fd|ls|head|tail|less|tree|wc|"
    r"git\s+(?:diff|show|status|grep|ls-files))\b"
)
_ACK_RE = re.compile(
    r"^(?:yes|no|ok(?:ay)?|sure|continue|go ahead|do it|thanks?|looks good|"
    r"approved|proceed)[.! ]*$",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password)\b(\s*[:=]\s*)(\S+)"
)
_URL_RE = re.compile(r"https?://\S+")
_ABSOLUTE_HOME_RE = re.compile(r"(?:/Users|/home)/[^/\s]+/")
_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_AUTOMATION_NAME_RE = re.compile(
    r"(?i)\bAutomation:\s*.*?(?=\s+Automation ID:)"
)
_AUTOMATION_ID_RE = re.compile(r"(?i)\bAutomation ID:\s*\S+")
_AUTOMATION_MEMORY_RE = re.compile(r"(?i)\bAutomation memory:\s*\S+")
_AUTOMATION_LAST_RUN_RE = re.compile(
    r"(?i)\bLast run:\s*.*?(?:\(\d+\)|(?=\s+(?:update|review|critique|make)\b))"
)
_DELEGATION_INPUT_RE = re.compile(
    r"<input>(?P<input>.*?)</input>",
    re.DOTALL | re.IGNORECASE,
)
_DELIVERY_LOGISTICS_RE = re.compile(
    r"(?i)\b(?:create (?:a |the )?branch|commit (?:the |your )?(?:changes|"
    r"improvements)|push (?:it|the branch)|open (?:a |the )?pull request)"
    r"[^.\n]*(?:\.|$)"
)
_LATEST_BRANCH_RE = re.compile(
    r"(?i)\b(?:update|pull) from (?:the )?latest [`'\"]?(?:main|master)[`'\"]?"
    r"(?:\s+branch)?(?:,\s*then)?\s*"
)
_ACTION_RE = re.compile(
    r"\b(?:add(?:ed|ing|s)?|build(?:ing|s)?|chang(?:e|ed|es|ing)|"
    r"creat(?:e|ed|es|ing)|debug(?:ged|ging|s)?|document(?:ed|ing|s)?|"
    r"find(?:ing|s)?|fix(?:ed|es|ing)?|generat(?:e|ed|es|ing)|"
    r"implement(?:ed|ing|s)?|improv(?:e|ed|es|ing|ements?)|"
    r"integrat(?:e|ed|es|ing)|investigat(?:e|ed|es|ing)|"
    r"migrat(?:e|ed|es|ing)|refactor(?:ed|ing|s)?|remov(?:e|ed|es|ing)|"
    r"renam(?:e|ed|es|ing)|research(?:ed|ing|es)?|review(?:ed|ing|s)?|"
    r"support(?:ed|ing|s)?|updat(?:e|ed|es|ing)|writ(?:e|es|ing|ten))\b",
    re.IGNORECASE,
)
_PRIMARY_ACTION_RE = re.compile(
    r"\b(?:add(?:ed|ing|s)?|creat(?:e|ed|es|ing)|document(?:ed|ing|s)?|"
    r"fix(?:ed|es|ing)?|implement(?:ed|ing|s)?|migrat(?:e|ed|es|ing)|"
    r"refactor(?:ed|ing|s)?|remov(?:e|ed|es|ing)|renam(?:e|ed|es|ing)|"
    r"updat(?:e|ed|es|ing)|writ(?:e|es|ing|ten))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BenchmarkBuildResult:
    benchmark_id: str
    path: Path
    task_count: int
    snapshot_commit: str
    observed_predictions_path: Path
    rejected_episode_counts: dict[str, int]


@dataclass(frozen=True)
class BenchmarkEvaluationResult:
    benchmark_id: str
    run_id: str
    path: Path
    aggregate: list[dict[str, Any]]
    paired_comparisons: list[dict[str, Any]]


@dataclass(frozen=True)
class _Repository:
    root: Path
    common_dir: Path
    worktrees_dir: Path
    head_commit: str
    snapshot_commit: str
    snapshot_tree: str
    snapshot_committed_at: str


@dataclass(frozen=True)
class _Task:
    task_id: str
    host: Host
    session_id: str
    goal_event_id: str
    started_at: str
    ended_at: str
    source_artifact: str
    source_event_ids: tuple[str, ...]
    prompts: dict[str, str]
    task_family: str
    component: str
    expected_files: tuple[str, ...]
    observed_files: tuple[str, ...]


def parse_timestamp(value: str) -> datetime:
    """Parse an explicit timezone-aware timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def build_time_consistent_benchmark(
    layout: Layout,
    *,
    benchmark_id: str,
    project_root: Path,
    cutoff_time: str,
    end_time: str,
    hosts: Sequence[Host] = HOSTS,
) -> BenchmarkBuildResult:
    """Build an immutable rollout-derived localization benchmark."""
    _validate_identifier(benchmark_id, "benchmark id")
    cutoff = parse_timestamp(cutoff_time)
    end = parse_timestamp(end_time)
    if cutoff >= end:
        raise ValueError("cutoff_time must be earlier than end_time")
    if not hosts:
        raise ValueError("at least one host is required")
    unknown_hosts = sorted(set(hosts) - set(HOSTS))
    if unknown_hosts:
        raise ValueError(f"unknown hosts: {unknown_hosts}")

    layout.ensure()
    repository = _load_repository(project_root, cutoff)
    target = layout.benchmark_dir(benchmark_id)
    if target.exists():
        raise FileExistsError(f"benchmark already exists: {target}")

    tasks: list[_Task] = []
    knowledge_refs: list[dict[str, Any]] = []
    rejection_counts: Counter[str] = Counter()
    source_sessions = 0
    source_events = 0
    session_root_cache: dict[str, Path | None] = {}

    for host in hosts:
        normalized_dir = layout.root / "normalized" / host
        if not normalized_dir.is_dir():
            continue
        for path in sorted(normalized_dir.glob("*.events.jsonl")):
            loaded = _load_matching_session(path, repository, session_root_cache)
            if loaded is None:
                continue
            events, session_cwd, session_root = loaded
            source_sessions += 1
            source_events += len(events)
            artifact = path.relative_to(layout.root).as_posix()
            for event in events:
                timestamp = _event_timestamp(event)
                if timestamp is not None and timestamp <= cutoff:
                    knowledge_refs.append(
                        {
                            "event_id": event.event_id,
                            "host": event.host,
                            "normalized_artifact": artifact,
                            "session_id": event.session_id,
                            "timestamp": _format_timestamp(timestamp),
                        }
                    )
            tasks.extend(
                _extract_tasks(
                    events,
                    source_artifact=artifact,
                    canonical_root=repository.root,
                    session_cwd=session_cwd,
                    session_root=session_root,
                    snapshot_commit=repository.snapshot_commit,
                    cutoff=cutoff,
                    end=end,
                    rejection_counts=rejection_counts,
                )
            )

    tasks.sort(key=lambda task: (task.started_at, task.host, task.session_id, task.goal_event_id))
    if not tasks:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(rejection_counts.items()))
        raise RuntimeError(
            "no benchmark tasks had post-cutoff goals and extractable file-edit ground truth"
            + (f" ({summary})" if summary else "")
        )

    with exclusive_lock(layout.benchmarks_dir() / ".build.lock"):
        if target.exists():
            raise FileExistsError(f"benchmark already exists: {target}")
        staging = Path(
            tempfile.mkdtemp(prefix=f".{benchmark_id}.", dir=layout.benchmarks_dir())
        )
        try:
            _write_benchmark(
                staging,
                layout=layout,
                benchmark_id=benchmark_id,
                repository=repository,
                cutoff=cutoff,
                end=end,
                tasks=tasks,
                knowledge_refs=knowledge_refs,
                rejection_counts=rejection_counts,
                source_sessions=source_sessions,
                source_events=source_events,
            )
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    return BenchmarkBuildResult(
        benchmark_id=benchmark_id,
        path=target,
        task_count=len(tasks),
        snapshot_commit=repository.snapshot_commit,
        observed_predictions_path=target
        / "private"
        / "historical-localization-predictions.jsonl",
        rejected_episode_counts=dict(sorted(rejection_counts.items())),
    )


def evaluate_time_consistent_benchmark(
    layout: Layout,
    *,
    benchmark_id: str,
    predictions_path: Path,
    run_id: str | None = None,
    allow_partial: bool = False,
    baseline_condition: str = "baseline",
    augmented_condition: str = "augmented",
) -> BenchmarkEvaluationResult:
    """Evaluate predicted file sets and persist an immutable run."""
    _validate_identifier(benchmark_id, "benchmark id")
    benchmark_dir = layout.benchmark_dir(benchmark_id)
    manifest = _load_json(benchmark_dir / "manifest.json")
    _validate_manifest(manifest, benchmark_id)
    _verify_artifacts(benchmark_dir, manifest)

    task_ids = _load_prompt_task_ids(benchmark_dir)
    ground_truth = {
        record["task_id"]: tuple(record["expected_files"])
        for record in _read_jsonl_strict(
            benchmark_dir / "private" / "ground-truth.jsonl"
        )
    }
    if task_ids != set(ground_truth):
        raise RuntimeError("prompt tasks and private ground truth do not contain the same task ids")

    prediction_records = _read_jsonl_strict(predictions_path.expanduser().resolve())
    normalized_predictions: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    group_task_ids: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    task_rows: list[dict[str, Any]] = []
    private_rows: list[dict[str, Any]] = []

    for line_number, record in enumerate(prediction_records, start=1):
        prediction = _validate_prediction(
            record,
            line_number=line_number,
            task_ids=task_ids,
            repository_root=Path(manifest["repository"]["local_path"]),
        )
        key = (
            prediction["condition"],
            prediction["model"],
            prediction["prompt_level"],
            prediction["task_id"],
        )
        if key in seen_keys:
            raise ValueError(f"duplicate prediction key at line {line_number}: {key}")
        seen_keys.add(key)
        group = key[:3]
        group_task_ids[group].add(prediction["task_id"])
        normalized_predictions.append(prediction)

        expected_files = ground_truth[prediction["task_id"]]
        predicted_files = tuple(prediction["predicted_files"])
        metrics = file_localization_metrics(expected_files, predicted_files)
        task_rows.append(
            {
                "condition": prediction["condition"],
                "metrics": metrics.to_dict(),
                "model": prediction["model"],
                "prompt_level": prediction["prompt_level"],
                "task_id": prediction["task_id"],
            }
        )
        expected = set(expected_files)
        predicted = set(predicted_files)
        private_rows.append(
            {
                "condition": prediction["condition"],
                "expected_files": sorted(expected),
                "false_negative_files": sorted(expected - predicted),
                "false_positive_files": sorted(predicted - expected),
                "model": prediction["model"],
                "predicted_files": sorted(predicted),
                "prompt_level": prediction["prompt_level"],
                "task_id": prediction["task_id"],
                "true_positive_files": sorted(expected & predicted),
            }
        )

    if not normalized_predictions:
        raise ValueError("predictions file contains no records")

    all_task_ids = task_ids
    if not allow_partial:
        for group, predicted_task_ids in sorted(group_task_ids.items()):
            missing = sorted(all_task_ids - predicted_task_ids)
            if missing:
                raise ValueError(
                    f"prediction group {group} is missing {len(missing)} task(s), "
                    "or pass allow_partial=True"
                )

    aggregate = _aggregate_rows(task_rows)
    paired_comparisons = _paired_comparisons(
        task_rows,
        baseline_condition=baseline_condition,
        augmented_condition=augmented_condition,
    )
    resolved_run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _validate_identifier(resolved_run_id, "run id")
    run_target = layout.benchmark_runs_dir(benchmark_id) / resolved_run_id
    if run_target.exists():
        raise FileExistsError(f"benchmark run already exists: {run_target}")

    results = {
        "aggregate": aggregate,
        "benchmark_id": benchmark_id,
        "completed_at": _format_timestamp(datetime.now(timezone.utc)),
        "method": METHOD_NAME,
        "paired_comparisons": paired_comparisons,
        "paper": {"title": PAPER_TITLE, "url": PAPER_URL},
        "protocol": {
            "allow_partial": allow_partial,
            "augmented_condition": augmented_condition,
            "baseline_condition": baseline_condition,
            "matched_variables": [
                "task_id",
                "model",
                "prompt_level",
                "repository_snapshot",
                "file_set_metric",
            ],
            "score": "unweighted mean task-level file F1",
        },
        "run_id": resolved_run_id,
        "schema_version": SCHEMA_VERSION,
        "tasks": task_rows,
    }

    run_parent = layout.benchmark_runs_dir(benchmark_id)
    run_parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(run_parent / ".record.lock"):
        if run_target.exists():
            raise FileExistsError(f"benchmark run already exists: {run_target}")
        staging = Path(
            tempfile.mkdtemp(prefix=f".{resolved_run_id}.", dir=run_parent)
        )
        try:
            _write_jsonl(staging / "predictions.jsonl", normalized_predictions)
            private_path = staging / "private" / "task-details.jsonl"
            _write_jsonl(private_path, private_rows)
            _protect_private_file(private_path)
            _write_json(staging / "results.json", results)
            _write_evaluation_report(staging / "report.md", results)
            artifacts = _artifact_records(
                staging,
                (
                    ("predictions.jsonl", len(normalized_predictions), "evaluator"),
                    ("private/task-details.jsonl", len(private_rows), "private"),
                    ("report.md", None, "evaluator"),
                    ("results.json", None, "evaluator"),
                ),
            )
            _write_json(
                staging / "run-manifest.json",
                {
                    "artifacts": artifacts,
                    "benchmark_id": benchmark_id,
                    "method": METHOD_NAME,
                    "recorded_at": _format_timestamp(datetime.now(timezone.utc)),
                    "run_id": resolved_run_id,
                    "schema_version": SCHEMA_VERSION,
                },
            )
            os.replace(staging, run_target)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    return BenchmarkEvaluationResult(
        benchmark_id=benchmark_id,
        run_id=resolved_run_id,
        path=run_target,
        aggregate=aggregate,
        paired_comparisons=paired_comparisons,
    )


def _load_repository(project_root: Path, cutoff: datetime) -> _Repository:
    requested = project_root.expanduser().resolve()
    root = Path(_git(requested, "rev-parse", "--show-toplevel")).resolve()
    common_dir_raw = Path(_git(root, "rev-parse", "--git-common-dir"))
    common_dir = (
        common_dir_raw.resolve()
        if common_dir_raw.is_absolute()
        else (root / common_dir_raw).resolve()
    )
    snapshot_commit = _git(
        root,
        "rev-list",
        "-1",
        f"--before={_format_timestamp(cutoff)}",
        "HEAD",
    )
    if not snapshot_commit:
        raise RuntimeError("repository has no commit at or before the cutoff")
    return _Repository(
        root=root,
        common_dir=common_dir,
        worktrees_dir=root.parent / f"{root.name}.worktrees",
        head_commit=_git(root, "rev-parse", "HEAD"),
        snapshot_commit=snapshot_commit,
        snapshot_tree=_git(root, "rev-parse", f"{snapshot_commit}^{{tree}}"),
        snapshot_committed_at=_git(root, "show", "-s", "--format=%cI", snapshot_commit),
    )


def _load_matching_session(
    path: Path,
    repository: _Repository,
    cache: dict[str, Path | None],
) -> tuple[list[NormalizedEvent], Path, Path] | None:
    iterator = iter(read_events(path))
    prefix = list(islice(iterator, 20))
    cwd_value = _session_cwd(prefix)
    if cwd_value is None:
        return None
    cached = cache.get(cwd_value)
    if cwd_value not in cache:
        cached = _matching_session_root(Path(cwd_value).expanduser(), repository)
        cache[cwd_value] = cached
    if cached is None:
        return None
    events = prefix + list(iterator)
    return events, Path(cwd_value).expanduser(), cached


def _matching_session_root(cwd: Path, repository: _Repository) -> Path | None:
    resolved = cwd.resolve()
    if _is_within(resolved, repository.root):
        return repository.root
    if _is_within(resolved, repository.worktrees_dir):
        relative = resolved.relative_to(repository.worktrees_dir)
        if relative.parts:
            return repository.worktrees_dir / relative.parts[0]
    if not resolved.exists():
        return None

    process = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        return None
    lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    session_root = Path(lines[0]).resolve()
    common_raw = Path(lines[1])
    common = common_raw.resolve() if common_raw.is_absolute() else (resolved / common_raw).resolve()
    return session_root if common == repository.common_dir else None


def _extract_tasks(
    events: list[NormalizedEvent],
    *,
    source_artifact: str,
    canonical_root: Path,
    session_cwd: Path,
    session_root: Path,
    snapshot_commit: str,
    cutoff: datetime,
    end: datetime,
    rejection_counts: Counter[str],
) -> list[_Task]:
    tasks: list[_Task] = []
    user_indexes = [
        index
        for index, event in enumerate(events)
        if event.actor == "user" and event.event_type == "message"
    ]
    for position, start_index in enumerate(user_indexes):
        goal_event = events[start_index]
        started_at = _event_timestamp(goal_event)
        if started_at is None:
            rejection_counts["untimestamped_goal"] += 1
            continue
        if started_at <= cutoff:
            rejection_counts["at_or_before_cutoff"] += 1
            continue
        if started_at > end:
            rejection_counts["after_end"] += 1
            continue

        source_goal = event_text(goal_event).strip()
        normalized_goal = _normalize_text(source_goal)
        if len(normalized_goal) < 12 or _ACK_RE.fullmatch(normalized_goal):
            rejection_counts["non_task_goal"] += 1
            continue

        next_index = (
            user_indexes[position + 1]
            if position + 1 < len(user_indexes)
            else len(events)
        )
        episode = events[start_index + 1 : next_index]
        edit_events = _successful_file_edits(episode)
        if not edit_events:
            rejection_counts["no_file_edit"] += 1
            continue
        edit_times = [_event_timestamp(event) for event in edit_events]
        if any(timestamp is None for timestamp in edit_times):
            rejection_counts["untimestamped_file_edit"] += 1
            continue
        if any(cast(datetime, timestamp) > end for timestamp in edit_times):
            rejection_counts["episode_crosses_end"] += 1
            continue

        expected_files = _normalized_event_paths(
            edit_events,
            canonical_root=canonical_root,
            session_cwd=session_cwd,
            session_root=session_root,
        )
        if not expected_files:
            rejection_counts["unextractable_file_edit"] += 1
            continue

        first_edit = min(index for index, event in enumerate(episode) if event.event_type == "file_edit")
        observed_events = [
            event
            for event in episode[:first_edit]
            if event.event_type == "file_read"
            or (
                event.event_type == "command"
                and _INSPECTION_COMMAND_RE.search(event_command_text(event))
            )
        ]
        observed_files = _normalized_event_paths(
            observed_events,
            canonical_root=canonical_root,
            session_cwd=session_cwd,
            session_root=session_root,
            include_command_candidates=True,
        )
        component = _component_label(expected_files)
        prompts = _prompt_variants(source_goal, expected_files, component)
        _assert_no_file_leakage(prompts, expected_files)
        ended_at = max(cast(datetime, timestamp) for timestamp in edit_times)
        edit_event_ids = tuple(event.event_id for event in edit_events)
        task_id = _task_id(
            snapshot_commit,
            goal_event.host,
            goal_event.session_id,
            goal_event.event_id,
        )
        tasks.append(
            _Task(
                task_id=task_id,
                host=goal_event.host,
                session_id=goal_event.session_id,
                goal_event_id=goal_event.event_id,
                started_at=_format_timestamp(started_at),
                ended_at=_format_timestamp(ended_at),
                source_artifact=source_artifact,
                source_event_ids=(goal_event.event_id, *edit_event_ids),
                prompts=prompts,
                task_family=_task_family(normalized_goal, expected_files),
                component=component,
                expected_files=tuple(expected_files),
                observed_files=tuple(observed_files),
            )
        )
    return tasks


def _normalized_event_paths(
    events: Sequence[NormalizedEvent],
    *,
    canonical_root: Path,
    session_cwd: Path,
    session_root: Path,
    include_command_candidates: bool = False,
) -> list[str]:
    paths: set[str] = set()
    for event in events:
        for raw_path in event_file_paths(
            event,
            include_command_candidates=include_command_candidates,
        ):
            relative_base = session_cwd if event.event_type == "command" else session_root
            normalized = _normalize_repo_path(
                raw_path,
                canonical_root=canonical_root,
                relative_base=relative_base,
                session_root=session_root,
            )
            if normalized is not None:
                if include_command_candidates and not _plausible_observed_file(
                    normalized,
                    session_root,
                ):
                    continue
                paths.add(normalized)
    return sorted(paths)


def _successful_file_edits(
    episode: Sequence[NormalizedEvent],
) -> list[NormalizedEvent]:
    outcomes: dict[str, bool] = {}
    for event in episode:
        success = _explicit_event_success(event)
        if success is None:
            continue
        for identifier in _event_call_ids(event):
            outcomes[identifier] = success
        if event.parent_event_id:
            outcomes[event.parent_event_id] = success

    edits: list[NormalizedEvent] = []
    for event in episode:
        if event.event_type != "file_edit":
            continue
        success = _explicit_event_success(event)
        if success is False or outcomes.get(event.event_id) is False:
            continue
        if any(outcomes.get(identifier) is False for identifier in _event_call_ids(event)):
            continue
        edits.append(event)
    return edits


def _explicit_event_success(event: NormalizedEvent) -> bool | None:
    payload = event.payload or {}
    success = payload.get("success")
    if isinstance(success, bool):
        return success
    is_error = payload.get("is_error")
    if isinstance(is_error, bool):
        return not is_error
    return None


def _event_call_ids(event: NormalizedEvent) -> set[str]:
    payload = event.payload or {}
    identifiers: set[str] = set()
    for key in (
        "call_id",
        "tool_id",
        "tool_use_id",
        "toolCallId",
        "parentToolCallId",
    ):
        value = payload.get(key)
        if isinstance(value, str) and value:
            identifiers.add(value)
    return identifiers


def _normalize_repo_path(
    raw_path: str,
    *,
    canonical_root: Path,
    relative_base: Path,
    session_root: Path,
) -> str | None:
    value = raw_path.strip().strip("'\"`")
    value = re.sub(r":\d+(?::\d+)?$", "", value)
    value = value.replace("\\", "/")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value or value in {".", "..", "/dev/null"}:
        return None
    if any(character in value for character in "\n\r\t*?[]{}|<>"):
        return None

    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
        for root in (session_root.resolve(), canonical_root.resolve()):
            if _is_within(resolved, root):
                relative = resolved.relative_to(root)
                return _clean_relative_path(relative)
        return None

    resolved = (relative_base.resolve() / path).resolve()
    if _is_within(resolved, session_root.resolve()):
        return _clean_relative_path(resolved.relative_to(session_root.resolve()))

    pure = PurePosixPath(value)
    if ".." in pure.parts:
        return None
    return _clean_relative_path(Path(*pure.parts))


def _clean_relative_path(path: Path) -> str | None:
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or ".." in parts or parts[0] in {".git", "node_modules"}:
        return None
    return PurePosixPath(*parts).as_posix()


def _prompt_variants(
    goal: str,
    expected_files: Sequence[str],
    component: str,
) -> dict[str, str]:
    safe_goal = _redact_task_text(goal, expected_files)
    contextual_goal = _normalize_text(_INLINE_CODE_RE.sub("[implementation detail]", safe_goal))
    minimal = _minimal_prompt(contextual_goal)
    concise = _join_prompt(
        minimal,
        f"Focus on the {component} component without assuming a specific implementation.",
        limit=360,
    )
    contextual_source = (
        contextual_goal
        if contextual_goal.startswith(minimal)
        else f"{minimal} Context: {contextual_goal}"
    )
    contextual = _join_prompt(
        _truncate(contextual_source, 1000),
        f"The affected area is the {component} component. Preserve adjacent behavior.",
        limit=1250,
    )
    change_shape = _change_shape(expected_files)
    guided_source = safe_goal if safe_goal.startswith(minimal) else f"{minimal} Context: {safe_goal}"
    guided = _join_prompt(
        _truncate(guided_source, 1800),
        (
            f"Investigate the {component} component. The task likely spans {change_shape}; "
            "trace the existing contracts, make the smallest coherent change, and validate "
            "relevant regression behavior."
        ),
        limit=2300,
    )
    return {
        "minimal": minimal,
        "concise": concise,
        "contextual": contextual,
        "guided": guided,
    }


def _redact_task_text(text: str, expected_files: Sequence[str]) -> str:
    delegated = _DELEGATION_INPUT_RE.search(text)
    if delegated:
        text = delegated.group("input")
    redacted = _FENCED_CODE_RE.sub("[code omitted]", text)
    redacted = _AUTOMATION_NAME_RE.sub("", redacted)
    redacted = _AUTOMATION_ID_RE.sub("", redacted)
    redacted = _AUTOMATION_MEMORY_RE.sub("", redacted)
    redacted = _AUTOMATION_LAST_RUN_RE.sub("", redacted)
    redacted = _DELIVERY_LOGISTICS_RE.sub("", redacted)
    redacted = _LATEST_BRANCH_RE.sub("", redacted)
    redacted = _URL_RE.sub("[url]", redacted)
    redacted = _ABSOLUTE_HOME_RE.sub("[home]/", redacted)
    redacted = _SECRET_RE.sub(r"\1\2[redacted]", redacted)
    leakage_terms: set[str] = set()
    for path in expected_files:
        leakage_terms.add(path)
        name = PurePosixPath(path).name
        if len(name) >= 3:
            leakage_terms.add(name)
    for term in sorted(leakage_terms, key=len, reverse=True):
        redacted = re.sub(re.escape(term), "[affected file]", redacted, flags=re.IGNORECASE)
    return _normalize_text(redacted)


def _assert_no_file_leakage(
    prompts: dict[str, str],
    expected_files: Sequence[str],
) -> None:
    terms = {
        term.casefold()
        for path in expected_files
        for term in (path, PurePosixPath(path).name)
        if len(term) >= 3
    }
    for level, prompt in prompts.items():
        folded = prompt.casefold()
        leaked = sorted(term for term in terms if term in folded)
        if leaked:
            raise RuntimeError(f"{level} prompt leaked ground-truth file names: {leaked}")


def _component_label(paths: Sequence[str]) -> str:
    labels: set[str] = set()
    generic = {"src", "lib", "app", "apps", "packages", "test", "tests"}
    for value in paths:
        parts = PurePosixPath(value).parts
        if not parts:
            continue
        if parts[0] == "docs" or parts[-1].lower().startswith("readme"):
            labels.add("documentation")
            continue
        directories = parts[:-1]
        selected = next((part for part in directories if part not in generic), None)
        labels.add(selected or (directories[-1] if directories else "repository root"))
    if len(labels) == 1:
        return next(iter(labels)).replace("_", " ").replace("-", " ")
    return "multiple repository areas"


def _change_shape(paths: Sequence[str]) -> str:
    has_tests = any(_is_test_path(path) for path in paths)
    has_docs = any(_is_doc_path(path) for path in paths)
    has_code = any(not _is_test_path(path) and not _is_doc_path(path) for path in paths)
    labels = [
        label
        for present, label in (
            (has_code, "implementation"),
            (has_tests, "tests"),
            (has_docs, "documentation"),
        )
        if present
    ]
    return " and ".join(labels) if labels else "repository artifacts"


def _task_family(goal: str, paths: Sequence[str]) -> str:
    lowered = goal.casefold()
    if all(_is_doc_path(path) for path in paths):
        return "documentation"
    if all(_is_test_path(path) for path in paths):
        return "test_maintenance"
    if re.search(
        r"\b(?:dependenc|package|lockfile|upgrade|bump|install|version)\w*\b",
        lowered,
    ) and any(
        PurePosixPath(path).name
        in {"pyproject.toml", "package.json", "package-lock.json", "uv.lock", "poetry.lock"}
        for path in paths
    ):
        return "dependency_maintenance"
    if re.search(r"\b(?:fix|bug|broken|regression|error|fail)\b", lowered):
        return "regression_fix"
    if re.search(r"\b(?:refactor|simplif|cleanup|reorganize|rename)\w*\b", lowered):
        return "refactoring"
    return "repository_feature"


def _write_benchmark(
    staging: Path,
    *,
    layout: Layout,
    benchmark_id: str,
    repository: _Repository,
    cutoff: datetime,
    end: datetime,
    tasks: Sequence[_Task],
    knowledge_refs: Sequence[dict[str, Any]],
    rejection_counts: Counter[str],
    source_sessions: int,
    source_events: int,
) -> None:
    prompt_records: dict[PromptLevel, list[dict[str, Any]]] = {
        level: [] for level in PROMPT_LEVELS
    }
    private_records: list[dict[str, Any]] = []
    observed_predictions: list[dict[str, Any]] = []
    for task in tasks:
        truth_fingerprint = _fingerprint("\n".join(task.expected_files))
        for level in PROMPT_LEVELS:
            prompt_records[level].append(
                {
                    "instruction": task.prompts[level],
                    "prompt_level": level,
                    "schema_version": SCHEMA_VERSION,
                    "snapshot_commit": repository.snapshot_commit,
                    "task_id": task.task_id,
                }
            )
        private_records.append(
            {
                "component": task.component,
                "expected_files": list(task.expected_files),
                "ground_truth_fingerprint": truth_fingerprint,
                "host": task.host,
                "prompt_fingerprints": {
                    level: _fingerprint(task.prompts[level])
                    for level in PROMPT_LEVELS
                },
                "schema_version": SCHEMA_VERSION,
                "session_id": task.session_id,
                "source_evidence_ids": list(task.source_event_ids),
                "source_goal_event_id": task.goal_event_id,
                "source_normalized_artifact": task.source_artifact,
                "started_at": task.started_at,
                "task_family": task.task_family,
                "task_id": task.task_id,
            }
        )
        observed_predictions.append(
            {
                "condition": "historical_observed",
                "metadata": {
                    "diagnostic_only": True,
                    "host": task.host,
                    "source_goal_event_id": task.goal_event_id,
                    "warning": (
                        "Derived from pre-edit inspection events in the captured rollout; "
                        "the agent saw the source prompt, not a generated prompt variant."
                    ),
                },
                "model": "captured-agent",
                "predicted_files": list(task.observed_files),
                "prompt_level": SOURCE_PROMPT_LEVEL,
                "schema_version": PREDICTION_SCHEMA_VERSION,
                "task_id": task.task_id,
            }
        )

    for level, records in prompt_records.items():
        _write_jsonl(staging / "tasks" / "prompts" / f"{level}.jsonl", records)
    private_path = staging / "private" / "ground-truth.jsonl"
    _write_jsonl(private_path, private_records)
    _protect_private_file(private_path)
    _write_jsonl(
        staging / "knowledge" / "eligible-event-refs.jsonl",
        sorted(knowledge_refs, key=lambda item: (item["timestamp"], item["event_id"])),
    )
    observed_path = staging / "private" / "historical-localization-predictions.jsonl"
    _write_jsonl(observed_path, observed_predictions)
    _protect_private_file(observed_path)
    _write_benchmark_report(
        staging / "reports" / "benchmark-card.md",
        benchmark_id=benchmark_id,
        repository=repository,
        cutoff=cutoff,
        end=end,
        tasks=tasks,
        rejection_counts=rejection_counts,
    )

    artifact_specs: list[tuple[str, int | None, str]] = [
        (
            f"tasks/prompts/{level}.jsonl",
            len(prompt_records[level]),
            f"learner:{level}",
        )
        for level in PROMPT_LEVELS
    ]
    artifact_specs.extend(
        [
            ("private/ground-truth.jsonl", len(private_records), "private"),
            ("knowledge/eligible-event-refs.jsonl", len(knowledge_refs), "evaluator"),
            (
                "private/historical-localization-predictions.jsonl",
                len(observed_predictions),
                "private",
            ),
            ("reports/benchmark-card.md", None, "evaluator"),
        ]
    )
    artifacts = _artifact_records(staging, artifact_specs)
    family_counts = Counter(task.task_family for task in tasks)
    manifest = {
        "artifacts": artifacts,
        "benchmark_id": benchmark_id,
        "created_at": _format_timestamp(datetime.now(timezone.utc)),
        "default_prompt_level": "contextual",
        "method": METHOD_NAME,
        "paper": {
            "adaptation": (
                "Historical rollout goal episodes replace merged pull requests; file-edit "
                "events replace PR-modified-file ground truth."
            ),
            "title": PAPER_TITLE,
            "url": PAPER_URL,
        },
        "prompt_generation": {
            "levels": list(PROMPT_LEVELS),
            "method": "deterministic_leakage_filtered_v1",
            "source_level": SOURCE_PROMPT_LEVEL,
        },
        "quality": {
            "ground_truth_file_leakage_checks": "passed",
            "immutable_publication": True,
            "task_family_counts": dict(sorted(family_counts.items())),
        },
        "repository": {
            "head_at_generation": repository.head_commit,
            "local_path": str(repository.root),
            "snapshot_commit": repository.snapshot_commit,
            "snapshot_committed_at": repository.snapshot_committed_at,
            "snapshot_tree": repository.snapshot_tree,
        },
        "schema_version": SCHEMA_VERSION,
        "source": {
            "eligible_knowledge_event_count": len(knowledge_refs),
            "matched_event_count": source_events,
            "matched_session_count": source_sessions,
            "rejected_episode_counts": dict(sorted(rejection_counts.items())),
            "task_count": len(tasks),
            "task_source": "normalized_rollout_goal_episode",
        },
        "temporal_contract": {
            "end_inclusive": _format_timestamp(end),
            "knowledge_and_snapshot_at_or_before": _format_timestamp(cutoff),
            "task_interval": "(cutoff, end]",
        },
        "limitations": [
            "The metric evaluates file localization, not patch correctness or test outcomes.",
            "Rollout file-edit events are observational ground truth and may omit external edits.",
            "Historical-observed predictions are diagnostics, not a generated-prompt baseline.",
            (
                "The paper does not publish exact prompt-generation templates; "
                "Retro's templates are an explicit reconstruction."
            ),
        ],
    }
    _write_json(staging / "manifest.json", manifest)


def _aggregate_rows(task_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[FileLocalizationMetrics]] = defaultdict(list)
    for row in task_rows:
        metric_values = row["metrics"]
        row_metrics = FileLocalizationMetrics(**metric_values)
        key = (row["condition"], row["model"], row["prompt_level"])
        grouped[key].append(row_metrics)

    aggregate: list[dict[str, Any]] = []
    for (condition, model, prompt_level), group_metrics in sorted(grouped.items()):
        aggregate.append(
            {
                "condition": condition,
                "metric": "file_f1",
                "model": model,
                "prompt_level": prompt_level,
                **aggregate_file_localization(group_metrics),
            }
        )
    return aggregate


def _paired_comparisons(
    task_rows: Sequence[dict[str, Any]],
    *,
    baseline_condition: str,
    augmented_condition: str,
) -> list[dict[str, Any]]:
    values: dict[tuple[str, str, str, str], float] = {}
    models_and_levels: set[tuple[str, str]] = set()
    for row in task_rows:
        key = (
            row["condition"],
            row["model"],
            row["prompt_level"],
            row["task_id"],
        )
        values[key] = float(row["metrics"]["f1"])
        models_and_levels.add((row["model"], row["prompt_level"]))

    comparisons: list[dict[str, Any]] = []
    for model, prompt_level in sorted(models_and_levels):
        baseline = {
            task_id: score
            for (condition, row_model, level, task_id), score in values.items()
            if condition == baseline_condition
            and row_model == model
            and level == prompt_level
        }
        augmented = {
            task_id: score
            for (condition, row_model, level, task_id), score in values.items()
            if condition == augmented_condition
            and row_model == model
            and level == prompt_level
        }
        matched = sorted(set(baseline) & set(augmented))
        if not matched:
            continue
        deltas = [augmented[task_id] - baseline[task_id] for task_id in matched]
        comparisons.append(
            {
                "augmented_condition": augmented_condition,
                "baseline_condition": baseline_condition,
                "losses": sum(delta < 0 for delta in deltas),
                "matched_task_count": len(matched),
                "mean_file_f1_delta": round(sum(deltas) / len(deltas), 6),
                "model": model,
                "prompt_level": prompt_level,
                "ties": sum(delta == 0 for delta in deltas),
                "wins": sum(delta > 0 for delta in deltas),
            }
        )
    return comparisons


def _validate_prediction(
    record: dict[str, Any],
    *,
    line_number: int,
    task_ids: set[str],
    repository_root: Path,
) -> dict[str, Any]:
    schema_version = record.get("schema_version", PREDICTION_SCHEMA_VERSION)
    if schema_version != PREDICTION_SCHEMA_VERSION:
        raise ValueError(
            f"prediction line {line_number} has unsupported schema_version "
            f"{schema_version!r}"
        )
    for key in ("task_id", "condition", "model", "prompt_level", "predicted_files"):
        if key not in record:
            raise ValueError(f"prediction line {line_number} is missing {key!r}")
    task_id = record["task_id"]
    condition = record["condition"]
    model = record["model"]
    prompt_level = record["prompt_level"]
    predicted_files = record["predicted_files"]
    if not isinstance(task_id, str) or task_id not in task_ids:
        raise ValueError(f"prediction line {line_number} has unknown task_id {task_id!r}")
    for key, value in (("condition", condition), ("model", model)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"prediction line {line_number} has invalid {key}")
    if prompt_level not in (*PROMPT_LEVELS, SOURCE_PROMPT_LEVEL):
        raise ValueError(
            f"prediction line {line_number} has invalid prompt_level {prompt_level!r}"
        )
    if not isinstance(predicted_files, list) or not all(
        isinstance(path, str) for path in predicted_files
    ):
        raise ValueError(f"prediction line {line_number} predicted_files must be a string list")

    normalized_files: set[str] = set()
    for raw_path in predicted_files:
        normalized = _normalize_repo_path(
            raw_path,
            canonical_root=repository_root,
            relative_base=repository_root,
            session_root=repository_root,
        )
        if normalized is None:
            raise ValueError(
                f"prediction line {line_number} contains an invalid repository path: {raw_path!r}"
            )
        normalized_files.add(normalized)
    normalized_record: dict[str, Any] = {
        "condition": condition.strip(),
        "model": model.strip(),
        "predicted_files": sorted(normalized_files),
        "prompt_level": prompt_level,
        "schema_version": PREDICTION_SCHEMA_VERSION,
        "task_id": task_id,
    }
    metadata = record.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError(f"prediction line {line_number} metadata must be an object")
        normalized_record["metadata"] = metadata
    return normalized_record


def _validate_manifest(manifest: dict[str, Any], benchmark_id: str) -> None:
    if manifest.get("benchmark_id") != benchmark_id:
        raise RuntimeError("benchmark manifest id does not match its directory")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(
            f"unsupported benchmark schema version: {manifest.get('schema_version')!r}"
        )
    if manifest.get("method") != METHOD_NAME:
        raise RuntimeError(f"benchmark was not built by {METHOD_NAME}")


def _verify_artifacts(root: Path, manifest: dict[str, Any]) -> None:
    for artifact in manifest.get("artifacts", []):
        relative = artifact.get("path")
        expected_hash = artifact.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise RuntimeError("benchmark manifest contains an invalid artifact record")
        path = root / relative
        if not path.is_file():
            raise RuntimeError(f"benchmark artifact is missing: {relative}")
        if _file_sha256(path) != expected_hash:
            raise RuntimeError(f"benchmark artifact checksum mismatch: {relative}")


def _artifact_records(
    root: Path,
    artifacts: Sequence[tuple[str, int | None, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for relative, count, visibility in artifacts:
        path = root / relative
        record: dict[str, Any] = {
            "bytes": path.stat().st_size,
            "path": relative,
            "sha256": _file_sha256(path),
            "visibility": visibility,
        }
        if count is not None:
            record["records"] = count
        records.append(record)
    return records


def _write_benchmark_report(
    path: Path,
    *,
    benchmark_id: str,
    repository: _Repository,
    cutoff: datetime,
    end: datetime,
    tasks: Sequence[_Task],
    rejection_counts: Counter[str],
) -> None:
    families = Counter(task.task_family for task in tasks)
    lines = [
        f"# Benchmark card: {benchmark_id}",
        "",
        f"- Method: `{METHOD_NAME}`",
        f"- Paper: [{PAPER_TITLE}]({PAPER_URL})",
        f"- Repository snapshot: `{repository.snapshot_commit}`",
        f"- Task interval: `({_format_timestamp(cutoff)}, {_format_timestamp(end)}]`",
        f"- Accepted tasks: `{len(tasks)}`",
        "- Default prompt level: `contextual`",
        "",
        "## Task families",
        "",
        "| Family | Tasks |",
        "|---|---:|",
    ]
    lines.extend(f"| `{family}` | {count} |" for family, count in sorted(families.items()))
    lines.extend(
        [
            "",
            "## Rejected episodes",
            "",
            "| Reason | Episodes |",
            "|---|---:|",
        ]
    )
    lines.extend(
        f"| `{reason}` | {count} |"
        for reason, count in sorted(rejection_counts.items())
    )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Tasks are generated from post-cutoff rollout goal episodes and evaluated by exact",
            "set overlap between predicted files and files edited in the historical episode.",
            "This measures localization only; it does not establish patch correctness.",
            "",
        ]
    )
    _write_text(path, "\n".join(lines))


def _write_evaluation_report(path: Path, results: dict[str, Any]) -> None:
    lines = [
        f"# Evaluation run: {results['run_id']}",
        "",
        f"- Benchmark: `{results['benchmark_id']}`",
        f"- Method: `{results['method']}`",
        "",
        "| Condition | Model | Prompt | Tasks | Macro F1 | Exact match |",
        "|---|---|---|---:|---:|---:|",
    ]
    for aggregate in results["aggregate"]:
        lines.append(
            "| {condition} | {model} | {prompt_level} | {task_count} | "
            "{macro_f1:.3f} | {exact_match_rate:.3f} |".format(**aggregate)
        )
    if results["paired_comparisons"]:
        lines.extend(
            [
                "",
                "## Matched comparisons",
                "",
                "| Model | Prompt | Matched tasks | Mean F1 delta | Wins/Ties/Losses |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for comparison in results["paired_comparisons"]:
            lines.append(
                "| {model} | {prompt_level} | {matched_task_count} | "
                "{mean_file_f1_delta:.3f} | {wins}/{ties}/{losses} |".format(
                    **comparison
                )
            )
    lines.append("")
    _write_text(path, "\n".join(lines))


def _load_prompt_task_ids(benchmark_dir: Path) -> set[str]:
    expected_ids: set[str] | None = None
    for level in PROMPT_LEVELS:
        records = _read_jsonl_strict(
            benchmark_dir / "tasks" / "prompts" / f"{level}.jsonl"
        )
        task_ids: set[str] = set()
        for record in records:
            task_id = record.get("task_id")
            instruction = record.get("instruction")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError(f"{level} prompt artifact contains an invalid task id")
            if task_id in task_ids:
                raise RuntimeError(f"{level} prompt artifact contains duplicate task {task_id}")
            if record.get("prompt_level") != level:
                raise RuntimeError(f"{level} prompt artifact contains a mismatched level")
            if not isinstance(instruction, str) or not instruction.strip():
                raise RuntimeError(f"{level} prompt artifact contains an empty instruction")
            task_ids.add(task_id)
        if expected_ids is None:
            expected_ids = task_ids
        elif task_ids != expected_ids:
            raise RuntimeError("prompt-level artifacts do not contain the same task ids")
    if not expected_ids:
        raise RuntimeError("benchmark prompt artifacts contain no tasks")
    return expected_ids


def _read_jsonl_strict(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"JSONL artifact does not exist: {path}") from exc
    with handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object at {path}:{line_number}")
            records.append(value)
    return records


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"benchmark manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_jsonl(path: Path, records: Sequence[dict[str, Any]]) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        for record in records
    )
    _write_text(path, content)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _protect_private_file(path: Path) -> None:
    path.parent.chmod(0o700)
    path.chmod(0o600)


def _git(cwd: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout.strip()


def _session_cwd(events: Sequence[NormalizedEvent]) -> str | None:
    for event in events:
        payload = event.payload or {}
        for key in ("cwd", "current_working_directory", "workspace"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _event_timestamp(event: NormalizedEvent) -> datetime | None:
    if not event.timestamp:
        return None
    try:
        return parse_timestamp(event.timestamp)
    except ValueError:
        return None


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _task_id(snapshot_commit: str, host: Host, session_id: str, event_id: str) -> str:
    material = "\0".join((snapshot_commit, host, session_id, event_id))
    return f"tcl_{hashlib.sha256(material.encode()).hexdigest()[:20]}"


def _fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _minimal_prompt(value: str) -> str:
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    ]
    scored = [
        (
            (3 if _PRIMARY_ACTION_RE.search(sentence) else 0)
            + (1 if _ACTION_RE.search(sentence) else 0)
            + index / max(len(sentences), 1),
            -10 if _is_negative_constraint(sentence) else 0,
            -len(sentence),
            sentence,
        )
        for index, sentence in enumerate(sentences)
        if _ACTION_RE.search(sentence)
    ]
    if scored:
        selected = max(scored, key=lambda item: (item[0] + item[1], item[2]))[3]
    else:
        selected = sentences[0] if sentences else value
    return _truncate(selected, 180)


def _is_negative_constraint(sentence: str) -> bool:
    return bool(re.match(r"(?i)^(?:do not|don't|avoid|never)\b", sentence))


def _join_prompt(first: str, second: str, *, limit: int) -> str:
    return _truncate(f"{first.rstrip()} {second.strip()}".strip(), limit)


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    lowered = path.casefold()
    return (
        any(part.casefold() in {"test", "tests", "__tests__"} for part in pure.parts)
        or pure.name.casefold().startswith("test_")
        or ".test." in lowered
        or ".spec." in lowered
    )


def _is_doc_path(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        any(part.casefold() in {"doc", "docs", "documentation"} for part in pure.parts)
        or pure.suffix.casefold() in {".md", ".mdx", ".rst"}
    )


def _plausible_observed_file(path: str, session_root: Path) -> bool:
    candidate = session_root / path
    if candidate.is_dir():
        return False
    name = PurePosixPath(path).name
    return bool(
        candidate.is_file()
        or PurePosixPath(path).suffix
        or name
        in {
            "Dockerfile",
            "Gemfile",
            "LICENSE",
            "Makefile",
            "Procfile",
            "README",
        }
    )


def _validate_identifier(value: str, label: str) -> None:
    if not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(
            f"{label} must start with an alphanumeric character and contain only "
            "letters, numbers, '.', '_', or '-'"
        )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
