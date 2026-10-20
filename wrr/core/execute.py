"""Concurrent source execution under one absolute monotonic deadline."""
from __future__ import annotations

import asyncio
from typing import Mapping, Sequence, TYPE_CHECKING

from . import rubric
from .contracts import Plan, RunStatus, SourceDescriptor, SourceRun
from .query_style import to_keywords

if TYPE_CHECKING:
    from .sources import SourceAdapter


async def execute(query: str, plan: Plan, adapters: Mapping[str, SourceAdapter], *,
                  deadline: float, count: int, inventory: Sequence[SourceDescriptor] = (),
                  source_timeout_s: float = rubric.SOURCE_TIMEOUT_S) -> list[SourceRun]:
    """Cancel every child before returning cancelled runs to the caller."""
    loop = asyncio.get_running_loop()
    descriptors = {s.id: s for s in inventory}
    runs = [SourceRun(sid, RunStatus.CANCELLED, 0,
                      device=descriptors[sid].device if sid in descriptors else None)
            for sid in plan.sources]

    async def one(run: SourceRun) -> None:
        started = loop.time()
        try:
            adapter = adapters.get(run.source_id)
            if adapter is None:
                run.status = RunStatus.SCHEMA_ERROR
                run.error = "adapter_unavailable"
                return
            descriptor = descriptors.get(run.source_id)
            sent = to_keywords(query) if descriptor and descriptor.query_style == "keywords" else query
            run.query_sent = sent if sent != query else None
            limit = min(deadline, started + source_timeout_s)
            async with asyncio.timeout_at(limit):
                run.results = await adapter.search(sent, count, deadline=limit)
            run.status = RunStatus.OK if run.results else RunStatus.EMPTY
        except asyncio.CancelledError:
            run.status = RunStatus.CANCELLED
            run.results = []
            raise
        except TimeoutError:
            run.status = RunStatus.TIMEOUT
            run.error = "timeout"
        except Exception as error:
            # AdapterError owns the translation; never serialize raw exception
            # text, which may contain provider credentials or private queries.
            status = getattr(error, "status", RunStatus.ERROR)
            run.status = status if isinstance(status, RunStatus) else RunStatus.ERROR
            run.error = run.status.value
        finally:
            run.elapsed_ms = round((loop.time() - started) * 1000)

    tasks = [asyncio.create_task(one(run)) for run in runs]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for run in runs:
            run.status = RunStatus.CANCELLED
            run.results = []
            run.error = None
    return runs
