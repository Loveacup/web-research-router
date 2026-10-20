"""Fetch, similar, error and quality formatter contracts."""
import json

from conftest import mk_results
from wrr.formatters import format_error, format_extract, format_similar
from wrr.schemas import ExtractResult, FallbackStep, RouteQuality, RouteTrace, RouterResult


def test_format_extract_includes_highlights():
    result = RouterResult("exa", ExtractResult("https://x", "body", ["hl1"]),
                          [FallbackStep("exa", True, 4)])
    output = json.loads(format_extract(result, "https://x"))
    assert output["details"]["actualProvider"] == "exa"
    assert output["details"]["highlights"] == ["hl1"]
    assert "Highlights" in output["content"]


def test_format_similar_keys():
    result = RouterResult("exa", mk_results(2), [FallbackStep("exa", True, 2)])
    output = json.loads(format_similar(result, "https://x"))
    assert output["details"]["result_count"] == 2
    assert output["details"]["quality"]["verdict"] == "complete"
    assert "web_similar" in output["content"]


def test_format_error_shape():
    output = json.loads(format_error("web_fetch", "https://x", ValueError("boom")))
    assert "web_fetch failed" in output["error"]
    assert output["details"]["identifier"] == "https://x"


def test_format_error_exposes_failed_quality():
    output = json.loads(format_error("web_similar", "https://x", ValueError("boom")))
    quality = output["details"]["quality"]
    assert quality["verdict"] == "failed"
    assert quality["independent_source_count"] == 0
    assert quality["reasons"] == ["no_valid_results"]


def test_format_similar_warns_for_noncomplete_quality():
    quality = RouteQuality(
        verdict="degraded_success",
        expected_sources=["exa", "brave"],
        successful_sources=["exa"],
        failed_sources=["brave"],
        independent_source_count=1,
        min_required=1,
        reasons=["expected_sources_failed"],
    )
    result = RouterResult(
        "exa", mk_results(1), [FallbackStep("exa", True, 1)],
        diagnostics=RouteTrace(quality=quality),
    )
    output = json.loads(format_similar(result, "https://x"))
    assert output["details"]["quality"]["verdict"] == "degraded_success"
    assert "⚠️ quality" in output["content"]
