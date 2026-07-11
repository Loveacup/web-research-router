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


def test_format_similar_warns_for_noncomplete_quality():
    result = _result_with_quality(
        "degraded_success", failed=["brave"], success=["exa"],
    )
    out = json.loads(format_similar(result, "https://x"))

    assert out["details"]["quality"]["verdict"] == "degraded_success"
    assert "⚠️ quality" in out["content"]
