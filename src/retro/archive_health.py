"""Archive volume and macOS privacy diagnostics."""
from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArchiveAccess:
    root: str
    volume: str | None
    external_volume: bool
    mounted: bool
    accessible: bool
    message: str | None = None
    python_bundle: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchiveVolumeUnavailable(RuntimeError):
    def __init__(self, volume: Path):
        self.volume = volume
        super().__init__(unmounted_archive_message(volume))


def external_volume_root(path: Path) -> Path | None:
    absolute = Path(os.path.abspath(str(path.expanduser())))
    parts = absolute.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return Path("/", "Volumes", parts[2])
    return None


def archive_access(path: Path) -> ArchiveAccess:
    root = Path(os.path.abspath(str(path.expanduser())))
    volume = external_volume_root(root)
    if volume is not None and not os.path.ismount(str(volume)):
        return ArchiveAccess(
            root=str(root),
            volume=str(volume),
            external_volume=True,
            mounted=False,
            accessible=False,
            message=unmounted_archive_message(volume),
        )

    probe = root
    while True:
        try:
            stat = os.stat(probe)
        except FileNotFoundError:
            parent = probe.parent
            if parent == probe:
                break
            probe = parent
            continue
        except OSError as exc:
            if is_external_volume_permission_error(exc, root):
                bundle = python_app_bundle()
                return ArchiveAccess(
                    root=str(root),
                    volume=str(volume) if volume is not None else None,
                    external_volume=volume is not None,
                    mounted=True,
                    accessible=False,
                    message=macos_archive_permission_message(root, bundle),
                    python_bundle=str(bundle),
                )
            return ArchiveAccess(
                root=str(root),
                volume=str(volume) if volume is not None else None,
                external_volume=volume is not None,
                mounted=True,
                accessible=False,
                message=f"Cannot access archive path {root}: {type(exc).__name__}: {exc}",
            )
        else:
            if probe == root and stat.st_mode:
                try:
                    os.listdir(probe)
                except NotADirectoryError:
                    pass
                except OSError as exc:
                    if is_external_volume_permission_error(exc, root):
                        bundle = python_app_bundle()
                        return ArchiveAccess(
                            root=str(root),
                            volume=str(volume) if volume is not None else None,
                            external_volume=volume is not None,
                            mounted=True,
                            accessible=False,
                            message=macos_archive_permission_message(root, bundle),
                            python_bundle=str(bundle),
                        )
                    return ArchiveAccess(
                        root=str(root),
                        volume=str(volume) if volume is not None else None,
                        external_volume=volume is not None,
                        mounted=True,
                        accessible=False,
                        message=(
                            f"Cannot access archive path {root}: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
            break

    return ArchiveAccess(
        root=str(root),
        volume=str(volume) if volume is not None else None,
        external_volume=volume is not None,
        mounted=True,
        accessible=True,
    )


def scheduled_archive_access(path: Path) -> dict[str, Any]:
    current = archive_access(path).to_dict()
    current["evidence"] = "current process probe"
    if not current["accessible"] or not current["external_volume"]:
        return current
    runs_dir = path / "sync" / "runs"
    try:
        reports = sorted(
            runs_dir.glob("*.json"),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return current
    for report_path in reports:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict) or not report.get("scheduled"):
            continue
        current["evidence"] = str(report_path)
        serialized = json.dumps(report, ensure_ascii=False)
        if (
            "Operation not permitted" in serialized
            or "PermissionError: [Errno 1]" in serialized
        ):
            bundle = python_app_bundle()
            current["accessible"] = False
            current["message"] = macos_archive_permission_message(path, bundle)
            current["python_bundle"] = str(bundle)
        return current
    return current


def require_archive_volume(path: Path) -> None:
    volume = external_volume_root(path)
    if volume is not None and not os.path.ismount(str(volume)):
        raise ArchiveVolumeUnavailable(volume)


def duplicate_volume_mounts(volume: Path) -> list[Path]:
    parent = volume.parent
    try:
        candidates = list(parent.iterdir())
    except OSError:
        return []
    prefix = f"{volume.name} "
    return sorted(
        candidate
        for candidate in candidates
        if candidate.name.startswith(prefix)
        and candidate.name[len(prefix) :].isdigit()
        and os.path.ismount(str(candidate))
    )


def is_external_volume_permission_error(
    exc: BaseException,
    path: Path | None = None,
) -> bool:
    if sys.platform != "darwin" or not isinstance(exc, OSError):
        return False
    if exc.errno not in {errno.EPERM, errno.EACCES}:
        return False
    candidate = Path(exc.filename) if isinstance(exc.filename, str) else path
    return candidate is not None and external_volume_root(candidate) is not None


def python_app_bundle() -> Path:
    executables = [
        Path(value)
        for value in (
            getattr(sys, "_base_executable", None),
            sys.executable,
        )
        if isinstance(value, str) and value
    ]
    candidates: list[Path] = []
    for executable in executables:
        candidates.append(executable)
        try:
            candidates.append(executable.resolve())
        except OSError:
            pass
    for executable in candidates:
        for parent in (executable, *executable.parents):
            if parent.name == "Python.app":
                return parent
        parts = executable.parts
        for index, part in enumerate(parts):
            if part.endswith(".framework") and index + 2 < len(parts):
                version_root = Path(*parts[: index + 3])
                bundle = version_root / "Resources" / "Python.app"
                if bundle.is_dir():
                    return bundle
    return candidates[-1] if candidates else Path(sys.executable)


def macos_archive_permission_message(path: Path, bundle: Path | None = None) -> str:
    target = bundle or python_app_bundle()
    return (
        f"macOS denied Retro access to the external archive at {path} "
        f"(Operation not permitted). Grant Full Disk Access to {target} in "
        "System Settings → Privacy & Security → Full Disk Access, or allow "
        "Removable Volumes under Files and Folders, then run Retro again."
    )


def unmounted_archive_message(volume: Path) -> str:
    return (
        f"Archive volume is not mounted: {volume}. Connect the volume before "
        f"syncing. Retro skipped this run and did not create {volume}."
    )


def describe_archive_error(exc: BaseException, path: Path) -> str:
    if is_external_volume_permission_error(exc, path):
        return macos_archive_permission_message(path)
    return f"{type(exc).__name__}: {exc}"
