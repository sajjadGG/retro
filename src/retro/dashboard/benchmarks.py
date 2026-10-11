"""Read-only, allowlisted projections of published benchmark artifacts.

Inputs live at ``benchmarks/<name>/task-scorer``. Suite tasks are read from
``suites/<suite_id>/tasks``, never substituted with mutable live tasks. Only
JSON metadata and public prompts are read: bundles, scorer source, patches,
agent artifacts and environment variables stay private. Bounded component
evidence from attempt records explains grades without publishing scorer code.

The index, tasksets, task details and evaluation details are separate classic
JavaScript shards, loaded through the existing file:// dashboard loader.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..storage import Layout
from .inputs import CatalogView
from .shards import write_js

INDEX_REF = {"name": "benchmark-index", "file": "data/benchmarks/index.js"}
CATALOG_REF = {"name": "benchmark-task-catalog", "file": "data/benchmarks/catalog.js"}
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_PROMPT_CHARS = 8000
VALIDATION_KINDS = (
    "base",
    "oracle",
    "no_op",
    "construct_changing",
    "construct_preserving",
    "regression",
)
SPLITS = ("train", "validation", "test")
ATTEMPT_STATUSES = {
    "scored",
    "agent_error",
    "agent_timeout",
    "model_unavailable",
    "scorer_error",
    "scorer_timeout",
    "judge_unavailable",
    "invalid_candidate_artifact",
    "harness_error",
    "invalid_result",
}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IMAGE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")
_SEED = re.compile(r"^seed-(0|[1-9][0-9]*)$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (["']?[\w.-]*(?:password|passwd|token|secret|api[_-]?key|authorization|credential)
    [\w.-]*["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)
    """
)
_SECRET_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"sk-[A-Za-z0-9_-]{12,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.S)
_INVALID_UNICODE = re.compile(r"[\ud800-\udfff]")


def _text(
    value: Any, limit: int = 1000, *, multiline: bool = False, preserve_code: bool = False
) -> str | None:
    if not isinstance(value, str):
        return None
    text = _ANSI.sub("", value)
    text = _PRIVATE_KEY.sub("[private key omitted]", text)
    if not preserve_code:
        text = re.sub(r"```.*?(?:```|$)", "[code omitted]", text, flags=re.S)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 [redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _SECRET_TOKEN.sub("[redacted]", text)
    text = re.sub(r"(https?://)[^/\s@]+:[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(
        r"(?i)([?&](?:sig|signature|access_token|api_key|key|token)=)[^&\s]+",
        r"\1[redacted]",
        text,
    )
    if not multiline:
        text = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return text[:limit] + ("…" if len(text) > limit else "")


def _strings(value: Any, limit: int = 200) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _text(item, limit)) is not None]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _invalid_unicode(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and _INVALID_UNICODE.search(item):
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _ref(kind: str, key: str) -> dict[str, str]:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return {"name": f"benchmark-{kind}:{digest}", "file": f"data/benchmarks/{kind}-{digest}.js"}


def project_experiment(record: dict[str, Any]) -> dict[str, Any]:
    """Allowlist job setup/progress without embedding configs or candidate code."""
    raw_datasets = record.get("datasets")
    raw_agents = record.get("agents")
    datasets: list[dict[str, Any]] = [
        {
            key: _strings(raw.get(key), 250) if key == "task_ids" else _text(raw.get(key), 250)
            for key in ("dataset_id", "name", "suite_id", "split", "suite_content_sha256", "task_ids")
        }
        for raw in raw_datasets or [] if isinstance(raw, dict)
    ] if isinstance(raw_datasets, list) else []
    agents: list[dict[str, Any]] = [
        {
            "agent_id": _text(raw.get("agent_id"), 150),
            "model": _text(raw.get("model"), 150),
            "runtime": {
                key: _text(value, 250) for key, value in _mapping(raw.get("runtime")).items()
                if key in {"model", "backend", "agent", "parser", "runner", "script"}
            },
            "config_sha256": _text(raw.get("config_sha256"), 100),
            "asset_count": len(raw["asset_sha256"]) if isinstance(raw.get("asset_sha256"), dict) else None,
        }
        for raw in raw_agents or [] if isinstance(raw, dict)
    ] if isinstance(raw_agents, list) else []
    seeds = [
        seed for seed in record.get("seeds", [])
        if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0
    ] if isinstance(record.get("seeds"), list) else []
    runs = []
    for raw in record.get("runs", []) if isinstance(record.get("runs"), list) else []:
        if not isinstance(raw, dict):
            continue
        run: dict[str, Any] = {key: _text(raw.get(key), 250) for key in (
            "run_id", "dataset_id", "name", "agent_id", "eval_id", "status", "started_at", "finished_at",
        )}
        for key in ("requested_attempts", "completed_attempts", "scored_attempts"):
            value = raw.get(key)
            run[key] = (
                value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
            )
        error = raw.get("error")
        run["error"] = _text(error.get("detail"), 1500) if isinstance(error, dict) else _text(error, 1500)
        run["results_available"] = bool(run["completed_attempts"])
        runs.append(run)
    error = record.get("error")
    cleanup = _mapping(record.get("cleanup"))
    return {
        "experiment_id": _text(record.get("experiment_id"), 100),
        "title": _text(record.get("title"), 160),
        "description": _text(record.get("description") or record.get("hypothesis"), 4000, multiline=True),
        "status": _text(record.get("status"), 100),
        "created_at": _text(record.get("created_at"), 100),
        "finished_at": _text(record.get("finished_at"), 100),
        "setup_sha256": _text(record.get("setup_sha256") or record.get("content_sha256"), 100),
        "datasets": datasets,
        "agents": agents,
        "seeds": seeds,
        "requested_attempts": (
            sum(len(dataset["task_ids"]) for dataset in datasets) * len(agents) * len(seeds)
            if datasets and agents and seeds else None
        ),
        "limits": {
            target: value if (
                isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value > 0
            ) else None
            for field, target in (
                ("aut_timeout_seconds", "agent_timeout_seconds"),
                ("scorer_timeout_seconds", "scorer_timeout_seconds"),
            )
            for value in [record.get(field)]
        },
        "tags": _strings(record.get("tags"), 100),
        "error": _text(error.get("detail"), 2000, multiline=True)
        if isinstance(error, dict) else _text(error, 2000, multiline=True),
        "runs": runs,
        "progress": {
            "completed_attempts": sum(run["completed_attempts"] or 0 for run in runs),
            "scored_attempts": sum(run["scored_attempts"] or 0 for run in runs),
            "unscored_attempts": sum(
                max(0, (run["completed_attempts"] or 0) - (run["scored_attempts"] or 0))
                for run in runs
            ),
        },
        "cleanup": {
            "at": _text(cleanup.get("at"), 100),
            "prune_artifacts": _boolean(cleanup.get("prune_artifacts")),
            "removed_count": len(cleanup["removed"]) if isinstance(cleanup.get("removed"), list) else None,
            "errors": [
                _text(item.get("detail"), 1500) if isinstance(item, dict) else _text(item, 1500)
                for item in cleanup.get("errors", []) if item
            ] if isinstance(cleanup.get("errors"), list) else [],
            "sandbox_cleanup": _text(cleanup.get("sandbox_cleanup"), 300),
            "artifacts_pruned": _boolean(record.get("artifacts_pruned")),
        } if cleanup else None,
    }


def _task_overview(
    prompt: str | None,
    goal: dict[str, Any],
    definition: dict[str, Any],
    environment: dict[str, Any],
    scorer: dict[str, Any],
    observables: list[dict[str, Any]],
) -> dict[str, Any]:
    runtimes: set[str] = set()
    tooling = {
        "python": "Python", "python3": "Python", "pytest": "Python",
        "node": "Node.js", "npm": "Node.js", "npx": "Node.js",
        "go": "Go", "cargo": "Rust", "rustc": "Rust", "java": "Java", "mvn": "Java",
    }
    counts = {}
    for stage in ("setup", "smoke", "test"):
        commands = environment.get(stage)
        counts[stage] = len(commands) if isinstance(commands, list) else None
        for command in commands if isinstance(commands, list) else []:
            if isinstance(command, list) and command and isinstance(command[0], str):
                runtime = tooling.get(Path(command[0]).name)
                if runtime:
                    runtimes.add(runtime)
    image = environment.get("image")
    image_label = None
    if isinstance(image, str) and _IMAGE.fullmatch(image):
        image_label = image.split("@")[0].rsplit("/", 1)[-1] if "@" in image else "local image"
    mode = scorer.get("mode")
    return {
        "title": _text(goal.get("summary") or prompt, 220),
        "description": _text(prompt, 280),
        "success_criteria": [
            item["description"] for item in observables if item.get("description")
        ],
        "base_failure": _text(definition.get("base_failure_claim"), 500),
        "accepted_outcome": _text(definition.get("outcome_success_claim"), 500),
        "environment": {
            "image_label": _text(image_label, 100),
            "runtimes": sorted(runtimes),
            "workdir": _text(environment.get("workdir"), 200),
            "command_counts": counts,
        },
        "grader": {
            "label": {
                "deterministic": "Behavioral checks", "code": "Behavioral checks",
                "hybrid": "Checks and model judgment", "judge": "Model judgment",
                "agentic": "Agentic review",
            }.get(mode if isinstance(mode, str) else "", "Grader type not recorded"),
            "criterion_count": len(observables),
        },
    }


@dataclass
class BenchmarkView:
    missing: bool = False
    warnings: list[str] = field(default_factory=list)
    tasksets: list[dict[str, Any]] = field(default_factory=list)
    payloads: list[tuple[dict[str, str], dict[str, Any]]] = field(default_factory=list)
    ready_tasks: list[dict[str, Any]] = field(default_factory=list)
    draft_tasks: list[dict[str, Any]] = field(default_factory=list)
    datasets: list[dict[str, Any]] = field(default_factory=list)
    evaluations: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "tasksets": len(self.tasksets),
            "tasks": sum(item["counts"]["tasks"] for item in self.tasksets),
            "suites": sum(item["counts"]["suites"] for item in self.tasksets),
            "evaluations": sum(item["counts"]["evaluations"] for item in self.tasksets),
            "warnings": len(self.warnings),
            "ready_tasks": len(self.ready_tasks),
            "draft_tasks": len(self.draft_tasks),
            "datasets": len(self.datasets),
            "experiments": len(self.jobs),
        }


