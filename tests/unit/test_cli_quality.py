"""P0-3 CLI quality/provenance contract tests（不启动真实引擎）。"""
import json

from wrr import _cli
from wrr.errors import AllEnginesFailedError
from wrr.formatters import format_search
from wrr.schemas import (FallbackStep, RouteQuality, RouteTrace, RouterResult,
                         SearchOptions, SearchResult)


def _result(verdict="complete"):
    quality = RouteQuality(
        verdict=verdict,
        expected_sources=["exa", "brave"],
        successful_sources=["exa"],
        failed_sources=[] if verdict == "complete" else ["brave"],
        independent_source_count=1,
        min_required=1 if verdict != "insufficient" else 2,
        reasons=[] if verdict == "complete" else ["test_reason"],
    )
    result = SearchResult(
        title="A", url="https://a", fusion_sources=["exa", "brave"], rrf_score=0.25,
    )
    return RouterResult(
        "rrf:grounding", [result], [FallbackStep("exa", True, 1)],
        mode="grounding", diagnostics=RouteTrace(mode="grounding", quality=quality),
    )


def test_cli_json_exposes_quality_and_nonempty_provenance(monkeypatch, capsys):
    async def fake_run(*_args):
        return _result("complete")

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "search", SearchOptions("q"), None, "q", True, False, format_search,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["quality"]["verdict"] == "complete"
    assert payload["result"][0]["fusion_sources"] == ["exa", "brave"]
    assert payload["result"][0]["rrf_score"] == 0.25


def test_cli_noncomplete_text_warns_but_keeps_zero_exit(monkeypatch, capsys):
    async def fake_run(*_args):
        return _result("insufficient")

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "search", SearchOptions("q"), None, "q", False, False, format_search,
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert "⚠️ quality" in captured.out
    assert "insufficient" in captured.out


def test_cli_failed_json_keeps_exit_one_and_failed_quality(monkeypatch, capsys):
    async def fake_run(*_args):
        raise AllEnginesFailedError("down")

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "search", SearchOptions("q"), None, "q", True, False, format_search,
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["quality"]["verdict"] == "failed"
    assert payload["quality"]["independent_source_count"] == 0


def test_cli_omits_only_empty_additive_provenance(monkeypatch, capsys):
    async def fake_run(*_args):
        item = SearchResult(title="plain", url="https://plain")
        return RouterResult(
            "exa", [item], [FallbackStep("exa", True, 1)],
            diagnostics=RouteTrace(quality=RouteQuality(
                verdict="complete", expected_sources=["exa"],
                successful_sources=["exa"], independent_source_count=1,
            )),
        )

    monkeypatch.setattr(_cli, "_run", fake_run)
    rc = _cli._dispatch(
        "search", SearchOptions("q"), None, "q", True, False, format_search,
    )
    item = json.loads(capsys.readouterr().out)["result"][0]

    assert rc == 0
    assert "fusion_sources" not in item
    assert "rrf_score" not in item
    assert "source_ts" in item                # 旧 CLI asdict 形状保持
    assert "freshness_score" in item
