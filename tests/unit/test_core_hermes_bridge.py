"""Hermes sync, provider envelope and credential-boundary contracts (offline)."""
import asyncio
from contextvars import ContextVar
import importlib.util
from pathlib import Path
import sys
import threading
import types

import pytest

from wrr.core import hermes_bridge, inventory, judge, sources
from wrr.core.contracts import Health, Kind, PrivacyClass, Provision, SourceDescriptor, Transport
from wrr.core.judge import ReplayJudge
from wrr.schemas import SearchResult


def answers(state, questions):
    result = {}
    for key, question in questions.items():
        if question.type == "choice":
            selected = "overview" if key == "evidence_shape" else "accept"
            result[key] = {"type": "choice", "choice": selected, "confidence": .9,
                           "probabilities": {option: float(option == selected)
                                             for option in question.criteria}}
        else:
            result[key] = {"type": "noul", "noul": .1 if key.startswith("inj.") else .9}
    return result


@pytest.fixture
def core_search(monkeypatch):
    observations = []
    descriptors = [SourceDescriptor(
        "exa", Kind.WEB, Transport.API, "exa", PrivacyClass.PUBLIC,
        provision=Provision.AUTHORIZED, health=Health.HEALTHY, primary=True,
    )]

    class Adapter:
        async def search(self, query, count, *, deadline):
            observations.append((query, count, threading.get_ident(), asyncio.get_running_loop()))
            return [SearchResult("Public finding", "https://example.com/finding", "Grounded evidence")]

    monkeypatch.setattr(inventory, "build_inventory", lambda **kwargs: descriptors)
    monkeypatch.setattr(sources, "build_adapters", lambda *args, **kwargs: {"exa": Adapter()})
    monkeypatch.setattr(judge, "resolve_judge", lambda **kwargs: ReplayJudge(answers))
    return observations


def test_bridge_returns_core_evidence(core_search):
    result = hermes_bridge.search_sync("needle", count=2)
    assert result["plan"]["sources"] == ["exa"]
    assert result["plan"]["judge"] == "replay"
    assert result["results"][0]["url"] == "https://example.com/finding"
    assert result["diagnostics"]["judge"]["calls"] == 3
    assert core_search[0][:3] == ("needle", 2, threading.get_ident())


def test_bridge_uses_worker_loop_inside_running_loop(core_search, monkeypatch):
    scope = ContextVar("hermes_test_scope", default=None)
    original = hermes_bridge.search_evidence
    seen_scopes = []

    async def scoped_search(*args, **kwargs):
        seen_scopes.append(scope.get())
        return await original(*args, **kwargs)

    monkeypatch.setattr(hermes_bridge, "search_evidence", scoped_search)

    async def host():
        host_loop = asyncio.get_running_loop()
        token = scope.set("profile-a")
        try:
            result = hermes_bridge.search_sync("needle", count=1)
        finally:
            scope.reset(token)
        assert result["quality"]["executed"] == "ok"
        assert core_search[0][2] != threading.get_ident()
        assert core_search[0][3] is not host_loop
        assert not host_loop.is_closed()

    asyncio.run(host())
    assert core_search[0][3].is_closed()
    assert seen_scopes == ["profile-a"]


def test_bridge_loads_only_hermes_authorized_snapshot(core_search, monkeypatch, tmp_path):
    import os
    from wrr.core import runtime

    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text("TYPESAFE_API_KEY=hermes-test-value\n")
    (tmp_path / ".omp").mkdir()
    (tmp_path / ".omp" / ".env").write_text("TYPESAFE_API_KEY=wrong-host-value\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WRR_RUNTIME", "omp")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("WRR_ENV", raising=False)
    identities = []
    environments = []
    original_detect = runtime.detect_runtime

    def detect(**kwargs):
        identity = original_detect(**kwargs)
        identities.append(identity.name)
        return identity

    def resolve(**kwargs):
        environments.append(kwargs)
        return ReplayJudge(answers)

    monkeypatch.setattr(runtime, "detect_runtime", detect)
    monkeypatch.setattr(judge, "resolve_judge", resolve)
    hermes_bridge.search_sync("needle")
    assert identities == ["hermes"]
    assert environments[0]["env"]["TYPESAFE_API_KEY"] == "hermes-test-value"
    assert "host_judge" not in environments[0]
    assert "TYPESAFE_API_KEY" not in os.environ