def read_benchmarks(layout: Layout, catalog: CatalogView | None = None) -> BenchmarkView:
    """Decode published disk contracts without importing or invoking task runtime."""
    records = catalog.by_key() if catalog else {}
    return _Reader(
        layout.root,
        set(records),
        {key: _text(record.project.name, 150) for key, record in records.items()},
    ).read()


def write_benchmarks(view: BenchmarkView, data_dir: Path) -> dict[str, Any]:
    """Write only projected metadata into the enclosing dashboard generation."""
    warning_sha256 = hashlib.sha256("\n".join(sorted(view.warnings)).encode("utf-8")).hexdigest()
    for ref, payload in view.payloads:
        write_js(data_dir.parent / ref["file"], ref["name"], payload)
    write_js(
        data_dir.parent / INDEX_REF["file"],
        INDEX_REF["name"],
        {
            "missing": view.missing,
            "tasksets": view.tasksets,
            "counts": view.counts(),
            "warnings": view.warnings,
            "warning_sha256": warning_sha256,
            "datasets": view.datasets,
            "evaluations": view.evaluations,
            "jobs": view.jobs,
            **({"catalog": dict(CATALOG_REF)} if view.tasksets else {}),
        },
    )
    if view.tasksets:
        write_js(
            data_dir.parent / CATALOG_REF["file"],
            CATALOG_REF["name"],
            {"tasks": view.ready_tasks, "draft_tasks": view.draft_tasks},
        )
    return {
        **view.counts(), "index": dict(INDEX_REF),
        "warning_sha256": warning_sha256,
        **({"catalog": dict(CATALOG_REF)} if view.tasksets else {}),
        "shards": len(view.payloads) + 1 + bool(view.tasksets),
    }


