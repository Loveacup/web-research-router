"""P0-3 route-level quality contract tests（纯 fake engine，零网络）。"""
import asyncio

import httpx
import pytest

from conftest import FakeEngine, mk_results
from wrr import config
from wrr.engines import brave as brave_mod
from wrr.engines.base import SearchEngine
from wrr.errors import EngineError, EngineTimeoutError, RateLimitError
from wrr.registry import EngineRegistry
from wrr.router import _route_quality, route_search_v5
from wrr.schemas import FallbackStep, SearchOptions, SearchResult


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


# ══════════════════════════════════════════════════════════════════════
# Diversity backfill：Brave 失败时 Tavily 条件性独立来源补位（纯 fake，零网络）
# ══════════════════════════════════════════════════════════════════════

class _ProbeEngine(SearchEngine):
    """可编排且可计数的 fake 引擎：按需返回结果或抛指定异常，记录 search 调用次数。"""

    def __init__(self, name, *, results=None, exc=None, timeout=5.0):
        self.name = name
        self._results = results
        self._exc = exc
        self._timeout = timeout
        self.calls = 0

    @property
    def timeout(self):
        return self._timeout

    async def search(self, options):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._results if self._results is not None else []

    async def extract(self, options):
        return None

    async def similar(self, options):
        return []


def _tavily_result(url="https://tav-unique"):
    return [SearchResult(title="tav", url=url, snippet="tv", highlights=["tvh"])]


@pytest.mark.parametrize("mode", ["grounding", "research"])
@pytest.mark.parametrize(
    "brave_exc",
    [RateLimitError("429"), EngineTimeoutError("slow"), asyncio.TimeoutError(), None],
    ids=["ratelimit", "enginetimeout", "asynctimeout", "empty"],
)
def test_diversity_backfill_invokes_tavily_exactly_once(monkeypatch, mode, brave_exc):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    brave = _ProbeEngine("brave", results=[]) if brave_exc is None else _ProbeEngine("brave", exc=brave_exc)
    reg.register(brave)
    tavily = _ProbeEngine("tavily", results=_tavily_result())
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", mode=mode), reg))

    assert tavily.calls == 1          # 恰调用一次
    assert brave.calls == 1           # brave 不重跑
    assert "tavily" in result.diagnostics.selected_engines


def test_diversity_backfill_success_is_degraded_with_two_sources(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(_ProbeEngine("brave", exc=RateLimitError("429")))
    reg.register(_ProbeEngine("tavily", results=_tavily_result()))

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), reg))

    q = result.quality
    assert q.verdict == "degraded_success"
    assert set(q.successful_sources) == {"exa", "tavily"}
    assert q.independent_source_count == 2
    assert "tavily" in result.diagnostics.selected_engines
    urls = [r.url for r in result.payload]
    assert any("tav-unique" in u for u in urls)   # 非重叠 Tavily 结果被保留
    assert any("u0" in u for u in urls)           # primary 未被抹掉


def test_diversity_backfill_failure_keeps_primary_single_source_only(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(_ProbeEngine("brave", exc=EngineTimeoutError("slow")))
    reg.register(_ProbeEngine("tavily", exc=EngineError("tavily down")))

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), reg))

    q = result.quality
    assert q.verdict == "insufficient"
    assert q.successful_sources == ["exa"]
    assert "single_source_only" in q.reasons
    payload = result.diagnostics.to_dict()
    assert "single_source_only" in payload["quality"]["reasons"]
    assert [r.url for r in result.payload] == ["https://u0"]     # 保留 exa primary
    tavily_failed = [s for s in result.fallback_chain if s.provider == "tavily" and not s.ok]
    assert tavily_failed


def test_normal_brave_engine_error_does_not_trigger_backfill(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(_ProbeEngine("brave", exc=EngineError("boom")))
    tavily = _ProbeEngine("tavily", results=mk_results(1))
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), reg))

    assert tavily.calls == 0
    assert "tavily" not in result.diagnostics.selected_engines
    assert result.quality.verdict == "insufficient"
    assert "single_source_only" in result.quality.reasons


