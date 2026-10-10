from __future__ import annotations

from pathlib import Path

from retro.archive_health import duplicate_volume_mounts, python_app_bundle


def test_duplicate_volume_mounts_reports_numbered_mounted_siblings(
    monkeypatch,
    tmp_path: Path,
):
    volume = tmp_path / "SSD"
    duplicate = tmp_path / "SSD 1"
    unmounted = tmp_path / "SSD 2"
    unrelated = tmp_path / "Other 1"
    for path in (volume, duplicate, unmounted, unrelated):
        path.mkdir()
    monkeypatch.setattr(
        "retro.archive_health.os.path.ismount",
        lambda path: Path(path) == duplicate,
    )

    assert duplicate_volume_mounts(volume) == [duplicate]


def test_python_app_bundle_uses_enclosing_bundle(monkeypatch):
    executable = "/Applications/Developer/Python.app/Contents/MacOS/Python"
    monkeypatch.setattr("retro.archive_health.sys._base_executable", executable)
    monkeypatch.setattr("retro.archive_health.sys.executable", executable)

    assert python_app_bundle() == Path("/Applications/Developer/Python.app")
