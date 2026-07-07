"""formatters 输出契约单测。"""
import json

from conftest import mk_results
from wrr.schemas import FallbackStep, RouterResult, ExtractResult
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
    assert "Highlights" in out["content"]


def test_format_similar_keys():
    rr = RouterResult("exa", mk_results(2), [FallbackStep("exa", True, 2)])
    out = json.loads(format_similar(rr, "https://x"))
    assert out["details"]["result_count"] == 2
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
