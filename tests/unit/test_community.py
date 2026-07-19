"""CommunityEngine 单测：评分/去重/源选择（纯）+ search 聚合（mock 子进程）。

通过 monkeypatch 模块级 `_run_cmd` 注入各源的 canned 输出，零真实子进程/网络。
"""
import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from wrr.engines import community as cm
from wrr.schemas import SearchOptions
from wrr.errors import EngineError
from wrr.router import build_chain
from wrr import config


def run(coro):
    return asyncio.run(coro)


# ── 纯评分函数 ───────────────────────────────────────────────────────
def test_score_weights():
    assert config.COMMUNITY_SCORE_WEIGHTS == (0.40, 0.35, 0.25)


def test_engagement_log_compression():
    assert cm._engagement_score(0) == 0.0
    assert 0.0 < cm._engagement_score(100, 1000) < 1.0
    assert cm._engagement_score(1000, 1000) == 1.0          # 命中上限
    assert cm._engagement_score(10**9, 1000) == 1.0         # 钳位


def test_recency_steps():
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    mk = lambda h: now - timedelta(hours=h)
    assert cm._recency_score(mk(1), now) == 1.0
    assert cm._recency_score(mk(24), now) == 1.0
    assert cm._recency_score(mk(100), now) == 0.7           # ≤7d
    assert cm._recency_score(mk(500), now) == 0.3           # ≤30d
    assert cm._recency_score(mk(2000), now) == 0.0          # 更旧
    assert cm._recency_score(None, now) == 0.5              # 未知→中等


def test_recency_score_created_defensive():
    """_recency_score() 对 created 参数做防御归一化（float/int/naive datetime）。"""
    now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    # float epoch (2h ago)
    created_float = (now - timedelta(hours=2)).timestamp()
    assert cm._recency_score(created_float, now) == 1.0
    # int epoch millis (1h ago)
    created_int = int((now - timedelta(hours=1)).timestamp() * 1000)
    assert cm._recency_score(created_int, now) == 1.0
    # naive datetime (3h ago)
    created_naive = now.replace(tzinfo=None) - timedelta(hours=3)
    assert cm._recency_score(created_naive, now) == 1.0
    # overflow epoch → 0.5
    assert cm._recency_score(1e20, now) == 0.5


def test_quality_ratio():
    assert cm._quality_score(0, 100) == 0.0
    assert cm._quality_score(10, 0) == 0.0                  # 无互动→0
    assert cm._quality_score(20, 100) == 1.0               # 20% → 1.0
    assert cm._quality_score(2, 100) == 0.1


