"""Core adapter seam: trusted manifests, bounded execution and stable failures."""
import asyncio
from dataclasses import replace
import importlib
import json
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from wrr import config
from wrr.core import exec_declarative, sources
from wrr.core.contracts import Health, Kind, PrivacyClass, Provision, RunStatus, SourceDescriptor, Transport
from wrr.core.inventory import build_inventory
from wrr.engines.loader import builtin_manifest_paths, load_engine_manifest
from wrr.engines.community_sources import SourceFetchResult, SourceOutcome
from wrr.errors import EngineError, EngineTimeoutError, RateLimitError
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env
from wrr.schemas import SearchResult


def descriptor(**kwargs):
    return replace(SourceDescriptor(
        id="test", kind=Kind.WEB, transport=Transport.API, upstream_family="test",
        privacy_class=PrivacyClass.PUBLIC, provision=Provision.AUTHORIZED,
        health=Health.HEALTHY, adapter="wrr.engines.exa:ExaEngine",
    ), **kwargs)


def search(adapter, *, deadline_seconds=1):
    async def run():
        return await adapter.search("WRR test", 3, deadline=asyncio.get_running_loop().time() + deadline_seconds)
    return asyncio.run(run())


def fake_engine(monkeypatch, implementation):
    monkeypatch.setattr("wrr.engines.exa.ExaEngine.search", implementation)
    return sources.build_adapters([descriptor()], env={})["test"]


@pytest.mark.parametrize("path", builtin_manifest_paths(), ids=lambda path: path.parent.name)
def test_every_builtin_adapter_search_mock(monkeypatch, path):
    manifest = load_engine_manifest(path, source="builtin", trust_level="builtin").manifest
    module_name, _, name = manifest.adapter.partition(":")
    cls = getattr(importlib.import_module(module_name), name)
    called = []
    async def implementation(self, options):
        called.append((options.query, options.count, options.exa_mode))
        return [SearchResult("Result", "https://example.test")]
    monkeypatch.setattr(cls, "search", implementation)
    adapter = sources.build_adapters([descriptor(adapter=manifest.adapter)], env={}, exa_mode="fast")["test"]
    assert len(search(adapter)) == 1
    assert called == [("WRR test", 3, "fast")]


def test_sync_engine_runs_in_thread_and_receives_count(monkeypatch):
    parent_thread = threading.get_ident()
    def implementation(self, options):
        assert threading.get_ident() != parent_thread
        assert options.query == "WRR test" and options.count == 3
        assert config.get_env("TOKEN") == "snapshot"
        return [SearchResult("Result", "https://example.test")]
    monkeypatch.setattr("wrr.engines.exa.ExaEngine.search", implementation)
    adapter = sources.build_adapters([descriptor()], env={"TOKEN": "snapshot"})["test"]
    assert len(search(adapter)) == 1


def test_task_scoped_environment_does_not_leak(monkeypatch):
    monkeypatch.setenv("TOKEN", "host")
    async def implementation(self, options):
        await asyncio.sleep(0)
        return [SearchResult(config.get_env("TOKEN"), "https://example.test")]
    monkeypatch.setattr("wrr.engines.exa.ExaEngine.search", implementation)
    first = sources.build_adapters([descriptor()], env={"TOKEN": "one"})["test"]
    second = sources.build_adapters([descriptor()], env={"TOKEN": "two"})["test"]
    async def run():
        deadline = asyncio.get_running_loop().time() + 1
        return await asyncio.gather(first.search("q", 3, deadline=deadline), second.search("q", 3, deadline=deadline))
    assert [rows[0].title for rows in asyncio.run(run())] == ["one", "two"]
    assert config.get_env("TOKEN") == "host"


def test_deadline_cancels_async_engine(monkeypatch):
    cancelled = []
    async def implementation(self, options):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    adapter = fake_engine(monkeypatch, implementation)
    with pytest.raises(sources.AdapterError) as error:
        search(adapter, deadline_seconds=0.01)
    assert error.value.status == RunStatus.TIMEOUT and cancelled == [True]


def test_remaining_deadline_replaces_legacy_engine_timeout(monkeypatch):
    from wrr.engines.exa import get_timeout_for_mode
    async def implementation(self, options):
        assert 19 < self.timeout <= 20
        assert 19 < get_timeout_for_mode("auto") <= 20
        return []
    assert search(fake_engine(monkeypatch, implementation), deadline_seconds=20) == []


def test_expired_deadline_does_not_load_engine(monkeypatch):
    monkeypatch.setattr(sources, "_load_adapter_class", lambda _: pytest.fail("expired request loaded engine"))
    with pytest.raises(sources.AdapterError) as error:
        search(sources.build_adapters([descriptor()], env={})["test"], deadline_seconds=-1)
    assert error.value.status == RunStatus.TIMEOUT


