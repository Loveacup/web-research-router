"""Plugin-owned, bounded sampler for one fixed grounding campaign."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

_QUERIES = (
    "official documentation current release notes",
    "official documentation security guidance",
    "official documentation API compatibility policy",
    "official documentation operational best practices",
)
_TERMINAL_STATES = frozenset({"draining", "closed", "disabled"})


@dataclass(frozen=True)
class CampaignSamplerConfig:
    interval_sec: float


def _sample_succeeded(result: Any) -> bool:
    """Recognize the production handler's structured fail-open error payloads."""
    payload = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return False
    if not isinstance(payload, Mapping):
        return False
    if "error" in payload or payload.get("success") is False:
        return False
    details = payload.get("details")
    if isinstance(details, Mapping):
        quality = details.get("quality")
        if isinstance(quality, Mapping) and quality.get("verdict") == "failed":
            return False
    return True


def _record_sampler_fault(controller: Any, reason: str) -> None:
    """Disable the campaign best-effort without leaking failure into the user turn."""
    record_fault = getattr(controller, "record_fault", None)
    if callable(record_fault):
        try:
            record_fault(reason)
        except Exception:  # noqa: BLE001 - campaign failure must not break search
            logger.warning("campaign sampler could not persist fault: %s", reason)


def parse_campaign_sampler_config(raw: Any) -> CampaignSamplerConfig | None:
    """Parse a strict opt-in sampler config; disabled/missing values are inert."""
    if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
        return None
    allowed = {"enabled", "interval_sec"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown campaign sampler keys: {sorted(unknown)}")
    interval = raw.get("interval_sec")
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ValueError("campaign sampler interval_sec must be finite and at least 1800")
    seconds = float(interval)
    if not math.isfinite(seconds) or seconds < 1800.0:
        raise ValueError("campaign sampler interval_sec must be finite and at least 1800")
    return CampaignSamplerConfig(interval_sec=seconds)


async def run_campaign_sampler(
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
    controller: Any,
    *,
    campaign_id: str,
    config: CampaignSamplerConfig,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    should_stop: Callable[[], bool] = lambda: False,
) -> None:
    """Sample through the registered production handler until the campaign drains.

    The loop is intentionally process-local. A gateway restart leaves the open
    ledger inspect-only, preserving fail-closed campaign semantics.
    """
    index = 0
    while (
        not should_stop()
        and getattr(controller, "state", "disabled") not in _TERMINAL_STATES
    ):
        args = {
            "query": _QUERIES[index % len(_QUERIES)],
            "mode": "grounding",
            "campaign_id": campaign_id,
        }
        try:
            result = await handler(args)
        except Exception as exc:  # noqa: BLE001 - stop bounded sampler, preserve gateway
            _record_sampler_fault(controller, "sampler_search_failed")
            logger.warning("campaign sampler stopped after search failure: %s", exc)
            return
        if not _sample_succeeded(result):
            _record_sampler_fault(controller, "sampler_search_failed")
            logger.warning("campaign sampler stopped after structured search failure")
            return
        index += 1
        if getattr(controller, "state", "disabled") in _TERMINAL_STATES:
            return
        try:
            await sleep(config.interval_sec)
        except Exception as exc:  # noqa: BLE001 - stop bounded sampler, preserve gateway
            _record_sampler_fault(controller, "sampler_sleep_failed")
            logger.warning("campaign sampler stopped after sleep failure: %s", exc)
            return


def start_campaign_sampler(
    handler: Callable[[dict[str, Any]], Awaitable[Any]],
    controller: Any,
    *,
    campaign_id: str,
    config: CampaignSamplerConfig,
) -> Callable[[], None]:
    """Start one cancellable plugin-owned sampler thread.

    Hermes loads plugins and invokes bounded hooks from synchronous worker
    threads, so neither surface guarantees a running asyncio loop. The sampler
    therefore owns its loop, while the unload callback cancels the exact task
    thread-safely before joining it.
    """
    stop_event = threading.Event()
    ready = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, Any] = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(run_campaign_sampler(
            handler,
            controller,
            campaign_id=campaign_id,
            config=config,
            should_stop=stop_event.is_set,
        ))
        with state_lock:
            state["loop"] = loop
            state["task"] = task
        ready.set()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    thread = threading.Thread(
        target=worker,
        name=f"wrr:{campaign_id}:sampler",
        daemon=True,
    )
    thread.start()

    def stop() -> None:
        stop_event.set()
        ready.wait(5.0)
        with state_lock:
            loop = state.get("loop")
            task = state.get("task")
        if loop is not None and task is not None and not task.done():
            loop.call_soon_threadsafe(task.cancel)
        if threading.current_thread() is not thread:
            thread.join(timeout=5.0)
        if thread.is_alive():
            raise RuntimeError("campaign sampler did not stop during plugin unload")

    return stop
