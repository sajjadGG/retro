"""Explicit provenance for prompts executed by experiment/evaluation runners."""
from __future__ import annotations

import re

from .config import TAG_PATTERN
from .schema import SESSION_ID_PATTERN

EXPERIMENT_TAG = "experiment"
EXPERIMENT_TAG_PREFIX = f"{EXPERIMENT_TAG}/"
EXPERIMENT_RULE_ID = "builtin.experiment"
MAX_EXPERIMENT_ID_LENGTH = 64 - len(EXPERIMENT_TAG_PREFIX)
EXPERIMENT_METADATA_HEADER = "[Retro experiment run metadata]"
EXPERIMENT_METADATA_FOOTER = "[End Retro experiment run metadata]"
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")


def is_experiment_tag(value: object) -> bool:
    """Recognize the reserved tag or a single path-safe experiment ID tag."""
    if not isinstance(value, str):
        return False
    folded = value.casefold()
    if folded == EXPERIMENT_TAG:
        return True
    if not folded.startswith(EXPERIMENT_TAG_PREFIX):
        return False
    experiment_id = value[len(EXPERIMENT_TAG_PREFIX) :]
    return bool(
        SESSION_ID_PATTERN.fullmatch(experiment_id)
        and TAG_PATTERN.fullmatch(value)
    )


def tag_experiment_prompt(prompt: str, experiment_id: str) -> str:
    """Append visible run metadata without modifying the original task prompt.

    IDs follow Retro's session naming convention and must fit a 64-character
    tag value. Reapplying the same metadata to an already-tagged prompt is a
    no-op. Invalid IDs raise ``ValueError``.
    """
    if not isinstance(experiment_id, str) or not SESSION_ID_PATTERN.fullmatch(experiment_id):
        raise ValueError(
            "experiment ID must be a single path-safe segment starting with a "
            "letter or digit and containing only letters, digits, dots, underscores, or hyphens"
        )
    tag = f"{EXPERIMENT_TAG_PREFIX}{experiment_id}"
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError(f"experiment ID must be at most {MAX_EXPERIMENT_ID_LENGTH} characters")
    block = (
        f"{EXPERIMENT_METADATA_HEADER}\n"
        "This is automated experiment/evaluation run provenance, not human task evidence.\n"
        "#retro:ignore\n"
        f"#retro:tag={EXPERIMENT_TAG}\n"
        f"#retro:tag={tag}\n"
        f"{EXPERIMENT_METADATA_FOOTER}"
    )
    suffix = f"\n\n{block}"
    fence = _unclosed_fence(prompt)
    if prompt.endswith(suffix) and fence is None:
        return prompt
    closure = f"\n{fence}\n" if fence else ""
    return f"{prompt}{closure}{suffix}"


def _unclosed_fence(text: str) -> str | None:
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE_RE.match(line)
        if match is None:
            continue
        token, remainder = match.groups()
        if fence is None:
            fence = token
        elif (
            token[0] == fence[0]
            and len(token) >= len(fence)
            and not remainder.strip()
        ):
            fence = None
    return fence
