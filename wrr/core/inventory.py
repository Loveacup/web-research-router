"""Local source inventory built on registry discovery, light probes and TTL health.

No adapter imports, live probes, browser operations or recovery run here. Cached
live health can veto routing, but cannot authorize missing local dependencies.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from wrr.engines.configured_sources import load_source_config
from wrr.engines.loader import EngineDiscovery, EnginePluginManifest, parse_engine_manifest
from wrr.engines.registry import EngineDescriptor, EngineHealth, EngineRegistry
from wrr.runtime.detect import RuntimeInfo
from wrr.runtime.env import EnvSnapshot

from .contracts import (
    Capabilities, Health, Kind, PrivacyClass, Provision, SourceDescriptor,
    SourceProfile, Transport,
)


def inventory_plugin_paths(runtime: RuntimeInfo) -> tuple[Path, ...]:
    """User engines first, project overrides last, matching registry precedence."""
    paths = [Path.home() / ".config" / "wrr" / "engines"]
    paths.extend(root / "engines" for root in runtime.config_roots if root != runtime.cwd)
    paths.append(runtime.cwd / "plugins" / "engines")
    return tuple(dict.fromkeys(paths))


def invalid_manifests(*, runtime: RuntimeInfo, trust_project: bool = False) -> list[dict]:
    """Manifests that were found but rejected or blocked; never silent for users."""
    from wrr.engines.loader import discover_engine_plugins
    out = []
    for item in discover_engine_plugins(inventory_plugin_paths(runtime), include_builtin=False,
                                        trust_project=trust_project):
        if not item.valid or item.blocked_reasons:
            out.append({"path": str(item.path), "id": item.engine_id, "trust": item.trust_level,
                        "errors": list(item.errors), "blocked": list(item.blocked_reasons)})
    return out


def build_inventory(
    *, runtime: RuntimeInfo, env: EnvSnapshot, trust_project: bool = False,
    host_capabilities: list[SourceDescriptor] | None = None,
) -> list[SourceDescriptor]:
    """Discover authorized sources without doing live work on the hot path."""
    registry = EngineRegistry(
        runtime=runtime, env=env, plugin_paths=inventory_plugin_paths(runtime),
        trust_project=trust_project,
    )
    discoveries = registry.discover()
    configured, _ = load_source_config(
        reserved={item.engine_id for item in discoveries if item.engine_id},
        env={key: value.value for key, value in env.values.items()},
    )
    if configured:
        registry = EngineRegistry(
            runtime=runtime, env=env, trust_project=trust_project,
            discoveries=(*discoveries, *(
                _configured_discovery(source_id, config)
                for source_id, config in configured.items()
            )),
        )

    sources = []
    for descriptor in registry.report(health_mode="auto").resolved:
        if descriptor.discovery.overridden_by is not None:
            continue
        # auto consumes fresh live cache but never executes a live check. Verify
        # local prerequisites separately when the returned health is cached.
        health = descriptor.health
        assert health is not None
        light = (
            registry.health((descriptor,), mode="light")[0]
            if health.health_source == "cache" else health
        )
        sources.append(_source_descriptor(descriptor, health, light, trust_project))
    sources.extend(
        replace(source, transport=Transport.HOST)
        for source in host_capabilities or ()
    )
    return sources


def _source_descriptor(
    descriptor: EngineDescriptor, health: EngineHealth, light: EngineHealth,
    trust_project: bool,
) -> SourceDescriptor:
    manifest = descriptor.manifest
    source = manifest.source or _legacy_source(manifest)
    light_ok = all(check.ok for check in light.checks if check.required)
    provision = (
        Provision.CONFIGURED if descriptor.configured else Provision.INSTALLED
    )
    blocked = (
        descriptor.discovery.blocked_reasons
        or not descriptor.adapter_load_allowed
        or (descriptor.discovery.trust_level == "project" and not trust_project)
        or manifest.raw.get("enabled") is False
        or manifest.raw.get("_inventory_authorized") is False
        or health.status == "disabled"
    )
    if blocked:
        provision = Provision.DISABLED
    elif descriptor.resolved and descriptor.configured and light_ok:
        provision = Provision.AUTHORIZED
    # Registry also has a legacy disabled health value; it belongs on provision,
    # not on the core health axis. Every other health status maps directly.
    core_health = Health.UNKNOWN if health.status == "disabled" else Health(health.status)
    actions = manifest.capabilities.get("actions", [])
    return SourceDescriptor(
        id=descriptor.id, kind=Kind(source["kind"]),
        transport=Transport(source["transport"]),
        upstream_family=source["upstream_family"],
        privacy_class=PrivacyClass(source["privacy_class"]),
        profile=SourceProfile.from_dict(source.get("profile", {})),
        capabilities=Capabilities(
            search="search" in actions, fetch="extract" in actions or "fetch" in actions,
            similar="similar" in actions,
            returns_answer=bool(manifest.capabilities.get("returns_answer", False)),
            returns_citations=bool(manifest.capabilities.get("returns_citations", False)),
            fresh=source.get("fresh", False),
        ),
        provision=provision, health=core_health, primary=source.get("primary", False),
        failure_category=(light.failure_category if not light_ok else health.failure_category),
        light_probe_ok=light_ok,
        source_block="present" if manifest.source is not None else "missing",
        query_style=source.get("query_style", "natural"),
        adapter=manifest.adapter, declarative=manifest.raw.get("declarative"),
        configured_source=manifest.raw.get("_configured_source"),
    )


def _legacy_source(manifest: EnginePluginManifest) -> dict:
    domains = manifest.capabilities.get("domains", [])
    if manifest.kind.startswith("local_") or manifest.id.startswith("local_") or "local" in domains:
        kind = "local"
    elif manifest.id == "community" or "community" in domains:
        kind = "community"
    elif "academic" in domains:
        kind = "academic"
    elif "code" in domains:
        kind = "code"
    else:
        kind = "web"
    adapter_kind = manifest.kind.lower()
    transport = next((value for token, value in (
        ("mcp", "mcp"), ("cli", "cli"), ("skill", "skill"),
        ("hermes", "host"), ("host", "host"), ("filesystem", "host"),
        ("remote", "remote"),
    ) if token in adapter_kind), "api")
    return {
        "kind": kind, "transport": transport, "upstream_family": manifest.id,
        # Missing metadata never authorizes public-state disclosure.
        "privacy_class": "authenticated",
    }


def _configured_discovery(source_id: str, config: dict) -> EngineDiscovery:
    """Adapt validated host configuration to registry-owned prerequisite probes."""
    mcp = config["kind"] == "mcp"
    manifest, errors = parse_engine_manifest({
        "schema_version": 1, "id": source_id, "name": source_id,
        "kind": "mcp" if mcp else "cli",
        "adapter": "wrr.engines.community:CommunityEngine",
        "capabilities": {"actions": ["search"], "domains": ["community"]},
        "routing": {"modes": []},
        "requirements": {
            "env": [], "binaries": [{"name": config["command"][0], "required": True}],
            "python": [{"name": "mcp", "required": True}] if mcp else [],
        },
        "health": {"checks": []},
        "source": {
            "kind": "community", "transport": "mcp" if mcp else "cli",
            "upstream_family": source_id if mcp else "opencli",
            "privacy_class": "authenticated",
            "profile": {"covers": [source_id], "auth": "login"},
        },
        "_inventory_authorized": config["authorized"],
        "_configured_source": config,
    })
    if errors:
        raise ValueError(f"invalid configured source manifest: {errors}")
    return EngineDiscovery(
        path=Path(f"configured:{source_id}"), source="configured", trust_level="user",
        valid=True, manifest=manifest,
    )
