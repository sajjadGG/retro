"""Frozen, evidence-gated task suites and leakage-aware train/evaluation splits."""
from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .build import (
    BASE_MAX_TOTAL,
    CONSTRUCT_CHANGING_MIN_DROP,
    CONSTRUCT_PRESERVING_MAX_DELTA,
    ORACLE_MIN_TOTAL,
    PUBLIC_TASK_FILES,
    REQUIRED_VALIDATION_KINDS,
    SCORER_VALIDATION_SCHEMA,
    TasksetPaths,
    list_published_tasks,
    resolve_taskset_paths,
    utc_now,
)
from .ghostlab_cli import GhostlabError, read_json, sha256_json, sha256_path, write_json
from .run import PublishedTask, TaskVerificationError, materialize_task_base, verify_published_task
from .schema import (
    SchemaError,
    _bool,
    _enum,
    _integer,
    _known_keys,
    _mapping,
    _number,
    _string,
    _string_list,
    normalize_prompt,
    require_sha256_hex,
    require_task_id,
)
from .selection import require_safe_id

SUITE_SCHEMA = "retro-benchmark-suite-v1"
CURATION_POLICY_SCHEMA = "retro-task-curation-policy-v1"
CURATION_REPORT_SCHEMA = "retro-task-curation-v1"
COHORT_SCHEMA = "retro-eval-cohort-v1"
SPLITS = ("train", "validation", "test")
NEAR_DUPLICATE_THRESHOLD = 0.85
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class SuiteError(ValueError):
    """A task release, curation policy, or evaluation cohort is not trustworthy."""


@dataclass(frozen=True)
class TaskOverride:
    include: bool = True
    family: str | None = None
    tags: tuple[str, ...] = ()
    priority: float = 1.0
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.include, bool):
            raise SuiteError("task inclusion must be a boolean")
        if self.family is not None and (
            not isinstance(self.family, str) or not self.family.strip()
        ):
            raise SuiteError("task family must be a non-empty string or null")
        if not isinstance(self.tags, (tuple, list)) or any(
            not isinstance(tag, str) or not tag.strip() for tag in self.tags
        ):
            raise SuiteError("task tags must be non-empty strings")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, (int, float))
            or not math.isfinite(self.priority)
            or not 0.01 <= self.priority <= 1000
        ):
            raise SuiteError("sampling priority must be finite and in [0.01, 1000]")
        if not isinstance(self.reason, str):
            raise SuiteError("curation reason must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "include": self.include,
            "family": self.family,
            "tags": list(self.tags),
            "priority": float(self.priority),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Any, where: str) -> TaskOverride:
        data = _mapping(value, where)
        _known_keys(data, ("include", "family", "tags", "priority", "reason"), where)
        family = data.get("family")
        if family is not None and (not isinstance(family, str) or not family.strip()):
            raise SchemaError(f"{where}.family must be a non-empty string or null")
        result = cls(
            include=_bool(data, "include", where, default=True),
            family=family,
            tags=tuple(_string_list(data, "tags", where)) if "tags" in data else (),
            priority=_number(data, "priority", where, minimum=0.01, maximum=1000)
            if "priority" in data else 1.0,
            reason=_string(data, "reason", where) if "reason" in data else "",
        )
        if not result.reason:
            raise SchemaError(f"{where}.reason is required for an explicit curation decision")
        return result


