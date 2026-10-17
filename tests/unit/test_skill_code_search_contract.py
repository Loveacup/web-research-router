"""GitHub REST code-search contract for the skill engine (zero network).

GitHub REST /search/code does not support glob path qualifiers such as
``path:**/SKILL.md``: every such query returns total_count 0, which silently
made the skill provider return nothing in production. These tests drive the
real SkillDiscoveryEngine -> GitHubClient -> httpx stack against a mock
transport that behaves like the REST API, with the expected qualifier written
out literally (not read from config) so a regression fails here.
"""
import asyncio
import base64

import httpx
import pytest

from wrr.engines import skill_discovery as sd
from wrr.engines._github_client import GitHubClient
from wrr.schemas import SearchOptions

SKILL_MD = "---\nname: demo-skill\ndescription: does demo things\nversion: 1.0\n---\nbody\n"


def _github(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path == "/search/code":
        q = params["q"]
        # REST code search: the glob path form matches nothing; filename: works.
        if "filename:SKILL.md" not in q.split():
            return httpx.Response(200, json={"total_count": 0, "items": []})
        return httpx.Response(200, json={"total_count": 1, "incomplete_results": False, "items": [
            {"path": "skills/demo-skill/SKILL.md", "repository": {"full_name": "acme/skills"}}]})
    if path == "/repos/acme/skills/contents/skills/demo-skill/SKILL.md":
        return httpx.Response(200, json={"content": base64.b64encode(SKILL_MD.encode()).decode(), "encoding": "base64"})
    if path == "/search/repositories":
        return httpx.Response(200, json={"total_count": 0, "items": []})
    if path.startswith("/repos/"):
        return httpx.Response(200, json={"stargazers_count": 1, "forks_count": 0, "pushed_at": "2026-09-01T00:00:00Z"})
    return httpx.Response(404, json={})


@pytest.fixture
def seen(monkeypatch):
    requests: list[httpx.Request] = []
    # Other suites replace httpx.AsyncClient without restoring it; use the real class.
    from httpx._client import AsyncClient

    def handler(request):
        requests.append(request)
        return _github(request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: AsyncClient(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return requests


def _code_search_queries(requests):
    return [r.url.params["q"] for r in requests if r.url.path == "/search/code"]


def test_search_uses_filename_qualifier_and_finds_skill(seen):
    engine = sd.SkillDiscoveryEngine(client=GitHubClient(token="test-token"))
    results = asyncio.run(engine.search(SearchOptions(query="demo", count=1)))
    assert sorted(_code_search_queries(seen)) == sorted([
        "demo filename:SKILL.md",
        "demo filename:SKILL.md repo:anthropics/skills repo:kepano/obsidian-skills repo:firecrawl/cli repo:vercel-labs/skills",
    ])
    # a single candidate yields no feedback token, so no phase-2 searches
    assert not [r for r in seen if r.url.path == "/search/repositories"]
    assert [(r.title, r.url) for r in results] == [("demo-skill", "https://github.com/acme/skills/tree/HEAD/skills/demo-skill")]


def test_phase2_channels_on_the_wire(seen, monkeypatch):
    """Two repos share the query term, so phase 2 issues the path search and a star-sorted repo search."""
    def two_repos(request):
        seen.append(request)
        if request.url.path == "/search/code" and "in:path" not in request.url.params["q"] \
                and "repo:" not in request.url.params["q"]:
            return httpx.Response(200, json={"total_count": 2, "items": [
                {"path": "skills/demo-skill/SKILL.md", "repository": {"full_name": "acme/skills"}},
                {"path": "demo-tool/SKILL.md", "repository": {"full_name": "other/demo"}}]})
        return _github(request)
    from httpx._client import AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: AsyncClient(*a, **{**k, "transport": httpx.MockTransport(two_repos)}))
    asyncio.run(sd.SkillDiscoveryEngine(client=GitHubClient(token="test-token")).search(SearchOptions(query="demo", count=5)))
    assert "demo in:path filename:SKILL.md" in _code_search_queries(seen)
    repo = [r.url.params for r in seen if r.url.path == "/search/repositories"]
    assert [(p["q"], p["sort"], p["order"]) for p in repo] == [("demo skill", "stars", "desc")]


def test_health_check_probes_with_filename_qualifier(seen):
    asyncio.run(sd.SkillDiscoveryEngine(client=GitHubClient(token="test-token")).health_check())
    queries = _code_search_queries(seen)
    assert queries == ["filename:SKILL.md"]
    assert not any("path:" in q for q in queries)
