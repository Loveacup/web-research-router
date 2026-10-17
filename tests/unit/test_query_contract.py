"""Cross-host observable query policy; providers are deterministic and offline."""
import asyncio
import json

import pytest

from conftest import FakeEngine, mk_results
from wrr import _cli
from wrr.registry import EngineRegistry
from wrr.tools.web_search import execute_web_search


def test_default_search_has_same_policy_in_cli_and_hermes(monkeypatch, capsys):
    import wrr.registry as registry_module

    registry = EngineRegistry()
    registry.register(FakeEngine("exa", search_results=mk_results(2)))
    registry.register(FakeEngine("brave", search_results=mk_results(2)))
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    args = _cli.build_parser().parse_args(["search", "needle", "--json"])

    assert _cli.cmd_search(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(execute_web_search(
        {"query": "needle"}, registry=registry, stage_s_enabled=False,
    )))
    assert hermes_result["success"] is True
    details = hermes_result["details"]
    assert cli_result["provider"] == details["provider"]
    assert cli_result["quality"] == details["quality"]
    assert cli_result["fallback_chain"] == details["fallback_chain"]


def test_cli_does_not_probe_another_hosts_credentials(monkeypatch, tmp_path):
    import builtins
    import os

    home = tmp_path / "home"
    host_env = home / ".hermes" / ".env"
    host_env.parent.mkdir(parents=True)
    host_env.write_text("WRR_CREDENTIAL_SENTINEL=private-host-value\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("WRR_ENV", raising=False)
    monkeypatch.delenv("WRR_CREDENTIAL_SENTINEL", raising=False)
    accesses = []
    original_open = builtins.open
    original_stat = os.stat

    def tracked_open(path, *args, **kwargs):
        if str(path) == str(host_env):
            accesses.append("open")
        return original_open(path, *args, **kwargs)

    def tracked_stat(path, *args, **kwargs):
        if str(path) == str(host_env):
            accesses.append("stat")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(os, "stat", tracked_stat)
    _cli.load_env()
    assert accesses == []
    assert "WRR_CREDENTIAL_SENTINEL" not in os.environ


def test_missing_explicit_env_never_uses_default_credentials(monkeypatch, tmp_path, capsys):
    import os

    default = tmp_path / ".config" / "wrr" / "env.d" / "default.env"
    default.parent.mkdir(parents=True)
    default.write_text("WRR_CREDENTIAL_SENTINEL=wrong-account\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WRR_ENV", raising=False)
    monkeypatch.delenv("WRR_CREDENTIAL_SENTINEL", raising=False)
    with pytest.raises(FileNotFoundError):
        _cli.load_env(str(tmp_path / "missing.env"))
    assert "WRR_CREDENTIAL_SENTINEL" not in os.environ
    assert _cli.main(["search", "needle", "--env", str(tmp_path / "missing.env"), "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"] == "configuration_error"
    assert "wrong-account" not in json.dumps(output)


def test_descriptor_search_does_not_reopen_host_credentials(monkeypatch, tmp_path):
    import builtins
    from wrr.engines.exa import ExaEngine
    from wrr.errors import AllEnginesFailedError
    from wrr.schemas import SearchOptions

    host_env = tmp_path / ".hermes" / ".env"
    host_env.parent.mkdir()
    host_env.write_text("EXA_API_KEY=descriptor-host-sentinel\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WRR_RUNTIME", "hermes")
    monkeypatch.setenv("WRR_V6_ROUTER", "1")
    monkeypatch.delenv("WRR_ENV", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    opened = []
    original_open = builtins.open
    # pathlib uses io.open, while the legacy CLI loader uses builtins.open.
    import io
    original_io_open = io.open

    def record(opener, path, *args, **kwargs):
        if str(path) == str(host_env):
            opened.append(str(path))
        return opener(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", lambda p, *a, **k: record(original_open, p, *a, **k))
    monkeypatch.setattr(io, "open", lambda p, *a, **k: record(original_io_open, p, *a, **k))

    async def empty_provider(self, options):
        return []

    monkeypatch.setattr(ExaEngine, "search", empty_provider)
    with pytest.raises(AllEnginesFailedError):
        asyncio.run(_cli._run("search", SearchOptions("needle", provider="exa"), "exa"))
    assert opened == []


def test_explicit_provider_failure_never_falls_back_across_hosts(monkeypatch, capsys):
    import wrr.registry as registry_module

    calls = []

    class BackupEngine(FakeEngine):
        async def search(self, options):
            calls.append(self.name)
            return mk_results(2)

    registry = EngineRegistry()
    registry.register(FakeEngine("exa", error="provider unavailable"))
    registry.register(BackupEngine("brave"))
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    args = _cli.build_parser().parse_args(["search", "needle", "--provider", "exa", "--json"])
    assert _cli.cmd_search(args) == 1
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(execute_web_search(
        {"query": "needle", "provider": "exa"}, registry=registry, stage_s_enabled=False,
    )))
    assert cli_result["ok"] is False
    assert "error" in hermes_result
    assert cli_result["quality"] == hermes_result["details"]["quality"]
    assert calls == []


@pytest.mark.parametrize("operation", ["fetch", "similar"])
def test_nonsearch_operations_share_provider_fallback_and_quality(operation, monkeypatch, capsys):
    import wrr.registry as registry_module
    from wrr.tools import web_fetch, web_similar

    registry = EngineRegistry()
    if operation == "fetch":
        registry.register(FakeEngine("exa", error="primary unavailable"))
        registry.register(FakeEngine("brave", extract_text="A deterministic article body."))
        host = web_fetch
        handler = host.handle_web_fetch
    else:
        registry.register(FakeEngine("exa", similar_results=mk_results(2)))
        host = web_similar
        handler = host.handle_web_similar
    monkeypatch.setattr(registry_module, "get_registry", lambda: registry)
    monkeypatch.setattr(host, "get_registry", lambda: registry)
    args = _cli.build_parser().parse_args([operation, "https://fixture.invalid/article", "--json"])
    assert args.func(args) == 0
    cli_result = json.loads(capsys.readouterr().out)
    hermes_result = json.loads(asyncio.run(handler({"url": "https://fixture.invalid/article"})))
    assert hermes_result["success"] is True
    details = hermes_result["details"]
    assert cli_result["provider"] == details["provider"]
    assert cli_result["fallback_chain"] == details["fallback_chain"]
    assert cli_result["quality"] == details["quality"]
    if operation == "fetch":
        assert cli_result["result"]["text"] == "A deterministic article body."
        assert cli_result["result"]["text"] in hermes_result["content"]
    else:
        fields = ("title", "url", "snippet", "highlights", "source_tag")
        assert [
            {key: item[key] for key in fields} for item in cli_result["result"]
        ] == [
            {key: item[key] for key in fields} for item in details["results"]
        ]