@dataclass(frozen=True)
class CurationPolicy:
    split_unit: str = "repository"
    split_ratios: Mapping[str, float] = field(
        default_factory=lambda: {"train": 0.0, "validation": 0.0, "test": 1.0}
    )
    seed: int = 0
    min_confidence: float = 0.8
    include_judged: bool = False
    allow_reconstructed: bool = False
    overrides: Mapping[str, TaskOverride] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.split_unit not in ("repository", "source"):
            raise SuiteError("split_unit must be repository or source")
        if not isinstance(self.split_ratios, Mapping) or set(self.split_ratios) != set(SPLITS):
            raise SuiteError(f"split_ratios must contain exactly {SPLITS}")
        for value in self.split_ratios.values():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise SuiteError("split ratios must be finite numbers in [0, 1]")
        if abs(sum(self.split_ratios.values()) - 1.0) > 1e-9:
            raise SuiteError("split ratios must sum to 1")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise SuiteError("seed must be a non-negative integer")
        if (
            isinstance(self.min_confidence, bool)
            or not isinstance(self.min_confidence, (int, float))
            or not math.isfinite(self.min_confidence)
            or not 0 <= self.min_confidence <= 1
        ):
            raise SuiteError("min_confidence must be a finite number in [0, 1]")
        if not isinstance(self.include_judged, bool):
            raise SuiteError("include_judged must be a boolean")
        if not isinstance(self.allow_reconstructed, bool):
            raise SuiteError("allow_reconstructed must be a boolean")
        if self.include_judged and self.split_ratios["train"] > 0:
            raise SuiteError("judged or performance rewards are evaluation-only; train must be zero")
        if not isinstance(self.overrides, Mapping):
            raise SuiteError("overrides must be a task-ID mapping")
        for task_id, override in self.overrides.items():
            require_task_id(task_id, "curation override task_id")
            if not isinstance(override, TaskOverride):
                raise SuiteError(f"override {task_id} must be a TaskOverride")
            if not override.reason.strip():
                raise SuiteError(f"override {task_id} must explain its curation decision")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CURATION_POLICY_SCHEMA,
            "split_unit": self.split_unit,
            "split_ratios": {key: float(value) for key, value in self.split_ratios.items()},
            "seed": self.seed,
            "min_confidence": float(self.min_confidence),
            "include_judged": self.include_judged,
            "allow_reconstructed": self.allow_reconstructed,
            "overrides": {
                key: value.to_dict() for key, value in sorted(self.overrides.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Any) -> CurationPolicy:
        data = _mapping(value, "curation_policy")
        if data.get("schema_version") != CURATION_POLICY_SCHEMA:
            raise SchemaError(f"curation policy schema_version must be {CURATION_POLICY_SCHEMA}")
        _known_keys(
            data,
            (
                "schema_version", "split_unit", "split_ratios", "seed",
                "min_confidence", "include_judged", "allow_reconstructed", "overrides",
            ),
            "curation_policy",
        )
        ratios = _mapping(
            data.get("split_ratios", {"train": 0.0, "validation": 0.0, "test": 1.0}),
            "curation_policy.split_ratios",
        )
        _known_keys(ratios, SPLITS, "curation_policy.split_ratios")
        overrides = _mapping(data.get("overrides", {}), "curation_policy.overrides")
        return cls(
            split_unit=_enum(data, "split_unit", ("repository", "source"), "curation_policy")
            if "split_unit" in data else "repository",
            split_ratios={
                split: _number(ratios, split, "split_ratios", minimum=0, maximum=1)
                for split in SPLITS
            },
            seed=_integer(data, "seed", "curation_policy", minimum=0) if "seed" in data else 0,
            min_confidence=_number(data, "min_confidence", "curation_policy", minimum=0, maximum=1)
            if "min_confidence" in data else 0.8,
            include_judged=_bool(data, "include_judged", "curation_policy", default=False),
            allow_reconstructed=_bool(data, "allow_reconstructed", "curation_policy", default=False),
            overrides={
                task_id: TaskOverride.from_dict(override, f"overrides.{task_id}")
                for task_id, override in overrides.items()
            },
        )


def load_policy(path: Path | None) -> CurationPolicy:
    return CurationPolicy.from_dict(read_json(path, label="curation policy")) if path else CurationPolicy()


def _evidence_failure(task: PublishedTask, policy: CurationPolicy) -> tuple[str, str] | None:
    provenance = task.provenance
    if provenance.get("scorer_package_sha256") != task.scorer_package_sha256:
        return (
            "LEGACY_SCORER_PROVENANCE",
            "release provenance must use the verified canonical scorer hash; republish a new task version",
        )
    if not task.source_id:
        return "MISSING_TASK_EVIDENCE", "publication has no source identity"
    if {item.name for item in (task.task_dir / "public").iterdir()} != set(PUBLIC_TASK_FILES):
        return "UNSAFE_PUBLIC_ARTIFACT", "public task files differ from the agent-input allowlist"
    definition = provenance.get("task_definition")
    if not isinstance(definition, Mapping):
        return "MISSING_TASK_EVIDENCE", "publication has no task definition"
    if (
        definition.get("prompt") != task.public_task.get("prompt")
        or task.prompt_path.read_text(encoding="utf-8") != task.public_task.get("prompt")
    ):
        return "PROMPT_EVIDENCE_MISMATCH", "public prompt differs from its published task definition"
    source_link = read_json(task.task_dir / "private" / "source-link.json", label="task source link")
    if not isinstance(source_link, Mapping) or source_link.get("source_id") != task.source_id:
        return "MISSING_TASK_EVIDENCE", "source link does not match the publication"
    repo = source_link.get("repo")
    if not isinstance(repo, Mapping):
        return "MISSING_TASK_EVIDENCE", "source link has no Git anchor"
    if repo.get("state_confidence") != "exact_clean_commit" and not (
        policy.allow_reconstructed
        and repo.get("state_confidence") == "approximate"
        and repo.get("base_resolution") == "first_commit_parent"
    ):
        return (
            "APPROXIMATE_START_STATE",
            "exact replay needs clean start proof; known-parent reconstructions require explicit opt-in",
        )
    for key in ("base_sha", "base_tree", "repo_id"):
        if repo.get(key) != task.public_task["repository"].get(key):
            return "SOURCE_EVIDENCE_MISMATCH", f"source repository {key} differs from the public task"
    confidence = definition.get("confidence")
    if not isinstance(confidence, Mapping):
        return "MISSING_TASK_EVIDENCE", "task has no goal/state/scorability confidence record"
    for key in ("goal", "state", "scorability"):
        value = confidence.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not policy.min_confidence <= value <= 1
        ):
            return "LOW_TASK_CONFIDENCE", f"{key} confidence is below {policy.min_confidence}"
    if not policy.include_judged and (
        task.scorer_manifest.get("mode") != "deterministic"
        or any(component.get("kind") != "deterministic" for component in task.scorer_manifest["components"])
    ):
        return "NONDETERMINISTIC_REWARD", "judged/performance scorers require an evaluation-only policy"
    validation = read_json(
        task.task_dir / "private" / "scorer-validation.json", label="scorer validation"
    )
    if (
        not isinstance(validation, Mapping)
        or validation.get("schema_version") != SCORER_VALIDATION_SCHEMA
        or validation.get("task_id") != task.task_id
        or validation.get("passed") is not True
        or validation.get("codes") != []
        or validation.get("scorer_package_sha256") != task.scorer_package_sha256
        or sha256_json(validation) != sha256_json(provenance.get("validation"))
    ):
        return "INVALID_SCORER_VALIDATION", "validation is missing, failed or not bound to this scorer"
    repeatability = validation.get("repeatability")
    if (
        not isinstance(repeatability, Mapping)
        or repeatability.get("ok") is not True
        or repeatability.get("deterministic_mismatches") != []
    ):
        return "SCORER_NONDETERMINISTIC", "scorer repeatability did not pass"
    isolation = validation.get("isolation_attestation")
    if (
        not isinstance(isolation, Mapping)
        or isolation.get("schema_version") != "ghostlab-scorer-isolation-v1"
        or isolation.get("scorer_launcher") != "landlock"
        or isolation.get("candidate_mount") != "read_only"
        or isolation.get("secure_exec_available") is not True
    ):
        return (
            "UNATTESTED_SCORER",
            "validation must attest read-only candidates and isolated scorer execution",
        )
    cases = validation.get("cases")
    if not isinstance(cases, list):
        return "INVALID_SCORER_VALIDATION", "mandatory validation cases are missing"
    kinds: set[str] = set()
    for case in cases:
        if not isinstance(case, Mapping):
            return "INVALID_SCORER_VALIDATION", "malformed validation case"
        runs = case.get("runs")
        score = case.get("score_total")
        if (
            case.get("ok") is not True
            or case.get("status") != "scored"
            or isinstance(runs, bool)
            or not isinstance(runs, int)
            or runs < 2
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or not 0 <= score <= 1
            or not isinstance(case.get("kind"), str)
            or case["kind"] not in REQUIRED_VALIDATION_KINDS
            or not isinstance(case.get("passed"), bool)
        ):
            return "INVALID_SCORER_VALIDATION", "every validation case must pass at least two scored runs"
        kinds.add(case["kind"])
    if not set(REQUIRED_VALIDATION_KINDS).issubset(kinds):
        return "INVALID_SCORER_VALIDATION", "base/oracle/no-op/mutant/regression cases are required"
    oracle = next(case for case in cases if case["kind"] == "oracle")
    base = next(case for case in cases if case["kind"] == "base")
    component_ids = {component["id"] for component in task.scorer_manifest["components"]}
    gate_ids = {
        component["id"] for component in task.scorer_manifest["components"]
        if component["hard_gate"]
    }
    for case in cases:
        score = case["score_total"]
        failures = case.get("hard_gate_failures")
        values = case.get("component_values")
        if (
            not isinstance(failures, list)
            or any(not isinstance(gate, str) or gate not in gate_ids for gate in failures)
            or not isinstance(values, Mapping)
            or set(values) != component_ids
        ):
            return "INVALID_SCORER_VALIDATION", "validation gates/components do not match the scorer"
        for value in values.values():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                return "INVALID_SCORER_VALIDATION", "invalid validation component value"
    for case in cases:
        score = case["score_total"]
        failures = case["hard_gate_failures"]
        values = case["component_values"]
        kind = case["kind"]
        if kind == "base" and (
            score > BASE_MAX_TOTAL or not failures or case.get("passed") is not False
        ):
            return "BASE_ALREADY_PASSES", "base must fail a requested hard gate and score at most 0.20"
        if kind == "oracle" and (
            score < ORACLE_MIN_TOTAL or failures or case.get("passed") is not True
        ):
            return "ORACLE_DOES_NOT_PASS", "oracle must pass every gate and score at least 0.90"
        if kind == "no_op" and (
            case.get("passed") is not False or abs(score - base["score_total"]) > 1e-9
        ):
            return "SCORER_OVERFIT", "a no-op must reproduce the failing deterministic base score"
        if kind == "construct_changing":
            component = case.get("targeted_component")
            if not isinstance(component, str) or component not in component_ids:
                return "INVALID_SCORER_VALIDATION", "mutant summary must identify its targeted component"
            before, after = oracle["component_values"].get(component), values.get(component)
            if before is None or after is None or before - after < CONSTRUCT_CHANGING_MIN_DROP:
                return "SCORER_OVERFIT", "construct-changing mutant must drop its targeted component by 0.50"
        if kind == "construct_preserving" and (
            abs(score - oracle["score_total"]) > CONSTRUCT_PRESERVING_MAX_DELTA
        ):
            return "SCORER_OVERFIT", "construct-preserving mutant moved the score by more than 0.05"
        if kind == "regression":
            component = case.get("targeted_component")
            if component not in failures or score != 0:
                return "SCORER_OVERFIT", "regression mutant must fail its targeted gate with total zero"
    audit = provenance.get("audit")
    if (
        not isinstance(audit, Mapping)
        or audit.get("decision") != "accept"
        or audit.get("skipped") is True
        or audit.get("leakage") != []
        or audit.get("missing_observables") != []
    ):
        return "UNAUDITED_SCORER", "an accepting, non-skipped independent scorer audit is required"
    return None


