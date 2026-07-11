"""Fallback 路由（search / extract / similar 共用）+ 总预算控制。

规则（对齐执行包关键约束）：
  - 显式 provider → 单元素链，禁用 fallback
  - 自动模式按 config fallback order；引擎异常 **或空结果** → 切下一个
  - 每引擎只试一次（不重试同一 provider）
  - per-engine timeout + 按操作的总预算 config.budget_for(op)（超预算的后续引擎标记跳过）
"""
import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Protocol

from . import config
from .engines import _fusion
from .engines.base import SearchEngine
from .errors import EngineError, AllEnginesFailedError
from .schemas import (DecisionSnapshot, DiagnosticEvent, FallbackStep,
                      RouteQuality, RouteTrace, RouterResult, SearchResult)


class SearchRegistry(Protocol):
    """Minimal registry interface consumed by router hot paths."""

    def get(self, name: str) -> Optional[SearchEngine]:
        ...


def build_chain(operation: str, explicit_provider: Optional[str],
                query: Optional[str] = None) -> List[str]:
    if explicit_provider:
        return [explicit_provider]
    if operation == "search":
        chain = list(config.SEARCH_FALLBACK_ORDER)
        promote = []
        # P3-3: explicit site:github.com wins over early-news community promotion.
        if query and config.github_triggered(query):
            promote.append("github")           # site:github.com
        if query and config.early_news_triggered(query):
            promote.append("community")      # P3-3: early-news → RSS/community first
        if query and config.community_triggered(query):
            promote.append("community")      # site:reddit/hn/twitter/zhihu/weibo
        if promote:
            chain = promote + [p for p in chain if p not in promote]
        return chain
    if operation == "extract":
        return list(config.EXTRACT_FALLBACK_ORDER)
    if operation == "similar":
        return list(config.SIMILAR_PROVIDERS)
    raise ValueError(f"unknown operation: {operation}")


async def _invoke(engine, operation: str, options) -> Any:
    if operation == "search":
        return await engine.search(options)
    if operation == "extract":
        return await engine.extract(options)
    if operation == "similar":
        return await engine.similar(options)
    raise ValueError(f"unknown operation: {operation}")


def _is_empty(operation: str, result) -> bool:
    if operation in ("search", "similar"):
        return not result                       # 空 list
    return not getattr(result, "text", "")      # extract：空 text


def _count(operation: str, result) -> int:
    if operation in ("search", "similar"):
        return len(result)
    return len(getattr(result, "text", "") or "")


async def route(operation: str, options, registry: SearchRegistry,
                explicit_provider: Optional[str] = None) -> RouterResult:
    chain = build_chain(operation, explicit_provider, getattr(options, "query", None))
    budget = config.budget_for(operation)
    # Explicit single provider → allow full engine timeout, no fallback-chain budget cap.
    if explicit_provider and len(chain) == 1:
        budget = config.ENGINE_TIMEOUT.get(explicit_provider, budget)
    if operation == "search" and chain and chain[0] == "community":
        budget = max(budget, config.ENGINE_TIMEOUT.get("community", config.DEFAULT_ENGINE_TIMEOUT))
    steps: List[FallbackStep] = []
    events: List[DiagnosticEvent] = []
    start = time.monotonic()
    actual: Optional[str] = None
    payload: Any = None

    for provider in chain:
        step_start = time.monotonic()
        elapsed = step_start - start
        if elapsed > budget:
            step = FallbackStep(provider, False, 0, "budget exceeded (skipped)")
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=False, category=operation,
                elapsed_ms=0.0, count=0, message=step.error
            )
            events.append(event)
            continue
        engine = registry.get(provider)
        if engine is None:
            step = FallbackStep(provider, False, 0, f"unknown provider: {provider}")
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=False, category=operation,
                elapsed_ms=_elapsed_ms(step_start), count=0, message=step.error
            )
            events.append(event)
            continue
        remaining = budget - elapsed
        per_engine = min(engine.timeout, max(0.1, remaining))
        try:
            result = await asyncio.wait_for(_invoke(engine, operation, options), timeout=per_engine)
            step_elapsed = _elapsed_ms(step_start)
            if _is_empty(operation, result):
                step = FallbackStep(provider, False, 0, "empty result")
                steps.append(step)
                event = DiagnosticEvent(
                    engine=provider, ok=False, category=operation,
                    elapsed_ms=step_elapsed, timeout_ms=per_engine * 1000.0,
                    count=0, message=step.error
                )
                events.append(event)
                continue
            count = _count(operation, result)
            step = FallbackStep(provider, True, count)
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=True, category=operation,
                elapsed_ms=step_elapsed, timeout_ms=per_engine * 1000.0,
                count=count
            )
            events.append(event)
            actual, payload = provider, result
            break
        except asyncio.TimeoutError:
            step_elapsed = _elapsed_ms(step_start)
            step = FallbackStep(provider, False, 0, f"timeout >{per_engine:.1f}s")
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=False, category=operation,
                elapsed_ms=step_elapsed, timeout_ms=per_engine * 1000.0,
                count=0, message="timeout"
            )
            events.append(event)
        except EngineError as e:
            step_elapsed = _elapsed_ms(step_start)
            step = FallbackStep(provider, False, 0, str(e) or type(e).__name__)
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=False, category=operation,
                elapsed_ms=step_elapsed, count=0, message=step.error
            )
            events.append(event)
        except Exception as e:  # 引擎内部未归一的异常也不该让整链崩
            step_elapsed = _elapsed_ms(step_start)
            step = FallbackStep(provider, False, 0, str(e) or type(e).__name__)
            steps.append(step)
            event = DiagnosticEvent(
                engine=provider, ok=False, category=operation,
                elapsed_ms=step_elapsed, count=0, message=step.error
            )
            events.append(event)

    if actual is None:
        reasons = "\n".join(f"  - {s.provider}: {s.error}" for s in steps)
        raise AllEnginesFailedError(f"All engines failed for {operation}:\n{reasons}")

    route_elapsed = _elapsed_ms(start)
    quality = _route_quality(
        None,
        [step.provider for step in steps],
        steps,
        payload is not None,
    )
    trace = RouteTrace(
        mode=None,
        mode_reason="v4_fallback_chain",
        selected_engines=chain,
        events=events,
        elapsed_ms=route_elapsed,
        timeout_ms=budget * 1000.0,
        quality=quality,
    )
    return RouterResult(actual_provider=actual, payload=payload, fallback_chain=steps,
                        diagnostics=trace)


