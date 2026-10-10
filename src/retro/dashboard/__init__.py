"""Static dashboard over the session catalog and the signal store.

Public entry points:

``build_and_publish(layout, output_dir, mode="auto") -> Path``
    Build into a staging directory and swap it into place atomically.
``build_dashboard(layout, output_dir, mode="auto", publish=True) -> BuildReport``
    Same, but returns the full build report (used by the CLI).
``run_terminal_dashboard(layout, ...)``
    Interactive terminal view over the same inputs.
"""
from __future__ import annotations

from .build import BUILD_MODES, BuildReport, build
from .publish import build_and_publish, build_dashboard, publish_generation
from .terminal import run_terminal_dashboard

__all__ = [
    "BUILD_MODES",
    "BuildReport",
    "build",
    "build_and_publish",
    "build_dashboard",
    "publish_generation",
    "run_terminal_dashboard",
]
