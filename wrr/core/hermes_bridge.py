"""Synchronous Hermes boundary over the shared core Evidence search path.

Hermes can dispatch tools inside an existing asyncio loop. In that case the
complete search (including runtime setup and async clients) belongs to a fresh
worker loop, not a nested run or a client shared across loops.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from .runtime import search_evidence


def search_sync(query: str, *, count: int = 8, runtime: str = "hermes") -> dict:
    """Return serialized Evidence; the backend owns the Hermes-specific envelope."""
    def run() -> dict:
        return asyncio.run(search_evidence(query, count=count, runtime=runtime)).to_dict()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()
    context = copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="wrr-hermes") as worker:
        return worker.submit(context.run, run).result()
