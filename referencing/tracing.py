"""
Opt-in tracing of reference resolution.

This module provides a way to observe *how* a reference was resolved --
which URIs were joined, which resources were found in (or retrieved into)
the registry, which JSON pointer segments were followed, and which anchors
(static or dynamic) were consulted -- without changing the cost of normal
resolution.

Tracing is entirely opt-in: `referencing.Resolver.lookup` behaves exactly
as before and allocates nothing related to tracing. Callers who want a
verifiable trail use `referencing.Resolver.lookup_traced`, which returns
the usual resolution result along with an immutable `Trace`.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from typing import TYPE_CHECKING, Generic

from attrs import field

from referencing._attrs import frozen
from referencing.typing import URI, D

if TYPE_CHECKING:
    from collections.abc import Iterator

    from referencing._core import Resolved

__all__ = ["Action", "Trace", "TraceEvent", "Traced"]


class Action(Enum):
    """
    The kind of step taken while resolving a reference.
    """

    #: A reference was joined against the current base URI.
    JOIN = "join"

    #: A resource was found directly in the registry.
    HIT = "hit"

    #: The registry was crawled (and may or may not have found the resource).
    CRAWL = "crawl"

    #: The registry's retrieval function was invoked.
    RETRIEVE = "retrieve"

    #: A JSON pointer segment was followed within a resource.
    POINTER = "pointer"

    #: A plain (name) anchor was looked up within a resource.
    ANCHOR = "anchor"

    #: A URI in the dynamic scope was consulted for a dynamic anchor.
    DYNAMIC_SCOPE = "dynamic_scope"

    #: The base URI changed because a subresource with its own ID was entered.
    BASE_URI = "base_uri"


@frozen
class TraceEvent:
    """
    A single step taken while resolving a reference.

    Events deliberately contain only identifiers -- never full resource
    contents -- and never hold on to any registry internals.
    """

    #: The kind of step this event describes.
    action: Action

    #: The URI which was the input to this step, as written.
    uri: URI

    #: The normalized (e.g. joined, defragmented) form of the input URI.
    normalized_uri: URI

    #: The identifier of the resource or anchor this step hit, if any.
    hit: str | None = None

    #: Whether this step invoked the registry's retrieval function.
    retrieved: bool = False

    #: For pointer steps, the segment as written (escapes preserved).
    segment: str | None = None

    #: For pointer steps, the decoded segment which was indexed with.
    decoded: int | str | None = None


@frozen
class Trace:
    """
    An immutable, ordered record of the steps taken during a resolution.
    """

    #: The events, in the order they occurred.
    events: tuple[TraceEvent, ...] = field(converter=tuple)

    def __iter__(self) -> Iterator[TraceEvent]:
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __getitem__(self, index: int) -> TraceEvent:
        return self.events[index]


@frozen
class Traced(Generic[D]):
    """
    The result of `referencing.Resolver.lookup_traced`.

    Pairs the usual resolution result with the trace of how it was reached.
    """

    #: What the reference resolved to.
    resolved: Resolved[D]

    #: The steps taken to reach the result.
    trace: Trace


class _Tracer:
    """
    A mutable collector used internally while a single traced lookup runs.
    """

    __slots__ = ("events",)

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(self, action: Action, **kwargs: object) -> None:
        self.events.append(TraceEvent(action=action, **kwargs))  # type: ignore[call-arg]

    def trace(self) -> Trace:
        return Trace(events=self.events)


#: The tracer active for the current context (thread/task), if any.
_current: ContextVar[_Tracer | None] = ContextVar(
    "referencing_tracing",
    default=None,
)


def record(action: Action, **kwargs: object) -> None:
    """
    Record an event if (and only if) a traced lookup is currently running.
    """
    tracer = _current.get()
    if tracer is not None:
        tracer.record(action, **kwargs)


@contextmanager
def _recording() -> Iterator[_Tracer]:
    """
    Collect trace events for the duration of a single resolution.
    """
    tracer = _Tracer()
    token = _current.set(tracer)
    try:
        yield tracer
    finally:
        _current.reset(token)
