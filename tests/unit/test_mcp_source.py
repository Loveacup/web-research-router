"""Behavioral tests for the optional official-SDK MCP stdio adapter."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from wrr.engines.community_sources import SourceOutcome
from wrr.engines.mcp_source import McpSourceAdapter
from wrr.schemas import SearchOptions


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional official MCP SDK is not installed",
)

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tests" / "fixtures" / "mcp_catalog_server.py"
_PRIVATE_SENTINEL = "PRIVATE_MCP_FIXTURE_SENTINEL"


async def _unused_run_cmd(_cli: list[str], _timeout: float):
    raise AssertionError("McpSourceAdapter must delegate stdio lifecycle to the MCP SDK")


def _source_config(
    tmp_path: Path,
    mode: str,
    *,
    authorized: bool = True,
    configured_tool: str = "catalog_search",
    extra_server_args: tuple[str, ...] = (),
    **overrides: Any,
) -> tuple[dict[str, Any], Path]:
    events = tmp_path / f"{mode}-{configured_tool}.jsonl"
    cfg: dict[str, Any] = {
        "kind": "mcp",
        "authorized": authorized,
        "command": [
            sys.executable,
            str(SERVER),
            "--mode",
            mode,
            "--events",
            str(events),
            *extra_server_args,
        ],
        "tool": configured_tool,
        "query_arg": "query",
        "count_arg": "count",
        "arguments": {},
        "shared_env_keys": [],
    }
    cfg.update(overrides)
    return cfg, events


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    parsed = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            parsed.append(json.loads(line))
    return parsed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recorded_pids(records: list[dict[str, Any]]) -> set[int]:
    pids = {
        value
        for record in records
        for value in (record.get("pid"), record.get("child_pid"))
        if isinstance(value, int) and value > 1
    }
    return pids


def _kill_recorded(pids: set[int]) -> None:
    for pid in pids:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


async def _wait_for_event(path: Path, event: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        records = _events(path)
        if any(record.get("event") == event for record in records):
            return records
        await asyncio.sleep(0.02)
    raise AssertionError(f"fixture did not record {event!r}")


async def _wait_for_exit(pids: set[int], timeout: float) -> list[int]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        living = [pid for pid in pids if _alive(pid)]
        if not living:
            return []
        await asyncio.sleep(0.02)
    return [pid for pid in pids if _alive(pid)]


def test_unauthorized_source_never_starts_process(tmp_path: Path) -> None:
    cfg, events = _source_config(tmp_path, "success", authorized=False)

    result = asyncio.run(
        McpSourceAdapter().fetch_result(
            cfg,
            SearchOptions(query="Mandarin", count=1),
            _unused_run_cmd,
            4.0,
        )
    )

    assert result.outcome is SourceOutcome.PERMISSION_DENIED
    assert result.items == []
    assert not events.exists()


@pytest.mark.parametrize(
    ("configured_tool", "mode", "expected"),
    [
        ("catalog_search", "permission", SourceOutcome.PERMISSION_DENIED),
        ("absent_search", "success", SourceOutcome.SCHEMA_ERROR),
    ],
)
def test_tool_must_exist_exactly_once_and_be_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_tool: str,
    mode: str,
    expected: SourceOutcome,
) -> None:
    private_key = "WRR_MCP_UNAPPROVED_SENTINEL"
    monkeypatch.setenv(private_key, _PRIVATE_SENTINEL)
    cfg, events = _source_config(
        tmp_path,
        mode,
        configured_tool=configured_tool,
        extra_server_args=("--probe-env", private_key),
    )

    result = asyncio.run(
        McpSourceAdapter().fetch_result(
            cfg,
            SearchOptions(query="Mandarin", count=1),
            _unused_run_cmd,
            4.0,
        )
    )
    records = _events(events)
    pids = _recorded_pids(records)
    living = asyncio.run(_wait_for_exit(pids, 2.0))
    _kill_recorded(set(living))

    assert result.outcome is expected
    assert not any(record["event"] == "call" for record in records)
    start = next(record for record in records if record["event"] == "start")
    assert start["isolated_home_and_cwd"] is True
    assert start["probed_env"][private_key] is False
    assert any(record["event"] == "list" for record in records)
    assert any(record["event"] == "stop" for record in records)
    assert living == []


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("error", SourceOutcome.ERROR),
        ("malformed", SourceOutcome.SCHEMA_ERROR),
    ],
)
def test_tool_errors_and_malformed_results_are_safe(
    tmp_path: Path,
    mode: str,
    expected: SourceOutcome,
) -> None:
    cfg, events = _source_config(tmp_path, mode)

    result = asyncio.run(
        McpSourceAdapter().fetch_result(
            cfg,
            SearchOptions(query="Mandarin", count=1),
            _unused_run_cmd,
            4.0,
        )
    )
    records = _events(events)
    pids = _recorded_pids(records)
    living = asyncio.run(_wait_for_exit(pids, 2.0))
    _kill_recorded(set(living))

    assert result.outcome is expected
    assert result.items == []
    assert _PRIVATE_SENTINEL not in (result.detail or "")
    assert any(record["event"] == "call" for record in records)
    assert any(record["event"] == "stop" for record in records)
    assert living == []


def test_advertised_input_schema_is_checked_before_tool_call(tmp_path: Path) -> None:
    cfg, events = _source_config(
        tmp_path,
        "success",
        arguments={"count": "not-an-integer"},
        count_arg="limit",
    )

    result = asyncio.run(
        McpSourceAdapter().fetch_result(
            cfg,
            SearchOptions(query="Mandarin", count=1),
            _unused_run_cmd,
            4.0,
        )
    )
    records = _events(events)
    pids = _recorded_pids(records)
    living = asyncio.run(_wait_for_exit(pids, 2.0))
    _kill_recorded(set(living))

    assert result.outcome is SourceOutcome.SCHEMA_ERROR
    assert any(record["event"] == "list" for record in records)
    assert not any(record["event"] == "call" for record in records)
    assert living == []


def test_pending_tool_timeout_is_typed_and_reaps_owned_processes(tmp_path: Path) -> None:
    async def scenario():
        cfg, events = _source_config(tmp_path, "cancel")
        task = asyncio.create_task(
            McpSourceAdapter().fetch_result(
                cfg,
                SearchOptions(query="Mandarin", count=1),
                _unused_run_cmd,
                4.0,
            )
        )
        pids: set[int] = set()
        living: list[int] = []
        try:
            records = await _wait_for_event(events, "child_started")
            pids = _recorded_pids(records)
            result = await asyncio.wait_for(task, 8.0)
            living = await _wait_for_exit(pids, 8.0)
            return result, _events(events), living
        finally:
            try:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                pids.update(_recorded_pids(_events(events)))
                _kill_recorded(pids)

    result, records, living = asyncio.run(scenario())

    assert result.outcome is SourceOutcome.TIMEOUT
    assert any(record["event"] == "call" for record in records)
    assert any(record["event"] == "child_started" for record in records)
    assert living == []


def test_cancellation_propagates_and_reaps_server_and_owned_child(tmp_path: Path) -> None:
    async def scenario() -> tuple[list[dict[str, Any]], list[int]]:
        cfg, events = _source_config(tmp_path, "cancel")
        task = asyncio.create_task(
            McpSourceAdapter().fetch_result(
                cfg,
                SearchOptions(query="Mandarin", count=1),
                _unused_run_cmd,
                30.0,
            )
        )
        pids: set[int] = set()
        living: list[int] = []
        try:
            records = await _wait_for_event(events, "child_started")
            pids = _recorded_pids(records)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8.0)
            living = await _wait_for_exit(pids, 8.0)
            return _events(events), living
        finally:
            try:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            finally:
                # Cleanup is deliberately after observing and retaining
                # `living`; a product leak still fails the assertion below.
                pids.update(_recorded_pids(_events(events)))
                _kill_recorded(pids)

    records, living = asyncio.run(scenario())

    assert any(record["event"] == "call" for record in records)
    assert any(record["event"] == "child_started" for record in records)
    assert living == []
