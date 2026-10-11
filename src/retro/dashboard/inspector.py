"""On-demand local inspection of private grader inputs, code and outputs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..benchmarks.task_scorer.build import resolve_taskset_paths
from ..benchmarks.task_scorer.ghostlab_cli import sha256_file, sha256_text
from ..benchmarks.task_scorer.run import PublishedTask, verify_published_task
from ..benchmarks.task_scorer.schema import SCHEMA_ASSET_DIR
from ..benchmarks.task_scorer.suite import load_suite
from ..storage import Layout
from .benchmarks import _text

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TEXT_SUFFIXES = {".py", ".sh", ".js", ".mjs", ".cjs", ".md", ".txt", ".json", ".yaml", ".yml"}
_SECRET_FIELDS = re.compile(
    r"(?i)(password|passwd|secret|credential|authorization|api[_-]?key|access[_-]?token|^token$|^env$)"
)
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARS = 64 * 1024
_MAX_PACKAGE_CHARS = 256 * 1024


class InspectionError(ValueError):
    pass


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in {".", ".."}:
        raise InspectionError(f"Invalid {label}")
    return value


def _safe_path(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (ValueError, OSError) as exc:
        raise InspectionError(f"Inspection artifact is missing or outside its archive: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise InspectionError(f"Inspection does not follow artifact symlinks: {current}")
    return resolved


def _json(root: Path, path: Path) -> dict[str, Any]:
    checked = _safe_path(root, path)
    if checked.stat().st_size > _MAX_JSON_BYTES:
        raise InspectionError(f"Inspection JSON exceeds 2 MiB: {path}")
    try:
        value = json.loads(checked.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InspectionError(f"Cannot read inspection JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InspectionError(f"Inspection JSON is not an object: {path}")
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _text(value, _MAX_TEXT_CHARS, multiline=True, preserve_code=True)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[redacted]" if _SECRET_FIELDS.search(str(key)) else _redact(item)
            for key, item in value.items()
        }
    return value


def _published_task(layout: Layout, name: str, task_id: str, suite_id: str | None) -> PublishedTask:
    paths = resolve_taskset_paths(layout, _identifier(name, "collection"))
    _identifier(task_id, "task ID")
    if suite_id is not None:
        _identifier(suite_id, "dataset version")
        _safe_path(layout.root, paths.root / "suites" / suite_id)
        paths = load_suite(paths, suite_id).paths
    _safe_path(layout.root, paths.task_dir(task_id))
    return verify_published_task(paths, task_id)


def _packaged_path(package: Path, value: str) -> Path:
    relative = value.removeprefix("/scorer/")
    if Path(relative).is_absolute() or ".." in Path(relative).parts or "\\" in relative:
        raise InspectionError(f"Grader reference is not package-relative: {value}")
    return package / relative


def inspect_grader(layout: Layout, name: str, task_id: str, *, suite_id: str | None = None) -> dict[str, Any]:
    """Expose private code only on demand, never in a static task shard."""
    task = _published_task(layout, name, task_id, suite_id)
    package = task.scorer_manifest_path.parent
    manifest = _json(layout.root, task.scorer_manifest_path)
    public_task = _json(layout.root, task.public_task_path)
    judge = manifest.get("judge")
    judge_enabled = isinstance(judge, dict) and judge.get("enabled") is True
    files: list[dict[str, Any]] = []
    notes = ["Obvious credentials are redacted. Judge agent configuration contents are not published."]
    used_chars = 0
    judge_config_path = None
    if judge_enabled and isinstance(judge, dict) and isinstance(judge.get("agent_config"), str):
        judge_config_path = _packaged_path(package, judge["agent_config"])
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise InspectionError(f"Inspection does not follow grader symlinks: {path}")
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES or path == judge_config_path:
            continue
        relative = path.relative_to(package)
        if any(part.startswith(".") or part in {"node_modules", "__pycache__"} for part in relative.parts):
            continue
        if path == task.scorer_manifest_path:
            continue
        if len(files) >= 64 or used_chars >= _MAX_PACKAGE_CHARS:
            notes.append(
                "The grader package exceeds the inspection limit; remaining files are not displayed."
            )
            break
        checked = _safe_path(layout.root, path)
        try:
            with checked.open(encoding="utf-8") as handle:
                text = handle.read(min(_MAX_TEXT_CHARS, _MAX_PACKAGE_CHARS - used_chars) + 1)
        except (OSError, UnicodeError) as exc:
            raise InspectionError(f"Cannot read grader text {relative}: {exc}") from exc
        limit = min(_MAX_TEXT_CHARS, _MAX_PACKAGE_CHARS - used_chars)
        truncated = len(text) > limit
        text = text[:limit]
        used_chars += len(text)
        files.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_file(checked),
                "content": _text(text, limit, multiline=True, preserve_code=True),
                "truncated": truncated,
            }
        )
    judge_prompt = None
    if judge_enabled and isinstance(judge, dict) and isinstance(judge.get("prompt"), str):
        prompt_path = _packaged_path(package, judge["prompt"])
        checked = _safe_path(layout.root, prompt_path)
        if checked.stat().st_size > _MAX_TEXT_CHARS:
            raise InspectionError("Judge prompt exceeds the 64 KiB inspection limit")
        judge_prompt = _text(
            checked.read_text(encoding="utf-8"), _MAX_TEXT_CHARS, multiline=True, preserve_code=True
        )
    schema = json.loads((SCHEMA_ASSET_DIR / "score-report.schema.json").read_text(encoding="utf-8"))
    safe_manifest = {
        key: manifest.get(key)
        for key in (
            "schema_version",
            "task_id",
            "mode",
            "entrypoint",
            "components",
            "pass_threshold",
            "required_artifacts",
            "package_sha256",
        )
    }
    runtime = manifest.get("runtime")
    safe_manifest["runtime"] = (
        {
            key: runtime.get(key)
            for key in ("image", "network", "timeout_seconds", "cpu", "memory_mb", "candidate_mount")
        }
        if isinstance(runtime, dict)
        else None
    )
    return {
        "task_id": task_id,
        "name": name,
        "suite_id": suite_id,
        "scorer_package_sha256": task.scorer_package_sha256,
        "manifest": _redact(safe_manifest),
        "public_task": _redact(public_task),
        "prompt_status": "pinned_judge_prompt" if judge_enabled else "no_llm_prompt",
        "judge_prompt": judge_prompt,
        "input_contract": {
            "schema_version": "retro-score-input-v1",
            "repo_path": "/candidate/repo",
            "task_path": "/input/task.json",
            "trace_path": "Optional declared agent trace; not the original trajectory",
            "resource_usage_path": "Optional declared resource report",
            "seed": "Selected per attempt",
        },
        "output_schema": schema,
        "files": files,
        "notes": notes,
    }


def inspect_run(
    layout: Layout,
    name: str,
    eval_id: str,
    task_id: str,
    agent_id: str,
    seed: int,
) -> dict[str, Any]:
    """Return recorded scorer input/output and linked evidence for one attempt."""
    for value, label in (
        (name, "collection"),
        (eval_id, "run ID"),
        (task_id, "task ID"),
        (agent_id, "agent ID"),
    ):
        _identifier(value, label)
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise InspectionError("Run seed must be a non-negative integer")
    paths = resolve_taskset_paths(layout, name)
    attempt_dir = paths.attempt_dir(eval_id, task_id, agent_id, seed)
    attempt = _json(layout.root, attempt_dir / "attempt.json")
    if (attempt.get("task_id"), attempt.get("agent_id"), attempt.get("seed")) != (task_id, agent_id, seed):
        raise InspectionError("Attempt identity differs from its canonical directory")
    artifact_paths = {
        "score_input": attempt_dir / "scorer" / "scorer-staging" / "input" / "score-input.json",
        "score_report": attempt_dir / "score-report.json",
        "scorer_attestation": attempt_dir / "scorer" / "scorer-run.json",
    }
    documents: dict[str, dict[str, Any] | None] = {}
    missing: list[str] = []
    for label, path in artifact_paths.items():
        if not path.exists() and not path.is_symlink():
            documents[label] = None
            missing.append(label)
        else:
            documents[label] = _json(layout.root, path)
    report = documents["score_report"]
    if report is not None and (report.get("task_id"), report.get("attempt_id")) != (
        task_id,
        attempt.get("attempt_id"),
    ):
        raise InspectionError("Score report identity does not match this attempt")
    score_input = documents["score_input"]
    if score_input is not None and (
        score_input.get("task_id"),
        score_input.get("attempt_id"),
        score_input.get("seed"),
    ) != (task_id, attempt.get("attempt_id"), seed):
        raise InspectionError("Recorded grader input belongs to a different attempt")
    attestation = documents["scorer_attestation"]
    hashes = attestation.get("hashes") if isinstance(attestation, dict) else None
    if score_input is not None and isinstance(hashes, dict):
        actual = sha256_text(json.dumps(score_input, sort_keys=True, separators=(",", ":")))
        if hashes.get("score_input_sha256") != actual:
            raise InspectionError("Recorded grader input checksum differs from its attestation")
    cohort_path = paths.eval_dir(eval_id) / "cohort.json"
    cohort = _json(layout.root, cohort_path) if cohort_path.exists() else None
    return {
        "name": name,
        "eval_id": eval_id,
        "attempt": _redact(attempt),
        **{key: _redact(value) for key, value in documents.items()},
        "cohort": _redact(cohort),
        "missing_artifacts": missing,
        "input_status": "recorded" if score_input is not None else "not_retained",
        "inspection_only": True,
        "notes": [
            "These are saved artifacts, not a new grader execution.",
            "Missing input/output artifacts are not synthesized.",
            "The candidate repository is identified by its saved hash; "
            "its full contents are not embedded here.",
        ],
    }
