"""Offline contracts for trusted declarative manifests and bounded execution."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time

import httpx
import pytest

from wrr.core.exec_declarative import (
    DeclarativeError, DeclarativeSpec, json_path, run_declarative,
)


def parsed(block):
    spec = DeclarativeSpec.parse(block)
    assert isinstance(spec, DeclarativeSpec), spec
    return spec


def cli_spec(tmp_path, code, *, output="json", results=None, **limits):
    script = tmp_path / "source.py"
    script.write_text(code)
    return parsed({
        "mode": "cli",
        "cli": {"command": [sys.executable, str(script), "{query}", "{count}"], "output": output},
        "results": results or {"root": "$.data.items", "title": "$.title", "url": "$.url", "snippet": "$.desc", "time": "$.pubdate"},
        **limits,
    })


def run(spec, query="asyncio; $(echo unsafe)", count=2, **kwargs):
    return asyncio.run(run_declarative(spec, query, count, env=kwargs.pop("env", {}), deadline=kwargs.pop("deadline", time.monotonic() + 5), **kwargs))


def test_cli_json_mapping_literal_query_count_and_time(tmp_path):
    spec = cli_spec(tmp_path, '''import json, sys
print(json.dumps({"data":{"items":[{"title":sys.argv[1],"url":"https://example.test/1","desc":sys.argv[2],"pubdate":1700000000},{"title":"second","url":"https://example.test/2"},{"title":"third","url":"https://example.test/3"}]}}))
''')
    rows = run(spec)
    assert len(rows) == 2
    assert rows[0].to_dict() == {
        "title": "asyncio; $(echo unsafe)", "url": "https://example.test/1", "snippet": "2", "highlights": [], "source_tag": "", "source_ts": 1700000000.0, "freshness_score": 0.5,
    }
    assert rows[1].snippet == "" and rows[1].source_ts == 0


def test_cli_ndjson_and_template_encodes_fields(tmp_path):
    spec = cli_spec(tmp_path, '''import json
print(json.dumps({"title":"one","bvid":"BV /?"}))
print(json.dumps({"title":"two","bvid":"BV2"}))
''', output="ndjson", results={"root": "$", "title": "$.title", "url_template": "https://www.bilibili.com/video/{bvid}"})
    rows = run(spec)
    assert [row.url for row in rows] == ["https://www.bilibili.com/video/BV%20%2F%3F", "https://www.bilibili.com/video/BV2"]


@pytest.mark.parametrize("shared_env_keys,expected", [(None, None), ([], None), (["TOKEN"], "allowed")])
def test_cli_environment_isolation(tmp_path, shared_env_keys, expected):
    spec = cli_spec(tmp_path, '''import json, os
print(json.dumps({"data":{"items":[{"title":os.getcwd(),"url":"https://example.test","desc":json.dumps({k:os.environ.get(k) for k in ["TOKEN", "OTHER_SECRET", "PYTHONPATH", "HOME", "PATH"]})}]}}))
''', **({"shared_env_keys": shared_env_keys} if shared_env_keys is not None else {}))
    rows = run(spec, env={"TOKEN": "allowed", "OTHER_SECRET": "private", "PYTHONPATH": "/unsafe", "HOME": "/unsafe", "PATH": "/unsafe"})
    child = json.loads(rows[0].snippet)
    assert child["TOKEN"] == expected
    assert child["OTHER_SECRET"] is None
    assert child["PYTHONPATH"] is None
    assert child["HOME"] != "/unsafe" and child["PATH"] == os.defpath
    assert rows[0].title != str(Path.cwd())
    assert not Path(rows[0].title).exists()


def test_cli_option_query_rejected_before_spawn(tmp_path, monkeypatch):
    spec = cli_spec(tmp_path, "print('{}')")
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(DeclarativeError) as error:
        run(spec, query="--help")
    assert error.value.status == "schema_error"
    assert spawned == []
    assert spec.warnings


def test_cli_option_query_after_literal_separator_executes(tmp_path):
    spec = cli_spec(tmp_path, '''import json, sys
print(json.dumps({"data":{"items":[{"title":sys.argv[2],"url":"https://example.test"}]}}))
''')
    spec.cli["command"].insert(2, "--")
    spec = parsed({"mode": "cli", "cli": spec.cli, "results": spec.results})
    assert spec.warnings == ()
    assert run(spec, query="--help")[0].title == "--help"


@pytest.mark.parametrize("keys", ["TOKEN", ["PYTHONPATH"], ["HOME"], ["BAD-NAME"], ["TOKEN", "TOKEN"], [1]])
def test_cli_shared_env_keys_reject_invalid_names(keys):
    block = {"mode": "cli", "cli": {"command": ["echo", "--", "{query}"]},
             "results": {"root": "$", "title": "$.title", "url": "$.url"}, "shared_env_keys": keys}
    assert isinstance(DeclarativeSpec.parse(block), list)


def test_cli_timeout_kills_even_sigterm_ignoring_child(tmp_path):
    marker = tmp_path / "pid"
    spec = cli_spec(tmp_path, f'''import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(marker)!r}, "w").write(str(os.getpid()))
time.sleep(30)
''', timeout_s=0.4)
    with pytest.raises(DeclarativeError) as error:
        run(spec)
    assert error.value.status == "timeout"
    assert marker.exists()
    pid = int(marker.read_text())
    # A reparented child may briefly remain a zombie; it cannot execute anymore.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    import subprocess
    state = subprocess.check_output(["/bin/ps", "-o", "stat=", "-p", str(pid)], text=True).strip()
    assert state.startswith("Z")


def test_cli_cancellation_kills_child(tmp_path):
    marker = tmp_path / "pid"
    spec = cli_spec(tmp_path, f'''import os, time
open({str(marker)!r}, "w").write(str(os.getpid()))
time.sleep(30)
''')

    async def scenario():
        task = asyncio.create_task(run_declarative(spec, "q", 1, env={}, deadline=time.monotonic() + 5))
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(0.01)
        assert marker.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


@pytest.mark.parametrize("code", ["print('not JSON')", "print('{}')", "print('{\"data\":{\"items\":[{\"title\":\"x\"}]}}')"])
def test_bad_cli_payload_is_schema_error(tmp_path, code):
    with pytest.raises(DeclarativeError) as error:
        run(cli_spec(tmp_path, code))
    assert error.value.status == "schema_error"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_cli_output_limit_stops_stream_without_retaining_excess(tmp_path, stream):
    spec = cli_spec(tmp_path, f'''import sys, time
sys.{stream}.write("x" * 2000000)
sys.{stream}.flush()
time.sleep(30)
''', max_output_bytes=100)
    start = time.monotonic()
    with pytest.raises(DeclarativeError) as error:
        run(spec)
    assert error.value.status == "schema_error"
    assert error.value.reason == "output_limit"
    assert time.monotonic() - start < 3


def test_expired_deadline_never_starts_cli(tmp_path):
    marker = tmp_path / "ran"
    spec = cli_spec(tmp_path, f"open({str(marker)!r}, 'w').write('ran')")
    with pytest.raises(DeclarativeError) as error:
        run(spec, deadline=time.monotonic() - 1)
    assert error.value.status == "timeout" and not marker.exists()


@pytest.mark.parametrize("path,expected", [("$", {"a": {"b": [{"c": 7}]}}), ("$.a.b[0].c", 7), ("$.a.b[2]", None), ("$.missing", None), ("$.a.b.c", None)])
def test_json_path_subset(path, expected):
    assert json_path({"a": {"b": [{"c": 7}]}}, path) == expected


@pytest.mark.parametrize("path", ["a", "$..a", "$.a[*]", "$.a[-1]", "$.a[", "$.a.", "$[01]", "$.a[1:2]"])
def test_json_path_rejects_unsupported_syntax(path):
    with pytest.raises(ValueError):
        json_path({}, path)


@pytest.mark.parametrize("change", [
    {"mode": "shell"}, {"timeout_s": True}, {"timeout_s": float("inf")}, {"max_output_bytes": 0},
    {"cli": {"command": ["{query}"]}}, {"cli": {"command": ["echo", "--q={query}"]}},
    {"cli": {"command": ["echo", "{other}"]}}, {"cli": {"command": ["./echo", "{query}"]}},
    {"results": {"root": "$..items", "title": "$.title", "url": "$.url"}},
    {"results": {"root": "$", "title": "$.title", "url_template": "https://{host}/x"}},
])
def test_manifest_parse_reports_errors(change):
    block = {"mode": "cli", "cli": {"command": ["echo", "{query}"]}, "results": {"root": "$", "title": "$.title", "url": "$.url"}}
    block.update(change)
    assert isinstance(DeclarativeSpec.parse(block), list)


def http_spec(method="GET", **limits):
    return parsed({"mode": "http", "http": {"url": "https://api.example.test/search", "method": method, "query_param": "q", "count_param": "n", "headers_env": {"Authorization": "TOKEN"}, "body": {"fixed": "value"}}, "results": {"root": "$.items", "title": "$.title", "url": "$.url", "time": "$.time"}, **limits})


def mock_http(monkeypatch, handler):
    client = httpx.AsyncClient
    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr("wrr.core.exec_declarative.httpx.AsyncClient", factory)


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_http_single_request_mapping_auth_and_parameters(monkeypatch, method):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer test"
        if method == "GET":
            assert dict(request.url.params) == {"q": "literal & query", "n": "2"}
        else:
            assert json.loads(request.content) == {"fixed": "value", "q": "literal & query", "n": 2}
        return httpx.Response(200, json={"items": [{"title": "first", "url": "https://example.test/1", "time": "2023-11-14T22:13:20Z"}]})
    mock_http(monkeypatch, handler)
    rows = run(http_spec(method), query="literal & query", env={"TOKEN": "Bearer test"})
    assert len(seen) == 1 and rows[0].source_ts == 1700000000


@pytest.mark.parametrize("status,expected", [(302, "schema_error"), (401, "auth_error"), (403, "auth_error"), (429, "rate_limit"), (500, "error")])
def test_http_errors_and_redirects_never_retried(monkeypatch, status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://elsewhere.test"})
    mock_http(monkeypatch, handler)
    with pytest.raises(DeclarativeError) as error:
        run(http_spec(), env={"TOKEN": "x"})
    assert error.value.status == expected and len(calls) == 1


def test_http_limits_malformed_and_missing_credentials(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="x" * 1000)
    mock_http(monkeypatch, handler)
    with pytest.raises(DeclarativeError) as error:
        run(http_spec(max_output_bytes=100), env={"TOKEN": "x"})
    assert error.value.reason == "output_limit"
    with pytest.raises(DeclarativeError) as error:
        run(http_spec(), env={"TOKEN": "x"})
    assert error.value.status == "schema_error"
    with pytest.raises(DeclarativeError) as error:
        run(http_spec())
    assert error.value.status == "auth_error" and len(calls) == 2


def test_http_timeout_and_cancellation(monkeypatch):
    async def handler(request):
        await asyncio.sleep(30)
    mock_http(monkeypatch, handler)
    with pytest.raises(DeclarativeError) as error:
        run(http_spec(timeout_s=0.01), env={"TOKEN": "x"})
    assert error.value.status == "timeout"

    async def scenario():
        task = asyncio.create_task(run_declarative(http_spec(), "q", 1, env={"TOKEN": "x"}, deadline=time.monotonic() + 2))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())


def test_mcp_refuses_unresolved_named_server():
    spec = parsed({"mode": "mcp", "mcp": {"server": "trusted-name", "tool": "search", "query_arg": "query", "count_arg": "limit"}, "results": {"root": "$", "title": "$.title", "url": "$.url"}})
    with pytest.raises(NotImplementedError, match="trusted server-name"):
        run(spec)


def test_bili_example_manifest_is_valid_and_maps_observed_output():
    manifest = json.loads(Path("wrr/engines/examples/bili/engine.yaml").read_text())
    spec = parsed(manifest["declarative"])
    assert spec.cli["command"] == ["bili", "search", "{query}", "--type", "video", "-n", "{count}", "--json"]
    assert spec.results["root"] == "$.data"
    assert spec.results["url_template"] == "https://www.bilibili.com/video/{bvid}"
