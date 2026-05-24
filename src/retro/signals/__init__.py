"""Signals: evaluators that observe a captured rollout and emit comparable readings.

A signal answers a question like "did a commit happen during this session?" or
"how many user corrections did the assistant get?". Signals are separate from
the mining layer in `retro.mining`: mining produces *memories to
inject* into future prompts; signals produce *observations about how a rollout
went* that can be aggregated across many sessions.

See `specs/rollout_signals_spec.md`.
"""
# Import the catalogs so the decorators register them.
from . import (
    external,  # noqa: F401
    heuristics,  # noqa: F401
)
from .base import (  # noqa: F401
    REGISTRY,
    SessionContext,
    Signal,
    SignalReading,
    register,
)
from .runner import (  # noqa: F401
    aggregate_readings,
    iter_sessions,
    run_signals,
    write_signal_artifacts,
)
