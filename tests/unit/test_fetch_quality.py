"""Fetch content quality is distinct from successful provider execution."""
import asyncio
import json

import pytest

from wrr import config
from wrr.engines.base import SearchEngine
from wrr.errors import AllEnginesFailedError
from wrr.extraction import make_extraction
from wrr.formatters import format_extract
from wrr.router import route
from wrr.schemas import ExtractOptions, ExtractResult, RouterResult, SearchResult
from conftest import FakeAsyncClient
from wrr import _cli
from wrr.engines import exa
from wrr.registry import EngineRegistry
from wrr.tools import web_fetch


def test_exa_truncation_is_partial_in_both_host_envelopes(monkeypatch, capsys):
    import wrr.registry as registry_module

    monkeypatch.setenv("EXA_API_KEY", "offline-fixture-key")
    monkeypatch.setattr(exa.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(FakeAsyncClient, "response_data", {
        "results": [{"text": "A complete fixture article exceeds the requested character limit."}],
    })
    registry = EngineRegistry()
    registry.register(exa.ExaEngine())
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    monkeypatch.setattr(web_fetch, "get_registry", lambda: registry)

    args = _cli.build_parser().parse_args([
        "fetch", "https://fixture.invalid/article", "--provider", "exa", "--max-chars", "10", "--json",
    ])
    assert _cli.cmd_fetch(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(web_fetch.handle_web_fetch({
        "url": "https://fixture.invalid/article", "provider": "exa", "max_characters": 10,
    })))
    assert cli_result["result"]["text"] == "A complete"
    assert cli_result["result"]["completeness"] == "truncated"
    assert cli_result["result"]["partial"] is True
    assert cli_result["quality"]["verdict"] == "degraded"
    assert "content_truncated" in cli_result["quality"]["reasons"]
    assert hermes_result["details"]["extraction"]["completeness"] == "truncated"
    assert hermes_result["details"]["extraction"]["partial"] is True
    assert hermes_result["details"]["quality"] == cli_result["quality"]


class FixtureExtractor(SearchEngine):
    def __init__(self, name, result):
        self.name = name
        self.result = result
        self.calls = 0

    async def search(self, options):
        raise AssertionError("Extraction must not turn into search")

    async def extract(self, options):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def extraction_registry(monkeypatch, first):
    options = ExtractOptions("https://fixture.invalid/article")
    good = make_extraction(options, "Full fixture body.", method="fixture", source_complete=True)
    primary = FixtureExtractor("exa", first)
    backup = FixtureExtractor("brave", good)
    registry = EngineRegistry()
    registry.register(primary)
    registry.register(backup)
    monkeypatch.setattr(config, "EXTRACT_FALLBACK_ORDER", ["exa", "brave"])
    return options, registry, primary, backup


@pytest.mark.parametrize(("first", "reason"), [
    (ExtractResult("https://fixture.invalid", " \n\t "), "empty_body"),
    (ExtractResult("https://fixture.invalid", "Just a moment... Enable JavaScript and cookies."),
     "challenge_page"),
    (ExtractResult("https://fixture.invalid", "A short synopsis.", content_kind="summary"),
     "summary_not_body"),
    (SearchResult("Article", "https://fixture.invalid", "Search snippet."), "not_extraction_result"),
])
def test_invalid_body_falls_back_but_explicit_provider_does_not(monkeypatch, first, reason):
    options, registry, primary, backup = extraction_registry(monkeypatch, first)
    result = asyncio.run(route("extract", options, registry))
    assert result.actual_provider == "brave"
    assert result.payload.text == "Full fixture body."
    assert result.fallback_chain[0].error == reason
    assert (primary.calls, backup.calls) == (1, 1)
    with pytest.raises(AllEnginesFailedError) as failure:
        asyncio.run(route("extract", options, registry, explicit_provider="exa"))
    assert failure.value.diagnostics.events[0].message == reason
    assert backup.calls == 1


def test_attested_complete_body_and_long_challenge_discussion_remain_body(monkeypatch):
    text = "Just a moment: enable JavaScript and cookies is a challenge-page phrase. " * 40
    first = make_extraction(ExtractOptions("https://fixture.invalid", max_characters=5000),
                            text, method="fixture", source_complete=True)
    options, registry, primary, backup = extraction_registry(monkeypatch, first)
    result = asyncio.run(route("extract", options, registry))
    assert result.payload.text == text
    assert result.payload.content_kind == "body"
    assert result.payload.completeness == "complete"
    assert result.payload.partial is False
    assert result.quality.verdict == "complete"
    assert json.loads(format_extract(result, options.url))["details"]["extraction"]["partial"] is False
    assert (primary.calls, backup.calls) == (1, 0)


@pytest.mark.parametrize("first", [
    ExtractResult("https://fixture.invalid", "Usable legacy text."),
    make_extraction(ExtractOptions("https://fixture.invalid"), "Unattested API body.", method="fixture"),
])
def test_unattested_body_is_usable_but_never_complete(monkeypatch, first):
    options, registry, primary, backup = extraction_registry(monkeypatch, first)
    result = asyncio.run(route("extract", options, registry))
    assert result.payload.text == first.text
    assert result.payload.partial is True
    assert result.quality.verdict == "degraded"
    assert "completeness_unknown" in result.quality.reasons
    assert (primary.calls, backup.calls) == (1, 0)
    legacy = json.loads(format_extract(RouterResult("exa", first), options.url))
    assert legacy["details"]["quality"]["verdict"] == "degraded"


def test_cancelled_extraction_never_starts_backup(monkeypatch):
    async def exercise():
        started = asyncio.Event()

        class BlockingExtractor(FixtureExtractor):
            async def extract(self, options):
                self.calls += 1
                started.set()
                await asyncio.Event().wait()

        options, registry, _, backup = extraction_registry(monkeypatch, None)
        primary = BlockingExtractor("exa", None)
        registry.register(primary)
        task = asyncio.create_task(route("extract", options, registry))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (primary.calls, backup.calls) == (1, 0)

    asyncio.run(exercise())


def test_challenge_is_classified_before_clipping(monkeypatch):
    options = ExtractOptions("https://fixture.invalid", max_characters=5)
    first = make_extraction(options, "Just a moment... Enable JavaScript and cookies.", method="fixture")
    options, registry, primary, backup = extraction_registry(monkeypatch, first)
    result = asyncio.run(route("extract", options, registry))
    assert result.actual_provider == "brave"
    assert result.fallback_chain[0].error == "challenge_page"
    assert (primary.calls, backup.calls) == (1, 1)


def test_truncation_does_not_collect_a_second_body(monkeypatch):
    options = ExtractOptions("https://fixture.invalid", max_characters=1000)
    text = "Just a moment: enable JavaScript and cookies is a challenge-page phrase. " * 40
    first = make_extraction(options, text, method="fixture", source_complete=True)
    options, registry, primary, backup = extraction_registry(monkeypatch, first)
    result = asyncio.run(route("extract", options, registry))
    assert result.payload.text == text[:1000]
    assert result.quality.verdict == "degraded"
    assert result.payload.partial is True
    assert (primary.calls, backup.calls) == (1, 0)


def test_complete_fixture_metadata_agrees_in_both_host_envelopes(monkeypatch, capsys):
    import wrr.registry as registry_module

    body = make_extraction(ExtractOptions("https://fixture.invalid/article"), "Full fixture body.",
                           method="fixture", source_complete=True, content_type="text/plain")
    options, registry, _, _ = extraction_registry(monkeypatch, body)
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    monkeypatch.setattr(web_fetch, "get_registry", lambda: registry)
    args = _cli.build_parser().parse_args(["fetch", options.url, "--provider", "exa", "--json"])
    assert _cli.cmd_fetch(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(web_fetch.handle_web_fetch({
        "url": options.url, "provider": "exa",
    })))
    metadata = hermes_result["details"]["extraction"]
    assert metadata == {
        "content_kind": "body", "content_type": "text/plain",
        "completeness": "complete", "extraction_method": "fixture",
        "fetched_at": body.fetched_at, "partial": False,
    }
    assert {key: cli_result["result"][key] for key in metadata} == metadata
    assert cli_result["result"]["text"] == "Full fixture body."
    assert cli_result["quality"]["verdict"] == "complete"
    assert hermes_result["details"]["quality"] == cli_result["quality"]