@pytest.fixture
def plugin(monkeypatch):
    # Hermes is not a WRR dependency. Supply only its documented backend API.
    common = types.ModuleType("plugins.web._common")

    class BaseWebSearchProvider:
        name = property(lambda self: self.NAME)

        def supports_extract(self):
            return self.EXTRACT

    common.BaseWebSearchProvider = BaseWebSearchProvider
    common.search_ok = lambda hits: {"success": True, "data": {"web": hits}}
    common.search_fail = lambda error: {"success": False, "error": error}
    common.web_hit = lambda url, title, description, position: {
        "url": url, "title": title, "description": description, "position": position,
    }
    monkeypatch.setitem(sys.modules, "plugins.web._common", common)
    root = Path(__file__).resolve().parents[2] / "hermes-plugin"
    spec = importlib.util.spec_from_file_location(
        "_wrr_test_backend", root / "__init__.py", submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name + ".provider", None)


def provider_for(plugin):
    providers = []
    plugin.register(types.SimpleNamespace(register_web_search_provider=providers.append))
    assert len(providers) == 1
    return providers[0]


def test_registered_provider_preserves_web_and_wrr_envelopes(plugin, core_search):
    provider = provider_for(plugin)
    response = provider.search("needle", limit=2)
    assert provider.name == "wrr"
    assert provider.supports_extract() is False
    assert response["success"] is True
    assert response["data"]["web"] == [{
        "url": "https://example.com/finding", "title": "Public finding",
        "description": "Grounded evidence", "position": 1,
    }]
    assert response["data"]["wrr"]["plan"]["sources"] == ["exa"]
    assert response["data"]["wrr"]["quality"]["executed"] == "ok"
    assert response["data"]["wrr"]["diagnostics"]["judge"]["calls"] == 3
    assert core_search[0][:2] == ("needle", 2)


def test_availability_is_local_primary_key_check(plugin, monkeypatch):
    provider = provider_for(plugin)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "judge-only")

    def forbidden(*args, **kwargs):
        pytest.fail("availability performed a search")

    monkeypatch.setattr(hermes_bridge, "search_sync", forbidden)
    assert provider.is_available() is False
    monkeypatch.setenv("DEEPSEEK_API_KEY", "local-test-value")
    assert provider.is_available() is True
    monkeypatch.setenv("DEEPSEEK_API_KEY", " ")
    assert provider.is_available() is False


@pytest.mark.parametrize("error,category", [
    (ImportError("secret-sentinel"), "dependency_unavailable"),
    (ValueError("secret-sentinel"), "configuration_error"),
    (OSError("secret-sentinel"), "configuration_error"),
    (TimeoutError("secret-sentinel"), "timeout"),
    (RuntimeError("secret-sentinel"), "search_failed"),
])
def test_provider_errors_never_expose_exception_text(plugin, monkeypatch, error, category):
    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(hermes_bridge, "search_sync", broken)
    assert provider_for(plugin).search("needle", 1) == {
        "success": False, "error": "WRR " + category,
    }


@pytest.mark.parametrize("executed", ["failed", "cancelled"])
def test_provider_does_not_claim_success_for_failed_execution(plugin, monkeypatch, executed):
    evidence = {"results": [], "plan": {"sources": ["exa"]},
                "quality": {"executed": executed}, "diagnostics": {"judge": {"calls": 0}}}
    monkeypatch.setattr(hermes_bridge, "search_sync", lambda *args, **kwargs: evidence)
    result = provider_for(plugin).search("needle", 1)
    assert result["success"] is False
    assert result["error"] == "WRR " + executed
    assert result["data"]["web"] == []
    assert result["data"]["wrr"]["quality"]["executed"] == executed
