"""
Tests for the opt-in resolution tracing API.
"""

from __future__ import annotations

import threading

import pytest

from referencing import Registry, TracedResolved, _trace, exceptions
from referencing.jsonschema import DRAFT202012
from referencing.trace import Action, Trace


def make(contents):
    return DRAFT202012.create_resource(contents)


def test_demo_prints_fixed_action_order():
    """
    Print a single, stable trace (action name then normalized URI per step).
    """
    schema = make(
        {
            "$id": "http://example.com/base/",
            "allOf": [{"$id": "item.json", "$anchor": "target"}],
        },
    )

    registry = schema @ Registry()
    root = registry.resolver().lookup_traced("http://example.com/base/")
    entered = root.resolver.lookup_traced("#/allOf/0")
    traced = entered.resolver.lookup_traced("item.json#target")

    print("\nTrace steps (action uri):")  # noqa: T201
    for event in traced.trace:
        print(f"  {event.action.value} {event.uri}")  # noqa: T201

    assert [event.action.value for event in traced.trace] == [
        "resolve",
        "crawl",
        "crawl_hit",
        "anchor",
    ]


def names(traced):
    return [event.action.value for event in traced.trace]


def uris(traced):
    return [event.uri for event in traced.trace]


def test_traced_result_type_and_plain_result():
    resource = make({"$id": "urn:example:schema"})
    registry = resource @ Registry()
    resolved = registry.resolver().lookup_traced("urn:example:schema")

    assert isinstance(resolved, TracedResolved)
    assert resolved.contents == resource.contents
    assert resolved.resolved.contents == resource.contents
    assert resolved.resolved.resolver is resolved.resolver
    assert isinstance(resolved.trace, Trace)


def test_lookup_signature_unchanged():
    resource = make({})
    registry = Registry().with_resource("urn:example:schema", resource)
    resolved = registry.resolver().lookup("urn:example:schema")
    assert resolved.contents is resource.contents
    assert not hasattr(resolved, "trace")


def test_local_hit(monkeypatch):
    resource = make({"$id": "http://example.com/schema.json"})
    registry = resource @ Registry()

    def blow_up(uri):  # pragma: no cover
        raise AssertionError("must not retrieve")

    traced = (
        Registry(
            resources=registry._resources,
            retrieve=blow_up,
        )
        .resolver()
        .lookup_traced("http://example.com/schema.json")
    )

    assert traced.contents == resource.contents
    assert names(traced) == ["resolve", "hit"]
    resolve, hit = traced.trace
    assert resolve.input_uri == "http://example.com/schema.json"
    assert resolve.uri == "http://example.com/schema.json"
    assert resolve.target == "http://example.com/schema.json"
    assert hit.action is Action.hit
    assert hit.target == "http://example.com/schema.json"
    assert [event.retrieved for event in traced.trace] == [False, False]


def test_relative_reference_uri_combination():
    resource = make({"$id": "http://example.com/root/base.json"})
    registry = resource @ Registry()
    resolver = registry.resolver(base_uri="http://example.com/root/")
    traced = resolver.lookup_traced("base.json")

    assert names(traced) == ["resolve", "hit"]
    assert traced.trace[0].input_uri == "base.json"
    assert traced.trace[0].uri == "http://example.com/root/base.json"


def test_crawl_hit():
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "$defs": {"foo": {"$id": "foo.json", "type": "integer"}},
        },
    )
    registry = Registry().with_resource("http://example.com/root.json", schema)

    traced = registry.resolver().lookup_traced(
        "http://example.com/foo.json",
    )

    assert traced.contents == {"$id": "foo.json", "type": "integer"}
    assert names(traced) == ["resolve", "crawl", "crawl_hit"]
    crawl, hit = traced.trace[1:]
    assert crawl.action is Action.crawl
    assert crawl.retrieved is False
    assert hit.action is Action.crawl_hit
    assert hit.uri == "http://example.com/foo.json"
    assert hit.target == "http://example.com/foo.json"
    assert hit.retrieved is False


def test_retrieve_success_once_per_lookup():
    calls = []

    def retrieve(uri):
        calls.append(uri)
        return make({"$id": uri, "type": "string"})

    registry = Registry(retrieve=retrieve)
    traced = registry.resolver().lookup_traced(
        "http://other.example/schema.json",
    )

    assert calls == ["http://other.example/schema.json"]
    assert names(traced) == ["resolve", "retrieve"]
    assert traced.trace[-1].retrieved is True
    assert traced.trace[-1].target == "http://other.example/schema.json"
    assert traced.contents == {
        "$id": "http://other.example/schema.json",
        "type": "string",
    }

    # A second lookup which *reuses the resolver returned after retrieval*
    # hits the now-cached resource and never invokes retrieve again.
    again = traced.resolver.lookup_traced(
        "http://other.example/schema.json",
    )
    assert calls == ["http://other.example/schema.json"]
    assert names(again) == ["resolve", "hit"]


