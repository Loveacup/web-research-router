from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from wrr.engines.community_sources import OpenCliSourceAdapter, SourceOutcome
from wrr.engines.configured_sources import (
    isolated_environment,
    load_source_config,
    run_isolated_command,
)
from wrr.errors import EngineError
from wrr.schemas import SearchOptions


_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "opencli_catalog.py"


def _write_config(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def _source_config(command: list[str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "sources": {
            "catalog-opencli": {
                "kind": "opencli",
                "command": command,
                "shared_env_keys": ["FIXTURE_SHARED_SENTINEL"],
                "title": "title",
                "url": "url",
                "snippet": "snippet",
            },
            "catalog-mcp": {
                "kind": "mcp",
                "command": [sys.executable, str(_FIXTURE)],
                "tool": "catalog_search",
                "arguments": {"catalog": "public"},
            },
        },
        "default_sources": ["catalog-opencli"],
        "authorized_sources": ["catalog-opencli"],
    }


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_for_file(path: Path, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"fixture did not create {path.name}")
        await asyncio.sleep(0.01)


async def _wait_for_exit(pid: int, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _pid_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _emergency_kill(*pids: int) -> None:
    """Test hygiene only; assertions happen before this fallback is reached."""
    for pid in pids:
        if pid and _pid_exists(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_unset_config_performs_no_discovery_or_process_start(monkeypatch):
    monkeypatch.delenv("WRR_COMMUNITY_CONFIG", raising=False)

    def unexpected_open(*_args, **_kwargs):
        raise AssertionError("unset configuration must not access a candidate path")

    monkeypatch.setattr(os, "open", unexpected_open)
    assert load_source_config({"reddit", "twitter"}) == ({}, None)


def test_explicit_config_separates_discovery_defaults_and_authority(tmp_path, monkeypatch):
    event_file = tmp_path / "must-not-start.jsonl"
    config_path = _write_config(
        tmp_path / "sources.json",
        _source_config(
            [sys.executable, str(_FIXTURE), "--event-file", str(event_file)]
        ),
    )
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(config_path))

    sources, defaults = load_source_config({"reddit", "twitter"})

    assert defaults == ("catalog-opencli",)
    assert sources["catalog-opencli"]["authorized"] is True
    assert sources["catalog-mcp"]["authorized"] is False
    assert sources["catalog-opencli"]["cli"][0] == sys.executable
    assert not event_file.exists()


@pytest.mark.parametrize(
    "change, expected_reason",
    [
        (lambda doc: doc.update(extra="not-allowed"), "unknown fields"),
        (
            lambda doc: doc["authorized_sources"].append("missing-source"),
            "unknown ID",
        ),
        (
            lambda doc: doc["sources"].update(
                reddit={"kind": "opencli", "command": [sys.executable]}
            ),
            "reserved",
        ),
        (
            lambda doc: doc["sources"]["catalog-opencli"].update(
                kind="unknown"
            ),
            "unsupported",
        ),
    ],
)
def test_configuration_rejects_unknown_authority_and_schema(
    tmp_path, monkeypatch, change, expected_reason
):
    document = _source_config([sys.executable, str(_FIXTURE)])
    change(document)
    config_path = _write_config(tmp_path / "sources.json", document)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(config_path))

    with pytest.raises(EngineError, match=expected_reason):
        load_source_config({"reddit"})


def test_configuration_requires_private_regular_owned_path(tmp_path, monkeypatch):
    target = _write_config(
        tmp_path / "sources.json",
        _source_config([sys.executable, str(_FIXTURE)]),
    )
    target.chmod(0o620)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(target))
    with pytest.raises(EngineError, match="permissions are unsafe"):
        load_source_config(set())

    target.chmod(0o600)
    link = tmp_path / "sources-link.json"
    link.symlink_to(target)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(link))
    with pytest.raises(EngineError, match="symbolic link"):
        load_source_config(set())


def test_configuration_errors_do_not_echo_file_content(tmp_path, monkeypatch):
    private_sentinel = "PRIVATE-CONFIG-CONTENT-9471"
    config_path = tmp_path / "invalid.json"
    config_path.write_text('{"' + private_sentinel, encoding="utf-8")
    config_path.chmod(0o600)
    monkeypatch.setenv("WRR_COMMUNITY_CONFIG", str(config_path))

    with pytest.raises(EngineError) as captured:
        load_source_config(set())
    assert private_sentinel not in str(captured.value)
    assert str(config_path) not in str(captured.value)


