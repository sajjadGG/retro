"""Durable, explicitly started experiments over immutable evaluation datasets.

The setup pins paths and content hashes, never secret agent configuration
payloads. Each dataset owns an evaluation shared by its candidate agents, while
a job-local lock protects execution without blocking capture or other jobs.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...archive_health import require_archive_volume
from ...experiment_traces import EXPERIMENT_TAG, EXPERIMENT_TAG_PREFIX, MAX_EXPERIMENT_ID_LENGTH
from ...locking import LockUnavailableError, exclusive_lock
from ...storage import Layout
from .aggregate import validate_attempt_record
from .build import TasksetPaths, utc_now
from .ghostlab_cli import GhostlabCli, GhostlabError, read_json, sha256_file, sha256_json, write_json
from .run import (
    ATTEMPT_METADATA_SCHEMA,
    CANDIDATE_EXPORT_NAME,
    AgentSpec,
    AttemptResult,
    TaskVerificationError,
    cleanup_attempt_workspace,
    collect_eval_report,
    run_taskset,
)
from .schema import require_sha256_hex, require_task_id
from .suite import COHORT_SCHEMA, SPLITS, CuratedSuite, SuiteError, load_evaluation_cohort, load_suite

EXPERIMENT_SETUP_SCHEMA = "retro-experiment-setup-v1"
EXPERIMENT_JOB_SCHEMA = "retro-experiment-job-v1"
EXPERIMENT_JOB_ARTIFACTS = ("setup.json", "job.json", ".job.lock", "controller-status.json")
EXPERIMENT_STATUSES = ("planned", "running", "completed", "failed", "interrupted")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FINISHED = frozenset({"completed", "failed", "interrupted"})
_RUN_ID_FIELDS = (
    "run_id", "dataset_id", "name", "suite_id", "split", "agent_id",
    "eval_id", "eval_dir", "results_path", "requested_attempts",
)


class ExperimentError(ValueError):
    """An experiment cannot be created, verified, recovered, or cleaned safely."""


def _id(value: Any, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value) or len(value) > maximum:
        raise ExperimentError(f"{label} must be a path-safe identifier of at most {maximum} characters")
    return value


def _layout(value: Layout | Path | str) -> Layout:
    if isinstance(value, Layout):
        return Layout(value.root.expanduser().absolute())
    return Layout(Path(value).expanduser().absolute())


def _path(layout: Layout, path: Path) -> Path:
    root = layout.root
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise ExperimentError(f"experiment artifact path escapes the archive: {path}") from exc
    current = root
    for part in ("", *relative.parts):
        current = current / part
        if current.is_symlink():
            raise ExperimentError(f"experiment artifacts must not use symlinks: {current}")
    resolved = path.resolve()
    if root.resolve() not in (resolved, *resolved.parents):
        raise ExperimentError(f"experiment artifact path escapes the archive: {path}")
    return path


def _job_dir(layout: Layout, experiment_id: str) -> Path:
    return _path(
        layout,
        layout.root / "experiments" / _id(
            experiment_id, "experiment id", maximum=MAX_EXPERIMENT_ID_LENGTH,
        ),
    )


def _timeout(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ExperimentError(f"{label} must be a positive finite number")
    return float(value)


def _seeds(values: Any) -> list[int]:
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or not values
        or any(isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in values)
        or len(values) != len(set(values))
    ):
        raise ExperimentError("experiment seeds must be unique non-negative integers")
    return list(values)


def _suite(layout: Layout, name: str, suite_id: str) -> CuratedSuite:
    paths = TasksetPaths.from_layout(layout, _id(name, "taskset name"))
    _path(layout, paths.root / "suites" / _id(suite_id, "suite id"))
    return load_suite(paths, suite_id)


def _dataset(layout: Layout, value: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperimentError("experiment datasets must be objects")
    unknown = set(value) - {"dataset_id", "name", "suite_id", "split", "task_ids"}
    if unknown:
        raise ExperimentError(f"unknown experiment dataset fields: {sorted(unknown)}")
    name = _id(value.get("name"), "dataset taskset name")
    suite_id = _id(value.get("suite_id"), "dataset suite id")
    split = value.get("split", "test")
    if split not in SPLITS:
        raise ExperimentError(f"dataset split must be one of {SPLITS}")
    suite = _suite(layout, name, suite_id)
    selected = value.get("task_ids", suite.task_ids(split))
    if (
        not isinstance(selected, Sequence)
        or isinstance(selected, (str, bytes))
        or not selected
        or any(not isinstance(task_id, str) for task_id in selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(suite.task_ids(split))
    ):
        raise ExperimentError("dataset task_ids must be a non-empty unique subset of its frozen split")
    artifacts: dict[str, Any] = {}
    for task_id in sorted(selected):
        task = suite.task(task_id, split)
        row = next(row for row in suite.manifest["tasks"] if row["task_id"] == task_id)
        artifacts[task_id] = {
            "content_sha256": row["content_sha256"],
            "public_task_sha256": task.public_task_sha256,
            "base_bundle_sha256": task.base_bundle_sha256,
            "scorer_package_sha256": task.scorer_package_sha256,
            "source_id": task.source_id,
            "pass_threshold": task.pass_threshold,
        }
    return {
        "dataset_id": _id(value.get("dataset_id", f"dataset-{index + 1}"), "dataset id"),
        "name": name,
        "suite_id": suite_id,
        "suite_content_sha256": suite.content_sha256,
        "split": split,
        "task_ids": sorted(selected),
        "task_artifacts": artifacts,
    }


def _agent(spec: AgentSpec) -> dict[str, Any]:
    if not isinstance(spec, AgentSpec):
        raise ExperimentError("experiment agents must be verified AgentSpec objects")
    _id(spec.agent_id, "agent id")
    if spec.config_path.is_symlink():
        raise ExperimentError("agent config must not be a symlink")
    spec.verify()
    pinned = AgentSpec.from_path(
        spec.config_path.expanduser().resolve(),
        agent_id=spec.agent_id,
        expected_sha256=spec.expected_sha256,
    )
    if pinned.config_sha256 != spec.config_sha256:
        raise ExperimentError("agent config changed while the experiment was being planned")
    labels = pinned.runtime_labels()
    return {
        "agent_id": spec.agent_id,
        "config_path": str(pinned.config_path),
        "config_sha256": pinned.config_sha256,
        "expected_sha256": pinned.expected_sha256,
        "asset_sha256": pinned.verify_assets(),
        "model": labels.get("model"),
        "runtime": labels,
    }


def _new_runs(layout: Layout, setup: Mapping[str, Any]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for dataset in setup["datasets"]:
        for agent in setup["agents"]:
            eval_id = "experiment-" + sha256_json(
                [setup["experiment_id"], dataset["dataset_id"]]
            )[:24]
            paths = TasksetPaths.from_layout(layout, dataset["name"])
            eval_dir = _path(layout, paths.eval_dir(eval_id))
            runs.append(
                {
                    "run_id": "invocation-" + sha256_json([eval_id, agent["agent_id"]])[:24],
                    "dataset_id": dataset["dataset_id"],
                    "name": dataset["name"],
                    "suite_id": dataset["suite_id"],
                    "split": dataset["split"],
                    "agent_id": agent["agent_id"],
                    "eval_id": eval_id,
                    "eval_dir": str(eval_dir),
                    "results_path": str(paths.results_path(eval_id)),
                    "requested_attempts": len(dataset["task_ids"]) * len(setup["seeds"]),
                    "status": "planned",
                    "started_at": None,
                    "finished_at": None,
                    "completed_attempts": 0,
                    "scored_attempts": 0,
                    "reused_attempts": 0,
                    "status_counts": {},
                    "benchmark_score": None,
                    "pass_rate": None,
                    "valid_coverage": 0.0,
                    "attempts": [],
                    "errors": [],
                    "error": None,
                }
            )
    return runs


def _summary(state: Mapping[str, Any]) -> dict[str, Any]:
    runs = state["runs"]
    counts: Counter[str] = Counter()
    for run in runs:
        counts.update(run["status_counts"])
    return {
        "runs": len(runs),
        "completed_runs": sum(run["status"] == "completed" for run in runs),
        "requested_attempts": sum(run["requested_attempts"] for run in runs),
        "completed_attempts": sum(run["completed_attempts"] for run in runs),
        "scored_attempts": sum(run["scored_attempts"] for run in runs),
        "status_counts": dict(sorted(counts.items())),
        "error_attempts": sum(run["completed_attempts"] - run["scored_attempts"] for run in runs),
    }


def _write_state(layout: Layout, setup: Mapping[str, Any], state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    state["summary"] = _summary(state)
    write_json(_path(layout, _job_dir(layout, setup["experiment_id"]) / "job.json"), state)


def _view(layout: Layout, setup: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **setup,
        **state,
        "path": str(_job_dir(layout, setup["experiment_id"])),
        "requested_attempts": sum(run["requested_attempts"] for run in state["runs"]),
    }


def create_experiment(
    layout: Layout | Path | str,
    *,
    title: str,
    description: str = "",
    hypothesis: str = "",
    datasets: Sequence[Mapping[str, Any]],
    agents: Sequence[AgentSpec],
    seeds: Sequence[int] = (0,),
    experiment_id: str | None = None,
    aut_timeout_seconds: float | None = None,
    scorer_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Publish a planned job; this never resolves Ghostlab or starts an agent."""
    archive = _layout(layout)
    require_archive_volume(archive.root)
    if not isinstance(title, str) or not title.strip():
        raise ExperimentError("experiment title must be a non-empty string")
    if not isinstance(description, str) or not isinstance(hypothesis, str):
        raise ExperimentError("experiment description and hypothesis must be strings")
    identity = _id(
        experiment_id if experiment_id is not None else "experiment-" + uuid.uuid4().hex,
        "experiment id", maximum=MAX_EXPERIMENT_ID_LENGTH,
    )
    if not isinstance(datasets, Sequence) or isinstance(datasets, (str, bytes)) or not datasets:
        raise ExperimentError("an experiment needs at least one frozen dataset")
    if not isinstance(agents, Sequence) or isinstance(agents, (str, bytes)) or not agents:
        raise ExperimentError("an experiment needs at least one candidate agent")
    try:
        pinned_datasets = [_dataset(archive, value, index) for index, value in enumerate(datasets)]
        pinned_agents = [_agent(spec) for spec in agents]
    except (TaskVerificationError, SuiteError, GhostlabError) as exc:
        raise ExperimentError(f"cannot pin experiment inputs: {exc}") from exc
    for label, values in (
        ("dataset", [dataset["dataset_id"] for dataset in pinned_datasets]),
        ("agent", [agent["agent_id"] for agent in pinned_agents]),
    ):
        if len(values) != len(set(values)):
            raise ExperimentError(f"experiment {label} IDs must be unique")
    setup: dict[str, Any] = {
        "schema_version": EXPERIMENT_SETUP_SCHEMA,
        "experiment_id": identity,
        "title": title.strip(),
        "description": description,
        "hypothesis": hypothesis,
        "created_at": utc_now(),
        "datasets": pinned_datasets,
        "agents": pinned_agents,
        "seeds": _seeds(seeds),
        "aut_timeout_seconds": _timeout(aut_timeout_seconds, "agent timeout"),
        "scorer_timeout_seconds": _timeout(scorer_timeout_seconds, "scorer timeout"),
        "tags": [EXPERIMENT_TAG, f"{EXPERIMENT_TAG_PREFIX}{identity}"],
    }
    setup["content_sha256"] = sha256_json(setup)
    state: dict[str, Any] = {
        "schema_version": EXPERIMENT_JOB_SCHEMA,
        "experiment_id": identity,
        "setup_sha256": setup["content_sha256"],
        "status": "planned",
        "started_at": None,
        "finished_at": None,
        "updated_at": setup["created_at"],
        "worker": None,
        "ghostlab": None,
        "runs": _new_runs(archive, setup),
        "error": None,
        "cleanup": None,
        "cleanup_history": [],
        "artifacts_pruned": False,
    }
    state["summary"] = _summary(state)
    try:
        _verify_inputs(archive, setup)
    except (TaskVerificationError, SuiteError, GhostlabError, OSError) as exc:
        raise ExperimentError(f"experiment inputs changed during planning: {exc}") from exc
    root = _path(archive, archive.root / "experiments")
    root.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(_path(archive, root / ".create.lock")):
        destination = _job_dir(archive, identity)
        if destination.exists():
            raise ExperimentError(f"experiment {identity!r} already exists; its setup is immutable")
        for run in state["runs"]:
            if Path(run["eval_dir"]).exists():
                raise ExperimentError(f"experiment evaluation already exists: {run['eval_id']}")
        stage = _path(archive, root / ("." + identity + ".creating-" + uuid.uuid4().hex))
        stage.mkdir()
        try:
            write_json(stage / "setup.json", setup)
            write_json(stage / "job.json", state)
            os.replace(stage, destination)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return _view(archive, setup, state)


