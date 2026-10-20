"""Bridge trusted inventory execution references to the core search boundary.

Adapters bind credentials per task (also propagated by ``to_thread``), never
mutate the process environment, and expose only stable failure categories.
Execution references, rather than transport labels, determine local adapters.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import inspect
import re
import time
from typing import Mapping, Protocol

import httpx

from wrr import config
from wrr.engines.adapter_bridge import _load_adapter_class
from wrr.errors import EngineError, EngineTimeoutError, RateLimitError
from wrr.schemas import SearchOptions, SearchResult

from . import exec_declarative
from .contracts import RunStatus, SourceDescriptor


class SourceAdapter(Protocol):
    async def search(self, query: str, count: int, *, deadline: float) -> list[SearchResult]:
        """Search before an absolute ``loop.time()`` deadline."""
        ...


class AdapterError(Exception):
    """A source failure without provider output, request URLs or credentials."""

    def __init__(self, status: RunStatus):
        self.status = RunStatus(status)
        if self.status in {RunStatus.OK, RunStatus.EMPTY}:
            raise ValueError("AdapterError requires a failure status")
        super().__init__(self.status.value)


_OUTCOMES = {
    "auth_missing": RunStatus.AUTH_ERROR,
    "auth_error": RunStatus.AUTH_ERROR,
    "permission_denied": RunStatus.AUTH_ERROR,
    "unauthenticated": RunStatus.AUTH_ERROR,
    "soft_blocked": RunStatus.RATE_LIMIT,
    "rate_limit": RunStatus.RATE_LIMIT,
    "timeout": RunStatus.TIMEOUT,
    "cutoff": RunStatus.TIMEOUT,
    "schema_error": RunStatus.SCHEMA_ERROR,
    "cancelled": RunStatus.CANCELLED,
}


def _failure_status(error: Exception) -> RunStatus:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (TimeoutError, EngineTimeoutError, httpx.TimeoutException)):
            return RunStatus.TIMEOUT
        if isinstance(current, RateLimitError):
            return RunStatus.RATE_LIMIT
        category = getattr(current, "status", None) or getattr(current, "failure_category", None)
        if category in _OUTCOMES:
            return _OUTCOMES[category]
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if response is not None:
            status = getattr(response, "status_code", status)
        if status in (401, 403):
            return RunStatus.AUTH_ERROR
        if status in (429, 432):
            return RunStatus.RATE_LIMIT
        if isinstance(current, (ValueError, TypeError, KeyError, AttributeError)):
            return RunStatus.SCHEMA_ERROR
        if isinstance(current, EngineError):
            # Several legacy adapters preserve only a short category message.
            text = str(current).lower()
            if re.search(r"\b(401|403)\b|unauth|forbidden|(?:api[_ ]?key|token).*(?:not set|missing)|auth(?:entication)? failed|authentication", text):
                return RunStatus.AUTH_ERROR
            if re.search(r"\b(429|432)\b|rate.?limit", text):
                return RunStatus.RATE_LIMIT
            if "timed out" in text or "timeout" in text:
                return RunStatus.TIMEOUT
            if re.search(r"parse|json|schema|malformed", text):
                return RunStatus.SCHEMA_ERROR
        cause = current.__cause__ or current.__context__
        if cause is not None:
            pending.append(cause)
    return RunStatus.ERROR


class _Adapter:
    def __init__(self, descriptor: SourceDescriptor, env: Mapping[str, str]):
        self.descriptor = descriptor
        self.env = env

    async def search(self, query: str, count: int, *, deadline: float) -> list[SearchResult]:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AdapterError(RunStatus.TIMEOUT)
        try:
            with config.source_environment(self.env, deadline=time.monotonic() + remaining):
                async with asyncio.timeout(remaining):
                    rows = await self._search(query, count, remaining)
            if not isinstance(rows, list) or any(not isinstance(row, SearchResult) for row in rows):
                raise AdapterError(RunStatus.SCHEMA_ERROR)
            return rows
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(_failure_status(exc)) from None

    async def _search(self, query: str, count: int, timeout: float) -> list[SearchResult]:
        raise NotImplementedError


class _EngineAdapter(_Adapter):
    def __init__(self, descriptor: SourceDescriptor, env: Mapping[str, str]):
        super().__init__(descriptor, env)
        self.engine = None
        self.exa_mode: str | None = None

    async def _search(self, query: str, count: int, timeout: float) -> list[SearchResult]:
        if self.engine is None:
            self.engine = _load_adapter_class(self.descriptor.adapter)()
        options = SearchOptions(query=query, count=count, provider=self.descriptor.id, exa_mode=self.exa_mode)
        search = self.engine.search
        if inspect.iscoroutinefunction(search):
            return await search(options)
        rows = await asyncio.to_thread(search, options)
        return await rows if inspect.isawaitable(rows) else rows


class _ConfiguredAdapter(_EngineAdapter):
    async def _search(self, query: str, count: int, timeout: float) -> list[SearchResult]:
        if self.engine is None:
            self.engine = _load_adapter_class(self.descriptor.adapter)()
            self.engine._configured_sources = {self.descriptor.id: self.descriptor.configured_source}
        execution = await self.engine._fetch_source_execution(
            self.descriptor.id, SearchOptions(query=query, count=count), datetime.now(timezone.utc),
        )
        outcome = execution.diagnostic.outcome.value
        if outcome not in {"ready", "empty"}:
            raise AdapterError(_OUTCOMES.get(outcome, RunStatus.ERROR))
        return [row for _, row in sorted(execution.scored, key=lambda item: item[0], reverse=True)][:count]


class _DeclarativeAdapter(_Adapter):
    def __init__(self, descriptor: SourceDescriptor, env: Mapping[str, str]):
        super().__init__(descriptor, env)
        block = descriptor.declarative
        self.spec = block if isinstance(block, exec_declarative.DeclarativeSpec) else exec_declarative.DeclarativeSpec.parse(block)

    async def _search(self, query: str, count: int, timeout: float) -> list[SearchResult]:
        if isinstance(self.spec, list):
            raise AdapterError(RunStatus.SCHEMA_ERROR)
        # The executor uses monotonic(), while the public seam uses loop.time().
        return await exec_declarative.run_declarative(
            self.spec, query, count, env=self.env, deadline=time.monotonic() + timeout,
        )


def build_adapters(descriptors: list[SourceDescriptor], *, env: Mapping[str, str], exa_mode: str | None = None) -> dict[str, SourceAdapter]:
    """Wrap routable local sources; execute owns query-style conversion and evidence."""
    snapshot = dict(env)
    adapters: dict[str, SourceAdapter] = {}
    for descriptor in descriptors:
        if not descriptor.routable:
            continue
        if descriptor.declarative is not None:
            adapters[descriptor.id] = _DeclarativeAdapter(descriptor, snapshot)
        elif descriptor.adapter:
            wrapper = _ConfiguredAdapter if descriptor.configured_source is not None else _EngineAdapter
            adapter = wrapper(descriptor, snapshot)
            adapter.exa_mode = exa_mode
            adapters[descriptor.id] = adapter
    return adapters
