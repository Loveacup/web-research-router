"""DeepSeekEngine 请求 payload 与响应映射单测（fake httpx，无网络）。"""
import asyncio
import os

import pytest
from conftest import FakeAsyncClient
from wrr.engines import deepseek as ds_mod
from wrr.errors import EngineError
from wrr.schemas import SearchOptions
from wrr import config


def _patch(monkey_response, env="k"):
    FakeAsyncClient.captured = []
    FakeAsyncClient.response_data = monkey_response
    ds_mod.httpx.AsyncClient = FakeAsyncClient
    os.environ["DEEPSEEK_API_KEY"] = env


def run(coro):
    return asyncio.run(coro)


def test_deepseek_search_payload_and_mapping():
    _patch({"results": [{
        "rerank_score": 2.4, "title": "T", "url": "U",
        "snippet": "short", "summary": "long summary " * 60, "content": "",
        "site_name": "S", "site_icon": None,
        "date": "2026-06-01T00:00:00+00:00", "authority": 3,
    }]})
    eng = ds_mod.DeepSeekEngine()
    out = run(eng.search(SearchOptions("hello world", count=5)))
    sent = FakeAsyncClient.captured[-1]
    assert sent["url"] == ds_mod.DEEPSEEK_SEARCH_URL
    assert sent["json"] == {"query": "hello world", "count": 5, "need_content": False}
    init_record = [r for r in FakeAsyncClient.captured if r.get("method") == "INIT"][0]
    assert init_record["kwargs"]["timeout"] == config.DEEPSEEK_TIMEOUT
    r = out[0]
    assert (r.title, r.url) == ("T", "U")
    assert r.snippet.startswith("long summary") and len(r.snippet) == config.EXA_SEARCH_TEXT_MAX
    assert r.highlights == []                      # 上游无独立 highlights，不复制 snippet
    assert r.source_ts == 1780272000.0             # 2026-06-01T00:00:00Z
    assert r.freshness_score == 1.0


def test_deepseek_null_date_and_snippet_fallback():
    _patch({"results": [{"title": "T", "url": "U", "snippet": "short", "summary": "",
                         "date": None, "authority": -1}]})
    out = run(ds_mod.DeepSeekEngine().search(SearchOptions("q")))
    assert out[0].snippet == "short"
    assert out[0].source_ts == 0.0
    assert out[0].freshness_score == 0.5


def test_deepseek_query_truncated_and_count_clamped():
    _patch({"results": []})
    long_q = "中" * 200
    out = run(ds_mod.DeepSeekEngine().search(SearchOptions(long_q, count=99)))
    sent = FakeAsyncClient.captured[-1]
    assert len(sent["json"]["query"]) == ds_mod.DEEPSEEK_QUERY_MAX
    assert sent["json"]["count"] == ds_mod.DEEPSEEK_COUNT_MAX
    assert out == []


def test_deepseek_missing_key_raises_before_request():
    _patch({"results": []}, env="")
    with pytest.raises(EngineError, match="DEEPSEEK_API_KEY"):
        run(ds_mod.DeepSeekEngine().search(SearchOptions("q")))
    assert FakeAsyncClient.captured == []
