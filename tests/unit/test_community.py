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


def test_quality_ratio():
    assert cm._quality_score(0, 100) == 0.0
    assert cm._quality_score(10, 0) == 0.0                  # 无互动→0
    assert cm._quality_score(20, 100) == 1.0               # 20% → 1.0
    assert cm._quality_score(2, 100) == 0.1


def test_parse_time_epoch_and_iso():
    assert cm._parse_time(1778165271).year >= 2026          # epoch 秒
    assert cm._parse_time(1778165271000).year >= 2026       # epoch 毫秒
    dt = cm._parse_time("2026-06-01T00:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    # naive / aware subtract must not raise (P3 RSS regression)
    now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    assert cm._recency_score(dt, now) >= 0.0
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
    assert eng._detect_sources("z site:news.ycombinator.com") == ["last30days_en"]
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
        # site:news.ycombinator.com → 仅 last30days_en
        out = run(cm.CommunityEngine().search(
            SearchOptions("ai site:news.ycombinator.com", count=5)))
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
