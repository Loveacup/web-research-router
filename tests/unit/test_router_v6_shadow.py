"""Router v6 shadow consumption tests."""

import asyncio
import os
import json
import sys

import pytest

from conftest import FakeEngine
from wrr.errors import AllEnginesFailedError
from wrr.router import route_search_v5
from wrr.schemas import SearchOptions, SearchResult


def run(coro):
    return asyncio.run(coro)


class MinimalRegistry:
    """Descriptor-backed registry stand-in: router only needs get(name)."""

    def __init__(self, engines):
        self._engines = {engine.name: engine for engine in engines}

    def get(self, name):
        return self._engines.get(name)


def _results(provider, n=2):
    return [
        SearchResult(
            title=f"{provider} title {i}",
            url=f"https://example.test/{provider}/{i}",
            snippet=f"{provider} snippet {i}",
        )
        for i in range(n)
    ]


def _registry(names):
    return MinimalRegistry(
        FakeEngine(name, search_results=_results(name))
        for name in names
    )


def _selected(result):
    return [step.provider for step in result.fallback_chain]


def test_unit_default_isolates_live_v6_router_env(monkeypatch):
    """Unit tests must consume their fake registry, not a live inherited v6 registry."""
    assert os.environ["WRR_V6_ROUTER"] == "0"

    def fail_if_descriptor_registry_is_built():
        raise AssertionError("unit default must not build descriptor-backed registry")

    monkeypatch.setattr("wrr.router._descriptor_backed_registry", fail_if_descriptor_registry_is_built)

    result = run(route_search_v5(SearchOptions("what is python"), _registry(("exa",))))

    assert result.actual_provider == "rrf:grounding"
    assert _selected(result) == ["exa", "brave"]


@pytest.mark.parametrize(
    "query,mode",
    [
        ("what is python", "grounding"),
        ("深度分析 ai", "research"),
        ("survey of llm", "academic"),
        ("best python tools", "discovery"),
        ("gpt site:reddit.com", "platform"),
        ("missing deleted config", "recovery"),
    ],
)
def test_old_and_descriptor_backed_registries_select_same_engines(query, mode):
    names = ("exa", "brave", "searxng", "github", "community", "academic", "skill")
    old_registry = _registry(names)
    descriptor_backed = _registry(names)

    old = run(route_search_v5(SearchOptions(query, count=10), old_registry))
    shadow = run(route_search_v5(SearchOptions(query, count=10), descriptor_backed))

    assert old.mode == mode
    assert shadow.mode == mode
    assert old.actual_provider == shadow.actual_provider == f"rrf:{mode}"
    assert _selected(old) == _selected(shadow)
    assert old.weights == shadow.weights


def test_route_search_v5_accepts_injected_descriptor_backed_registry_helper():
    old_registry = _registry(())
    descriptor_backed = _registry(("exa", "brave"))

    result = run(
        route_search_v5(
            SearchOptions("what is python", count=5),
            old_registry,
            descriptor_registry_factory=lambda: descriptor_backed,
        )
    )

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]


def test_explicit_provider_behavior_is_unchanged_with_descriptor_backed_registry():
    registry = _registry(("exa", "brave"))

    result = run(route_search_v5(SearchOptions("q", provider="brave"), registry))

    assert result.actual_provider == "brave"
    assert _selected(result) == ["brave"]
    assert len(result.payload) == 2


def test_v6_router_env_flag_uses_shadow_registry(monkeypatch):
    shadow_registry = _registry(("exa", "brave"))

    monkeypatch.setenv("WRR_V6_ROUTER", "1")
    monkeypatch.setattr(
        "wrr.router._descriptor_backed_registry",
        lambda: shadow_registry,
    )

    result = run(route_search_v5(SearchOptions("what is python"), _registry(())))

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]


def test_without_v6_router_env_flag_keeps_legacy_registry(monkeypatch):
    monkeypatch.delenv("WRR_V6_ROUTER", raising=False)

    def fail_if_called():
        raise AssertionError("v6 shadow registry must not be built")

    monkeypatch.setattr("wrr.router._descriptor_backed_registry", fail_if_called)

    result = run(route_search_v5(SearchOptions("what is python"), _registry(("exa",))))

    assert result.mode == "grounding"
    assert _selected(result) == ["exa", "brave"]
    assert result.fallback_chain[0].ok is True
    assert result.fallback_chain[1].ok is False
    assert result.fallback_chain[1].error == "unknown provider: brave"


def _routable_probe_manifest(engine_id, marker):
    """Manifest that is routable via light/static checks but carries a live probe."""

    return {
        "schema_version": 1,
        "id": engine_id,
        "name": engine_id.title(),
        "kind": "web_api",
        # Real importable adapter so the bridge instantiates a SearchEngine.
        "adapter": "wrr.engines.exa:ExaEngine",
        "capabilities": {"actions": ["search"], "domains": ["web"]},
        "routing": {"modes": ["auto", "web"], "weight": 1.0},
        "requirements": {"env": [], "binaries": [], "repos": []},
        "health": {
            "checks": [
                {"type": "env_present", "env": "OPTIONAL_KEY", "required": False},
                {
                    "type": "live_probe",
                    "level": "live",
                    "required": True,
                    "command": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')"
                        ),
                    ],
                },
            ]
        },
        "requires_capabilities": {},
    }


def test_default_registry_v6_shadow_bridges_only_routable_without_live_probe(tmp_path, monkeypatch):
    from wrr.runtime.detect import detect_runtime
    from wrr.runtime.env import load_env
    from wrr.registry import default_registry_v6_shadow

    marker = tmp_path / "probe.marker"
    plugin_dir = tmp_path / "plugins" / "engines" / "exa"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(
        json.dumps(_routable_probe_manifest("exa", marker)),
        encoding="utf-8",
    )

    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    env = load_env(runtime, overrides={}, env_files=[])

    monkeypatch.setattr(
        "wrr.engines.registry._live_probe_check",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("shadow bridge must not run live probes")
        ),
    )

    report = default_registry_v6_shadow(
        runtime=runtime,
        env=env,
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
        state_file=tmp_path / "state.json",
    )

    # Bridge instantiated the single routable descriptor's adapter, no probe ran.
    assert report.registry.names() == ["exa"]
    assert not marker.exists()


def test_without_v6_router_env_flag_empty_legacy_registry_stays_empty(monkeypatch):
    monkeypatch.delenv("WRR_V6_ROUTER", raising=False)

    with pytest.raises(AllEnginesFailedError):
        run(route_search_v5(SearchOptions("what is python"), _registry(())))