def test_two_successful_primary_sources_skip_backfill(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(_ProbeEngine("brave", results=_tavily_result("https://brave0")))
    tavily = _ProbeEngine("tavily", results=mk_results(1))
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", mode="research"), reg))

    assert tavily.calls == 0
    assert "tavily" not in result.diagnostics.selected_engines
    assert result.quality.independent_source_count >= 2


def test_explicit_provider_skips_backfill(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("brave", results=mk_results(1)))
    tavily = _ProbeEngine("tavily", results=mk_results(1))
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", provider="brave"), reg))

    assert tavily.calls == 0
    assert "tavily" not in result.diagnostics.selected_engines


# ══════════════════════════════════════════════════════════════════════
# 真实 BraveEngine + 真实 httpx error class 注入：端到端归一化 tripwire
# （证明 brave.py 的 HTTP 归一化真能穿透 router 触发 Tavily，不是仅 _ProbeEngine 人工异常）
# ══════════════════════════════════════════════════════════════════════

class _BraveHTTPFake:
    """fake httpx.AsyncClient：注入真实 httpx 异常/HTTP status。零网络。"""
    exc = None            # client.get() 直接抛（timeout/transport 类）
    status = None         # resp.raise_for_status() 抛真实 HTTPStatusError

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        if _BraveHTTPFake.exc is not None:
            raise _BraveHTTPFake.exc
        req = httpx.Request("GET", url)
        return httpx.Response(_BraveHTTPFake.status, request=req)


def _arm_brave_http(monkeypatch, *, exc=None, status=None):
    _BraveHTTPFake.exc = exc
    _BraveHTTPFake.status = status
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setattr(brave_mod.httpx, "AsyncClient", _BraveHTTPFake)


@pytest.mark.parametrize("mode", ["grounding", "research"])
@pytest.mark.parametrize(
    "exc,status",
    [(None, 429), (httpx.TimeoutException("slow"), None)],
    ids=["http429", "timeout"],
)
def test_real_brave_http_failure_triggers_tavily_exactly_once(monkeypatch, mode, exc, status):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    _arm_brave_http(monkeypatch, exc=exc, status=status)
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(brave_mod.BraveEngine())         # 真实 Brave，httpx 被注入抛错
    tavily = _ProbeEngine("tavily", results=_tavily_result())
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", mode=mode), reg))

    assert tavily.calls == 1
    assert "tavily" in result.diagnostics.selected_engines


@pytest.mark.parametrize(
    "exc,status",
    [(None, 500), (httpx.ConnectError("boom"), None)],
    ids=["http500", "transport"],
)
def test_real_brave_non_tripwire_http_does_not_trigger_backfill(monkeypatch, exc, status):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    _arm_brave_http(monkeypatch, exc=exc, status=status)
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(brave_mod.BraveEngine())         # 真实 Brave：500 / transport → 普通 EngineError
    tavily = _ProbeEngine("tavily", results=_tavily_result())
    reg.register(tavily)

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), reg))

    assert tavily.calls == 0
    assert "tavily" not in result.diagnostics.selected_engines
    assert result.quality.verdict == "insufficient"
    assert "single_source_only" in result.quality.reasons


def test_tavily_unknown_provider_degrades_to_single_source_only(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    reg = EngineRegistry()
    reg.register(_ProbeEngine("exa", results=mk_results(1)))
    reg.register(_ProbeEngine("brave", exc=RateLimitError("429")))
    # tavily 故意不注册

    result = run(route_search_v5(SearchOptions("what is x", mode="grounding"), reg))

    assert result.quality.verdict == "insufficient"
    assert "single_source_only" in result.quality.reasons
    assert [r.url for r in result.payload] == ["https://u0"]
    tavily_steps = [s for s in result.fallback_chain if s.provider == "tavily"]
    assert len(tavily_steps) == 1 and not tavily_steps[0].ok
    assert "tavily" in result.diagnostics.selected_engines