def _read_job(layout: Layout, experiment_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    directory = _job_dir(layout, experiment_id)
    try:
        setup = read_json(_path(layout, directory / "setup.json"), label="experiment setup")
        state = read_json(_path(layout, directory / "job.json"), label="experiment job")
        if (
            not isinstance(setup, dict)
            or setup.get("schema_version") != EXPERIMENT_SETUP_SCHEMA
            or setup.get("experiment_id") != experiment_id
            or setup.get("content_sha256") != sha256_json(
                {key: value for key, value in setup.items() if key != "content_sha256"}
            )
        ):
            raise ExperimentError("experiment setup identity or immutable content hash is invalid")
        if (
            not isinstance(setup.get("title"), str) or not setup["title"].strip()
            or not isinstance(setup.get("description"), str)
            or not isinstance(setup.get("hypothesis", ""), str)
        ):
            raise ExperimentError("experiment setup title, description, or hypothesis is invalid")
        _seeds(setup.get("seeds"))
        _timeout(setup.get("aut_timeout_seconds"), "agent timeout")
        _timeout(setup.get("scorer_timeout_seconds"), "scorer timeout")
        for collection, identifier in (("datasets", "dataset_id"), ("agents", "agent_id")):
            rows = setup.get(collection)
            if not isinstance(rows, list) or not rows:
                raise ExperimentError(f"experiment setup needs {collection}")
            ids = [_id(row.get(identifier), identifier) for row in rows if isinstance(row, Mapping)]
            if len(ids) != len(rows) or len(ids) != len(set(ids)):
                raise ExperimentError(f"experiment setup has malformed or duplicate {collection}")
        for dataset in setup["datasets"]:
            _id(dataset.get("name"), "taskset name")
            _id(dataset.get("suite_id"), "suite id")
            require_sha256_hex(dataset.get("suite_content_sha256"), "experiment suite hash")
            tasks = dataset.get("task_ids")
            if (
                dataset.get("split") not in SPLITS
                or not isinstance(tasks, list) or not tasks
                or any(not isinstance(task, str) for task in tasks)
                or len(tasks) != len(set(tasks))
                or not isinstance(dataset.get("task_artifacts"), Mapping)
                or set(dataset["task_artifacts"]) != set(tasks)
            ):
                raise ExperimentError("experiment dataset has an invalid frozen task cohort")
            for task_id in tasks:
                require_task_id(task_id, "experiment task id")
                record = dataset["task_artifacts"][task_id]
                if not isinstance(record, Mapping):
                    raise ExperimentError("experiment task artifact pins must be objects")
                for key in (
                    "content_sha256", "public_task_sha256", "base_bundle_sha256", "scorer_package_sha256",
                ):
                    digest = record.get(key)
                    if not isinstance(digest, str):
                        raise ExperimentError(f"experiment task {key} must be a sha256")
                    require_sha256_hex(digest, key)
                if not isinstance(record.get("source_id"), str) or not record["source_id"]:
                    raise ExperimentError("experiment tasks must carry source provenance")
                threshold = record.get("pass_threshold")
                if (
                    isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                    or not math.isfinite(threshold) or not 0 <= threshold <= 1
                ):
                    raise ExperimentError("experiment task pass threshold must be finite and within [0, 1]")
        for agent in setup["agents"]:
            if not isinstance(agent.get("config_path"), str) or not Path(agent["config_path"]).is_absolute():
                raise ExperimentError("experiment agent config path must be absolute")
            require_sha256_hex(agent.get("config_sha256"), "experiment agent config hash")
            assets = agent.get("asset_sha256")
            if not isinstance(assets, Mapping):
                raise ExperimentError("experiment agent assets must be content pinned")
            for reference, digest in assets.items():
                if not isinstance(reference, str):
                    raise ExperimentError("experiment asset references must be strings")
                if digest != "<missing>":
                    require_sha256_hex(digest, "experiment asset hash")
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != EXPERIMENT_JOB_SCHEMA
            or state.get("experiment_id") != experiment_id
            or state.get("setup_sha256") != setup["content_sha256"]
            or state.get("status") not in EXPERIMENT_STATUSES
        ):
            raise ExperimentError("experiment job identity, setup pin, or status is invalid")
        runs = state.get("runs")
        expected = _new_runs(layout, setup)
        if not isinstance(runs, list) or len(runs) != len(expected):
            raise ExperimentError("experiment job has an invalid evaluation plan")
        for actual, pinned in zip(runs, expected):
            if (
                not isinstance(actual, Mapping)
                or any(actual.get(key) != pinned[key] for key in _RUN_ID_FIELDS)
                or actual.get("status") not in EXPERIMENT_STATUSES
                or not isinstance(actual.get("attempts"), list)
                or not isinstance(actual.get("status_counts"), Mapping)
            ):
                raise ExperimentError("experiment job evaluations differ from their immutable plan")
            for key in ("completed_attempts", "scored_attempts", "reused_attempts"):
                count = actual.get(key)
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ExperimentError(f"experiment evaluation {key} must be a non-negative integer")
            if (
                actual["scored_attempts"] > actual["completed_attempts"]
                or actual["completed_attempts"] > actual["requested_attempts"]
                or any(
                    not isinstance(status, str) or isinstance(count, bool)
                    or not isinstance(count, int) or count < 0
                    for status, count in actual["status_counts"].items()
                )
            ):
                raise ExperimentError("experiment evaluation counts are invalid")
        if state.get("summary") != _summary(state):
            raise ExperimentError("experiment job summary does not match its persisted evaluations")
        return setup, state
    except (GhostlabError, ValueError, TypeError, KeyError, OSError) as exc:
        if isinstance(exc, ExperimentError):
            raise
        raise ExperimentError(f"cannot read experiment {experiment_id}: {exc}") from exc


