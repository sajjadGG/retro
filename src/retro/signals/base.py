"""Core signal abstractions: SessionContext, SignalReading, Signal, registry."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..schema import Host, NormalizedEvent, read_events
from ..utils import event_text as _shared_event_text
from ..utils import iter_messages as _shared_iter_messages

SignalGroup = Literal["activity", "outcome", "cost", "risk"]
SignalKind = Literal["numeric", "boolean", "categorical", "text"]
SignalMethod = Literal["heuristic", "regex", "external", "llm_judge"]


@dataclass
class SessionContext:
    """All the inputs a signal can use to evaluate a session."""

    host: Host
    session_id: str
    events: Sequence[NormalizedEvent]
    raw_dir: Path
    raw_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cwd(self) -> str | None:
        for key in ("cwd", "current_working_directory"):
            value = self.raw_meta.get(key)
            if isinstance(value, str) and value:
                return value
        for ev in self.events:
            payload = ev.payload or {}
            value = payload.get("cwd")
            if isinstance(value, str) and value:
                return value
        return None


@dataclass
class SignalReading:
    signal: str
    group: SignalGroup
    kind: SignalKind
    method: SignalMethod
    session_id: str
    host: Host
    value: Any
    unit: str | None = None
    confidence: float = 1.0
    evidence_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Signal:
    name: str
    group: SignalGroup
    kind: SignalKind
    method: SignalMethod
    unit: str | None
    description: str
    fn: Callable[[SessionContext], SignalReading | None | Iterable[SignalReading]]

    def __call__(self, ctx: SessionContext) -> list[SignalReading]:
        result = self.fn(ctx)
        if result is None:
            return []
        if isinstance(result, SignalReading):
            return [result]
        return list(result)


REGISTRY: dict[str, Signal] = {}


def register(
    name: str,
    *,
    group: SignalGroup,
    kind: SignalKind,
    method: SignalMethod = "heuristic",
    unit: str | None = None,
    description: str = "",
) -> Callable[[Callable], Signal]:
    """Decorator to register a signal in the global REGISTRY.

    The decorated function should accept a `SessionContext` and return a
    `SignalReading`, `None`, or an iterable of readings.
    """

    def wrap(fn: Callable) -> Signal:
        if name in REGISTRY:
            raise ValueError(f"signal {name!r} already registered")
        sig = Signal(
            name=name,
            group=group,
            kind=kind,
            method=method,
            unit=unit,
            description=description.strip(),
            fn=fn,
        )
        REGISTRY[name] = sig
        return sig

    return wrap


# --- helpers signals reuse ---------------------------------------------------


def load_events(normalized_path: Path) -> list[NormalizedEvent]:
    return list(read_events(normalized_path))


def iter_messages(events: Sequence[NormalizedEvent], actor: str) -> Iterator[NormalizedEvent]:
    return _shared_iter_messages(events, actor)


def event_text(ev: NormalizedEvent) -> str:
    return _shared_event_text(ev)


def reading(
    ctx: SessionContext,
    signal: Signal,
    value: Any,
    *,
    evidence: Iterable[NormalizedEvent] | Iterable[str] = (),
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> SignalReading:
    refs: list[str] = []
    for item in evidence:
        if isinstance(item, NormalizedEvent):
            refs.append(item.event_id)
        elif isinstance(item, str):
            refs.append(item)
    return SignalReading(
        signal=signal.name,
        group=signal.group,
        kind=signal.kind,
        method=signal.method,
        session_id=ctx.session_id,
        host=ctx.host,
        value=value,
        unit=signal.unit,
        confidence=confidence,
        evidence_refs=refs[:8],
        metadata=metadata or {},
    )
