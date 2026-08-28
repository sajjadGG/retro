"""Atomic publication of generated dashboard generations."""
from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def build_and_publish_dashboard(
    *,
    artifact_root: Path,
    output_dir: Path,
    mode: str = "auto",
    backup_dir: Path | None = None,
) -> Path:
    from .dashboard_build import build

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".retro-dashboard-", dir=output_dir.parent)
    )
    try:
        build(mode=mode, artifact_root=artifact_root, out_dir=staging)
        publish_dashboard_generation(
            staging,
            output_dir,
            backup_dir=backup_dir,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return output_dir / "index.html"


def publish_dashboard_generation(
    staging: Path,
    output_dir: Path,
    *,
    backup_dir: Path | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    builds = output_dir / ".retro-builds"
    builds.mkdir(exist_ok=True)
    generation_name = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-{os.getpid()}"
    )
    generation = builds / generation_name
    shutil.copytree(staging, generation)

    current = output_dir / "current"
    temporary_link = output_dir / f".current.{generation_name}"
    temporary_link.symlink_to(Path(".retro-builds") / generation_name, target_is_directory=True)
    os.replace(temporary_link, current)

    _ensure_compatibility_link(
        output_dir / "index.html",
        Path("current") / "index.html",
        backup_dir=backup_dir,
        output_dir=output_dir,
    )
    _ensure_compatibility_link(
        output_dir / "data",
        Path("current") / "data",
        backup_dir=backup_dir,
        output_dir=output_dir,
    )
    trajectory = generation / "trajectory_experiments.html"
    if trajectory.exists():
        _ensure_compatibility_link(
            output_dir / "trajectory_experiments.html",
            Path("current") / "trajectory_experiments.html",
            backup_dir=backup_dir,
            output_dir=output_dir,
        )

    _trim_generations(builds, current.resolve(), keep=3)
    return output_dir / "index.html"


def _ensure_compatibility_link(
    path: Path,
    target: Path,
    *,
    backup_dir: Path | None,
    output_dir: Path,
) -> None:
    if path.is_symlink() and os.readlink(path) == str(target):
        return
    if path.exists() or path.is_symlink():
        if backup_dir is not None:
            backup_target = backup_dir / path.relative_to(output_dir)
            if not backup_target.exists():
                backup_target.parent.mkdir(parents=True, exist_ok=True)
                if path.is_dir() and not path.is_symlink():
                    shutil.copytree(path, backup_target)
                else:
                    shutil.copy2(path, backup_target, follow_symlinks=True)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    temporary = path.with_name(f".{path.name}.link")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(target, target_is_directory=target.name == "data")
    os.replace(temporary, path)


def _trim_generations(builds: Path, current: Path, *, keep: int) -> None:
    generations = sorted(
        (path for path in builds.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    retained = 0
    for generation in generations:
        if generation.resolve() == current:
            retained += 1
            continue
        if retained < keep:
            retained += 1
            continue
        shutil.rmtree(generation)
