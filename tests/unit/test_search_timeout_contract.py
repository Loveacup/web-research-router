"""Request cancellation and effective timeout diagnostics (no network)."""
import asyncio

import pytest

from conftest import FakeEngine
from wrr.engines import community
from wrr.registry import EngineRegistry
from wrr.router import _run_engine
from wrr.schemas import SearchOptions


def test_community_outer_cancellation_kills_and_reaps(monkeypatch):
    events = []

    async def scenario():
        entered = asyncio.Event()

        class Process:
            returncode = None

            async def communicate(self):
                entered.set()
                await asyncio.Event().wait()

            def kill(self):
                events.append("kill")

            async def wait(self):
                events.append("wait")
                return -9

        async def spawn(*args, **kwargs):
            return Process()

        monkeypatch.setattr(community.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(community._run_cmd(["fake"], 20))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert events == ["kill", "wait"]


def test_timeout_event_reports_effective_dispatch_budget():
    class SlowEngine(FakeEngine):
        @property
        def timeout(self):
            return 20

        async def search(self, options):
            await asyncio.Event().wait()

    registry = EngineRegistry()
    registry.register(SlowEngine("community"))
    _, results, step, event = asyncio.run(
        _run_engine(registry, "community", SearchOptions("test"), 0.1))
    assert results is None and step.error == "timeout"
    assert event.timeout_ms == 100
