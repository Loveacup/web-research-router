"""Router v6 shadow consumption tests."""

import asyncio

import pytest

from conftest import FakeEngine
from wrr.errors import AllEnginesFailedError
from wrr.router import route_search_v5
from wrr.schemas import SearchOptions, SearchResult


def run(coro):
    return asyncio.run(coro)


class MinimalRegistry:
    """Descriptor-backed registry stand-in: router only needs get(name)."""

    def __init__(self, engines):
        self._engines = {engine.name: engine for engine in engines}

    def get(self, name):
        return self._engines.get(name)


def _results(provider, n=2):
    return [
        SearchResult(
            title=f"{provider} title {i}",
            url=f"https://example.test/{provider}/{i}",
            snippet=f"{provider} snippet {i}",
        )
        for i in range(n)
    ]


def _registry(names):
    return MinimalRegistry(
        FakeEngine(name, search_results=_results(name))
        for name in names
    )


def _selected(result):
    return [step.provider for step in result.fallback_chain]


@pytest.mark.parametrize(
    "query,mode",
    [
        ("what is python", "grounding"),
        ("深度分析 ai", "research"),
        ("survey of llm", "academic"),
        ("best python tools", "discovery"),
        ("gpt site:reddit.com", "platform"),
        ("missing deleted config", "recovery"),
    ],
)
def test_old_and_descriptor_backed_registries_select_same_engines(query, mode):
    names = ("exa", "brave", "searxng", "github", "community", "academic", "skill")
    old_registry = _registry(names)
    descriptor_backed = _registry(names)

    old = run(route_search_v5(SearchOptions(query, count=10), old_registry))
    shadow = run(route_search_v5(SearchOptions(query, count=10), descriptor_backed))

    assert old.mode == mode
    assert shadow.mode == mode
    assert old.actual_provider == shadow.actual_provider == f"rrf:{mode}"
    assert _selected(old) == _selected(shadow)
    assert old.weights == shadow.weights


def test_route_search_v5_accepts_injected_descriptor_backed_registry_helper():
    old_registry = _registry(())
    descriptor_backed = _registry(("exa", "brave"))

    result = run(
        route_search_v5(
            SearchOptions("what is python", count=5),
            old_registry,
            descriptor_registry_factory=lambda: descriptor_backed,
        )
    )

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]


def test_explicit_provider_behavior_is_unchanged_with_descriptor_backed_registry():
    registry = _registry(("exa", "brave"))

    result = run(route_search_v5(SearchOptions("q", provider="brave"), registry))

    assert result.actual_provider == "brave"
    assert _selected(result) == ["brave"]
    assert len(result.payload) == 2


def test_v6_router_env_flag_uses_shadow_registry(monkeypatch):
    shadow_registry = _registry(("exa", "brave"))

    monkeypatch.setenv("WRR_V6_ROUTER", "1")
    monkeypatch.setattr(
        "wrr.router._descriptor_backed_registry",
        lambda: shadow_registry,
    )

    result = run(route_search_v5(SearchOptions("what is python"), _registry(())))

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]


def test_without_v6_router_env_flag_keeps_legacy_registry(monkeypatch):
    monkeypatch.delenv("WRR_V6_ROUTER", raising=False)

    def fail_if_called():
        raise AssertionError("v6 shadow registry must not be built")

    monkeypatch.setattr("wrr.router._descriptor_backed_registry", fail_if_called)

    result = run(route_search_v5(SearchOptions("what is python"), _registry(("exa",))))

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]
    assert result.fallback_chain[0].ok is True
    assert result.fallback_chain[1].ok is False
    assert result.fallback_chain[1].error == "unknown provider: brave"


def test_without_v6_router_env_flag_empty_legacy_registry_stays_empty(monkeypatch):
    monkeypatch.delenv("WRR_V6_ROUTER", raising=False)

    with pytest.raises(AllEnginesFailedError):
        run(route_search_v5(SearchOptions("what is python"), _registry(())))