def _shingles(prompt: str) -> frozenset[tuple[str, ...]]:
    tokens = _TOKEN_RE.findall(normalize_prompt(prompt).lower())
    return frozenset(tuple(tokens[index:index + 3]) for index in range(len(tokens) - 2))


def group_tasks(
    tasks: Sequence[Mapping[str, Any]], policy: CurationPolicy
) -> dict[str, str]:
    """Keep sources, explicit families, duplicates and optionally repositories together."""
    parent = {str(task["task_id"]): str(task["task_id"]) for task in tasks}

    def find(task_id: str) -> str:
        while parent[task_id] != task_id:
            parent[task_id] = parent[parent[task_id]]
            task_id = parent[task_id]
        return task_id

    def join(left: str, right: str) -> None:
        roots = sorted((find(left), find(right)))
        parent[roots[1]] = roots[0]

    anchors: dict[tuple[str, str], str] = {}
    for task in tasks:
        task_id = str(task["task_id"])
        keys = [("source", str(task["source_id"])), ("family", str(task["family"]))]
        if policy.split_unit == "repository":
            keys.append(("repository", str(task["repo_id"])))
        for key in keys:
            if key in anchors:
                join(task_id, anchors[key])
            else:
                anchors[key] = task_id
    shingles = {str(task["task_id"]): _shingles(str(task["prompt"])) for task in tasks}
    for index, left in enumerate(tasks):
        for right in tasks[index + 1:]:
            if left["repo_id"] != right["repo_id"]:
                continue
            left_id, right_id = str(left["task_id"]), str(right["task_id"])
            a, b = shingles[left_id], shingles[right_id]
            identical = normalize_prompt(str(left["prompt"])) == normalize_prompt(str(right["prompt"]))
            if identical or (
                min(len(a), len(b)) >= 5
                and len(a & b) / len(a | b) >= NEAR_DUPLICATE_THRESHOLD
            ):
                join(left_id, right_id)
    members: dict[str, list[str]] = {}
    for task_id in parent:
        members.setdefault(find(task_id), []).append(task_id)
    group_ids = {
        root: sha256_json(sorted(task_ids))[:20] for root, task_ids in members.items()
    }
    return {task_id: group_ids[find(task_id)] for task_id in parent}


