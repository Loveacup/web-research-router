"""formatters 输出契约单测。"""
import json

import pytest

from conftest import mk_results
from wrr.schemas import (FallbackStep, RouterResult, ExtractResult,
                         RouteQuality, RouteTrace)
from wrr.formatters import format_search, format_extract, format_similar, format_error
from wrr import config


def test_format_search_keys_and_backup_hint():
    rr = RouterResult("exa", mk_results(2), [FallbackStep("exa", True, 2)])
    out = json.loads(format_search(rr, "q"))
    assert out["success"] is True
    d = out["details"]
    assert d["provider"] == "exa"
    assert d["result_count"] == 2
    assert d["query"] == "q"
    assert "fallback_chain" in d
    assert d["backup_hint"] == config.BACKUP_HINT
    assert "⚠️ fallback" not in out["content"]      # 未降级无 banner


def test_format_search_banner_on_degrade():
    steps = [FallbackStep("exa", False, 0, "down"), FallbackStep("brave", True, 1)]
    rr = RouterResult("brave", mk_results(1), steps)
    out = json.loads(format_search(rr, "q"))
    assert "⚠️ fallback" in out["content"]
    assert "brave" in out["content"]


def test_format_extract_includes_highlights():
    rr = RouterResult("exa", ExtractResult("https://x", "body", ["hl1"]),
                      [FallbackStep("exa", True, 4)])
    out = json.loads(format_extract(rr, "https://x"))
    assert out["details"]["actualProvider"] == "exa"
    assert out["details"]["highlights"] == ["hl1"]
    assert out["details"]["quality"]["verdict"] == "complete"
    assert "Highlights" in out["content"]


def test_format_similar_keys():
    rr = RouterResult("exa", mk_results(2), [FallbackStep("exa", True, 2)])
    out = json.loads(format_similar(rr, "https://x"))
    assert out["details"]["result_count"] == 2
    assert out["details"]["quality"]["verdict"] == "complete"
    assert "web_similar" in out["content"]


def test_format_error_shape():
    out = json.loads(format_error("web_search", "q", ValueError("boom")))
    assert "web_search failed" in out["error"]
    assert out["details"]["identifier"] == "q"


def test_format_search_includes_diagnostics():
    """format_search 应在 details 中包含 diagnostics（如果存在）。"""
    from wrr.schemas import RouteTrace, DiagnosticEvent
    trace = RouteTrace(
        mode="grounding",
        mode_reason="classify_intent",
        selected_engines=["exa", "brave"],
        events=[
            DiagnosticEvent(engine="exa", ok=True, category="search", elapsed_ms=100.5, count=2),
            DiagnosticEvent(engine="brave", ok=False, category="search", elapsed_ms=50.2, count=0, message="timeout"),
        ],
        elapsed_ms=150.7,
        timeout_ms=10000.0,
    )
    rr = RouterResult("exa", mk_results(2), [FallbackStep("exa", True, 2)], diagnostics=trace)
    out = json.loads(format_search(rr, "q"))
    assert "diagnostics" in out["details"]
    d = out["details"]["diagnostics"]
    assert d["mode"] == "grounding"
    assert d["mode_reason"] == "classify_intent"
    assert d["selected_engines"] == ["exa", "brave"]
    assert len(d["events"]) == 2
    assert d["events"][0]["engine"] == "exa"
    assert d["events"][0]["ok"] is True
    assert d["elapsed_ms"] == 150.7


def _result_with_quality(verdict, *, failed=None, success=None, min_required=1):
    successful = success or ["exa"]
    failed_sources = failed or []
    expected = successful + [name for name in failed_sources if name not in successful]
    quality = RouteQuality(
        verdict=verdict,
        expected_sources=expected,
        successful_sources=successful,
        failed_sources=failed_sources,
        independent_source_count=len(successful),
        min_required=min_required,
        reasons=[] if verdict == "complete" else ["test_reason"],
    )
    trace = RouteTrace(mode="grounding", quality=quality)
    return RouterResult(
        "rrf:grounding", mk_results(1), [FallbackStep("exa", True, 1)],
        mode="grounding", diagnostics=trace,
    )


def test_format_search_always_exposes_machine_readable_quality():
    complete = json.loads(format_search(_result_with_quality("complete"), "q"))
    legacy = json.loads(format_search(
        RouterResult("exa", mk_results(1), [FallbackStep("exa", True, 1)]), "q"
    ))

    assert complete["details"]["quality"]["verdict"] == "complete"
    assert "⚠️ quality" not in complete["content"]
    assert legacy["details"]["quality"]["verdict"] == "complete"
    assert legacy["details"]["quality"]["reasons"] == ["legacy_route_without_quality"]


@pytest.mark.parametrize("verdict", ["insufficient", "degraded_success"])
def test_format_search_warns_only_for_non_complete_quality(verdict):
    result = _result_with_quality(
        verdict, failed=["brave"], success=["exa"], min_required=2,
    )
    out = json.loads(format_search(result, "q"))

    assert out["details"]["quality"]["verdict"] == verdict
    assert "⚠️ quality" in out["content"]
    assert verdict in out["content"]


def test_format_error_exposes_failed_quality():
    out = json.loads(format_error("web_search", "q", ValueError("boom")))

    quality = out["details"]["quality"]
    assert quality["verdict"] == "failed"
    assert quality["independent_source_count"] == 0
    assert quality["reasons"] == ["no_valid_results"]


