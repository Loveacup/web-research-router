"""Post-retrieval filtering must preserve routing, privacy, and evidence semantics."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from wrr import config, router
from wrr.engines.base import SearchEngine
from wrr.registry import EngineRegistry
from wrr.schemas import SearchOptions, SearchResult


class ResultEngine(SearchEngine):
    def __init__(self, name, results, clock=None):
        self.name = name
        self.results = results
        self.calls = 0
        self.clock = clock

    async def search(self, options):
        self.calls += 1
        if self.clock is not None:
            self.clock.now = 11.0
        return self.results


@pytest.fixture
def filtered_route(monkeypatch):
    from wrr import search_filter

    monkeypatch.setattr(config, "JEV_FILTER_ENABLED", True)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    monkeypatch.setenv("WRR_V6_ROUTER", "0")
    requests = []
    state = {"relevance": 0.0, "status": 200}

    def respond(request):
        requests.append(json.loads(request.content))
        answers = {
            key: {"type": "noul", "noul": 0.0 if key.startswith("inj_") else state["relevance"]}
            for key in requests[-1]["questions"]
        }
        return httpx.Response(state["status"], json={"answers": answers, "model": "jev-test"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(search_filter.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    return requests, state


def make_registry(name="exa", clock=None):
    registry = EngineRegistry()
    docs = [SearchResult("A useful document", "https://example.org/article", "Public material.")]
    primary = ResultEngine(name, docs, clock)
    backup = ResultEngine("searxng", [SearchResult("Unrelated backup", "https://example.com/backup")])
    registry.register(primary)
    registry.register(backup)
    return registry, primary, backup


@pytest.mark.parametrize("stage_s", [False, True])
def test_all_rejected_is_empty_not_a_recovery_invitation(filtered_route, stage_s):
    requests, _ = filtered_route
    registry, primary, backup = make_registry()
    result = asyncio.run(router.route_search_v5(
        SearchOptions("specific public factual question", provider="exa"), registry,
        stage_s_enabled=stage_s,
    ))
    assert result.payload == []
    assert primary.calls == 1 and backup.calls == 0
    assert len(requests) == 1
    assert result.quality is not None
    assert result.diagnostics is not None and result.diagnostics.search_filter is not None
    assert result.quality.verdict == "insufficient"
    assert result.quality.independent_source_count == 1
    assert result.diagnostics.search_filter["status"] == "ok"
    assert result.diagnostics.search_filter["evaluated_count"] == 1
    if stage_s:
        assert result.diagnostics.decision_evidence is not None
        assert result.diagnostics.decision_evidence.outcome == "empty"
        assert result.diagnostics.decision_evidence.result_count == 0
    else:
        assert result.diagnostics.decision_evidence is None


@pytest.mark.parametrize("name,query", [
    ("exa", "我的笔记里关于数据库的决定"),
    ("community", "public-looking forum discussion"),
    ("github", "repository issue containing account-specific results"),
    ("academic", "doi:10.1038/nature14539"),
])
def test_nonpublic_and_exact_scopes_do_not_leave_host(filtered_route, name, query):
    requests, _ = filtered_route
    registry, primary, _ = make_registry(name)
    result = asyncio.run(router.route_search_v5(SearchOptions(query, provider=name), registry))
    assert requests == []
    assert result.payload is primary.results
    assert result.diagnostics is not None and result.diagnostics.search_filter is not None
    assert result.diagnostics.search_filter["evaluated_count"] == 0
    assert result.diagnostics.search_filter["status"] == "skipped"


def test_primary_exhausting_budget_does_not_launch_filter(filtered_route, monkeypatch):
    requests, _ = filtered_route
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(router, "time", SimpleNamespace(monotonic=lambda: clock.now))
    registry, primary, _ = make_registry(clock=clock)
    result = asyncio.run(router.route_search_v5(
        SearchOptions("a public question", provider="exa"), registry))
    assert requests == []
    assert result.payload is primary.results
    assert result.diagnostics is not None and result.diagnostics.search_filter is not None
    assert result.diagnostics.search_filter["reason"] == "budget_exhausted"


def test_filter_outage_keeps_retrieved_evidence_without_new_source(filtered_route):
    requests, state = filtered_route
    state["status"] = 429
    registry, primary, backup = make_registry()
    result = asyncio.run(router.route_search_v5(
        SearchOptions("a public question", provider="exa"), registry,
        stage_s_enabled=True))
    assert result.payload is primary.results
    assert len(requests) == 1
    assert backup.calls == 0
    assert result.quality is not None
    assert result.diagnostics is not None and result.diagnostics.search_filter is not None
    assert result.diagnostics.decision_evidence is not None
    assert result.quality.successful_sources == ["exa"]
    assert result.quality.independent_source_count == 1
    assert result.quality.verdict == "degraded_success"
    assert result.diagnostics.search_filter["status"] == "fail_open"
    assert result.diagnostics.decision_evidence.result_count == 1
    assert result.diagnostics.decision_evidence.outcome == "success"


def test_disabled_filter_keeps_existing_output_contract(filtered_route, monkeypatch):
    requests, _ = filtered_route
    monkeypatch.setattr(config, "JEV_FILTER_ENABLED", False)
    registry, primary, _ = make_registry()
    result = asyncio.run(router.route_search_v5(
        SearchOptions("a public question", provider="exa"), registry))
    assert result.payload is primary.results
    assert requests == []
    assert result.diagnostics is not None
    assert "search_filter" not in result.diagnostics.to_dict()


def test_fused_duplicates_are_judged_once_without_new_retrieval(filtered_route):
    requests, _ = filtered_route
    shared = SearchResult("Public specification", "https://example.org/spec", "Specific facts.")
    extra = SearchResult("Unrelated commentary", "https://example.org/comment", "Other topic.")
    registry = EngineRegistry()
    exa = ResultEngine("exa", [shared, extra])
    deepseek = ResultEngine("deepseek", [shared])
    backup = ResultEngine("searxng", [extra])
    for engine in (exa, deepseek, backup):
        registry.register(engine)
    result = asyncio.run(router.route_search_v5(
        SearchOptions("public specification", route_mode="grounding"), registry))
    assert result.payload == []
    assert exa.calls == deepseek.calls == 1 and backup.calls == 0
    assert len(requests) == 1
    assert len(requests[0]["state"]["passages"]) == 2
    assert result.quality is not None
    assert result.quality.independent_source_count == 2
