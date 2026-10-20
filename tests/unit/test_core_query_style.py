"""Deterministic keyword extraction and truthful per-source query evidence."""
import asyncio
from dataclasses import replace

import pytest

from wrr.core.contracts import Health, Kind, Plan, PrivacyClass, Provision, SourceDescriptor, Transport
from wrr.core.execute import execute
from wrr.core.query_style import to_keywords
from wrr.core.sources import build_adapters
from wrr.engines.loader import parse_engine_manifest
from wrr.schemas import SearchResult


@pytest.mark.parametrize("query, expected", [
    ("github repos for web search mcp server", "web search mcp server"),
    ("find me github repos for web search mcp server", "web search mcp server"),
    ("What is the Python-3.14 release?", "python-3.14 release"),
    ("How are C++ and .NET different?", "c++ and .net different"),
    ('find "Open Source" projects', '"Open Source" projects'),
    ("Python, python; PYTHON! rust", "python rust"),
    ("web search github projects", "web search projects"),  # "github" is filler for repo search
    ("请 搜索 关于 Python 的 教程", "python 教程"),
    ("帮我 找 最新 视频 教程", "视频 教程"),
    ("如何 学习机器学习 和 深度学习", "学习机器学习 深度学习"),
    ("搜索引擎的原理", "搜索引擎的原理"),
    ("foo_bar C# example.org", "foo_bar c# example.org"),
    ("the a for", "the a for"),
    ("one two three four five six seven", "one two three four five six"),
    ('"The Best" "the best" Rust', '"The Best" rust'),
])
def test_to_keywords(query, expected):
    assert to_keywords(query) == expected


def test_keyword_term_limit_and_empty_fallback():
    assert to_keywords("alpha beta gamma", max_terms=2) == "alpha beta"
    assert to_keywords("alpha beta", max_terms=0) == "alpha beta"
    assert to_keywords("!!!") == "!!!"
    assert to_keywords("") == ""


@pytest.mark.parametrize("gate", [None, "explicit_sources"])
def test_execute_sends_source_query_and_records_only_changes(monkeypatch, gate):
    received = []

    async def search(self, options):
        received.append(options.query)
        return [SearchResult("Result", "https://example.test")]

    monkeypatch.setattr("wrr.engines.exa.ExaEngine.search", search)
    base = SourceDescriptor(
        id="keywords", kind=Kind.WEB, transport=Transport.API,
        upstream_family="test", privacy_class=PrivacyClass.PUBLIC,
        provision=Provision.AUTHORIZED, health=Health.HEALTHY,
        adapter="wrr.engines.exa:ExaEngine", query_style="keywords",
    )
    inventory = [base, replace(base, id="natural", query_style="natural")]
    adapters = build_adapters(inventory, env={})
    query = "find me github repos for web search mcp server"

    async def run():
        return await execute(query, Plan([s.id for s in inventory], {}, "none", gate=gate),
                             adapters, deadline=asyncio.get_running_loop().time() + 2,
                             count=3, inventory=inventory)

    runs = asyncio.run(run())
    assert received == ["web search mcp server", query]
    assert runs[0].to_dict()["query_sent"] == received[0]
    assert "query_sent" not in runs[1].to_dict()
    assert base.to_dict()["query_style"] == "keywords"
    assert all(run.to_dict()["status"] == "ok" for run in runs)


@pytest.mark.parametrize("style", ["natural", "keywords", "wrong", None, 7, [], {}])
def test_manifest_query_style_validation(style):
    manifest, errors = parse_engine_manifest({
        "schema_version": 1, "id": "test", "name": "Test", "kind": "web_api",
        "adapter": "wrr.engines.exa:ExaEngine",
        "capabilities": {"actions": ["search"], "domains": ["web"]},
        "source": {"transport": "api", "kind": "web", "upstream_family": "test",
                   "privacy_class": "public", "query_style": style},
    })
    if style in ("natural", "keywords"):
        assert manifest is not None and not errors
    else:
        assert manifest is None
        assert "invalid:source.query_style" in errors
