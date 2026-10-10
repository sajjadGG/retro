"""Atomic publication, generation retention and staging cleanup."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from retro.dashboard import publish as publish_module
from retro.dashboard.publish import BUILDS_DIRNAME, build_and_publish, build_dashboard
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