def test_retrieve_failure_trace_and_cause():
    def retrieve(uri):
        raise exceptions.NoSuchResource(ref=uri)

    registry = Registry(retrieve=retrieve)
    with pytest.raises(exceptions.Unresolvable) as excinfo:
        registry.resolver().lookup_traced("http://other.example/missing.json")

    error = excinfo.value
    assert error.ref == "http://other.example/missing.json"
    assert isinstance(error.trace, Trace)
    assert [event.action.value for event in error.trace] == [
        "resolve",
        "retrieve",
        "retrieve_failed",
    ]
    failed = error.trace[-1]
    assert failed.found is False
    assert failed.retrieved is True
    assert error.__cause__ is None


def test_retrieve_unexpected_error_is_wrapped_with_trace():
    def retrieve(uri):
        raise ValueError("boom")

    registry = Registry(retrieve=retrieve)
    with pytest.raises(exceptions.Unresolvable) as excinfo:
        registry.resolver().lookup_traced("http://other.example/broken.json")

    error = excinfo.value
    assert isinstance(error.__cause__, exceptions.Unretrievable)
    assert isinstance(error.__cause__.__cause__, ValueError)
    assert error.trace[-1].action is Action.retrieve_failed


def test_pointer_escaped_segments():
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "properties": {
                "a/b": {"type": "integer"},
                "100%": {"type": "string"},
                "~tilde": {"type": "null"},
            },
        },
    )
    registry = schema @ Registry()

    slash = registry.resolver().lookup_traced(
        "http://example.com/root.json#/properties/a~1b",
    )
    assert slash.contents == {"type": "integer"}
    last = slash.trace[-1]
    assert last.action is Action.pointer
    assert last.raw_segment == "a~1b"
    assert last.decoded_segment == "a/b"

    percent = registry.resolver().lookup_traced(
        "http://example.com/root.json#/properties/100%25",
    )
    last = percent.trace[-1]
    assert last.raw_segment == "100%"
    assert last.decoded_segment == "100%"

    tilde = registry.resolver().lookup_traced(
        "http://example.com/root.json#/properties/~0tilde",
    )
    last = tilde.trace[-1]
    assert last.raw_segment == "~0tilde"
    assert last.decoded_segment == "~tilde"


def test_pointer_array_index_segment():
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "allOf": [{"type": "integer"}],
        },
    )
    registry = schema @ Registry()
    traced = registry.resolver().lookup_traced(
        "http://example.com/root.json#/allOf/0",
    )
    index = traced.trace[-1]
    assert index.action is Action.pointer
    assert index.raw_segment == "0"
    assert index.decoded_segment == 0


def test_pointer_to_nowhere_trace():
    schema = make({"$id": "http://example.com/root.json", "type": "object"})
    registry = schema @ Registry()
    with pytest.raises(exceptions.PointerToNowhere) as excinfo:
        registry.resolver().lookup_traced(
            "http://example.com/root.json#/missing",
        )

    error = excinfo.value
    assert error.ref == "/missing"
    assert error.trace[-1].action is Action.pointer
    assert error.trace[-1].found is False
    assert error.trace[-1].raw_segment == "missing"


def test_plain_anchor_hit():
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "$defs": {"foo": {"$anchor": "myAnchor", "type": "null"}},
        },
    )
    registry = schema @ Registry()
    traced = registry.resolver().lookup_traced(
        "http://example.com/root.json#myAnchor",
    )
    assert traced.contents == {"$anchor": "myAnchor", "type": "null"}
    event = traced.trace[-1]
    assert event.action is Action.anchor
    assert event.uri == "http://example.com/root.json"
    assert event.target == "http://example.com/root.json#myAnchor"


def test_missing_anchor_trace_and_exception_type():
    schema = make({"$id": "http://example.com/root.json"})
    registry = schema @ Registry()
    with pytest.raises(exceptions.NoSuchAnchor) as excinfo:
        registry.resolver().lookup_traced(
            "http://example.com/root.json#missing",
        )
    error = excinfo.value
    assert error.anchor == "missing"
    last = error.trace[-1]
    assert last.action is Action.anchor
    assert last.found is False
    assert last.target == "http://example.com/root.json#missing"


def test_subresource_base_uri_change_is_visible():
    schema = make(
        {
            "$id": "http://example.com/base/",
            "items": {"$id": "item.json", "type": "integer"},
        },
    )
    registry = schema @ Registry()
    first = registry.resolver().lookup_traced(
        "http://example.com/base/",
    )
    entered = first.resolver.lookup_traced("#/items")

    subresource_events = [
        event for event in entered.trace if event.action is Action.subresource
    ]
    assert len(subresource_events) == 1
    event = subresource_events[0]
    assert event.input_uri == "http://example.com/base/"
    assert event.uri == "http://example.com/base/item.json"
    assert event.target == "http://example.com/base/item.json"

    # The updated base URI is now used to combine the relative reference.
    relative = entered.resolver.lookup_traced("item.json")
    assert relative.contents == {"$id": "item.json", "type": "integer"}
    assert relative.trace[0].input_uri == "item.json"
    assert relative.trace[0].uri == "http://example.com/base/item.json"
    assert names(relative) == ["resolve", "crawl", "crawl_hit"]