def _error(code: str, exc: BaseException | str) -> dict[str, Any]:
    return {
        "code": code,
        "message": str(exc),
        "type": type(exc).__name__ if isinstance(exc, BaseException) else None,
        "at": utc_now(),
    }


def _refresh_run(layout: Layout, setup: Mapping[str, Any], run: dict[str, Any]) -> None:
    dataset = next(item for item in setup["datasets"] if item["dataset_id"] == run["dataset_id"])
    agent = next(item for item in setup["agents"] if item["agent_id"] == run["agent_id"])
    paths = TasksetPaths.from_layout(layout, run["name"])
    rows: list[dict[str, Any]] = []
    for task_id in dataset["task_ids"]:
        for seed in setup["seeds"]:
            directory = _path(layout, paths.attempt_dir(run["eval_id"], task_id, run["agent_id"], seed))
            attempt_path = _path(layout, directory / "attempt.json")
            if not attempt_path.is_file():
                continue
            payload = read_json(attempt_path)
            if not isinstance(payload, Mapping):
                raise ExperimentError(f"experiment attempt is not a JSON object: {attempt_path}")
            record = validate_attempt_record(payload, path=attempt_path)
            artifacts = dataset["task_artifacts"][task_id]
            if (
                record.task_id != task_id or record.agent_id != run["agent_id"] or record.seed != seed
                or record.agent_config_sha256 != agent["config_sha256"]
                or record.base_bundle_sha256 != artifacts["base_bundle_sha256"]
                or record.scorer_package_sha256 != artifacts["scorer_package_sha256"]
                or record.pass_threshold != artifacts["pass_threshold"]
            ):
                raise ExperimentError(f"experiment attempt does not match its pinned inputs: {attempt_path}")
            metadata_path = _path(layout, directory / "attempt-metadata.json")
            metadata = read_json(metadata_path)
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("schema_version") != ATTEMPT_METADATA_SCHEMA
                or metadata.get("experiment_job_id") != setup["experiment_id"]
                or metadata.get("attempt_id") != record.attempt_id
                or metadata.get("input_sha256") != record.input_sha256
                or metadata.get("attempt_sha256") != sha256_file(attempt_path)
            ):
                raise ExperimentError(f"experiment attempt has invalid trace metadata: {metadata_path}")
            score_path = _path(layout, directory / "score-report.json")
            if record.status == "scored" and (
                not score_path.is_file()
                or metadata.get("score_report_sha256") != sha256_file(score_path)
            ):
                raise ExperimentError(f"experiment score evidence changed after execution: {score_path}")
            events = metadata.get("agent_events_path")
            if isinstance(events, str):
                events_path = _path(layout, Path(events))
                if directory / "agent" not in events_path.parents:
                    raise ExperimentError("experiment agent events path is outside its attempt")
                if (
                    not events_path.is_file()
                    or metadata.get("agent_events_sha256") != sha256_file(events_path)
                ):
                    raise ExperimentError("experiment agent events changed after execution")
            candidate_exports = metadata.get("candidate_exports", [])
            if not isinstance(candidate_exports, list):
                raise ExperimentError("experiment candidate export metadata must be a list")
            allowed = {
                CANDIDATE_EXPORT_NAME, CANDIDATE_EXPORT_NAME.removesuffix(".tar.zst") + ".tar.gz",
            }
            for export in candidate_exports:
                if not isinstance(export, Mapping) or not isinstance(export.get("path"), str):
                    raise ExperimentError("experiment candidate export metadata is malformed")
                candidate = _path(layout, Path(export["path"]))
                if candidate.parent != directory / "agent" or candidate.name not in allowed:
                    raise ExperimentError("experiment candidate export path is outside its exact attempt")
                if candidate.exists() and sha256_file(candidate) != export.get("sha256"):
                    raise ExperimentError("experiment candidate export changed after execution")
            if payload.get("artifact_run") is not None:
                artifact_path = _path(layout, Path(payload["artifact_run"]))
                if artifact_path != directory / "agent" / "artifact-run.json":
                    raise ExperimentError("experiment artifact-run report path is outside its exact attempt")
            if payload.get("score_report") is not None:
                report_path = _path(layout, Path(payload["score_report"]))
                if report_path != directory / "score-report.json":
                    raise ExperimentError("experiment score-report path is outside its exact attempt")
            rows.append(
                {
                    **{key: payload.get(key) for key in (
                        "attempt_id", "task_id", "agent_id", "seed", "status", "score",
                        "passed", "error", "artifact_run", "score_report", "candidate_state_sha256",
                        "components", "source_id", "tokens", "wall_time_ms", "cost_usd", "input_sha256",
                    )},
                    "attempt_path": str(attempt_path),
                    "metadata_path": str(metadata_path),
                    "agent_events_path": metadata.get("agent_events_path"),
                    "candidate_exports": metadata.get("candidate_exports", []),
                }
            )
    before = {
        row["attempt_id"]: row for row in run["attempts"] if isinstance(row, Mapping)
    }
    for row in rows:
        previous = before.get(row["attempt_id"])
        if previous is not None and previous != row:
            raise ExperimentError("experiment attempt evidence changed since the job recorded it")
    run["attempts"] = rows
    run["completed_attempts"] = len(rows)
    run["scored_attempts"] = sum(row["status"] == "scored" for row in rows)
    run["status_counts"] = dict(sorted(Counter(row["status"] for row in rows).items()))
    run["valid_coverage"] = run["scored_attempts"] / run["requested_attempts"]
    run["errors"] = [
        {key: row[key] for key in ("task_id", "seed", "status", "error")}
        for row in rows if row["status"] != "scored"
    ]


