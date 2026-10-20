"""Request cancellation must terminate and reap community subprocesses."""
import asyncio

import pytest

from wrr.engines import community


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