def _dynamic_resolver():
    true_resource = make(True)
    root = make(
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
    registry = Registry().with_resources(
        [
            ("http://example.com", root),
            ("http://example.com/foo/", true_resource),
            ("http://example.com/foo/bar", root),
        ],
    )
    first = registry.resolver().lookup("http://example.com")
    second = first.resolver.lookup("foo/")
    third = second.resolver.lookup("bar")
    return third.resolver, root


def test_dynamic_anchor_records_scope_in_order():
    resolver, root = _dynamic_resolver()
    traced = resolver.lookup_traced("#fooAnchor")

    assert traced.contents == root.contents
    scope = [
        event for event in traced.trace if event.action is Action.dynamic_scope
    ]
    # Real dynamic-scope order, every examined URI recorded -- not just
    # the final hit.
    assert [event.uri for event in scope] == [
        "http://example.com/foo/",
        "http://example.com",
    ]
    assert scope[0].found is False
    assert scope[0].dynamic is False
    assert scope[1].found is True
    assert scope[1].dynamic is True
    # The entry into the dynamically selected resource is traced too.
    assert traced.trace[-1].action is Action.subresource
    assert traced.trace[-1].uri == "http://example.com"


def test_dynamic_anchor_non_dynamic_match():
    one = make({"$anchor": "fooAnchor"})
    two = make(
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
    registry = Registry().with_resources(
        [
            ("http://example.com", two),
            ("http://example.com/foo/", one),
            ("http://example.com/foo/bar", two),
        ],
    )
    first = registry.resolver().lookup("http://example.com")
    second = first.resolver.lookup("foo/")
    resolver = second.resolver.lookup("bar").resolver

    traced = resolver.lookup_traced("#fooAnchor")
    scope = [
        event for event in traced.trace if event.action is Action.dynamic_scope
    ]
    assert scope[0].uri == "http://example.com/foo/"
    assert scope[0].found is True
    assert scope[0].dynamic is False
    assert scope[1].found is True
    assert scope[1].dynamic is True
    assert traced.contents == two.contents


def test_repeated_traced_lookups_do_not_cross_contaminate():
    schema = make({"$id": "http://example.com/root.json", "type": "object"})
    registry = schema @ Registry()
    resolver = registry.resolver()

    first = resolver.lookup_traced("http://example.com/root.json")
    second = resolver.lookup_traced("http://example.com/root.json#/type")

    assert first is not second
    assert len(first.trace) == 2
    assert len(second.trace) == 3
    assert [event.action for event in first.trace] == [
        Action.resolve,
        Action.hit,
    ]
    assert first.trace == Trace(events=tuple(first.trace))


def test_trace_is_immutable_and_contents_free():
    schema = make({"$id": "http://example.com/root.json", "secret": 42})
    registry = schema @ Registry()
    traced = registry.resolver().lookup_traced(
        "http://example.com/root.json#/secret",
    )
    with pytest.raises((AttributeError, TypeError)):
        traced.trace.events = ()  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        traced.trace.events.append(None)  # type: ignore[union-attr]
    rendered = repr(traced.trace)
    assert "42" not in rendered


def test_concurrent_lookups_are_isolated():
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "$defs": {
                "one": {"$anchor": "one"},
                "two": {"$anchor": "two"},
            },
        },
    )
    registry = schema @ Registry()
    shared = registry.resolver()
    results = {}
    errors = []

    def worker(name, ref):
        try:
            traced = shared.lookup_traced(ref)
            results[name] = [
                (event.action.value, event.target) for event in traced.trace
            ]
        except Exception as error:  # pragma: no cover  # noqa: BLE001
            errors.append(error)

    threads = [
        threading.Thread(
            target=worker,
            args=("one", "http://example.com/root.json#one"),
        ),
        threading.Thread(
            target=worker,
            args=("two", "http://example.com/root.json#two"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    targets_one = [target for _, target in results["one"]]
    targets_two = [target for _, target in results["two"]]
    assert targets_one[-1] == "http://example.com/root.json#one"
    assert targets_two[-1] == "http://example.com/root.json#two"
    assert all("two" not in target for target in targets_one)
    assert all("#one" not in target for target in targets_two)


def test_normal_lookup_allocates_no_trace_objects(monkeypatch):
    schema = make(
        {
            "$id": "http://example.com/root.json",
            "$defs": {"foo": {"$id": "foo.json"}},
        },
    )
    registry = Registry().with_resource("http://example.com/root.json", schema)
    resolver = registry.resolver()

    def fail(*args, **kwargs):  # pragma: no cover
        raise AssertionError("tracing types must not be built by lookup")

    monkeypatch.setattr(_trace, "TraceEvent", fail)
    monkeypatch.setattr(_trace, "_Collector", fail)

    # Local hits, crawling and pointer traversal all stay untraced.
    assert (
        resolver.lookup(
            "http://example.com/root.json",
        ).contents
        == schema.contents
    )
    assert resolver.lookup(
        "http://example.com/foo.json",
    ).contents == {"$id": "foo.json"}
    assert resolver.lookup(
        "http://example.com/root.json#/$defs/foo",
    ).contents == {"$id": "foo.json"}
