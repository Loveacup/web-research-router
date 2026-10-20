"""Trusted manifest-only JSON sources (design §4.0).

Only the host supplies a spec and the explicit credential environment. Search
requests supply query/count, never commands, headers, URLs or tool names.
CLI ownership, temporary HOME/cwd and cancellation cleanup reuse configured
sources' POSIX supervisor. Oversized output is stopped, never parsed as partial
success. Deadlines are absolute ``time.monotonic()`` values.

MCP server names resolve only through explicitly authorized entries in the
host-owned WRR_COMMUNITY_CONFIG file; its tool/read-only and shared-env gates
remain authoritative. Unknown names never become executable commands.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx

from wrr import config
from wrr.engines import configured_sources
from wrr.errors import EngineError
from wrr.schemas import SearchOptions, SearchResult

_PATH_PART = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)|\[(0|[1-9][0-9]*)\]")
_TEMPLATE_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


class DeclarativeError(EngineError):
    """Stable status/reason; source output and credentials never enter errors."""

    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(f"declarative source {status}: {reason}")


def _path_parts(path: str) -> tuple[str | int, ...]:
    if not isinstance(path, str) or not path.startswith("$") or len(path) > 4096:
        raise ValueError("JSONPath must start with $")
    parts: list[str | int] = []
    position = 1
    while position < len(path):
        match = _PATH_PART.match(path, position)
        if match is None:
            raise ValueError("unsupported JSONPath syntax")
        parts.append(match[1] if match[1] is not None else int(match[2]))
        position = match.end()
    return tuple(parts)


def json_path(value: Any, path: str) -> Any:
    """Read $.field[0].field; missing fields/wrong container types return None."""
    for part in _path_parts(path):
        if isinstance(part, str):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        else:
            if not isinstance(value, list) or part >= len(value):
                return None
            value = value[part]
    return value


def _http_url(value: Any) -> bool:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"https", "http"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None and not parsed.fragment
    except ValueError:
        return False


@dataclass(frozen=True)
class DeclarativeSpec:
    mode: str
    results: dict[str, str]
    cli: dict[str, Any] = field(default_factory=dict)
    http: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 15.0
    max_output_bytes: int = 1048576
    shared_env_keys: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @classmethod
    def parse(cls, block: Any) -> DeclarativeSpec | list[str]:
        """Validate a trusted top-level declarative block without probing/spawn."""
        errors: list[str] = []
        warnings: list[str] = []
        if not isinstance(block, dict):
            return ["declarative must be an object"]
        unknown = block.keys() - {"mode", "cli", "http", "mcp", "results", "timeout_s", "max_output_bytes", "shared_env_keys"}
        if unknown:
            errors.append("declarative contains unknown fields")
        try:
            shared_env_keys = configured_sources._valid_shared_env_keys(block.get("shared_env_keys", []))
        except EngineError:
            errors.append("declarative.shared_env_keys must contain unique non-control environment names")
        mode = block.get("mode")
        if mode not in ("cli", "http", "mcp"):
            errors.append("declarative.mode must be cli, http or mcp")
        timeout = block.get("timeout_s", 15)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            errors.append("declarative.timeout_s must be finite and positive")
        limit = block.get("max_output_bytes", 1048576)
        if type(limit) is not int or not 1 <= limit <= 64 * 1024 * 1024:
            errors.append("declarative.max_output_bytes must be 1..67108864")
        results = block.get("results")
        if not isinstance(results, dict):
            errors.append("declarative.results must be an object")
        else:
            if results.keys() - {"root", "title", "url", "url_template", "snippet", "time"}:
                errors.append("declarative.results contains unknown fields")
            for required in ("root", "title"):
                if required not in results:
                    errors.append(f"declarative.results.{required} is required")
            if ("url" in results) == ("url_template" in results):
                errors.append("declarative.results requires exactly one of url or url_template")
            for name, path in results.items():
                if name == "url_template":
                    if not isinstance(path, str) or not _http_url(path):
                        errors.append("declarative.results.url_template must be an HTTP URL")
                        continue
                    parsed = urlsplit(path)
                    residual = _TEMPLATE_FIELD.sub("", path)
                    if "{" in parsed.netloc or "}" in parsed.netloc or "{" in residual or "}" in residual:
                        errors.append("url_template placeholders must be row fields outside the authority")
                elif name in {"root", "title", "url", "snippet", "time"}:
                    try:
                        _path_parts(path)
                    except ValueError:
                        errors.append(f"declarative.results.{name} has invalid JSONPath")
        config = block.get(mode) if isinstance(mode, str) else None
        if mode in ("cli", "http", "mcp") and not isinstance(config, dict):
            errors.append(f"declarative.{mode} must be an object")
        elif mode == "cli":
            if config.keys() - {"command", "output", "home"}:
                errors.append("declarative.cli contains unknown fields")
            command = config.get("command")
            try:
                # Preserve configured sources' shape/length/NUL restrictions.
                # Bare executable names are resolved once by the trusted host.
                if not isinstance(command, list) or not command:
                    raise EngineError("invalid command")
                configured_sources._normalize_command(["/trusted-executable", *command[1:]])
                if not isinstance(command[0], str) or not command[0] or "\x00" in command[0] or len(command[0]) > 4096:
                    raise EngineError("invalid executable")
                if "/" in command[0] and not os.path.isabs(command[0]):
                    raise EngineError("relative executable")
                for index, arg in enumerate(command):
                    if "{" in arg or "}" in arg:
                        if index == 0 or arg not in {"{query}", "{count}"}:
                            raise EngineError("invalid placeholder")
                if any(arg == "{query}" and command[index - 1] != "--" for index, arg in enumerate(command)):
                    warnings.append("declarative.cli.command query placeholder is not preceded by literal --; option-like queries will be rejected")
            except EngineError:
                errors.append("declarative.cli.command must be bounded literal argv with standalone query/count placeholders")
            if config.get("output", "json") not in ("json", "ndjson"):
                errors.append("declarative.cli.output must be json or ndjson")
            # Login-state CLIs (gh, opencli, bili) keep credentials under the real
            # HOME; a manifest must opt in explicitly. Default stays isolated.
            if config.get("home", "isolated") not in ("isolated", "user"):
                errors.append("declarative.cli.home must be isolated or user")
        elif mode == "http":
            if config.keys() - {"url", "method", "query_param", "count_param", "headers_env", "body"}:
                errors.append("declarative.http contains unknown fields")
            if not _http_url(config.get("url")):
                errors.append("declarative.http.url must be an HTTP URL without credentials or fragment")
            if config.get("method", "GET") not in ("GET", "POST"):
                errors.append("declarative.http.method must be GET or POST")
            names = [config.get("query_param", "q"), config.get("count_param", "n")]
            if any(not isinstance(n, str) or not n for n in names) or names[0] == names[1]:
                errors.append("declarative.http parameter names must be distinct non-empty strings")
            headers = config.get("headers_env", {})
            if not isinstance(headers, dict) or any(not isinstance(k, str) or not _HEADER_NAME.fullmatch(k) or not isinstance(v, str) or not _ENV_NAME.fullmatch(v) for k, v in headers.items()):
                errors.append("declarative.http.headers_env must map header names to environment names")
            body = config.get("body", {})
            try:
                if not isinstance(body, dict):
                    raise ValueError
                json.dumps(body, allow_nan=False)
            except (TypeError, ValueError):
                errors.append("declarative.http.body must be a JSON object")
        elif mode == "mcp":
            if config.keys() - {"server", "tool", "query_arg", "count_arg"}:
                errors.append("declarative.mcp contains unknown fields")
            for name, default in (("server", None), ("tool", None), ("query_arg", "query"), ("count_arg", "limit")):
                if not isinstance(config.get(name, default), str) or not config.get(name, default):
                    errors.append(f"declarative.mcp.{name} must be a non-empty string")
            if config.get("query_arg", "query") == config.get("count_arg", "limit"):
                errors.append("declarative.mcp argument names must differ")
        if errors:
            return errors
        return cls(mode=mode, results=deepcopy(results), cli=deepcopy(block.get("cli", {})), http=deepcopy(block.get("http", {})), mcp=deepcopy(block.get("mcp", {})), timeout_s=float(timeout), max_output_bytes=limit, shared_env_keys=shared_env_keys, warnings=tuple(warnings))


def _cli_environment(env: Mapping[str, str], home: Path, shared_env_keys: tuple[str, ...]) -> dict[str, str]:
    isolated = configured_sources.isolated_environment((), home)
    for key in shared_env_keys:
        value = env.get(key)
        if isinstance(value, str) and "\x00" not in value:
            isolated[key] = value
    return isolated


async def _cli(spec: DeclarativeSpec, query: str, count: int, env: Mapping[str, str]) -> bytes:
    if os.name != "posix":
        raise DeclarativeError("error", "process_isolation_requires_posix")
    argv = [{"{query}": query, "{count}": str(count)}.get(arg, arg) for arg in spec.cli["command"]]
    executable = argv[0]
    if not os.path.isabs(executable):
        executable = shutil.which(executable, path=env.get("PATH", os.environ.get("PATH", os.defpath)))
    if executable is None:
        raise DeclarativeError("error", "executable_unavailable")
    argv[0] = executable
    try:
        configured_sources._normalize_command(argv)
    except EngineError:
        raise DeclarativeError("schema_error", "invalid_argv") from None
    with tempfile.TemporaryDirectory(prefix="wrr-declarative-") as temporary:
        root = Path(temporary)
        home, cwd = root / "home", root / "cwd"
        if spec.cli.get("home") == "user":
            home = Path.home()
        else:
            home.mkdir(mode=0o700)
        cwd.mkdir(mode=0o700)
        receipt = root / "exit.json"
        command = [sys.executable, "-I", "-S", str(Path(configured_sources.__file__).with_name("_source_supervisor.py")), str(receipt), *argv]
        try:
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=_cli_environment(env, home, spec.shared_env_keys), start_new_session=True)
        except (OSError, ValueError):
            raise DeclarativeError("error", "failed_to_start") from None
        remaining = spec.max_output_bytes

        async def consume(stream, retain):
            nonlocal remaining
            data = bytearray()
            while True:
                chunk = await stream.read(min(65536, remaining + 1))
                if not chunk:
                    return bytes(data)
                if len(chunk) > remaining:
                    raise DeclarativeError("schema_error", "output_limit")
                remaining -= len(chunk)
                if retain:
                    data.extend(chunk)
        tasks = [
            asyncio.create_task(consume(process.stdout, True)),
            asyncio.create_task(consume(process.stderr, False)),
            asyncio.create_task(process.wait()),
        ]
        try:
            stdout, _, _ = await asyncio.gather(*tasks)
            try:
                returncode = json.loads(receipt.read_text())["returncode"]
            except (OSError, ValueError, KeyError):
                raise DeclarativeError("error", "missing_exit_receipt") from None
            if returncode != 0:
                raise DeclarativeError("error", f"nonzero_exit:{returncode}")
            return stdout
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await configured_sources._finish_cleanup(process)


async def _http(spec: DeclarativeSpec, query: str, count: int, env: Mapping[str, str], timeout: float) -> bytes:
    config = spec.http
    headers = {}
    for name, key in config.get("headers_env", {}).items():
        value = env.get(key)
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise DeclarativeError("auth_error", "missing_or_invalid_credential")
        headers[name] = value
    parameters = {config.get("query_param", "q"): query, config.get("count_param", "n"): count}
    method = config.get("method", "GET")
    kwargs = {"params": parameters} if method == "GET" else {"json": {**config.get("body", {}), **parameters}}
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            async with client.stream(method, config["url"], headers=headers, **kwargs) as response:
                status = response.status_code
                if status in (401, 403):
                    raise DeclarativeError("auth_error", "http_authentication")
                if status == 429:
                    raise DeclarativeError("rate_limit", "http_rate_limit")
                if 300 <= status < 400:
                    raise DeclarativeError("schema_error", "http_redirect")
                if not 200 <= status < 300:
                    raise DeclarativeError("error", "http_status")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=min(65536, spec.max_output_bytes + 1)):
                    if len(data) + len(chunk) > spec.max_output_bytes:
                        raise DeclarativeError("schema_error", "output_limit")
                    data.extend(chunk)
                return bytes(data)
    except httpx.TimeoutException:
        raise DeclarativeError("timeout", "http_timeout") from None
    except httpx.HTTPError:
        raise DeclarativeError("error", "http_transport") from None


def _reject_constant(value):
    raise ValueError("non-finite JSON")


def _timestamp(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if type(value) in (int, float):
        if math.isfinite(value) and value >= 0:
            return float(value)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            timestamp = parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
            if timestamp >= 0:
                return timestamp
        except (ValueError, OverflowError):
            pass
    raise DeclarativeError("schema_error", "invalid_time")


def _map_results(spec: DeclarativeSpec, payload: bytes, count: int) -> list[SearchResult]:
    try:
        text = payload.decode("utf-8")
        if spec.mode == "cli" and spec.cli.get("output", "json") == "ndjson":
            document = [json.loads(line, parse_constant=_reject_constant) for line in text.splitlines() if line.strip()]
        else:
            document = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise DeclarativeError("schema_error", "invalid_json") from None
    rows = json_path(document, spec.results["root"])
    if not isinstance(rows, list):
        raise DeclarativeError("schema_error", "results_root_not_array")
    results = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            raise DeclarativeError("schema_error", "result_not_object")
        title = json_path(row, spec.results["title"])
        if "url_template" in spec.results:
            def substitute(match):
                value = row.get(match[1])
                if not isinstance(value, (str, int)) or isinstance(value, bool) or value == "":
                    raise DeclarativeError("schema_error", "missing_url_template_field")
                return quote(str(value), safe="")
            url = _TEMPLATE_FIELD.sub(substitute, spec.results["url_template"])
        else:
            url = json_path(row, spec.results["url"])
        snippet = json_path(row, spec.results["snippet"]) if "snippet" in spec.results else None
        if not isinstance(title, str) or not title.strip() or not _http_url(url) or (snippet is not None and not isinstance(snippet, str)):
            raise DeclarativeError("schema_error", "invalid_result_fields")
        timestamp = json_path(row, spec.results["time"]) if "time" in spec.results else None
        results.append(SearchResult(title=title, url=url, snippet=snippet or "", source_ts=_timestamp(timestamp)))
    return results


async def _mcp(spec: DeclarativeSpec, query: str, count: int, env: Mapping[str, str], timeout: float) -> list[SearchResult]:
    from wrr.engines.mcp_source import McpSourceAdapter, _McpSchemaFailure

    sources, _ = configured_sources.load_source_config(reserved=(), env=env)
    trusted = sources.get(spec.mcp["server"])
    if trusted is None or trusted["kind"] != "mcp":
        raise NotImplementedError("MCP trusted server-name has no authorized configured stdio command")
    if trusted["authorized"] is not True or trusted["tool"] != spec.mcp["tool"]:
        raise DeclarativeError("auth_error", "mcp_tool_not_authorized")
    cfg = {
        **trusted,
        "query_arg": spec.mcp.get("query_arg", "query"),
        "count_arg": spec.mcp.get("count_arg", "limit"),
    }

    def read_rows(result):
        payload = result.structured_content
        try:
            if payload is None:
                content = result.content
                if not isinstance(content, list) or len(content) != 1 or getattr(content[0], "type", None) != "text":
                    raise ValueError
                text = content[0].text
                if len(text.encode("utf-8")) > spec.max_output_bytes:
                    raise ValueError
                payload = json.loads(text, parse_constant=_reject_constant)
            if len(json.dumps(payload, allow_nan=False).encode("utf-8")) > spec.max_output_bytes:
                raise ValueError
            rows = json_path(payload, spec.results["root"])
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError
            return rows
        except (ValueError, TypeError, AttributeError, RecursionError) as exc:
            raise _McpSchemaFailure("invalid declarative MCP payload") from exc

    with config.source_environment(env):
        result = await McpSourceAdapter().fetch_result(
            cfg, SearchOptions(query=query, count=count),
            configured_sources.run_isolated_command, timeout,
            rows_from_result=read_rows,
        )
    outcome = result.outcome.value
    if outcome not in {"ready", "empty"}:
        status = {
            "permission_denied": "auth_error", "unauthenticated": "auth_error",
            "soft_blocked": "rate_limit", "timeout": "timeout",
            "schema_error": "schema_error",
        }.get(outcome, "error")
        raise DeclarativeError(status, "mcp_" + outcome)
    mapped = replace(spec, results={**spec.results, "root": "$"})
    return _map_results(mapped, json.dumps(result.items, allow_nan=False).encode("utf-8"), count)


async def run_declarative(spec: DeclarativeSpec, query: str, count: int, *, env: Mapping[str, str], deadline: float | None) -> list[SearchResult]:
    """Run one bounded operation; cancellation propagates after owned cleanup.

    ``env`` is a host credential mapping; CLI calls share only manifest
    shared_env_keys on top of the isolated base environment.
    HTTP credentials are taken only from manifest headers_env names. MCP server
    names must resolve to an explicitly authorized, read-only configured tool.
    """
    if not isinstance(query, str) or "\x00" in query or type(count) is not int or count < 1:
        raise DeclarativeError("schema_error", "invalid_search_options")
    if spec.mode == "cli" and query.startswith("-") and any(
        arg == "{query}" and (index == 0 or spec.cli["command"][index - 1] != "--")
        for index, arg in enumerate(spec.cli["command"])
    ):
        raise DeclarativeError("schema_error", "query_requires_option_separator")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise DeclarativeError("schema_error", "invalid_deadline")
    timeout = min(spec.timeout_s, deadline - time.monotonic()) if deadline is not None else spec.timeout_s
    if timeout <= 0:
        raise DeclarativeError("timeout", "deadline_expired")
    try:
        async with asyncio.timeout(timeout):
            if spec.mode == "mcp":
                return await _mcp(spec, query, count, env, timeout)
            payload = await _cli(spec, query, count, env) if spec.mode == "cli" else await _http(spec, query, count, env, timeout)
            return _map_results(spec, payload, count)
    except TimeoutError:
        raise DeclarativeError("timeout", "deadline_expired") from None