def test_parse_time_epoch_iso_and_search_date_formats():
    assert cm._parse_time(1778165271).year >= 2026          # epoch 秒
    assert cm._parse_time(1778165271000).year >= 2026       # epoch 毫秒
    dt = cm._parse_time("2026-06-01T00:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None

    relative_before = datetime.now(timezone.utc)
    relative = cm._parse_time("2 hours ago")
    relative_after = datetime.now(timezone.utc)
    assert relative is not None
    assert relative_before - timedelta(hours=2, seconds=1) <= relative <= relative_after - timedelta(hours=2)

    assert cm._parse_time("Apr 28, 2026") == datetime(2026, 4, 28, tzinfo=timezone.utc)
    assert cm._parse_time("12/01/2024") == datetime(2024, 12, 1, tzinfo=timezone.utc)
    assert cm._parse_time("Apr 28") is None                # 不猜测缺失年份

    # naive / aware subtract must not raise (P3 RSS regression)
    now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    assert cm._recency_score(dt, now) >= 0.0
    assert cm._recency_score(datetime(2099, 1, 1, tzinfo=timezone.utc), now) == 1.0
    assert cm._parse_time("garbage") is None
    assert cm._parse_time(None) is None


def test_calculate_score_formula():
    cfg = cm.COMMUNITY_SOURCES["reddit"]
    now = datetime.now(timezone.utc)
    recent = int(now.timestamp()) - 3600
    item = {"score": 1000, "comments": 200, "created_utc": recent}
    # eng=log10(1001)/log10(10001)=0.75; rec=1.0; qual=min(1,200/1000*5)=1.0
    expected = 0.40 * cm._engagement_score(1000, 10000) + 0.35 * 1.0 + 0.25 * 1.0
    assert abs(cm.calculate_score(item, cfg, now) - expected) < 1e-9


# ── 去重 ─────────────────────────────────────────────────────────────
def test_dedup_url_and_title():
    from wrr.schemas import SearchResult
    rs = [
        SearchResult("Python is great", "https://a.com/x?utm=1", "", source_tag="reddit"),
        SearchResult("totally different", "https://a.com/x", "", source_tag="twitter"),  # URL 规范化后同
        SearchResult("Python is great!!", "https://b.com/y", "", source_tag="hn"),       # 标题高度相似
        SearchResult("unique topic here", "https://c.com/z", "", source_tag="v2ex"),
    ]
    out = cm.deduplicate(rs)
    urls = [r.url for r in out]
    assert "https://a.com/x?utm=1" in urls
    assert "https://a.com/x" not in urls          # URL 规范化去重
    assert "https://b.com/y" not in urls          # 标题相似去重
    assert "https://c.com/z" in urls


# ── 源选择 ───────────────────────────────────────────────────────────
def test_detect_sources_default_and_triggers():
    eng = cm.CommunityEngine()
    assert eng._detect_sources("python") == list(config.COMMUNITY_DEFAULT_SOURCES)
    assert eng._detect_sources("x site:reddit.com") == ["reddit"]
    assert eng._detect_sources("y site:x.com") == ["twitter"]
    assert eng._detect_sources("z site:news.ycombinator.com") == ["hackernews"]
    assert eng._detect_sources("w site:zhihu.com") == ["last30days_cn"]
    assert eng._detect_sources("site:v2ex.com python") == []
    assert "xiaohongshu" in eng._detect_sources("小红书 美食")


def test_detect_sources_last30days_gated():
    eng = cm.CommunityEngine()
    # 研究意图关键词 → 追加 last30days
    s = eng._detect_sources("trending ai")
    assert "last30days_en" in s and "last30days_cn" in s
    # 显式开关
    config.COMMUNITY_INCLUDE_LAST30DAYS = True
    try:
        s2 = eng._detect_sources("python")
        assert "last30days_en" in s2
    finally:
        config.COMMUNITY_INCLUDE_LAST30DAYS = False


def test_community_triggered():
    assert config.community_triggered("foo site:reddit.com")
    assert config.community_triggered("X SITE:X.COM")
    assert not config.community_triggered("site:v2ex.com python")
    assert not config.community_triggered("plain query")


# ── search 聚合（mock 子进程）────────────────────────────────────────
def _recent_epoch(h=1):
    return int(time.time()) - h * 3600


_REDDIT = [
    {"title": "Reddit A", "url": "https://reddit.com/r/p/comments/1/a",
     "score": 500, "comments": 100, "created_utc": _recent_epoch(2), "selftext": "body a"},
    {"title": "Reddit B", "url": "https://reddit.com/r/p/comments/2/b",
     "score": 3, "comments": 0, "created_utc": int(time.time()) - 100 * 86400, "selftext": ""},
]
_TWITTER = [
    {"text": "tweet about python", "url": "https://x.com/u/1",
     "likes": 1000, "replies": 50, "created_at": _recent_epoch(1)},
]
_L30 = {"clusters": [
    {"title": "cluster py", "score": 42.0, "sources": ["x", "reddit"],
     "representative_ids": ["https://di.gg/ai/m1"]},
]}
_L30_CN = {
    "topic": "zhihu topic",
    "bilibili": [
        {"title": "中文视频", "url": "https://www.bilibili.com/video/BV1", "description": "desc", "score": 25}
    ],
    "zhihu": [
        {"title": "知乎回答", "url": "https://www.zhihu.com/question/1", "snippet": "answer", "relevance": 0.8}
    ],
}


def _fake_run_factory(mapping, fail=()):
    async def fake_run(cli, timeout):
        joined = " ".join(cli).lower()
        if cli[:3] == ["opencli", "daemon", "status"]:
            return (0, "Daemon: running on port 19825\nExtension: connected", "")
        for key, payload in mapping.items():
            if key in joined:
                if key in fail:
                    return (None, "", "")           # 模拟该源失败
                return (0, json.dumps(payload), "")
        return (0, "[]", "")
    return fake_run


def test_search_aggregates_scores_and_dedups(monkeypatch=None):
    orig = cm._run_cmd
    cm._run_cmd = _fake_run_factory(
        {"reddit": _REDDIT, "twitter": _TWITTER, "xiaohongshu": [], "v2ex": []},
        fail=("v2ex",))
    try:
        out = run(cm.CommunityEngine().search(SearchOptions("python", count=10)))
    finally:
        cm._run_cmd = orig
    titles = [r.title for r in out]
    assert set(titles) == {"Reddit A", "tweet about python", "Reddit B"}
    assert titles.index("Reddit A") < titles.index("Reddit B")     # 高分在前
    tags = {r.source_tag for r in out}
    assert tags == {"reddit", "twitter"}                            # v2ex 失败被跳过
    assert all(r.url for r in out)


def test_search_respects_count():
    orig = cm._run_cmd
    cm._run_cmd = _fake_run_factory({"reddit": _REDDIT, "twitter": _TWITTER,
                                     "xiaohongshu": [], "v2ex": []})
    try:
        out = run(cm.CommunityEngine().search(SearchOptions("python", count=1)))
    finally:
        cm._run_cmd = orig
    assert len(out) == 1


def test_search_all_empty_raises():
    orig = cm._run_cmd
    cm._run_cmd = _fake_run_factory({"reddit": [], "twitter": [],
                                     "xiaohongshu": [], "v2ex": []})
    try:
        run(cm.CommunityEngine().search(SearchOptions("python")))
        assert False, "all empty should raise"
    except EngineError as e:
        assert "community" in str(e).lower()
    finally:
        cm._run_cmd = orig


def test_search_unsupported_v2ex_points_to_web_engine():
    try:
        run(cm.CommunityEngine().search(SearchOptions("site:v2ex.com python")))
        assert False, "unsupported v2ex search should raise"
    except EngineError as e:
        msg = str(e).lower()
        assert "v2ex" in msg
        assert "site:v2ex.com" in msg
        assert "external web engine" in msg


def test_search_last30days_clusters_mapped():
    orig = cm._run_cmd
    cm._run_cmd = _fake_run_factory({"last30days": _L30})
    config.COMMUNITY_INCLUDE_LAST30DAYS = True
    try:
        # trending AI → last30days_en/cn
        out = run(cm.CommunityEngine().search(
            SearchOptions("trending AI", count=5)))
    finally:
        cm._run_cmd = orig
        config.COMMUNITY_INCLUDE_LAST30DAYS = False
    assert len(out) == 1
    assert out[0].title == "cluster py"
    assert out[0].url == "https://di.gg/ai/m1"
    assert out[0].source_tag == "last30days_en"


def test_search_last30days_mixed_stdout_and_cn_platform_arrays_mapped():
    orig = cm._run_cmd

    async def fake_run(cli, timeout):
        if cli[:3] == ["opencli", "daemon", "status"]:
            return (0, "Daemon: running on port 19825\nExtension: connected", "")
        return (0, "正在搜索...\n" + json.dumps(_L30_CN, ensure_ascii=False), "")

    cm._run_cmd = fake_run
    try:
        out = run(cm.CommunityEngine().search(
            SearchOptions("ai site:zhihu.com", count=5)))
    finally:
        cm._run_cmd = orig
    assert len(out) == 2
    assert {r.title for r in out} == {"中文视频", "知乎回答"}
    assert {r.source_tag for r in out} == {"last30days_cn"}


def test_item_to_result_drops_incomplete():
    eng = cm.CommunityEngine()
    cfg = cm.COMMUNITY_SOURCES["reddit"]
    now = datetime.now(timezone.utc)
    assert eng._item_to_result({"title": "", "url": "https://x"}, "reddit", cfg, now) is None
    assert eng._item_to_result({"title": "t", "url": ""}, "reddit", cfg, now) is None
    ok = eng._item_to_result({"title": "t", "url": "https://x", "score": 10,
                              "comments": 1, "created_utc": _recent_epoch()}, "reddit", cfg, now)
    assert ok is not None and ok[1].source_tag == "reddit"


def test_search_does_not_probe_or_restart_opencli_daemon():
    """H3 refactor: search 热路径不再运行 daemon status probe 或 restart。"""
    recorded: list = []
    orig = cm._run_cmd

    async def guard(cli, timeout):
        if cli[:3] == ["opencli", "daemon", "restart"]:
            raise AssertionError("search hot path must not run opencli daemon restart")
        if cli[:3] == ["opencli", "daemon", "status"]:
            raise AssertionError("search hot path must not run opencli daemon status")
        recorded.append(cli)
        joined = " ".join(cli).lower()
        if "reddit" in joined:
            return (0, json.dumps(_REDDIT[:1]), "")
        return (0, "[]", "")

    cm._run_cmd = guard
    try:
        out = run(cm.CommunityEngine().search(
            SearchOptions("python site:reddit.com", count=5)))
    finally:
        cm._run_cmd = orig
    assert len(out) == 1
    # 验证没有调用任何 daemon 相关命令
    assert all(cli[:2] != ["opencli", "daemon"] for cli in recorded)


# ── 源适配器 seam（Slice 1）─────────────────────────────────────────
def test_community_sources_module_exposes_adapters():
    from wrr.engines import community_sources as cs
    assert hasattr(cs, "CommunitySourceAdapter")
    assert hasattr(cs, "OpenCliSourceAdapter")
    assert hasattr(cs, "Last30DaysSourceAdapter")


def test_opencli_adapter_backup_command_and_query_filter():
    from wrr.engines import community_sources as cs
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        if "top" in cli:
            return (0, json.dumps({"results": [
                {"title": "AI dominates HN", "url": "https://a"},
                {"title": "Unrelated show", "url": "https://b"},
            ]}), "")
        return (1, "", "timeout")

    cfg = {"cli": ["opencli", "hackernews", "search"], "backup_commands": [["opencli", "hackernews", "top"]], "backup_filter_by_query": True}
    items = run(cs.OpenCliSourceAdapter().fetch(
        cfg, SearchOptions("AI", count=5), fake_run, 1.0))
    assert len(calls) == 2
    assert calls[0] == ["opencli", "hackernews", "search", "AI", "-f", "json", "--limit", "5"]
    assert calls[1] == ["opencli", "hackernews", "top", "-f", "json", "--limit", "15"]
    assert len(items) == 1
    assert items[0]["title"] == "AI dominates HN"


def test_opencli_adapter_backup_skipped_when_no_query():
    from wrr.engines import community_sources as cs
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "timeout")

    cfg = {"cli": ["opencli", "hackernews", "search"], "backup_commands": [["opencli", "hackernews", "top"]]}
    items = run(cs.OpenCliSourceAdapter().fetch(
        cfg, SearchOptions("", count=5), fake_run, 1.0))
    assert len(calls) == 1
    assert items == []


