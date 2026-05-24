"""External signals — touch state outside the rollout (git, filesystem)."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from .base import SessionContext, reading, register


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_bounds(ctx: SessionContext) -> tuple[datetime, datetime] | None:
    stamps = [_parse_ts(e.timestamp) for e in ctx.events if e.timestamp]
    stamps = [s for s in stamps if s is not None]
    if len(stamps) < 2:
        return None
    return min(stamps), max(stamps)


def _is_git_repo(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


@register(
    "git_commits_made_during",
    group="outcome",
    kind="numeric",
    method="external",
    unit="count",
    description="Commits authored in the session's cwd between first and last event timestamp.",
)
def _git_commits_made_during(ctx: SessionContext):
    cwd = ctx.cwd
    if not cwd:
        return None
    cwd_path = Path(cwd)
    if not cwd_path.exists():
        return _missing(ctx, "cwd_missing", cwd)
    if not _is_git_repo(cwd_path):
        return _missing(ctx, "not_a_git_repo", cwd)
    bounds = _session_bounds(ctx)
    if bounds is None:
        return _missing(ctx, "insufficient_timestamps", cwd)
    start, end = bounds
    # Pad the window slightly so a commit made right at session end still counts.
    start_arg = start.isoformat()
    end_arg = end.isoformat()
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(cwd_path),
                "log",
                f"--since={start_arg}",
                f"--until={end_arg}",
                "--pretty=oneline",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return _missing(ctx, "git_timeout", cwd)
    if result.returncode != 0:
        return _missing(ctx, "git_error", cwd, extra={"stderr": result.stderr.strip()[:200]})
    commits = [line for line in result.stdout.splitlines() if line.strip()]
    return reading(
        ctx,
        _git_commits_made_during,
        len(commits),
        metadata={
            "cwd": cwd,
            "window_start": start_arg,
            "window_end": end_arg,
            "first_commits": commits[:5],
        },
    )


def _missing(ctx: SessionContext, reason: str, cwd: str | None, extra: dict | None = None):
    # Return a reading with value=None so aggregates can count missing readings explicitly.
    meta = {"reason": reason, "cwd": cwd}
    if extra:
        meta.update(extra)
    return reading(ctx, _git_commits_made_during, None, confidence=0.0, metadata=meta)
