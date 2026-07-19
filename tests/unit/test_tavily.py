"""TavilyEngine 离线单测（自带 fake httpx，绝不发真实 HTTP）。

覆盖：
1) POST 方法 / header / payload 与结果映射
2) 缺 key：search 抛 EngineError，health_check status="fail"
3) HTTP 429 / 432 → RateLimitError（参数化，含 status code）
4) 其他 HTTP error 不伪成功（抛 EngineError）
5) default_registry() 含 tavily
"""
import asyncio
import os

import httpx
import pytest

from wrr.engines import tavily as tavily_mod
from wrr.registry import default_registry
from wrr.schemas import SearchOptions
from wrr.errors import EngineError, RateLimitError


def run(coro):
    return asyncio.run(coro)


# ── 自带 fake httpx（捕获 header + payload，可模拟任意 status code）───────
class _FakeResp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", tavily_mod.TAVILY_SEARCH_URL)
            resp = httpx.Response(self.status_code, request=req)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=resp)

    def json(self):
        return self._data


class _FakeClient:
    captured = []
    response_data = {}
    status_code = 200

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None, timeout=None):
        _FakeClient.captured.append(
            {"url": url, "headers": headers, "json": json})
        return _FakeResp(_FakeClient.response_data, _FakeClient.status_code)


def _install(monkeypatch, *, data=None, status_code=200):
    _FakeClient.captured = []
    _FakeClient.response_data = data or {}
    _FakeClient.status_code = status_code
    monkeypatch.setattr(tavily_mod.httpx, "AsyncClient", _FakeClient)


# ── 1) POST 方法 / header / payload 与映射 ─────────────────────────────
def test_search_post_headers_payload_and_mapping(monkeypatch):
    _install(monkeypatch, data={"results": [
        {"title": "T", "url": "U", "content": "C", "score": 0.9},
        {"title": "T2", "url": "U2"},  # 缺 content：snippet 空串
    ]})
    monkeypatch.setenv("TAVILY_API_KEY", "secret")

    out = run(tavily_mod.TavilyEngine().search(SearchOptions("hello", count=5)))

    sent = _FakeClient.captured[-1]
    assert sent["url"] == "https://api.tavily.com/search"
    assert sent["headers"]["Authorization"] == "Bearer secret"
    assert sent["headers"]["Content-Type"] == "application/json"
    assert sent["json"] == {
        "query": "hello",
        "search_depth": "basic",
        "max_results": 5,
        "include_answer": False,
        "include_raw_content": False,
    }
    assert out[0].title == "T" and out[0].url == "U" and out[0].snippet == "C"
    assert out[1].snippet == ""  # 缺字段容忍


def test_max_results_capped_at_20(monkeypatch):
    _install(monkeypatch, data={"results": []})
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    run(tavily_mod.TavilyEngine().search(SearchOptions("q", count=50)))
    assert _FakeClient.captured[-1]["json"]["max_results"] == 20


# ── 2) 缺 key ─────────────────────────────────────────────────────────
def test_missing_key_search_raises(monkeypatch):
    _install(monkeypatch, data={"results": []})
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(EngineError) as ei:
        run(tavily_mod.TavilyEngine().search(SearchOptions("q")))
    assert "TAVILY_API_KEY not set" in str(ei.value)


def test_missing_key_health_fail(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    res = run(tavily_mod.TavilyEngine().health_check())
    assert res.status == "fail"
    assert res.repair
    assert res.evidence.get("env.TAVILY_API_KEY") == "missing"


def test_present_key_health_ok(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    res = run(tavily_mod.TavilyEngine().health_check())
    assert res.status == "ok"
    assert res.evidence.get("env.TAVILY_API_KEY") == "present"


# ── 3) 429 / 432 → RateLimitError ─────────────────────────────────────
@pytest.mark.parametrize("status", [429, 432])
def test_rate_limit_status_maps_to_ratelimiterror(monkeypatch, status):
    _install(monkeypatch, data={}, status_code=status)
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    with pytest.raises(RateLimitError) as ei:
        run(tavily_mod.TavilyEngine().search(SearchOptions("q")))
    assert str(status) in str(ei.value)


# ── 4) 其他 HTTP error 不伪成功 ───────────────────────────────────────
def test_other_http_error_raises_engineerror(monkeypatch):
    _install(monkeypatch, data={}, status_code=500)
    monkeypatch.setenv("TAVILY_API_KEY", "secret")
    with pytest.raises(EngineError) as ei:
        run(tavily_mod.TavilyEngine().search(SearchOptions("q")))
    # 不是 RateLimitError，是普通 EngineError
    assert not isinstance(ei.value, RateLimitError)
    assert "500" in str(ei.value)


def test_transport_error_fails_closed(monkeypatch):
    """连接层故障不得退化为空结果或伪成功。"""
    monkeypatch.setenv("TAVILY_API_KEY", "secret")

    class _TransportFailClient(_FakeClient):
        async def post(self, url, headers=None, json=None, timeout=None):
            req = httpx.Request("POST", url)
            raise httpx.ConnectError("offline", request=req)

    monkeypatch.setattr(tavily_mod.httpx, "AsyncClient", _TransportFailClient)
    with pytest.raises(EngineError, match="Tavily transport error") as ei:
        run(tavily_mod.TavilyEngine().search(SearchOptions("q")))
    assert not isinstance(ei.value, RateLimitError)


# ── 5) default_registry 含 tavily ─────────────────────────────────────
def test_default_registry_contains_tavily():
    reg = default_registry()
    eng = reg.get("tavily")
    assert eng is not None
    assert eng.name == "tavily"
    assert eng.tier == 1