def test_opencli_adapter_preserves_command_and_parsing():
    from wrr.engines import community_sources as cs
    captured = {}

    async def fake_run(cli, timeout):
        captured["cli"] = cli
        captured["timeout"] = timeout
        return (0, json.dumps(_REDDIT), "")

    items = run(cs.OpenCliSourceAdapter().fetch(
        cm.COMMUNITY_SOURCES["reddit"], SearchOptions("python", count=5),
        fake_run, 9.9))
    assert captured["cli"] == [
        "opencli", "reddit", "search", "python", "-f", "json", "--limit", "5"]
    assert captured["timeout"] == 9.9
    assert items == _REDDIT


def test_opencli_adapter_caps_limit_at_20():
    from wrr.engines import community_sources as cs
    captured = {}

    async def fake_run(cli, timeout):
        captured["cli"] = cli
        return (0, json.dumps({"results": _REDDIT}), "")

    items = run(cs.OpenCliSourceAdapter().fetch(
        cm.COMMUNITY_SOURCES["reddit"], SearchOptions("python", count=100),
        fake_run, 1.0))
    assert captured["cli"][-1] == "20"
    assert items == _REDDIT               # dict {"results": [...]} 解包


def test_last30days_adapter_parses_mixed_stdout():
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        return (0, "正在搜索...\n" + json.dumps(_L30), "")

    items = run(cs.Last30DaysSourceAdapter().fetch(
        cm.COMMUNITY_SOURCES["last30days_en"], SearchOptions("ai", count=5),
        fake_run, 1.0))
    assert items and items[0]["title"] == "cluster py"
    assert items[0]["url"] == "https://di.gg/ai/m1"