def test_caller_cancellation_is_not_mapped(monkeypatch):
    async def implementation(self, options):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        search(fake_engine(monkeypatch, implementation))


@pytest.mark.parametrize("exception,status", [
    (EngineTimeoutError("secret"), RunStatus.TIMEOUT),
    (TimeoutError("secret"), RunStatus.TIMEOUT),
    (httpx.ReadTimeout("secret"), RunStatus.TIMEOUT),
    (RateLimitError("secret"), RunStatus.RATE_LIMIT),
    (EngineError("EXA_API_KEY not set"), RunStatus.AUTH_ERROR),
    (EngineError("HTTP 401"), RunStatus.AUTH_ERROR),
    (EngineError("HTTP 403"), RunStatus.AUTH_ERROR),
    (EngineError("HTTP 429"), RunStatus.RATE_LIMIT),
    (EngineError("qmd search timed out >1s"), RunStatus.TIMEOUT),
    (EngineError("invalid JSON"), RunStatus.SCHEMA_ERROR),
    (ValueError("secret"), RunStatus.SCHEMA_ERROR),
    (EngineError("upstream unavailable secret"), RunStatus.ERROR),
    (exec_declarative.DeclarativeError("auth_error", "secret"), RunStatus.AUTH_ERROR),
])
def test_engine_failures_are_stable_and_redacted(monkeypatch, exception, status):
    async def implementation(self, options):
        raise exception
    with pytest.raises(sources.AdapterError) as error:
        search(fake_engine(monkeypatch, implementation))
    assert error.value.status == status
    assert str(error.value) == status.value
    assert error.value.__cause__ is None


@pytest.mark.parametrize("status_code,status", [(401, RunStatus.AUTH_ERROR), (403, RunStatus.AUTH_ERROR), (429, RunStatus.RATE_LIMIT), (503, RunStatus.ERROR)])
def test_wrapped_http_status_is_preserved(monkeypatch, status_code, status):
    response = httpx.Response(status_code, request=httpx.Request("GET", "https://secret.test"))
    async def implementation(self, options):
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as cause:
            raise EngineError("upstream failure") from cause
    with pytest.raises(sources.AdapterError) as error:
        search(fake_engine(monkeypatch, implementation))
    assert error.value.status == status


@pytest.mark.parametrize("rows", [None, {}, [{}]])
def test_invalid_result_shape_is_schema_error(monkeypatch, rows):
    async def implementation(self, options):
        return rows
    with pytest.raises(sources.AdapterError) as error:
        search(fake_engine(monkeypatch, implementation))
    assert error.value.status == RunStatus.SCHEMA_ERROR


def test_empty_results_are_left_to_pipeline(monkeypatch):
    async def implementation(self, options):
        return []
    assert search(fake_engine(monkeypatch, implementation)) == []


def test_host_remote_and_nonroutable_sources_are_not_loaded(monkeypatch):
    monkeypatch.setattr(sources, "_load_adapter_class", lambda _: pytest.fail("must not load"))
    assert sources.build_adapters([
        descriptor(id="host", transport=Transport.HOST, adapter=None),
        descriptor(id="remote", transport=Transport.REMOTE, adapter=None),
        descriptor(id="disabled", provision=Provision.DISABLED),
        descriptor(id="unhealthy", health=Health.UNHEALTHY),
        descriptor(id="missing", adapter=None),
    ], env={}) == {}


@pytest.mark.parametrize("transport", [Transport.HOST, Transport.REMOTE, Transport.API])
def test_execution_references_are_loaded_regardless_of_transport(transport):
    source = descriptor(transport=transport)
    assert "test" in sources.build_adapters([source], env={})


@pytest.mark.parametrize("references,expected", [
    ({}, False),
    ({"adapter": "example:Engine"}, True),
    ({"declarative": {}}, True),
    ({"configured_source": {}}, True),
])
def test_executable_reports_execution_references(references, expected):
    source = descriptor(adapter=None, **references) if "adapter" not in references else descriptor(**references)
    assert source.executable is expected
    assert source.to_dict()["executable"] is expected


def test_declarative_manifest_precedes_engine_reference(monkeypatch):
    block = {"mode": "http", "http": {"url": "https://example.test/search"},
             "results": {"root": "$.results", "title": "$.title", "url": "$.url"}}
    called = []
    async def run(spec, query, count, *, env, deadline):
        called.append((spec.mode, query, count, env))
        assert 0 < deadline - time.monotonic() <= 1
        return [SearchResult("Result", "https://example.test")]
    monkeypatch.setattr(exec_declarative, "run_declarative", run)
    monkeypatch.setattr(sources, "_load_adapter_class", lambda _: pytest.fail("declarative tried engine"))
    adapter = sources.build_adapters([descriptor(declarative=block)], env={"TOKEN": "snapshot"})["test"]
    assert len(search(adapter)) == 1
    assert called == [("http", "WRR test", 3, {"TOKEN": "snapshot"})]


