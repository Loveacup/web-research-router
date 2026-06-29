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
    RepoRequirement,
    discover_engine_plugins,
    verify_repo_pin,
)
from wrr.runtime.detect import RuntimeInfo
from wrr.runtime.env import EnvSnapshot
from wrr.runtime.state import (
    DEFAULT_HEALTH_TTL_SEC,
    circuit_status,
    get_cached_health,
    record_engine_failure,
    set_cached_health,
    state_path,
)


HealthStatus = str
HealthMode = str
Resolver = Callable[[str], str | None]
RevisionResolver = Callable[..., str | None]

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

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "HealthCheckResult":
        return cls(
            type=str(payload.get("type") or "unknown"),
            status=str(payload.get("status") or "unhealthy"),
            required=bool(payload.get("required", True)),
            message=str(payload.get("message") or ""),
            target=str(payload["target"]) if payload.get("target") is not None else None,
            details=dict(payload.get("details") or {}),
        )


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

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EngineHealth":
        raw_checks = payload.get("checks") or []
        return cls(
            engine_id=str(payload.get("engine_id") or ""),
            status=str(payload.get("status") or "unhealthy"),
            checks=tuple(
                HealthCheckResult.from_dict(check)
                for check in raw_checks
                if isinstance(check, Mapping)
            ),
        )


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
        revision_resolver: RevisionResolver | None = None,
        state_file: str | Path | None = None,
        health_ttl_sec: int = DEFAULT_HEALTH_TTL_SEC,
    ) -> None:
        self.runtime = runtime
        self.env = env
        self.plugin_paths = tuple(plugin_paths or ())
        self.include_builtin = include_builtin
        self.trust_project = trust_project
        self._discoveries = tuple(discoveries) if discoveries is not None else None
        self._executable_resolver = executable_resolver or shutil.which
        self._revision_resolver = revision_resolver
        self.state_file = Path(state_file).expanduser() if state_file is not None else state_path(runtime)
        self.health_ttl_sec = health_ttl_sec

    def discover(self) -> tuple[EngineDiscovery, ...]:
        if self._discoveries is None:
            self._discoveries = tuple(
                discover_engine_plugins(
                    self.plugin_paths,
                    include_builtin=self.include_builtin,
                    trust_project=self.trust_project,
                )
            )
        return self._discoveries

    def resolve(
        self,
        *,
        action: str | None = None,
        domain: str | None = None,
    ) -> tuple[EngineDescriptor, ...]:
        return tuple(
            self._resolve_discovery(discovery)
            for discovery in self.discover()
            if discovery.valid and discovery.manifest is not None
            and _capability_matches(discovery.manifest, action=action, domain=domain)
        )

    def health(
        self,
        descriptors: Iterable[EngineDescriptor] | None = None,
        *,
        mode: HealthMode = "light",
    ) -> tuple[EngineHealth, ...]:
        return tuple(
            self._health_descriptor(descriptor, mode=mode)
            for descriptor in (descriptors or self.resolve())
        )

    def report(self, *, health_mode: HealthMode = "light") -> RegistryReport:
        resolved = self.resolve()
        health_by_id = {item.engine_id: item for item in self.health(resolved, mode=health_mode)}
        resolved_with_health = tuple(
            descriptor.with_health(health_by_id[descriptor.id]) for descriptor in resolved
        )
        return RegistryReport(
            discovered=self.discover(),
            resolved=resolved_with_health,
            health=tuple(health_by_id[descriptor.id] for descriptor in resolved),
        )

    def routable(self) -> tuple[EngineDescriptor, ...]:
        return self.report(health_mode="auto").routable

    def get(
        self,
        engine_id: str,
        *,
        action: str | None = None,
        domain: str | None = None,
    ) -> EngineDescriptor | None:
        for descriptor in self.resolve(action=action, domain=domain):
            if descriptor.id == engine_id:
                return descriptor.with_health(self._health_descriptor(descriptor, mode="light"))
        return None

    def record_engine_failure(self, engine_id: str, reason: str):
        return record_engine_failure(engine_id, reason, path=self.state_file)

    def circuit_status(self, engine_id: str):
        return circuit_status(engine_id, path=self.state_file)

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

    def _health_descriptor(self, descriptor: EngineDescriptor, *, mode: HealthMode) -> EngineHealth:
        breaker = circuit_status(descriptor.id, path=self.state_file)
        if breaker.open:
            return EngineHealth(
                descriptor.id,
                "unhealthy",
                (
                    HealthCheckResult(
                        type="circuit_breaker",
                        status="unhealthy",
                        required=True,
                        message="circuit_open",
                        details=breaker.to_dict(),
                    ),
                ),
            )

        health_mode = _normalize_health_mode(mode)
        cached = None
        if health_mode in {"auto", "live"}:
            cached = get_cached_health(
                descriptor.id,
                "live",
                self.health_ttl_sec,
                path=self.state_file,
            )
        if cached is not None:
            return EngineHealth.from_dict(cached)
        if health_mode == "auto":
            health_mode = "light"

        checks = tuple(
            self._run_check(descriptor.manifest, check)
            for check in descriptor.manifest.health.get("checks", [])
            if isinstance(check, Mapping) and _check_applies(check, health_mode)
        )
        health = EngineHealth(descriptor.id, _combine_health(checks), checks)
        if health_mode == "live":
            set_cached_health(
                descriptor.id,
                "live",
                health.to_dict(),
                self.health_ttl_sec,
                path=self.state_file,
            )
        return health

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
            return _repo_revision_check(
                check,
                manifest,
                required,
                revision_resolver=self._revision_resolver,
            )
        if check_type == "live_probe":
            return _live_probe_check(check, required)
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


