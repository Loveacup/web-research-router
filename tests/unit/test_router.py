"""Sequential fetch/similar fallback and public diagnostic contracts."""
import asyncio

import pytest

from conftest import FakeEngine, mk_results
from wrr import config
from wrr.errors import AllEnginesFailedError, EngineError
from wrr.registry import EngineRegistry
from wrr.router import build_chain, route
from wrr.schemas import ExtractOptions, SimilarOptions


def _reg(*engines):
    registry = EngineRegistry()
    for engine in engines:
        registry.register(engine)
    return registry


def run(coro):
    return asyncio.run(coro)


def test_build_chain_default_and_explicit():
    assert build_chain("extract", None) == ["exa", "brave"]
    assert build_chain("similar", None) == ["exa"]
    assert build_chain("extract", "brave") == ["brave"]


@pytest.mark.parametrize("operation,options", [
    ("extract", ExtractOptions("https://x")),
    ("similar", SimilarOptions("https://x")),
])
def test_first_nonempty_provider_stops_fallback(operation, options, monkeypatch):
    if operation == "similar":
        monkeypatch.setattr(config, "SIMILAR_PROVIDERS", ["exa", "brave"])
    registry = _reg(FakeEngine("exa", extract_text="body", similar_results=mk_results(2)),
                    FakeEngine("brave", error="must not run"))
    result = run(route(operation, options, registry))
    assert result.actual_provider == "exa"
    assert len(result.fallback_chain) == 1
    assert result.diagnostics.events[0].ok is True
    assert result.quality.verdict == ("degraded" if operation == "extract" else "complete")


@pytest.mark.parametrize("first", [FakeEngine("exa", error="down"),
                                      FakeEngine("exa", extract_text="")])
def test_extract_failure_or_empty_text_falls_back(first):
    result = run(route("extract", ExtractOptions("https://x"),
                       _reg(first, FakeEngine("brave", extract_text="hello"))))
    assert result.actual_provider == "brave"
    assert result.payload.text == "hello"
    assert result.degraded_from == "exa"
    assert result.quality.verdict == "degraded_success"
    assert [event.engine for event in result.diagnostics.events] == ["exa", "brave"]


def test_similar_empty_result_falls_back(monkeypatch):
    monkeypatch.setattr(config, "SIMILAR_PROVIDERS", ["exa", "brave"])
    result = run(route("similar", SimilarOptions("https://x"), _reg(
        FakeEngine("exa", similar_results=[]),
        FakeEngine("brave", similar_results=mk_results(3)),
    )))
    assert result.actual_provider == "brave"
    assert len(result.payload) == 3


def test_explicit_provider_disables_fallback():
    registry = _reg(FakeEngine("exa", extract_text="must not run"),
                    FakeEngine("brave", error="down"))
    with pytest.raises(AllEnginesFailedError) as caught:
        run(route("extract", ExtractOptions("https://x"), registry, explicit_provider="brave"))
    assert [event.engine for event in caught.value.diagnostics.events] == ["brave"]


def test_explicit_provider_gets_full_engine_timeout(monkeypatch):
    import wrr.router as router

    captured = []

    async def fake_wait_for(awaitable, timeout):
        captured.append(timeout)
        return await awaitable

    monkeypatch.setattr(router.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setitem(config.ENGINE_TIMEOUT, "brave", 20.0)
    result = run(route("extract", ExtractOptions("https://x"),
                       _reg(FakeEngine("brave", extract_text="body", timeout=20.0)),
                       explicit_provider="brave"))
    assert result.actual_provider == "brave"
    assert captured[0] >= 19.9


def test_unknown_provider_in_chain_fails_gracefully():
    with pytest.raises(AllEnginesFailedError) as caught:
        run(route("extract", ExtractOptions("https://x"), _reg(FakeEngine("exa", error="down"))))
    assert "unknown provider: brave" in str(caught.value)
    assert caught.value.diagnostics.quality.verdict == "failed"


def test_all_fail_error_retains_safe_request_diagnostics():
    class DiagnosticFailEngine(FakeEngine):
        async def extract(self, options):
            error = EngineError("no completed sources")
            error.details = {
                "partial": False,
                "cutoff_reason": "request_deadline",
                "sources": [{"source": "fixture", "outcome": "cutoff", "token": "secret"}],
                "credential": "secret",
            }
            raise error

    with pytest.raises(AllEnginesFailedError) as caught:
        run(route("extract", ExtractOptions("https://x"),
                  _reg(DiagnosticFailEngine("exa")), explicit_provider="exa"))
    assert caught.value.diagnostics.events[0].details == {
        "partial": False,
        "cutoff_reason": "request_deadline",
        "sources": [{"source": "fixture", "outcome": "cutoff"}],
    }


def test_route_success_includes_diagnostics():
    result = run(route("similar", SimilarOptions("https://x"),
                       _reg(FakeEngine("exa", similar_results=mk_results(2)))))
    assert result.diagnostics.mode_reason == "v4_fallback_chain"
    assert result.diagnostics.selected_engines == ["exa"]
    assert result.diagnostics.events[0].count == 2
