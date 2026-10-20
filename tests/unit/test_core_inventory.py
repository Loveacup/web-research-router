"""Slice 2: additive source manifests and non-live local inventory."""
import json
import sys
from dataclasses import replace

import pytest

from wrr.core import inventory
from wrr.core.contracts import (
    Health, Kind, PrivacyClass, Provision, SourceDescriptor, Transport,
)
from wrr.engines.loader import builtin_manifest_paths, load_engine_manifest, parse_engine_manifest
from wrr.engines.registry import EngineHealth, EngineRegistry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env
from wrr.runtime.state import set_cached_health


def _source(**overrides):
    source = {
        "transport": "api", "kind": "web", "upstream_family": "example",
        "privacy_class": "public", "primary": False, "fresh": True,
        "profile": {"covers": ["public web"], "best_for": [], "weak_for": [],
                    "freshness": "indexed", "languages": ["en"], "auth": "api_key"},
    }
    source.update(overrides)
    return source


def _manifest(source=True, **overrides):
    payload = {
        "schema_version": 1, "id": "example", "name": "Example", "kind": "web_api",
        "adapter": "wrr.engines.exa:ExaEngine",
        "capabilities": {"actions": ["search", "extract", "similar"], "domains": ["web"]},
        "routing": {"modes": []},
        "requirements": {"env": [{"name": "EXAMPLE_API_KEY", "required": True}]},
        "health": {"checks": [
            {"type": "env_present", "env": "EXAMPLE_API_KEY", "required": True},
            {"type": "live_probe", "command": ["must-not-execute"], "required": True},
        ]},
    }
    if source is not False:
        payload["source"] = _source() if source is True else source
    payload.update(overrides)
    return payload


def test_source_block_is_additive_and_round_trips():
    manifest, errors = parse_engine_manifest(_manifest())
    assert not errors
    assert manifest.source == _source()
    assert manifest.to_dict()["source"] == _source()
    legacy, errors = parse_engine_manifest(_manifest(source=False))
    assert not errors and legacy.source is None


@pytest.mark.parametrize("field,value", [
    ("transport", "filesystem"), ("transport", []), ("kind", "web_api"),
    ("privacy_class", "private"), ("upstream_family", ""), ("upstream_family", None),
    ("primary", "true"), ("fresh", 1), ("profile", []),
    ("profile", {"freshness": "hourly"}), ("profile", {"freshness": []}),
    ("profile", {"auth": "oauth"}), ("profile", {"covers": "web"}),
    ("profile", {"best_for": [1]}), ("profile", {"weak_for": [""]}),
    ("profile", {"languages": {"en": True}}),
])
def test_source_rejects_invalid_enums_and_structures(field, value):
    manifest, errors = parse_engine_manifest(_manifest(source=_source(**{field: value})))
    assert manifest is None
    assert any(error.startswith(f"invalid:source.{field}") for error in errors)


@pytest.mark.parametrize("field", ["transport", "kind", "privacy_class", "upstream_family"])
def test_source_requires_identity_fields(field):
    source = _source()
    del source[field]
    manifest, errors = parse_engine_manifest(_manifest(source=source))
    assert manifest is None and f"invalid:source.{field}" in errors


@pytest.mark.parametrize("value", [[], "api", 1])
def test_source_must_be_an_object(value):
    manifest, errors = parse_engine_manifest(_manifest(source=value))
    assert manifest is None and "invalid:source" in errors


def test_all_builtin_manifests_have_valid_source_profiles():
    paths = builtin_manifest_paths()
    assert len(paths) == 13
    manifests = {}
    for path in paths:
        discovery = load_engine_manifest(path, source="builtin", trust_level="builtin")
        assert discovery.valid, discovery.errors
        manifest = discovery.manifest
        assert manifest.source is not None
        assert manifest.source["profile"]["covers"]
        manifests[manifest.id] = manifest
    assert {key for key, manifest in manifests.items() if manifest.source["primary"]} == {"exa", "deepseek"}
    assert manifests["academic"].source["upstream_family"] == "openalex"
    assert manifests["community"].source["upstream_family"] == "opencli"
    assert manifests["github"].source["upstream_family"] == "github"
    for source_id in ("qmd", "local_obsidian", "local_session", "local_supermemory"):
        assert manifests[source_id].source["privacy_class"] == "local"
    assert manifests["qmd"].source["transport"] == "cli"
    for source_id in ("local_obsidian", "local_session", "local_supermemory"):
        assert manifests[source_id].source["transport"] == "api"


