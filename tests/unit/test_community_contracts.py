"""Community CLI wire contracts; subprocess/network calls are injected."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from wrr.engines import community as cm
from wrr.engines.community_sources import (
    Last30DaysSourceAdapter, OpenCliSourceAdapter, SourceOutcome,
)
from wrr.schemas import SearchOptions


def fetch(adapter, payload, *, cfg=None, rc=0, err="", query="python", count=5):
    calls = []

    async def runner(cli, timeout):
        calls.append(cli)
        return rc, json.dumps(payload), err

    result = asyncio.run(adapter.fetch_result(
        cfg or {"cli": ["tool", "search"]}, SearchOptions(query, count=count), runner, 1.0))
    return result, calls


@pytest.fixture
def en_config(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for source in ("devto", "techmeme"):
        (lib / f"{source}.py").write_text("# source adapter present\n")
    return dict(cm.COMMUNITY_SOURCES["last30days_en"],
                cli=["python3", str(tmp_path / "last30days.py")])


@pytest.mark.parametrize("wrap", [
    lambda rows: rows, lambda rows: {"results": rows}, lambda rows: {"items": rows},
    lambda rows: {"ok": True, "data": rows},
    lambda rows: {"ok": True, "data": {"results": rows}},
])
def test_opencli_unwraps_supported_versions(wrap):
    rows = [{"title": "403 forbidden is a story title", "url": "https://example.com/story"}]
    result, _ = fetch(OpenCliSourceAdapter(), wrap(rows))
    assert result.outcome is SourceOutcome.READY
    assert result.items == rows


@pytest.mark.parametrize("payload", [
    {}, {"ok": True}, {"ok": 1, "data": []}, {"ok": True, "data": None},
    {"results": {}}, {"items": [1]}, {"ok": False, "error": "bad"},
])
def test_opencli_malformed_payload_is_schema_error(payload):
    result, _ = fetch(OpenCliSourceAdapter(), payload)
    assert result.outcome is SourceOutcome.SCHEMA_ERROR
    assert result.items == []


@pytest.mark.parametrize("rc", [0, 1])
@pytest.mark.parametrize("code,expected", [
    ("AUTH_REQUIRED", SourceOutcome.UNAUTHENTICATED),
    ("LOGIN_WALL", SourceOutcome.UNAUTHENTICATED),
    ("RATE_LIMIT", SourceOutcome.SOFT_BLOCKED),
    ("BROWSER_CONNECT", SourceOutcome.BRIDGE_DISCONNECTED),
    ("TIMEOUT", SourceOutcome.TIMEOUT),
    ("SELECTOR", SourceOutcome.PLATFORM_ERROR),
    ("COMMAND_EXEC", SourceOutcome.ERROR),
])
def test_opencli_failure_envelopes_are_not_empty_success(rc, code, expected):
    cfg = {"cli": ["tool", "search"], "backup_commands": [["tool", "top"]]}
    result, calls = fetch(OpenCliSourceAdapter(),
                          {"ok": False, "error": {"code": code, "message": "failed"}},
                          cfg=cfg, rc=rc)
    assert result.outcome is expected
    assert result.items == []
    if expected in (SourceOutcome.UNAUTHENTICATED, SourceOutcome.SOFT_BLOCKED):
        assert len(calls) == 1


def test_last30days_agent_results_preferred_over_cluster_and_raw():
    payload = {
        "results": [{"candidate_id": "C1", "title": "Dev post", "url": "https://dev.to/a/post",
                     "summary": "Unverified source claim", "relevance_score": 0.8,
                     "published_at": "2026-10-17"}],
        "clusters": [{"title": "cluster", "candidate_ids": ["C1"]}],
        "reddit": [{"title": "older", "url": "https://reddit.com/r/a/comments/b"}],
    }
    result, _ = fetch(Last30DaysSourceAdapter(), payload)
    assert result.outcome is SourceOutcome.READY
    assert [row["url"] for row in result.items] == ["https://dev.to/a/post"]
    assert result.items[0]["snippet"] == "Unverified source claim"
    assert result.items[0]["published_at"] == "2026-10-17"
    assert result.items[0]["score"] == 80


@pytest.mark.parametrize("platform", ["reddit", "x", "youtube", "hackernews", "techmeme", "devto",
                                      "weibo", "zhihu", "bilibili", "xiaohongshu", "wechat"])
def test_last30days_legacy_platform_arrays(platform):
    result, _ = fetch(Last30DaysSourceAdapter(), {
        platform: [{"title": "post", "url": "https://example.com/post", "snippet": "source text"}],
    })
    assert result.outcome is SourceOutcome.READY
    assert result.items[0]["url"] == "https://example.com/post"


def test_last30days_resolves_cluster_ids_against_candidates_not_urls():
    result, _ = fetch(Last30DaysSourceAdapter(), {
        "clusters": [{"title": "cluster", "representative_ids": ["missing", "C1"]}],
        "ranked_candidates": [{"candidate_id": "C1", "item_id": "R1", "title": "candidate",
                               "url": "https://example.com/post", "snippet": "source text"}],
    })
    assert result.outcome is SourceOutcome.READY
    assert result.items[0]["url"] == "https://example.com/post"
    assert result.items[0]["snippet"] == "source text"


@pytest.mark.parametrize("url", ["C1", "javascript:alert(1)", "file:///tmp/x", "//example.com/post", "https:///missing"])
def test_last30days_never_exports_non_http_urls(url):
    result, _ = fetch(Last30DaysSourceAdapter(), {"results": [{"title": "post", "url": url}]})
    assert result.items == []
    assert result.outcome is SourceOutcome.SCHEMA_ERROR


@pytest.mark.parametrize("payload", [[], {}, {"results": {}}, {"results": [1]}, {"clusters": [1]},
                                      {"reddit": "bad"}, {"items_by_source": []}])
def test_last30days_malformed_is_not_empty(payload):
    result, _ = fetch(Last30DaysSourceAdapter(), payload)
    assert result.outcome is SourceOutcome.SCHEMA_ERROR


@pytest.mark.parametrize("payload,expected", [
    ({"results": []}, SourceOutcome.EMPTY),
    ({"results": [], "source_status": {"reddit": "auth-failed"}}, SourceOutcome.UNAUTHENTICATED),
    ({"results": [], "source_status": {"x": "rate-limited"}}, SourceOutcome.SOFT_BLOCKED),
    ({"results": [], "source_status": {"x": "timeout"}}, SourceOutcome.TIMEOUT),
    ({"error": "provider failed"}, SourceOutcome.ERROR),
    ({"ok": False, "error": {"code": "AUTH_REQUIRED", "message": "failed"}}, SourceOutcome.UNAUTHENTICATED),
])
def test_last30days_empty_and_failed_are_distinct(payload, expected):
    result, _ = fetch(Last30DaysSourceAdapter(), payload)
    assert result.outcome is expected
    assert result.items == []


def test_last30days_en_passes_real_inline_plan_without_changing_environment(monkeypatch, en_config):
    monkeypatch.setenv("CODEX_THREAD_ID", "retained-host-signal")
    result, calls = fetch(Last30DaysSourceAdapter(), {"results": []},
                          cfg=en_config, query="python workflows")
    call = calls[0]
    plan = json.loads(call[call.index("--plan") + 1])
    assert plan["intent"] == "factual"
    assert plan["freshness_mode"] == "balanced_recent"
    assert plan["cluster_mode"] == "none"
    assert plan["subqueries"] == [{"search_query": "python workflows", "ranking_query": "python workflows",
                                  "sources": ["hackernews", "devto"], "weight": 1.0}]
    assert call[-1] == "python workflows"
    import os
    assert os.environ["CODEX_THREAD_ID"] == "retained-host-signal"
    assert result.outcome is SourceOutcome.EMPTY


def test_last30days_cn_keeps_legacy_query_command():
    _, calls = fetch(Last30DaysSourceAdapter(), {"zhihu": []},
                     cfg=cm.COMMUNITY_SOURCES["last30days_cn"])
    assert calls[0][-4:] == ["--emit", "json", "--quick", "python"]
    assert "--plan" not in calls[0]


@pytest.mark.parametrize("source,query,row,extra", [
    ("bilibili", "哔哩哔哩 python", {"title": "video", "score": 123, "url": "https://www.bilibili.com/video/BV1"}, ["--type", "video"]),
    ("zhihu", "知乎 python", {"title": "answer", "votes": 99, "url": "https://www.zhihu.com/question/1/answer/2"}, []),
    ("weibo", "微博 python", {"title": "post", "time": "2026-10-17", "url": "https://weibo.com/1/Abc"}, []),
])
def test_optional_platform_selects_exact_search_and_mapping(monkeypatch, source, query, row, extra):
    engine = cm.CommunityEngine()
    assert engine._detect_sources(query) == [source]
    assert source not in engine._detect_sources("ordinary query")
    calls = []

    async def runner(cli, timeout):
        calls.append(cli)
        return 0, json.dumps({"ok": True, "data": [row]}), ""

    monkeypatch.setattr(cm, "_run_cmd", runner)
    results = asyncio.run(engine.search(SearchOptions(query, count=3)))
    assert calls == [["opencli", source, "search", query] + extra + ["-f", "json", "--limit", "3"]]
    assert results[0].url == row["url"]
    assert results[0].title == row["title"]
    assert results[0].source_tag == source


@pytest.mark.parametrize("query,source", [("site:bilibili.com python", "bilibili"),
                                          ("site:b23.tv python", "bilibili"),
                                          ("site:zhihu.com python", "zhihu"),
                                          ("site:zhuanlan.zhihu.com python", "zhihu"),
                                          ("site:weibo.com python", "weibo")])
def test_optional_platform_site_selection(query, source):
    assert cm.CommunityEngine()._detect_sources(query) == [source]


def test_v2ex_has_no_invalid_opencli_search_definition():
    assert "v2ex" not in cm.COMMUNITY_SOURCES
    assert cm.CommunityEngine()._detect_sources("V2EX python") == []


def test_last30days_failure_reaches_request_diagnostics(monkeypatch):
    async def runner(cli, timeout):
        return 0, json.dumps({"results": {}, "clusters": []}), ""

    monkeypatch.setattr(cm, "_run_cmd", runner)
    execution = asyncio.run(cm.CommunityEngine()._fetch_source_execution(
        "last30days_en", SearchOptions("python"), datetime.now(timezone.utc)))
    assert execution.scored == []
    assert execution.diagnostic.outcome is SourceOutcome.SCHEMA_ERROR


@pytest.mark.parametrize("query,sources,search_query", [
    ("python site:reddit.com", ["reddit"], "python"),
    ("python site:dev.to", ["devto"], "python"),
    ("ai site:techmeme.com", ["techmeme"], "ai"),
    ("YouTube cameras", ["youtube"], "YouTube cameras"),
    ("AI technology news", ["techmeme", "hackernews"], "AI technology news"),
    ("garden discussion", ["hackernews", "reddit"], "garden discussion"),
])
def test_last30days_plan_honors_platform_and_topic_budget(query, sources, search_query, en_config):
    _, calls = fetch(Last30DaysSourceAdapter(), {"results": []},
                     cfg=en_config, query=query)
    plan = json.loads(calls[0][calls[0].index("--plan") + 1])
    assert plan["subqueries"][0]["sources"] == sources
    assert len(sources) <= 2
    assert plan["subqueries"][0]["search_query"] == search_query
    assert plan["subqueries"][0]["ranking_query"] == query


@pytest.mark.parametrize("query", ["python site:zhihu.com", "知乎 python", "python site:example.com",
                                  "python site:reddit.com site:youtube.com site:github.com"])
def test_last30days_plan_rejects_unimplemented_source_scope_without_running(query, en_config):
    result, calls = fetch(Last30DaysSourceAdapter(), {"results": []},
                          cfg=en_config, query=query)
    assert result.outcome is SourceOutcome.SKIPPED
    assert result.detail == "unsupported_source_scope"
    assert calls == []


def test_last30days_normalized_platform_provenance_survives_engine(monkeypatch):
    async def runner(cli, timeout):
        return 0, json.dumps({"results": [
            {"title": "Developer post", "url": "https://dev.to/u/post", "source": "devto",
             "summary": "Source claim, not independently checked"},
        ]}), ""

    monkeypatch.setattr(cm, "_run_cmd", runner)
    execution = asyncio.run(cm.CommunityEngine()._fetch_source_execution(
        "last30days_en", SearchOptions("python"), datetime.now(timezone.utc)))
    result = execution.scored[0][1]
    assert result.source_tag == "devto"
    assert result.snippet == "Source claim, not independently checked"
    assert execution.diagnostic.source == "last30days_en"


def test_last30days_old_install_does_not_plan_unknown_sources(tmp_path):
    cfg = dict(cm.COMMUNITY_SOURCES["last30days_en"],
               cli=["python3", str(tmp_path / "last30days.py")])
    _, calls = fetch(Last30DaysSourceAdapter(), {"results": []}, cfg=cfg)
    plan = json.loads(calls[0][calls[0].index("--plan") + 1])
    assert plan["subqueries"][0]["sources"] == ["hackernews", "reddit"]
    assert calls[0][calls[0].index("--search") + 1] == "hackernews,reddit"
    result, calls = fetch(Last30DaysSourceAdapter(), {"results": []},
                          cfg=cfg, query="python site:dev.to")
    assert result.outcome is SourceOutcome.SKIPPED
    assert calls == []
