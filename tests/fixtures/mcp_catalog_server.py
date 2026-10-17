#!/usr/bin/env python3
"""Deterministic MCP 2.x stdio catalog fixture used by WRR acceptance tests."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations


_PRIVATE_SENTINEL = "PRIVATE_MCP_FIXTURE_SENTINEL"
_CATALOG = (
    {
        "title": "Mandarin Guide",
        "url": "https://fixture.invalid/mandarin",
        "snippet": "Citrus catalog entry",
    },
    {
        "title": "Cedar Guide",
        "url": "https://fixture.invalid/cedar",
        "snippet": "Evergreen catalog entry",
    },
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("success", "error", "malformed", "permission", "cancel"),
        default="success",
    )
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--tool", default="catalog_search")
    parser.add_argument("--text-json", action="store_true")
    parser.add_argument("--probe-env", action="append", default=[])
    return parser.parse_args()


ARGS = _parse_args()


def _record(event: str, **fields: Any) -> None:
    entry = {"event": event, "pid": os.getpid(), **fields}
    ARGS.events.parent.mkdir(parents=True, exist_ok=True)
    with ARGS.events.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
        stream.flush()


class _EventMiddleware:
    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if ctx.method == "initialize":
            _record("initialize")
        elif ctx.method == "tools/list":
            _record("list")
        elif ctx.method == "notifications/cancelled":
            _record("cancelled")
        return await call_next(ctx)


@asynccontextmanager
async def _lifespan(_server: MCPServer[Any]):
    cwd = Path.cwd()
    home = Path(os.environ.get("HOME", ""))
    _record(
        "start",
        isolated_home_and_cwd=(home.is_dir() and home.parent.samefile(cwd.parent)
                               and not home.samefile(cwd)),
        probed_env={key: key in os.environ for key in ARGS.probe_env},
    )
    try:
        yield {}
    finally:
        _record("stop")


server = MCPServer(
    "wrr-mcp-catalog-fixture",
    lifespan=_lifespan,
    middleware=[_EventMiddleware()],
    log_level="CRITICAL",
)


async def _stop_child(child: subprocess.Popen[bytes]) -> None:
    with anyio.CancelScope(shield=True):
        if child.poll() is None:
            child.terminate()
            try:
                await anyio.to_thread.run_sync(lambda: child.wait(timeout=2.0))
            except subprocess.TimeoutExpired:
                child.kill()
                await anyio.to_thread.run_sync(child.wait)
        _record("child_stopped", child_pid=child.pid, returncode=child.returncode)


async def _catalog_result(query: str, count: int) -> dict[str, Any]:
    _record("call", tool=ARGS.tool)
    if ARGS.mode == "error":
        raise RuntimeError(f"fixture failure: {_PRIVATE_SENTINEL}")
    if ARGS.mode == "malformed":
        return {"unexpected": _PRIVATE_SENTINEL}
    if ARGS.mode == "cancel":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _record("child_started", child_pid=child.pid)
        try:
            await anyio.sleep_forever()
        finally:
            await _stop_child(child)
        raise AssertionError("unreachable")

    needle = query.casefold()
    rows = [
        dict(row)
        for row in _CATALOG
        if needle in row["title"].casefold() or needle in row["snippet"].casefold()
    ][:count]
    return {"results": rows}


async def _catalog_text(query: str, count: int) -> str:
    return json.dumps(await _catalog_result(query, count))


server.add_tool(
    _catalog_text if ARGS.text_json else _catalog_result,
    name=ARGS.tool,
    description="Search the fixture's two-record local catalog.",
    annotations=ToolAnnotations(
        read_only_hint=ARGS.mode != "permission",
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
    structured_output=not ARGS.text_json,
)


if __name__ == "__main__":
    server.run("stdio")
