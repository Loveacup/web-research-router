"""P0-T4 v6 registry resolve and light health tests."""

import json

from wrr.engines.loader import parse_engine_manifest
from wrr.engines.registry import EngineRegistry, check_runtime_capabilities
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env


def _manifest(engine_id="tavily", **overrides):
    payload = {
        "schema_version": 1,
        "id": engine_id,
        "name": engine_id.title(),
        "kind": "web_api",
        "adapter": f"wrr.engines.{engine_id}:{engine_id.title()}Engine",
        "capabilities": {"actions": ["search"], "domains": ["web"]},
        "routing": {"modes": ["auto", "web"], "weight": 1.0},
        "requirements": {"env": [], "binaries": [], "repos": []},
        "health": {"checks": []},
        "requires_capabilities": {"can_read_user_env": True},
    }
    payload.update(overrides)
    return payload


def _runtime(tmp_path, explicit="standalone"):
    return detect_runtime(explicit=explicit, cwd=tmp_path, env={})


def _env(runtime, overrides=None):
    return load_env(runtime, overrides=overrides or {}, env_files=[])


def test_missing_required_env_is_unhealthy_and_not_routable(tmp_path):
    runtime = _runtime(tmp_path)
    registry = EngineRegistry(runtime=runtime, env=_env(runtime), include_builtin=True)

    report = registry.report()
    exa = next(item for item in report.resolved if item.id == "exa")

    assert exa.configured is False
    assert "missing_required_env:EXA_API_KEY" in exa.resolve_reasons
    assert exa.health.status == "unhealthy"
    assert exa.routable is False
    assert "health_unhealthy" in exa.routable_reasons
    assert set(report.to_dict()) == {"discovered", "resolved", "health", "routable"}


def test_required_env_alias_can_satisfy_configuration_and_health(tmp_path):
    runtime = _runtime(tmp_path)
    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime, {"BRAVE_SEARCH_API_KEY": "secret"}),
        include_builtin=True,
    )

    brave = registry.get("brave")

    assert brave is not None
    assert brave.configured is True
    assert brave.health.status == "healthy"
    assert brave.routable is True


def test_runtime_capability_gate_blocks_incompatible_engine(tmp_path):
    plugin_dir = tmp_path / "plugins" / "engines" / "memory"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(
        json.dumps(
            _manifest(
                "memory",
                requires_capabilities={"agent_memory": True},
            )
        ),
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path, explicit="standalone")

    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
    )

    descriptor = registry.get("memory")

    assert descriptor is not None
    assert descriptor.runtime_compatible is False
    assert descriptor.missing_capabilities == ("agent_memory",)
    assert "agent_memory" in descriptor.resolve_reasons
    assert descriptor.routable is False


def test_ai_cli_self_recursion_guard_blocks_target_runtime(tmp_path):
    manifest = _manifest(
        "codex_search",
        kind="ai_cli",
        requires_capabilities={"ai_cli_search": True},
        ai_cli={"target_runtime": "codex"},
    )
    runtime = _runtime(tmp_path, explicit="codex")

    result = check_runtime_capabilities(
        manifest["requires_capabilities"],
        runtime.capabilities,
        manifest=parse_engine_manifest(manifest)[0],
        runtime=runtime,
    )

    assert result.compatible is False
    assert result.reasons == ("self_recursive_ai_cli",)


def test_project_adapter_is_discovered_but_not_loadable_by_default(tmp_path):
    plugin_dir = tmp_path / "plugins" / "engines" / "tavily"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(json.dumps(_manifest("tavily")), encoding="utf-8")
    runtime = _runtime(tmp_path)

    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
    )

    report = registry.report()
    tavily = report.resolved[0]

    assert report.discovered[0].trust_level == "project"
    assert tavily.resolved is False
    assert tavily.adapter_load_allowed is False
    assert tavily.adapter_imported is False
    assert "untrusted_project_plugin" in tavily.resolve_reasons
    assert tavily.routable is False


def test_trust_project_allows_project_adapter_without_importing_it(tmp_path):
    plugin_dir = tmp_path / "plugins" / "engines" / "tavily"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(json.dumps(_manifest("tavily")), encoding="utf-8")
    runtime = _runtime(tmp_path)

    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
    )

    tavily = registry.get("tavily")

    assert tavily is not None
    assert tavily.resolved is True
    assert tavily.adapter_load_allowed is True
    assert tavily.adapter_imported is False
    assert tavily.routable is True


def test_binary_present_light_health_uses_injected_resolver(tmp_path):
    runtime = _runtime(tmp_path)
    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        include_builtin=True,
        executable_resolver=lambda name: "/usr/local/bin/qmd" if name == "qmd" else None,
    )

    qmd = registry.get("qmd")

    assert qmd is not None
    assert qmd.configured is True
    assert qmd.health.status == "healthy"
    assert qmd.routable is True
    assert qmd.health.checks[0].details["path"] == "/usr/local/bin/qmd"


def test_path_and_repo_revision_checks_are_light_and_reporting_only(tmp_path):
    repo_path = tmp_path / "repo"
    present_path = tmp_path / "data"
    present_path.mkdir()
    plugin_dir = tmp_path / "plugins" / "engines" / "local"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(
        json.dumps(
            _manifest(
                "local",
                requirements={
                    "env": [],
                    "binaries": [],
                    "repos": [{"name": "sample", "default_path": str(repo_path), "required": True}],
                },
                health={
                    "checks": [
                        {"type": "path_present", "path": str(present_path), "required": True},
                        {"type": "repo_revision", "repo": "sample", "required": True},
                    ]
                },
            )
        ),
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path)

    registry = EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        plugin_paths=[tmp_path / "plugins" / "engines"],
        include_builtin=False,
        trust_project=True,
    )

    descriptor = registry.get("local")

    assert descriptor is not None
    assert descriptor.health.status == "degraded"
    assert [check.type for check in descriptor.health.checks] == ["path_present", "repo_revision"]
    assert descriptor.health.checks[0].status == "healthy"
    assert descriptor.health.checks[1].message == "repo_path_missing"
    assert descriptor.routable is True
