"""Host-specific importers.

Each importer takes a host-native session/thread reference and emits:
  - an immutable copy of the source files in rollout-memory/raw/<host>/<id>/
  - a normalized event stream at rollout-memory/normalized/<host>/<id>.events.jsonl
"""
from .base import Importer, ImportResult  # noqa: F401
