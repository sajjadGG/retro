from __future__ import annotations

import re
from typing import Any

_EXIT_CODE_RE = re.compile(
    r"(?:process|command)\s+exited\s+with\s+(?:code\s+)?(-?\d+)",
    re.IGNORECASE,
)


def normalized_payload(
    source: dict[str, Any] | None,
    *,
    timestamp_inferred: bool = False,
    **fields: Any,
) -> dict[str, Any]:
    payload = dict(source or {})
    payload.update(fields)
    if timestamp_inferred:
        payload["timestamp_inferred"] = True
    return payload


def resolve_timestamp(
    timestamp: str | None,
    previous: str | None,
) -> tuple[str | None, str | None, bool]:
    if timestamp:
        return timestamp, timestamp, False
    if previous:
        return previous, previous, True
    return None, previous, False


def normalized_result_fields(*sources: object) -> dict[str, Any]:
    mappings = [source for source in sources if isinstance(source, dict)]
    expanded = list(mappings)
    for source in mappings:
        for key in (
            "shellExecution",
            "resultDetails",
            "toolUseResult",
            "error",
        ):
            nested = source.get(key)
            if isinstance(nested, dict):
                expanded.append(nested)

    success: bool | None = None
    is_error: bool | None = None
    exit_code: int | None = None
    for source in expanded:
        if success is None and isinstance(source.get("success"), bool):
            success = source["success"]
        for key in ("is_error", "isError"):
            if is_error is None and isinstance(source.get(key), bool):
                is_error = source[key]
        for key in ("exit_code", "exitCode"):
            value = source.get(key)
            if exit_code is None and isinstance(value, int) and not isinstance(value, bool):
                exit_code = value
        status = source.get("status")
        if isinstance(status, str) and status.casefold() in {"error", "failed", "failure"}:
            is_error = True
        if source.get("error") not in (None, "", False, {}):
            is_error = True
        if "Err" in source:
            is_error = True
        if "Ok" in source and is_error is None:
            is_error = False
        if exit_code is None:
            for key in ("output", "content", "aggregated_output", "stderr", "stdout"):
                text = source.get(key)
                if not isinstance(text, str):
                    continue
                match = _EXIT_CODE_RE.search(text[-4096:])
                if match:
                    exit_code = int(match.group(1))
                    break

    if exit_code is None:
        for raw_source in sources:
            if not isinstance(raw_source, str):
                continue
            match = _EXIT_CODE_RE.search(raw_source[-4096:])
            if match:
                exit_code = int(match.group(1))
                break

    if success is not None:
        is_error = not success
    elif is_error is not None:
        success = not is_error
    elif exit_code is not None:
        success = exit_code == 0
        is_error = not success

    result: dict[str, Any] = {}
    if success is not None:
        result["success"] = success
    if is_error is not None:
        result["is_error"] = is_error
    if exit_code is not None:
        result["exit_code"] = exit_code
    return result
