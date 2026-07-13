"""P1 slice: cached DecisionContext provider seam contracts.

This provider is a lazy, refresh-driven cache in front of a builder. It never
builds on construction or on read; it only rebuilds when ``refresh()`` is called.
Refreshes are serialized, publish atomically on success, and retain the last good
snapshot on failure. Reads never block a concurrent refresh and never filter by TTL.
Before the first successful refresh ``get()`` returns ``None``.
"""
import ast
import pathlib
import threading
import time

import pytest

import wrr.runtime.decision_context_provider as provider_module

from wrr.runtime.decision_context_provider import CachedDecisionContextProvider
from wrr.schemas import DecisionContext


def _context(*, snapshot_version="v1", built_at=1.0, expires_at=2.0):
    """Build a minimal valid DecisionContext for provider tests."""
    return DecisionContext(
        snapshot_version=snapshot_version,
        built_at=built_at,
        expires_at=expires_at,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa",),
        bridged_provider_ids=("exa",),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(),
        config_fingerprint="fp",
    )


class _CountingBuilder:
    """Records how many times it is invoked and returns a scripted context."""

    def __init__(self, context):
        self.context = context
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.context


# ── construction / read never build ─────────────────────────────────────

def test_construction_does_not_call_builder():
    builder = _CountingBuilder(_context())
    CachedDecisionContextProvider(builder)
    assert builder.calls == 0


def test_get_before_refresh_does_not_call_builder_and_returns_none():
    builder = _CountingBuilder(_context())
    provider = CachedDecisionContextProvider(builder)
    assert provider.get() is None
    assert builder.calls == 0


# ── refresh publishes; get reads last published ─────────────────────────

def test_refresh_calls_builder_once_and_publishes():
    context = _context()
    builder = _CountingBuilder(context)
    provider = CachedDecisionContextProvider(builder)

    published = provider.refresh()

    assert builder.calls == 1
    assert published is context
    assert provider.get() is context


def test_get_does_not_call_builder_after_publish():
    builder = _CountingBuilder(_context())
    provider = CachedDecisionContextProvider(builder)
    provider.refresh()
    provider.get()
    provider.get()
    assert builder.calls == 1


def test_refresh_replaces_previous_snapshot_atomically():
    first = _context(snapshot_version="v1")
    second = _context(snapshot_version="v2")
    contexts = iter((first, second))
    provider = CachedDecisionContextProvider(lambda: next(contexts))

    provider.refresh()
    assert provider.get() is first
    provider.refresh()
    assert provider.get() is second


# ── failure retains last-good and propagates ────────────────────────────

def test_refresh_exception_propagates_and_retains_last_good():
    good = _context(snapshot_version="good")
    state = {"fail": False}

    def builder():
        if state["fail"]:
            raise RuntimeError("build boom")
        return good

    provider = CachedDecisionContextProvider(builder)
    provider.refresh()
    assert provider.get() is good

    state["fail"] = True
    with pytest.raises(RuntimeError, match="build boom"):
        provider.refresh()
    # last-good snapshot survives the failed refresh.
    assert provider.get() is good


def test_refresh_wrong_return_type_retains_last_good_and_raises():
    good = _context(snapshot_version="good")
    state = {"bad": False}

    def builder():
        return "not-a-decision-context" if state["bad"] else good

    provider = CachedDecisionContextProvider(builder)
    provider.refresh()

    state["bad"] = True
    with pytest.raises(TypeError):
        provider.refresh()
    assert provider.get() is good


def test_first_refresh_failure_leaves_no_snapshot():
    def builder():
        raise RuntimeError("boom")

    provider = CachedDecisionContextProvider(builder)
    with pytest.raises(RuntimeError):
        provider.refresh()
    assert provider.get() is None


# ── TTL is not a read filter ────────────────────────────────────────────

def test_get_does_not_filter_expired_snapshot():
    # built_at/expires_at are far in the past relative to any wall clock.
    stale = _context(built_at=1.0, expires_at=2.0)
    provider = CachedDecisionContextProvider(lambda: stale)
    provider.refresh()
    assert provider.get() is stale


# ── refresh serialization + non-blocking reads ──────────────────────────

def test_concurrent_refreshes_are_serialized():
    active = {"count": 0, "max": 0, "calls": 0}
    lock = threading.Lock()

    def builder():
        with lock:
            active["calls"] += 1
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
        time.sleep(0.05)
        with lock:
            active["count"] -= 1
        return _context()

    provider = CachedDecisionContextProvider(builder)
    threads = [threading.Thread(target=provider.refresh) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Never more than one builder running at a time.
    assert active["max"] == 1
    assert active["calls"] == 4


def test_get_returns_old_snapshot_while_refresh_builds():
    old = _context(snapshot_version="old")
    new = _context(snapshot_version="new")
    started = threading.Event()
    release = threading.Event()
    call = {"n": 0}

    def builder():
        call["n"] += 1
        if call["n"] == 1:
            return old
        # Second refresh blocks mid-build until the test releases it.
        started.set()
        release.wait(timeout=2.0)
        return new

    provider = CachedDecisionContextProvider(builder)
    provider.refresh()  # publish `old`

    worker = threading.Thread(target=provider.refresh)
    worker.start()
    assert started.wait(timeout=2.0)

    # While the second build is in flight, get() must not block and must
    # still return the previously published snapshot.
    assert provider.get() is old

    release.set()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert provider.get() is new


# ── source purity: no hot-path / I/O dependencies ───────────────────────

# Tokens that would betray the provider reaching into routing, search,
# comparison, discovery, registry defaults, the environment, the filesystem,
# evidence, or the network — none belong in this leaf cache.
_FORBIDDEN_TOKENS = (
    "route",
    "search",
    "comparison",
    "discovery",
    "default_registry",
    "env",
    "open",
    "evidence",
    "network",
)


def _provider_source_tree():
    source = pathlib.Path(provider_module.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def test_provider_only_imports_stdlib_and_schemas():
    # The only permitted imports are stdlib plumbing and the DecisionContext schema.
    allowed_roots = {"__future__", "threading", "typing", "wrr"}
    allowed_modules = {"__future__", "threading", "typing", "wrr.schemas"}
    for node in ast.walk(_provider_source_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in allowed_roots, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module in allowed_modules, module


def test_provider_source_has_no_hot_path_or_io_identifiers():
    # Scan real code identifiers (AST Names/Attributes) and imported module paths.
    # Docstrings and comments are excluded by construction: they never appear as
    # identifiers, so prose like "routing" or "discovery" cannot trip this check.
    identifiers = []
    for node in ast.walk(_provider_source_tree()):
        if isinstance(node, ast.Name):
            identifiers.append(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.append(node.attr)
        elif isinstance(node, ast.Import):
            identifiers.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            identifiers.append(node.module or "")
            identifiers.extend(alias.name for alias in node.names)

    for name in identifiers:
        lowered = name.lower()
        for token in _FORBIDDEN_TOKENS:
            assert token not in lowered, f"forbidden token {token!r} in identifier {name!r}"