def _recover(layout: Layout, setup: Mapping[str, Any], state: dict[str, Any]) -> None:
    if state["status"] != "running":
        return
    for run in state["runs"]:
        try:
            _verify_evaluation(layout, setup, run)
            _refresh_run(layout, setup, run)
            if run["completed_attempts"] == run["requested_attempts"]:
                paths = TasksetPaths.from_layout(layout, run["name"])
                aggregate = collect_eval_report(paths, run["eval_id"])
                scored = next((item for item in aggregate.agents if item.agent_id == run["agent_id"]), None)
                if scored is not None:
                    run.update(benchmark_score=scored.benchmark_score, pass_rate=scored.pass_rate)
                run.update(status="completed", finished_at=run["finished_at"] or utc_now(), error=None)
            elif run["status"] == "running":
                run.update(
                    status="interrupted", error=_error("worker_interrupted", "experiment worker stopped"),
                )
        except (ExperimentError, GhostlabError, TaskVerificationError, ValueError, OSError) as exc:
            run.update(status="interrupted", error=_error("recovery_error", exc))
    completed = all(run["status"] == "completed" for run in state["runs"])
    state.update(
        status="completed" if completed else "interrupted",
        finished_at=utc_now(),
        worker=None,
        error=None if completed else _error("worker_interrupted", "no worker holds this experiment's lock"),
    )
    _write_state(layout, setup, state)