def test_isolated_environment_allows_only_explicit_noncontrol_values(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_SHARED_SENTINEL", "shared-ok")
    monkeypatch.setenv("WRR_FIXTURE_PRIVATE_SENTINEL", "private-value")
    monkeypatch.setenv("PYTHONPATH", "/private/import/path")
    monkeypatch.setenv("SHELL", "/private/shell")
    home = tmp_path / "empty-home"

    env = isolated_environment(["FIXTURE_SHARED_SENTINEL"], home)

    assert env["HOME"] == str(home)
    assert env["PATH"] == os.defpath
    assert env["FIXTURE_SHARED_SENTINEL"] == "shared-ok"
    assert "WRR_FIXTURE_PRIVATE_SENTINEL" not in env
    assert "PYTHONPATH" not in env
    assert "SHELL" not in env
    with pytest.raises(EngineError, match="process-control"):
        isolated_environment(["GIT_CONFIG_SYSTEM"], home)


def test_real_opencli_adapter_observes_isolation_and_reaps_normal_child(
    tmp_path, monkeypatch
):
    environment_file = tmp_path / "environment.json"
    child_pid_file = tmp_path / "child.pid"
    cfg = {
        "cli": [
            sys.executable,
            str(_FIXTURE),
            "--environment-file",
            str(environment_file),
            "--spawn-child",
            "--child-pid-file",
            str(child_pid_file),
        ]
    }
    monkeypatch.setenv("FIXTURE_SHARED_SENTINEL", "shared-ok")
    monkeypatch.setenv("WRR_FIXTURE_PRIVATE_SENTINEL", "must-not-cross")

    async def scenario():
        async def run_cmd(cli, timeout):
            return await run_isolated_command(
                cli, timeout, shared_env_keys=["FIXTURE_SHARED_SENTINEL"]
            )

        result = await OpenCliSourceAdapter().fetch_result(
            cfg, SearchOptions("Mandarin", count=1), run_cmd, 3.0
        )
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        observation = json.loads(environment_file.read_text(encoding="utf-8"))
        cleaned = await _wait_for_exit(child_pid)
        try:
            assert result.outcome is SourceOutcome.READY
            assert result.items == [
                {
                    "snippet": "Citrus catalog entry",
                    "title": "Mandarin Guide",
                    "url": "https://fixture.invalid/mandarin",
                }
            ]
            assert observation["shared_present"] is True
            assert observation["private_present"] is False
            assert observation["pythonpath_present"] is False
            assert observation["shell_present"] is False
            assert not Path(observation["home"]).exists()
            assert not Path(observation["cwd"]).exists()
            assert cleaned, "runner returned before its normal-exit descendant was gone"
        finally:
            _emergency_kill(child_pid)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fixture_flag, expected_outcome",
    [
        ("--nonzero", SourceOutcome.ERROR),
        ("--malformed", SourceOutcome.SCHEMA_ERROR),
        ("--auth-required", SourceOutcome.UNAUTHENTICATED),
    ],
)
def test_real_opencli_failures_are_typed(fixture_flag, expected_outcome):
    cfg = {"cli": [sys.executable, str(_FIXTURE), fixture_flag]}

    async def scenario():
        result = await OpenCliSourceAdapter().fetch_result(
            cfg, SearchOptions("Mandarin", count=1), run_isolated_command, 3.0
        )
        assert result.items == []
        assert result.outcome is expected_outcome

    asyncio.run(scenario())


def test_timeout_returns_stable_reason_and_reaps_process_tree(tmp_path):
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    command = [
        sys.executable,
        str(_FIXTURE),
        "--pending",
        "--spawn-child",
        "--pid-file",
        str(parent_pid_file),
        "--child-pid-file",
        str(child_pid_file),
        "Mandarin",
        "-f",
        "json",
        "--limit",
        "1",
    ]

    async def scenario():
        rc, stdout, stderr = await run_isolated_command(command, 1.0)
        parent_pid = int(parent_pid_file.read_text(encoding="utf-8"))
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        parent_cleaned = await _wait_for_exit(parent_pid)
        child_cleaned = await _wait_for_exit(child_pid)
        try:
            assert (rc, stdout, stderr) == (
                None,
                "",
                "configured source timed out",
            )
            assert parent_cleaned, "timeout returned before fixture parent was gone"
            assert child_cleaned, "timeout returned before fixture child was gone"
        finally:
            _emergency_kill(parent_pid, child_pid)

    asyncio.run(scenario())


def test_cancellation_propagates_after_reaping_process_tree(tmp_path):
    parent_pid_file = tmp_path / "parent.pid"
    child_pid_file = tmp_path / "child.pid"
    event_file = tmp_path / "events.jsonl"
    command = [
        sys.executable,
        str(_FIXTURE),
        "--pending",
        "--spawn-child",
        "--pid-file",
        str(parent_pid_file),
        "--child-pid-file",
        str(child_pid_file),
        "--event-file",
        str(event_file),
        "Mandarin",
        "-f",
        "json",
        "--limit",
        "1",
    ]

    async def scenario():
        task = asyncio.create_task(run_isolated_command(command, 30.0))
        await _wait_for_file(parent_pid_file)
        await _wait_for_file(child_pid_file)
        await _wait_for_file(event_file)
        parent_pid = int(parent_pid_file.read_text(encoding="utf-8"))
        child_pid = int(child_pid_file.read_text(encoding="utf-8"))
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            parent_cleaned = await _wait_for_exit(parent_pid)
            child_cleaned = await _wait_for_exit(child_pid)
            assert parent_cleaned, "cancellation propagated before parent cleanup"
            assert child_cleaned, "cancellation propagated before child cleanup"
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            _emergency_kill(parent_pid, child_pid)

    asyncio.run(scenario())
