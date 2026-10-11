"""Dashboard build output: shards, manifest, incremental detail buckets, smoke checks."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from retro.dashboard.build import build, copy_assets
from retro.dashboard.facts import COLUMNS
from retro.dashboard.shards import js_payload
from retro.storage import Layout
from tests.dashboard_helpers import archive, empty_archive, read_shard  # noqa: F401

KEY_MAIN = "codex/019d0001-0000-7000-8000-000000000001"


@pytest.fixture
def built(archive: Layout, tmp_path: Path):  # noqa: F811
    out = tmp_path / "dashboard"
    report = build(archive, out, cache_dir=tmp_path / "cache")
    return out, report


def manifest_of(out: Path) -> dict:
    name, payload = read_shard(out / "data" / "manifest.js")
    assert name == "manifest"
    return payload


def test_build_emits_a_small_shell_and_assets(built):
    out, report = built
    assert report.index_path == out / "index.html"
    assert report.index_bytes < 300 * 1024
    html = (out / "index.html").read_text(encoding="utf-8")
    for reference in re.findall(r'(?:src|href)="([^"]+)"', html):
        if reference.startswith("#"):
            continue
        assert (out / reference).exists(), reference
    assert "type=\"module\"" not in html
    assert "http://" not in html and "https://" not in html


def test_manifest_describes_the_fact_table(built):
    out, _ = built
    manifest = manifest_of(out)
    assert manifest["schema"] == 1
    assert manifest["columns"] == list(COLUMNS)
    assert manifest["counts"]["sessions"] == 8
    assert manifest["counts"]["included"] == 6
    assert manifest["dict"]["agents"]
    assert manifest["signals"]["version"] == "sig-v2-0001"
    assert "usage.cost_usd" in manifest["signals"]["headline"]
    assert manifest["signals"]["encodings"]["outcome.status"]["encoding"] == "cat"
    assert manifest["quality"]["excluded"] == 2
    assert manifest["sync"]["latest"]["status"] == "failed"
    assert manifest["archive"]["url"].startswith("file://")


def test_every_declared_shard_exists_and_parses(built):
    out, _ = built
    manifest = manifest_of(out)
    counts = {}
    for channel in ("sessions", "excluded"):
        total = 0
        for shard in manifest["shards"][channel]:
            name, payload = read_shard(out / shard["file"])
            assert name == channel + ":" + str(shard["shard"])
            assert len(payload["rows"]) == shard["rows"]
            assert payload["offset"] == shard["offset"]
            total += len(payload["rows"])
            for row in payload["rows"]:
                assert len(row) == len(COLUMNS)
        counts[channel] = total
    assert counts["sessions"] == manifest["counts"]["included"]
    assert counts["excluded"] == manifest["counts"]["excluded"]

    for shard in manifest["shards"]["signals"]:
        name, payload = read_shard(out / shard["file"])
        assert name == "signals:" + str(shard["shard"])
        for values in payload["signals"].values():
            assert len(values) % 2 == 0

    for path in (out / "data" / "detail").glob("*.js"):
        name, payload = read_shard(path)
        assert name.startswith("detail:")
        assert payload["sessions"]
    for path in (out / "data" / "detail-x").glob("*.js"):
        name, payload = read_shard(path)
        assert name.startswith("detailx:")
        assert all(session["minimal"] for session in payload["sessions"].values())


def test_detail_shards_hold_full_session_records(built):
    out, _ = built
    manifest = manifest_of(out)
    bucket = None
    for shard in manifest["shards"]["sessions"]:
        _, payload = read_shard(out / shard["file"])
        for row in payload["rows"]:
            if row[0] == KEY_MAIN:
                bucket = row[manifest["columns"].index("bucket")]
    assert bucket is not None
    _, detail = read_shard(out / "data" / "detail" / f"d-{bucket:04d}.js")
    session = detail["sessions"][KEY_MAIN]
    assert session["signals"]["evidence"]["outcome.status"] == ["ev-401", "ev-402"]
    assert session["tag_sources"]["keep"] == ["marker"]
    assert session["paths"]["transcript"].endswith(".md")


def test_payload_escapes_script_terminators(tmp_path: Path):
    path = tmp_path / "shard.js"
    path.write_text(js_payload("x", {"title": "</script><img src=x>&"}), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "</script>" not in text and "&" not in text
    name, payload = read_shard(path)
    assert name == "x"
    assert payload["title"] == "</script><img src=x>&"


def test_build_is_incremental_between_runs(archive: Layout, tmp_path: Path):  # noqa: F811
    cache = tmp_path / "cache"
    first = build(archive, tmp_path / "one", cache_dir=cache)
    all_shards = first.detail_shards + first.excluded_detail_shards
    assert first.detail_written == all_shards
    assert first.detail_cached == 0

    second = build(archive, tmp_path / "two", cache_dir=cache)
    assert second.detail_cached == all_shards
    assert second.detail_written == 0

    path = archive.root / "catalog" / "sessions.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines = [line.replace('"fingerprint": "fp-codex-1"', '"fingerprint": "fp-codex-1b"') for line in lines]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    third = build(archive, tmp_path / "three", cache_dir=cache)
    assert third.detail_written == 1
    assert third.detail_cached == all_shards - 1


def test_full_mode_rebuilds_every_detail_shard(archive: Layout, tmp_path: Path):  # noqa: F811
    cache = tmp_path / "cache"
    build(archive, tmp_path / "one", cache_dir=cache)
    report = build(archive, tmp_path / "two", mode="full", cache_dir=cache)
    assert report.detail_cached == 0
    assert report.detail_written == report.detail_shards + report.excluded_detail_shards


def test_cache_drops_buckets_that_no_longer_exist(archive: Layout, tmp_path: Path):  # noqa: F811
    cache = tmp_path / "cache"
    build(archive, tmp_path / "one", cache_dir=cache)
    for name, prefix in (("detail", "d"), ("detail-x", "x")):
        index = json.loads((cache / name / "index.json").read_text(encoding="utf-8"))
        assert index["buckets"]
        assert list((cache / name).glob(f"{prefix}-*.js"))
    (archive.root / "catalog" / "sessions.jsonl").write_text("", encoding="utf-8")
    build(archive, tmp_path / "two", cache_dir=cache)
    for name, prefix in (("detail", "d"), ("detail-x", "x")):
        index = json.loads((cache / name / "index.json").read_text(encoding="utf-8"))
        assert index["buckets"] == {}
        assert not list((cache / name).glob(f"{prefix}-*.js"))


def test_build_warns_when_inputs_are_missing(empty_archive: Layout, tmp_path: Path):  # noqa: F811
    report = build(empty_archive, tmp_path / "dashboard")
    assert report.sessions == 0
    assert any("catalog missing" in warning for warning in report.warnings)
    assert any("signal store missing" in warning for warning in report.warnings)
    manifest = manifest_of(tmp_path / "dashboard")
    assert manifest["counts"]["sessions"] == 0


def test_first_load_excludes_the_excluded_payload(built):
    out, report = built
    manifest = manifest_of(out)
    boot = [out / "index.html", out / "data" / "manifest.js"]
    boot += [out / shard["file"] for shard in manifest["shards"]["sessions"]]
    boot += [path for path in (out / "assets").iterdir()]
    boot_bytes = sum(path.stat().st_size for path in boot)
    lazy_bytes = sum(
        (out / shard["file"]).stat().st_size for shard in manifest["shards"]["excluded"]
    ) + (out / manifest["shards"]["dict_tail"]["file"]).stat().st_size
    assert boot_bytes > 0
    assert lazy_bytes > 0
    assert report.excluded_shards >= 1
    assert manifest["shards"]["detail_excluded"]["buckets"] >= 1


def test_manifest_dictionary_holds_only_the_included_prefix(built):
    out, _ = built
    manifest = manifest_of(out)
    split = manifest["dict_split"]
    assert len(manifest["dict"]["projects"]) == split["projects"]
    _, tail = read_shard(out / manifest["shards"]["dict_tail"]["file"])
    assert tail["projects"]["offset"] == split["projects"]
    assert tail["projects"]["items"]
    ids = [entry["id"] for entry in manifest["dict"]["projects"]]
    assert "path:/tmp/eval" not in ids
    assert any(entry["id"] == "path:/tmp/eval" for entry in tail["projects"]["items"])


def test_manifest_publishes_cost_basis_and_sync_health(built):
    out, _ = built
    manifest = manifest_of(out)
    assert manifest["cost_basis"][0] == "exact"
    assert manifest["quality"]["cost"]["unpriced"] == 2
    latest = manifest["sync"]["latest"]
    assert latest["health"] == "failed"
    assert latest["unchanged"] == 0


def test_stale_signals_surface_as_a_warning(built):
    _, report = built
    assert any("older than their catalog fingerprint" in warning for warning in report.warnings)


def test_build_never_copies_transcripts(built):
    out, _ = built
    assert not (out / "rendered").exists()
    assert not list(out.rglob("*.md"))


def test_build_report_records_size_and_mode(built):
    out, report = built
    assert report.mode == "auto"
    assert report.bytes_total > 0
    assert report.duration_s >= 0
    name, payload = read_shard(out / "data" / "build.js")
    assert name == "build"
    assert payload["sessions"] == 8


def test_legacy_cost_modes_are_accepted(archive: Layout, tmp_path: Path):  # noqa: F811
    assert build(archive, tmp_path / "legacy", mode="display").mode == "auto"
    with pytest.raises(ValueError):
        build(archive, tmp_path / "bad", mode="nonsense")


def test_static_assets_stay_offline_and_controls_stay_local_and_framework_free(tmp_path: Path):
    out = tmp_path / "assets-contract"
    copy_assets(out)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "type=\"module\"" not in html
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts == [
        "data/manifest.js", "assets/core.js", "assets/views.js", "assets/benchmarks.js",
        "assets/experiments.js", "assets/app.js",
    ]
    for asset in sorted((out / "assets").iterdir()):
        text = asset.read_text(encoding="utf-8")
        if asset.name == "experiments.js":
            assert 'global.fetch("/api/" + path' in text
            assert 'global.location.protocol === "http:"' in text
            assert 'options.headers["X-Retro-Control-Token"] = token' in text
        else:
            assert "fetch(" not in text
        assert "innerHTML" not in text
        assert "import " not in text.replace("important", "")
        for url in re.findall(r"https?://[^\s\"')]+", text):
            assert url.startswith("http://www.w3.org/"), url


def test_copy_assets_is_idempotent(tmp_path: Path):
    out = tmp_path / "assets-out"
    copy_assets(out)
    copy_assets(out)
    assert (out / "index.html").exists()
    assert (out / "assets" / "app.css").exists()
    assert (out / "assets" / "core.js").exists()
