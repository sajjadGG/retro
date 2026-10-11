"""Framework-neutral repository episodes with private, attested terminal rewards."""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ...utils import atomic_write_text, iter_jsonl
from .build import compute_scorer_package_hash, pack_directory, utc_now
from .ghostlab_cli import (
    GhostlabCli,
    GhostlabError,
    read_json,
    sha256_file,
    sha256_json,
    sha256_path,
    tree_manifest,
    write_json,
)
from .run import CandidateScore, PublishedTask, materialize_task_base, score_candidate
from .schema import (
    BenchmarkTask,
    ProjectEnvironment,
    ScorerManifest,
    _bool,
    _enum,
    _integer,
    _known_keys,
    _mapping,
    _number,
    _string,
    canonical_json,
    require_sha256_hex,
    require_task_id,
)
from .suite import SPLITS, CuratedSuite

EPISODE_SCHEMA = "retro-task-episode-v1"
REWARD_SCHEMA = "retro-episode-reward-v1"
EXPORT_SCHEMA = "retro-rl-task-export-v1"


class EpisodeError(ValueError):
    """An episode or export would violate its data or isolation contract."""


def source_balanced_weights(tasks: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    """Give each source equal sampling mass; priorities only redistribute within it."""
    if not tasks:
        raise EpisodeError("cannot sample an empty task split")
    totals: dict[str, float] = {}
    priorities: dict[str, tuple[str, float]] = {}
    for row in tasks:
        task_id = require_task_id(_string(row, "task_id", "sampling task"), "sampling task id")
        source = _string(row, "source_id", "sampling task")
        priority = _number(row, "priority", "sampling task", minimum=0.01, maximum=1000)
        if task_id in priorities:
            raise EpisodeError("sampling task IDs must be unique")
        priorities[task_id] = (source, priority)
        totals[source] = totals.get(source, 0.0) + priority
    return {
        task_id: priority / totals[source] / len(totals)
        for task_id, (source, priority) in priorities.items()
    }


def _check_output(path: Path, suite: CuratedSuite) -> None:
    resolved = path.resolve()
    if resolved == suite.path.resolve() or suite.path.resolve() in resolved.parents:
        raise EpisodeError("episode output must not modify the immutable suite")
    if path.exists() or path.is_symlink():
        raise EpisodeError(f"output already exists; use a fresh episode/export path: {path}")


def _check_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise EpisodeError("episode seed must be a non-negative integer")


def _observation(task: PublishedTask) -> dict[str, Any]:
    return {
        "schema_version": EPISODE_SCHEMA,
        "task_id": task.task_id,
        "prompt": task.public_task["prompt"],
        "repository": dict(task.public_task["repository"]),
        "environment": dict(task.public_environment),
        "limits": dict(task.public_task["limits"]),
        "scoring": dict(task.public_task["scoring"]),
    }


def materialize_episode(
    suite: CuratedSuite,
    task_id: str,
    output_dir: Path,
    *,
    split: str = "train",
    seed: int = 0,
) -> dict[str, Any]:
    """Reset to the pinned repository; do not execute setup or an agent on the host."""
    _check_seed(seed)
    task = suite.task(task_id, split)
    output_dir = Path(output_dir).absolute()
    _check_output(output_dir, suite)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        materialize_task_base(task, staging / "workspace")
        observation = _observation(task)
        observation.update(
            seed=seed,
            workspace=str(output_dir / "workspace"),
            suite_id=suite.suite_id,
            suite_content_sha256=suite.content_sha256,
            split=split,
        )
        write_json(staging / "observation.json", observation)
        shutil.copyfile(task.public_environment_path, staging / "environment.json")
        shutil.copyfile(task.prompt_path, staging / "prompt.txt")
        os.rename(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return observation


def score_episode(
    suite: CuratedSuite,
    task_id: str,
    candidate: Path,
    output_dir: Path,
    *,
    split: str = "train",
    seed: int = 0,
    ghostlab: GhostlabCli | None = None,
    ghostlab_bin: str | Path | None = None,
    trace_path: Path | None = None,
    resource_usage_path: Path | None = None,
) -> dict[str, Any]:
    """Grade a terminal candidate through the same isolated scorer as benchmark runs."""
    _check_seed(seed)
    task = suite.task(task_id, split)
    candidate = Path(candidate).resolve()
    if not candidate.exists():
        raise EpisodeError(f"candidate does not exist: {candidate}")
    output_dir = Path(output_dir).absolute()
    _check_output(output_dir, suite)
    if candidate.is_dir() and (
        output_dir.resolve() == candidate or candidate in output_dir.resolve().parents
    ):
        raise EpisodeError("reward output must not modify its read-only candidate")
    if ghostlab is not None and ghostlab_bin is not None:
        raise EpisodeError("pass ghostlab or ghostlab_bin, not both")
    client = ghostlab
    version_error: GhostlabError | None = None
    try:
        if client is None:
            client = GhostlabCli(ghostlab_bin)
        version = client.version().fingerprint()
    except GhostlabError as exc:
        version_error = exc
        version = {
            "binary": client.binary if client is not None else str(ghostlab_bin or "ghostlab"),
            "version": None,
            "binary_sha256": None,
        }
    candidate_sha256 = sha256_path(candidate, excludes=())
    input_sha256 = sha256_json(
        {
            "schema_version": REWARD_SCHEMA,
            "suite_content_sha256": suite.content_sha256,
            "task_sha256": task.public_task_sha256,
            "scorer_package_sha256": task.scorer_package_sha256,
            "candidate_sha256": candidate_sha256,
            "trace_sha256": sha256_file(trace_path) if trace_path is not None else None,
            "resources_sha256": sha256_file(resource_usage_path) if resource_usage_path is not None else None,
            "seed": seed,
            "ghostlab": version,
        }
    )
    attempt_id = input_sha256[:32]
    output_dir.mkdir(parents=True)
    scored = (
        CandidateScore(status="harness_error", error=str(version_error))
        if version_error is not None or client is None
        else score_candidate(
            task, client, candidate, attempt_id=attempt_id, output_dir=output_dir,
            seed=seed, trace_path=trace_path, resource_usage_path=resource_usage_path,
        )
    )
    reward = {
        "schema_version": REWARD_SCHEMA,
        "task_id": task_id,
        "suite_id": suite.suite_id,
        "suite_content_sha256": suite.content_sha256,
        "split": split,
        "seed": seed,
        "attempt_id": attempt_id,
        "input_sha256": input_sha256,
        "candidate_sha256": candidate_sha256,
        "scorer_package_sha256": task.scorer_package_sha256,
        "ghostlab": version,
        "status": scored.status,
        "reward": scored.score,
        "passed": scored.passed,
        "terminated": scored.status == "scored",
        "truncated": scored.status != "scored",
        "components": list(scored.components),
        "error": scored.error,
        "warnings": list(scored.warnings),
        "score_report": "score-report.json" if scored.report_path is not None else None,
        "created_at": utc_now(),
    }
    write_json(output_dir / "episode-reward.json", reward)
    return reward


def _export_files(path: Path) -> list[dict[str, Any]]:
    return [row for row in tree_manifest(path, excludes=()) if row["path"] != "manifest.json"]


def verify_export(path: Path, *, expected_input_sha256: str | None = None) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or (path / "manifest.json").is_symlink():
        raise EpisodeError("task exports must not use symlinked manifests")
    manifest = read_json(path / "manifest.json", label="RL task export")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != EXPORT_SCHEMA
        or manifest.get("content_sha256") != sha256_json(
            {key: value for key, value in manifest.items() if key != "content_sha256"}
        )
        or manifest.get("files") != _export_files(path)
    ):
        raise EpisodeError("RL task export failed its manifest/file integrity checks")
    if expected_input_sha256 is not None and manifest.get("input_sha256") != expected_input_sha256:
        raise EpisodeError(
            "export path is immutable; use a new path for a changed suite, split or reward policy"
        )
    _known_keys(
        manifest,
        (
            "schema_version", "name", "suite_id", "suite_content_sha256", "split", "task_count",
            "include_rewards", "input_sha256", "required_images", "images_bundled",
            "episodes_sha256", "files", "created_at", "content_sha256",
        ),
        "RL export",
    )
    for key in ("suite_content_sha256", "input_sha256", "episodes_sha256", "content_sha256"):
        require_sha256_hex(_string(manifest, key, "RL export"), f"RL export.{key}")
    split = _enum(manifest, "split", SPLITS, "RL export")
    count = _integer(manifest, "task_count", "RL export", minimum=1)
    private = _bool(manifest, "include_rewards", "RL export")
    if _bool(manifest, "images_bundled", "RL export"):
        raise EpisodeError("this export schema carries pinned image references, not image layers")
    if manifest["episodes_sha256"] != sha256_file(path / "episodes.jsonl"):
        raise EpisodeError("episode stream hash does not match its manifest")
    rows = [row for _line, row in iter_jsonl(path / "episodes.jsonl", strict=True)]
    task_ids = [
        require_task_id(_string(row, "task_id", "episode"), "episode task id") for row in rows
    ]
    if len(rows) != count or len(set(task_ids)) != count:
        raise EpisodeError("episode count or unique task IDs do not match the export")
    images: set[str] = set()
    for row in rows:
        task_id = require_task_id(_string(row, "task_id", "episode"), "episode task id")
        public = path / "tasks" / task_id / "public"
        task = BenchmarkTask.from_dict(read_json(public / "task.json", label="exported public task"))
        environment = ProjectEnvironment.from_dict(
            read_json(public / "environment.json", label="exported environment")
        )
        expected_paths = {
            key: f"tasks/{task_id}/public/{filename}"
            for key, filename in (
                ("task", "task.json"), ("prompt", "prompt.txt"),
                ("environment", "environment.json"), ("base_bundle", "base.bundle"),
            )
        }
        if (
            row.get("schema_version") != EPISODE_SCHEMA
            or row.get("split") != split
            or row.get("suite_id") != manifest["suite_id"]
            or row.get("paths") != expected_paths
            or row.get("prompt") != task.prompt
            or row.get("repository") != task.repository
            or row.get("environment") != environment.to_dict()
            or row.get("public_task_sha256") != sha256_file(public / "task.json")
            or row.get("base_bundle_sha256") != sha256_file(public / "base.bundle")
        ):
            raise EpisodeError(f"episode {task_id} does not match its public task inputs")
        images.add(environment.image)
    if manifest.get("required_images") != sorted(images):
        raise EpisodeError("required image digests do not match the episode environments")
    expected_top = {"manifest.json", "episodes.jsonl", "tasks"} | ({"private"} if private else set())
    if {entry.name for entry in path.iterdir()} != expected_top:
        raise EpisodeError("export contains unexpected public/private artifacts")
    if private:
        rewards = _mapping(
            read_json(path / "private" / "reward-manifest.json", label="reward manifest"),
            "reward manifest",
        )
        reward_rows = rewards.get("tasks")
        if (
            rewards.get("schema_version") != EXPORT_SCHEMA
            or rewards.get("suite_content_sha256") != manifest["suite_content_sha256"]
            or rewards.get("split") != split
            or not isinstance(reward_rows, list)
            or len(reward_rows) != count
        ):
            raise EpisodeError("private reward manifest does not match its public episodes")
        reward_ids: set[str] = set()
        for reward_row in reward_rows:
            reward_row = _mapping(reward_row, "private reward task")
            task_id = require_task_id(_string(reward_row, "task_id", "reward task"), "reward task id")
            package = path / "private" / "rewards" / task_id
            scorer = ScorerManifest.from_dict(
                read_json(package / "scorer.json", label="exported scorer")
            )
            if (
                task_id in reward_ids
                or task_id not in task_ids
                or scorer.task_id != task_id
                or reward_row.get("scorer") != f"private/rewards/{task_id}/scorer.json"
                or reward_row.get("scorer_package_sha256") != compute_scorer_package_hash(package)
            ):
                raise EpisodeError("private reward package identity/hash is invalid")
            reward_ids.add(task_id)
    return manifest


def export_episodes(
    suite: CuratedSuite,
    output_dir: Path,
    *,
    split: str = "train",
    include_rewards: bool = False,
) -> dict[str, Any]:
    """Export actor inputs separately from optional trusted-harness verifier packages."""
    if not isinstance(include_rewards, bool):
        raise EpisodeError("include_rewards must be a boolean")
    task_ids = suite.task_ids(split)
    if not task_ids:
        raise EpisodeError(
            f"suite {suite.suite_id} has no {split} tasks; choose an explicit populated split"
        )
    input_sha256 = sha256_json(
        {
            "schema_version": EXPORT_SCHEMA,
            "suite_content_sha256": suite.content_sha256,
            "split": split,
            "include_rewards": include_rewards,
        }
    )
    output_dir = Path(output_dir).absolute()
    if output_dir.exists():
        return verify_export(output_dir, expected_input_sha256=input_sha256)
    _check_output(output_dir, suite)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        episodes: list[dict[str, Any]] = []
        rewards: list[dict[str, Any]] = []
        images: set[str] = set()
        metadata = {
            row["task_id"]: row for row in suite.manifest["tasks"] if row["task_id"] in task_ids
        }
        weights = source_balanced_weights(list(metadata.values()))
        for task_id in task_ids:
            task = suite.task(task_id, split)
            public = staging / "tasks" / task_id / "public"
            public.mkdir(parents=True)
            for name in ("task.json", "prompt.txt", "environment.json"):
                shutil.copyfile(task.task_dir / "public" / name, public / name)
            with tempfile.TemporaryDirectory(prefix=".base.", dir=staging) as scratch:
                workspace = materialize_task_base(task, Path(scratch) / "repo")
                pack_directory(workspace, public / "base.bundle")
            row = _observation(task)
            row.update(
                suite_id=suite.suite_id,
                split=split,
                paths={
                    name: (public / filename).relative_to(staging).as_posix()
                    for name, filename in (
                        ("task", "task.json"), ("prompt", "prompt.txt"),
                        ("environment", "environment.json"), ("base_bundle", "base.bundle"),
                    )
                },
                base_bundle_sha256=sha256_file(public / "base.bundle"),
                source_base_bundle_sha256=task.base_bundle_sha256,
                public_task_sha256=task.public_task_sha256,
                sampling_weight=weights[task_id],
                curation={
                    key: metadata[task_id][key]
                    for key in (
                        "source_id", "family", "tags", "priority", "goal_summary",
                        "provenance_grade", "curation_reason",
                    )
                },
            )
            episodes.append(row)
            images.add(task.environment_image)
            if include_rewards:
                private = staging / "private" / "rewards" / task_id
                shutil.copytree(task.scorer_manifest_path.parent, private)
                rewards.append(
                    {
                        "task_id": task_id,
                        "scorer": (private / "scorer.json").relative_to(staging).as_posix(),
                        "scorer_package_sha256": task.scorer_package_sha256,
                    }
                )
        atomic_write_text(
            staging / "episodes.jsonl", "".join(canonical_json(row) + "\n" for row in episodes)
        )
        if include_rewards:
            write_json(
                staging / "private" / "reward-manifest.json",
                {
                    "schema_version": EXPORT_SCHEMA,
                    "suite_content_sha256": suite.content_sha256,
                    "split": split,
                    "tasks": rewards,
                },
            )
        manifest: dict[str, Any] = {
            "schema_version": EXPORT_SCHEMA,
            "name": suite.manifest["name"],
            "suite_id": suite.suite_id,
            "suite_content_sha256": suite.content_sha256,
            "split": split,
            "task_count": len(episodes),
            "include_rewards": include_rewards,
            "input_sha256": input_sha256,
            "required_images": sorted(images),
            "images_bundled": False,
            "episodes_sha256": sha256_file(staging / "episodes.jsonl"),
            "files": _export_files(staging),
            "created_at": utc_now(),
        }
        manifest["content_sha256"] = sha256_json(manifest)
        write_json(staging / "manifest.json", manifest)
        os.rename(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return verify_export(output_dir, expected_input_sha256=input_sha256)
