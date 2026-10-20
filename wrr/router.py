"""Sequential fetch/extract and similar fallback with per-engine and total budgets.

规则（对齐执行包关键约束）：
  - 显式 provider → 单元素链，禁用 fallback
  - 自动模式按 config fallback order；引擎异常 **或空结果** → 切下一个
  - 每引擎只试一次（不重试同一 provider）
  - per-engine timeout + 按操作的总预算 config.budget_for(op)（超预算的后续引擎标记跳过）
"""
import asyncio
import time
from typing import Any, Dict, List, Optional, Protocol

from . import config
from .engines import _fusion
from .engines.base import SearchEngine
from .errors import AllEnginesFailedError, EngineError, FetchPolicyError, TerminalReason
from .extraction import extraction_error, qualify_extraction
from .schemas import DiagnosticEvent, FallbackStep, RouteQuality, RouteTrace, RouterResult


class FallbackRegistry(Protocol):
    """Minimal registry interface consumed by sequential fallback."""

    def get(self, name: str) -> Optional[SearchEngine]:
        ...


def build_chain(operation: str, explicit_provider: Optional[str]) -> List[str]:
    if operation not in ("extract", "similar"):
        raise ValueError(f"unknown operation: {operation}")
    if explicit_provider:
        return [explicit_provider]
    if operation == "extract":
        return list(config.EXTRACT_FALLBACK_ORDER)
    if operation == "similar":
        return list(config.SIMILAR_PROVIDERS)
    raise ValueError(f"unknown operation: {operation}")


async def _invoke(engine, operation: str, options) -> Any:
    if operation == "extract":
        return await engine.extract(options)
    if operation == "similar":
        return await engine.similar(options)
    raise ValueError(f"unknown operation: {operation}")


def _is_empty(operation: str, result) -> bool:
    if operation == "similar":
        return not result                       # 空 list
    return not getattr(result, "text", "")      # extract：空 text


def _count(operation: str, result) -> int:
    if operation == "similar":
        return len(result)
    return len(getattr(result, "text", "") or "")


def _safe_diagnostic_token(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    if not all(char.isalnum() or char in "._-" for char in value):
        return None
    return value


def _safe_request_diagnostic_details(details: Any) -> Optional[Dict[str, Any]]:
    """Project engine-owned request diagnostics onto the public safe schema."""
    if not isinstance(details, dict):
        return None

    projected: Dict[str, Any] = {}
    if isinstance(details.get("partial"), bool):
        projected["partial"] = details["partial"]
    if "cutoff_reason" in details:
        cutoff_reason = details["cutoff_reason"]
        if cutoff_reason is None:
            projected["cutoff_reason"] = None
        else:
            safe_cutoff = _safe_diagnostic_token(cutoff_reason)
            if safe_cutoff is not None:
                projected["cutoff_reason"] = safe_cutoff

    sources = details.get("sources")
    if isinstance(sources, list):
        safe_sources = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            safe_source: Dict[str, Any] = {}
            for key in ("source", "outcome", "detail", "cutoff_reason"):
                value = _safe_diagnostic_token(source.get(key))
                if value is not None:
                    safe_source[key] = value
            elapsed_ms = source.get("elapsed_ms")
            if (isinstance(elapsed_ms, (int, float))
                    and not isinstance(elapsed_ms, bool) and elapsed_ms >= 0):
                safe_source["elapsed_ms"] = round(float(elapsed_ms), 2)
            items_count = source.get("items_count")
            if type(items_count) is int and items_count >= 0:
                safe_source["items_count"] = items_count
            if safe_source:
                safe_sources.append(safe_source)
        projected["sources"] = safe_sources

    return projected or None


def _result_diagnostic_details(result) -> Optional[Dict[str, Any]]:
    """提取引擎返回值上的加法式请求诊断；普通 list 保持无感。"""
    source_diagnostics = getattr(result, "source_diagnostics", None)
    if not source_diagnostics:
        return None
    sources = []
    for diagnostic in source_diagnostics:
        to_dict = getattr(diagnostic, "to_dict", None)
        if callable(to_dict):
            sources.append(to_dict())
    if not sources:
        return None
    return _safe_request_diagnostic_details({
        "partial": bool(getattr(result, "partial", False)),
        "cutoff_reason": getattr(result, "cutoff_reason", None),
        "sources": sources,
    })


async def route(operation: str, options, registry: FallbackRegistry,
                explicit_provider: Optional[str] = None) -> RouterResult:
    if (operation == "extract" and getattr(options, "allow_private_origin", None) is not None
            and explicit_provider != "brave"):
        raise AllEnginesFailedError(
            "fetch denied: a private-origin grant requires explicit provider brave",
            terminal_reason=TerminalReason.FETCH_POLICY_DENIED,
        )
    chain = build_chain(operation, explicit_provider)
    budget = config.budget_for(operation)
    # Explicit single provider → allow full engine timeout, no fallback-chain budget cap.
    if explicit_provider and len(chain) == 1:
        budget = config.ENGINE_TIMEOUT.get(explicit_provider, budget)
    steps: List[FallbackStep] = []
    events: List[DiagnosticEvent] = []
    start = time.monotonic()
    actual: Optional[str] = None
    payload: Any = None
    terminal_reason: TerminalReason | None = None

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
        per_engine = min(engine.timeout, remaining)
        try:
            result = await asyncio.wait_for(_invoke(engine, operation, options), timeout=per_engine)
            step_elapsed = _elapsed_ms(step_start)
            body_error = extraction_error(result) if operation == "extract" else None
            if body_error or _is_empty(operation, result):
                step = FallbackStep(provider, False, 0, body_error or "empty result")
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
                count=count, details=_result_diagnostic_details(result)
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
                elapsed_ms=step_elapsed, count=0, message=step.error,
                details=_safe_request_diagnostic_details(getattr(e, "details", None)),
            )
            events.append(event)
            if isinstance(e, FetchPolicyError):
                terminal_reason = TerminalReason.FETCH_POLICY_DENIED
                break
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
        route_elapsed = _elapsed_ms(start)
        trace = RouteTrace(
            mode=None,
            mode_reason="v4_fallback_chain",
            selected_engines=chain,
            events=events,
            elapsed_ms=route_elapsed,
            timeout_ms=budget * 1000.0,
            quality=_route_quality(chain, steps, False),
        )
        raise AllEnginesFailedError(
            f"All engines failed for {operation}:\n{reasons}",
            diagnostics=trace,
            terminal_reason=terminal_reason,
        )

    route_elapsed = _elapsed_ms(start)
    quality = _route_quality(
        [step.provider for step in steps],
        steps,
        payload is not None,
    )
    if operation == "extract":
        qualify_extraction(payload, quality)
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


def _elapsed_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000.0


def _route_quality(selected_engines, steps, has_results: bool) -> RouteQuality:
    """Describe attempted fallback providers without changing control flow."""
    expected = list(dict.fromkeys(selected_engines))
    successful_set = {
        step.provider for step in steps
        if step.ok and step.count > 0 and step.provider in expected
    }
    successful = [name for name in expected if name in successful_set]
    failed = [name for name in expected if name not in successful_set]
    if not has_results:
        verdict, reasons = "failed", ["no_valid_results"]
    elif failed:
        verdict, reasons = "degraded_success", ["expected_sources_failed"]
    else:
        verdict, reasons = "complete", []
    return RouteQuality(
        verdict=verdict,
        expected_sources=expected,
        successful_sources=successful,
        failed_sources=failed,
        independent_source_count=len({_fusion.source_family(name) for name in successful}),
        min_required=1,
        reasons=reasons,
    )