class _Reader:
    def __init__(
        self, root: Path, session_keys: set[str], projects: dict[str, str | None] | None = None
    ) -> None:
        self.root = root
        self.session_keys = session_keys
        self.view = BenchmarkView()
        self.file_hashes: dict[Path, str] = {}
        self.source_keys: dict[str, str] = {}
        self.projects = projects or {}

    def warn(self, path: Path, detail: str) -> None:
        relative = path.relative_to(self.root).as_posix()
        message = f"{_text(relative, 1000)}: {_text(detail, 1000)}"
        if message not in self.view.warnings:
            self.view.warnings.append(message)

    def safe(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                self.warn(path, "symlink artifact skipped; benchmark inputs must remain inside the archive")
                return False
        return True

    def children(self, path: Path) -> list[Path]:
        if not self.safe(path) or not path.exists():
            return []
        if not path.is_dir():
            self.warn(path, "expected a directory")
            return []
        try:
            items = sorted(path.iterdir())
        except OSError:
            self.warn(path, "directory could not be read")
            return []
        result = []
        for item in items:
            if item.name.startswith("."):
                continue
            if not self.safe(item):
                continue
            if item.is_dir():
                if _ID.fullmatch(item.name):
                    result.append(item)
                else:
                    self.warn(item, "unsafe artifact identifier skipped")
        return result

    def json(self, path: Path, schema: str | None = None, *, required: bool = False) -> dict[str, Any] | None:
        if not self.safe(path):
            return None
        if not path.exists():
            if required:
                self.warn(path, "published artifact missing")
            return None
        try:
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                self.warn(path, "artifact exceeds the 32 MiB metadata limit; skipped")
                return None
            raw = path.read_bytes()
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            self.warn(path, f"malformed JSON at line {error.lineno}, column {error.colno}; skipped")
            return None
        except (OSError, UnicodeError, ValueError, RecursionError):
            self.warn(path, "artifact could not be decoded; skipped")
            return None
        if not isinstance(payload, dict):
            self.warn(path, "expected a JSON object; skipped")
            return None
        if _invalid_unicode(payload):
            self.warn(path, "malformed Unicode strings; artifact skipped")
            return None
        if schema and payload.get("schema_version", payload.get("schema")) != schema:
            self.warn(path, f"unsupported schema; expected {schema}; skipped")
            return None
        self.file_hashes[path] = hashlib.sha256(raw).hexdigest()
        return payload

    def array(self, doc: dict[str, Any], key: str, path: Path, *, required: bool = False) -> list[Any]:
        value = doc.get(key)
        if isinstance(value, list):
            return value
        if required or value is not None:
            self.warn(path, f"{key} must be an array")
        return []

    def objects(
        self,
        doc: dict[str, Any],
        key: str,
        path: Path,
        *,
        required: bool = False,
    ) -> list[dict[str, Any]]:
        result = []
        for item in self.array(doc, key, path, required=required):
            if isinstance(item, dict):
                result.append(item)
            else:
                self.warn(path, f"malformed {key} entry skipped")
        return result

    def identifier(self, value: Any, path: Path, field: str) -> str | None:
        if isinstance(value, str) and len(value) <= 250 and _ID.fullmatch(value):
            return value
        self.warn(path, f"{field} is missing or is not a path-safe identifier")
        return None

    def number(
        self,
        value: Any,
        path: Path,
        field: str,
        *,
        ratio: bool = False,
        integer: bool = False,
    ) -> int | float | None:
        if value is None:
            return None
        try:
            finite = isinstance(value, (int, float)) and math.isfinite(value)
        except OverflowError:
            finite = False
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not finite
            or value < 0
            or (isinstance(value, int) and value > 2**53 - 1)
            or (ratio and value > 1)
            or (integer and not isinstance(value, int))
        ):
            self.warn(path, f"{field} has an invalid numeric value; shown as unavailable")
            return None
        return value

    def numbers(
        self,
        value: Any,
        path: Path,
        field: str,
        *,
        ratio: bool = False,
        integer: bool = False,
    ) -> dict[str, int | float | None]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.warn(path, f"{field} must be an object")
            return {}
        return {
            name: self.number(number, path, f"{field}.{name}", ratio=ratio, integer=integer)
            for key, number in value.items()
            if (name := _text(key, 150))
        }

    def digest(
        self, value: Any, path: Path, field: str, *, length: int = 64, required: bool = False
    ) -> str | None:
        if value is None and not required:
            return None
        if isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value):
            return value
        self.warn(path, f"{field} is missing or is not a lowercase {length}-character hash")
        return None

    def source(self, source_id: Any) -> dict[str, Any]:
        source_id = _text(source_id, 250)
        key = self.source_keys.get(source_id or "")
        if not key and source_id and "__" in source_id:
            host, session_id = source_id.split("__", 1)
            if _ID.fullmatch(host) and _ID.fullmatch(session_id):
                key = f"{host}/{session_id}"
        return {"source_id": source_id, "session_key": key, "captured": bool(key in self.session_keys)}

    def rejections(self, value: Any, path: Path, stage: str) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            if value is not None:
                self.warn(path, "rejections must be an array")
            return []
        result = []
        for item in value:
            if not isinstance(item, dict):
                self.warn(path, "malformed rejection entry skipped")
                continue
            result.append(
                {
                    "stage": _text(item.get("stage")) or stage,
                    "task_id": _text(item.get("task_id"), 250),
                    "source": self.source(item.get("source_id")),
                    "code": _text(item.get("code"), 150) or "UNSPECIFIED",
                    "detail": _text(item.get("detail")) or "",
                    "event_ids": _strings(_mapping(item.get("evidence")).get("event_ids")),
                }
            )
        return result

    def read(self) -> BenchmarkView:
        root = self.root / "benchmarks"
        self.view.missing = not root.exists()
        for taskset in self.children(root):
            path = taskset / "task-scorer"
            if path.exists() and self.safe(path):
                self.taskset(path, taskset.name)
        unique_tasks: dict[tuple[str, str], dict[str, Any]] = {}
        for task in sorted(
            self.view.ready_tasks,
            key=lambda item: (item["released_at"] or "", item["name"], item["suite_id"]),
        ):
            unique_tasks[(task["task_id"], task["repo_id"])] = task
        self.view.ready_tasks = sorted(
            unique_tasks.values(), key=lambda item: ((item["project"] or "").lower(), item["task_id"])
        )
        self.view.draft_tasks = [
            task for task in self.view.draft_tasks
            if (task["task_id"], task["repo_id"]) not in unique_tasks
        ]
        for directory in self.children(self.root / "experiments"):
            artifacts = ("setup.json", "job.json", ".job.lock", "controller-status.json")
            if any((directory / name).exists() or (directory / name).is_symlink() for name in artifacts):
                self.experiment(directory)
        return self.view

    def experiment(self, directory: Path) -> None:
        warning_start = len(self.view.warnings)
        setup_path, state_path = directory / "setup.json", directory / "job.json"
        setup = self.json(setup_path, "retro-experiment-setup-v1", required=True)
        state = self.json(state_path, "retro-experiment-job-v1", required=True)
        if setup is None or state is None:
            self.view.jobs.append(project_experiment({
                "experiment_id": directory.name, "title": directory.name, "status": "failed",
                "error": (
                    "Saved experiment setup or progress is unavailable; inspect its verification warning."
                ),
            }))
            return
        digest = hashlib.sha256(json.dumps(
            {key: value for key, value in setup.items() if key != "content_sha256"},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        valid = (
            setup.get("experiment_id") == state.get("experiment_id") == directory.name
            and setup.get("content_sha256") == state.get("setup_sha256") == digest
            and state.get("status") in {"planned", "running", "completed", "failed", "interrupted"}
        )
        datasets = self.objects(setup, "datasets", setup_path, required=True)
        agents = self.objects(setup, "agents", setup_path, required=True)
        runs = self.objects(state, "runs", state_path, required=True)
        seeds = self.array(setup, "seeds", setup_path, required=True)
        dimensions_ok = bool(datasets and agents and runs and seeds) and all(
            isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0 for seed in seeds
        )
        if dimensions_ok:
            dimensions_ok = len(set(seeds)) == len(seeds)
        for dataset in datasets:
            dimensions_ok &= (
                all(isinstance(dataset.get(key), str) for key in ("dataset_id", "name", "suite_id", "split"))
                and dataset.get("split") in SPLITS
                and isinstance(dataset.get("task_ids"), list) and bool(dataset["task_ids"])
                and all(
                    isinstance(task_id, str) and _ID.fullmatch(task_id) for task_id in dataset["task_ids"]
                )
            )
        for agent in agents:
            dimensions_ok &= (
                isinstance(agent.get("agent_id"), str) and bool(_ID.fullmatch(agent["agent_id"]))
                and self.digest(
                    agent.get("config_sha256"), setup_path, "agent config", required=True
                ) is not None
            )
        for run in runs:
            dimensions_ok &= all(
                isinstance(run.get(key), str) and bool(_ID.fullmatch(run[key]))
                for key in ("dataset_id", "agent_id", "name", "eval_id")
            )
        if not dimensions_ok:
            self.warn(setup_path, "experiment dataset/agent/trial dimensions are missing or invalid")
            valid = False
        for setting in ("aut_timeout_seconds", "scorer_timeout_seconds"):
            value = setup.get(setting)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0
            ):
                self.warn(setup_path, f"experiment {setting} must be positive and finite")
                valid = False
        if dimensions_ok:
            expected_pairs = {
                (dataset.get("dataset_id"), agent.get("agent_id"))
                for dataset in datasets for agent in agents
            }
            observed_pairs = [(run.get("dataset_id"), run.get("agent_id")) for run in runs]
            if len(observed_pairs) != len(expected_pairs) or set(observed_pairs) != expected_pairs:
                self.warn(state_path, "experiment executions do not match its frozen datasets and agents")
                valid = False
            evaluations = {(item["name"], item["eval_id"]): item for item in self.view.evaluations}
            for run in runs:
                bound_dataset = next(
                    (item for item in datasets if item.get("dataset_id") == run.get("dataset_id")), None
                )
                if bound_dataset is None or bound_dataset["name"] != run.get("name"):
                    self.warn(state_path, "experiment execution names a different dataset collection")
                    valid = False
                    continue
                evaluation = evaluations.get((run["name"], run.get("eval_id")))
                if run.get("status") == "completed" and evaluation is None:
                    self.warn(state_path, "completed experiment execution has no saved result evidence")
                    valid = False
                if evaluation is not None:
                    cohort = evaluation["cohort"] or {}
                    if (
                        cohort.get("suite_content_sha256") != bound_dataset.get("suite_content_sha256")
                        or cohort.get("suite_id") != bound_dataset["suite_id"]
                        or cohort.get("split") != bound_dataset["split"]
                        or sorted(cohort.get("task_ids", [])) != sorted(bound_dataset["task_ids"])
                        or sorted(cohort.get("seeds", [])) != sorted(seeds)
                    ):
                        self.warn(state_path, "experiment result cohort differs from its frozen setup")
                        valid = False
        valid = valid and len(self.view.warnings) == warning_start
        if not valid:
            self.warn(
                state_path, "experiment identity, immutable setup checksum or progress status is invalid"
            )
        projected = project_experiment({**setup, **state})
        if not valid:
            projected.update(status="failed", error="The saved experiment setup could not be verified.")
        self.view.jobs.append(projected)

    def taskset(self, root: Path, name: str) -> None:
        start = len(self.view.warnings)
        selection_path = root / "selection.json"
        selection = self.json(selection_path, "retro-taskset-selection-v1")
        selected = []
        if selection is not None:
            for raw in self.array(selection, "selected", selection_path, required=True):
                if not isinstance(raw, dict):
                    self.warn(selection_path, "malformed selected-source entry skipped")
                    continue
                source_id = self.identifier(raw.get("source_id"), selection_path, "selected.source_id")
                if not source_id:
                    continue
                host, session = raw.get("host"), raw.get("session_id")
                if (
                    isinstance(host, str)
                    and isinstance(session, str)
                    and _ID.fullmatch(host)
                    and _ID.fullmatch(session)
                ):
                    self.source_keys[source_id] = f"{host}/{session}"
                selected.append(
                    {
                        "source": self.source(source_id),
                        "repo_id": _text(_mapping(raw.get("repo")).get("repo_id"), 250),
                        "base_sha": self.digest(
                            _mapping(raw.get("base")).get("sha"), selection_path, "base.sha", length=40
                        ),
                        "outcome_sha": self.digest(
                            _mapping(raw.get("outcome")).get("sha"),
                            selection_path,
                            "outcome.sha",
                            length=40,
                        ),
                    }
                )
        selection_rejections = self.rejections(
            self.array(selection, "rejections", selection_path, required=True) if selection else None,
            selection_path,
            "selection",
        )
        if selection is not None and (
            _mapping(selection.get("counts")).get("selected") != len(selected)
            or _mapping(selection.get("counts")).get("rejected") != len(selection_rejections)
        ):
            self.warn(selection_path, "selection counts differ from decoded entries; observed yield shown")
        selection_view = (
            None
            if selection is None
            else {
                "selected": selected,
                "rejections": selection_rejections,
                "counts": {"selected": len(selected), "rejected": len(selection_rejections)},
            }
        )
        bundles_path = root / "bundles.json"
        bundles = self.json(bundles_path, "retro-taskset-bundles-v1")
        bundle_sources = []
        if bundles is not None:
            for raw in self.array(bundles, "sources", bundles_path, required=True):
                if not isinstance(raw, dict):
                    self.warn(bundles_path, "malformed bundle-source entry skipped")
                    continue
                bundle_sources.append(
                    {
                        "source": self.source(raw.get("source_id")),
                        "status": _text(raw.get("status"), 150),
                        "code": _text(raw.get("code"), 150),
                        "detail": _text(raw.get("detail")),
                        "content_sha256": self.digest(
                            raw.get("content_sha256"), bundles_path, "content_sha256"
                        ),
                    }
                )
        bundle_view = (
            None
            if bundles is None
            else {
                "sources": bundle_sources,
                "counts": dict(Counter(item["status"] or "unknown" for item in bundle_sources)),
            }
        )
        builds = [
            item
            for path in self.children(root / "builds")
            if (item := self.build(path / "build.json", path.name)) is not None
        ]
        curation_path = root / "curation.json"
        curation_doc = self.json(curation_path, "retro-task-curation-v1")
        curation = None
        if curation_doc is not None:
            curation_policy = _mapping(curation_doc.get("policy"))
            curation = {
                "status": _text(curation_doc.get("status"), 150),
                "suite_id": _text(curation_doc.get("suite_id"), 250),
                "created_at": _text(curation_doc.get("created_at"), 150),
                "candidate_tasks": self.number(
                    curation_doc.get("candidate_tasks"),
                    curation_path,
                    "candidate_tasks",
                    integer=True,
                ),
                "accepted_task_ids": _strings(curation_doc.get("accepted_task_ids"), 250),
                "rejections": self.rejections(curation_doc.get("rejections"), curation_path, "curation"),
                "error": self.error(curation_doc.get("error")),
                "policy": {
                    "split_unit": _text(curation_policy.get("split_unit"), 150),
                    "split_ratios": {
                        split: self.number(
                            _mapping(curation_policy.get("split_ratios")).get(split),
                            curation_path,
                            f"policy.split_ratios.{split}",
                            ratio=True,
                        )
                        for split in SPLITS
                    },
                    "seed": self.number(
                        curation_policy.get("seed"), curation_path, "policy.seed", integer=True
                    ),
                    "allow_reconstructed": _boolean(curation_policy.get("allow_reconstructed")),
                },
            }
            if curation_doc.get("name") != name or curation["status"] not in ("curated", "rejected"):
                self.warn(curation_path, "curation identity/status is missing or unsupported")
            if curation["status"] == "curated" and not curation["accepted_task_ids"]:
                self.warn(
                    curation_path, "curation claims success without accepted tasks; no empty release verified"
                )
                curation["reported_status"] = "curated"
                curation["status"] = "incomplete"
        active_path = root / "active-tasks.json"
        active_doc = self.json(active_path)
        active: list[str] | None = None
        if active_doc is not None:
            raw_ids = self.array(active_doc, "task_ids", active_path, required=True)
            if isinstance(active_doc.get("task_ids"), list):
                active = [
                    task_id
                    for raw in raw_ids
                    if (task_id := self.identifier(raw, active_path, "task_ids entry")) is not None
                ]
        tasks = [
            self.task(path, name, path.name, active=None if active is None else path.name in active)
            for path in self.children(root / "tasks")
        ]
        if active is not None:
            missing = set(active) - {item["task_id"] for item in tasks}
            for task_id in sorted(missing):
                self.warn(active_path, f"active task {task_id} has no published task directory")
        suites = [
            item for path in self.children(root / "suites") if (item := self.suite(path, name)) is not None
        ]
        suite_lookup = {item["suite_id"]: item for item in suites}
        evaluations = [self.evaluation(path, name, suite_lookup) for path in self.children(root / "evals")]
        verified_suites = [suite for suite in suites if suite["valid"]]
        preferred_suite = suite_lookup.get((curation or {}).get("suite_id"))
        if not preferred_suite or not preferred_suite["valid"]:
            preferred_suite = max(
                verified_suites, key=lambda suite: (suite["created_at"] or "", suite["suite_id"]),
                default=None,
            )
        if preferred_suite:
            for task in preferred_suite["tasks"]:
                if task["snapshot_valid"]:
                    self.view.ready_tasks.append({
                        **task,
                        "name": name,
                        "suite_id": preferred_suite["suite_id"],
                        "released_at": preferred_suite["created_at"],
                    })
        else:
            self.view.draft_tasks.extend(
                {**task, "name": name, "suite_id": None}
                for task in tasks if task["active"] is not False
            )
        warnings = self.view.warnings[start:]
        counts = {
            "selected": len(selected) if selection is not None else None,
            "rejected": len(selection_rejections) if selection is not None else None,
            "bundled": sum(item["status"] in ("bundled", "reused") for item in bundle_sources)
            if bundles is not None
            else None,
            "builds": len(builds),
            "tasks": len(tasks),
            "active_tasks": len(active) if active is not None else None,
            "suites": len(suites),
            "evaluations": len(evaluations),
        }
        ref = _ref("set", name)
        self.view.datasets.extend(
            {
                "name": name,
                "suite_id": suite["suite_id"],
                "created_at": suite["created_at"],
                "content_sha256": suite["content_sha256"],
                "summary": suite["summary"],
                "valid": suite["valid"],
                "current": suite is preferred_suite,
                "detail": ref,
            }
            for suite in suites
        )
        self.view.evaluations.extend({**evaluation, "name": name} for evaluation in evaluations)
        payload = {
            "name": name,
            "selection": selection_view,
            "bundles": bundle_view,
            "builds": builds,
            "curation": curation,
            "active_task_ids": active,
            "tasks": tasks,
            "suites": suites,
            "evaluations": evaluations,
            "counts": counts,
            "warnings": warnings,
        }
        self.view.payloads.append((ref, payload))
        self.view.tasksets.append({
            "name": name, "counts": counts, "detail": ref, "warnings": len(warnings),
            "preferred_suite_id": preferred_suite["suite_id"] if preferred_suite else None,
        })

    def build(self, path: Path, build_id: str) -> dict[str, Any] | None:
        doc = self.json(path, "retro-taskset-build-v1", required=True)
        if doc is None:
            return None
        if doc.get("build_id") != build_id:
            self.warn(path, "build_id does not match its directory")
        sources = []
        for raw in self.array(doc, "sources", path, required=True):
            if not isinstance(raw, dict):
                self.warn(path, "malformed build-source entry skipped")
                continue
            sources.append(
                {
                    "source": self.source(raw.get("source_id")),
                    "stage": _text(raw.get("stage"), 150),
                    "status": _text(raw.get("status"), 150),
                    "published_task_ids": _strings(raw.get("published_task_ids"), 250),
                    "warnings": _strings(raw.get("warnings"), 500),
                    "error": self.error(raw.get("error")),
                }
            )
        return {
            "build_id": build_id,
            "sources": sources,
            "published_task_ids": _strings(doc.get("published_task_ids"), 250),
            "rejections": self.rejections(doc.get("rejections"), path, "build"),
            "stage_counts": dict(Counter(item["stage"] or "unknown" for item in sources)),
            "status_counts": dict(Counter(item["status"] or "unknown" for item in sources)),
        }

    def error(self, raw: Any) -> str | None:
        if isinstance(raw, dict):
            code = _text(raw.get("code"), 150)
            detail = _text(raw.get("detail", raw.get("message")), 800)
            return ": ".join(item for item in (code, detail) if item) or None
        return _text(raw, 800)

    def validation(self, doc: dict[str, Any] | None, path: Path) -> dict[str, Any]:
        doc = doc or {}
        cases = []
        for raw in self.objects(doc, "cases", path):
            score = self.number(raw.get("score_total"), path, "case.score_total", ratio=True)
            status = _text(raw.get("status"), 150)
            if status != "scored":
                score = None
            cases.append(
                {
                    "case_id": _text(raw.get("case_id"), 250),
                    "kind": _text(raw.get("kind"), 150),
                    "targeted_component": _text(raw.get("targeted_component"), 150),
                    "status": status,
                    "ok": _boolean(raw.get("ok")),
                    "score_total": score,
                    "passed": _boolean(raw.get("passed")) if status == "scored" else None,
                    "hard_gate_failures": _strings(raw.get("hard_gate_failures")),
                    "component_values": self.numbers(
                        raw.get("component_values"), path, "component_values", ratio=True
                    ),
                    "runs": self.number(raw.get("runs"), path, "case.runs", integer=True),
                    "code": _text(raw.get("code"), 150),
                    "detail": _text(raw.get("detail")),
                }
            )
        missing = [kind for kind in VALIDATION_KINDS if not any(case["kind"] == kind for case in cases)]
        if missing:
            self.warn(path, "mandatory validation evidence missing: " + ", ".join(missing))
        repeat = _mapping(doc.get("repeatability"))
        if _boolean(repeat.get("ok")) is None:
            self.warn(path, "repeatability decision missing")
        status = "incomplete" if missing or not repeat else "failed"
        if (
            doc.get("passed") is True
            and not missing
            and repeat.get("ok") is True
            and all(case["ok"] is True and case["score_total"] is not None for case in cases)
        ):
            status = "passed"
        return {
            "status": status,
            "passed": _boolean(doc.get("passed")),
            "cases": cases,
            "missing_kinds": missing,
            "codes": _strings(doc.get("codes")),
            "scorer_package_sha256": self.digest(
                doc.get("scorer_package_sha256"),
                path,
                "scorer_package_sha256",
                required=bool(doc),
            ),
            "repeatability": {
                "ok": _boolean(repeat.get("ok")),
                "deterministic_mismatches": _strings(repeat.get("deterministic_mismatches")),
                "performance_spread": self.number(
                    repeat.get("performance_spread"), path, "performance_spread"
                ),
                "judge_stdev": self.numbers(repeat.get("judge_stdev"), path, "judge_stdev"),
                "detail": _text(repeat.get("detail")),
            },
        }

    def task(
        self,
        root: Path,
        name: str,
        task_id: str,
        *,
        active: bool | None = None,
        suite_id: str | None = None,
        membership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        start = len(self.view.warnings)
        public_path = root / "public" / "task.json"
        public = self.json(public_path, "retro-benchmark-task-v1", required=True)
        provenance_path = root / "private" / "provenance.json"
        provenance = self.json(provenance_path, "retro-task-provenance-v1", required=True)
        environment_path = root / "public" / "environment.json"
        environment = self.json(environment_path, "retro-project-environment-v1", required=True)
        source_path = root / "private" / "source-link.json"
        source_link = self.json(source_path, required=True)
        scorer_path = root / "private" / "scorer" / "scorer.json"
        scorer = self.json(scorer_path, "retro-scorer-v1", required=True)
        validation_path = root / "private" / "scorer-validation.json"
        validation_doc = self.json(validation_path, "retro-scorer-validation-v1", required=True)
        public, provenance, environment, source_link, scorer = (
            item or {} for item in (public, provenance, environment, source_link, scorer)
        )
        identity_ok = True
        for path, doc in (
            (public_path, public),
            (provenance_path, provenance),
            (scorer_path, scorer),
            (validation_path, validation_doc or {}),
        ):
            if doc and doc.get("task_id") != task_id:
                identity_ok = False
                self.warn(path, "task_id does not match its published task directory")
        if validation_doc is None and isinstance(provenance.get("validation"), dict):
            embedded = provenance["validation"]
            if embedded.get("schema_version", embedded.get("schema")) == "retro-scorer-validation-v1":
                validation_doc = embedded
                self.warn(
                    validation_path, "using embedded provenance validation; standalone evidence unavailable"
                )
        validation = self.validation(validation_doc, validation_path)
        definition = _mapping(provenance.get("task_definition"))
        brief = _mapping(definition.get("scorer_brief"))
        if not isinstance(brief.get("observables"), list):
            self.warn(provenance_path, "observable/regression rubric missing")
        observables = [
            {
                "id": _text(raw.get("id"), 150),
                "description": _text(raw.get("description")),
                "importance": _text(raw.get("importance"), 150),
                "evidence": _strings(raw.get("evidence")),
            }
            for raw in self.objects(brief, "observables", provenance_path)
        ]
        repo = {**_mapping(public.get("repository")), **_mapping(source_link.get("repo"))}
        for key in ("repo_id", "base_sha", "base_tree"):
            if _mapping(public.get("repository")).get(key) != _mapping(source_link.get("repo")).get(key):
                identity_ok = False
                self.warn(source_path, f"source {key} differs from public task repository")
        repository = {
            "repo_id": _text(repo.get("repo_id"), 250),
            "subdir": _text(repo.get("subdir"), 250),
            "state_confidence": _text(repo.get("state_confidence"), 150),
            "base_resolution": _text(repo.get("base_resolution"), 150),
            "outcome_resolution": _text(repo.get("outcome_resolution"), 150),
            **{
                key: self.digest(repo.get(key), source_path, key, length=40, required=True)
                for key in ("base_sha", "base_tree", "outcome_sha", "outcome_tree")
            },
        }
        hashes = {
            key: self.digest(provenance.get(key), provenance_path, key)
            for key in (
                "public_task_sha256",
                "base_bundle_sha256",
                "oracle_bundle_sha256",
                "scorer_package_sha256",
                "publication_sha256",
            )
        }
        hashes["public_environment_sha256"] = self.file_hashes.get(environment_path)
        if hashes["public_task_sha256"] and self.file_hashes.get(public_path) != hashes["public_task_sha256"]:
            identity_ok = False
            self.warn(public_path, "public task checksum differs from published provenance")
        if (
            validation["scorer_package_sha256"]
            and hashes["scorer_package_sha256"]
            and validation["scorer_package_sha256"] != hashes["scorer_package_sha256"]
        ):
            identity_ok = False
            self.warn(validation_path, "validation is bound to a different scorer package")
        source_id = (
            provenance.get("source_id") or source_link.get("source_id") or (membership or {}).get("source_id")
        )
        if source_link.get("source_id") and source_link["source_id"] != source_id:
            identity_ok = False
            self.warn(source_path, "source identity differs from provenance")
        if membership:
            for key in (
                "public_task_sha256",
                "base_bundle_sha256",
                "scorer_package_sha256",
                "publication_sha256",
            ):
                if membership.get(key) != hashes.get(key):
                    identity_ok = False
                    self.warn(provenance_path, f"frozen suite {key} differs from its task copy")
            if (
                membership.get("public_environment_sha256") is not None
                and membership["public_environment_sha256"] != hashes["public_environment_sha256"]
            ):
                identity_ok = False
                self.warn(environment_path, "frozen suite environment checksum differs from its task copy")
            for key, actual in (("source_id", source_id), ("repo_id", repository["repo_id"])):
                if membership.get(key) != actual:
                    identity_ok = False
                    self.warn(provenance_path, f"frozen suite {key} differs from its task copy")
            if membership.get("kind") != public.get("kind"):
                identity_ok = False
                self.warn(public_path, "frozen suite kind differs from its task copy")
        prompt_value = public.get("prompt")
        if not isinstance(prompt_value, str):
            self.warn(public_path, "task.json public prompt is missing or is not text")
        prompt_path = root / "public" / "prompt.txt"
        if self.safe(prompt_path) and prompt_path.exists():
            try:
                if prompt_path.stat().st_size > 64 * 1024:
                    self.warn(prompt_path, "public prompt exceeds 64 KiB; skipped")
                else:
                    prompt_file = prompt_path.read_text(encoding="utf-8")
                    if isinstance(prompt_value, str) and prompt_file != prompt_value:
                        identity_ok = False
                        self.warn(prompt_path, "prompt differs from public task.json")
                    if not isinstance(prompt_value, str):
                        prompt_value = prompt_file
            except (OSError, UnicodeError):
                self.warn(prompt_path, "public prompt could not be read")
        prompt = _text(prompt_value, MAX_PROMPT_CHARS, multiline=True)
        if not prompt:
            self.warn(public_path, "public prompt unavailable")
        image = _mapping(public.get("environment")).get("image") or environment.get("image")
        pinned = isinstance(image, str) and bool(_IMAGE.fullmatch(image))
        if not pinned:
            self.warn(environment_path, "container image is missing or is not digest-pinned")
        audit = _mapping(provenance.get("audit"))
        audit_decision = _text(audit.get("decision"), 150)
        if audit_decision is None:
            self.warn(provenance_path, "audit decision missing")
        components = [
            {
                "id": _text(raw.get("id"), 150),
                "kind": _text(raw.get("kind"), 150),
                "weight": self.number(raw.get("weight"), scorer_path, "component.weight", ratio=True),
                "hard_gate": _boolean(raw.get("hard_gate")),
            }
            for raw in self.objects(scorer, "components", scorer_path, required=True)
        ]
        prompt_provenance = _mapping(definition.get("prompt_provenance"))
        goal = _mapping(definition.get("goal_segment"))
        source = self.source(source_id)
        overview = _task_overview(prompt, goal, definition, environment, scorer, observables)
        provenance_grade = _text((membership or {}).get("provenance_grade"), 150)
        if provenance_grade is not None and provenance_grade not in (
            "exact_replay",
            "anchored_reconstruction",
            "adjacent_synthesis",
        ):
            self.warn(provenance_path, "unsupported curated provenance grade")
            provenance_grade = None
        ref = _ref("task", json.dumps([name, suite_id, task_id], separators=(",", ":")))
        payload: dict[str, Any] = {
            "name": name,
            "task_id": task_id,
            "suite_id": suite_id,
            "source": source,
            "kind": _text(public.get("kind") or provenance.get("kind"), 150),
            "build_id": _text(provenance.get("build_id"), 250),
            "published_at": _text(provenance.get("published_at"), 150),
            "prompt": prompt,
            "prompt_truncated": isinstance(prompt_value, str) and len(prompt_value) > MAX_PROMPT_CHARS,
            "repository": repository,
            "hashes": hashes,
            "environment": {
                "image": _text(image, 500),
                "pinned": pinned,
                "environment_id": (
                    _text(environment.get("environment_id"), 100)
                    if isinstance(environment.get("environment_id"), str)
                    and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", environment["environment_id"])
                    else None
                ),
                "network": _text(
                    _mapping(public.get("environment")).get("network")
                    or environment.get("network_during_run"),
                    150,
                ),
                "validated": {
                    key: _boolean(_mapping(environment.get("validated")).get(key))
                    for key in ("base", "outcome")
                },
                "validation_runs": self.number(
                    _mapping(environment.get("validated")).get("runs"),
                    environment_path,
                    "validated.runs",
                    integer=True,
                ),
            },
            "provenance": {
                "user_event_ids": _strings(prompt_provenance.get("user_event_ids")),
                "prompt_mode": _text(prompt_provenance.get("mode"), 150),
                "goal": {
                    "introduced_event_id": _text(goal.get("introduced_event_id"), 200),
                    "closed_event_id": _text(goal.get("closed_event_id"), 200),
                    "summary": _text(goal.get("summary")),
                },
            },
            "rubric": {
                "observables": observables,
                "regressions_to_protect": _strings(brief.get("regressions_to_protect"), 1000),
                "forbidden_shortcuts": _strings(brief.get("forbidden_shortcuts"), 1000),
            },
            "scorer": {
                "mode": _text(scorer.get("mode"), 150),
                "pass_threshold": self.number(
                    scorer.get("pass_threshold", _mapping(public.get("scoring")).get("pass_threshold")),
                    scorer_path,
                    "pass_threshold",
                    ratio=True,
                ),
                "components": components,
            },
            "audit": {
                "decision": audit_decision,
                "counts": {
                    key: len(audit[key]) if isinstance(audit.get(key), list) else None
                    for key in ("leakage", "overfit_checks", "missing_observables", "mutants", "evidence")
                },
            },
            "validation": validation,
            "membership": membership,
            "provenance_grade": provenance_grade,
            "project": self.projects.get(source["session_key"]) or name,
            "overview": overview,
            "limits": {
                "wall_time_seconds": self.number(
                    _mapping(public.get("limits")).get("wall_time_seconds"), public_path,
                    "limits.wall_time_seconds", integer=True,
                ),
                "max_output_chars": self.number(
                    _mapping(public.get("limits")).get("max_output_chars"), public_path,
                    "limits.max_output_chars", integer=True,
                ),
            },
            "grader_contract": {
                "mode": _text(scorer.get("mode"), 150),
                "input_schema": "retro-score-input-v1",
                "input_fields": [
                    "task_id", "attempt_id", "repo_path", "task_path",
                    "trace_path", "resource_usage_path", "seed",
                ],
                "provided_content": [
                    "Read-only final candidate repository", "Public task instructions and constraints",
                    "Declared trace/resource artifacts when provided", "Selected seed",
                ],
                "withheld_content": [
                    "Original trajectory", "Accepted solution and outcome repository",
                    "Agent/model identity", "Prior scores",
                ],
                "judge_enabled": _mapping(scorer.get("judge")).get("enabled") is True,
                "output_schema": "retro-score-report-v1",
                "output_fields": [
                    "status", "valid", "score_total", "passed", "pass_threshold",
                    "components", "hard_gate_failures", "commands", "judge", "warnings",
                ],
                "failure_rule": (
                    "A failed required check gives reward 0; "
                    "grader/infrastructure errors have no numeric reward."
                ),
            },
            "warnings": self.view.warnings[start:],
        }
        self.view.payloads.append((ref, payload))
        return {
            "task_id": task_id,
            "source": source,
            "repo_id": repository["repo_id"],
            "kind": payload["kind"],
            "prompt_preview": _text(prompt, 240),
            "active": active,
            "validation": validation["status"],
            "audit": audit_decision,
            "pass_threshold": payload["scorer"]["pass_threshold"],
            "provenance_grade": payload["provenance_grade"],
            "detail": ref,
            "warnings": len(payload["warnings"]),
            "snapshot_valid": bool(
                public
                and provenance
                and environment
                and source_link
                and scorer
                and validation_doc
                and identity_ok
                and pinned
                and validation["status"] == "passed"
                and not payload["warnings"]
            ),
            "project": payload["project"],
            "overview": overview,
            "environment": payload["environment"],
            "scorer": payload["scorer"],
            "limits": payload["limits"],
            "name": name,
            "suite_id": suite_id,
            **(membership or {}),
        }

    def suite(self, root: Path, name: str) -> dict[str, Any] | None:
        start = len(self.view.warnings)
        path = root / "manifest.json"
        doc = self.json(path, "retro-benchmark-suite-v1", required=True)
        if doc is None:
            return None
        valid = doc.get("suite_id") == root.name and doc.get("name") == name
        if not valid:
            self.warn(path, "suite identity does not match its taskset/directory")
        content_hash = self.digest(doc.get("content_sha256"), path, "content_sha256", required=True)
        try:
            manifest_hash = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in doc.items() if key != "content_sha256"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
        except (ValueError, TypeError, RecursionError):
            manifest_hash = None
        if content_hash != manifest_hash:
            valid = False
            self.warn(path, "suite manifest content checksum mismatch; comparisons disabled")
        policy = _mapping(doc.get("policy"))
        split_unit = _text(policy.get("split_unit"), 150)
        ratios = {
            split: self.number(
                _mapping(policy.get("split_ratios")).get(split), path, f"split_ratios.{split}", ratio=True
            )
            for split in SPLITS
        }
        seed = self.number(policy.get("seed"), path, "policy.seed", integer=True)
        if (
            split_unit not in ("repository", "source")
            or seed is None
            or any(value is None for value in ratios.values())
        ):
            valid = False
            self.warn(path, "suite split policy is incomplete or unsupported")
        elif abs(sum(value or 0 for value in ratios.values()) - 1) > 1e-9:
            valid = False
            self.warn(path, "suite split ratios do not sum to 1")
        tasks = []
        seen: set[str] = set()
        group_splits: dict[str, str] = {}
        unit_splits: dict[str, str] = {}
        for raw in self.array(doc, "tasks", path, required=True):
            if not isinstance(raw, dict):
                valid = False
                self.warn(path, "malformed suite task entry skipped")
                continue
            task_id = self.identifier(raw.get("task_id"), path, "tasks.task_id")
            if not task_id:
                valid = False
                continue
            if task_id in seen:
                valid = False
                self.warn(path, f"duplicate suite task {task_id} skipped")
                continue
            seen.add(task_id)
            membership: dict[str, Any] = {
                key: _text(raw.get(key), 250)
                for key in (
                    "task_id",
                    "source_id",
                    "repo_id",
                    "kind",
                    "split",
                    "group_id",
                    "family",
                    "provenance_grade",
                    "goal_summary",
                    "curation_reason",
                )
            }
            membership.update(
                {
                    "tags": _strings(raw.get("tags"), 150),
                    "priority": _text(raw.get("priority"), 150)
                    if isinstance(raw.get("priority"), str)
                    else self.number(raw.get("priority"), path, "priority"),
                    **{
                        key: self.digest(raw.get(key), path, f"tasks.{key}", required=True)
                        for key in (
                            "public_task_sha256",
                            "base_bundle_sha256",
                            "scorer_package_sha256",
                            "publication_sha256",
                            "content_sha256",
                        )
                    },
                }
            )
            if "public_environment_sha256" in raw:
                membership["public_environment_sha256"] = self.digest(
                    raw["public_environment_sha256"],
                    path,
                    "public_environment_sha256",
                    required=True,
                )
            split, group = membership["split"], membership["group_id"]
            unit = membership["repo_id"] if split_unit == "repository" else membership["source_id"]
            if split not in SPLITS or not group or not unit:
                valid = False
                self.warn(path, f"task {task_id} has incomplete split/group metadata")
            else:
                if group in group_splits and group_splits[group] != split:
                    valid = False
                    self.warn(path, f"group {group} crosses suite splits")
                if unit in unit_splits and unit_splits[unit] != split:
                    valid = False
                    self.warn(path, f"{split_unit} split unit {unit} crosses suite splits")
                group_splits[group] = split
                unit_splits[unit] = split
            summary = self.task(
                root / "tasks" / task_id,
                name,
                task_id,
                suite_id=root.name,
                membership=membership,
            )
            if not summary["snapshot_valid"]:
                valid = False
            tasks.append(summary)
        summary = {
            "tasks": len(tasks),
            "sources": len({task["source_id"] for task in tasks if task["source_id"]}),
            "repositories": len({task["repo_id"] for task in tasks if task["repo_id"]}),
            "groups": len({task["group_id"] for task in tasks if task["group_id"]}),
            "splits": {split: sum(task["split"] == split for task in tasks) for split in SPLITS},
        }
        if not tasks:
            valid = False
            self.warn(path, "suite has no trusted task copies; not a successful empty release")
        declared_summary = _mapping(doc.get("summary"))
        if any(declared_summary.get(key) != value for key, value in summary.items()):
            valid = False
            self.warn(
                path, "declared suite summary differs from task/split/group counts; observed counts shown"
            )
        groups = [
            {
                "group_id": group,
                "split": group_splits.get(group),
                "tasks": sum(task["group_id"] == group for task in tasks),
                "sources": len({task["source_id"] for task in tasks if task["group_id"] == group}),
                "repositories": len({task["repo_id"] for task in tasks if task["group_id"] == group}),
            }
            for group in sorted({task["group_id"] for task in tasks if task["group_id"]})
        ]
        notes = _strings(doc.get("warnings"), 1000)
        if split_unit == "source" and sum((value or 0) > 0 for value in ratios.values()) > 1:
            note = (
                "Source-grouped splits share repository history; "
                "they do not measure repository generalization."
            )
            if note not in notes:
                notes.append(note)
        min_confidence = self.number(policy.get("min_confidence"), path, "min_confidence", ratio=True)
        rejections = self.rejections(doc.get("rejections"), path, f"suite {root.name}")
        artifact_warnings = self.view.warnings[start:]
        return {
            "name": name,
            "suite_id": root.name,
            "created_at": _text(doc.get("created_at"), 150),
            "content_sha256": content_hash,
            "valid": bool(valid and content_hash and not artifact_warnings),
            "policy": {
                "split_unit": split_unit,
                "split_ratios": ratios,
                "seed": seed,
                "allow_reconstructed": _boolean(policy.get("allow_reconstructed")),
                "include_judged": _boolean(policy.get("include_judged")),
                "min_confidence": min_confidence,
            },
            "tasks": tasks,
            "summary": summary,
            "groups": groups,
            "rejections": rejections,
            "warnings": artifact_warnings + notes,
        }

    def cohort(
        self, root: Path, name: str, suites: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str]:
        path = root / "cohort.json"
        doc = self.json(path, "retro-eval-cohort-v1", required=True)
        if doc is None:
            return None, "unbound"
        suite_id = self.identifier(doc.get("suite_id"), path, "suite_id")
        suite = suites.get(suite_id or "")
        task_ids = [
            task_id
            for raw in self.array(doc, "task_ids", path, required=True)
            if (task_id := self.identifier(raw, path, "task_ids entry")) is not None
        ]
        raw_seeds = self.array(doc, "seeds", path, required=True)
        seeds = [
            number
            for raw in raw_seeds
            if (number := self.number(raw, path, "seeds entry", integer=True)) is not None
        ]
        split = _text(doc.get("split"), 150)
        content_hash = self.digest(
            doc.get("suite_content_sha256"), path, "suite_content_sha256", required=True
        )
        valid = bool(
            doc.get("name") == name
            and suite
            and suite["valid"]
            and content_hash == suite["content_sha256"]
            and split in (*SPLITS, "all")
            and task_ids
            and len(task_ids) == len(set(task_ids))
            and seeds
            and len(seeds) == len(set(seeds))
            and len(seeds) == len(raw_seeds)
            and len(task_ids) == len(self.array(doc, "task_ids", path))
        )
        expected = {
            task["task_id"]
            for task in (suite or {}).get("tasks", [])
            if split == "all" or task["split"] == split
        }
        expected_sources = {
            task["task_id"]: task.get("source_id") or task["source"]["source_id"]
            for task in (suite or {}).get("tasks", [])
            if task["task_id"] in task_ids
        }
        if "task_sources" in doc and doc["task_sources"] != expected_sources:
            valid = False
            self.warn(path, "cohort task_sources differs from frozen task provenance; comparisons disabled")
        if not valid or not set(task_ids) <= expected:
            valid = False
            self.warn(
                path,
                "cohort does not match an intact frozen suite, its hash, split, tasks and seeds; "
                "comparisons disabled",
            )
        return {
            "name": _text(doc.get("name"), 250),
            "suite_id": suite_id,
            "suite_content_sha256": content_hash,
            "split": split,
            "task_ids": task_ids,
            "seeds": seeds,
            "task_sources": expected_sources,
        }, "verified" if valid else "incompatible"

    def task_aggregate(self, raw: dict[str, Any], path: Path) -> dict[str, Any] | None:
        task_id = self.identifier(raw.get("task_id"), path, "tasks.task_id")
        if not task_id:
            return None
        requested = self.number(raw.get("requested_attempts"), path, "requested_attempts", integer=True)
        scored = self.number(raw.get("scored_attempts"), path, "scored_attempts", integer=True)
        if requested is None or scored is None or scored > requested:
            self.warn(path, f"task {task_id} has inconsistent requested/scored counts")
        mean = self.number(raw.get("mean_score"), path, "mean_score", ratio=True) if scored else None
        if scored and mean is None:
            self.warn(path, f"task {task_id} has scored attempts but no published mean reward")
        return {
            "task_id": task_id,
            "source_id": _text(raw.get("source_id"), 250),
            "requested_attempts": requested,
            "scored_attempts": scored,
            "mean_score": mean,
            "std_score": self.number(raw.get("std_score"), path, "std_score", ratio=True) if scored else None,
            "pass_rate": self.number(raw.get("pass_rate"), path, "pass_rate", ratio=True) if scored else None,
            "pass_threshold": self.number(raw.get("pass_threshold"), path, "pass_threshold", ratio=True),
            "status_counts": self.numbers(raw.get("status_counts"), path, "status_counts", integer=True),
        }

    def agent(self, raw: dict[str, Any], path: Path) -> dict[str, Any] | None:
        agent_id = self.identifier(raw.get("agent_id"), path, "agent_id")
        if not agent_id:
            return None
        requested = self.number(raw.get("requested_attempts"), path, "requested_attempts", integer=True)
        scored = self.number(raw.get("scored_attempts"), path, "scored_attempts", integer=True)
        if requested is None or scored is None or scored > requested:
            self.warn(path, f"agent {agent_id} has inconsistent requested/scored attempt counts")
        if not scored and raw.get("benchmark_score") is not None:
            self.warn(path, f"agent {agent_id} has no scored attempts; numeric reward suppressed")
        resources = _mapping(raw.get("resources"))
        tasks = [
            item
            for raw_task in self.objects(raw, "tasks", path, required=True)
            if (item := self.task_aggregate(raw_task, path)) is not None
        ]
        score = (
            self.number(raw.get("benchmark_score"), path, "benchmark_score", ratio=True) if scored else None
        )
        pass_rate = self.number(raw.get("pass_rate"), path, "pass_rate", ratio=True) if scored else None
        coverage = self.number(raw.get("valid_coverage"), path, "valid_coverage", ratio=True)
        interval = _mapping(raw.get("score_interval"))
        score_interval = None
        if interval:
            lower = self.number(interval.get("lower"), path, "score_interval.lower", ratio=True)
            upper = self.number(interval.get("upper"), path, "score_interval.upper", ratio=True)
            sources = self.number(interval.get("sources"), path, "score_interval.sources", integer=True)
            if (
                interval.get("method") != "source_percentile_bootstrap"
                or interval.get("confidence") != 0.95
                or interval.get("conditional_on_scored_sources") is not True
                or lower is None or upper is None or lower > upper
                or sources is None or sources < 2
            ):
                self.warn(path, f"agent {agent_id} has an invalid source score interval")
            else:
                score_interval = {
                    "method": "source_percentile_bootstrap", "confidence": 0.95,
                    "lower": lower, "upper": upper, "sources": sources,
                    "conditional_on_scored_sources": True,
                }
        if scored and (score is None or pass_rate is None):
            self.warn(
                path, f"agent {agent_id} has scored attempts but incomplete published score/pass metrics"
            )
        if coverage is None or (
            requested and scored is not None and abs(coverage - scored / requested) > 1e-6
        ):
            self.warn(path, f"agent {agent_id} coverage differs from scored/requested attempts")
        return {
            "agent_id": agent_id,
            "requested_attempts": requested,
            "scored_attempts": scored,
            "benchmark_score": score,
            "pass_rate": pass_rate,
            "valid_coverage": coverage,
            "score_interval": score_interval,
            "pass_at_k": self.numbers(raw.get("pass_at_k"), path, "pass_at_k", ratio=True),
            "status_counts": self.numbers(raw.get("status_counts"), path, "status_counts", integer=True),
            "error_counts": self.numbers(raw.get("error_counts"), path, "error_counts", integer=True),
            "component_means": self.numbers(raw.get("component_means"), path, "component_means", ratio=True)
            if scored
            else {},
            "tasks": tasks,
            "resources": {
                **{
                    key: self.number(resources.get(key), path, f"resources.{key}")
                    for key in (
                        "scored_attempts",
                        "wall_time_ms_mean",
                        "wall_time_ms_total",
                        "cost_usd_mean",
                        "cost_usd_total",
                    )
                },
                "tokens_mean": self.numbers(resources.get("tokens_mean"), path, "tokens_mean"),
                "tokens_total": self.numbers(
                    resources.get("tokens_total"), path, "tokens_total", integer=True
                ),
            },
        }

    def attempt(self, path: Path, task_id: str, agent_id: str, seed: int) -> dict[str, Any] | None:
        doc = self.json(path, "retro-benchmark-attempt-v1", required=True)
        if doc is None:
            return None
        if isinstance(doc.get("seed"), bool) or (
            doc.get("task_id"),
            doc.get("agent_id"),
            doc.get("seed"),
        ) != (task_id, agent_id, seed):
            self.warn(path, "attempt task/agent/seed identity differs from its canonical directory; skipped")
            return None
        status = _text(doc.get("status"), 150)
        if status not in ATTEMPT_STATUSES:
            self.warn(path, "unknown attempt status; reward shown as unavailable")
        score = self.number(doc.get("score"), path, "score", ratio=True) if status == "scored" else None
        if status != "scored" and doc.get("score") is not None:
            self.warn(
                path, "unscored attempt carries a numeric reward; suppressed (errors are not zero rewards)"
            )
        if status == "scored" and (score is None or _boolean(doc.get("passed")) is None):
            self.warn(path, "scored attempt is missing a valid reward/pass decision")
        projected: dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "seed": seed,
            "status": status,
            "attempt_id": _text(doc.get("attempt_id"), 250),
            "source_id": _text(doc.get("source_id"), 250),
            "score": score,
            "passed": _boolean(doc.get("passed")) if score is not None else None,
            "pass_threshold": self.number(doc.get("pass_threshold"), path, "pass_threshold", ratio=True),
            "components": [
                {
                    "id": _text(raw.get("id"), 150),
                    "kind": _text(raw.get("kind"), 150),
                    "weight": self.number(raw.get("weight"), path, "component.weight", ratio=True),
                    "value": self.number(raw.get("value"), path, "component.value", ratio=True)
                    if status == "scored" and raw.get("scored") is not False
                    else None,
                    "hard_gate": _boolean(raw.get("hard_gate")),
                    "gate_passed": _boolean(raw.get("gate_passed")),
                    "evidence": [
                        {
                            "kind": _text(evidence.get("kind"), 100),
                            "ref": _text(evidence.get("ref"), 300),
                            "summary": _text(evidence.get("summary"), 1500, multiline=True),
                        }
                        for evidence in self.objects(raw, "evidence", path)
                    ],
                }
                for raw in self.objects(doc, "components", path)
            ],
            "tokens": self.numbers(doc.get("tokens"), path, "tokens", integer=True),
            "wall_time_ms": self.number(doc.get("wall_time_ms"), path, "wall_time_ms", integer=True),
            "cost_usd": self.number(doc.get("cost_usd"), path, "cost_usd"),
            "error": self.error(doc.get("error")),
            "warnings": _strings(doc.get("warnings"), 500),
            "hashes": {
                key: self.digest(doc.get(key), path, key, required=key != "candidate_state_sha256")
                for key in (
                    "agent_config_sha256",
                    "base_bundle_sha256",
                    "scorer_package_sha256",
                    "candidate_state_sha256",
                    "input_sha256",
                )
            },
            "created_at": _text(doc.get("created_at"), 150),
        }
        if score is not None and projected["passed"] is False:
            failed_components = [
                component for component in projected["components"]
                if component["hard_gate"] and component["gate_passed"] is False
            ]
            reasons = [
                evidence["summary"]
                for component in failed_components
                for evidence in component["evidence"]
                if evidence["summary"]
            ]
            if reasons:
                projected["failure_reason"] = " ".join(reasons)
            elif failed_components:
                projected["failure_reason"] = "Required checks failed: " + ", ".join(
                    component["id"] or "unknown" for component in failed_components
                )
            else:
                projected["failure_reason"] = (
                    f"Reward {score:g} is below the pass threshold {projected['pass_threshold']}."
                )
        else:
            projected["failure_reason"] = projected["error"]
        return projected

    def evaluation(self, root: Path, name: str, suites: dict[str, dict[str, Any]]) -> dict[str, Any]:
        start = len(self.view.warnings)
        cohort, cohort_status = self.cohort(root, name, suites)
        results_path = root / "results.json"
        results_start = len(self.view.warnings)
        results = self.json(results_path, "retro-benchmark-aggregate-v1", required=True)
        agents: list[dict[str, Any]] = [
            agent
            for raw in self.objects(results or {}, "agents", results_path, required=True)
            if (agent := self.agent(raw, results_path)) is not None
        ]
        agents.sort(key=lambda agent: agent["agent_id"])
        if results and (results.get("name"), results.get("eval_id")) != (name, root.name):
            self.warn(results_path, "aggregate identity differs from its taskset/evaluation directory")
        if results and not agents:
            self.warn(results_path, "no usable agent summaries; comparison unavailable")
        if results and "cohort" in results:
            embedded = results["cohort"]
            keys = ("name", "suite_id", "suite_content_sha256", "split", "task_ids", "seeds")
            if (
                not isinstance(embedded, dict)
                or cohort is None
                or any(embedded.get(key) != cohort.get(key) for key in keys)
                or (
                    "task_sources" in embedded
                    and embedded["task_sources"] != (cohort or {}).get("task_sources")
                )
            ):
                self.warn(results_path, "aggregate cohort differs from cohort.json; comparisons disabled")
        allowed = cohort_status == "verified" and bool(results) and len(self.view.warnings) == results_start
        attempts: list[dict[str, Any]] = []
        attempt_read_start = len(self.view.warnings)
        for task_root in self.children(root / "attempts"):
            for agent_root in self.children(task_root):
                for seed_root in self.children(agent_root):
                    match = _SEED.fullmatch(seed_root.name)
                    if not match:
                        self.warn(seed_root, "non-canonical attempt seed directory skipped")
                        allowed = False
                        continue
                    attempt_path = seed_root / "attempt.json"
                    if not attempt_path.exists() and not attempt_path.is_symlink():
                        continue
                    attempt_start = len(self.view.warnings)
                    attempt = self.attempt(
                        attempt_path,
                        task_root.name,
                        agent_root.name,
                        int(match.group(1)),
                    )
                    if len(self.view.warnings) != attempt_start:
                        allowed = False
                    if attempt is not None:
                        attempts.append(attempt)
        if len(self.view.warnings) != attempt_read_start:
            allowed = False
        agent_ids = [agent["agent_id"] for agent in agents]
        agent_id_set = set(agent_ids)
        by_agent: dict[str, list[dict[str, Any]]] = {}
        by_agent_task: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for attempt in attempts:
            by_agent.setdefault(attempt["agent_id"], []).append(attempt)
            by_agent_task.setdefault((attempt["agent_id"], attempt["task_id"]), []).append(attempt)
        if len(set(agent_ids)) != len(agent_ids):
            allowed = False
            self.warn(results_path, "duplicate agent IDs; comparisons disabled")
        suite_tasks = {
            task["task_id"]: task
            for task in suites.get((cohort or {}).get("suite_id", ""), {}).get("tasks", [])
        }
        if cohort:
            for agent in agents:
                task_ids = [task["task_id"] for task in agent["tasks"]]
                if len(set(task_ids)) != len(task_ids) or not set(task_ids) <= set(cohort["task_ids"]):
                    allowed = False
                    self.warn(
                        results_path, "aggregate tasks fall outside the evaluation cohort or are duplicated"
                    )
            for attempt in attempts:
                if attempt["task_id"] not in cohort["task_ids"] or attempt["seed"] not in cohort["seeds"]:
                    allowed = False
                    self.warn(
                        root / "cohort.json",
                        "attempt task/seed falls outside the bound cohort; comparisons disabled",
                    )
                expected = suite_tasks.get(attempt["task_id"], {})
                for key in ("base_bundle_sha256", "scorer_package_sha256"):
                    if expected and attempt["hashes"].get(key) != expected.get(key):
                        allowed = False
                        self.warn(
                            results_path, f"attempt {key} differs from the frozen suite; comparisons disabled"
                        )
                expected_source = cohort["task_sources"].get(attempt["task_id"])
                if attempt["source_id"] is not None and attempt["source_id"] != expected_source:
                    allowed = False
                    self.warn(results_path, "attempt source differs from frozen cohort task_sources")
                if attempt["source_id"] is None:
                    attempt["source_id"] = expected_source
                if expected and attempt["pass_threshold"] != expected.get("pass_threshold"):
                    allowed = False
                    self.warn(results_path, "attempt pass threshold differs from frozen scorer metadata")
        missing_attempts: list[dict[str, Any]] = []
        for agent in agents:
            records = by_agent.get(agent["agent_id"], [])
            statuses = dict(Counter(attempt["status"] for attempt in records))
            expected_requested = len(cohort["task_ids"]) * len(cohort["seeds"]) if cohort else len(records)
            published_statuses = agent["status_counts"]
            if (
                agent["requested_attempts"] != expected_requested
                or agent["scored_attempts"] != sum(attempt["score"] is not None for attempt in records)
                or published_statuses.get("scored", 0) != statuses.get("scored", 0)
                or any((published_statuses.get(key) or 0) < value for key, value in statuses.items())
                or sum(value or 0 for value in published_statuses.values()) > expected_requested
            ):
                allowed = False
                self.warn(
                    results_path, f"agent {agent['agent_id']} aggregate counts differ from published attempts"
                )
            if cohort:
                task_lookup = {task["task_id"]: task for task in agent["tasks"]}
                for task_id in cohort["task_ids"]:
                    task_records = by_agent_task.get((agent["agent_id"], task_id), [])
                    scored_records = sum(attempt["score"] is not None for attempt in task_records)
                    requested_seeds = len(cohort["seeds"])
                    expected_task = suite_tasks.get(task_id, {})
                    task = task_lookup.get(task_id)
                    if task is None:
                        if task_records:
                            allowed = False
                            self.warn(
                                results_path, f"agent {agent['agent_id']} task {task_id} summary missing"
                            )
                        task = {
                            "task_id": task_id,
                            "source_id": cohort["task_sources"].get(task_id),
                            "requested_attempts": requested_seeds,
                            "scored_attempts": 0,
                            "mean_score": None,
                            "std_score": None,
                            "pass_rate": None,
                            "status_counts": {},
                            "pass_threshold": expected_task.get("pass_threshold"),
                        }
                        agent["tasks"].append(task)
                    if (
                        task["scored_attempts"] != scored_records
                        or (task["requested_attempts"] or 0) > requested_seeds
                        or task["source_id"] != cohort["task_sources"].get(task_id)
                    ):
                        allowed = False
                        self.warn(
                            results_path, f"agent {agent['agent_id']} task {task_id} evidence is incompatible"
                        )
                    task["cohort_requested_attempts"] = requested_seeds
                    task["missing_attempts"] = requested_seeds - len(task_records)
                    present_seeds = {attempt["seed"] for attempt in task_records}
                    for seed in cohort["seeds"]:
                        if seed not in present_seeds:
                            missing_attempts.append(
                                {
                                    "task_id": task_id,
                                    "agent_id": agent["agent_id"],
                                    "seed": seed,
                                    "source_id": cohort["task_sources"].get(task_id),
                                    "status": "missing_artifact",
                                    "score": None,
                                    "passed": None,
                                    "synthetic": True,
                                    "error": (
                                        "Requested cohort cell has no published attempt.json; "
                                        "reward unavailable."
                                    ),
                                }
                            )
                agent["missing_attempts"] = expected_requested - len(records)
            configs = {attempt["hashes"]["agent_config_sha256"] for attempt in records}
            if len(configs) > 1:
                allowed = False
                self.warn(
                    results_path,
                    f"agent {agent['agent_id']} uses incompatible configurations within this evaluation",
                )
        if any(attempt["agent_id"] not in agent_id_set for attempt in attempts):
            allowed = False
            self.warn(results_path, "published attempts have agents missing from the aggregate")
        if missing_attempts:
            self.warn(
                results_path,
                f"{len(missing_attempts)} requested cohort cells have no attempt artifact; "
                "coverage is reduced, not zero reward",
            )
        if cohort_status == "unbound":
            self.warn(
                root / "cohort.json",
                "legacy/unbound evaluation; suite compatibility unknown; comparisons disabled",
            )
        if not allowed and cohort_status == "verified":
            self.warn(
                results_path,
                "comparison unavailable: aggregate/attempt evidence is incomplete or incompatible",
            )
        ref = _ref("eval", f"{name}/{root.name}")
        payload: dict[str, Any] = {
            "name": name,
            "eval_id": root.name,
            "generated_at": _text((results or {}).get("generated_at"), 150),
            "cohort": cohort,
            "cohort_status": cohort_status,
            "comparison_allowed": allowed,
            "agents": agents,
            "attempts": attempts,
            "missing_attempts": missing_attempts,
            "warnings": self.view.warnings[start:],
        }
        self.view.payloads.append((ref, payload))
        return {
            "eval_id": root.name,
            "generated_at": payload["generated_at"],
            "cohort": cohort,
            "cohort_status": cohort_status,
            "comparison_allowed": allowed,
            "agents": len(agents),
            "attempts": len(attempts),
            "missing_attempts": len(missing_attempts),
            "warnings": len(payload["warnings"]),
            "detail": ref,
        }
