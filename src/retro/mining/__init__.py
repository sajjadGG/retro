"""Mining: turn captured rollouts into prompt-time memory candidates.

This package replaces the original single-file `mining.py`. The public API
that `cli.py` depends on (`mine_file`, `write_mining_artifacts`,
`render_prompt_block`, `MiningResult`, `MemoryCandidate`) is preserved.

Methods are registered via `@register_method` in `methods/<name>.py` and
filters via `@register_filter` in `filters/<name>.py`. Both kinds of plugins
self-register on import.

See:
  - specs/rollout_mining_methods.md
  - specs/full_rollout_capture_feature_spec.md  (Mining Layer)
"""
from .base import (  # noqa: F401
    METHOD_REGISTRY,
    FILTER_REGISTRY,
    MemoryCandidate,
    MemoryKind,
    MemoryRisk,
    MiningContext,
    MiningResult,
    register_filter,
    register_method,
)
from .render import (  # noqa: F401
    mine_file,
    mine_with_method,
    render_prompt_block,
    write_mining_artifacts,
)

# Import method + filter modules so their @register decorators fire.
from .methods import reme_refine, skill_pro, memp_procedural  # noqa: F401
from .filters import risk_aware  # noqa: F401

DEFAULT_METHOD = "reme_refine_poc"
