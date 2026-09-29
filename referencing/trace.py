"""
Opt-in tracing of reference resolution.

Call `referencing.Resolver.lookup_traced` to resolve a reference while also
collecting an immutable `Trace` describing each step taken.
"""

from __future__ import annotations

from referencing._core import TracedResolved
from referencing._trace import Action, Trace, TraceEvent

__all__ = ["Action", "Trace", "TraceEvent", "TracedResolved"]