def assign_splits(
    groups: Sequence[str], ratios: Mapping[str, float], seed: int
) -> dict[str, str]:
    """Allocate whole groups by largest remainder, reserving each requested split."""
    CurationPolicy(split_ratios=ratios, seed=seed)
    ordered = sorted(set(groups), key=lambda group: sha256_json([seed, group]))
    positive = [split for split in SPLITS if ratios[split] > 0]
    if len(ordered) < len(positive):
        raise SuiteError(
            f"{len(ordered)} independent groups cannot populate {len(positive)} requested splits; "
            "use an explicit evaluation-only policy or collect more independent sources/repositories"
        )
    quotas = {split: len(ordered) * ratios[split] for split in SPLITS}
    counts = {split: math.floor(quotas[split]) for split in SPLITS}
    remaining = len(ordered) - sum(counts.values())
    rank = sorted(positive, key=lambda split: (-(quotas[split] - counts[split]), SPLITS.index(split)))
    for split in rank[:remaining]:
        counts[split] += 1
    for split in positive:
        if counts[split] == 0:
            donors = [name for name in positive if counts[name] > 1]
            donor = max(donors, key=lambda name: (counts[name] - quotas[name], counts[name]))
            counts[donor] -= 1
            counts[split] = 1
    result: dict[str, str] = {}
    offset = 0
    for split in SPLITS:
        for group in ordered[offset:offset + counts[split]]:
            result[group] = split
        offset += counts[split]
    return result


