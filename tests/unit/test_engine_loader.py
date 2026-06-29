"""P0-T3 engine manifest loader tests."""

import json

from wrr.engines.loader import (
    EnginePluginManifest,
    discover_engine_plugins,
    load_engine_manifest,
    parse_engine_manifest,
)


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


def test_builtin_manifests_are_discovered_and_valid():
    discoveries = discover_engine_plugins()
    by_id = {item.manifest.id: item for item in discoveries if item.manifest}

    assert {"exa", "brave", "searxng", "github", "qmd"} <= set(by_id)
    assert all(item.valid for item in by_id.values())
    assert by_id["exa"].trust_level == "builtin"
    assert by_id["exa"].manifest.capabilities["actions"] == ["search", "extract", "similar"]
    assert by_id["qmd"].manifest.requires_capabilities == {
        "local_kb": True,
        "can_spawn_cli": True,
    }


def test_parse_manifest_validates_required_schema_fields():
    manifest, errors = parse_engine_manifest({"schema_version": 1, "id": "broken"})

    assert manifest is None
    assert "missing:name" in errors
    assert "missing:kind" in errors
    assert "missing:capabilities" in errors
    assert "missing:routing" in errors
    assert "missing:requirements" in errors
    assert "missing:health" in errors


def test_invalid_manifest_is_returned_as_invalid_discovery(tmp_path):
    path = tmp_path / "engine.yaml"
    path.write_text('{"schema_version": 2, "id": "", "adapter": "https://example.invalid/a.py"}')

    discovery = load_engine_manifest(path)

    assert discovery.valid is False
    assert discovery.manifest is None
    assert "unsupported_schema_version:2" in discovery.errors
    assert "invalid:id" in discovery.errors
    assert "invalid:adapter" in discovery.errors


def test_synthetic_project_plugin_is_discovered_without_source_edit(tmp_path):
    plugin_dir = tmp_path / "plugins" / "engines" / "tavily"
    plugin_dir.mkdir(parents=True)
    manifest_path = plugin_dir / "engine.yaml"
    manifest_path.write_text(json.dumps(_manifest("tavily")), encoding="utf-8")

    discoveries = discover_engine_plugins([tmp_path / "plugins" / "engines"], include_builtin=False)

    assert len(discoveries) == 1
    assert discoveries[0].valid is True
    assert discoveries[0].source == "project"
    assert discoveries[0].trust_level == "project"
    assert discoveries[0].manifest.id == "tavily"
    assert isinstance(discoveries[0].manifest, EnginePluginManifest)


def test_duplicate_ids_report_override_chain(tmp_path):
    plugin_dir = tmp_path / "plugins" / "engines" / "exa"
    plugin_dir.mkdir(parents=True)
    plugin_manifest = plugin_dir / "engine.yaml"
    plugin_manifest.write_text(json.dumps(_manifest("exa", name="Project Exa")), encoding="utf-8")

    discoveries = discover_engine_plugins([tmp_path / "plugins" / "engines"])
    exa_discoveries = [item for item in discoveries if item.engine_id == "exa"]

    assert len(exa_discoveries) == 2
    builtin, project = exa_discoveries
    assert builtin.source == "builtin"
    assert builtin.overridden_by == plugin_manifest
    assert project.source == "project"
    assert project.duplicate_of == builtin.path


def test_yaml_subset_supports_block_lists_and_bool_capabilities(tmp_path):
    path = tmp_path / "engine.yaml"
    path.write_text(
        """
schema_version: 1
id: lexical
name: Lexical Search
kind: local_cli
capabilities:
  actions:
    - search
routing:
  modes:
    - auto
    - local
requirements: {}
health:
  checks: []
requires_capabilities:
  local_kb: true
  can_spawn_cli: true
""".strip(),
        encoding="utf-8",
    )

    discovery = load_engine_manifest(path)

    assert discovery.valid is True
    assert discovery.manifest.id == "lexical"
    assert discovery.manifest.capabilities["actions"] == ["search"]
    assert discovery.manifest.routing["modes"] == ["auto", "local"]
    assert discovery.manifest.requires_capabilities == {
        "local_kb": True,
        "can_spawn_cli": True,
    }