def load_experiment(layout: Layout | Path | str, experiment_id: str) -> dict[str, Any]:
    """Read a job and atomically recover a running job whose worker lock is gone."""
    archive = _layout(layout)
    setup, state = _read_job(archive, experiment_id)
    if state["status"] == "running":
        try:
            require_archive_volume(archive.root)
            with exclusive_lock(_path(archive, _job_dir(archive, experiment_id) / ".job.lock")):
                setup, state = _read_job(archive, experiment_id)
                _recover(archive, setup, state)
        except LockUnavailableError:
            pass
    return _view(archive, setup, state)


def list_experiments(layout: Layout | Path | str) -> list[dict[str, Any]]:
    """List recognized jobs, preserving failures without claiming unrelated legacy folders."""
    archive = _layout(layout)
    root = _path(archive, archive.root / "experiments")
    if not root.exists():
        return []
    jobs: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if directory.name.startswith("."):
            continue
        if not directory.is_dir() and not directory.is_symlink():
            continue
        try:
            if not any(
                (directory / name).exists() or (directory / name).is_symlink()
                for name in EXPERIMENT_JOB_ARTIFACTS
            ):
                continue
            jobs.append(load_experiment(archive, directory.name))
        except (ExperimentError, OSError) as exc:
            jobs.append(
                {
                    "schema_version": EXPERIMENT_JOB_SCHEMA,
                    "experiment_id": directory.name,
                    "title": directory.name,
                    "path": str(directory),
                    "status": "failed",
                    "error": _error("invalid_job", exc),
                    "datasets": [], "agents": [], "runs": [],
                }
            )
    return sorted(jobs, key=lambda job: (job.get("created_at", ""), job["experiment_id"]), reverse=True)


