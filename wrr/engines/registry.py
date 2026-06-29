"""v6 engine registry resolve, light health, and routability.

This module is additive and intentionally separate from legacy ``wrr.registry``.
It consumes v6 manifests from ``wrr.engines.loader`` and injected runtime/env
snapshots without importing engine adapters during discovery or resolve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Iterable, Mapping

from wrr.engines.loader import (
    EngineDiscovery,
    EnginePluginManifest,
    discover_engine_plugins,
)
from wrr.runtime.detect import RuntimeInfo
from wrr.runtime.env import EnvSnapshot


HealthStatus = str
Resolver = Callable[[str], str | None]

TRUSTED_ADAPTER_LEVELS = {"builtin", "user", "entry_point"}


@dataclass(frozen=True)
class RuntimeCapabilityResult:
    compatible: bool
    missing: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "missing": list(self.missing),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class HealthCheckResult:
    type: str
    status: HealthStatus
    required: bool
    message: str
    target: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"healthy", "degraded"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "status": self.status,
            "required": self.required,
            "message": self.message,
            "target": self.target,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class EngineHealth:
    engine_id: str
    status: HealthStatus
    checks: tuple[HealthCheckResult, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "status": self.status,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class EngineDescriptor:
    id: str
    name: str
    kind: str
    manifest: EnginePluginManifest
    discovery: EngineDiscovery
    runtime_compatible: bool
    missing_capabilities: tuple[str, ...]
    resolved: bool
    configured: bool
    adapter_load_allowed: bool
    adapter_imported: bool = False
    resolve_reasons: tuple[str, ...] = ()
    health: EngineHealth | None = None
    routable: bool = False
    routable_reasons: tuple[str, ...] = ()

    def with_health(self, health: EngineHealth) -> "EngineDescriptor":
        routable, reasons = _routability(self, health)
        return EngineDescriptor(
            id=self.id,
            name=self.name,
            kind=self.kind,
            manifest=self.manifest,
            discovery=self.discovery,
            runtime_compatible=self.runtime_compatible,
            missing_capabilities=self.missing_capabilities,
            resolved=self.resolved,
            configured=self.configured,
            adapter_load_allowed=self.adapter_load_allowed,
            adapter_imported=self.adapter_imported,
            resolve_reasons=self.resolve_reasons,
            health=health,
            routable=routable,
            routable_reasons=reasons,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "runtime_compatible": self.runtime_compatible,
            "missing_capabilities": list(self.missing_capabilities),
            "resolved": self.resolved,
            "configured": self.configured,
            "adapter_load_allowed": self.adapter_load_allowed,
            "adapter_imported": self.adapter_imported,
            "resolve_reasons": list(self.resolve_reasons),
            "routable": self.routable,
            "routable_reasons": list(self.routable_reasons),
            "health": self.health.to_dict() if self.health else None,
            "manifest": self.manifest.to_dict(),
            "discovery": self.discovery.to_dict(),
        }


@dataclass(frozen=True)
class RegistryReport:
    discovered: tuple[EngineDiscovery, ...]
    resolved: tuple[EngineDescriptor, ...]
    health: tuple[EngineHealth, ...]

    @property
    def routable(self) -> tuple[EngineDescriptor, ...]:
        return tuple(descriptor for descriptor in self.resolved if descriptor.routable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovered": [discovery.to_dict() for discovery in self.discovered],
            "resolved": [descriptor.to_dict() for descriptor in self.resolved],
            "health": [health.to_dict() for health in self.health],
            "routable": [descriptor.id for descriptor in self.routable],
        }


def check_runtime_capabilities(
    requires: Mapping[str, bool],
    capabilities: object,
    *,
    manifest: EnginePluginManifest | None = None,
    runtime: RuntimeInfo | None = None,
) -> RuntimeCapabilityResult:
    """Return missing runtime capabilities required by a manifest."""

    missing = tuple(
        sorted(
            name
            for name, required in requires.items()
            if required and not bool(getattr(capabilities, name, False))
        )
    )
    reasons = list(missing)

    if (
        manifest is not None
        and runtime is not None
        and manifest.kind == "ai_cli"
        and _ai_cli_target_runtime(manifest) == runtime.name
    ):
        reasons.append("self_recursive_ai_cli")

    return RuntimeCapabilityResult(
        compatible=not reasons,
        missing=missing,
        reasons=tuple(reasons),
    )


class EngineRegistry:
    """v6 registry with discovery, resolve, light health, and routability."""

    def __init__(
        self,
        *,
        runtime: RuntimeInfo,
        env: EnvSnapshot,
        plugin_paths: Iterable[str | Path] | None = None,
        discoveries: Iterable[EngineDiscovery] | None = None,
        include_builtin: bool = True,
        trust_project: bool = False,
        executable_resolver: Resolver | None = None,
    ) -> None:
        self.runtime = runtime
        self.env = env
        self.plugin_paths = tuple(plugin_paths or ())
        self.include_builtin = include_builtin
        self.trust_project = trust_project
        self._discoveries = tuple(discoveries) if discoveries is not None else None
        self._executable_resolver = executable_resolver or shutil.which

    def discover(self) -> tuple[EngineDiscovery, ...]:
        if self._discoveries is None:
            self._discoveries = tuple(
                discover_engine_plugins(self.plugin_paths, include_builtin=self.include_builtin)
            )
        return self._discoveries

    def resolve(self) -> tuple[EngineDescriptor, ...]:
        return tuple(
            self._resolve_discovery(discovery)
            for discovery in self.discover()
            if discovery.valid and discovery.manifest is not None
        )

    def health(self, descriptors: Iterable[EngineDescriptor] | None = None) -> tuple[EngineHealth, ...]:
        return tuple(self._health_descriptor(descriptor) for descriptor in (descriptors or self.resolve()))

    def report(self) -> RegistryReport:
        resolved = self.resolve()
        health_by_id = {item.engine_id: item for item in self.health(resolved)}
        resolved_with_health = tuple(
            descriptor.with_health(health_by_id[descriptor.id]) for descriptor in resolved
        )
        return RegistryReport(
            discovered=self.discover(),
            resolved=resolved_with_health,
            health=tuple(health_by_id[descriptor.id] for descriptor in resolved),
        )

    def routable(self) -> tuple[EngineDescriptor, ...]:
        return self.report().routable

    def get(self, engine_id: str) -> EngineDescriptor | None:
        for descriptor in self.report().resolved:
            if descriptor.id == engine_id:
                return descriptor
        return None

    def _resolve_discovery(self, discovery: EngineDiscovery) -> EngineDescriptor:
        manifest = discovery.manifest
        assert manifest is not None

        capability_result = check_runtime_capabilities(
            manifest.requires_capabilities,
            self.runtime.capabilities,
            manifest=manifest,
            runtime=self.runtime,
        )
        adapter_allowed, adapter_reason = _adapter_allowed(
            discovery,
            manifest,
            trust_project=self.trust_project,
        )
        reasons = list(capability_result.reasons)
        if adapter_reason:
            reasons.append(adapter_reason)

        configured, configure_reasons = _configured(manifest, self.env)
        reasons.extend(configure_reasons)

        resolved = capability_result.compatible and adapter_allowed
        return EngineDescriptor(
            id=manifest.id,
            name=manifest.name,
            kind=manifest.kind,
            manifest=manifest,
            discovery=discovery,
            runtime_compatible=capability_result.compatible,
            missing_capabilities=capability_result.missing,
            resolved=resolved,
            configured=configured,
            adapter_load_allowed=adapter_allowed,
            resolve_reasons=tuple(reasons),
        )

    def _health_descriptor(self, descriptor: EngineDescriptor) -> EngineHealth:
        checks = tuple(
            self._run_check(descriptor.manifest, check)
            for check in descriptor.manifest.health.get("checks", [])
            if isinstance(check, Mapping)
        )
        return EngineHealth(descriptor.id, _combine_health(checks), checks)

    def _run_check(
        self,
        manifest: EnginePluginManifest,
        check: Mapping[str, Any],
    ) -> HealthCheckResult:
        check_type = str(check.get("type", "unknown"))
        required = bool(check.get("required", True))
        if check_type == "env_present":
            return _env_present_check(check, self.env, required)
        if check_type == "binary_present":
            return _binary_present_check(check, self._executable_resolver, required)
        if check_type == "path_present":
            return _path_present_check(check, required)
        if check_type == "repo_revision":
            return _repo_revision_check(check, manifest, required)
        return HealthCheckResult(
            type=check_type,
            status="degraded" if not required else "unhealthy",
            required=required,
            message="unsupported_health_check",
        )


def _configured(manifest: EnginePluginManifest, env: EnvSnapshot) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    for requirement in _list_requirements(manifest, "env"):
        if not bool(requirement.get("required", True)):
            continue
        names = _env_names(requirement)
        if not any(name in env.values for name in names):
            reasons.append(f"missing_required_env:{names[0]}")
    return not reasons, reasons


def _env_present_check(
    check: Mapping[str, Any],
    env: EnvSnapshot,
    required: bool,
) -> HealthCheckResult:
    names = _env_names(check)
    present = next((name for name in names if name in env.values), None)
    if present:
        return HealthCheckResult(
            type="env_present",
            status="healthy",
            required=required,
            target=present,
            message="env_present",
        )
    return HealthCheckResult(
        type="env_present",
        status="unhealthy" if required else "degraded",
        required=required,
        target=names[0] if names else None,
        message="missing_required_env" if required else "missing_optional_env",
        details={"aliases": names[1:]},
    )


def _binary_present_check(
    check: Mapping[str, Any],
    resolver: Resolver,
    required: bool,
) -> HealthCheckResult:
    binary = str(check.get("binary") or check.get("name") or "")
    resolved = resolver(binary) if binary else None
    if resolved:
        return HealthCheckResult(
            type="binary_present",
            status="healthy",
            required=required,
            target=binary,
            message="binary_present",
            details={"path": resolved},
        )
    return HealthCheckResult(
        type="binary_present",
        status="unhealthy" if required else "degraded",
        required=required,
        target=binary or None,
        message="missing_required_binary" if required else "missing_optional_binary",
    )


def _path_present_check(check: Mapping[str, Any], required: bool) -> HealthCheckResult:
    path_value = check.get("path") or check.get("name")
    path = Path(str(path_value)).expanduser() if path_value else None
    exists = path.exists() if path else False
    if exists:
        return HealthCheckResult(
            type="path_present",
            status="healthy",
            required=required,
            target=str(path),
            message="path_present",
        )
    return HealthCheckResult(
        type="path_present",
        status="unhealthy" if required else "degraded",
        required=required,
        target=str(path) if path else None,
        message="missing_required_path" if required else "missing_optional_path",
    )


def _repo_revision_check(
    check: Mapping[str, Any],
    manifest: EnginePluginManifest,
    required: bool,
) -> HealthCheckResult:
    path = _repo_path(check, manifest)
    if path is None or not path.exists():
        return HealthCheckResult(
            type="repo_revision",
            status="degraded",
            required=required,
            target=str(path) if path else None,
            message="repo_path_missing",
        )

    revision = _git_revision(path)
    if revision is None:
        return HealthCheckResult(
            type="repo_revision",
            status="degraded",
            required=required,
            target=str(path),
            message="repo_revision_unavailable",
        )
    return HealthCheckResult(
        type="repo_revision",
        status="healthy",
        required=required,
        target=str(path),
        message="repo_revision",
        details={"revision": revision},
    )


def _repo_path(check: Mapping[str, Any], manifest: EnginePluginManifest) -> Path | None:
    if check.get("path"):
        return Path(str(check["path"])).expanduser()

    repo_name = check.get("repo")
    for requirement in _list_requirements(manifest, "repos"):
        if repo_name and requirement.get("name") != repo_name:
            continue
        raw_path = requirement.get("path") or requirement.get("default_path")
        if raw_path:
            return Path(str(raw_path)).expanduser()
    return None


def _git_revision(path: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = completed.stdout.strip()
    if completed.returncode != 0 or not revision:
        return None
    return revision


def _combine_health(checks: tuple[HealthCheckResult, ...]) -> HealthStatus:
    if any(check.status == "unhealthy" and check.required for check in checks):
        return "unhealthy"
    if any(check.status != "healthy" for check in checks):
        return "degraded"
    return "healthy"


def _routability(
    descriptor: EngineDescriptor,
    health: EngineHealth,
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if not descriptor.resolved:
        reasons.append("not_resolved")
    if not descriptor.configured:
        reasons.append("not_configured")
    if not descriptor.adapter_load_allowed:
        reasons.append("adapter_not_loadable")
    if health.status == "unhealthy":
        reasons.append("health_unhealthy")
    return not reasons, tuple(reasons)


def _adapter_allowed(
    discovery: EngineDiscovery,
    manifest: EnginePluginManifest,
    *,
    trust_project: bool,
) -> tuple[bool, str | None]:
    if not manifest.adapter:
        return False, "adapter_missing"
    if discovery.trust_level == "project" and not trust_project:
        return False, "untrusted_project_plugin"
    if discovery.trust_level in TRUSTED_ADAPTER_LEVELS or (
        discovery.trust_level == "project" and trust_project
    ):
        return True, None
    return False, f"untrusted_adapter:{discovery.trust_level}"


def _ai_cli_target_runtime(manifest: EnginePluginManifest) -> str | None:
    ai_cli = manifest.raw.get("ai_cli")
    if isinstance(ai_cli, Mapping):
        target = ai_cli.get("target_runtime")
        return str(target) if target else None
    return None


def _list_requirements(manifest: EnginePluginManifest, key: str) -> list[Mapping[str, Any]]:
    value = manifest.requirements.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _env_names(item: Mapping[str, Any]) -> list[str]:
    primary = item.get("env") or item.get("name")
    names = [str(primary)] if primary else []
    aliases = item.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(str(alias) for alias in aliases if alias)
    return names
