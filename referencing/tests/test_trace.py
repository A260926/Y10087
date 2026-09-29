"""
Tests for `referencing.tracing`, the opt-in resolution tracing API.
"""

from threading import Barrier, Thread

import pytest

from referencing import Registry, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.tracing import Action


def blow_up(uri):
    """
    A retriever which always fails with an arbitrary error.
    """
    raise RuntimeError("This resource cannot be retrieved!")


def no_such(uri):
    """
    A retriever which (correctly) says nothing exists anywhere.
    """
    raise exceptions.NoSuchResource(ref=uri)


def actions_of(trace):
    return [event.action for event in trace]


class TestTracedLookup:
    def test_demo(self):
        """
        A traced lookup shows each step taken to reach the result.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root.json",
                "$defs": {
                    "child": {
                        "$id": "child.json",
                        "properties": {"foo": {"const": 42}},
                    },
                },
            },
        )
        registry = Registry().with_resource(
            "http://example.com/root.json",
            root,
        )
        resolver = registry.resolver(base_uri="http://example.com/")

        traced = resolver.lookup_traced(
            "root.json#/$defs/child/properties/foo",
        )

        print()  # noqa: T201
        for event in traced.trace:
            print(f"{event.action.value:<13} {event.uri}")  # noqa: T201
            print(f"{'':<13} -> {event.normalized_uri}")  # noqa: T201

        assert traced.resolved.contents == {"const": 42}
        assert actions_of(traced.trace) == [
            Action.JOIN,
            Action.HIT,
            Action.POINTER,
            Action.POINTER,
            Action.BASE_URI,
            Action.POINTER,
            Action.POINTER,
        ]
        join = traced.trace[0]
        assert join.uri == "root.json#/$defs/child/properties/foo"
        assert (
            join.normalized_uri
            == "http://example.com/root.json#/$defs/child/properties/foo"
        )

    def test_local_hit(self):
        """
        A resource already in the registry is found without crawling.
        """
        root = DRAFT202012.create_resource(
            {"$id": "http://example.com/root", "type": "object"},
        )
        registry = Registry().with_resource("http://example.com/root", root)

        traced = registry.resolver().lookup_traced("http://example.com/root")

        assert traced.resolved.contents is root.contents
        assert actions_of(traced.trace) == [Action.JOIN, Action.HIT]
        hit = traced.trace[-1]
        assert hit.hit == "http://example.com/root"
        assert not hit.retrieved

    def test_hit_after_crawl(self):
        """
        A subresource is found only after crawling its parent resource.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root",
                "$defs": {"child": {"$id": "child", "type": "string"}},
            },
        )
        registry = Registry().with_resource("http://example.com/root", root)

        traced = registry.resolver().lookup_traced("http://example.com/child")

        assert traced.resolved.contents == {"$id": "child", "type": "string"}
        assert actions_of(traced.trace) == [Action.JOIN, Action.CRAWL]
        crawl = traced.trace[-1]
        assert crawl.hit == "http://example.com/child"
        assert not crawl.retrieved

    def test_retrieve_success(self):
        """
        An unknown resource is retrieved, exactly once, via `retrieve`.
        """
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return DRAFT202012.create_resource({"$id": uri, "type": "null"})

        registry = Registry(retrieve=retrieve)

        traced = registry.resolver().lookup_traced("http://example.com/remote")

        assert calls == ["http://example.com/remote"]
        assert traced.resolved.contents == {
            "$id": "http://example.com/remote",
            "type": "null",
        }
        assert actions_of(traced.trace) == [
            Action.JOIN,
            Action.CRAWL,
            Action.RETRIEVE,
        ]
        retrieved = traced.trace[-1]
        assert retrieved.retrieved
        assert retrieved.hit == "http://example.com/remote"

    def test_retrieve_failure(self):
        """
        A failing retriever surfaces the trace up to the failed retrieval.
        """
        registry = Registry(retrieve=blow_up)

        with pytest.raises(exceptions.Unresolvable) as excinfo:
            registry.resolver().lookup_traced("http://example.com/missing")

        error = excinfo.value
        assert isinstance(error.__cause__, exceptions.Unretrievable)
        assert isinstance(error.__cause__.__cause__, RuntimeError)

        trace = error.trace
        assert trace is not None
        assert actions_of(trace) == [
            Action.JOIN,
            Action.CRAWL,
            Action.RETRIEVE,
        ]
        retrieved = trace[-1]
        assert retrieved.retrieved
        assert retrieved.hit is None

    def test_retrieve_no_such_resource(self):
        """
        A `NoSuchResource` from `retrieve` keeps the existing exception shape.
        """
        registry = Registry(retrieve=no_such)

        with pytest.raises(exceptions.Unresolvable) as excinfo:
            registry.resolver().lookup_traced("http://example.com/missing")

        error = excinfo.value
        assert error.ref == "http://example.com/missing"
        assert error.__cause__ is None
        assert actions_of(error.trace) == [
            Action.JOIN,
            Action.CRAWL,
            Action.RETRIEVE,
        ]

    def test_pointer_escapes(self):
        """
        Pointer steps preserve both the raw and decoded forms of segments.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root",
                "a/b": {"~key": 42},
            },
        )
        registry = Registry().with_resource("http://example.com/root", root)
        resolver = registry.resolver(base_uri="http://example.com/root")

        traced = resolver.lookup_traced("#/a~1b/~0key")

        assert traced.resolved.contents == 42
        pointers = [
            event for event in traced.trace if event.action is Action.POINTER
        ]
        assert [(each.segment, each.decoded) for each in pointers] == [
            ("a~1b", "a/b"),
            ("~0key", "~key"),
        ]

    def test_pointer_to_nowhere(self):
        """
        A failing pointer step is the last event of the trace.
        """
        root = DRAFT202012.create_resource(
            {"$id": "http://example.com/root", "a/b": {}},
        )
        registry = Registry().with_resource("http://example.com/root", root)
        resolver = registry.resolver(base_uri="http://example.com/root")

        with pytest.raises(exceptions.PointerToNowhere) as excinfo:
            resolver.lookup_traced("#/a~1b/nope")

        trace = excinfo.value.trace
        assert trace is not None
        last = trace[-1]
        assert last.action is Action.POINTER
        assert last.segment == "nope"
        assert last.decoded == "nope"
        assert last.hit is None

    def test_plain_anchor(self):
        """
        A plain-name anchor is recorded with the anchor it hit.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root",
                "$defs": {"foo": {"$anchor": "myAnchor", "type": "integer"}},
            },
        )
        registry = Registry().with_resource("http://example.com/root", root)

        traced = registry.resolver().lookup_traced(
            "http://example.com/root#myAnchor",
        )

        assert traced.resolved.contents == {
            "$anchor": "myAnchor",
            "type": "integer",
        }
        assert actions_of(traced.trace) == [
            Action.JOIN,
            Action.HIT,
            Action.ANCHOR,
        ]
        anchor = traced.trace[-1]
        assert anchor.hit == "myAnchor"
        assert anchor.normalized_uri == "http://example.com/root#myAnchor"

    def test_no_such_anchor(self):
        """
        A missing anchor surfaces the trace up to the failed anchor lookup.
        """
        root = DRAFT202012.create_resource({"$id": "http://example.com/root"})
        registry = Registry().with_resource("http://example.com/root", root)

        with pytest.raises(exceptions.NoSuchAnchor) as excinfo:
            registry.resolver().lookup_traced("http://example.com/root#nope")

        trace = excinfo.value.trace
        assert trace is not None
        assert actions_of(trace) == [Action.JOIN, Action.HIT, Action.ANCHOR]
        assert trace[-1].hit is None

    def test_base_uri_update_is_visible(self):
        """
        Entering a subresource with its own ID records the base URI change.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root",
                "$defs": {"child": {"$id": "child", "foo": "bar"}},
            },
        )
        registry = Registry().with_resource("http://example.com/root", root)
        resolver = registry.resolver(base_uri="http://example.com/root")

        traced = resolver.lookup_traced("#/$defs/child/foo")

        assert traced.resolved.contents == "bar"
        (base_uri,) = [
            event for event in traced.trace if event.action is Action.BASE_URI
        ]
        assert base_uri.uri == "http://example.com/root"
        assert base_uri.normalized_uri == "http://example.com/child"
        assert base_uri.hit == "child"
        assert traced.resolved.resolver._base_uri == "http://example.com/child"

    def test_dynamic_anchor_records_dynamic_scope_in_order(self):
        """
        Dynamic anchor resolution records each dynamic scope URI consulted.
        """
        true = DRAFT202012.create_resource(True)
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com",
                "$dynamicAnchor": "fooAnchor",
                "$defs": {
                    "foo": {
                        "$id": "foo",
                        "$dynamicAnchor": "fooAnchor",
                        "$defs": {
                            "bar": True,
                            "baz": {"$dynamicAnchor": "fooAnchor"},
                        },
                    },
                },
            },
        )
        resolver = (
            Registry()
            .with_resources(
                [
                    ("http://example.com", root),
                    ("http://example.com/foo/", true),
                    ("http://example.com/foo/bar", root),
                ],
            )
            .resolver()
        )

        first = resolver.lookup_traced("http://example.com")
        second = first.resolved.resolver.lookup_traced("foo/")
        third = second.resolved.resolver.lookup_traced("bar")
        fourth = third.resolved.resolver.lookup_traced("#fooAnchor")

        assert fourth.resolved.contents == root.contents
        dynamic = [
            event
            for event in fourth.trace
            if event.action is Action.DYNAMIC_SCOPE
        ]
        assert [each.uri for each in dynamic] == [
            "http://example.com/foo/",
            "http://example.com",
        ]
        assert [each.hit for each in dynamic] == [None, "fooAnchor"]

    def test_repeated_lookup(self):
        """
        Each lookup retrieves at most once, and evolved resolvers reuse it.
        """
        calls = []

        def retrieve(uri):
            calls.append(uri)
            return DRAFT202012.create_resource({"$id": uri, "type": "integer"})

        resolver = Registry(retrieve=retrieve).resolver()

        first = resolver.lookup_traced("http://example.com/remote")
        assert calls == ["http://example.com/remote"]

        second = first.resolved.resolver.lookup_traced(
            "http://example.com/remote",
        )
        assert calls == ["http://example.com/remote"]
        assert actions_of(second.trace) == [Action.JOIN, Action.HIT]

        # The original resolver (and registry) is unchanged, so starting
        # from it again retrieves again -- exactly once for that lookup.
        again = resolver.lookup_traced("http://example.com/remote")
        assert calls == ["http://example.com/remote"] * 2
        assert actions_of(again.trace) == [
            Action.JOIN,
            Action.CRAWL,
            Action.RETRIEVE,
        ]

    def test_concurrent_lookups_have_isolated_traces(self):
        """
        Two threads sharing a resolver get independent, untangled traces.
        """
        root = DRAFT202012.create_resource(
            {
                "$id": "http://example.com/root",
                "a": {"aaa": 1},
                "b": {"bbb": 2},
            },
        )
        registry = Registry().with_resource("http://example.com/root", root)
        resolver = registry.resolver(base_uri="http://example.com/root")
        barrier = Barrier(2)
        collected = {"a": [], "b": []}

        def work(key, ref):
            barrier.wait()
            for _ in range(100):
                traced = resolver.lookup_traced(ref)
                collected[key].append(traced.trace)

        threads = [
            Thread(target=work, args=("a", "#/a/aaa")),
            Thread(target=work, args=("b", "#/b/bbb")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        cases = [("a", "#/a/aaa", "aaa"), ("b", "#/b/bbb", "bbb")]
        for key, ref, leaf in cases:
            assert len(collected[key]) == 100
            for trace in collected[key]:
                assert trace[0].action is Action.JOIN
                assert trace[0].uri == ref
                segments = [
                    event.decoded
                    for event in trace
                    if event.action is Action.POINTER
                ]
                assert segments == [key, leaf]

    def test_trace_is_immutable(self):
        """
        Traces and their events cannot be mutated or hold registry state.
        """
        root = DRAFT202012.create_resource({"$id": "http://example.com/root"})
        registry = Registry().with_resource("http://example.com/root", root)

        traced = registry.resolver().lookup_traced("http://example.com/root")

        assert isinstance(traced.trace.events, tuple)
        with pytest.raises(AttributeError):
            traced.trace.events = ()
        with pytest.raises(AttributeError):
            traced.trace[0].uri = "http://example.com/elsewhere"


class TestPlainLookupIsUnchanged:
    def test_lookup_returns_plain_resolved(self):
        """
        `lookup` still returns a `Resolved` and allocates no trace.
        """
        root = DRAFT202012.create_resource({"$id": "http://example.com/root"})
        registry = Registry().with_resource("http://example.com/root", root)

        resolved = registry.resolver().lookup("http://example.com/root")

        assert resolved.contents is root.contents
        assert not hasattr(resolved, "trace")

    def test_lookup_exceptions_have_no_trace(self):
        """
        Exceptions from untraced lookups expose `trace` as `None`.
        """
        registry = Registry(retrieve=blow_up)

        with pytest.raises(exceptions.Unresolvable) as excinfo:
            registry.resolver().lookup("http://example.com/missing")

        assert excinfo.value.trace is None

    def test_lookup_does_not_record_events(self):
        """
        No tracer is active while a plain `lookup` runs.
        """
        from referencing import tracing

        seen = []

        def retrieve(uri):
            seen.append(tracing._current.get())
            return DRAFT202012.create_resource({"$id": uri})

        registry = Registry(retrieve=retrieve)
        registry.resolver().lookup("http://example.com/remote")

        assert seen == [None]
