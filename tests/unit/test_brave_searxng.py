"""BraveEngine / SearxngEngine 请求 payload 与映射单测（fake httpx）。"""
import asyncio
import os

import httpx

from conftest import FakeAsyncClient
from wrr.engines import brave as brave_mod
from wrr.engines import searxng as searxng_mod
from wrr.schemas import SearchOptions, ExtractOptions
from wrr.errors import EngineError, EngineTimeoutError, RateLimitError
from wrr import config


def run(coro):
    return asyncio.run(coro)


class _RaisingClient:
    """fake httpx.AsyncClient：get 时抛出预置异常，或返回抛状态错误的响应。零网络。"""
    exc = None            # client.get() 直接抛（transport/timeout 类）
    status = None         # resp.raise_for_status() 抛 HTTPStatusError 的状态码

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, headers=None, timeout=None):
        if _RaisingClient.exc is not None:
            raise _RaisingClient.exc
        req = httpx.Request("GET", url)
        resp = httpx.Response(_RaisingClient.status, request=req)
        return resp        # brave 随后调用 resp.raise_for_status() → HTTPStatusError


def _arm_status(status):
    _RaisingClient.exc = None
    _RaisingClient.status = status
    brave_mod.httpx.AsyncClient = _RaisingClient
    os.environ["BRAVE_API_KEY"] = "k"


def _arm_exc(exc):
    _RaisingClient.exc = exc
    _RaisingClient.status = None
    brave_mod.httpx.AsyncClient = _RaisingClient
    os.environ["BRAVE_API_KEY"] = "k"


def _reset(data=None, text=""):
    FakeAsyncClient.captured = []
    FakeAsyncClient.response_data = data or {}
    FakeAsyncClient.response_text = text


# ── Brave ────────────────────────────────────────────────────────────
def test_brave_search_params_encoded_and_mapped():
    _reset({"web": {"results": [{"title": "T", "url": "U", "description": "D"}]}})
    brave_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ["BRAVE_API_KEY"] = "k"
    out = run(brave_mod.BraveEngine().search(SearchOptions("a b & c", count=7)))
    sent = FakeAsyncClient.captured[-1]
    assert sent["params"] == {"q": "a b & c", "count": 7}   # H3: 经 params 不裸插值
    assert out[0].snippet == "D"


def test_brave_key_fallback_name():
    _reset({"web": {"results": []}})
    brave_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ.pop("BRAVE_API_KEY", None)
    os.environ["BRAVE_SEARCH_API_KEY"] = "legacy"
    # 不应抛 key 缺失（用后备名）
    run(brave_mod.BraveEngine().search(SearchOptions("q")))
    os.environ.pop("BRAVE_SEARCH_API_KEY", None)
    os.environ["BRAVE_API_KEY"] = "k"


def test_brave_extract_strips_html():
    _reset(text="<html><body><script>x=1</script><p>Hello <b>World</b></p></body></html>")
    brave_mod.httpx.AsyncClient = FakeAsyncClient
    out = run(brave_mod.BraveEngine().extract(ExtractOptions("https://x", max_characters=100)))
    assert "Hello World" in out.text
    assert "x=1" not in out.text         # script 被剥


def test_brave_http_429_normalized_to_ratelimit():
    _arm_status(429)
    try:
        run(brave_mod.BraveEngine().search(SearchOptions("q")))
        assert False, "429 should raise RateLimitError"
    except RateLimitError:
        pass


def test_brave_http_500_normalized_to_engineerror_not_ratelimit():
    _arm_status(500)
    try:
        run(brave_mod.BraveEngine().search(SearchOptions("q")))
        assert False, "500 should raise"
    except RateLimitError:
        assert False, "500 must NOT be a RateLimitError"
    except EngineTimeoutError:
        assert False, "500 must NOT be a timeout"
    except EngineError:
        pass          # 普通 HTTP status → 纯 EngineError，不触发 backfill


def test_brave_timeout_normalized_to_engine_timeout():
    _arm_exc(httpx.TimeoutException("slow"))
    try:
        run(brave_mod.BraveEngine().search(SearchOptions("q")))
        assert False, "timeout should raise EngineTimeoutError"
    except EngineTimeoutError:
        pass


def test_brave_transport_error_normalized_to_engineerror_not_backfill():
    _arm_exc(httpx.ConnectError("boom"))
    try:
        run(brave_mod.BraveEngine().search(SearchOptions("q")))
        assert False, "transport error should raise"
    except EngineTimeoutError:
        assert False, "transport error must NOT be a timeout"
    except RateLimitError:
        assert False, "transport error must NOT be a RateLimitError"
    except EngineError:
        pass


def test_brave_missing_key_still_raises_engineerror():
    # 归一化不得吞掉既有的缺 key 行为（_key 在 try 之前抛）
    brave_mod.httpx.AsyncClient = _RaisingClient
    os.environ.pop("BRAVE_API_KEY", None)
    os.environ.pop("BRAVE_SEARCH_API_KEY", None)
    try:
        run(brave_mod.BraveEngine().search(SearchOptions("q")))
        assert False, "missing key should raise"
    except EngineError as e:
        assert "BRAVE_API_KEY" in str(e)
    finally:
        os.environ["BRAVE_API_KEY"] = "k"


# ── SearXNG ──────────────────────────────────────────────────────────
def test_searxng_pins_engines_and_language():
    _reset({"results": [{"title": "T", "url": "U", "content": "C"}]})
    searxng_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ["SEARXNG_URL"] = "http://127.0.0.1:32080"
    out = run(searxng_mod.SearxngEngine().search(SearchOptions("中文 query", count=3)))
    sent = FakeAsyncClient.captured[-1]
    assert sent["params"]["engines"] == config.SEARXNG_ENGINES == "bing,baidu"   # M1
    assert sent["params"]["language"] == config.SEARXNG_LANGUAGE == "zh-CN"
    assert sent["params"]["format"] == "json"
    assert out[0].snippet == "C"


def test_searxng_empty_raises_engineerror_not_rewrapped():
    _reset({"results": []})
    searxng_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ["SEARXNG_URL"] = "http://127.0.0.1:32080"
    try:
        run(searxng_mod.SearxngEngine().search(SearchOptions("q")))
        assert False, "empty should raise"
    except EngineError as e:
        assert "empty results" in str(e)          # M4: 未被二次包成 "SearXNG error:"


def test_searxng_missing_url_raises():
    searxng_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ.pop("SEARXNG_URL", None)
    try:
        run(searxng_mod.SearxngEngine().search(SearchOptions("q")))
        assert False
    except EngineError as e:
        assert "SEARXNG_URL" in str(e)
    os.environ["SEARXNG_URL"] = "http://127.0.0.1:32080"
