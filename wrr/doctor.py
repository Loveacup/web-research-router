"""WRR doctor 运行器 + 诊断汇总。"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .registry import EngineRegistry
from .schemas import EngineCheckResult


async def run_doctor(
    registry: EngineRegistry,
    *,
    engine: Optional[str] = None,
    tier: Optional[int] = None,
    deep: bool = False,
) -> List[EngineCheckResult]:
    """
    运行 doctor 检查。

    Args:
        registry: 引擎注册表
        engine: 指定单个引擎名称，None 表示检查所有
        tier: 过滤特定 tier，None 表示不过滤
        deep: 是否执行深度检查（传递给 engine/deps/v6 live health，具体探测深度由各实现决定）

    Returns:
        检查结果列表

    Raises:
        ValueError: 指定的 engine 不存在时抛出
    """
    targets = registry.doctor_targets()

    # 过滤指定 engine
    if engine:
        target_engine = registry.get(engine)
        if not target_engine:
            raise ValueError(f"Unknown engine: {engine}")
        targets = [target_engine]

    # 过滤 tier
    if tier is not None:
        targets = [e for e in targets if e.tier == tier]

    # 并发执行检查，隔离异常
    async def _check_safe(eng):
        try:
            return await eng.health_check(deep=deep)
        except Exception as exc:
            return EngineCheckResult(
                engine=eng.name,
                status="fail",
                tier=getattr(eng, "tier", 1),
                summary="Doctor check crashed",
                evidence={"exception": type(exc).__name__, "message": str(exc)},
            )

    results = await asyncio.gather(*[_check_safe(e) for e in targets])
    return list(results)


def summarize_checks(results: List[EngineCheckResult]) -> Dict:
    """
    汇总检查结果。

    Returns:
        {
            "ok": int,
            "warn": int,
            "fail": int,
            "skip": int,
            "status": "ok" | "warn" | "fail"
        }
    """
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    # 聚合状态：有 fail 则 fail，有 warn 则 warn，否则 ok
    if counts["fail"] > 0:
        agg_status = "fail"
    elif counts["warn"] > 0:
        agg_status = "warn"
    else:
        agg_status = "ok"

    return {**counts, "status": agg_status}


def doctor_exit_code(results: List[EngineCheckResult], *, strict: bool = False) -> int:
    """
    计算 doctor 退出码。

    Args:
        results: 检查结果列表
        strict: True 时 warn 也视为失败

    Returns:
        0: 通过（无 fail，或 strict=False 且仅有 warn）
        1: 失败（有 fail，或 strict=True 且有 warn）
    """
    has_fail = any(r.status == "fail" for r in results)
    has_warn = any(r.status == "warn" for r in results)

    if has_fail:
        return 1
    if strict and has_warn:
        return 1
    return 0


# ── v5.5 外部依赖 doctor ──

async def run_deps_doctor(*, deep: bool = False) -> List[Dict]:
    """运行全量依赖健康检查。

    Returns:
        [{"id": str, "type": str, "status": "ok"|"degraded"|"missing",
          "source_url": str, "required": bool, "version": str, "detail": str}, ...]
    """
    from .deps import DepRegistry

    registry = DepRegistry.get()
    deps = registry.all

    async def _check_safe(dep_id: str, dep):
        try:
            import asyncio as _asyncio
            result = dep.health(deep=deep)
            if _asyncio.iscoroutine(result):
                result = await result
            if _asyncio.iscoroutine(result):  # double-check for nested coroutines
                result = await result
            return {
                "id": dep_id,
                "type": dep.dep_type.value,
                "status": result.status.value,
                "source_url": dep.source_url,
                "required": dep.required,
                "version": result.version,
                "detail": result.detail,
            }
        except Exception as exc:
            return {
                "id": dep_id,
                "type": getattr(dep, "dep_type", None),
                "status": "missing",
                "source_url": getattr(dep, "source_url", ""),
                "required": getattr(dep, "required", True),
                "version": "unknown",
                "detail": str(exc),
            }

    results = await asyncio.gather(
        *[_check_safe(dep_id, dep) for dep_id, dep in deps.items()]
    )
    return list(results)


def summarize_deps(results: List[Dict]) -> Dict:
    """汇总外部依赖检查结果。"""
    counts = {"ok": 0, "degraded": 0, "missing": 0}
    for r in results:
        status = r.get("status", "missing")
        counts[status] = counts.get(status, 0) + 1
    if counts["missing"] > 0:
        agg = "fail"
    elif counts["degraded"] > 0:
        agg = "warn"
    else:
        agg = "ok"
    return {**counts, "status": agg}


# ── v6 control-plane doctor ──

@dataclass(frozen=True)
class DoctorReport:
    runtime: Any
    env: Any
    discovered: tuple[Any, ...]
    resolved: tuple[Any, ...]
    health: tuple[Any, ...]
    summary: dict[str, Any]
    trust_project: bool
    findings: tuple[dict[str, Any], ...] = ()
    state_file: str | Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime.to_dict(),
            "env": _env_report(self.env, self.resolved),
            "discovered": [item.to_dict() for item in self.discovered],
            "resolved": [item.to_dict() for item in self.resolved],
            "health": _health_with_cache_age(self.health, self.state_file),
            "findings": list(self.findings),
            "summary": dict(self.summary),
            "trust": {"project": self.trust_project},
        }


def doctor_v6(
    *,
    json: bool = True,
    deep: bool = False,
    trust_project: bool = False,
    runtime_hint: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    env_files: Sequence[str | Path] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
) -> DoctorReport:
    """Run the additive v6 doctor without changing legacy doctor behavior."""

    from .engines.registry import EngineRegistry as V6EngineRegistry
    from .runtime.control_plane import prepare_control_plane_env

    del json

    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
    process_env = os.environ if env is None else env
    control = prepare_control_plane_env(
        runtime_hint=runtime_hint,
        cwd=resolved_cwd,
        process_env=process_env,
        env_files=env_files,
        plugin_paths=plugin_paths,
        include_builtin=True,
        trust_project=trust_project,
    )
    runtime = control.runtime
    paths = control.plugin_paths
    discoveries = control.discoveries
    env_snapshot = control.env
    registry = V6EngineRegistry(
        runtime=runtime,
        env=env_snapshot,
        plugin_paths=paths,
        discoveries=discoveries,
        include_builtin=True,
        trust_project=trust_project,
    )
    report = registry.report(health_mode="live_recovery" if deep else "light")
    findings = _trust_findings(env_snapshot, report.resolved, trust_project=trust_project)
    summary = _summarize_v6(report)
    summary["findings"] = len(findings)
    summary["trust_project_explicit"] = trust_project
    return DoctorReport(
        runtime=runtime,
        env=env_snapshot,
        discovered=report.discovered,
        resolved=report.resolved,
        health=report.health,
        summary=summary,
        trust_project=trust_project,
        findings=findings,
    )


def _summarize_v6(report: Any) -> dict[str, Any]:
    health_counts = {
        "unknown": 0,
        "healthy": 0,
        "degraded": 0,
        "unhealthy": 0,
        "disabled": 0,
        "cooldown": 0,
    }
    for item in report.health:
        health_counts[item.status] = health_counts.get(item.status, 0) + 1
    valid_discoveries = sum(1 for item in report.discovered if item.valid)
    configured = sum(1 for item in report.resolved if item.configured)
    if health_counts.get("unhealthy", 0):
        status = "fail"
    elif health_counts.get("degraded", 0):
        status = "warn"
    else:
        status = "ok"
    return {
        "status": status,
        "discovered": len(report.discovered),
        "valid_discoveries": valid_discoveries,
        "resolved": len(report.resolved),
        "configured": configured,
        "unknown": health_counts.get("unknown", 0),
        "healthy": health_counts.get("healthy", 0),
        "degraded": health_counts.get("degraded", 0),
        "unhealthy": health_counts.get("unhealthy", 0),
        "disabled": health_counts.get("disabled", 0),
        "cooldown": health_counts.get("cooldown", 0),
        "routable": len(report.routable),
    }


def _health_with_cache_age(
    health_items: Iterable[Any],
    state_file: str | Path | None = None,
) -> list[dict[str, Any]]:
    """给每个 health item 附加 health_cache_age_ms 和 health_cache_expires_at（从 state 只读）。"""
    from .runtime.state import get_cached_health_meta

    result = []
    for item in health_items:
        item_dict = item.to_dict()
        engine_id = item_dict.get("engine_id")
        capability = item_dict.get("capability")
        if engine_id and capability:
            meta = get_cached_health_meta(engine_id, capability, path=state_file)
            if meta:
                item_dict["health_cache_age_ms"] = round(meta["age_ms"], 2)
                item_dict["health_cache_expires_at"] = meta["expires_at"]
        result.append(item_dict)
    return result


def _env_report(env: Any, resolved: Iterable[Any]) -> dict[str, Any]:
    relevant = _relevant_env_names(resolved)
    return {
        "values": {
            key: value.to_dict()
            for key, value in sorted(env.values.items())
            if key in relevant
        },
        "candidates": [candidate.to_dict() for candidate in env.candidates],
        "conflicts": [
            conflict.to_dict()
            for conflict in env.conflicts
            if conflict.key in relevant
        ],
        "ignored_values": [
            value.to_dict()
            for value in env.ignored_values
            if value.key in relevant
        ],
        "warnings": list(env.warnings),
    }


def _trust_findings(
    env: Any,
    resolved: Iterable[Any],
    *,
    trust_project: bool,
) -> tuple[dict[str, Any], ...]:
    findings: list[dict[str, Any]] = []
    if trust_project:
        findings.append(
            {
                "code": "trust_project_enabled",
                "severity": "info",
                "message": "Project-level plugin adapters and project .env secrets are explicitly trusted.",
            }
        )

    for value in env.ignored_values:
        if value.ignore_reason == "project_env_ignored_secret":
            findings.append(
                {
                    "code": "project_env_ignored_secret",
                    "severity": "warn",
                    "key": value.key,
                    "path": str(value.source_path) if value.source_path else None,
                }
            )

    for descriptor in resolved:
        discovery = descriptor.discovery
        for reason in discovery.blocked_reasons:
            findings.append(
                {
                    "code": reason,
                    "severity": "warn",
                    "engine_id": descriptor.id,
                    "path": str(discovery.path),
                    "adapter": descriptor.manifest.adapter,
                    "trust_level": discovery.trust_level,
                }
            )
        if (
            descriptor.manifest.adapter
            and discovery.trust_level != "builtin"
            and descriptor.adapter_load_allowed
        ):
            findings.append(
                {
                    "code": "non_builtin_adapter",
                    "severity": "info",
                    "engine_id": descriptor.id,
                    "path": str(discovery.path),
                    "adapter": descriptor.manifest.adapter,
                    "trust_level": discovery.trust_level,
                }
            )
    return tuple(findings)


def _relevant_env_names(resolved: Iterable[Any]) -> set[str]:
    names: set[str] = set()
    for descriptor in resolved:
        requirements = descriptor.manifest.requirements.get("env")
        if not isinstance(requirements, list):
            continue
        for item in requirements:
            if not isinstance(item, Mapping):
                continue
            primary = item.get("env") or item.get("name")
            if primary:
                names.add(str(primary))
            aliases = item.get("aliases", [])
            if isinstance(aliases, list):
                names.update(str(alias) for alias in aliases if alias)
    return names


# ── v6 profile matrix ──


@dataclass(frozen=True)
class DoctorProfile:
    """Profile configuration for doctor matrix diagnostic."""
    id: str
    label: str
    runtime_hint: str | None
    trust_project: bool
    env_files: Sequence[str | Path] | None


def default_doctor_profiles(
    *,
    runtime_hint: str | None,
    trust_project: bool,
) -> tuple[DoctorProfile, ...]:
    """Return default doctor profiles for profile matrix diagnostic."""
    return (
        DoctorProfile(
            id="default",
            label="Default (caller context)",
            runtime_hint=runtime_hint,
            trust_project=trust_project,
            env_files=None,
        ),
        DoctorProfile(
            id="editable",
            label="Editable install (trusted)",
            runtime_hint="editable",
            trust_project=True,
            env_files=None,
        ),
        DoctorProfile(
            id="standalone",
            label="Standalone (untrusted)",
            runtime_hint="standalone",
            trust_project=False,
            env_files=None,
        ),
        DoctorProfile(
            id="S2",
            label="S2 codex (untrusted)",
            runtime_hint="codex",
            trust_project=False,
            env_files=None,
        ),
        DoctorProfile(
            id="S3",
            label="S3 hermes (untrusted)",
            runtime_hint="hermes",
            trust_project=False,
            env_files=None,
        ),
        DoctorProfile(
            id="cron-worker",
            label="Cron worker (standalone, untrusted)",
            runtime_hint="standalone",
            trust_project=False,
            env_files=None,
        ),
        DoctorProfile(
            id="hermes",
            label="Hermes runtime (untrusted)",
            runtime_hint="hermes",
            trust_project=False,
            env_files=None,
        ),
    )


@dataclass(frozen=True)
class ProfileMatrixReport:
    """Profile matrix diagnostic report."""
    kind: str = "profile_matrix"
    profiles: list[dict[str, Any]] = None
    summary: dict[str, Any] = None

    def __post_init__(self):
        if self.profiles is None:
            object.__setattr__(self, "profiles", [])
        if self.summary is None:
            object.__setattr__(self, "summary", {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "profiles": list(self.profiles),
            "summary": dict(self.summary),
        }


def doctor_profile_matrix(
    *,
    json: bool = True,
    deep: bool = False,
    trust_project: bool = False,
    runtime_hint: str | None = None,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    env_files: Sequence[str | Path] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
) -> ProfileMatrixReport:
    """Run profile matrix diagnostic across multiple runtime/profile combinations."""

    if deep:
        raise ValueError("--profile-matrix does not support --deep")

    profiles = default_doctor_profiles(
        runtime_hint=runtime_hint,
        trust_project=trust_project,
    )

    profile_results: list[dict[str, Any]] = []
    status_counts = {"ok": 0, "warn": 0, "fail": 0}

    for profile in profiles:
        report = doctor_v6(
            json=json,
            deep=False,
            trust_project=profile.trust_project,
            runtime_hint=profile.runtime_hint,
            cwd=cwd,
            env=env,
            env_files=env_files or profile.env_files,
            plugin_paths=plugin_paths,
        )

        # Extract router mode from env or runtime
        router_mode = "auto"
        if hasattr(report.env, "values"):
            v6_router_value = report.env.values.get("V6_ROUTER") or report.env.values.get("WRR_V6_ROUTER")
            if v6_router_value and hasattr(v6_router_value, "value"):
                router_mode = v6_router_value.value

        # Get routable engine IDs
        routable_engine_ids = [e.engine_id for e in getattr(report, "routable", []) if getattr(e, "engine_id", None)]

        profile_entry = {
            "id": profile.id,
            "label": profile.label,
            "runtime": {
                "name": report.runtime.name if hasattr(report.runtime, "name") else "unknown",
                "hint": profile.runtime_hint,
            },
            "env": {
                "candidates": len(report.env.candidates) if hasattr(report.env, "candidates") else 0,
                "conflicts": len(report.env.conflicts) if hasattr(report.env, "conflicts") else 0,
            },
            "router_mode": router_mode,
            "routable_engine_ids": routable_engine_ids,
            "discovered": len(report.discovered),
            "resolved": len(report.resolved),
            "health_summary": {
                "healthy": report.summary.get("healthy", 0),
                "degraded": report.summary.get("degraded", 0),
                "unhealthy": report.summary.get("unhealthy", 0),
            },
            "status": report.summary.get("status", "unknown"),
        }

        profile_results.append(profile_entry)
        status = profile_entry["status"]
        if status in status_counts:
            status_counts[status] += 1

    # Determine overall status
    if status_counts["fail"] > 0:
        overall_status = "fail"
    elif status_counts["warn"] > 0:
        overall_status = "warn"
    else:
        overall_status = "ok"

    summary = {
        "status": overall_status,
        "profiles_total": len(profile_results),
        "profiles_ok": status_counts["ok"],
        "profiles_warn": status_counts["warn"],
        "profiles_fail": status_counts["fail"],
    }

    return ProfileMatrixReport(
        kind="profile_matrix",
        profiles=profile_results,
        summary=summary,
    )