def test_inventory_carries_nonserialized_execution_reference(tmp_path):
    root = tmp_path / "plugins" / "engines" / "sample"
    root.mkdir(parents=True)
    block = {"mode": "cli", "cli": {"command": [sys.executable, "-c", "pass"]},
             "results": {"root": "$", "title": "$.title", "url": "$.url"}}
    (root / "engine.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "sample", "name": "Sample", "kind": "cli",
        "declarative": block, "capabilities": {"actions": ["search"]}, "routing": {"modes": []},
        "adapter": None, "requirements": {"env": []}, "health": {"checks": []},
    }))
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    env = load_env(runtime, env_files=[], overrides={})
    source = next(row for row in build_inventory(runtime=runtime, env=env, trust_project=True) if row.id == "sample")
    assert source.declarative == block and source.routable
    assert "declarative" not in source.to_dict() and "adapter" not in source.to_dict()
    assert source == replace(source, declarative=None, adapter="another:Class")
    assert "sample" in sources.build_adapters([source], env={})


def mcp_spec():
    spec = exec_declarative.DeclarativeSpec.parse({
        "mode": "mcp", "mcp": {"server": "papers", "tool": "search", "query_arg": "term", "count_arg": "limit"},
        "results": {"root": "$.data.rows", "title": "$.name", "url": "$.link"},
    })
    assert isinstance(spec, exec_declarative.DeclarativeSpec)
    return spec


def mcp_config(tmp_path, *, authorized=True, tool="search"):
    path = tmp_path / "sources.json"
    path.write_text(json.dumps({
        "schema_version": 1, "sources": {"papers": {"kind": "mcp", "command": [sys.executable], "tool": tool, "shared_env_keys": ["TOKEN"]}},
        "authorized_sources": ["papers"] if authorized else [],
    }))
    return {"WRR_COMMUNITY_CONFIG": str(path), "TOKEN": "snapshot"}


def test_mcp_resolves_only_authorized_config_and_maps_nested_payload(tmp_path, monkeypatch):
    called = []
    async def fetch(self, cfg, options, runner, timeout, *, rows_from_result):
        called.append((cfg, options.count))
        assert config.get_env("TOKEN") == "snapshot"
        rows = rows_from_result(SimpleNamespace(structured_content={"data": {"rows": [{"name": "Paper", "link": "https://example.test/paper"}]}}, content=[]))
        return SourceFetchResult(items=rows, outcome=SourceOutcome.READY)
    monkeypatch.setattr("wrr.engines.mcp_source.McpSourceAdapter.fetch_result", fetch)
    env = mcp_config(tmp_path)
    rows = asyncio.run(exec_declarative.run_declarative(mcp_spec(), "WRR test", 3, env=env, deadline=time.monotonic() + 1))
    assert [row.title for row in rows] == ["Paper"]
    assert called[0][0]["authorized"] is True and called[0][0]["command"] == [sys.executable]
    assert called[0][0]["query_arg"] == "term" and called[0][1] == 3


@pytest.mark.parametrize("authorized,tool", [(False, "search"), (True, "write")])
def test_mcp_manifest_cannot_grant_or_change_authorized_tool(tmp_path, monkeypatch, authorized, tool):
    monkeypatch.setattr("wrr.engines.mcp_source.McpSourceAdapter.fetch_result", lambda *args, **kwargs: pytest.fail("unauthorized MCP executed"))
    with pytest.raises(exec_declarative.DeclarativeError) as error:
        asyncio.run(exec_declarative.run_declarative(mcp_spec(), "q", 3, env=mcp_config(tmp_path, authorized=authorized, tool=tool), deadline=time.monotonic() + 1))
    assert error.value.status == "auth_error"


def test_mcp_unknown_server_remains_unsupported():
    with pytest.raises(NotImplementedError):
        asyncio.run(exec_declarative.run_declarative(mcp_spec(), "q", 3, env={}, deadline=time.monotonic() + 1))


def test_configured_inventory_source_executes_itself_not_other_community_sources(tmp_path, monkeypatch):
    from wrr.engines.community import CommunityEngine
    called = []
    async def execution(self, source_id, options, now):
        called.append((source_id, options.count, self._configured_sources))
        return SimpleNamespace(diagnostic=SimpleNamespace(outcome=SourceOutcome.READY), scored=[(1, SearchResult("Paper", "https://example.test"))])
    monkeypatch.setattr(CommunityEngine, "_fetch_source_execution", execution)
    cfg = {"kind": "mcp", "authorized": True, "command": [sys.executable], "tool": "search"}
    source = descriptor(id="papers", adapter="wrr.engines.community:CommunityEngine", configured_source=cfg)
    assert len(search(sources.build_adapters([source], env={})["papers"])) == 1
    assert called == [("papers", 3, {"papers": cfg})]