def test_fetch_source_wired_to_adapter_registry():
    assert set(cm._SOURCE_ADAPTERS) >= {"opencli", "last30days"}
    from wrr.engines import community_sources as cs
    assert isinstance(cm._SOURCE_ADAPTERS["opencli"], cs.OpenCliSourceAdapter)
    assert isinstance(cm._SOURCE_ADAPTERS["last30days"], cs.Last30DaysSourceAdapter)


def test_fetch_source_rss_accepts_float_now_and_datetime_published_at():
    """P3 RSS regression: epoch float `now` must not break datetime RSS items."""
    eng = cm.CommunityEngine()
    fixed_now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

    class FakeRssAdapter:
        async def fetch(self, cfg, options, run_cmd, timeout):
            return [{
                "title": "AI 热点",
                "url": "https://example.com/ai-hot",
                "snippet": "rss item",
                "published_at": fixed_now - timedelta(hours=2),
            }]

    orig_adapter = cm._SOURCE_ADAPTERS["rss"]
    cm._SOURCE_ADAPTERS["rss"] = FakeRssAdapter()
    try:
        out = run(eng._fetch_source(
            "aihot_rss",
            SearchOptions("AI 热点", count=5),
            fixed_now.timestamp(),
        ))
    finally:
        cm._SOURCE_ADAPTERS["rss"] = orig_adapter

    assert len(out) == 1
    score, result = out[0]
    assert score > 0
    assert result.title == "AI 热点"
    assert result.source_tag == "aihot_rss"


