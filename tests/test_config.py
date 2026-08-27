from __future__ import annotations

import json
from pathlib import Path

from retro.config import (
    RetroConfig,
    config_path,
    load_config,
    resolve_archive_root,
    resolve_dashboard_dir,
    save_config,
)
from retro.storage import default_layout


def test_config_round_trip_and_default_layout(tmp_path: Path):
    archive = tmp_path / "global archive"
    dashboard = tmp_path / "global dashboard"
    saved = save_config(
        RetroConfig(
            archive_root=str(archive),
            dashboard_dir=str(dashboard),
            sync_interval_seconds=900,
            sync_on_login=True,
        )
    )

    assert saved == config_path()
    assert load_config().archive_root == str(archive.resolve())
    assert resolve_archive_root() == archive.resolve()
    assert resolve_dashboard_dir() == dashboard.resolve()
    assert default_layout().root == archive.resolve()


def test_root_precedence(monkeypatch, tmp_path: Path):
    configured = tmp_path / "configured"
    legacy = tmp_path / "legacy"
    env_root = tmp_path / "env"
    explicit = tmp_path / "explicit"
    save_config(
        RetroConfig(
            archive_root=str(configured),
            dashboard_dir=str(tmp_path / "dashboard"),
        )
    )

    monkeypatch.setenv("RETRO_ARTIFACT_ROOT", str(legacy))
    assert resolve_archive_root() == legacy.resolve()
    monkeypatch.setenv("RETRO_ROOT", str(env_root))
    assert resolve_archive_root() == env_root.resolve()
    assert resolve_archive_root(explicit) == explicit.resolve()


def test_invalid_config_is_not_silently_ignored():
    config_path().parent.mkdir(parents=True)
    config_path().write_text("{not-json", encoding="utf-8")

    try:
        load_config()
    except ValueError as exc:
        assert "Invalid Retro config" in str(exc)
    else:
        raise AssertionError("expected invalid config to raise")


def test_saved_config_is_valid_json(tmp_path: Path):
    save_config(
        RetroConfig(
            archive_root=str(tmp_path / "archive"),
            dashboard_dir=str(tmp_path / "dashboard"),
        )
    )

    raw = json.loads(config_path().read_text(encoding="utf-8"))
    assert raw["sync_interval_seconds"] == 900
