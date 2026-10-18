"""Routing contracts: exact evidence, independent families, and bounded recovery."""
import asyncio

import pytest

from wrr import config
from wrr.engines._fusion import rrf_fuse
from wrr.engines.base import SearchEngine
from wrr.errors import AllEnginesFailedError
from wrr.registry import EngineRegistry
from wrr.router import _route_quality, _run_engine, legacy_selection_plan, route_search_v5
from wrr.schemas import FallbackStep, SearchOptions, SearchResult


class RecordingEngine(SearchEngine):
    def __init__(self, name, result=None):
        self.name = name
        self.result = result or []
        self.queries = []

    async def search(self, options):
        self.queries.append(options.query)
        return self.result


def test_github_and_skill_do_not_manufacture_independent_votes():
    shared = SearchResult("same repository", "https://github.com/example/repo")
    independent = SearchResult("other evidence", "https://example.org/evidence")
    fused = rrf_fuse({"github": [shared, shared], "skill": [shared],
                      "exa": [independent], "deepseek": [independent]})
    assert fused[0]["doc"].url == independent.url
    same = next(row for row in fused if row["doc"].url == shared.url)
    assert same["rrf"] == pytest.approx(1 / 61)
    assert same["doc"].fusion_sources == ["github", "skill"]
    quality = _route_quality("research", ["github", "skill"],
                             [FallbackStep("github", True, 1), FallbackStep("skill", True, 1)], True)
    assert quality.independent_source_count == 1
    assert quality.verdict == "insufficient"


@pytest.mark.parametrize("query", ["10.1038/nature12373", "doi:10.1038/nature12373",
                                   "references:10.1038/nature12373", "citations:W2741809807"])
def test_structured_bibliographic_requests_do_not_fan_out(query):
    plan = legacy_selection_plan(SearchOptions(query))
    assert plan.engine_names == ("academic",)
    assert plan.mode_reason == "academic_lookup"
    explicit = legacy_selection_plan(SearchOptions(query, provider="deepseek"))
    assert explicit.engine_names == ("deepseek",)
    general = legacy_selection_plan(SearchOptions(query, route_mode="research"))
    assert "exa" in general.engine_names and "deepseek" in general.engine_names


def test_missing_exact_doi_does_not_return_unrelated_recovery(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    monkeypatch.setattr(config, "recovery_allowed", lambda: True)
    registry = EngineRegistry()
    academic = RecordingEngine("academic")
    web = RecordingEngine("exa", [SearchResult("unrelated", "https://example.org")])
    registry.register(academic)
    registry.register(web)
    with pytest.raises(AllEnginesFailedError):
        asyncio.run(route_search_v5(SearchOptions("doi:10.9999/not-a-real-paper"), registry))
    assert academic.queries == ["doi:10.9999/not-a-real-paper"]
    assert web.queries == []


def test_recovery_only_runs_untried_engines(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    monkeypatch.setattr(config, "recovery_allowed", lambda: True)
    registry = EngineRegistry()
    primary = [RecordingEngine(name) for name in ("exa", "deepseek", "brave")]
    backup = RecordingEngine("searxng", [SearchResult("surviving source", "https://example.org")])
    for engine in [*primary, backup]:
        registry.register(engine)
    result = asyncio.run(route_search_v5(SearchOptions("plain fact", route_mode="grounding"), registry))
    assert result.payload[0].url == "https://example.org"
    assert all(engine.queries == ["plain fact"] for engine in [*primary, backup])
    assert result.diagnostics is not None
    assert result.diagnostics.selected_engines == ["searxng"]


def test_exhausted_deadline_never_starts_another_provider():
    registry = EngineRegistry()
    engine = RecordingEngine("exa", [SearchResult("late", "https://example.org")])
    registry.register(engine)
    _, result, step, _ = asyncio.run(_run_engine(registry, "exa", SearchOptions("q"), 0))
    assert engine.queries == []
    assert result is None
    assert not step.ok and "budget exceeded" in step.error


def test_v2ex_uses_real_web_site_search_without_mutating_query(monkeypatch):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    registry = EngineRegistry()
    exa = RecordingEngine("exa", [SearchResult("V2EX thread", "https://v2ex.com/t/123")])
    community = RecordingEngine("community")
    registry.register(exa)
    registry.register(community)
    options = SearchOptions("v2ex Python experience", route_mode="platform")
    result = asyncio.run(route_search_v5(options, registry))
    assert exa.queries == ["site:v2ex.com v2ex Python experience"]
    assert community.queries == []
    assert options.query == "v2ex Python experience"
    assert result.payload[0].url == "https://v2ex.com/t/123"


def test_community_wrapper_does_not_double_vote_same_upstream():
    item = SearchResult("same repository", "https://github.com/example/repo",
                        source_tag="github")
    fused = rrf_fuse({"github": [item], "community": [item], "last30days_en": [item]})
    assert fused[0]["rrf"] == pytest.approx(1 / 61)


def test_biomedical_research_routes_without_overriding_personal_health():
    assert config.classify_intent("randomized clinical trials cancer immunotherapy") == "academic"
    assert not config.academic_triggered("how should I treat a cold?")


@pytest.mark.parametrize("query", ["v2ex vs reddit Python experience", "V2EX 和知乎 Python 体验"])
def test_v2ex_cross_platform_query_keeps_other_sources(monkeypatch, query):
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    registry = EngineRegistry()
    exa = RecordingEngine("exa", [SearchResult("comparison", "https://example.org")])
    community = RecordingEngine("community")
    registry.register(exa)
    registry.register(community)
    asyncio.run(route_search_v5(SearchOptions(query, route_mode="platform"), registry))
    assert exa.queries == [query]
    assert community.queries == [query]