def test_hackernews_item_without_time():
    """Hackernews source: time=None → recency=0.5, does not crash."""
    eng = cm.CommunityEngine()
    cfg = cm.COMMUNITY_SOURCES["hackernews"]
    now = datetime.now(timezone.utc)
    item = {
        "title": "HN story without timestamp",
        "url": "https://news.ycombinator.com/item?id=12345",
        "score": 100,
        "comments": 50,
    }
    scored = eng._item_to_result(item, "hackernews", cfg, now)
    assert scored is not None
    score, result = scored
    assert result.title == "HN story without timestamp"
    assert result.url == "https://news.ycombinator.com/item?id=12345"
    assert result.source_tag == "hackernews"
    assert score > 0  # 应该有非零分数 (engagement + 0.5*recency + quality)
    # 验证 recency 部分确实使用了 0.5
    expected_recency = 0.5
    w_e, w_r, w_q = config.COMMUNITY_SCORE_WEIGHTS
    expected_score = (
        w_e * cm._engagement_score(100, 1000) +
        w_r * expected_recency +
        w_q * cm._quality_score(50, 100)
    )
    assert abs(score - expected_score) < 1e-9


def test_hackernews_item_with_enriched_time_uses_recency():
    """Firebase 回填的 HN Unix time 必须实际进入统一评分。"""
    eng = cm.CommunityEngine()
    cfg = cm.COMMUNITY_SOURCES["hackernews"]
    now = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
    item = {
        "title": "HN story with timestamp",
        "url": "https://news.ycombinator.com/item?id=12346",
        "score": 100,
        "comments": 50,
        "time": int((now - timedelta(hours=2)).timestamp()),
    }
    scored = eng._item_to_result(item, "hackernews", cfg, now)
    assert scored is not None
    score, _result = scored
    w_e, w_r, w_q = config.COMMUNITY_SCORE_WEIGHTS
    expected_score = (
        w_e * cm._engagement_score(100, 1000)
        + w_r * 1.0
        + w_q * cm._quality_score(50, 100)
    )
    assert abs(score - expected_score) < 1e-9


def test_hackernews_search_includes_sort_date():
    """HN primary search 命令应包含 --sort date 参数。"""
    from wrr.engines import community_sources as cs
    captured = {}

    async def fake_run(cli, timeout):
        captured["cli"] = cli
        return (0, json.dumps([{"title": "HN A", "url": "https://news.ycombinator.com/item?id=1", "score": 10}]), "")

    cfg = cm.COMMUNITY_SOURCES["hackernews"]
    items = run(cs.OpenCliSourceAdapter().fetch(
        cfg, SearchOptions("python", count=5), fake_run, 1.0))
    assert "--sort" in captured["cli"]
    assert "date" in captured["cli"]


def test_hackernews_time_enrichment_from_firebase():
    """HN 行应尝试从 Firebase API 回填 time 字段（best-effort）。"""
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        # opencli hackernews search 返回无 time 的 items
        return (0, json.dumps([
            {"id": 41000001, "title": "Story A", "score": 100},  # 真实 search 形状：无 url
            {"id": "invalid", "title": "Story B", "score": 50},  # 非法 id
            {"id": "41000003", "title": "Story C", "score": 30},
        ]), "")

    # mock httpx.AsyncClient.get
    class FakeResponse:
        def __init__(self, data):
            self._data = data
        def json(self):
            return self._data
        def raise_for_status(self):
            if self._data is None:
                raise Exception("404")

    class FakeClient:
        def __init__(self):
            self.requests = []
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, timeout=None):
            self.requests.append(url)
            item_id = url.split("/")[-1].replace(".json", "")
            if item_id == "41000001":
                return FakeResponse({"time": 1720000000})  # Unix 秒
            elif item_id == "41000003":
                return FakeResponse({"time": 1720001000})
            return FakeResponse(None)  # 模拟 404

    fake_client = FakeClient()

    import wrr.engines.community_sources as cs_module
    orig_httpx = cs_module.httpx
    try:
        # 注入 fake httpx
        class FakeHttpx:
            AsyncClient = lambda *args, **kwargs: fake_client
        cs_module.httpx = FakeHttpx()

        cfg = cm.COMMUNITY_SOURCES["hackernews"]
        items = run(cs.OpenCliSourceAdapter().fetch(
            cfg, SearchOptions("python", count=5), fake_run, 2.0))
    finally:
        cs_module.httpx = orig_httpx

    assert len(items) == 3
    assert items[0]["url"] == "https://news.ycombinator.com/item?id=41000001"
    assert items[2]["url"] == "https://news.ycombinator.com/item?id=41000003"
    # 检查 time 是否已回填
    assert items[0].get("time") == 1720000000  # Story A 成功回填
    assert "time" not in items[1]              # Story B 无效 id,保持无 time
    assert items[2].get("time") == 1720001000  # Story C 成功回填
    # 验证请求了正确的 Firebase URL
    assert len(fake_client.requests) == 2  # 只请求了两个有效 id
    assert "41000001" in fake_client.requests[0]
    assert "41000003" in fake_client.requests[1]