# ── policy-sensitive 信号 + source_map 输出契约（加法式 details）──────
def _sr(url, *, source_tag="", fusion_sources=None):
    from wrr.schemas import SearchResult
    return SearchResult(title="t", url=url, snippet="s", source_tag=source_tag,
                        fusion_sources=list(fusion_sources or []))


def _search_out(items, query="q"):
    rr = RouterResult("exa", items, [FallbackStep("exa", True, len(items))])
    return json.loads(format_search(rr, query))


def test_format_search_policy_sensitive_flag():
    """details.policy_sensitive 对政策查询为 True，普通查询为 False。"""
    policy = _search_out(mk_results(1), "EU AI regulation policy")
    plain = _search_out(mk_results(1), "how to center a div in css")
    assert policy["details"]["policy_sensitive"] is True
    assert plain["details"]["policy_sensitive"] is False


def test_source_map_covers_five_source_types():
    """gov.uk official / 普通 public / reddit community / academic / local。"""
    items = [
        _sr("https://www.gov.uk/guidance/data-protection"),
        _sr("https://example.com/blog/post"),
        _sr("https://reddit.com/r/x/abc", source_tag="reddit"),
        _sr("https://api.openalex.org/W123", source_tag="academic:openalex"),
        _sr("obsidian://note/x", source_tag="local:qmd"),
    ]
    smap = _search_out(items)["details"]["source_map"]
    assert [e["index"] for e in smap] == [1, 2, 3, 4, 5]
    assert [e["source_type"] for e in smap] == [
        "official", "public", "community", "academic", "local"]
    assert smap[0]["url"] == "https://www.gov.uk/guidance/data-protection"
    assert smap[2]["source_tag"] == "reddit"


def test_source_map_providers_preserved_not_mislabeled_official():
    """fusion_sources=[academic,exa] → providers 保留、归类 academic 而非 official。"""
    items = [_sr("https://example.com/x", fusion_sources=["academic", "exa"])]
    entry = _search_out(items)["details"]["source_map"][0]
    assert entry["providers"] == ["academic", "exa"]
    assert entry["source_type"] == "academic"


def test_source_map_empty_and_malformed_url_stable_public():
    """空 tag / 空 URL / 畸形 URL 稳定归 public，providers 排序去重。"""
    items = [
        _sr("", fusion_sources=["exa", "exa", "brave"]),
        _sr("not-a-valid-url"),
    ]
    smap = _search_out(items)["details"]["source_map"]
    assert smap[0]["source_type"] == "public"
    assert smap[0]["providers"] == ["brave", "exa"]      # 排序去重
    assert smap[1]["source_type"] == "public"


@pytest.mark.parametrize("tag", [
    # 既有已覆盖的 community tags（回归保护）
    "reddit", "twitter", "xiaohongshu", "v2ex", "hackernews",
    "last30days", "aihot", "wechat",
    # CommunityEngine 实测输出的真实 tags（此前被误归 public）
    "aihot_rss", "wechat_rss", "last30days_en", "last30days_cn",
])
def test_source_map_real_community_tags_classified_community(tag):
    """CommunityEngine 实际 source_tag 均应归 community，不因大小写差异漏判。"""
    items = [_sr("https://example.com/thread", source_tag=tag)]
    entry = _search_out(items)["details"]["source_map"][0]
    assert entry["source_type"] == "community"
    assert entry["source_tag"] == tag


@pytest.mark.parametrize("tag", ["aihot_rss", "wechat_rss", "last30days_en", "last30days_cn"])
def test_source_map_new_community_tags_case_insensitive(tag):
    """新增 community tags 大小写不敏感（formatter 内部 lower 规整）。"""
    items = [_sr("https://example.com/x", source_tag=tag.upper())]
    entry = _search_out(items)["details"]["source_map"][0]
    assert entry["source_type"] == "community"


def test_source_map_community_tags_do_not_leak_to_public_or_priority():
    """普通 public 与优先级 local/academic 归类不受新增 community tags 影响。"""
    items = [
        _sr("https://example.com/blog/post"),                         # public
        _sr("https://api.openalex.org/W1", source_tag="academic:x"),  # academic
        _sr("obsidian://note/y", source_tag="local:qmd"),             # local
        _sr("https://example.com/z", source_tag="aihot_rss"),         # community
    ]
    smap = _search_out(items)["details"]["source_map"]
    assert [e["source_type"] for e in smap] == [
        "public", "academic", "local", "community"]


def test_format_search_core_fields_unchanged_with_new_details():
    """新增字段为加法式：既有关键字段保持不变。"""
    out = _search_out(mk_results(2), "q")
    d = out["details"]
    assert out["success"] is True
    assert d["provider"] == "exa"
    assert d["result_count"] == 2
    assert d["query"] == "q"
    assert d["backup_hint"] == config.BACKUP_HINT
    assert "quality" in d and "fallback_chain" in d
    assert "policy_sensitive" in d and "source_map" in d


def test_format_similar_warns_for_noncomplete_quality():
    result = _result_with_quality(
        "degraded_success", failed=["brave"], success=["exa"],
    )
    out = json.loads(format_similar(result, "https://x"))

    assert out["details"]["quality"]["verdict"] == "degraded_success"
    assert "⚠️ quality" in out["content"]
