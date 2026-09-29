"""
Optional, opt-in resolution tracing.

The objects defined here are produced only when a caller explicitly asks
for them (via `Resolver.lookup_traced`). A normal `Resolver.lookup` never
allocates any of this module's types, and the tracing machinery is stored
in a `~contextvars.ContextVar`, so concurrent lookups sharing a single
`Resolver` can never observe one another's traces.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextvars import ContextVar, Token
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from attrs import field

from referencing._attrs import frozen

if TYPE_CHECKING:
    from referencing.typing import URI


class Action(StrEnum):
    """
    The kind of step a single `TraceEvent` represents.
    """

    #: A reference string was combined with the current base URI.
    resolve = "resolve"

    #: Uncrawled known resources were walked to discover subresources.
    crawl = "crawl"

    #: A resource was found directly in an (already crawled) registry.
    hit = "hit"

    #: A resource was found after crawling known resources.
    crawl_hit = "crawl_hit"

    #: A resource was provided by the registry's retrieve callable.
    retrieve = "retrieve"

    #: A retrieve callable failed to produce a resource.
    retrieve_failed = "retrieve_failed"

    #: One JSON Pointer segment was followed within a resource.
    pointer = "pointer"

    #: Entering a subresource updated the base URI.
    subresource = "subresource"

    #: A plain-name (or dynamic) fragment anchor was located.
    anchor = "anchor"

    #: One URI in the dynamic scope was examined for a dynamic anchor.
    dynamic_scope = "dynamic_scope"


@frozen
class TraceEvent:
    """
    A single, verifiable step in the resolution of a reference.

    Events deliberately contain only identifiers (URIs, anchor names,
    pointer segments) and never full resource contents.
    """

    #: The raw input URI for this step (before normalization).
    input_uri: URI | None

    #: The URI after normalization (URI joining, fragment splitting, ...).
    uri: URI | None

    #: What kind of step this was.
    action: Action

    #: The identifier of the resource or anchor which was hit, if any.
    target: URI | None

    #: Whether a user-provided retrieve callable was invoked at this step.
    retrieved: bool = False

    #: For pointer events, the segment as written in the reference.
    raw_segment: str | None = None

    #: For pointer events, the decoded value the segment resolved to.
    decoded_segment: int | str | None = None

    #: Whether the step succeeded in reaching its target.
    found: bool = True

    #: For dynamic scope events, whether the found anchor was itself dynamic.
    dynamic: bool = False


@frozen
class Trace(Sequence[TraceEvent]):
    """
    An immutable sequence of `TraceEvent` steps recorded during one lookup.
    """

    _events: tuple[TraceEvent, ...] = field(default=(), alias="events")

    def __iter__(self) -> Iterator[TraceEvent]:
        return iter(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __getitem__(self, index: int | slice) -> Any:
        return self._events[index]

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        """
        Return all recorded events as a tuple.
        """
        return self._events


@frozen
class _Collector:
    """
    The per-lookup mutable sink into which events are recorded.

    Only a single `_Collector` is allocated, and only for an explicitly
    traced lookup. It is never shared across lookups or threads.
    """

    events: list[TraceEvent] = field(factory=list[TraceEvent])

    def trace(self) -> Trace:
        return Trace(events=tuple(self.events))


#: The collector active for the current context (thread / asyncio task).
_CURRENT: ContextVar[_Collector | None] = ContextVar(
    "_referencing_trace_collector",
    default=None,
)


def begin() -> Token[_Collector | None]:
    """
    Activate a fresh collector for one traced lookup.
    """
    return _CURRENT.set(_Collector())


def end(token: Token[_Collector | None]) -> None:
    """
    Deactivate the collector installed by `begin`.
    """
    _CURRENT.reset(token)


def collect() -> Trace | None:
    """
    Snapshot the active collector as an immutable trace, if one is active.
    """
    collector = _CURRENT.get()
    if collector is None:
        return None
    return collector.trace()


def required() -> Trace:
    """
    Snapshot the active collector, assuming a traced lookup is in progress.
    """
    collector = _CURRENT.get()
    if collector is None:  # pragma: no cover
        message = "No traced lookup is active."
        raise RuntimeError(message)
    return collector.trace()


def record(
    action: Action,
    *,
    input_uri: URI | None = None,
    uri: URI | None = None,
    target: URI | None = None,
    retrieved: bool = False,
    raw_segment: str | None = None,
    decoded_segment: int | str | None = None,
    found: bool = True,
    dynamic: bool = False,
) -> None:
    """
    Record an event into the active collector, if any.

    Does nothing at all (beyond one `ContextVar.get`) during a normal,
    untraced lookup.
    """
    collector = _CURRENT.get()
    if collector is not None:
        collector.events.append(
            TraceEvent(
                input_uri=input_uri,
                uri=uri,
                action=action,
                target=target,
                retrieved=retrieved,
                raw_segment=raw_segment,
                decoded_segment=decoded_segment,
                found=found,
                dynamic=dynamic,
            ),
        )