@pytest.fixture
def local_inventory(tmp_path, monkeypatch, request):
    root = tmp_path / ("user" if getattr(request, "param", "project") == "user" else "plugins") / "engines"
    root.mkdir(parents=True)
    state_file = tmp_path / "state.json"
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    monkeypatch.setattr(inventory, "inventory_plugin_paths", lambda runtime: (root,))
    def registry(**kwargs):
        return EngineRegistry(**kwargs, include_builtin=False, state_file=state_file,
                              executable_resolver=lambda name: None)
    monkeypatch.setattr(inventory, "EngineRegistry", registry)
    def live_probe(*args, **kwargs):
        pytest.fail("inventory must never execute a live probe")
    monkeypatch.setattr("wrr.engines.registry._live_probe_check", live_probe)
    def write_manifest(payload=None):
        payload = _manifest() if payload is None else payload
        folder = root / payload["id"]
        folder.mkdir(exist_ok=True)
        (folder / "engine.yaml").write_text(json.dumps(payload))
    def build(values=None, **kwargs):
        env = load_env(runtime, overrides=values or {}, env_files=[])
        return inventory.build_inventory(runtime=runtime, env=env, **kwargs)
    return write_manifest, build, state_file


def test_missing_key_is_not_authorized_or_routable(local_inventory):
    write, build, _ = local_inventory
    write()
    source = build(trust_project=True)[0]
    assert source.provision == Provision.INSTALLED
    assert source.health == Health.UNHEALTHY
    assert not source.routable and source.light_probe_ok is False
    assert source.failure_category == "auth_missing"


def test_satisfied_dependencies_are_authorized_without_live_probe(local_inventory):
    write, build, _ = local_inventory
    write()
    source = build({"EXAMPLE_API_KEY": "secret"}, trust_project=True)[0]
    assert source.provision == Provision.AUTHORIZED and source.routable
    assert source.light_probe_ok is True
    assert source.capabilities.search and source.capabilities.fetch and source.capabilities.similar
    assert source.capabilities.fresh
    assert source.to_dict()["source_block"] == "present"


def test_project_source_without_trust_is_disabled(local_inventory):
    write, build, _ = local_inventory
    write()
    source = build({"EXAMPLE_API_KEY": "secret"})[0]
    assert source.provision == Provision.DISABLED
    assert not source.routable


def test_missing_binary_is_configured_but_not_authorized(local_inventory):
    write, build, _ = local_inventory
    write(_manifest(requirements={"env": [], "binaries": [{"name": "absent", "required": True}]}))
    source = build({"EXAMPLE_API_KEY": "secret"}, trust_project=True)[0]
    assert source.provision == Provision.CONFIGURED
    assert not source.routable
    assert source.failure_category == "dependency_missing"


@pytest.mark.parametrize("status,routable", [
    ("cooldown", False), ("unhealthy", False), ("degraded", True), ("unknown", True),
])
def test_cached_health_maps_without_live_probe(local_inventory, status, routable):
    write, build, state_file = local_inventory
    write()
    health = EngineHealth("example", status, failure_category="rate_limit")
    set_cached_health("example", "live", health.to_dict(), 300, path=state_file)
    source = build({"EXAMPLE_API_KEY": "secret"}, trust_project=True)[0]
    assert source.health == Health(status)
    assert source.routable is routable
    assert source.light_probe_ok is True
    assert source.failure_category == "rate_limit"


def test_cached_healthy_cannot_authorize_missing_key(local_inventory):
    write, build, state_file = local_inventory
    write()
    set_cached_health("example", "live", EngineHealth("example", "healthy").to_dict(), 300, path=state_file)
    source = build(trust_project=True)[0]
    assert source.provision != Provision.AUTHORIZED and not source.routable
    assert source.failure_category == "auth_missing"


