"""Inline catalog directive parsing."""
from __future__ import annotations

import re
from typing import Any

_MARKER_RE = re.compile(
    r"(?<![A-Za-z0-9_])#retro:(?P<directive>ignore|keep|"
    r"tag=(?P<tag>[A-Za-z0-9][A-Za-z0-9._/-]{0,63})|"
    r"project=(?P<project>[A-Za-z0-9][A-Za-z0-9._/-]{0,63}))"
    r"(?![A-Za-z0-9._/-])",
    re.IGNORECASE,
)
_INLINE_CODE_RE = re.compile(r"(`+).*?\1")
_QUOTED_TEXT_RE = re.compile(
    r""""(?:\\.|[^"\\])*"|(?<!\w)(?:[rubf]{1,2})?'(?:\\.|[^'\\])*'|“[^”]*”|‘[^’]*’""",
    re.IGNORECASE,
)


def parse_markers(text: str, *, event_id: str | None) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for match in _MARKER_RE.finditer(_marker_visible_text(text)):
        raw = match.group("directive")
        lowered = raw.casefold()
        if lowered.startswith("tag="):
            directive = "tag"
            value = match.group("tag")
        elif lowered.startswith("project="):
            directive = "project"
            value = match.group("project")
        else:
            directive = lowered
            value = None
        markers.append(
            {
                "directive": directive,
                "value": value,
                "event_id": event_id,
            }
        )
    return markers


def _marker_visible_text(text: str) -> str:
    visible: list[str] = []
    fence: str | None = None
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})(.*)$", stripped)
        if fence_match:
            token, remainder = fence_match.groups()
            if fence is None:
                fence = token
            elif (
                fence[0] == token[0]
                and len(token) >= len(fence)
                and not remainder.strip()
            ):
                fence = None
            continue
        if fence is not None or stripped.startswith(">") or line.startswith(("    ", "\t")):
            continue
        visible.append(_QUOTED_TEXT_RE.sub("", _INLINE_CODE_RE.sub("", line)))
    return "\n".join(visible)