@dataclass(frozen=True)
class CuratedSuite:
    path: Path
    manifest: dict[str, Any]

    @property
    def suite_id(self) -> str:
        return str(self.manifest["suite_id"])

    @property
    def content_sha256(self) -> str:
        return str(self.manifest["content_sha256"])

    @property
    def paths(self) -> TasksetPaths:
        return TasksetPaths(root=self.path, name=str(self.manifest["name"]))

    def task_ids(self, split: str | None = None) -> list[str]:
        if split is not None and split not in SPLITS:
            raise SuiteError(f"split must be one of {SPLITS}")
        return sorted(
            row["task_id"] for row in self.manifest["tasks"]
            if split is None or row["split"] == split
        )

    def task(self, task_id: str, split: str | None = None) -> PublishedTask:
        require_task_id(task_id, "episode task id")
        if _manifest_hash(self.manifest) != self.content_sha256:
            raise SuiteError("suite manifest changed after it was loaded")
        if task_id not in self.task_ids(split):
            raise SuiteError(f"task {task_id} is not in the requested suite split")
        row = next(row for row in self.manifest["tasks"] if row["task_id"] == task_id)
        if sha256_path(self.paths.task_dir(task_id), excludes=()) != row["content_sha256"]:
            raise SuiteError(f"curated task {task_id} changed after publication")
        return verify_published_task(self.paths, task_id)


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    return sha256_json({key: value for key, value in manifest.items() if key != "content_sha256"})


def _suite_summary(
    tasks: Sequence[Mapping[str, Any]], rejections: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "tasks": len(tasks),
        "sources": len({row["source_id"] for row in tasks}),
        "repositories": len({row["repo_id"] for row in tasks}),
        "groups": len({row["group_id"] for row in tasks}),
        "splits": {split: sum(row["split"] == split for row in tasks) for split in SPLITS},
        "rejections": dict(Counter(row["code"] for row in rejections)),
    }


def _suite_path(paths: TasksetPaths, suite_id: str) -> Path:
    require_safe_id(suite_id, "suite id")
    path = paths.root / "suites" / suite_id
    if path.is_symlink() or (paths.root.resolve() not in path.resolve().parents):
        raise SuiteError("suite path escapes its taskset")
    return path


def _write_curation_report(
    paths: TasksetPaths,
    policy: CurationPolicy,
    task_ids: Sequence[str],
    accepted: Sequence[Mapping[str, Any]],
    rejections: Sequence[Mapping[str, Any]],
    *,
    status: str,
    suite_id: str | None = None,
    error: str | None = None,
) -> None:
    write_json(
        paths.root / "curation.json",
        {
            "schema_version": CURATION_REPORT_SCHEMA,
            "name": paths.name,
            "created_at": utc_now(),
            "policy": policy.to_dict(),
            "status": status,
            "suite_id": suite_id,
            "candidate_tasks": len(task_ids),
            "accepted_task_ids": sorted(row["task_id"] for row in accepted),
            "rejections": list(rejections),
            "error": error,
        },
    )