# ══════════════════════════════════════════════════════════════════════
# v5.0：mode 分发 + 多引擎并行 + 跨源 RRF 融合（加法式，与 route() 并存）
# 真相源：/tmp/wrr-v5.0-stdd-final.md。STDD §3。
# ══════════════════════════════════════════════════════════════════════

_V5_MODES = ("discovery", "grounding", "research", "academic", "platform",
             "recovery", "local", "broad")

_QUALITY_MIN_SOURCES = {
    "grounding": 2,
    "academic": 2,
    "research": 2,
    "broad": 2,
    "discovery": 1,
    "platform": 1,
    "local": 1,
    "recovery": 1,
}


def _unique(items) -> List[str]:
    return list(dict.fromkeys(items))


def _route_quality(mode, selected_engines, steps, has_results: bool) -> RouteQuality:
    """按 D2 contract 计算 route-level quality；不改变路由控制流。"""
    expected = _unique(selected_engines)
    successful_set = {
        step.provider for step in steps
        if step.ok and step.count > 0 and step.provider in expected
    }
    successful = [name for name in expected if name in successful_set]
    failed = [name for name in expected if name not in successful_set]
    min_required = _QUALITY_MIN_SOURCES.get(mode, 1)

    if not has_results:
        verdict = "failed"
        reasons = ["no_valid_results"]
    elif len(successful) < min_required:
        verdict = "insufficient"
        reasons = [f"successful_sources_below_minimum:{len(successful)}<{min_required}"]
    elif failed:
        verdict = "degraded_success"
        reasons = ["expected_sources_failed"]
    else:
        verdict = "complete"
        reasons = []

    return RouteQuality(
        verdict=verdict,
        expected_sources=expected,
        successful_sources=successful,
        failed_sources=failed,
        independent_source_count=len(successful),
        min_required=min_required,
        reasons=reasons,
    )


def resolve_mode(options) -> str:
    """route_mode 优先；legacy mode 若为 WRR mode 继续兼容，否则自动分类。"""
    route_mode = getattr(options, "route_mode", None)
    if route_mode in _V5_MODES:
        return route_mode
    legacy_mode = getattr(options, "mode", None)
    if legacy_mode in _V5_MODES:
        return legacy_mode
    return config.classify_intent(getattr(options, "query", "") or "")


def legacy_selection_plan(options) -> DecisionSnapshot:
    """纯选择：从 options 计算 legacy 路由的 DecisionSnapshot（无执行副作用）。

    显式 provider → 单元素 snapshot（mode=None，engine_names=(provider,)）；
    否则 resolve_mode → mode_engines/MODE_WEIGHTS，weights 收窄到实际引擎组合。
    """
    explicit = getattr(options, "provider", None)
    if explicit:
        return DecisionSnapshot(
            source="legacy",
            mode=None,
            mode_reason="explicit_provider",
            explicit_provider=explicit,
            engine_names=(explicit,),
            weights=(),
        )
    mode = resolve_mode(options)
    explicit_mode = (
        getattr(options, "route_mode", None) in _V5_MODES
        or getattr(options, "mode", None) in _V5_MODES
    )
    mode_reason = "explicit" if explicit_mode else "classify_intent"
    query = getattr(options, "query", "") or ""
    weights = config.MODE_WEIGHTS.get(mode, config.MODE_WEIGHTS["grounding"])
    engine_names = config.mode_engines(mode, query)
    used = tuple((n, float(weights.get(n, 1.0))) for n in engine_names)
    return DecisionSnapshot(
        source="legacy",
        mode=mode,
        mode_reason=mode_reason,
        explicit_provider=None,
        engine_names=tuple(engine_names),
        weights=used,
    )


