"""Signals: comparable, evidence-linked measurements of captured rollouts.

A signal answers one question about one session ("did it succeed?", "what did
it cost?", "where did it get stuck?") in a form that aggregates across
thousands of sessions. Mining, by contrast, extracts memories to inject into
future prompts.

The layer is three pieces:

* `profile.py` scans a session's events exactly once into a `SessionProfile`
  of facts (plus `usage.py`/`pricing.py` for tokens and money).
* `definitions.py` registers pure functions from that profile to values, each
  carrying id, title, category, kind, unit, polarity, method and version.
* `store.py` keeps `signals/definitions.json` and `signals/sessions.jsonl` in
  sync incrementally, keyed on the catalog fingerprint and the signal-set
  digest.

See `specs/rollout_signals_spec.md`.
"""

from . import definitions as _definitions  # noqa: F401  (registers the catalogue)
from .analyze import analyze_sessions, generate_report, render_console_report, session_tips
from .classify import OUTCOME_CATEGORIES, WORK_KINDS, classify_outcome, classify_work_kind
from .commands import VALIDATION_CATEGORIES, classify_command, get_base_command
from .pricing import PricingMap, Rates, default_pricing, normalize_model_id
from .profile import SessionProfile, build_profile, load_profile
from .registry import (
    CATEGORIES,
    KINDS,
    METHODS,
    POLARITIES,
    REGISTRY,
    SignalDef,
    SignalValue,
    definitions,
    definitions_payload,
    definitions_version,
    evaluate,
    lookup,
    register,
)
from .sources import (
    FileSessionRecord,
    NormalizedSource,
    SessionRecordLike,
    SessionSource,
    stat_fingerprint,
)
from .store import (
    SignalStore,
    SignalUpdate,
    load_signal_store,
    record_from_profile,
    signals_dir,
    update_signals,
)
from .summary import category_share, compare, group_by, rate, summarize, weekly_trend
from .usage import UsageStats, extract_usage

__all__ = [
    "CATEGORIES",
    "FileSessionRecord",
    "KINDS",
    "METHODS",
    "NormalizedSource",
    "OUTCOME_CATEGORIES",
    "POLARITIES",
    "PricingMap",
    "REGISTRY",
    "Rates",
    "SessionProfile",
    "SessionRecordLike",
    "SessionSource",
    "SignalDef",
    "SignalStore",
    "SignalUpdate",
    "SignalValue",
    "UsageStats",
    "VALIDATION_CATEGORIES",
    "WORK_KINDS",
    "analyze_sessions",
    "build_profile",
    "category_share",
    "classify_command",
    "classify_outcome",
    "classify_work_kind",
    "compare",
    "default_pricing",
    "definitions",
    "definitions_payload",
    "definitions_version",
    "evaluate",
    "extract_usage",
    "generate_report",
    "get_base_command",
    "group_by",
    "load_profile",
    "load_signal_store",
    "lookup",
    "normalize_model_id",
    "rate",
    "record_from_profile",
    "register",
    "render_console_report",
    "session_tips",
    "signals_dir",
    "stat_fingerprint",
    "summarize",
    "update_signals",
    "weekly_trend",
]