def load_suite(paths: TasksetPaths, suite_id: str) -> CuratedSuite:
    path = _suite_path(paths, suite_id)
    try:
        if (path / "manifest.json").is_symlink() or (path / "tasks").is_symlink():
            raise SuiteError("curated suite cannot use symlinked artifacts")
        manifest = read_json(path / "manifest.json", label="curated suite manifest")
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != SUITE_SCHEMA
            or manifest.get("suite_id") != suite_id
            or manifest.get("name") != paths.name
            or manifest.get("content_sha256") != _manifest_hash(manifest)
        ):
            raise SuiteError("curated suite manifest identity or content hash is invalid")
        policy = CurationPolicy.from_dict(manifest.get("policy"))
        rows = manifest.get("tasks")
        if not isinstance(rows, list) or not rows:
            raise SuiteError("curated suite must contain tasks")
        ids: set[str] = set()
        splits_by_group: dict[str, str] = {}
        grouping_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise SuiteError("curated suite contains a malformed task row")
            task_id = require_task_id(_string(row, "task_id", "suite task"), "suite task_id")
            if task_id in ids or row.get("split") not in SPLITS:
                raise SuiteError("curated suite has duplicate task IDs or an invalid split")
            group = _string(row, "group_id", "suite task")
            _number(row, "priority", "suite task", minimum=0.01, maximum=1000)
            _string_list(row, "tags", "suite task")
            family = _string(row, "family", "suite task")
            _enum(
                row, "provenance_grade",
                ("exact_replay", "anchored_reconstruction", "adjacent_synthesis"), "suite task",
            )
            split = str(row["split"])
            if group in splits_by_group and splits_by_group[group] != split:
                raise SuiteError("curated suite splits a related task group")
            splits_by_group[group] = split
            ids.add(task_id)
            task_dir = path / "tasks" / task_id
            if any(item.is_symlink() for item in task_dir.rglob("*")) or task_dir.is_symlink():
                raise SuiteError("curated task snapshots must not contain symlinks")
            if row.get("content_sha256") != sha256_path(task_dir, excludes=()):
                raise SuiteError(f"curated task {task_id} changed after publication")
            task = verify_published_task(TasksetPaths(root=path, name=paths.name), task_id)
            failure = _evidence_failure(task, policy)
            if failure:
                raise SuiteError(f"curated task {task_id}: {failure[0]}: {failure[1]}")
            for key, value in (
                ("public_task_sha256", task.public_task_sha256),
                ("base_bundle_sha256", task.base_bundle_sha256),
                ("scorer_package_sha256", task.scorer_package_sha256),
                ("publication_sha256", task.provenance.get("publication_sha256")),
                ("source_id", task.source_id),
                ("repo_id", task.public_task["repository"]["repo_id"]),
            ):
                if row.get(key) != value:
                    raise SuiteError(f"curated task {task_id} {key} does not match its snapshot")
            source_link = read_json(
                task.task_dir / "private" / "source-link.json", label="frozen source link"
            )
            grade = (
                "adjacent_synthesis" if task.public_task["kind"] == "adjacent"
                else "exact_replay" if source_link["repo"]["state_confidence"] == "exact_clean_commit"
                else "anchored_reconstruction"
            )
            if row["provenance_grade"] != grade:
                raise SuiteError(f"task {task_id} provenance grade disagrees with its source evidence")
            grouping_rows.append(
                {
                    "task_id": task_id, "source_id": task.source_id,
                    "repo_id": task.public_task["repository"]["repo_id"],
                    "family": family, "prompt": task.public_task["prompt"],
                    "group_id": group,
                }
            )
        related = group_tasks(grouping_rows, policy)
        group_roots: dict[str, str] = {}
        for row in grouping_rows:
            expected = related[row["task_id"]]
            if expected in group_roots and group_roots[expected] != row["group_id"]:
                raise SuiteError("suite divides a related source, repository, family or duplicate group")
            group_roots[expected] = row["group_id"]
        if assign_splits(list(splits_by_group), policy.split_ratios, policy.seed) != splits_by_group:
            raise SuiteError("suite split assignment does not match its policy and seed")
        rejections = manifest.get("rejections")
        if not isinstance(rejections, list) or any(not isinstance(row, Mapping) for row in rejections):
            raise SuiteError("suite rejection records must be an array of objects")
        for rejection in rejections:
            _string(rejection, "task_id", "suite rejection")
            _string(rejection, "code", "suite rejection")
            _string(rejection, "detail", "suite rejection")
        if manifest.get("summary") != _suite_summary(rows, rejections):
            raise SuiteError("suite summary does not match its task cohort")
        if manifest.get("input_sha256") != sha256_json(
            {"name": paths.name, "policy": policy.to_dict(), "tasks": rows, "rejections": rejections}
        ):
            raise SuiteError("suite input fingerprint does not match its frozen records")
        if {item.name for item in path.iterdir()} != {"manifest.json", "tasks"}:
            raise SuiteError("curated suite contains unmanifested artifacts")
        if {item.name for item in (path / "tasks").iterdir()} != ids:
            raise SuiteError("curated suite contains unmanifested task snapshots")
        return CuratedSuite(path=path, manifest=manifest)
    except (GhostlabError, TaskVerificationError, SchemaError, OSError) as exc:
        raise SuiteError(f"cannot verify curated suite {suite_id}: {exc}") from exc