def _elapsed_ms(start: float) -> float:
    """计算从 start 到现在的毫秒数。"""
    return (time.monotonic() - start) * 1000.0


def _event_from_step(step: FallbackStep, elapsed_ms: float, timeout_ms: Optional[float]) -> DiagnosticEvent:
    """从 FallbackStep 构造 DiagnosticEvent。"""
    return DiagnosticEvent(
        engine=step.provider,
        ok=step.ok,
        category="search",
        elapsed_ms=elapsed_ms,
        timeout_ms=timeout_ms,
        count=step.count,
        message=step.error,
    )


async def _run_engine(registry, name, options, budget):
    """跑单引擎 search，超时/异常隔离，返回 (name, results_or_None, FallbackStep, DiagnosticEvent)。"""
    start = time.monotonic()
    engine = registry.get(name)
    if engine is None:
        step = FallbackStep(name, False, 0, f"unknown provider: {name}")
        event = DiagnosticEvent(
            engine=name, ok=False, category="search",
            elapsed_ms=_elapsed_ms(start), count=0, message=step.error
        )
        return name, None, step, event
    try:
        per_engine = min(engine.timeout, max(0.1, budget))
        res = await asyncio.wait_for(engine.search(options), timeout=per_engine)
        elapsed = _elapsed_ms(start)
        if not res:
            step = FallbackStep(name, False, 0, "empty result")
            event = DiagnosticEvent(
                engine=name, ok=False, category="search",
                elapsed_ms=elapsed, timeout_ms=per_engine * 1000.0, count=0, message=step.error
            )
            return name, None, step, event
        step = FallbackStep(name, True, len(res))
        event = DiagnosticEvent(
            engine=name, ok=True, category="search",
            elapsed_ms=elapsed, timeout_ms=per_engine * 1000.0, count=len(res)
        )
        return name, res, step, event
    except asyncio.TimeoutError:
        elapsed = _elapsed_ms(start)
        step = FallbackStep(name, False, 0, "timeout")
        event = DiagnosticEvent(
            engine=name, ok=False, category="search",
            elapsed_ms=elapsed, timeout_ms=engine.timeout * 1000.0, count=0, message="timeout"
        )
        return name, None, step, event
    except Exception as e:                       # 单引擎异常不拖垮整组
        elapsed = _elapsed_ms(start)
        step = FallbackStep(name, False, 0, str(e) or type(e).__name__)
        event = DiagnosticEvent(
            engine=name, ok=False, category="search",
            elapsed_ms=elapsed, count=0, message=step.error
        )
        return name, None, step, event


async def _dispatch(registry, engine_names, options, weights, mode, budget):
    """并行发射一组引擎 → 跨源 RRF 融合 → canonical 去重。返回 (payload, steps, events)。"""
    results = await asyncio.gather(
        *[_run_engine(registry, n, options, budget) for n in engine_names])
    per_source: Dict[str, List[SearchResult]] = {}
    steps: List[FallbackStep] = []
    events: List[DiagnosticEvent] = []
    for name, res, step, event in results:
        steps.append(step)
        events.append(event)
        if res:
            per_source[name] = res
    if not per_source:
        return None, steps, events
    fused = _fusion.rrf_fuse(per_source, k=config.RRF_K, weights=weights)
    deduped = _fusion.dedup_cluster([f["doc"] for f in fused],
                                    config.COMMUNITY_DEDUP_THRESHOLD)
    return deduped[:options.count], steps, events


def _v6_router_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    source = os.environ if env is None else env
    return source.get("WRR_V6_ROUTER") == "1"


def _descriptor_backed_registry() -> SearchRegistry:
    """Build a v6 descriptor-backed legacy registry for opt-in shadow routing."""

    from .registry import default_registry_v6_shadow

    report = default_registry_v6_shadow()
    return report.registry


def _route_registry(
    registry: SearchRegistry,
    *,
    descriptor_registry_factory=None,
    env: Optional[Dict[str, str]] = None,
) -> SearchRegistry:
    """Return the registry consumed by v5 routing.

    Normal calls keep the provided legacy registry. Shadow routing is activated
    only by the env flag or by an explicit injected factory in tests/callers.
    """

    if descriptor_registry_factory is not None:
        return descriptor_registry_factory()
    if _v6_router_enabled(env):
        return _descriptor_backed_registry()
    return registry


