"""Atomic publication of dashboard generations."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ..storage import Layout
from .build import BuildReport, build, normalize_mode

BUILDS_DIRNAME = ".retro-builds"
CACHE_DIRNAME = ".retro-cache"
STAGING_PREFIX = ".staging-"
CURRENT_LINK = "current"
PUBLISHED_LINKS = ("index.html", "assets", "data")
KEEP_GENERATIONS = 2
STALE_STAGING_AGE_S = 6 * 3600


def build_and_publish(
    layout: Layout,
    output_dir: Path,
    *,
    mode: str = "auto",
    keep: int = KEEP_GENERATIONS,
    report: list | None = None,
) -> Path:
    """Build into a staging dir and publish it atomically; returns the index path."""
    resolved_mode = normalize_mode(mode)
    output_dir = Path(output_dir)
    builds_dir = output_dir / BUILDS_DIRNAME
    builds_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale_staging(builds_dir)

    staging = Path(tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=builds_dir))
    try:
        build_report = build(
            layout,
            staging,
            mode=resolved_mode,
            cache_dir=output_dir / CACHE_DIRNAME,
        )
        if report is not None:
            report.append(build_report)
        return publish_generation(staging, output_dir, keep=keep)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def publish_generation(staging: Path, output_dir: Path, *, keep: int = KEEP_GENERATIONS) -> Path:
    """Swap one pointer.

    ``index.html``, ``assets`` and ``data`` are permanent symlinks that resolve
    through ``current``, so publishing is a single atomic rename of ``current``
    once the new generation is fully materialised. A reader can therefore never
    observe a mix of two generations, and a failure before the swap leaves the
    previous generation serving untouched.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    builds_dir = output_dir / BUILDS_DIRNAME
    builds_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    generation = builds_dir / name
    os.replace(staging, generation)

    ensure_entry_points(output_dir)
    _swap_symlink(output_dir / CURRENT_LINK, Path(BUILDS_DIRNAME) / name)
    _trim_generations(builds_dir, generation, keep=keep)
    return output_dir / "index.html"


def ensure_entry_points(output_dir: Path) -> list[str]:
    """Create the stable entry-point symlinks when missing or pointing elsewhere."""
    repaired: list[str] = []
    for link in PUBLISHED_LINKS:
        path = output_dir / link
        target = Path(CURRENT_LINK) / link
        if path.is_symlink() and os.readlink(path) == str(target):
            continue
        _swap_symlink(path, target)
        repaired.append(link)
    return repaired


def build_dashboard(
    layout: Layout,
    output_dir: Path,
    *,
    mode: str = "auto",
    publish: bool = True,
) -> BuildReport:
    """Build (and by default publish) a dashboard, returning the build report."""
    if not publish:
        return build(
            layout,
            Path(output_dir),
            mode=mode,
            cache_dir=Path(output_dir) / CACHE_DIRNAME,
        )
    reports: list[BuildReport] = []
    index_path = build_and_publish(layout, Path(output_dir), mode=mode, report=reports)
    report = reports[0]
    report.index_path = index_path
    report.output_dir = Path(output_dir)
    return report


def _swap_symlink(path: Path, target: Path) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.is_symlink() or temporary.exists():
        _remove(temporary)
    temporary.symlink_to(target, target_is_directory=not target.name.endswith(".html"))
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() and not path.is_symlink():
        path.unlink()
    os.replace(temporary, path)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _trim_generations(builds_dir: Path, current: Path, *, keep: int) -> None:
    generations = sorted(
        (item for item in builds_dir.iterdir() if item.is_dir() and not item.name.startswith(".")),
        key=lambda item: item.name,
        reverse=True,
    )
    retained = 0
    for generation in generations:
        if generation == current:
            retained += 1
            continue
        if retained < max(1, keep):
            retained += 1
            continue
        shutil.rmtree(generation, ignore_errors=True)


def _clean_stale_staging(builds_dir: Path) -> None:
    now = time.time()
    for item in builds_dir.iterdir():
        if not item.name.startswith(STAGING_PREFIX) or not item.is_dir():
            continue
        try:
            age = now - item.stat().st_mtime
        except OSError:
            continue
        if age > STALE_STAGING_AGE_S:
            shutil.rmtree(item, ignore_errors=True)
