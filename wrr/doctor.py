"""WRR doctor 运行器 + 诊断汇总。"""
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
        deep: 是否执行深度检查（P0 未实现）

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime.to_dict(),
            "env": _env_report(self.env, self.resolved),
            "discovered": [item.to_dict() for item in self.discovered],
            "resolved": [item.to_dict() for item in self.resolved],
            "health": [item.to_dict() for item in self.health],
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

    from .cli.install import _filtered_env, _required_env
    from .engines.loader import discover_engine_plugins
    from .engines.registry import EngineRegistry as V6EngineRegistry
    from .runtime.detect import detect_runtime
    from .runtime.env import load_env

    del json, deep  # P0 v6 doctor is report-only and light-health only.

    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
    process_env = os.environ if env is None else env
    runtime = detect_runtime(explicit=runtime_hint, cwd=resolved_cwd, env=process_env)
    paths = tuple(plugin_paths or (resolved_cwd / "plugins" / "engines",))
    discoveries = tuple(discover_engine_plugins(paths, include_builtin=True))
    required_env = _required_env(discoveries)
    env_snapshot = load_env(
        runtime,
        overrides=_filtered_env(process_env, required_env),
        env_files=env_files,
        trust_project=trust_project,
    )
    registry = V6EngineRegistry(
        runtime=runtime,
        env=env_snapshot,
        plugin_paths=paths,
        discoveries=discoveries,
        include_builtin=True,
        trust_project=trust_project,
    )
    report = registry.report()
    return DoctorReport(
        runtime=runtime,
        env=env_snapshot,
        discovered=report.discovered,
        resolved=report.resolved,
        health=report.health,
        summary=_summarize_v6(report),
        trust_project=trust_project,
    )


def _summarize_v6(report: Any) -> dict[str, Any]:
    health_counts = {"healthy": 0, "degraded": 0, "unhealthy": 0}
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
        "healthy": health_counts.get("healthy", 0),
        "degraded": health_counts.get("degraded", 0),
        "unhealthy": health_counts.get("unhealthy", 0),
        "routable": len(report.routable),
    }


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