def _verify_inputs(layout: Layout, setup: Mapping[str, Any]) -> dict[str, AgentSpec]:
    for dataset in setup["datasets"]:
        suite = _suite(layout, dataset["name"], dataset["suite_id"])
        if suite.content_sha256 != dataset["suite_content_sha256"]:
            raise ExperimentError(f"dataset {dataset['dataset_id']} suite changed after planning")
        current = _dataset(
            layout, {key: dataset[key] for key in ("dataset_id", "name", "suite_id", "split", "task_ids")}, 0
        )
        if current != dataset:
            raise ExperimentError(f"dataset {dataset['dataset_id']} artifacts changed after planning")
    agents: dict[str, AgentSpec] = {}
    for pinned in setup["agents"]:
        path = Path(pinned["config_path"])
        if path.is_symlink():
            raise ExperimentError("pinned agent config became a symlink")
        agent = AgentSpec.from_path(
            path, agent_id=pinned["agent_id"], expected_sha256=pinned["config_sha256"]
        )
        agent.verify_assets(pinned["asset_sha256"])
        agents[agent.agent_id] = agent
    return agents


def _verify_evaluation(layout: Layout, setup: Mapping[str, Any], run: Mapping[str, Any]) -> None:
    eval_dir = _path(layout, Path(run["eval_dir"]))
    if not eval_dir.exists():
        return
    _path(layout, eval_dir / "attempts")
    _path(layout, eval_dir / "cohort.json")
    _path(layout, eval_dir / "results.json")
    for artifact in eval_dir.rglob("*"):
        if artifact.is_symlink():
            raise ExperimentError(f"evaluation artifacts must not use symlinks: {artifact}")
    dataset = next(item for item in setup["datasets"] if item["dataset_id"] == run["dataset_id"])
    paths = TasksetPaths.from_layout(layout, dataset["name"])
    cohort = load_evaluation_cohort(paths, run["eval_id"])
    expected = {
        "schema_version": COHORT_SCHEMA,
        "name": dataset["name"],
        "suite_id": dataset["suite_id"],
        "suite_content_sha256": dataset["suite_content_sha256"],
        "split": dataset["split"],
        "task_ids": dataset["task_ids"],
        "seeds": sorted(setup["seeds"]),
        "task_sources": {
            task_id: record["source_id"] for task_id, record in dataset["task_artifacts"].items()
        },
        "task_artifacts": {
            task_id: {
                key: record[key] for key in (
                    "base_bundle_sha256", "scorer_package_sha256", "public_task_sha256", "pass_threshold",
                )
            }
            for task_id, record in dataset["task_artifacts"].items()
        },
    }
    if cohort is not None and cohort != expected:
        raise ExperimentError("experiment evaluation cohort differs from its immutable dataset pin")
    if cohort is None and (eval_dir / "attempts").exists():
        raise ExperimentError("experiment evaluation attempts have no immutable cohort")
    allowed_agents = {agent["agent_id"] for agent in setup["agents"]}
    for path in (eval_dir / "attempts").glob("*/*/seed-*/attempt.json"):
        payload = read_json(path)
        if (
            not isinstance(payload, Mapping)
            or payload.get("task_id") not in dataset["task_ids"]
            or payload.get("agent_id") not in allowed_agents
            or payload.get("seed") not in setup["seeds"]
        ):
            raise ExperimentError(f"evaluation contains an unplanned attempt: {path}")