def curate_taskset(
    layout: Any,
    name: str,
    *,
    suite_id: str | None = None,
    policy: CurationPolicy | None = None,
) -> CuratedSuite:
    paths = resolve_taskset_paths(layout, name)
    resolved_policy = policy if policy is not None else CurationPolicy()
    if not isinstance(resolved_policy, CurationPolicy):
        raise SuiteError("policy must be a CurationPolicy")
    task_ids = list_published_tasks(paths)
    unknown = set(resolved_policy.overrides) - set(task_ids)
    if unknown:
        raise SuiteError(f"curation overrides reference unpublished tasks: {sorted(unknown)}")
    records: list[dict[str, Any]] = []
    tasks: dict[str, PublishedTask] = {}
    rejections: list[dict[str, Any]] = []
    verified_bases: set[tuple[str, str, str]] = set()
    for task_id in task_ids:
        override = resolved_policy.overrides.get(task_id, TaskOverride())
        if not override.include:
            rejections.append({"task_id": task_id, "code": "EXCLUDED_BY_POLICY", "detail": override.reason})
            continue
        try:
            task_dir = paths.task_dir(task_id)
            if task_dir.is_symlink() or any(item.is_symlink() for item in task_dir.rglob("*")):
                raise SuiteError(f"task {task_id} snapshots must not contain symlinks")
            task = verify_published_task(paths, task_id)
            failure = _evidence_failure(task, resolved_policy)
            if failure is None:
                base_key = (
                    task.base_bundle_sha256,
                    task.public_task["repository"]["base_sha"],
                    task.public_task["repository"]["base_tree"],
                )
                if base_key not in verified_bases:
                    with tempfile.TemporaryDirectory(prefix=".verify-base.", dir=paths.root) as scratch:
                        materialize_task_base(task, Path(scratch) / "repo")
                    verified_bases.add(base_key)
        except (SuiteError, TaskVerificationError, GhostlabError, SchemaError, OSError) as exc:
            failure = ("INVALID_PUBLICATION", str(exc))
        if failure:
            rejections.append({"task_id": task_id, "code": failure[0], "detail": failure[1]})
            continue
        tasks[task_id] = task
        definition = task.provenance["task_definition"]
        records.append(
            {
                "task_id": task_id,
                "source_id": task.source_id,
                "repo_id": task.public_task["repository"]["repo_id"],
                "kind": task.public_task["kind"],
                "base_tree": task.public_task["repository"]["base_tree"],
                "prompt": task.public_task["prompt"],
                "family": override.family or task.source_id,
                "tags": list(override.tags),
                "priority": override.priority,
                "curation_reason": (
                    override.reason or "passed Git-state, scorer validation and independent audit"
                ),
                "goal_summary": definition.get("goal_segment", {}).get("summary", ""),
                "provenance_grade": (
                    "adjacent_synthesis" if task.public_task["kind"] == "adjacent"
                    else "anchored_reconstruction" if read_json(
                        task.task_dir / "private" / "source-link.json", label="task source link"
                    )["repo"]["state_confidence"] != "exact_clean_commit"
                    else "exact_replay"
                ),
                "prompt_sha256": task.prompt_sha256,
                "public_task_sha256": task.public_task_sha256,
                "base_bundle_sha256": task.base_bundle_sha256,
                "public_environment_sha256": task.public_environment_sha256,
                "scorer_package_sha256": task.scorer_package_sha256,
                "publication_sha256": task.provenance.get("publication_sha256"),
                "content_sha256": sha256_path(task.task_dir, excludes=()),
            }
        )
    groups = group_tasks(records, resolved_policy)
    deduplicated: list[dict[str, Any]] = []
    duplicate_inputs: dict[tuple[str, str, str, str], str] = {}
    for record in sorted(
        records, key=lambda row: (-row["priority"], row["kind"] != "replay", row["task_id"])
    ):
        key = (
            record["repo_id"], record["base_tree"], normalize_prompt(record["prompt"]),
            record["public_environment_sha256"],
        )
        if key in duplicate_inputs:
            rejections.append(
                {
                    "task_id": record["task_id"],
                    "source_id": record["source_id"],
                    "code": "DUPLICATE_TASK_INPUT",
                    "detail": f"same repository, base, prompt and environment as {duplicate_inputs[key]}",
                    "duplicate_of": duplicate_inputs[key],
                }
            )
        else:
            duplicate_inputs[key] = record["task_id"]
            deduplicated.append(record)
    if not deduplicated:
        counts = dict(Counter(row["code"] for row in rejections))
        error = f"no tasks passed trusted-suite curation; rejections={counts}"
        _write_curation_report(
            paths, resolved_policy, task_ids, deduplicated, rejections, status="rejected", error=error
        )
        raise SuiteError(error)
    try:
        split_by_group = assign_splits(
            [groups[row["task_id"]] for row in deduplicated],
            resolved_policy.split_ratios,
            resolved_policy.seed,
        )
    except SuiteError as exc:
        _write_curation_report(
            paths, resolved_policy, task_ids, deduplicated, rejections, status="rejected", error=str(exc)
        )
        raise
    for record in deduplicated:
        record["group_id"] = groups[record["task_id"]]
        record["split"] = split_by_group[record["group_id"]]
        record["normalized_prompt_sha256"] = sha256_json(normalize_prompt(record.pop("prompt")))
    deduplicated.sort(key=lambda row: row["task_id"])
    rejections.sort(key=lambda row: (row["task_id"], row["code"]))
    input_sha256 = sha256_json(
        {"name": name, "policy": resolved_policy.to_dict(), "tasks": deduplicated, "rejections": rejections}
    )
    resolved_id = suite_id if suite_id is not None else input_sha256[:20]
    target = _suite_path(paths, resolved_id)
    if target.exists():
        existing = load_suite(paths, resolved_id)
        if existing.manifest.get("input_sha256") != input_sha256:
            raise SuiteError(f"suite {resolved_id} is immutable; use a new suite ID for changed inputs")
        _write_curation_report(
            paths, resolved_policy, task_ids, deduplicated, rejections,
            status="curated", suite_id=resolved_id,
        )
        return existing
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{resolved_id}.", dir=target.parent))
    try:
        for record in deduplicated:
            task_id = record["task_id"]
            destination = staging / "tasks" / task_id
            shutil.copytree(tasks[task_id].task_dir, destination)
            if sha256_path(destination, excludes=()) != record["content_sha256"]:
                raise SuiteError(f"task {task_id} changed while freezing the suite")
        warnings = []
        if resolved_policy.split_unit == "source" and len(
            {row["split"] for row in deduplicated}
        ) > 1:
            warnings.append(
                "Source-grouped splits share repository history; "
                "they do not measure repository generalization."
            )
        manifest: dict[str, Any] = {
            "schema_version": SUITE_SCHEMA,
            "name": name,
            "suite_id": resolved_id,
            "created_at": utc_now(),
            "input_sha256": input_sha256,
            "policy": resolved_policy.to_dict(),
            "tasks": deduplicated,
            "rejections": rejections,
            "summary": _suite_summary(deduplicated, rejections),
            "warnings": warnings,
        }
        manifest["content_sha256"] = _manifest_hash(manifest)
        write_json(staging / "manifest.json", manifest)
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    suite = load_suite(paths, resolved_id)
    _write_curation_report(
        paths, resolved_policy, task_ids, deduplicated, rejections,
        status="curated", suite_id=resolved_id,
    )
    return suite