def test_hackernews_time_enrichment_timeout_bounded():
    """HN Firebase 回填应有超时限制,避免阻塞整个搜索。"""
    from wrr.engines import community_sources as cs
    import time

    async def fake_run(cli, timeout):
        return (0, json.dumps([
            {"title": "Story", "url": "https://news.ycombinator.com/item?id=41000001", "score": 100},
        ]), "")

    class SlowClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get(self, url, timeout=None):
            await asyncio.sleep(10)  # 模拟超时
            return None

    import wrr.engines.community_sources as cs_module
    orig_httpx = cs_module.httpx
    try:
        class FakeHttpx:
            AsyncClient = lambda *args, **kwargs: SlowClient()
        cs_module.httpx = FakeHttpx()

        cfg = cm.COMMUNITY_SOURCES["hackernews"]
        start = time.time()
        items = run(cs.OpenCliSourceAdapter().fetch(
            cfg, SearchOptions("python", count=5), fake_run, 2.0))
        elapsed = time.time() - start
    finally:
        cs_module.httpx = orig_httpx

    # 应在 3s 内完成（超时后立即返回,不等待 10s）
    assert elapsed < 3.0
    # 超时不影响返回（保持原始 item）
    assert len(items) == 1
    assert "time" not in items[0]


# ── 自动触发链 ───────────────────────────────────────────────────────
def test_build_chain_promotes_community():
    assert build_chain("search", None, "x site:reddit.com") == \
        ["community", "exa", "brave", "github", "searxng"]
    assert build_chain("search", None, "plain") == \
        ["exa", "brave", "github", "community", "searxng"]


# ── OpenCLI 断开快速失败（端到端时延）───────────────────────────────────
def test_opencli_disconnect_no_longer_fails_fast_in_search():
    """H3 refactor: search 热路径不再预检 opencli，即使断开也会尝试 fetch。

    Daemon/extension 健康检查由 health_check() 负责，search 路径不再快速失败。
    如果 opencli 实际断开，fetch 会因超时返回空结果（由 adapter 捕获异常）。
    """
    eng = cm.CommunityEngine()

    async def fake_probe(timeout):
        # 即使 probe 返回失败，search 也不再调用它
        raise AssertionError("_probe_opencli_status should not be called in search")

    async def fake_run(cli, timeout):
        # 模拟 opencli search 实际执行但失败（返回空）
        return (0, "[]", "")

    orig_probe = cm._probe_opencli_status
    orig_run = cm._run_cmd
    cm._probe_opencli_status = fake_probe
    cm._run_cmd = fake_run
    try:
        # 不再抛出 EngineError，而是返回空结果
        res = run(eng._fetch_source("reddit", SearchOptions("python", count=5), datetime.now(timezone.utc)))
        assert res == []  # fetch 返回空（adapter 捕获了失败）
    finally:
        cm._probe_opencli_status = orig_probe
        cm._run_cmd = orig_run


def test_opencli_fetch_without_probe():
    """H3 refactor: search 路径直接 fetch，不再预检 probe。"""
    eng = cm.CommunityEngine()

    async def fake_probe(timeout):
        raise AssertionError("_probe_opencli_status should not be called in search")

    async def fake_run(cli, timeout):
        return (0, json.dumps(_REDDIT), "")

    orig_probe = cm._probe_opencli_status
    orig_run = cm._run_cmd
    cm._probe_opencli_status = fake_probe
    cm._run_cmd = fake_run
    try:
        res = run(eng._fetch_source("reddit", SearchOptions("python", count=5), datetime.now(timezone.utc)))
    finally:
        cm._probe_opencli_status = orig_probe
        cm._run_cmd = orig_run

    assert len(res) == len(_REDDIT)


def test_fetch_source_no_opencli_preflight():
    """H3 refactor: search 热路径不再调用 _probe_opencli_status preflight。"""
    eng = cm.CommunityEngine()

    async def probe_guard(timeout):
        raise AssertionError("_probe_opencli_status must not be called in search hot path")

    async def fake_run(cli, timeout):
        return (0, json.dumps(_REDDIT), "")

    orig_probe = cm._probe_opencli_status
    orig_run = cm._run_cmd
    cm._probe_opencli_status = probe_guard
    cm._run_cmd = fake_run
    try:
        # 如果 _fetch_source 仍调用 probe，会触发 AssertionError
        res = run(eng._fetch_source("reddit", SearchOptions("python", count=5), datetime.now(timezone.utc)))
        assert len(res) == len(_REDDIT)  # 确认正常获取数据
    finally:
        cm._probe_opencli_status = orig_probe
        cm._run_cmd = orig_run


# ── P0 reliability slice：SourceOutcome 四态分类 ─────────────────────
def _oc_cfg():
    return {"cli": ["opencli", "reddit", "search"]}


