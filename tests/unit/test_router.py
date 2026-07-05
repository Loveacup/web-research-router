"""router fallback 控制流单测（对齐执行包 Step4 验收）。"""
import asyncio

from conftest import FakeEngine, mk_results
from wrr.registry import EngineRegistry
from wrr.router import route, build_chain
from wrr.schemas import SearchOptions, ExtractOptions, SimilarOptions
from wrr.errors import AllEnginesFailedError


def _reg(*engines):
    r = EngineRegistry()
    for e in engines:
        r.register(e)
    return r


def run(coro):
    return asyncio.run(coro)


# ── build_chain ──────────────────────────────────────────────────────
def test_build_chain_default_and_explicit():
    assert build_chain("search", None) == ["exa", "brave", "github", "community", "searxng"]
    assert build_chain("extract", None) == ["exa", "brave"]
    assert build_chain("similar", None) == ["exa"]
    assert build_chain("search", "brave") == ["brave"]   # 显式 → 单元素


# ── search fallback ──────────────────────────────────────────────────
def test_search_normal_no_degrade():
    reg = _reg(FakeEngine("exa", search_results=mk_results(2)),
               FakeEngine("brave", error="should not call"),
               FakeEngine("searxng", error="should not call"))
    rr = run(route("search", SearchOptions("q"), reg))
    assert rr.actual_provider == "exa"
    assert len(rr.payload) == 2


def test_search_exception_falls_to_brave():
    reg = _reg(FakeEngine("exa", error="exa down"),
               FakeEngine("brave", search_results=mk_results(1)),
               FakeEngine("searxng", search_results=mk_results(1)))
    rr = run(route("search", SearchOptions("q"), reg))
    assert rr.actual_provider == "brave"
    assert rr.degraded_from == "exa"


def test_search_empty_falls_through():
    reg = _reg(FakeEngine("exa", search_results=[]),
               FakeEngine("brave", search_results=[]),
               FakeEngine("searxng", search_results=mk_results(1)))
    rr = run(route("search", SearchOptions("q"), reg))
    assert rr.actual_provider == "searxng"


def test_search_all_fail_raises():
    reg = _reg(FakeEngine("exa", error="down"),
               FakeEngine("brave", error="down"),
               FakeEngine("searxng", error="down"))
    try:
        run(route("search", SearchOptions("q"), reg))
        assert False, "should raise"
    except AllEnginesFailedError:
        pass


def test_explicit_provider_disables_fallback():
    reg = _reg(FakeEngine("exa", search_results=mk_results(1)),
               FakeEngine("brave", error="brave down"),
               FakeEngine("searxng", search_results=mk_results(1)))
    try:
        run(route("search", SearchOptions("q", provider="brave"), reg, explicit_provider="brave"))
        assert False, "explicit brave failure must not fall back"
    except AllEnginesFailedError:
        pass


def test_explicit_provider_gets_full_engine_timeout():
    """显式 --provider 时不受 10s 总预算限制，应使用 engine.timeout。"""
    import wrr.router as router

    captured = []
    orig_wait_for = router.asyncio.wait_for

    async def fake_wait_for(awaitable, timeout):
        captured.append(timeout)
        return await awaitable

    router.asyncio.wait_for = fake_wait_for
    try:
        reg = _reg(FakeEngine("github", search_results=mk_results(1), timeout=20.0))
        rr = run(route("search", SearchOptions("q", provider="github"), reg, explicit_provider="github"))
    finally:
        router.asyncio.wait_for = orig_wait_for
    assert rr.actual_provider == "github"
    assert captured[0] >= 19.9


def test_community_trigger_gets_community_budget():
    import wrr.router as router

    captured = []
    orig_wait_for = router.asyncio.wait_for

    async def fake_wait_for(awaitable, timeout):
        captured.append(timeout)
        return await awaitable

    router.asyncio.wait_for = fake_wait_for
    try:
        reg = _reg(FakeEngine("community", search_results=mk_results(1), timeout=20.0))
        rr = run(route("search", SearchOptions("site:reddit.com q"), reg))
    finally:
        router.asyncio.wait_for = orig_wait_for
    assert rr.actual_provider == "community"
    assert captured[0] >= 19.9


# ── extract fallback ─────────────────────────────────────────────────
def test_extract_empty_text_falls_back():
    reg = _reg(FakeEngine("exa", extract_text=""),
               FakeEngine("brave", extract_text="hello"))
    rr = run(route("extract", ExtractOptions("https://x"), reg))
    assert rr.actual_provider == "brave"
    assert rr.payload.text == "hello"


# ── similar ──────────────────────────────────────────────────────────
def test_similar_single_provider():
    reg = _reg(FakeEngine("exa", similar_results=mk_results(3)))
    rr = run(route("similar", SimilarOptions("https://x"), reg))
    assert rr.actual_provider == "exa"
    assert len(rr.payload) == 3


def test_unknown_provider_in_chain_fails_gracefully():
    reg = _reg(FakeEngine("exa", error="down"))   # brave/searxng 未注册
    try:
        run(route("search", SearchOptions("q"), reg))
        assert False
    except AllEnginesFailedError as e:
        assert "unknown provider" in str(e)