def bind_evaluation_cohort(
    paths: TasksetPaths,
    eval_id: str,
    suite: CuratedSuite,
    split: str,
    task_ids: Sequence[str],
    seeds: Sequence[int],
) -> dict[str, Any]:
    require_safe_id(eval_id, "evaluation id")
    if suite.manifest["name"] != paths.name:
        raise SuiteError("suite belongs to a different taskset")
    if not seeds or any(
        isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in seeds
    ):
        raise SuiteError("cohort seeds must be non-negative integers")
    selected = sorted(set(task_ids))
    if not selected or not set(selected).issubset(suite.task_ids(split)):
        raise SuiteError("evaluation tasks must be a non-empty subset of the requested suite split")
    payload = {
        "schema_version": COHORT_SCHEMA,
        "name": paths.name,
        "suite_id": suite.suite_id,
        "suite_content_sha256": suite.content_sha256,
        "split": split,
        "task_ids": selected,
        "seeds": sorted(set(seeds)),
        "task_sources": {
            row["task_id"]: row["source_id"] for row in suite.manifest["tasks"]
            if row["task_id"] in selected
        },
        "task_artifacts": {
            task_id: {
                "base_bundle_sha256": (task := suite.task(task_id, split)).base_bundle_sha256,
                "scorer_package_sha256": task.scorer_package_sha256,
                "public_task_sha256": task.public_task_sha256,
                "pass_threshold": task.pass_threshold,
            }
            for task_id in selected
        },
    }
    path = paths.eval_dir(eval_id) / "cohort.json"
    if path.is_file():
        existing = read_json(path, label="evaluation cohort")
        if existing != payload:
            raise SuiteError(
                "evaluation cohort changed; use --eval new for a different suite, split, tasks or seeds"
            )
    elif (paths.eval_dir(eval_id) / "attempts").exists():
        raise SuiteError("cannot attach a curated cohort to an existing unfrozen evaluation; use --eval new")
    else:
        write_json(path, payload)
    return payload


def load_evaluation_cohort(paths: TasksetPaths, eval_id: str) -> dict[str, Any] | None:
    path = paths.eval_dir(require_safe_id(eval_id, "evaluation id")) / "cohort.json"
    if path.is_symlink():
        raise SuiteError("evaluation cohort must not be a symlink")
    if not path.exists():
        return None
    try:
        data = _mapping(read_json(path, label="evaluation cohort"), "evaluation cohort")
        if data.get("schema_version") != COHORT_SCHEMA or data.get("name") != paths.name:
            raise SuiteError("evaluation cohort schema or taskset name is invalid")
        require_safe_id(_string(data, "suite_id", "cohort"), "suite id")
        require_sha256_hex(_string(data, "suite_content_sha256", "cohort"), "suite hash")
        _enum(data, "split", SPLITS, "cohort")
        task_ids = _string_list(data, "task_ids", "cohort")
        if not task_ids or len(task_ids) != len(set(task_ids)):
            raise SuiteError("evaluation cohort needs unique task IDs")
        for task_id in task_ids:
            require_task_id(task_id, "cohort task id")
        seeds = data.get("seeds")
        if (
            not isinstance(seeds, list)
            or not seeds
            or any(isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in seeds)
            or len(seeds) != len(set(seeds))
        ):
            raise SuiteError("evaluation cohort needs unique non-negative seeds")
        sources = _mapping(data.get("task_sources"), "cohort task_sources")
        if set(sources) != set(task_ids) or any(
            not isinstance(value, str) or not value for value in sources.values()
        ):
            raise SuiteError("cohort task source index does not match its task IDs")
        artifacts = _mapping(data.get("task_artifacts"), "cohort task_artifacts")
        if set(artifacts) != set(task_ids):
            raise SuiteError("cohort artifact index does not match its task IDs")
        for task_id, record in artifacts.items():
            record = _mapping(record, f"cohort task_artifacts.{task_id}")
            for key in ("base_bundle_sha256", "scorer_package_sha256", "public_task_sha256"):
                require_sha256_hex(_string(record, key, "cohort task artifact"), key)
            _number(record, "pass_threshold", "cohort task artifact", minimum=0, maximum=1)
        return data
    except (GhostlabError, SchemaError, OSError) as exc:
        raise SuiteError(f"invalid evaluation cohort: {exc}") from exc