async def route_search_v5(
    options,
    registry: SearchRegistry,
    *,
    descriptor_registry_factory=None,
) -> RouterResult:
    """v5 搜索路由：classify_intent → mode → 并行引擎 → RRF 融合 → 去重排序。

    显式 options.provider 仍走单引擎（兼容 v4 语义）。主 mode 空结果 → recovery 兜底。
    """
    route_start = time.monotonic()
    registry = _route_registry(
        registry,
        descriptor_registry_factory=descriptor_registry_factory,
    )

    # P1 S1：先做纯选择，得到 DecisionSnapshot（显式 provider 也生成单元素 snapshot）
    plan = legacy_selection_plan(options)

    # 显式 provider → 单引擎（复用 v4 route 语义，禁用 mode 路由）
    if plan.explicit_provider:
        return await route("search", options, registry,
                           explicit_provider=plan.explicit_provider)

    mode = plan.mode
    mode_reason = plan.mode_reason
    budget = config.budget_for("search")
    weights = dict(plan.weights)
    engine_names = list(plan.engine_names)

    payload, steps, events = await _dispatch(registry, engine_names, options, weights, mode, budget)

    # v5.3 全量陈旧门控：local mode 下所有结果 freshness < 0.8 → 追加外网交叉
    if mode == "local" and payload is not None:
        if all(getattr(r, "freshness_score", 1.0) < 0.8 for r in payload):
            web_mode = config.classify_intent(getattr(options, "query", "") or "")
            if web_mode == "local":
                web_mode = "discovery"
            web_engines = config.mode_engines(web_mode, getattr(options, "query", "") or "")
            web_weights = config.MODE_WEIGHTS.get(web_mode, config.MODE_WEIGHTS["grounding"])
            wpayload, wsteps, wevents = await _dispatch(registry, web_engines, options, web_weights,
                                               web_mode, budget)
            steps.extend(wsteps)
            events.extend(wevents)
            if wpayload is not None:
                # 合并：web 结果在前（更新），本地垫后
                payload = wpayload + payload

    # 主 mode 空 → recovery 兜底（Brave + Exa + SearXNG）
    if payload is None and mode != "recovery":
        if not config.recovery_allowed():
            route_elapsed = _elapsed_ms(route_start)
            trace = RouteTrace(
                mode=mode,
                mode_reason="recovery_blocked",
                selected_engines=engine_names,
                events=events,
                elapsed_ms=route_elapsed,
                timeout_ms=budget * 1000.0,
            )
            reasons = "\n".join(f"  - {s.provider}: {s.error}" for s in steps if not s.ok)
            raise AllEnginesFailedError(
                f"All engines failed for search (mode={mode}) and recovery is blocked in this runtime:\n{reasons}"
            )
        rec_weights = config.MODE_WEIGHTS["recovery"]
        rec_names = config.mode_engines("recovery", getattr(options, "query", "") or "")
        rpayload, rsteps, revents = await _dispatch(registry, rec_names, options, rec_weights,
                                           "recovery", budget)
        steps.extend(rsteps)
        events.extend(revents)
        if rpayload is not None:
            route_elapsed = _elapsed_ms(route_start)
            quality = _route_quality("recovery", rec_names, steps, bool(rpayload))
            trace = RouteTrace(
                mode="recovery",
                mode_reason="recovery_fallback",
                selected_engines=rec_names,
                events=events,
                elapsed_ms=route_elapsed,
                timeout_ms=budget * 1000.0,
                quality=quality,
            )
            return RouterResult(actual_provider=f"rrf:recovery", payload=rpayload,
                                fallback_chain=steps, mode="recovery",
                                fusion_method="rrf", weights=dict(rec_weights),
                                diagnostics=trace)

    if payload is None:
        reasons = "\n".join(f"  - {s.provider}: {s.error}" for s in steps if not s.ok)
        raise AllEnginesFailedError(f"All engines failed for search (mode={mode}):\n{reasons}")

    route_elapsed = _elapsed_ms(route_start)
    quality = _route_quality(
        mode, [step.provider for step in steps], steps, bool(payload)
    )
    trace = RouteTrace(
        mode=mode,
        mode_reason=mode_reason,
        selected_engines=engine_names,
        events=events,
        elapsed_ms=route_elapsed,
        timeout_ms=budget * 1000.0,
        quality=quality,
    )
    used = {n: weights.get(n, 1.0) for n in engine_names}
    return RouterResult(actual_provider=f"rrf:{mode}", payload=payload,
                        fallback_chain=steps, mode=mode,
                        fusion_method="rrf", weights=used,
                        diagnostics=trace)