def test_source_outcome_and_result_wrapper_exposed():
    from wrr.engines import community_sources as cs
    assert hasattr(cs, "SourceOutcome")
    assert hasattr(cs, "SourceFetchResult")
    # 四态齐备
    for name in ("READY", "EMPTY", "UNAUTHENTICATED", "SOFT_BLOCKED"):
        assert hasattr(cs.SourceOutcome, name)
    r = cs.SourceFetchResult(items=[{"a": 1}], outcome=cs.SourceOutcome.READY)
    assert r.items == [{"a": 1}]
    assert r.outcome is cs.SourceOutcome.READY


def test_opencli_outcome_ready():
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        return (0, json.dumps(_REDDIT), "")

    res = run(cs.OpenCliSourceAdapter().fetch_result(
        _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.READY
    assert res.items == _REDDIT


def test_opencli_outcome_empty_success_no_results():
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        return (0, "[]", "")

    res = run(cs.OpenCliSourceAdapter().fetch_result(
        _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.EMPTY
    assert res.items == []


def test_opencli_outcome_unauthenticated_login_wall():
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        return (1, "", "AUTH_REQUIRED: login wall, please log in to continue")

    res = run(cs.OpenCliSourceAdapter().fetch_result(
        _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.UNAUTHENTICATED
    assert res.items == []


def test_opencli_outcome_soft_blocked_captcha_and_403():
    from wrr.engines import community_sources as cs

    for stderr in ("HTTP 403 Forbidden", "captcha challenge presented",
                   "验证码 required", "429 Too Many Requests (rate limit)"):
        async def fake_run(cli, timeout, _e=stderr):
            return (1, "", _e)

        res = run(cs.OpenCliSourceAdapter().fetch_result(
            _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
        assert res.outcome is cs.SourceOutcome.SOFT_BLOCKED, stderr
        assert res.items == []


def test_opencli_ready_json_payload_not_misclassified_as_block():
    """成功 JSON 载荷即使内容含 403/forbidden 字样也不得误判为 soft_blocked。"""
    from wrr.engines import community_sources as cs
    payload = [{"title": "Error 403 forbidden explained", "url": "https://x/1",
                "score": 10}]

    async def fake_run(cli, timeout):
        return (0, json.dumps(payload), "")

    res = run(cs.OpenCliSourceAdapter().fetch_result(
        _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.READY
    assert res.items == payload


def test_opencli_unauthenticated_short_circuits_backup():
    """认证墙必须立即返回，不再尝试 backup 命令。"""
    from wrr.engines import community_sources as cs
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "login required")

    cfg = {"cli": ["opencli", "hackernews", "search"],
           "backup_commands": [["opencli", "hackernews", "top"]]}
    res = run(cs.OpenCliSourceAdapter().fetch_result(
        cfg, SearchOptions("AI", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.UNAUTHENTICATED
    assert len(calls) == 1                      # backup 未尝试


def test_opencli_softblock_short_circuits_backup():
    from wrr.engines import community_sources as cs
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "HTTP 403 Forbidden")

    cfg = {"cli": ["opencli", "hackernews", "search"],
           "backup_commands": [["opencli", "hackernews", "top"]]}
    res = run(cs.OpenCliSourceAdapter().fetch_result(
        cfg, SearchOptions("AI", count=5), fake_run, 1.0))
    assert res.outcome is cs.SourceOutcome.SOFT_BLOCKED
    assert len(calls) == 1


def test_opencli_plain_empty_still_tries_backup():
    """普通 empty（无阻断标记）仍走既有 backup 逻辑。"""
    from wrr.engines import community_sources as cs
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        if "top" in cli:
            return (0, json.dumps([{"title": "T", "url": "https://a"}]), "")
        return (0, "[]", "")

    cfg = {"cli": ["opencli", "hackernews", "search"],
           "backup_commands": [["opencli", "hackernews", "top"]]}
    res = run(cs.OpenCliSourceAdapter().fetch_result(
        cfg, SearchOptions("AI", count=5), fake_run, 1.0))
    assert len(calls) == 2
    assert res.outcome is cs.SourceOutcome.READY
    assert res.items and res.items[0]["title"] == "T"


def test_opencli_fetch_still_returns_plain_list():
    """向后兼容：fetch() 仍返回裸 item 列表。"""
    from wrr.engines import community_sources as cs

    async def fake_run(cli, timeout):
        return (0, json.dumps(_REDDIT), "")

    items = run(cs.OpenCliSourceAdapter().fetch(
        _oc_cfg(), SearchOptions("python", count=5), fake_run, 1.0))
    assert items == _REDDIT


# ── P0 reliability slice：source-local TTL breaker ──────────────────
class _FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _install_run(fn):
    orig = cm._run_cmd
    cm._run_cmd = fn
    return orig


def test_breaker_opens_on_unauthenticated_and_skips_within_ttl():
    clock = _FakeClock()
    eng = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "AUTH_REQUIRED login wall")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        r1 = run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert r1 == []
        n1 = len(calls)
        assert n1 >= 1
        # TTL 未过 → skip，不再调用 adapter
        r2 = run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert r2 == []
        assert len(calls) == n1
    finally:
        cm._run_cmd = orig


def test_breaker_opens_on_soft_blocked():
    clock = _FakeClock()
    eng = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "captcha / 403 forbidden")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        n1 = len(calls)
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert len(calls) == n1                 # skipped within TTL
    finally:
        cm._run_cmd = orig


def test_breaker_expires_after_ttl_allows_call():
    clock = _FakeClock()
    eng = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (1, "", "login required")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        n1 = len(calls)
        clock.t += 60.0                         # TTL 到期
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert len(calls) == n1 + 1             # 允许再次调用
    finally:
        cm._run_cmd = orig


def test_breaker_cleared_on_ready_outcome():
    clock = _FakeClock()
    eng = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
    state = {"mode": "block"}

    async def fake_run(cli, timeout):
        if state["mode"] == "block":
            return (1, "", "login required")
        return (0, json.dumps(_REDDIT), "")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert eng._breaker_open_until.get("reddit") is not None
        clock.t += 60.0                         # 到期允许调用
        state["mode"] = "ready"
        r = run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert r                                 # 拿到结果
        assert eng._breaker_open_until.get("reddit") is None   # ready 清除
    finally:
        cm._run_cmd = orig


def test_breaker_not_opened_on_empty_outcome():
    clock = _FakeClock()
    eng = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
    calls = []

    async def fake_run(cli, timeout):
        calls.append(cli)
        return (0, "[]", "")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert "reddit" not in eng._breaker_open_until
        # empty 不开闸：第二次仍会实际调用
        run(eng._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert len(calls) == 2
    finally:
        cm._run_cmd = orig


def test_breaker_isolated_per_source_and_per_engine():
    clock = _FakeClock()
    engA = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)

    async def fake_run(cli, timeout):
        joined = " ".join(cli)
        if "reddit" in joined:
            return (1, "", "AUTH_REQUIRED")
        return (0, json.dumps(_TWITTER), "")

    orig = _install_run(fake_run)
    now = datetime.now(timezone.utc)
    try:
        run(engA._fetch_source("reddit", SearchOptions("python", count=5), now))
        assert engA._breaker_open_until.get("reddit") is not None
        # 同一 engine 的其它 source 不受影响
        r_tw = run(engA._fetch_source("twitter", SearchOptions("python", count=5), now))
        assert r_tw
        assert "twitter" not in engA._breaker_open_until
        # 另一 engine 实例 breaker 独立，无全局污染
        engB = cm.CommunityEngine(clock=clock, breaker_ttl=60.0)
        assert engB._breaker_should_skip("reddit") is False
        assert engB._breaker_open_until == {}
    finally:
        cm._run_cmd = orig


def test_engine_default_clock_is_monotonic_and_no_kwargs_ok():
    eng = cm.CommunityEngine()
    assert callable(eng._clock)
    assert isinstance(eng._clock(), float)
    assert eng._breaker_open_until == {}


# ── P0 reliability slice：_run_cmd timeout kill + reaping ───────────
def test_run_cmd_timeout_kills_then_reaps_process():
    events = []

    class FakeProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)             # 会被 wait_for 超时取消
            return (b"", b"")

        def kill(self):
            events.append("kill")

        async def wait(self):
            events.append("wait")
            return -9

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    orig = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = fake_exec
    try:
        rc, out, err = run(cm._run_cmd(["opencli", "reddit", "search"], 0.01))
    finally:
        asyncio.create_subprocess_exec = orig
    assert (rc, out, err) == (None, "", "")      # 外部契约不变
    assert events == ["kill", "wait"]            # kill 之后 await 回收


def test_health_check_uses_probe_opencli_status():
    """health_check() 仍然调用 _probe_opencli_status 进行健康检查。"""
    probe_called = []

    async def fake_probe(timeout):
        probe_called.append(timeout)
        return (True, "opencli ready")

    orig_probe = cm._probe_opencli_status
    cm._probe_opencli_status = fake_probe
    try:
        import shutil
        if shutil.which("opencli"):  # 只在 opencli 存在时测试
            result = run(cm.CommunityEngine().health_check(deep=False))
            assert len(probe_called) > 0, "health_check should call _probe_opencli_status"
            assert probe_called[0] == 1.0  # timeout=1.0
    finally:
        cm._probe_opencli_status = orig_probe
