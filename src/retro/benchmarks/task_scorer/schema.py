"""Versioned contracts for the rollout-to-task-and-scorer pipeline.

Every artifact exchanged between Retro and Ghostlab is a JSON document carrying
an explicit ``schema_version``. Parsing is strict: unknown keys, missing keys,
wrong types, and out-of-range values raise :class:`SchemaError` instead of being
silently coerced. The canonical event schema stays in ``retro.schema``; this
module never redefines it.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

SOURCE_BUNDLE_SCHEMA = "retro-source-bundle-v1"
PROJECT_ENVIRONMENT_SCHEMA = "retro-project-environment-v1"
TASK_DEFINITIONS_SCHEMA = "retro-task-definitions-v1"
SCORER_SCHEMA = "retro-scorer-v1"
SCORE_INPUT_SCHEMA = "retro-score-input-v1"
SCORE_REPORT_SCHEMA = "retro-score-report-v1"
BENCHMARK_TASK_SCHEMA = "retro-benchmark-task-v1"
BENCHMARK_ATTEMPT_SCHEMA = "retro-benchmark-attempt-v1"
_PINNED_IMAGE_RE = re.compile(r"^(?:[^@\s]+@)?sha256:[0-9a-f]{64}$")

REJECTION_CODES: tuple[str, ...] = (
    "NO_NORMALIZED_ROLLOUT",
    "NO_REPO_CWD",
    "NOT_GIT_REPOSITORY",
    "NO_EXACT_BASE_SHA",
    "DIRTY_START_STATE",
    "NO_OUTCOME_SHA",
    "OUTCOME_NOT_DURABLE",
    "ENVIRONMENT_UNAVAILABLE",
    "NO_STABLE_GOAL",
    "NO_OBSERVABLE_OUTCOME",
    "MULTI_GOAL_NOT_SEPARABLE",
    "PROMPT_ORACLE_LEAKAGE",
    "BASE_ALREADY_PASSES",
    "ORACLE_DOES_NOT_PASS",
    "SCORER_NONDETERMINISTIC",
    "SCORER_OVERFIT",
    "SCORER_UNSAFE",
    "BUILDER_CONTRACT_ERROR",
    "HARNESS_ERROR",
)

BASE_RESOLUTIONS: tuple[str, ...] = (
    "captured_start",
    "rollout_command",
    "first_commit_parent",
    "unresolved",
)
USABLE_BASE_RESOLUTIONS: tuple[str, ...] = BASE_RESOLUTIONS[:3]
STATE_CONFIDENCES: tuple[str, ...] = ("exact_clean_commit", "approximate")
OUTCOME_RESOLUTIONS: tuple[str, ...] = (
    "linked_pr_merge",
    "rollout_commit",
    "captured_end",
    "unresolved",
)
TASK_KINDS: tuple[str, ...] = ("replay", "adjacent")
ADJACENCY_OPERATORS: tuple[str, ...] = (
    "sibling_transfer",
    "boundary_extension",
    "correction_regression",
    "performance_constraint",
)
SCORER_MODES: tuple[str, ...] = ("deterministic", "judge", "hybrid", "agentic")
COMPONENT_KINDS: tuple[str, ...] = ("deterministic", "judge", "agentic", "performance")
SCORE_STATUSES: tuple[str, ...] = (
    "scored",
    "invalid_candidate_artifact",
    "scorer_error",
    "scorer_timeout",
    "judge_unavailable",
)
ATTEMPT_STATUSES: tuple[str, ...] = (
    "scored",
    "agent_error",
    "agent_timeout",
    "model_unavailable",
    "invalid_candidate_artifact",
    "invalid_result",
    "scorer_error",
    "scorer_timeout",
    "judge_unavailable",
    "harness_error",
)
OBSERVABLE_IMPORTANCES: tuple[str, ...] = ("gate", "soft")
ENVIRONMENT_SOURCES: tuple[str, ...] = (
    "explicit",
    "project_container",
    "ci_derived",
    "repolaunch",
)

MAX_PROMPT_CHARS = 4000
MAX_REPLAY_TASKS = 3
MAX_ADJACENT_PER_REPLAY = 1
MAX_TASKS_PER_SOURCE = 6
WEIGHT_TOLERANCE = 1e-9
MAX_UNSCORED_WEIGHT = 0.20

_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REF_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{20}$")
_WS_RE = re.compile(r"\s+")
_REQUIRED = object()


class SchemaError(ValueError):
    """Raised when a pipeline artifact violates its versioned contract."""


# ---------------------------------------------------------------------------
# canonical serialization helpers
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Return a stable JSON encoding suitable for content hashing."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_prompt(prompt: str) -> str:
    """Whitespace- and unicode-normalized prompt used for stable task IDs."""
    return _WS_RE.sub(" ", unicodedata.normalize("NFC", prompt)).strip()


def compute_task_id(source_id: str, base_tree: str, kind: str, prompt: str) -> str:
    """``sha256(source_id + base_tree + kind + normalized_prompt)[:20]``."""
    if kind not in TASK_KINDS:
        raise SchemaError(f"kind must be one of {TASK_KINDS}, got {kind!r}")
    material = source_id + base_tree + kind + normalize_prompt(prompt)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# primitive validators
# ---------------------------------------------------------------------------


def _mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{where} must be an object")
    return dict(value)


def _known_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise SchemaError(f"{where} has unknown keys: {', '.join(unknown)}")


def _schema_version(data: Mapping[str, Any], expected: str, where: str) -> None:
    actual = data.get("schema_version")
    if actual != expected:
        raise SchemaError(f"{where} schema_version must be {expected!r}, got {actual!r}")


def _string(data: Mapping[str, Any], key: str, where: str, *, allow_empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise SchemaError(f"{where}.{key} must be a string")
    if not allow_empty and not value.strip():
        raise SchemaError(f"{where}.{key} must be a non-empty string")
    return value


def _optional_string(data: Mapping[str, Any], key: str, where: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{where}.{key} must be a string or null")
    return value


def _enum(data: Mapping[str, Any], key: str, options: Sequence[str], where: str) -> str:
    value = _string(data, key, where)
    if value not in options:
        raise SchemaError(f"{where}.{key} must be one of {tuple(options)}, got {value!r}")
    return value


def _bool(data: Mapping[str, Any], key: str, where: str, *, default: Any = _REQUIRED) -> bool:
    value = data.get(key, default)
    if value is _REQUIRED:
        raise SchemaError(f"{where}.{key} is required")
    if not isinstance(value, bool):
        raise SchemaError(f"{where}.{key} must be a boolean")
    return value


def _number(
    data: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{where}.{key} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise SchemaError(f"{where}.{key} must be >= {minimum}, got {number}")
    if maximum is not None and number > maximum:
        raise SchemaError(f"{where}.{key} must be <= {maximum}, got {number}")
    return number


def _integer(
    data: Mapping[str, Any],
    key: str,
    where: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{where}.{key} must be an integer")
    if minimum is not None and value < minimum:
        raise SchemaError(f"{where}.{key} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise SchemaError(f"{where}.{key} must be <= {maximum}, got {value}")
    return value


def _list(data: Mapping[str, Any], key: str, where: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise SchemaError(f"{where}.{key} must be an array")
    return list(value)


def _string_list(
    data: Mapping[str, Any],
    key: str,
    where: str,
    *,
    allow_empty_items: bool = False,
) -> list[str]:
    items = _list(data, key, where)
    out: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise SchemaError(f"{where}.{key}[{index}] must be a string")
        if not allow_empty_items and not item.strip():
            raise SchemaError(f"{where}.{key}[{index}] must be non-empty")
        out.append(item)
    return out


def _argv_list(data: Mapping[str, Any], key: str, where: str) -> list[list[str]]:
    items = _list(data, key, where)
    out: list[list[str]] = []
    for index, item in enumerate(items):
        if not isinstance(item, list) or not item:
            raise SchemaError(f"{where}.{key}[{index}] must be a non-empty argument array")
        argv: list[str] = []
        for position, token in enumerate(item):
            if not isinstance(token, str):
                raise SchemaError(f"{where}.{key}[{index}][{position}] must be a string")
            argv.append(token)
        out.append(argv)
    return out


def require_hex40(value: str, where: str) -> str:
    if not _HEX40_RE.match(value):
        raise SchemaError(f"{where} must be a 40-character lowercase hex object id, got {value!r}")
    return value


def require_sha256_hex(value: str, where: str) -> str:
    if not _SHA256_HEX_RE.match(value):
        raise SchemaError(f"{where} must be a 64-character lowercase hex sha256, got {value!r}")
    return value


def _strip_sha256_prefix(value: str) -> str:
    return value[len("sha256:") :] if value.startswith("sha256:") else value


def require_sha256_ref(value: str, where: str) -> str:
    if not _SHA256_REF_RE.match(value):
        raise SchemaError(f"{where} must look like 'sha256:<64 hex>', got {value!r}")
    return value


def require_task_id(value: str, where: str) -> str:
    if not _TASK_ID_RE.match(value):
        raise SchemaError(f"{where} must be a 20-character lowercase hex task id, got {value!r}")
    return value


def require_rejection_code(value: str, where: str) -> str:
    if value not in REJECTION_CODES:
        raise SchemaError(f"{where} must be a known rejection code, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# packaged JSON Schema assets
# ---------------------------------------------------------------------------

SCHEMA_ASSET_DIR = Path(__file__).resolve().parent / "schemas"
PACKAGED_SCHEMAS: dict[str, str] = {
    "task-definitions": "task-definitions.schema.json",
    "score-report": "score-report.schema.json",
    "scorer-audit": "scorer-audit.schema.json",
}


@cache
def load_packaged_schema(name: str) -> dict[str, Any]:
    """Load one of the JSON Schema documents shipped next to this module."""
    try:
        filename = PACKAGED_SCHEMAS[name]
    except KeyError:
        raise SchemaError(
            f"unknown packaged schema {name!r}, expected one of {sorted(PACKAGED_SCHEMAS)}"
        ) from None
    path = SCHEMA_ASSET_DIR / filename
    if not path.is_file():
        raise SchemaError(f"packaged schema {name!r} is missing at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SchemaError(f"packaged schema {name!r} must be a JSON object")
    return document


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _resolve_ref(ref: str, root: Mapping[str, Any]) -> Any:
    if not ref.startswith("#/"):
        raise SchemaError(f"unsupported JSON Schema $ref {ref!r}")
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or token not in node:
            raise SchemaError(f"unresolvable JSON Schema $ref {ref!r}")
        node = node[token]
    return node


_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$defs",
        "title",
        "description",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "allOf",
        "oneOf",
        "if",
        "then",
        "else",
    }
)


def json_schema_errors(
    value: Any,
    schema: Any,
    *,
    where: str = "$",
    root: Mapping[str, Any] | None = None,
) -> list[str]:
    """Validate ``value`` against the subset of JSON Schema used by the assets.

    Supported keywords: ``$ref``, ``type``, ``const``, ``enum``, ``required``,
    ``properties``, ``additionalProperties``, ``items``, ``minItems``,
    ``maxItems``, ``uniqueItems``, ``minLength``, ``maxLength``, ``minimum``,
    ``maximum``, ``allOf``, ``oneOf``, and ``if``/``then``/``else``, plus boolean
    schemas. Anything else in a packaged asset raises :class:`SchemaError` so an
    unsupported constraint can never pass silently.
    """
    if schema is True:
        return []
    if schema is False:
        return [f"{where} is not allowed"]
    if not isinstance(schema, Mapping):
        raise SchemaError(f"invalid JSON Schema node at {where}")
    document_root: Mapping[str, Any] = root if root is not None else schema

    if "$ref" in schema:
        return json_schema_errors(
            value,
            _resolve_ref(str(schema["$ref"]), document_root),
            where=where,
            root=document_root,
        )

    unsupported = set(schema) - _SUPPORTED_SCHEMA_KEYWORDS
    if unsupported:
        raise SchemaError(f"unsupported JSON Schema keywords at {where}: {sorted(unsupported)}")

    errors: list[str] = []
    expected_types = schema.get("type")
    if expected_types is not None:
        options = expected_types if isinstance(expected_types, list) else [expected_types]
        if not any(_json_type_matches(value, str(option)) for option in options):
            return [f"{where} must be of type {'|'.join(str(item) for item in options)}"]

    if "const" in schema and value != schema["const"]:
        errors.append(f"{where} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where} must be one of {schema['enum']!r}")

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{where} must have at least {minimum_length} characters")
        maximum_length = schema.get("maxLength")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            errors.append(f"{where} must have at most {maximum_length} characters")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{where} must be >= {minimum}")
        maximum = schema.get("maximum")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{where} must be <= {maximum}")

    if isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in sorted(value):
                if key not in properties:
                    errors.append(f"{where}.{key} is not an allowed property")
        for key in sorted(value):
            if key in properties:
                errors.extend(
                    json_schema_errors(
                        value[key], properties[key], where=f"{where}.{key}", root=document_root
                    )
                )

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{where} must contain at least {minimum_items} items")
        maximum_items = schema.get("maxItems")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{where} must contain at most {maximum_items} items")
        if schema.get("uniqueItems") is True:
            seen: list[Any] = []
            for item in value:
                if item in seen:
                    errors.append(f"{where} must contain unique items")
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                errors.extend(
                    json_schema_errors(
                        item, item_schema, where=f"{where}[{index}]", root=document_root
                    )
                )

    for sub_schema in schema.get("allOf", []):
        errors.extend(json_schema_errors(value, sub_schema, where=where, root=document_root))
    if "oneOf" in schema:
        matches = [
            branch
            for branch in schema["oneOf"]
            if not json_schema_errors(value, branch, where=where, root=document_root)
        ]
        if len(matches) != 1:
            errors.append(f"{where} must match exactly one allowed shape")
    if "if" in schema:
        matched = not json_schema_errors(value, schema["if"], where=where, root=document_root)
        branch = schema.get("then") if matched else schema.get("else")
        if branch is not None:
            errors.extend(json_schema_errors(value, branch, where=where, root=document_root))
    return errors


def packaged_schema_errors(document: Any, name: str, *, where: str = "$") -> list[str]:
    """Return every way ``document`` violates the packaged ``name`` schema."""
    return json_schema_errors(document, load_packaged_schema(name), where=where)


def validate_packaged(document: Any, name: str, *, where: str = "$") -> None:
    errors = packaged_schema_errors(document, name, where=where)
    if errors:
        raise SchemaError(f"{name} contract violation: " + "; ".join(errors))


# ---------------------------------------------------------------------------
# SourceBundle manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskLimits:
    max_replay_tasks: int = MAX_REPLAY_TASKS
    adjacent_per_replay: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_replay_tasks": self.max_replay_tasks,
            "adjacent_per_replay": self.adjacent_per_replay,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "task_limits") -> TaskLimits:
        payload = _mapping(data, where)
        _known_keys(payload, ("max_replay_tasks", "adjacent_per_replay"), where)
        return cls(
            max_replay_tasks=_integer(
                payload, "max_replay_tasks", where, minimum=0, maximum=MAX_REPLAY_TASKS
            ),
            adjacent_per_replay=_integer(
                payload,
                "adjacent_per_replay",
                where,
                minimum=0,
                maximum=MAX_ADJACENT_PER_REPLAY,
            ),
        )


@dataclass(frozen=True)
class RepoAnchor:
    root_at_capture: str
    repo_id: str
    base_sha: str
    base_tree: str
    outcome_sha: str
    outcome_tree: str
    base_resolution: str
    state_confidence: str
    subdir: str = "."
    environment_id: str | None = None
    outcome_resolution: str = "rollout_commit"

    def __post_init__(self) -> None:
        where = "repo"
        require_hex40(self.base_sha, f"{where}.base_sha")
        require_hex40(self.base_tree, f"{where}.base_tree")
        require_hex40(self.outcome_sha, f"{where}.outcome_sha")
        require_hex40(self.outcome_tree, f"{where}.outcome_tree")
        if self.base_resolution not in USABLE_BASE_RESOLUTIONS:
            raise SchemaError(
                f"{where}.base_resolution must be one of {USABLE_BASE_RESOLUTIONS}, "
                f"got {self.base_resolution!r}"
            )
        if self.outcome_resolution not in OUTCOME_RESOLUTIONS[:3]:
            raise SchemaError(
                f"{where}.outcome_resolution must be one of {OUTCOME_RESOLUTIONS[:3]}, "
                f"got {self.outcome_resolution!r}"
            )
        if self.state_confidence not in STATE_CONFIDENCES:
            raise SchemaError(
                f"{where}.state_confidence must be one of {STATE_CONFIDENCES}, "
                f"got {self.state_confidence!r}"
            )
        if self.base_resolution == "first_commit_parent" and self.state_confidence != "approximate":
            raise SchemaError("first_commit_parent bases must declare state_confidence=approximate")
        if self.base_tree == self.outcome_tree:
            raise SchemaError("repo.base_tree and repo.outcome_tree must differ")
        if self.environment_id is not None:
            require_sha256_ref(self.environment_id, f"{where}.environment_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_at_capture": self.root_at_capture,
            "repo_id": self.repo_id,
            "base_sha": self.base_sha,
            "base_tree": self.base_tree,
            "outcome_sha": self.outcome_sha,
            "outcome_tree": self.outcome_tree,
            "base_resolution": self.base_resolution,
            "outcome_resolution": self.outcome_resolution,
            "state_confidence": self.state_confidence,
            "subdir": self.subdir,
            "environment_id": self.environment_id,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "repo") -> RepoAnchor:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            (
                "root_at_capture",
                "repo_id",
                "base_sha",
                "base_tree",
                "outcome_sha",
                "outcome_tree",
                "base_resolution",
                "outcome_resolution",
                "state_confidence",
                "subdir",
                "environment_id",
            ),
            where,
        )
        return cls(
            root_at_capture=_string(payload, "root_at_capture", where),
            repo_id=_string(payload, "repo_id", where),
            base_sha=_string(payload, "base_sha", where),
            base_tree=_string(payload, "base_tree", where),
            outcome_sha=_string(payload, "outcome_sha", where),
            outcome_tree=_string(payload, "outcome_tree", where),
            base_resolution=_string(payload, "base_resolution", where),
            outcome_resolution=_string(payload, "outcome_resolution", where)
            if "outcome_resolution" in payload
            else "rollout_commit",
            state_confidence=_string(payload, "state_confidence", where),
            subdir=_string(payload, "subdir", where) if "subdir" in payload else ".",
            environment_id=_optional_string(payload, "environment_id", where),
        )


@dataclass(frozen=True)
class SourceBundleManifest:
    source_id: str
    host: str
    session_id: str
    started_at: str | None
    ended_at: str | None
    rollout_events_sha256: str
    repo: RepoAnchor
    task_limits: TaskLimits = field(default_factory=TaskLimits)
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        require_sha256_hex(self.rollout_events_sha256, "rollout_events_sha256")
        if self.content_sha256 is not None:
            require_sha256_hex(self.content_sha256, "content_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SOURCE_BUNDLE_SCHEMA,
            "source_id": self.source_id,
            "host": self.host,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "rollout_events_sha256": self.rollout_events_sha256,
            "repo": self.repo.to_dict(),
            "task_limits": self.task_limits.to_dict(),
            "content_sha256": self.content_sha256,
        }

    def with_content_hash(self, digest: str) -> SourceBundleManifest:
        require_sha256_hex(digest, "content_sha256")
        return SourceBundleManifest(
            source_id=self.source_id,
            host=self.host,
            session_id=self.session_id,
            started_at=self.started_at,
            ended_at=self.ended_at,
            rollout_events_sha256=self.rollout_events_sha256,
            repo=self.repo,
            task_limits=self.task_limits,
            content_sha256=digest,
        )

    @classmethod
    def from_dict(cls, data: Any, where: str = "manifest") -> SourceBundleManifest:
        payload = _mapping(data, where)
        _schema_version(payload, SOURCE_BUNDLE_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "source_id",
                "host",
                "session_id",
                "started_at",
                "ended_at",
                "rollout_events_sha256",
                "repo",
                "task_limits",
                "content_sha256",
            ),
            where,
        )
        return cls(
            source_id=_string(payload, "source_id", where),
            host=_string(payload, "host", where),
            session_id=_string(payload, "session_id", where),
            started_at=_optional_string(payload, "started_at", where),
            ended_at=_optional_string(payload, "ended_at", where),
            rollout_events_sha256=_string(payload, "rollout_events_sha256", where),
            repo=RepoAnchor.from_dict(payload.get("repo"), f"{where}.repo"),
            task_limits=TaskLimits.from_dict(
                payload.get("task_limits", {}), f"{where}.task_limits"
            ),
            content_sha256=_optional_string(payload, "content_sha256", where),
        )


# ---------------------------------------------------------------------------
# project environment
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectEnvironment:
    environment_id: str
    source: str
    base_sha: str
    image: str
    workdir: str
    setup: list[list[str]] = field(default_factory=list)
    smoke: list[list[str]] = field(default_factory=list)
    test: list[list[str]] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    network_during_build: str = "allowlisted"
    network_during_run: str = "disabled"
    workspace_excludes: list[str] = field(default_factory=list)
    validated: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_sha256_ref(self.environment_id, "environment.environment_id")
        require_hex40(self.base_sha, "environment.base_sha")
        if self.source not in ENVIRONMENT_SOURCES:
            raise SchemaError(
                f"environment.source must be one of {ENVIRONMENT_SOURCES}, got {self.source!r}"
            )
        if self.network_during_run != "disabled":
            raise SchemaError("environment.network_during_run must be 'disabled'")
        if not self.validated.get("base") or not self.validated.get("outcome"):
            raise SchemaError("environment must be validated against base and outcome")
        runs = self.validated.get("runs")
        if isinstance(runs, bool) or not isinstance(runs, int) or runs < 2:
            raise SchemaError("environment.validated.runs must be at least 2")
        if not _PINNED_IMAGE_RE.fullmatch(self.image):
            raise SchemaError("environment.image must be pinned by a sha256 digest")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_ENVIRONMENT_SCHEMA,
            "environment_id": self.environment_id,
            "source": self.source,
            "base_sha": self.base_sha,
            "image": self.image,
            "workdir": self.workdir,
            "setup": [list(argv) for argv in self.setup],
            "smoke": [list(argv) for argv in self.smoke],
            "test": [list(argv) for argv in self.test],
            "env": dict(self.env),
            "network_during_build": self.network_during_build,
            "network_during_run": self.network_during_run,
            "workspace_excludes": list(self.workspace_excludes),
            "validated": dict(self.validated),
        }

    def test_commands(self) -> dict[str, Any]:
        """Exact copy of validated command arrays for ``context/test-commands.json``."""
        return {
            "schema_version": PROJECT_ENVIRONMENT_SCHEMA,
            "environment_id": self.environment_id,
            "workdir": self.workdir,
            "setup": [list(argv) for argv in self.setup],
            "smoke": [list(argv) for argv in self.smoke],
            "test": [list(argv) for argv in self.test],
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "environment") -> ProjectEnvironment:
        payload = _mapping(data, where)
        _schema_version(payload, PROJECT_ENVIRONMENT_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "environment_id",
                "source",
                "base_sha",
                "image",
                "workdir",
                "setup",
                "smoke",
                "test",
                "env",
                "network_during_build",
                "network_during_run",
                "workspace_excludes",
                "validated",
            ),
            where,
        )
        env_map = _mapping(payload.get("env", {}), f"{where}.env")
        for key, value in env_map.items():
            if not isinstance(value, str):
                raise SchemaError(f"{where}.env.{key} must be a string")
        validated = _mapping(payload.get("validated", {}), f"{where}.validated")
        return cls(
            environment_id=_string(payload, "environment_id", where),
            source=_string(payload, "source", where),
            base_sha=_string(payload, "base_sha", where),
            image=_string(payload, "image", where),
            workdir=_string(payload, "workdir", where),
            setup=_argv_list(payload, "setup", where) if "setup" in payload else [],
            smoke=_argv_list(payload, "smoke", where) if "smoke" in payload else [],
            test=_argv_list(payload, "test", where) if "test" in payload else [],
            env={key: str(value) for key, value in env_map.items()},
            network_during_build=_string(payload, "network_during_build", where)
            if "network_during_build" in payload
            else "allowlisted",
            network_during_run=_string(payload, "network_during_run", where)
            if "network_during_run" in payload
            else "disabled",
            workspace_excludes=_string_list(payload, "workspace_excludes", where)
            if "workspace_excludes" in payload
            else [],
            validated=validated,
        )


# ---------------------------------------------------------------------------
# TaskDefinition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptProvenance:
    user_event_ids: list[str]
    mode: str = "resolved_user_messages"

    def to_dict(self) -> dict[str, Any]:
        return {"user_event_ids": list(self.user_event_ids), "mode": self.mode}

    @classmethod
    def from_dict(cls, data: Any, where: str) -> PromptProvenance:
        payload = _mapping(data, where)
        _known_keys(payload, ("user_event_ids", "mode"), where)
        return cls(
            user_event_ids=_string_list(payload, "user_event_ids", where),
            mode=_string(payload, "mode", where) if "mode" in payload else "resolved_user_messages",
        )


@dataclass(frozen=True)
class GoalSegment:
    introduced_event_id: str
    closed_event_id: str | None
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "introduced_event_id": self.introduced_event_id,
            "closed_event_id": self.closed_event_id,
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> GoalSegment:
        payload = _mapping(data, where)
        _known_keys(payload, ("introduced_event_id", "closed_event_id", "summary"), where)
        return cls(
            introduced_event_id=_string(payload, "introduced_event_id", where),
            closed_event_id=_optional_string(payload, "closed_event_id", where),
            summary=_string(payload, "summary", where, allow_empty=True),
        )


@dataclass(frozen=True)
class RepoEvidence:
    state: str
    path: str
    reason: str

    def __post_init__(self) -> None:
        if self.state not in ("base", "outcome"):
            raise SchemaError(f"repo_evidence.state must be 'base' or 'outcome', got {self.state!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "path": self.path, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: Any, where: str) -> RepoEvidence:
        payload = _mapping(data, where)
        _known_keys(payload, ("state", "path", "reason"), where)
        return cls(
            state=_string(payload, "state", where),
            path=_string(payload, "path", where),
            reason=_string(payload, "reason", where, allow_empty=True),
        )


@dataclass(frozen=True)
class Observable:
    id: str
    description: str
    importance: str
    evidence: list[str]

    def __post_init__(self) -> None:
        if self.importance not in OBSERVABLE_IMPORTANCES:
            raise SchemaError(
                f"observable.importance must be one of {OBSERVABLE_IMPORTANCES}, "
                f"got {self.importance!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "importance": self.importance,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> Observable:
        payload = _mapping(data, where)
        _known_keys(payload, ("id", "description", "importance", "evidence"), where)
        return cls(
            id=_string(payload, "id", where),
            description=_string(payload, "description", where),
            importance=_string(payload, "importance", where),
            evidence=_string_list(payload, "evidence", where),
        )


@dataclass(frozen=True)
class ScorerBrief:
    observables: list[Observable]
    regressions_to_protect: list[str] = field(default_factory=list)
    performance: list[Any] = field(default_factory=list)
    residual_judgment: list[Any] = field(default_factory=list)
    forbidden_shortcuts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observables": [item.to_dict() for item in self.observables],
            "regressions_to_protect": list(self.regressions_to_protect),
            "performance": list(self.performance),
            "residual_judgment": list(self.residual_judgment),
            "forbidden_shortcuts": list(self.forbidden_shortcuts),
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ScorerBrief:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            (
                "observables",
                "regressions_to_protect",
                "performance",
                "residual_judgment",
                "forbidden_shortcuts",
            ),
            where,
        )
        observables = [
            Observable.from_dict(item, f"{where}.observables[{index}]")
            for index, item in enumerate(_list(payload, "observables", where))
        ]
        return cls(
            observables=observables,
            regressions_to_protect=_string_list(payload, "regressions_to_protect", where)
            if "regressions_to_protect" in payload
            else [],
            performance=list(_list(payload, "performance", where))
            if "performance" in payload else [],
            residual_judgment=list(_list(payload, "residual_judgment", where))
            if "residual_judgment" in payload else [],
            forbidden_shortcuts=_string_list(payload, "forbidden_shortcuts", where)
            if "forbidden_shortcuts" in payload
            else [],
        )


@dataclass(frozen=True)
class Adjacency:
    operator: str
    parent_candidate_id: str
    transformed_object: str
    base_failure_reason: str

    def __post_init__(self) -> None:
        if self.operator not in ADJACENCY_OPERATORS:
            raise SchemaError(
                f"adjacency.operator must be one of {ADJACENCY_OPERATORS}, got {self.operator!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_candidate_id": self.parent_candidate_id,
            "operator": self.operator,
            "transformed_object": self.transformed_object,
            "base_failure_reason": self.base_failure_reason,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> Adjacency:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            ("operator", "parent_candidate_id", "transformed_object", "base_failure_reason"),
            where,
        )
        return cls(
            operator=_string(payload, "operator", where),
            parent_candidate_id=_string(payload, "parent_candidate_id", where),
            transformed_object=_string(payload, "transformed_object", where),
            base_failure_reason=_string(payload, "base_failure_reason", where),
        )


@dataclass(frozen=True)
class TaskConfidence:
    goal: float
    state: float
    scorability: float

    def to_dict(self) -> dict[str, Any]:
        return {"goal": self.goal, "state": self.state, "scorability": self.scorability}

    @classmethod
    def from_dict(cls, data: Any, where: str) -> TaskConfidence:
        payload = _mapping(data, where)
        _known_keys(payload, ("goal", "state", "scorability"), where)
        return cls(
            goal=_number(payload, "goal", where, minimum=0.0, maximum=1.0),
            state=_number(payload, "state", where, minimum=0.0, maximum=1.0),
            scorability=_number(payload, "scorability", where, minimum=0.0, maximum=1.0),
        )


@dataclass(frozen=True)
class TaskCandidate:
    candidate_id: str
    kind: str
    prompt: str
    prompt_provenance: PromptProvenance
    goal_segment: GoalSegment
    repo_evidence: list[RepoEvidence]
    scorer_brief: ScorerBrief
    base_failure_claim: str
    outcome_success_claim: str
    confidence: TaskConfidence
    adjacency: Adjacency | None = None

    def __post_init__(self) -> None:
        if self.kind not in TASK_KINDS:
            raise SchemaError(f"task.kind must be one of {TASK_KINDS}, got {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "prompt_provenance": self.prompt_provenance.to_dict(),
            "goal_segment": self.goal_segment.to_dict(),
            "repo_evidence": [item.to_dict() for item in self.repo_evidence],
            "scorer_brief": self.scorer_brief.to_dict(),
            "base_failure_claim": self.base_failure_claim,
            "outcome_success_claim": self.outcome_success_claim,
            "adjacency": self.adjacency.to_dict() if self.adjacency else None,
            "confidence": self.confidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> TaskCandidate:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            (
                "candidate_id",
                "kind",
                "prompt",
                "prompt_provenance",
                "goal_segment",
                "repo_evidence",
                "scorer_brief",
                "base_failure_claim",
                "outcome_success_claim",
                "adjacency",
                "confidence",
            ),
            where,
        )
        adjacency_payload = payload.get("adjacency")
        evidence = [
            RepoEvidence.from_dict(item, f"{where}.repo_evidence[{index}]")
            for index, item in enumerate(_list(payload, "repo_evidence", where))
        ]
        return cls(
            candidate_id=_string(payload, "candidate_id", where),
            kind=_string(payload, "kind", where),
            prompt=_string(payload, "prompt", where, allow_empty=True),
            prompt_provenance=PromptProvenance.from_dict(
                payload.get("prompt_provenance", {}), f"{where}.prompt_provenance"
            ),
            goal_segment=GoalSegment.from_dict(
                payload.get("goal_segment", {}), f"{where}.goal_segment"
            ),
            repo_evidence=evidence,
            scorer_brief=ScorerBrief.from_dict(
                payload.get("scorer_brief", {}), f"{where}.scorer_brief"
            ),
            base_failure_claim=_string(payload, "base_failure_claim", where, allow_empty=True),
            outcome_success_claim=_string(payload, "outcome_success_claim", where, allow_empty=True),
            adjacency=(
                Adjacency.from_dict(adjacency_payload, f"{where}.adjacency")
                if adjacency_payload is not None
                else None
            ),
            confidence=TaskConfidence.from_dict(
                payload.get("confidence", {}), f"{where}.confidence"
            ),
        )


@dataclass(frozen=True)
class TaskRejection:
    code: str
    detail: str
    goal_event_ids: list[str] = field(default_factory=list)
    candidate_id: str | None = None

    def __post_init__(self) -> None:
        require_rejection_code(self.code, "rejection.code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_event_ids": list(self.goal_event_ids),
            "code": self.code,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> TaskRejection:
        payload = _mapping(data, where)
        _known_keys(payload, ("goal_event_ids", "candidate_id", "code", "detail"), where)
        return cls(
            code=_string(payload, "code", where),
            detail=_string(payload, "detail", where, allow_empty=True),
            goal_event_ids=_string_list(payload, "goal_event_ids", where)
            if "goal_event_ids" in payload
            else [],
            candidate_id=_optional_string(payload, "candidate_id", where),
        )


@dataclass(frozen=True)
class TaskDefinitions:
    source_id: str
    tasks: list[TaskCandidate] = field(default_factory=list)
    rejections: list[TaskRejection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TASK_DEFINITIONS_SCHEMA,
            "source_id": self.source_id,
            "tasks": [task.to_dict() for task in self.tasks],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "task_definitions") -> TaskDefinitions:
        payload = _mapping(data, where)
        _schema_version(payload, TASK_DEFINITIONS_SCHEMA, where)
        _known_keys(payload, ("schema_version", "source_id", "tasks", "rejections"), where)
        tasks = [
            TaskCandidate.from_dict(item, f"{where}.tasks[{index}]")
            for index, item in enumerate(_list(payload, "tasks", where))
        ]
        if len(tasks) > MAX_TASKS_PER_SOURCE:
            raise SchemaError(
                f"{where} declares {len(tasks)} tasks, at most {MAX_TASKS_PER_SOURCE} are allowed"
            )
        rejections = [
            TaskRejection.from_dict(item, f"{where}.rejections[{index}]")
            for index, item in enumerate(
                _list(payload, "rejections", where) if "rejections" in payload else []
            )
        ]
        seen: set[str] = set()
        for task in tasks:
            if task.candidate_id in seen:
                raise SchemaError(f"{where} has duplicate candidate_id {task.candidate_id!r}")
            seen.add(task.candidate_id)
        return cls(
            source_id=_string(payload, "source_id", where),
            tasks=tasks,
            rejections=rejections,
        )


# ---------------------------------------------------------------------------
# ScorerPackage manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScorerComponent:
    id: str
    kind: str
    weight: float
    hard_gate: bool
    range: tuple[float, float] = (0.0, 1.0)

    def __post_init__(self) -> None:
        if self.kind not in COMPONENT_KINDS:
            raise SchemaError(
                f"component.kind must be one of {COMPONENT_KINDS}, got {self.kind!r}"
            )
        low, high = self.range
        if low != 0.0 or high != 1.0:
            raise SchemaError("component.range must be [0.0, 1.0]")
        if not 0.0 <= self.weight <= 1.0:
            raise SchemaError(f"component.weight must be within [0, 1], got {self.weight}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "weight": self.weight,
            "hard_gate": self.hard_gate,
            "range": [self.range[0], self.range[1]],
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ScorerComponent:
        payload = _mapping(data, where)
        _known_keys(payload, ("id", "kind", "weight", "hard_gate", "range"), where)
        range_values = _list(payload, "range", where) if "range" in payload else [0.0, 1.0]
        if len(range_values) != 2:
            raise SchemaError(f"{where}.range must have exactly two numbers")
        for value in range_values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SchemaError(f"{where}.range entries must be numbers")
        return cls(
            id=_string(payload, "id", where),
            kind=_string(payload, "kind", where),
            weight=_number(payload, "weight", where, minimum=0.0, maximum=1.0),
            hard_gate=_bool(payload, "hard_gate", where),
            range=(float(range_values[0]), float(range_values[1])),
        )


@dataclass(frozen=True)
class ScorerRuntime:
    image: str
    network: str = "disabled"
    timeout_seconds: int = 900
    cpu: int = 2
    memory_mb: int = 4096
    candidate_mount: str = "read_only"

    def __post_init__(self) -> None:
        if self.network != "disabled":
            raise SchemaError("scorer runtime.network must be 'disabled'")
        if self.candidate_mount != "read_only":
            raise SchemaError("scorer runtime.candidate_mount must be 'read_only'")
        if self.timeout_seconds <= 0:
            raise SchemaError("scorer runtime.timeout_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "image": self.image,
            "network": self.network,
            "timeout_seconds": self.timeout_seconds,
            "cpu": self.cpu,
            "memory_mb": self.memory_mb,
            "candidate_mount": self.candidate_mount,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ScorerRuntime:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            ("image", "network", "timeout_seconds", "cpu", "memory_mb", "candidate_mount"),
            where,
        )
        return cls(
            image=_string(payload, "image", where),
            network=_string(payload, "network", where) if "network" in payload else "disabled",
            timeout_seconds=_integer(payload, "timeout_seconds", where, minimum=1)
            if "timeout_seconds" in payload
            else 900,
            cpu=_integer(payload, "cpu", where, minimum=1) if "cpu" in payload else 2,
            memory_mb=_integer(payload, "memory_mb", where, minimum=1)
            if "memory_mb" in payload
            else 4096,
            candidate_mount=_string(payload, "candidate_mount", where)
            if "candidate_mount" in payload
            else "read_only",
        )


@dataclass(frozen=True)
class JudgeConfig:
    enabled: bool
    agent_config: str | None
    prompt: str | None
    output_schema: str | None
    criteria: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "agent_config": self.agent_config,
            "prompt": self.prompt,
            "output_schema": self.output_schema,
            "criteria": list(self.criteria),
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> JudgeConfig:
        payload = _mapping(data, where)
        _known_keys(
            payload, ("enabled", "agent_config", "prompt", "output_schema", "criteria"), where
        )
        enabled = _bool(payload, "enabled", where)
        judge = cls(
            enabled=enabled,
            agent_config=_optional_string(payload, "agent_config", where),
            prompt=_optional_string(payload, "prompt", where),
            output_schema=_optional_string(payload, "output_schema", where),
            criteria=_string_list(payload, "criteria", where) if "criteria" in payload else [],
        )
        if enabled and not (judge.agent_config and judge.prompt and judge.output_schema):
            raise SchemaError(
                f"{where} requires a pinned agent_config, prompt, and output_schema when enabled"
            )
        return judge


@dataclass(frozen=True)
class ScorerManifest:
    task_id: str
    mode: str
    entrypoint: list[str]
    runtime: ScorerRuntime
    components: list[ScorerComponent]
    pass_threshold: float = 0.8
    judge: JudgeConfig | None = None
    required_artifacts: list[str] = field(default_factory=lambda: ["repo", "task"])
    package_sha256: str | None = None

    def __post_init__(self) -> None:
        require_task_id(self.task_id, "scorer.task_id")
        if self.mode not in SCORER_MODES:
            raise SchemaError(f"scorer.mode must be one of {SCORER_MODES}, got {self.mode!r}")
        if not self.entrypoint:
            raise SchemaError("scorer.entrypoint must be a non-empty argument array")
        if not self.components:
            raise SchemaError("scorer.components must not be empty")
        ids = [component.id for component in self.components]
        if len(ids) != len(set(ids)):
            raise SchemaError("scorer.components must have unique ids")
        total = sum(component.weight for component in self.components)
        if abs(total - 1.0) > WEIGHT_TOLERANCE:
            raise SchemaError(f"scorer component weights must sum to 1.0, got {total}")
        if not 0.0 <= self.pass_threshold <= 1.0:
            raise SchemaError("scorer.pass_threshold must be within [0, 1]")
        judge_needed = self.mode in ("judge", "hybrid", "agentic")
        judge_enabled = bool(self.judge and self.judge.enabled)
        if judge_needed and not judge_enabled:
            raise SchemaError(f"scorer mode {self.mode!r} requires a pinned judge configuration")
        if judge_enabled and self.judge is not None:
            declared = {component.id for component in self.components}
            missing = sorted(set(self.judge.criteria) - declared)
            if missing:
                raise SchemaError(
                    f"judge criteria must map to declared components; missing {missing}"
                )
        if self.package_sha256 is not None:
            require_sha256_hex(self.package_sha256, "scorer.package_sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCORER_SCHEMA,
            "task_id": self.task_id,
            "mode": self.mode,
            "entrypoint": list(self.entrypoint),
            "runtime": self.runtime.to_dict(),
            "components": [component.to_dict() for component in self.components],
            "pass_threshold": self.pass_threshold,
            "judge": self.judge.to_dict() if self.judge else None,
            "required_artifacts": list(self.required_artifacts),
            "package_sha256": self.package_sha256,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "scorer") -> ScorerManifest:
        payload = _mapping(data, where)
        _schema_version(payload, SCORER_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "task_id",
                "mode",
                "entrypoint",
                "runtime",
                "components",
                "pass_threshold",
                "judge",
                "required_artifacts",
                "package_sha256",
            ),
            where,
        )
        judge_payload = payload.get("judge")
        components = [
            ScorerComponent.from_dict(item, f"{where}.components[{index}]")
            for index, item in enumerate(_list(payload, "components", where))
        ]
        return cls(
            task_id=_string(payload, "task_id", where),
            mode=_string(payload, "mode", where),
            entrypoint=_string_list(payload, "entrypoint", where),
            runtime=ScorerRuntime.from_dict(payload.get("runtime", {}), f"{where}.runtime"),
            components=components,
            pass_threshold=_number(payload, "pass_threshold", where, minimum=0.0, maximum=1.0)
            if "pass_threshold" in payload
            else 0.8,
            judge=(
                JudgeConfig.from_dict(judge_payload, f"{where}.judge")
                if judge_payload is not None
                else None
            ),
            required_artifacts=_string_list(payload, "required_artifacts", where)
            if "required_artifacts" in payload
            else ["repo", "task"],
            package_sha256=_optional_string(payload, "package_sha256", where),
        )


# ---------------------------------------------------------------------------
# scorer input / report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreInput:
    task_id: str
    attempt_id: str
    repo_path: str
    task_path: str
    trace_path: str | None = None
    resource_usage_path: str | None = None
    seed: int = 0

    def __post_init__(self) -> None:
        require_task_id(self.task_id, "score_input.task_id")
        if self.seed < 0:
            raise SchemaError("score_input.seed must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCORE_INPUT_SCHEMA,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "repo_path": self.repo_path,
            "task_path": self.task_path,
            "trace_path": self.trace_path,
            "resource_usage_path": self.resource_usage_path,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "score_input") -> ScoreInput:
        payload = _mapping(data, where)
        _schema_version(payload, SCORE_INPUT_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "task_id",
                "attempt_id",
                "repo_path",
                "task_path",
                "trace_path",
                "resource_usage_path",
                "seed",
            ),
            where,
        )
        return cls(
            task_id=_string(payload, "task_id", where),
            attempt_id=_string(payload, "attempt_id", where),
            repo_path=_string(payload, "repo_path", where),
            task_path=_string(payload, "task_path", where),
            trace_path=_optional_string(payload, "trace_path", where),
            resource_usage_path=_optional_string(payload, "resource_usage_path", where),
            seed=_integer(payload, "seed", where, minimum=0) if "seed" in payload else 0,
        )


@dataclass(frozen=True)
class ScoreEvidence:
    """One evidence object attached to a component result.

    The packaged ``score-report`` contract types evidence as a free-form object,
    so unrecognized keys are preserved verbatim instead of rejected.
    """

    kind: str
    ref: str
    summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in (("kind", self.kind), ("ref", self.ref), ("summary", self.summary)):
            if value:
                payload[key] = value
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ScoreEvidence:
        payload = _mapping(data, where)
        extra = {key: value for key, value in payload.items() if key not in ("kind", "ref", "summary")}
        return cls(
            kind=_string(payload, "kind", where, allow_empty=True) if "kind" in payload else "",
            ref=_string(payload, "ref", where, allow_empty=True) if "ref" in payload else "",
            summary=_string(payload, "summary", where, allow_empty=True)
            if "summary" in payload
            else "",
            extra=extra,
        )


@dataclass(frozen=True)
class ScoreComponentResult:
    id: str
    weight: float
    hard_gate: bool
    value: float | None = None
    gate_passed: bool | None = None
    evidence: list[ScoreEvidence] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.value is not None and not 0.0 <= self.value <= 1.0:
            raise SchemaError(f"component {self.id!r} value must be within [0, 1]")

    @property
    def scored(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "value": self.value,
            "weight": self.weight,
            "hard_gate": self.hard_gate,
            "gate_passed": self.gate_passed,
            "evidence": [item.to_dict() for item in self.evidence],
        }

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ScoreComponentResult:
        payload = _mapping(data, where)
        _known_keys(
            payload,
            ("id", "value", "weight", "hard_gate", "gate_passed", "evidence"),
            where,
        )
        raw_value = payload.get("value")
        value = (
            None
            if raw_value is None
            else _number(payload, "value", where, minimum=0.0, maximum=1.0)
        )
        gate_passed = payload.get("gate_passed")
        if gate_passed is not None and not isinstance(gate_passed, bool):
            raise SchemaError(f"{where}.gate_passed must be a boolean or null")
        evidence = [
            ScoreEvidence.from_dict(item, f"{where}.evidence[{index}]")
            for index, item in enumerate(
                _list(payload, "evidence", where) if "evidence" in payload else []
            )
        ]
        return cls(
            id=_string(payload, "id", where),
            weight=_number(payload, "weight", where, minimum=0.0, maximum=1.0),
            hard_gate=_bool(payload, "hard_gate", where),
            value=value,
            gate_passed=gate_passed,
            evidence=evidence,
        )


@dataclass(frozen=True)
class CommandRecord:
    argv: list[str]
    exit_code: int
    duration_ms: int
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
        }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, data: Any, where: str) -> CommandRecord:
        payload = _mapping(data, where)
        known = ("argv", "exit_code", "duration_ms")
        return cls(
            argv=_string_list(payload, "argv", where, allow_empty_items=True),
            exit_code=_integer(payload, "exit_code", where),
            duration_ms=_integer(payload, "duration_ms", where, minimum=0),
            extra={key: value for key, value in payload.items() if key not in known},
        )


@dataclass(frozen=True)
class ScoreReport:
    task_id: str
    attempt_id: str
    status: str
    scorer_package_sha256: str
    score_total: float | None = None
    passed: bool | None = None
    components: list[ScoreComponentResult] = field(default_factory=list)
    hard_gate_failures: list[str] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)
    judge: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def __post_init__(self) -> None:
        require_task_id(self.task_id, "score_report.task_id")
        if self.status not in SCORE_STATUSES:
            raise SchemaError(
                f"score_report.status must be one of {SCORE_STATUSES}, got {self.status!r}"
            )
        if self.status == "scored":
            if self.score_total is None:
                raise SchemaError("scored reports must carry score_total")
            if not 0.0 <= self.score_total <= 1.0:
                raise SchemaError("score_total must be within [0, 1]")
            if self.passed is None:
                raise SchemaError("scored reports must carry passed")
            if not self.components:
                raise SchemaError("scored reports must carry component results")
        else:
            if self.score_total is not None:
                raise SchemaError(f"status {self.status!r} must not carry a score_total")
            if self.passed is not None:
                raise SchemaError(f"status {self.status!r} must not carry passed")
        object.__setattr__(
            self,
            "scorer_package_sha256",
            require_sha256_hex(
                _strip_sha256_prefix(self.scorer_package_sha256),
                "score_report.scorer_package_sha256",
            ),
        )

    @property
    def is_numeric(self) -> bool:
        return self.status == "scored" and self.score_total is not None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SCORE_REPORT_SCHEMA,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
        }
        if self.status == "scored":
            payload["score_total"] = self.score_total
            payload["passed"] = self.passed
        payload.update(
            {
                "components": [component.to_dict() for component in self.components],
                "hard_gate_failures": list(self.hard_gate_failures),
                "commands": [command.to_dict() for command in self.commands],
                "judge": self.judge,
                "warnings": list(self.warnings),
                "scorer_package_sha256": self.scorer_package_sha256,
                "duration_ms": self.duration_ms,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: Any, where: str = "score_report") -> ScoreReport:
        payload = _mapping(data, where)
        _schema_version(payload, SCORE_REPORT_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "task_id",
                "attempt_id",
                "status",
                "score_total",
                "passed",
                "components",
                "hard_gate_failures",
                "commands",
                "judge",
                "warnings",
                "scorer_package_sha256",
                "duration_ms",
            ),
            where,
        )
        raw_total = payload.get("score_total")
        total = (
            None
            if raw_total is None
            else _number(payload, "score_total", where, minimum=0.0, maximum=1.0)
        )
        passed = payload.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise SchemaError(f"{where}.passed must be a boolean or null")
        judge_payload = payload.get("judge")
        if judge_payload is not None and not isinstance(judge_payload, Mapping):
            raise SchemaError(f"{where}.judge must be an object or null")
        components = [
            ScoreComponentResult.from_dict(item, f"{where}.components[{index}]")
            for index, item in enumerate(
                _list(payload, "components", where) if "components" in payload else []
            )
        ]
        commands = [
            CommandRecord.from_dict(item, f"{where}.commands[{index}]")
            for index, item in enumerate(
                _list(payload, "commands", where) if "commands" in payload else []
            )
        ]
        return cls(
            task_id=_string(payload, "task_id", where),
            attempt_id=_string(payload, "attempt_id", where),
            status=_string(payload, "status", where),
            score_total=total,
            passed=passed,
            components=components,
            hard_gate_failures=_string_list(payload, "hard_gate_failures", where)
            if "hard_gate_failures" in payload
            else [],
            commands=commands,
            judge=dict(judge_payload) if judge_payload is not None else None,
            warnings=_string_list(payload, "warnings", where, allow_empty_items=True)
            if "warnings" in payload
            else [],
            scorer_package_sha256=_string(payload, "scorer_package_sha256", where),
            duration_ms=_integer(payload, "duration_ms", where, minimum=0)
            if "duration_ms" in payload
            else 0,
        )


@dataclass(frozen=True)
class ScoreTotal:
    score_total: float
    passed: bool
    hard_gate_failures: list[str]
    unscored_weight: float
    valid: bool
    invalid_reason: str | None = None


def compute_score_total(
    components: Sequence[ScoreComponentResult],
    *,
    pass_threshold: float,
) -> ScoreTotal:
    """Apply the §10.5 total-score rules: gates dominate, unscored weight is reported."""
    if not components:
        raise SchemaError("cannot compute a score total without components")
    weight_sum = sum(component.weight for component in components)
    if abs(weight_sum - 1.0) > WEIGHT_TOLERANCE:
        raise SchemaError(f"component weights must sum to 1.0, got {weight_sum}")

    gate_failures = [
        component.id
        for component in components
        if component.hard_gate and component.gate_passed is not True
    ]
    unscored_weight = sum(
        component.weight for component in components if not component.scored
    )
    if gate_failures:
        return ScoreTotal(
            score_total=0.0,
            passed=False,
            hard_gate_failures=gate_failures,
            unscored_weight=unscored_weight,
            valid=True,
        )
    total = sum(
        (component.value or 0.0) * component.weight for component in components if component.scored
    )
    total = min(1.0, max(0.0, total))
    if unscored_weight > MAX_UNSCORED_WEIGHT + WEIGHT_TOLERANCE:
        return ScoreTotal(
            score_total=total,
            passed=False,
            hard_gate_failures=[],
            unscored_weight=unscored_weight,
            valid=False,
            invalid_reason=(
                f"unscored weight {unscored_weight:.3f} exceeds {MAX_UNSCORED_WEIGHT:.2f}"
            ),
        )
    return ScoreTotal(
        score_total=total,
        passed=total >= pass_threshold,
        hard_gate_failures=[],
        unscored_weight=unscored_weight,
        valid=True,
    )


# ---------------------------------------------------------------------------
# published benchmark task and attempt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    kind: str
    prompt: str
    repository: dict[str, Any]
    environment: dict[str, Any]
    limits: dict[str, Any] = field(
        default_factory=lambda: {"wall_time_seconds": 1800, "max_output_chars": 20000}
    )
    scoring: dict[str, Any] = field(
        default_factory=lambda: {"score_range": [0.0, 1.0], "pass_threshold": 0.8}
    )

    def __post_init__(self) -> None:
        require_task_id(self.task_id, "benchmark_task.task_id")
        if self.kind not in TASK_KINDS:
            raise SchemaError(f"benchmark_task.kind must be one of {TASK_KINDS}")
        if not self.prompt.strip():
            raise SchemaError("benchmark_task.prompt must be non-empty")
        if len(self.prompt) > MAX_PROMPT_CHARS:
            raise SchemaError(
                f"benchmark_task.prompt must be at most {MAX_PROMPT_CHARS} characters"
            )
        for key in ("repo_id", "base_sha", "base_tree"):
            if not isinstance(self.repository.get(key), str):
                raise SchemaError(f"benchmark_task.repository.{key} must be a string")
        require_hex40(str(self.repository["base_sha"]), "benchmark_task.repository.base_sha")
        require_hex40(str(self.repository["base_tree"]), "benchmark_task.repository.base_tree")
        for forbidden in ("outcome_sha", "outcome_tree", "root_at_capture"):
            if forbidden in self.repository:
                raise SchemaError(
                    f"benchmark_task.repository must not expose oracle field {forbidden!r}"
                )
        if self.environment.get("network") != "disabled":
            raise SchemaError("benchmark_task.environment.network must be 'disabled'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BENCHMARK_TASK_SCHEMA,
            "task_id": self.task_id,
            "kind": self.kind,
            "prompt": self.prompt,
            "repository": dict(self.repository),
            "environment": dict(self.environment),
            "limits": dict(self.limits),
            "scoring": dict(self.scoring),
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "benchmark_task") -> BenchmarkTask:
        payload = _mapping(data, where)
        _schema_version(payload, BENCHMARK_TASK_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "task_id",
                "kind",
                "prompt",
                "repository",
                "environment",
                "limits",
                "scoring",
            ),
            where,
        )
        return cls(
            task_id=_string(payload, "task_id", where),
            kind=_string(payload, "kind", where),
            prompt=_string(payload, "prompt", where),
            repository=_mapping(payload.get("repository"), f"{where}.repository"),
            environment=_mapping(payload.get("environment"), f"{where}.environment"),
            limits=_mapping(payload.get("limits", {}), f"{where}.limits")
            if "limits" in payload
            else {"wall_time_seconds": 1800, "max_output_chars": 20000},
            scoring=_mapping(payload.get("scoring", {}), f"{where}.scoring")
            if "scoring" in payload
            else {"score_range": [0.0, 1.0], "pass_threshold": 0.8},
        )


@dataclass(frozen=True)
class BenchmarkAttempt:
    attempt_id: str
    task_id: str
    agent_id: str
    seed: int
    status: str
    source_id: str | None = None
    agent_config_sha256: str | None = None
    base_bundle_sha256: str | None = None
    candidate_state_sha256: str | None = None
    scorer_package_sha256: str | None = None
    input_sha256: str | None = None
    score: float | None = None
    passed: bool | None = None
    pass_threshold: float = 0.8
    components: list[dict[str, Any]] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    wall_time_ms: int = 0
    cost_usd: float | None = None
    artifact_run: str | None = None
    score_report: str | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: str | None = None

    def __post_init__(self) -> None:
        require_task_id(self.task_id, "attempt.task_id")
        if self.status not in ATTEMPT_STATUSES:
            raise SchemaError(
                f"attempt.status must be one of {ATTEMPT_STATUSES}, got {self.status!r}"
            )
        if self.status == "scored":
            if self.score is None:
                raise SchemaError("scored attempts must carry a score")
            if not 0.0 <= self.score <= 1.0:
                raise SchemaError("attempt.score must be within [0, 1]")
            if self.passed is None:
                raise SchemaError("scored attempts must carry passed")
        elif self.score is not None:
            raise SchemaError(f"attempt status {self.status!r} must not carry a score")
        elif self.passed is not None:
            raise SchemaError(f"attempt status {self.status!r} must not carry passed")
        if self.seed < 0:
            raise SchemaError("attempt.seed must be non-negative")
        if not 0.0 <= self.pass_threshold <= 1.0:
            raise SchemaError("attempt.pass_threshold must be within [0, 1]")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise SchemaError("attempt.cost_usd must be non-negative")

    @property
    def is_numeric(self) -> bool:
        return self.status == "scored" and self.score is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": BENCHMARK_ATTEMPT_SCHEMA,
            "attempt_id": self.attempt_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "seed": self.seed,
            "status": self.status,
            "source_id": self.source_id,
            "agent_config_sha256": self.agent_config_sha256,
            "base_bundle_sha256": self.base_bundle_sha256,
            "candidate_state_sha256": self.candidate_state_sha256,
            "scorer_package_sha256": self.scorer_package_sha256,
            "input_sha256": self.input_sha256,
            "score": self.score,
            "passed": self.passed,
            "pass_threshold": self.pass_threshold,
            "components": [dict(component) for component in self.components],
            "tokens": dict(self.tokens),
            "wall_time_ms": self.wall_time_ms,
            "cost_usd": self.cost_usd,
            "artifact_run": self.artifact_run,
            "score_report": self.score_report,
            "error": self.error,
            "warnings": list(self.warnings),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Any, where: str = "attempt") -> BenchmarkAttempt:
        payload = _mapping(data, where)
        _schema_version(payload, BENCHMARK_ATTEMPT_SCHEMA, where)
        _known_keys(
            payload,
            (
                "schema_version",
                "attempt_id",
                "task_id",
                "agent_id",
                "seed",
                "status",
                "source_id",
                "agent_config_sha256",
                "base_bundle_sha256",
                "candidate_state_sha256",
                "scorer_package_sha256",
                "input_sha256",
                "score",
                "passed",
                "pass_threshold",
                "components",
                "tokens",
                "wall_time_ms",
                "cost_usd",
                "artifact_run",
                "score_report",
                "error",
                "warnings",
                "created_at",
            ),
            where,
        )
        raw_score = payload.get("score")
        score = (
            None if raw_score is None else _number(payload, "score", where, minimum=0.0, maximum=1.0)
        )
        passed = payload.get("passed")
        if passed is not None and not isinstance(passed, bool):
            raise SchemaError(f"{where}.passed must be a boolean or null")
        tokens_map = _mapping(payload.get("tokens", {}), f"{where}.tokens")
        tokens: dict[str, int] = {}
        for key, value in tokens_map.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise SchemaError(f"{where}.tokens.{key} must be an integer")
            tokens[key] = value
        raw_components = _list(payload, "components", where) if "components" in payload else []
        components: list[dict[str, Any]] = []
        for index, component in enumerate(raw_components):
            components.append(
                _mapping(component, f"{where}.components[{index}]")
            )
        raw_cost = payload.get("cost_usd")
        cost_usd = (
            None
            if raw_cost is None
            else _number(payload, "cost_usd", where, minimum=0.0)
        )
        return cls(
            attempt_id=_string(payload, "attempt_id", where),
            task_id=_string(payload, "task_id", where),
            agent_id=_string(payload, "agent_id", where),
            seed=_integer(payload, "seed", where, minimum=0),
            status=_string(payload, "status", where),
            source_id=_optional_string(payload, "source_id", where),
            agent_config_sha256=_optional_string(payload, "agent_config_sha256", where),
            base_bundle_sha256=_optional_string(payload, "base_bundle_sha256", where),
            candidate_state_sha256=_optional_string(payload, "candidate_state_sha256", where),
            scorer_package_sha256=_optional_string(payload, "scorer_package_sha256", where),
            input_sha256=_optional_string(payload, "input_sha256", where),
            score=score,
            passed=passed,
            pass_threshold=_number(
                payload, "pass_threshold", where, minimum=0.0, maximum=1.0
            )
            if "pass_threshold" in payload
            else 0.8,
            components=components,
            tokens=tokens,
            wall_time_ms=_integer(payload, "wall_time_ms", where, minimum=0)
            if "wall_time_ms" in payload
            else 0,
            cost_usd=cost_usd,
            artifact_run=_optional_string(payload, "artifact_run", where),
            score_report=_optional_string(payload, "score_report", where),
            error=_optional_string(payload, "error", where),
            warnings=_string_list(payload, "warnings", where, allow_empty_items=True)
            if "warnings" in payload
            else [],
            created_at=_optional_string(payload, "created_at", where),
        )


__all__ = [
    "ADJACENCY_OPERATORS",
    "ATTEMPT_STATUSES",
    "BASE_RESOLUTIONS",
    "BENCHMARK_ATTEMPT_SCHEMA",
    "BENCHMARK_TASK_SCHEMA",
    "COMPONENT_KINDS",
    "MAX_ADJACENT_PER_REPLAY",
    "MAX_PROMPT_CHARS",
    "MAX_REPLAY_TASKS",
    "MAX_UNSCORED_WEIGHT",
    "OBSERVABLE_IMPORTANCES",
    "OUTCOME_RESOLUTIONS",
    "PROJECT_ENVIRONMENT_SCHEMA",
    "REJECTION_CODES",
    "SCORER_MODES",
    "SCORER_SCHEMA",
    "SCORE_INPUT_SCHEMA",
    "SCORE_REPORT_SCHEMA",
    "SCORE_STATUSES",
    "SOURCE_BUNDLE_SCHEMA",
    "STATE_CONFIDENCES",
    "TASK_DEFINITIONS_SCHEMA",
    "TASK_KINDS",
    "USABLE_BASE_RESOLUTIONS",
    "WEIGHT_TOLERANCE",
    "Adjacency",
    "BenchmarkAttempt",
    "BenchmarkTask",
    "CommandRecord",
    "GoalSegment",
    "JudgeConfig",
    "Observable",
    "ProjectEnvironment",
    "PromptProvenance",
    "RepoAnchor",
    "RepoEvidence",
    "SchemaError",
    "ScoreComponentResult",
    "ScoreEvidence",
    "ScoreInput",
    "ScoreReport",
    "ScoreTotal",
    "ScorerBrief",
    "ScorerComponent",
    "ScorerManifest",
    "ScorerRuntime",
    "SourceBundleManifest",
    "TaskCandidate",
    "TaskConfidence",
    "TaskDefinitions",
    "TaskLimits",
    "TaskRejection",
    "canonical_json",
    "compute_score_total",
    "compute_task_id",
    "content_sha256",
    "normalize_prompt",
    "require_hex40",
    "require_rejection_code",
    "require_sha256_hex",
    "require_sha256_ref",
    "require_task_id",
    "text_sha256",
]
