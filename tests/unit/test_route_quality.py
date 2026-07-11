"""P0-3 route-level quality contract tests（纯 fake engine，零网络）。"""
import asyncio

import pytest

from conftest import FakeEngine, mk_results
from wrr import config
from wrr.registry import EngineRegistry
from wrr.router import _route_quality, route_search_v5
from wrr.schemas import FallbackStep, SearchOptions


def run(coro):
    return asyncio.run(coro)


def _steps(successful, selected):
    return [
        FallbackStep(name, name in successful, 1 if name in successful else 0,
                     None if name in successful else "down")
        for name in selected
    ]


@pytest.mark.parametrize("mode", ["grounding", "academic", "research", "broad"])
def test_multi_source_modes_require_two_successful_engines(mode):
    selected = ["exa", "brave"]

    complete = _route_quality(mode, selected, _steps({"exa", "brave"}, selected), True)
    insufficient = _route_quality(mode, selected, _steps({"exa"}, selected), True)

    assert complete.verdict == "complete"
    assert complete.min_required == 2
    assert complete.independent_source_count == 2
    assert insufficient.verdict == "insufficient"
    assert insufficient.successful_sources == ["exa"]
    assert insufficient.failed_sources == ["brave"]


@pytest.mark.parametrize("mode", ["discovery", "platform", "local", "recovery"])
def test_single_source_modes_require_one_but_record_expected_failures(mode):
    selected = ["community", "exa"]
    quality = _route_quality(mode, selected, _steps({"community"}, selected), True)

    assert quality.verdict == "degraded_success"
    assert quality.min_required == 1
    assert quality.independent_source_count == 1
    assert quality.failed_sources == ["exa"]


def test_quality_precedence_failed_then_insufficient_then_degraded_then_complete():
    selected = ["exa", "brave"]
    assert _route_quality("grounding", selected, _steps(set(), selected), False).verdict == "failed"
    assert _route_quality("grounding", selected, _steps({"exa"}, selected), True).verdict == "insufficient"
    assert _route_quality("discovery", selected, _steps({"exa"}, selected), True).verdict == "degraded_success"
    assert _route_quality("discovery", selected, _steps({"exa", "brave"}, selected), True).verdict == "complete"


def test_successful_engine_is_counted_once():
    steps = [FallbackStep("exa", True, 2), FallbackStep("exa", True, 1)]
    quality = _route_quality("grounding", ["exa", "brave"], steps, True)

    assert quality.independent_source_count == 1
    assert quality.successful_sources == ["exa"]
    assert quality.verdict == "insufficient"


def test_grounding_route_exposes_insufficient_quality_in_trace(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    registry = EngineRegistry()
    registry.register(FakeEngine("exa", error="down"))
    registry.register(FakeEngine("brave", search_results=mk_results(2)))

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), registry))

    assert result.diagnostics is not None
    assert result.quality is result.diagnostics.quality
    assert result.quality is not None
    assert result.quality.verdict == "insufficient"
    assert result.quality.successful_sources == ["brave"]
    payload = result.diagnostics.to_dict()
    assert payload["quality"]["verdict"] == "insufficient"
    assert payload["quality"]["min_required"] == 2


def test_local_web_only_route_is_degraded_success(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    registry = EngineRegistry()
    for name in config.mode_engines("local", "查笔记"):
        if name == "exa":
            registry.register(FakeEngine(name, search_results=mk_results(1)))
        else:
            registry.register(FakeEngine(name, error="unavailable"))

    result = run(route_search_v5(SearchOptions("查笔记", mode="local"), registry))

    assert result.quality is not None
    assert result.quality.verdict == "degraded_success"
    assert result.quality.successful_sources == ["exa"]
    assert result.quality.min_required == 1