@pytest.mark.parametrize("local_inventory", ["project", "user"], indirect=True)
@pytest.mark.parametrize("source_kind,source_id", [
    ("local_cli", "local_example"), ("web_api", "example"), ("mcp", "mcp_example"),
])
def test_legacy_source_gets_explicit_defaults(local_inventory, source_kind, source_id):
    write, build, _ = local_inventory
    write(_manifest(source=False, kind=source_kind, id=source_id))
    source = build({"EXAMPLE_API_KEY": "secret"}, trust_project=True)[0]
    assert source.privacy_class == PrivacyClass.AUTHENTICATED
    assert source.upstream_family == source_id
    assert source.to_dict()["source_block"] == "missing"
    assert source.executable is True
    assert source.to_dict()["executable"] is True


def test_host_capabilities_are_merged_with_host_transport(local_inventory):
    _, build, _ = local_inventory
    host = SourceDescriptor(id="host:web", kind=Kind.HOST, transport=Transport.API,
                            upstream_family="host-web:opaque", privacy_class=PrivacyClass.PUBLIC,
                            provision=Provision.AUTHORIZED, health=Health.HEALTHY)
    assert build(host_capabilities=[host]) == [replace(host, transport=Transport.HOST)]
    assert host.transport == Transport.API


def test_invalid_manifests_are_not_inventory_sources(local_inventory):
    write, build, _ = local_inventory
    write(_manifest(source={"transport": "invalid"}))
    assert build(trust_project=True) == []


def test_configured_sources_and_mcp_are_discovered(tmp_path, local_inventory):
    _, build, _ = local_inventory
    config_path = tmp_path / "sources.json"
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "sources": {"forum": {"kind": "opencli", "command": [sys.executable]},
                    "papers": {"kind": "mcp", "command": [sys.executable], "tool": "search"}},
        "authorized_sources": ["forum"],
    }))
    sources = {source.id: source for source in build({"WRR_COMMUNITY_CONFIG": str(config_path)})}
    assert sources["forum"].transport == Transport.CLI
    assert sources["forum"].upstream_family == "opencli"
    assert sources["forum"].privacy_class == PrivacyClass.AUTHENTICATED
    assert sources["papers"].transport == Transport.MCP
    assert sources["papers"].privacy_class == PrivacyClass.AUTHENTICATED
    assert sources["papers"].provision == Provision.DISABLED
    assert not sources["papers"].routable


def test_inventory_cli_json_and_table(monkeypatch, capsys):
    from wrr._cli import main
    host = SourceDescriptor(id="host:web", kind=Kind.HOST, transport=Transport.HOST,
                            upstream_family="host-web:opaque", privacy_class=PrivacyClass.PUBLIC,
                            provision=Provision.AUTHORIZED, health=Health.HEALTHY)
    monkeypatch.setattr(inventory, "build_inventory", lambda **kwargs: [host])
    assert main(["inventory", "--json", "--runtime", "standalone"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sources"] == [host.to_dict()]
    assert payload["routable"] == ["host:web"]
    assert payload["summary"]["provision"]["authorized"] == 1
    assert payload["summary"]["health"]["healthy"] == 1
    assert main(["inventory", "--runtime", "standalone"]) == 0
    assert "host:web" in capsys.readouterr().out


def test_declarative_manifest_validation_uses_shared_parser():
    manifest, errors = parse_engine_manifest(_manifest(declarative={"mode": "invalid"}))
    assert manifest is None
    assert any(error.startswith("invalid:declarative:") for error in errors)


@pytest.mark.parametrize("mode", ["cli", "http"])
def test_declarative_without_adapter_uses_registry_prerequisites(local_inventory, mode):
    write, build, _ = local_inventory
    block = {
        "mode": mode, "results": {"root": "$", "title": "$.title", "url": "$.url"},
        mode: ({"command": ["missing-cli", "{query}"]} if mode == "cli" else
               {"url": "https://example.com/search", "headers_env": {"X-Key": "SERVICE_KEY"}}),
    }
    write(_manifest(adapter=None, requirements={"env": []}, health={"checks": []},
                    declarative=block))
    source = build(trust_project=True)[0]
    assert source.provision != Provision.DISABLED
    assert source.provision != Provision.AUTHORIZED
    assert not source.routable
    if mode == "http":
        authorized = build({"SERVICE_KEY": "secret"}, trust_project=True)[0]
        assert authorized.provision == Provision.AUTHORIZED and authorized.routable
    assert build()[0].provision == Provision.DISABLED