def _capability_matches(
    manifest: EnginePluginManifest,
    *,
    action: str | None,
    domain: str | None,
) -> bool:
    capabilities = manifest.capabilities
    actions = capabilities.get("actions", [])
    domains = capabilities.get("domains", [])
    if action is not None and action not in actions:
        return False
    if domain is not None and domain not in domains:
        return False
    return True


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
    *,
    revision_resolver: RevisionResolver | None = None,
) -> HealthCheckResult:
    requirement = _repo_requirement(check, manifest)
    path = _repo_path(check, manifest, requirement=requirement)
    expected_pin = str(check.get("pin")) if check.get("pin") else (
        requirement.pin if requirement is not None else None
    )
    check_required = required if requirement is None else requirement.required
    if path is None:
        return HealthCheckResult(
            type="repo_revision",
            status="degraded",
            required=check_required,
            target=None,
            message="repo_requirement_missing",
        )

    pin_status = verify_repo_pin(path, expected_pin, revision_resolver=revision_resolver)
    if pin_status.status in {"missing", "unavailable"}:
        return HealthCheckResult(
            type="repo_revision",
            status="degraded",
            required=check_required,
            target=str(path),
            message=pin_status.message,
            details=_repo_details(requirement, pin_status),
        )

    if pin_status.status == "mismatch":
        return HealthCheckResult(
            type="repo_revision",
            status="unhealthy" if check_required else "degraded",
            required=check_required,
            target=str(path),
            message="repo_pin_mismatch",
            details=_repo_details(requirement, pin_status),
        )

    return HealthCheckResult(
        type="repo_revision",
        status="healthy",
        required=check_required,
        target=str(path),
        message="repo_revision",
        details=_repo_details(requirement, pin_status),
    )


def _repo_requirement(
    check: Mapping[str, Any],
    manifest: EnginePluginManifest,
) -> RepoRequirement | None:
    repo_name = check.get("repo")
    for requirement in manifest.repo_requirements:
        if repo_name and requirement.name != repo_name:
            continue
        return requirement
    return None


def _repo_path(
    check: Mapping[str, Any],
    manifest: EnginePluginManifest,
    *,
    requirement: RepoRequirement | None = None,
) -> Path | None:
    if check.get("path"):
        return Path(str(check["path"])).expanduser()

    if requirement is None:
        requirement = _repo_requirement(check, manifest)
    if requirement is None:
        return None
    raw_path = requirement.path or requirement.default_path
    if raw_path:
        return Path(raw_path).expanduser()
    return None


def _repo_details(
    requirement: RepoRequirement | None,
    pin_status: Any,
) -> dict[str, Any]:
    details = pin_status.to_dict()
    details["revision"] = pin_status.current_revision
    details["commit"] = pin_status.current_revision
    if requirement is not None:
        details["repo"] = requirement.name
        details["remote"] = requirement.remote
        details["pin"] = requirement.pin
        details["default_path"] = requirement.default_path
    return details


def _live_probe_check(check: Mapping[str, Any], required: bool) -> HealthCheckResult:
    command = check.get("command")
    if not isinstance(command, list) or not command:
        return HealthCheckResult(
            type="live_probe",
            status="unhealthy" if required else "degraded",
            required=required,
            message="invalid_live_probe",
        )
    try:
        completed = subprocess.run(
            [str(part) for part in command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(check.get("timeout_sec") or 2),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HealthCheckResult(
            type="live_probe",
            status="unhealthy" if required else "degraded",
            required=required,
            message="live_probe_failed",
            details={"error": type(exc).__name__},
        )
    if completed.returncode == 0:
        return HealthCheckResult(
            type="live_probe",
            status="healthy",
            required=required,
            message="live_probe_ok",
        )
    return HealthCheckResult(
        type="live_probe",
        status="unhealthy" if required else "degraded",
        required=required,
        message="live_probe_failed",
        details={"returncode": completed.returncode},
    )


def _combine_health(checks: tuple[HealthCheckResult, ...]) -> HealthStatus:
    if any(check.status == "unhealthy" and check.required for check in checks):
        return "unhealthy"
    if any(check.status != "healthy" for check in checks):
        return "degraded"
    return "healthy"


def _normalize_health_mode(mode: HealthMode) -> str:
    if mode in {"light", "live", "auto"}:
        return mode
    return "light"


def _check_applies(check: Mapping[str, Any], mode: str) -> bool:
    level = str(check.get("level") or "light")
    if mode == "live":
        return level in {"light", "live"}
    return level == "light"


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
