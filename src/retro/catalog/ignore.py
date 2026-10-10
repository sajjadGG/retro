"""Persistent nearest-ancestor .retroignore evaluation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..config import TAG_PATTERN
from .models import SessionRecord


@dataclass(frozen=True)
class IgnoreResult:
    tags: list[str]
    path: str | None
    signature: str


def read_retroignore(
    cwd_resolved: str | None,
    *,
    prior: SessionRecord | None = None,
) -> IgnoreResult:
    if cwd_resolved and "://" not in cwd_resolved:
        cwd = Path(cwd_resolved)
        if cwd.exists():
            current = cwd if cwd.is_dir() else cwd.parent
            for directory in (current, *current.parents):
                candidate = directory / ".retroignore"
                if candidate.is_file():
                    return _read_file(candidate)
            return IgnoreResult([], None, "none")
    recovered = _recover_prior(prior)
    if recovered is not None:
        return recovered
    return IgnoreResult([], None, "none")


def _read_file(path: Path) -> IgnoreResult:
    try:
        stat = path.stat()
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return IgnoreResult([], None, "none")
    tags = [
        line.strip()
        for line in lines
        if line.strip()
        and not line.lstrip().startswith("#")
        and TAG_PATTERN.fullmatch(line.strip())
    ]
    if not tags:
        tags = ["ignore"]
    absolute = str(path.resolve(strict=False))
    signature = hashlib.sha256(
        f"{absolute}\0{stat.st_mtime_ns}\0{stat.st_size}".encode()
    ).hexdigest()
    return IgnoreResult(list(dict.fromkeys(tags)), absolute, signature)


def _recover_prior(prior: SessionRecord | None) -> IgnoreResult | None:
    if prior is None:
        return None
    tags: list[str] = []
    path: str | None = None
    for tag, sources in prior.tag_sources.items():
        for source in sources:
            if source.startswith("dotfile:"):
                tags.append(tag)
                path = source[len("dotfile:") :]
                break
    if not tags or path is None:
        return None
    signature = hashlib.sha256(
        f"persisted\0{path}\0{','.join(sorted(tags))}".encode()
    ).hexdigest()
    return IgnoreResult(sorted(set(tags)), path, signature)
