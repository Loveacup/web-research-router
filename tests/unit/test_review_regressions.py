"""Regression contracts for the six scoped review findings."""
import io
import time
import urllib.request

import pytest

from wrr.engines.mcp_source import _McpSchemaFailure, _validate_input_schema


@pytest.mark.parametrize("reference", [
    "http://127.0.0.1:9/schema", "//127.0.0.1:9/schema", "child.json",
    "file:///nonexistent/schema.json",
])
def test_schema_reference_never_fetches(monkeypatch, reference):
    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        return io.BytesIO(b'{"type": "string"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    schema = {"type": "object", "$id": "http://127.0.0.1:9/root",
              "properties": {"query": {"$ref": reference}}}
    started = time.monotonic()
    rejected = False
    try:
        _validate_input_schema(schema, {"query": "hello"})
    except _McpSchemaFailure:
        rejected = True
    assert calls == [], "untrusted schema must not enter synchronous URL retrieval"
    assert rejected
    assert time.monotonic() - started < 1.0


def test_schema_nested_base_does_not_enable_retrieval(monkeypatch):
    calls = []

    def fake_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        return io.BytesIO(b'{"type": "string"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    schema = {"type": "object", "$defs": {"nested": {
        "$id": "http://127.0.0.1:9/nested/", "$ref": "child.json"}},
        "properties": {"query": {"$ref": "#/$defs/nested"}}}
    with pytest.raises(_McpSchemaFailure):
        _validate_input_schema(schema, {"query": "hello"})
    assert calls == []


def test_schema_local_fragment_keeps_validation():
    schema = {"type": "object", "$id": "https://schema.invalid/root",
              "$defs": {"query": {"type": "string"}},
              "properties": {"query": {"$ref": "#/$defs/query"}}}
    _validate_input_schema(schema, {"query": "hello"})
    with pytest.raises(_McpSchemaFailure):
        _validate_input_schema(schema, {"query": 1})


