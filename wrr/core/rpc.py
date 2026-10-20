"""One-search NDJSON bridge: credentials and host execution stay in the host."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
import inspect
import json
import re
from typing import Any, Callable, Mapping

from wrr.schemas import SearchResult
from .contracts import (Capabilities, Health, Kind, PrivacyClass, Provision,
                        RunStatus, SourceDescriptor, SourceProfile, Transport)
from .judge import JudgeError, Question, parse_response, resolve_judge
from .pipeline import search
from .rubric import MAX_SOURCES
from .sources import AdapterError

MAX_LINE_BYTES = 1024 * 1024
_REASONS = {"unavailable", "authentication", "http_error", "malformed_response",
            "timeout", "network", "cancelled"}
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,79}")


class RpcError(Exception):
    def __init__(self, reason: str):
        self.reason = reason if isinstance(reason, str) and reason in _REASONS else "unavailable"
        super().__init__(self.reason)


async def _read(reader):
    line = await reader.readline()
    if not line:
        raise EOFError
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("RPC line too large")
    def reject(value):
        raise ValueError("nonfinite JSON")
    message = json.loads(line, parse_constant=reject)
    if not isinstance(message, dict):
        raise ValueError("RPC message must be an object")
    return message


class RpcChannel:
    """Multiplex replies while listening for cancellation, including during search."""
    def __init__(self, reader, emit: Callable):
        self.reader = reader
        self.emit = emit
        self.pending: dict[str, asyncio.Future] = {}
        self.cancelled = asyncio.Event()
        self.sequence = 0
        self.closed = False

    async def send(self, message):
        result = self.emit(message)
        if inspect.isawaitable(result):
            await result

    async def listen(self):
        try:
            while True:
                message = await _read(self.reader)
                if message.get("method") == "cancel":
                    self.cancelled.set()
                    return
                request_id = message.get("id")
                if not isinstance(request_id, str):
                    raise ValueError("invalid RPC reply id")
                future = self.pending.get(request_id)
                if future is None or future.done():
                    continue  # Late replies to timed-out requests are not new work.
                if "error" in message:
                    error = message["error"]
                    future.set_exception(RpcError(error.get("reason") if isinstance(error, dict) else "unavailable"))
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    future.set_exception(RpcError("malformed_response"))
        except (EOFError, ValueError, UnicodeError, OSError):
            self.cancelled.set()
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RpcError("cancelled"))

    async def request(self, method: str, params: dict, *, deadline: float):
        if self.closed or self.cancelled.is_set():
            raise RpcError("cancelled")
        if deadline <= asyncio.get_running_loop().time():
            raise RpcError("timeout")
        self.sequence += 1
        request_id = str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            async with asyncio.timeout_at(deadline):
                return await future
        except (TimeoutError, asyncio.CancelledError):
            # Stop the host operation too; a timed-out judge must not keep posting.
            await self.send({"id": request_id, "method": "cancel"})
            raise
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()


class RpcHostJudge:
    backend = "host"

    def __init__(self, channel: RpcChannel, *, soft_budget: float = 3.0):
        self.channel = channel
        self.soft_budget = soft_budget

    async def judge(self, state: Any, questions: Mapping[str, Question], *, deadline=None):
        limit = asyncio.get_running_loop().time() + self.soft_budget
        if deadline is not None:
            limit = min(limit, deadline)
        if limit <= asyncio.get_running_loop().time():
            raise JudgeError("timeout", posts=0)
        try:
            result = await self.channel.request("judge", {
                "state": state, "questions": {key: q.to_wire() for key, q in questions.items()},
            }, deadline=limit)
            return parse_response(result, questions, self.backend)
        except asyncio.CancelledError:
            raise JudgeError("cancelled", posts=1) from None
        except TimeoutError:
            raise JudgeError("timeout", posts=1) from None
        except RpcError as error:
            raise JudgeError(error.reason, posts=int(error.reason != "unavailable")) from None
        except (ValueError, TypeError, KeyError):
            raise JudgeError("malformed_response", posts=1) from None


class RpcHostSearch:
    def __init__(self, channel: RpcChannel):
        self.channel = channel
        self.inventory: list[SourceDescriptor] = []
        self.descriptor = SourceDescriptor(
            id="host:web", kind=Kind.HOST, transport=Transport.HOST,
            upstream_family="host-web:opaque", privacy_class=PrivacyClass.PUBLIC,
            provision=Provision.AUTHORIZED, health=Health.HEALTHY,
            profile=SourceProfile(covers=("public web",), best_for=("host configured search chain",)),
            capabilities=Capabilities(search=True, returns_citations=True),
        )

    async def search(self, query: str, count: int, *, deadline: float) -> list[SearchResult]:
        try:
            payload = await self.channel.request("host_search", {"query": query, "count": count}, deadline=deadline)
        except TimeoutError:
            raise AdapterError(RunStatus.TIMEOUT) from None
        except RpcError as error:
            statuses = {"timeout": RunStatus.TIMEOUT, "cancelled": RunStatus.CANCELLED,
                        "authentication": RunStatus.AUTH_ERROR}
            raise AdapterError(statuses.get(error.reason, RunStatus.ERROR)) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise AdapterError(RunStatus.SCHEMA_ERROR)
        rows = []
        for row in payload["results"][:count]:
            if (not isinstance(row, dict) or not isinstance(row.get("url"), str)
                    or not row["url"] or not isinstance(row.get("title"), str)
                    or not isinstance(row.get("snippet", ""), str)):
                raise AdapterError(RunStatus.SCHEMA_ERROR)
            rows.append(SearchResult(title=row["title"], url=row["url"],
                                     snippet=row.get("snippet", ""), source_tag="host:web"))
        provider = payload.get("provider")
        family = provider if isinstance(provider, str) and _PROVIDER.fullmatch(provider) and provider != "none" else "host-web:opaque"
        self.descriptor = replace(self.descriptor, upstream_family=family)
        # inventory is the same live list consumed by fuse, not a stale snapshot.
        for index, source in enumerate(self.inventory):
            if source.id == "host:web":
                self.inventory[index] = self.descriptor
        return rows


def _request(message: dict) -> dict:
    query = message.get("query")
    count = message.get("count", 8)
    deadline = message.get("deadline_ms", 15000)
    sources = message.get("sources")
    host = message.get("host", {})
    if (not isinstance(query, str) or not query.strip()
            or type(count) is not int or not 1 <= count <= 20
            or type(deadline) is not int or not 1 <= deadline <= 300000
            or not isinstance(host, dict)
            or any(type(host.get(key, False)) is not bool for key in ("judge", "search"))
            or (sources is not None and (not isinstance(sources, list) or not sources
                or any(not isinstance(s, str) or not s for s in sources)))):
        raise ValueError("invalid search request")
    if len(query) > 4096:
        raise ValueError("query_too_long")
    return {"query": query, "count": count, "deadline_s": deadline / 1000,
            "sources": sources[:MAX_SOURCES] if sources is not None else None, "host": host,
            "dropped_sources": sources[MAX_SOURCES:] if sources is not None else []}


async def serve_rpc(reader, emit: Callable, *, inventory_factory: Callable, env: Mapping[str, str]) -> int:
    """Serve one search; ``reader.readline`` and ``emit(dict)`` are injectable seams."""
    channel = RpcChannel(reader, emit)
    try:
        request = _request(await _read(reader))
        host = request.pop("host")
        dropped_sources = request.pop("dropped_sources")
        host_search = RpcHostSearch(channel) if host.get("search") else None
        inventory, adapters = inventory_factory([host_search.descriptor] if host_search else [])
        if host_search:
            host_search.inventory = inventory
            adapters = {**adapters, "host:web": host_search}
        judge = resolve_judge(host_judge=RpcHostJudge(channel) if host.get("judge") else None, env=env)
    except ValueError as error:
        reason = "query_too_long" if str(error) == "query_too_long" else "invalid_request"
        await channel.send({"error": {"reason": reason}})
        return 2
    except (EOFError, OSError, UnicodeError):
        await channel.send({"error": {"reason": "invalid_request"}})
        return 2
    listener = asyncio.create_task(channel.listen())
    operation = asyncio.create_task(search(**request, inventory=inventory, adapters=adapters, judge=judge))
    cancellation = asyncio.create_task(channel.cancelled.wait())
    try:
        done, _ = await asyncio.wait((operation, cancellation), return_when=asyncio.FIRST_COMPLETED)
        if cancellation in done and not operation.done():
            operation.cancel()
        evidence = await operation
        for source in dropped_sources:
            if source not in evidence.plan.sources:
                evidence.plan.reasons[source] = "dropped:max_sources"
        await channel.send({"result": evidence.to_dict()})
        return 130 if evidence.quality.executed == "cancelled" else int(evidence.quality.executed == "failed")
    except ValueError:
        await channel.send({"error": {"reason": "invalid_request"}})
        return 2
    finally:
        listener.cancel()
        cancellation.cancel()
        for task in (listener, cancellation):
            with suppress(asyncio.CancelledError):
                await task


async def stdio_rpc(*, inventory_factory: Callable, env: Mapping[str, str]) -> int:
    import sys
    if callable(getattr(sys.stdout, "reconfigure", None)):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )
    def emit(message):
        sys.stdout.write(json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    try:
        return await serve_rpc(reader, emit, inventory_factory=inventory_factory, env=env)
    finally:
        transport.close()
