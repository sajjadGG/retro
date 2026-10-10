from __future__ import annotations

import json
from pathlib import Path

import pytest

from retro.config import (
    RetroConfig,
    config_path,
    load_config,
    resolve_archive_root,
    resolve_dashboard_dir,
    save_config,
    update_config,
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
    assert raw["derived_interval_seconds"] == 21600


def test_catalog_config_validates_and_preserves_unknown_keys(tmp_path: Path):
    config_path().parent.mkdir(parents=True)
    config_path().write_text(
        json.dumps(
            {
                "archive_root": str(tmp_path / "archive"),
                "dashboard_dir": str(tmp_path / "dashboard"),
                "exclude_tags": ["ignore", "private"],
                "disabled_rules": ["builtin.automated"],
                "rules": [
                    {
                        "id": "eval",
                        "description": "evaluation sessions",
                        "enabled": True,
                        "match": {"client": ["copilot-cli"], "interactive": False},
                        "tags": ["ignore"],
                    }
                ],
                "projects": {
                    "aliases": {"path:/old": "github.com/example/repo"},
                    "paths": [{"glob": "~/Dev/repo/**", "project": "github.com/example/repo"}],
                    "names": {"github.com/example/repo": "Example"},
                },
                "future_setting": {"preserve": True},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_config()
    updated = update_config(sync_interval_seconds=1200)
    raw = json.loads(config_path().read_text(encoding="utf-8"))

    assert loaded.exclude_tags == ["ignore", "private"]
    assert updated.rules[0]["id"] == "eval"
    assert raw["future_setting"] == {"preserve": True}
    assert raw["projects"]["names"]["github.com/example/repo"] == "Example"


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("exclude_tags", ["bad tag"], "exclude_tags[0]"),
        ("disabled_rules", [""], "disabled_rules[0]"),
        ("rules", [{"id": "x", "match": {"title": "("}, "tags": ["ignore"]}], "regex"),
        (
            "rules",
            [{"id": "builtin.fake", "match": {}, "tags": ["ignore"]}],
            "reserved",
        ),
        ("projects", {"aliases": {"a": "b", "b": "a"}}, "cycle"),
    ],
)
def test_catalog_config_errors_are_clear(tmp_path: Path, key, value, message):
    payload = {
        "archive_root": str(tmp_path / "archive"),
        "dashboard_dir": str(tmp_path / "dashboard"),
        key: value,
    }
    config_path().parent.mkdir(parents=True)
    config_path().write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        load_config()
    assert message in str(error.value)
