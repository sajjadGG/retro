"""Atomic publication, generation retention and staging cleanup."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from retro.dashboard import publish as publish_module
from retro.dashboard.publish import (
    BUILDS_DIRNAME,
    build_and_publish,
    build_dashboard,
    ensure_entry_points,
    publish_generation,
)
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive  # noqa: F401


def generations(output_dir: Path) -> list[Path]:
    items = (output_dir / BUILDS_DIRNAME).iterdir()
    return sorted(item for item in items if item.is_dir() and not item.name.startswith("."))


def test_publish_creates_symlinks_to_the_current_generation(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    index = build_and_publish(archive, out)
    assert index == out / "index.html"
    assert index.exists()
    assert (out / "current").is_symlink()
    assert os.readlink(out / "current").startswith(BUILDS_DIRNAME)
    for link in ("index.html", "assets", "data"):
        assert (out / link).is_symlink()
        assert os.readlink(out / link) == str(Path("current") / link)
    assert (out / "data" / "manifest.js").exists()
    assert len(generations(out)) == 1


def test_publish_keeps_at_most_two_generations(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    for _ in range(4):
        build_and_publish(archive, out)
    assert len(generations(out)) == 2
    assert (out / "current").resolve() == generations(out)[-1]
    assert (out / "index.html").exists()


def test_publish_leaves_no_staging_directory(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    assert not list((out / BUILDS_DIRNAME).glob(".staging-*"))


def test_failed_build_cleans_up_and_keeps_the_previous_generation(archive, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    previous = (out / "current").resolve()

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(publish_module, "build", explode)
    with pytest.raises(RuntimeError):
        build_and_publish(archive, out)

    assert not list((out / BUILDS_DIRNAME).glob(".staging-*"))
    assert (out / "current").resolve() == previous
    assert (out / "index.html").exists()


def test_stale_staging_directories_are_reaped(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    builds = out / BUILDS_DIRNAME
    builds.mkdir(parents=True)
    stale = builds / ".staging-old"
    stale.mkdir()
    old = 1_700_000_000
    os.utime(stale, (old, old))
    build_and_publish(archive, out)
    assert not stale.exists()


def test_publish_replaces_pre_existing_files(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    out.mkdir()
    (out / "index.html").write_text("stale 1.5 GB page", encoding="utf-8")
    (out / "data").mkdir()
    (out / "data" / "rollouts.json").write_text("{}", encoding="utf-8")
    build_and_publish(archive, out)
    assert (out / "index.html").is_symlink()
    assert (out / "data").is_symlink()
    assert not (out / "data" / "rollouts.json").exists()


def test_detail_cache_lives_outside_generations(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    build_and_publish(archive, out)
    cache = out / ".retro-cache" / "detail"
    assert (cache / "index.json").exists()
    assert list(cache.glob("d-*.js"))
    excluded_cache = out / ".retro-cache" / "detail-x"
    assert (excluded_cache / "index.json").exists()
    assert list(excluded_cache.glob("x-*.js"))


def test_build_dashboard_returns_a_report(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    report = build_dashboard(archive, out)
    assert report.index_path == out / "index.html"
    assert report.sessions == 8
    assert report.detail_shards >= 1

    unpublished = build_dashboard(archive, tmp_path / "plain", publish=False)
    assert (tmp_path / "plain" / "index.html").exists()
    assert not (tmp_path / "plain" / "current").exists()
    assert unpublished.sessions == 8


def test_publish_works_on_an_empty_archive(empty_archive: Layout, tmp_path: Path):  # noqa: F811
    index = build_and_publish(empty_archive, tmp_path / "dashboard")
    assert index.exists()


def read_index(output_dir: Path) -> bytes:
    return (output_dir / "data" / "manifest.js").read_bytes()


def test_entry_points_are_permanent_and_only_current_moves(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    inodes = {link: (out / link).lstat().st_ino for link in ("index.html", "assets", "data")}
    first = (out / "current").resolve()

    build_and_publish(archive, out)
    second = (out / "current").resolve()
    assert second != first
    for link, inode in inodes.items():
        assert (out / link).lstat().st_ino == inode, f"{link} symlink was recreated"
        assert os.readlink(out / link) == str(Path("current") / link)
    assert (out / "index.html").resolve().parent == second


def test_failed_build_leaves_the_previous_generation_serving(archive: Layout, tmp_path: Path, monkeypatch):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    previous = (out / "current").resolve()
    published = read_index(out)

    def explode(layout, staging, **kwargs):
        (Path(staging) / "index.html").write_text("half written", encoding="utf-8")
        (Path(staging) / "data").mkdir(parents=True, exist_ok=True)
        (Path(staging) / "data" / "manifest.js").write_text("broken", encoding="utf-8")
        raise RuntimeError("stage failed")

    monkeypatch.setattr(publish_module, "build", explode)
    with pytest.raises(RuntimeError):
        build_and_publish(archive, out)

    assert (out / "current").resolve() == previous
    assert read_index(out) == published
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert not list((out / BUILDS_DIRNAME).glob(".staging-*"))
    assert len(generations(out)) == 1


def test_failure_while_materialising_keeps_the_old_pointer(archive: Layout, tmp_path: Path, monkeypatch):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    previous = (out / "current").resolve()
    published = read_index(out)

    real_replace = os.replace
    state = {"calls": 0}

    def flaky(src, dst):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(publish_module.os, "replace", flaky)
    with pytest.raises(OSError):
        build_and_publish(archive, out)

    monkeypatch.undo()
    assert (out / "current").resolve() == previous
    assert read_index(out) == published
    assert (out / "data" / "manifest.js").exists()


def test_entry_points_are_repaired_when_replaced_by_real_files(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    (out / "index.html").unlink()
    (out / "index.html").write_text("stale copy", encoding="utf-8")
    if (out / "data").is_symlink():
        (out / "data").unlink()
    else:
        shutil.rmtree(out / "data", ignore_errors=True)
    (out / "data").mkdir(exist_ok=True)
    (out / "data" / "rollouts.json").write_text("{}", encoding="utf-8")

    repaired = ensure_entry_points(out)
    assert set(repaired) == {"index.html", "data"}
    assert (out / "index.html").is_symlink()
    assert (out / "data" / "manifest.js").exists()
    assert not (out / "data" / "rollouts.json").exists()


def test_publish_generation_accepts_a_prepared_staging_dir(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    build_and_publish(archive, out)
    staging = out / BUILDS_DIRNAME / ".staging-manual"
    staging.mkdir(parents=True)
    (staging / "index.html").write_text("<!DOCTYPE html><html></html>", encoding="utf-8")
    (staging / "data").mkdir()
    (staging / "data" / "manifest.js").write_text("RETRO_DATA(\"manifest\",{});\n", encoding="utf-8")
    (staging / "assets").mkdir()

    publish_generation(staging, out)
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    assert read_index(out) == b"RETRO_DATA(\"manifest\",{});\n"
    assert len(generations(out)) == 2
