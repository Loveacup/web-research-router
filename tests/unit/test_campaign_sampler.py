"""D7-S5 plugin-owned fixed campaign sampler contracts."""

import asyncio
import json
import math
import threading
import time
from pathlib import Path

import pytest
import yaml

from wrr.runtime.campaign_sampler import (
    CampaignSamplerConfig,
    parse_campaign_sampler_config,
    run_campaign_sampler,
    start_campaign_sampler,
)


def test_plugin_manifest_packages_sampler_as_disabled_by_default():
    manifest_path = Path(__file__).resolve().parents[2] / "plugin.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["config_schema"]["campaign_sampler"]["default"] == {
        "enabled": False,
    }


def test_campaign_sampler_is_strict_opt_in():
    assert parse_campaign_sampler_config(None) is None
    assert parse_campaign_sampler_config({}) is None
    assert parse_campaign_sampler_config({"enabled": False}) is None
    assert parse_campaign_sampler_config({"enabled": "true"}) is None

    config = parse_campaign_sampler_config({
        "enabled": True,
        "interval_sec": 1800,
    })
    assert config == CampaignSamplerConfig(interval_sec=1800.0)

    with pytest.raises(ValueError):
        parse_campaign_sampler_config({"enabled": True, "interval_sec": 1799})
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            parse_campaign_sampler_config({"enabled": True, "interval_sec": value})
    with pytest.raises(ValueError):
        parse_campaign_sampler_config({
            "enabled": True,
            "interval_sec": 1800,
            "unknown": True,
        })


def test_sampler_routes_through_bound_plugin_handler_until_controller_drains():
    class Controller:
        state = "active"

    controller = Controller()
    calls = []
    sleeps = []

    async def handler(args):
        calls.append(args)
        if len(calls) == 2:
            controller.state = "draining"
        return {"data": {"web": []}}

    async def sleep(seconds):
        sleeps.append(seconds)

    asyncio.run(run_campaign_sampler(
        handler,
        controller,
        campaign_id="d7-s5-grounding-001",
        config=CampaignSamplerConfig(interval_sec=1800.0),
        sleep=sleep,
    ))

    assert calls == [
        {
            "query": "official documentation current release notes",
            "mode": "grounding",
            "campaign_id": "d7-s5-grounding-001",
        },
        {
            "query": "official documentation security guidance",
            "mode": "grounding",
            "campaign_id": "d7-s5-grounding-001",
        },
    ]
    assert sleeps == [1800.0]


def test_sampler_stops_after_handler_failure_without_retry_storm():
    class Controller:
        state = "active"

        def __init__(self):
            self.faults = []

        def record_fault(self, reason):
            self.faults.append(reason)
            self.state = "disabled"

    controller = Controller()
    calls = []

    async def handler(args):
        calls.append(args)
        raise RuntimeError("bounded failure")

    async def sleep(_seconds):
        raise AssertionError("must not sleep or retry after a failed sample")

    asyncio.run(run_campaign_sampler(
        handler,
        controller,
        campaign_id="d7-s5-grounding-001",
        config=CampaignSamplerConfig(interval_sec=1800.0),
        sleep=sleep,
    ))

    assert len(calls) == 1
    assert controller.state == "disabled"
    assert controller.faults == ["sampler_search_failed"]


@pytest.mark.parametrize(
    "result",
    [
        {"error": "all engines failed"},
        {"success": False},
        {"details": {"quality": {"verdict": "failed"}}},
        json.dumps({"error": "all engines failed"}),
    ],
)
def test_sampler_stops_after_structured_failure_result(result):
    class Controller:
        state = "active"

        def __init__(self):
            self.faults = []

        def record_fault(self, reason):
            self.faults.append(reason)
            self.state = "disabled"

    controller = Controller()
    calls = []
    sleeps = []

    async def handler(args):
        calls.append(args)
        if len(calls) == 2:
            controller.state = "draining"
        return result

    async def sleep(seconds):
        sleeps.append(seconds)

    asyncio.run(run_campaign_sampler(
        handler,
        controller,
        campaign_id="d7-s5-grounding-001",
        config=CampaignSamplerConfig(interval_sec=1800.0),
        sleep=sleep,
    ))

    assert len(calls) == 1
    assert sleeps == []
    assert controller.state == "disabled"
    assert controller.faults == ["sampler_search_failed"]


def test_sampler_contains_sleep_failure_without_retry_storm():
    class Controller:
        state = "active"

        def __init__(self):
            self.faults = []

        def record_fault(self, reason):
            self.faults.append(reason)
            self.state = "disabled"

    controller = Controller()
    calls = []

    async def handler(args):
        calls.append(args)
        return {"data": {"web": []}}

    async def sleep(_seconds):
        raise RuntimeError("clock unavailable")

    asyncio.run(run_campaign_sampler(
        handler,
        controller,
        campaign_id="d7-s5-grounding-001",
        config=CampaignSamplerConfig(interval_sec=1800.0),
        sleep=sleep,
    ))

    assert len(calls) == 1
    assert controller.state == "disabled"
    assert controller.faults == ["sampler_sleep_failed"]


def test_thread_runner_cancels_an_inflight_handler_on_unload():
    class Controller:
        state = "active"

    started = threading.Event()
    cancelled = threading.Event()

    async def handler(_args):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    stop = start_campaign_sampler(
        handler,
        Controller(),
        campaign_id="d7-s5-grounding-001",
        config=CampaignSamplerConfig(interval_sec=1800.0),
    )
    assert started.wait(5)
    began = time.monotonic()
    stop()

    assert cancelled.wait(1)
    assert time.monotonic() - began < 2.0