def run_experiment(
    layout: Layout | Path | str,
    experiment_id: str,
    *,
    ghostlab: GhostlabCli | str | Path | None = None,
    ghostlab_env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Explicitly start or resume a job, persisting progress after every attempt."""
    archive = _layout(layout)
    require_archive_volume(archive.root)
    directory = _job_dir(archive, experiment_id)
    _read_job(archive, experiment_id)
    with exclusive_lock(_path(archive, directory / ".job.lock")):
        setup, state = _read_job(archive, experiment_id)
        _recover(archive, setup, state)
        if state["status"] in ("completed", "failed"):
            return _view(archive, setup, state)
        state.update(
            status="running",
            started_at=state["started_at"] or utc_now(),
            finished_at=None,
            worker={"pid": os.getpid(), "started_at": utc_now()},
            error=None,
        )
        _write_state(archive, setup, state)
        active: dict[str, Any] | None = None
        try:
            if state.get("artifacts_pruned"):
                raise ExperimentError(
                    "candidate exports were pruned; create a new experiment to run again"
                )
            agents = _verify_inputs(archive, setup)
            client = (
                ghostlab if isinstance(ghostlab, GhostlabCli) else GhostlabCli(ghostlab, env=ghostlab_env)
            )
            version = client.version().fingerprint()
            if state["ghostlab"] is not None and state["ghostlab"] != version:
                raise ExperimentError("Ghostlab changed since this experiment started; create a new job")
            state["ghostlab"] = version
            _write_state(archive, setup, state)
            for run in state["runs"]:
                if run["status"] == "completed":
                    continue
                active = run
                _verify_evaluation(archive, setup, run)
                dataset = next(item for item in setup["datasets"] if item["dataset_id"] == run["dataset_id"])
                agent = agents[run["agent_id"]]
                run.update(
                    status="running", started_at=run["started_at"] or utc_now(), finished_at=None, error=None,
                )
                _write_state(archive, setup, state)

                def progress(attempt: AttemptResult, current: dict[str, Any] = run) -> None:
                    _refresh_run(archive, setup, current)
                    if attempt.reused:
                        current["reused_attempts"] += 1
                    _write_state(archive, setup, state)

                result = run_taskset(
                    archive, run["name"], agent,
                    seeds=setup["seeds"], eval_id=run["eval_id"], task_ids=dataset["task_ids"],
                    suite_id=dataset["suite_id"], split=dataset["split"], ghostlab=client,
                    aut_timeout_seconds=setup["aut_timeout_seconds"],
                    scorer_timeout_seconds=setup["scorer_timeout_seconds"],
                    experiment_id=experiment_id,
                    expected_agent_asset_hashes=next(
                        item["asset_sha256"] for item in setup["agents"] if item["agent_id"] == agent.agent_id
                    ),
                    on_attempt=progress,
                )
                _refresh_run(archive, setup, run)
                run.update(
                    status="completed", finished_at=utc_now(), benchmark_score=result.benchmark_score,
                    pass_rate=result.pass_rate, valid_coverage=result.coverage, error=None,
                )
                _write_state(archive, setup, state)
                active = None
            state.update(status="completed", finished_at=utc_now(), worker=None, error=None)
        except (KeyboardInterrupt, SystemExit) as exc:
            if active is not None:
                active.update(status="interrupted", error=_error("worker_interrupted", exc))
            state.update(
                status="interrupted", finished_at=utc_now(), worker=None,
                error=_error("worker_interrupted", exc),
            )
        except Exception as exc:
            if active is not None:
                active.update(
                    status="failed", finished_at=utc_now(), error=_error("orchestration_error", exc),
                )
            state.update(
                status="failed", finished_at=utc_now(), worker=None, error=_error("orchestration_error", exc),
            )
        _write_state(archive, setup, state)
        return _view(archive, setup, state)


def cleanup_experiment(
    layout: Layout | Path | str,
    experiment_id: str,
    *,
    prune_artifacts: bool = False,
) -> dict[str, Any]:
    """Clean a stopped job's local inputs and optionally its exact candidate exports."""
    archive = _layout(layout)
    require_archive_volume(archive.root)
    if not isinstance(prune_artifacts, bool):
        raise ExperimentError("prune_artifacts must be a boolean")
    directory = _job_dir(archive, experiment_id)
    _read_job(archive, experiment_id)
    with exclusive_lock(_path(archive, directory / ".job.lock")):
        setup, state = _read_job(archive, experiment_id)
        _recover(archive, setup, state)
        if state["status"] not in _FINISHED:
            raise ExperimentError("only a finished or interrupted experiment may be cleaned")
        report: dict[str, Any] = {
            "at": utc_now(), "prune_artifacts": prune_artifacts, "removed": [], "errors": [],
            "sandbox_cleanup": "Ghostlab-owned; no global sandbox, container, image or cache pruning",
        }
        for run in state["runs"]:
            dataset = next(item for item in setup["datasets"] if item["dataset_id"] == run["dataset_id"])
            paths = TasksetPaths.from_layout(archive, run["name"])
            for task_id in dataset["task_ids"]:
                for seed in setup["seeds"]:
                    attempt_dir = paths.attempt_dir(run["eval_id"], task_id, run["agent_id"], seed)
                    try:
                        _path(archive, attempt_dir)
                        if not attempt_dir.exists():
                            continue
                        with exclusive_lock(_path(archive, attempt_dir / ".attempt.lock")):
                            ephemeral = cleanup_attempt_workspace(attempt_dir)
                            report["removed"].extend(ephemeral["removed"])
                            report["errors"].extend(ephemeral["errors"])
                            if not prune_artifacts:
                                continue
                            metadata_path = _path(archive, attempt_dir / "attempt-metadata.json")
                            if not metadata_path.exists():
                                continue
                            metadata = read_json(metadata_path)
                            if (
                                not isinstance(metadata, Mapping)
                                or metadata.get("schema_version") != ATTEMPT_METADATA_SCHEMA
                                or metadata.get("experiment_job_id") != experiment_id
                                or metadata.get("eval_id") != run["eval_id"]
                            ):
                                raise ExperimentError(
                                    "candidate exports have no trusted experiment ownership metadata"
                                )
                            allowed = {
                                CANDIDATE_EXPORT_NAME,
                                CANDIDATE_EXPORT_NAME.removesuffix(".tar.zst") + ".tar.gz",
                            }
                            exports = metadata.get("candidate_exports", [])
                            if not isinstance(exports, list):
                                raise ExperimentError("candidate export metadata is not a list")
                            for export in exports:
                                if not isinstance(export, Mapping) or not isinstance(
                                    export.get("path"), str
                                ):
                                    raise ExperimentError("candidate export metadata is malformed")
                                path = _path(archive, Path(export["path"]))
                                if path.parent != attempt_dir / "agent" or path.name not in allowed:
                                    raise ExperimentError(
                                        f"refusing to prune an unowned candidate export: {path}"
                                    )
                                if not path.exists():
                                    continue
                                if not path.is_file() or sha256_file(path) != export.get("sha256"):
                                    raise ExperimentError(f"candidate export changed after execution: {path}")
                                path.unlink()
                                state["artifacts_pruned"] = True
                                report["removed"].append(str(path))
                    except (
                        ExperimentError, TaskVerificationError, GhostlabError, LockUnavailableError, OSError,
                    ) as exc:
                        report["errors"].append({"path": str(attempt_dir), "error": str(exc)})
        state["cleanup"] = report
        state.setdefault("cleanup_history", []).append(report)
        _write_state(archive, setup, state)
        return _view(archive, setup, state)


__all__ = [
    "EXPERIMENT_JOB_ARTIFACTS", "EXPERIMENT_JOB_SCHEMA", "EXPERIMENT_SETUP_SCHEMA", "EXPERIMENT_STATUSES",
    "ExperimentError", "cleanup_experiment", "create_experiment", "list_experiments",
    "load_experiment", "run_experiment",
]
