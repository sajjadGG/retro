#!/usr/bin/env python3
"""Back-compat shim: the builder now lives in the package as retro.dashboard_build.

Prefer `retro dashboard build` or `python -m retro.dashboard_build`.
"""
from retro.dashboard_build import main

if __name__ == "__main__":
    main()