async def _wait_for_flood_pid(pid_file, task, timeout=1.5):
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        assert not task.done(), "flood command ended before PID readiness"
        try:
            pid = int(pid_file.read_text())
        except (FileNotFoundError, ValueError):
            pid = None
        if pid is not None and pid > 0:
            return pid
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "flood PID readiness timed out"
        await asyncio.sleep(min(0.01, remaining))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_cleanup_drains_flood_without_losing_original_outcome(tmp_path, monkeypatch, cancel):
    import asyncio
    import os
    import sys
    from wrr.engines.configured_sources import run_isolated_command

    processes = []
    spawn = asyncio.create_subprocess_exec

    async def capture_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    pid_file = tmp_path / "pid"
    code = (
        "import os,signal; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "exec('while True:\\n os.write(1,b\"x\"*65536)\\n os.write(2,b\"y\"*65536)')"
    )
    task = asyncio.create_task(run_isolated_command([sys.executable, "-c", code], 2 if not cancel else 10))
    try:
        pid = await _wait_for_flood_pid(pid_file, task)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == (None, "", "configured source timed out")
        assert processes[0].returncode is not None
        assert processes[0].stdout.at_eof()
        assert processes[0].stderr.at_eof()
    finally:
        # Readiness and cancellation failures must also settle the task and pipes.
        if not task.done():
            task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
        for process in processes:
            await asyncio.wait_for(process.communicate(), 2)
    assert pid_file.exists()
    assert int(pid_file.read_text()) == pid
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("owned child still exists after cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("initial_content", [None, "", "not-a-pid"])
async def test_flood_readiness_waits_for_complete_pid(tmp_path, monkeypatch, initial_content, cancel):
    import asyncio
    import os
    from types import SimpleNamespace
    from wrr.engines import configured_sources

    cleaned = []
    tasks = []
    pid_ready = asyncio.Event()
    readiness = _wait_for_flood_pid

    async def capture_readiness(pid_file, task):
        pid = await readiness(pid_file, task)
        pid_ready.set()
        return pid

    monkeypatch.setitem(globals(), "_wait_for_flood_pid", capture_readiness)
    process = SimpleNamespace(returncode=None)
    process.stdout = SimpleNamespace(at_eof=lambda: process.returncode is not None)
    process.stderr = SimpleNamespace(at_eof=lambda: process.returncode is not None)

    async def communicate():
        cleaned.append(True)
        return b"", b""

    process.communicate = communicate

    async def spawn(*args, **kwargs):
        return process

    async def publish_pid():
        if initial_content is not None:
            (tmp_path / "pid").write_text(initial_content)
        await asyncio.sleep(0.35)
        (tmp_path / "pid").write_text("12345")

    async def runner(argv, timeout):
        tasks.append(asyncio.current_task())
        await asyncio.create_subprocess_exec("fake-supervisor")
        try:
            # This fixture publishes before its timeout; wait_for can swallow a
            # concurrent cancellation as publish_pid completes on Python 3.11.
            await publish_pid()
            if not cancel:
                await asyncio.wait_for(pid_ready.wait(), 1)
                return None, "", "configured source timed out"
            await asyncio.Event().wait()
        finally:
            process.returncode = 0

    def gone(pid, signal):
        assert pid == 12345
        raise ProcessLookupError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(configured_sources, "run_isolated_command", runner)
    monkeypatch.setattr(os, "kill", gone)
    try:
        await test_cleanup_drains_flood_without_losing_original_outcome(tmp_path, monkeypatch, cancel)
        assert cleaned == [True]
        assert all(task.done() for task in tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("early_done", [False, True])
async def test_flood_startup_failure_settles_task_and_pipes(tmp_path, monkeypatch, cancel, early_done):
    import asyncio
    from types import SimpleNamespace
    from wrr.engines import configured_sources

    tasks = []
    cleaned = []
    process = SimpleNamespace(returncode=None)

    async def communicate():
        cleaned.append(process.returncode)
        return b"", b""

    process.communicate = communicate

    async def spawn(*args, **kwargs):
        return process

    async def runner(*args, **kwargs):
        tasks.append(asyncio.current_task())
        await asyncio.create_subprocess_exec("fake-supervisor")
        try:
            if early_done:
                return None, "", "configured source timed out"
            await asyncio.Event().wait()
        finally:
            process.returncode = 0

    readiness = _wait_for_flood_pid

    async def bounded_readiness(pid_file, task):
        return await readiness(pid_file, task, timeout=0.03)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(configured_sources, "run_isolated_command", runner)
    monkeypatch.setitem(globals(), "_wait_for_flood_pid", bounded_readiness)
    expected = "ended before PID readiness" if early_done else "PID readiness timed out"
    try:
        with pytest.raises(AssertionError, match=expected):
            await test_cleanup_drains_flood_without_losing_original_outcome(tmp_path, monkeypatch, cancel)
        assert all(task.done() for task in tasks), "startup failure left runner pending"
        assert cleaned == [0], "pipes were closed before runner cleanup finished"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("override", [False, True])
def test_cli_env_loaded_before_runtime_config(tmp_path, override):
    import json
    import os
    import subprocess
    import sys

    env_file = tmp_path / "explicit.env"
    env_file.write_text("WRR_GITHUB_TIMEOUT=0.7\nWRR_SKILL_TRUSTED_SOURCES=owner/custom\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("WRR_")}
    if override:
        env["WRR_GITHUB_TIMEOUT"] = "1.2"
    code = '''
import json, sys
from wrr import _cli
assert 'wrr.config' not in sys.modules
def inspect_config(ns):
    from wrr import config
    print(json.dumps([config.GITHUB_TIMEOUT, config.SKILL_TRUSTED_SOURCES]))
    return 0
_cli.cmd_search = inspect_config
assert _cli.main(['search', 'local-test', '--env', sys.argv[1], '--quiet']) == 0
'''
    result = subprocess.run([sys.executable, "-c", code, str(env_file)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [1.2 if override else 0.7, ["owner/custom"]]


def test_host_fetch_eligibility_help_discloses_dns():
    from wrr import _cli

    help_text = _cli.build_parser().format_help()
    assert "DNS" in help_text
    assert "离线判定 URL" not in help_text


@pytest.mark.parametrize("operation", [[], {}, True, False])
def test_malformed_committed_operation_is_stable_refusal(operation):
    from wrr.cli.release_supervisor import SupervisorRefusal, _validate_state

    state = {
        "schema_version": 1, "generation": 1, "last_transaction_id": "tx",
        "active_target": "/release", "committed_operation": operation,
        "committed_receipt_path": "/receipt.json", "committed_receipt_sha256": "a" * 64,
    }
    with pytest.raises(SupervisorRefusal, match="generation_state_invalid"):
        _validate_state(state)


