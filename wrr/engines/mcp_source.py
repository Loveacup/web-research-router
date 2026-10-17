"""Optional MCP-over-stdio community source adapter.

The official MCP SDK owns protocol negotiation, request dispatch, and subprocess
teardown.  Imports of the optional SDK and of the community result types stay
inside ``fetch_result`` so importing WRR does not require MCP and does not create
a reverse runtime import from ``community_sources``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
import logging
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection

if TYPE_CHECKING:
    from .community_sources import RunCmd, SourceFetchResult


_ACTIVE_MCP_LOG_SCOPE = ContextVar("wrr_mcp_log_scope", default=False)


class _SafeSdkLog(logging.Filter):
    def filter(self, record):
        if _ACTIVE_MCP_LOG_SCOPE.get():
            record.msg = "mcp_protocol_event"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


@contextmanager
def _safe_sdk_logs():
    token = _ACTIVE_MCP_LOG_SCOPE.set(True)
    guard = _SafeSdkLog()
    loggers = [logging.getLogger(name) for name in (
        "client", "mcp.client.stdio", "mcp.shared.jsonrpc_dispatcher",
        "mcp.shared.dispatcher")]
    for logger in loggers:
        logger.addFilter(guard)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(guard)
        _ACTIVE_MCP_LOG_SCOPE.reset(token)


@asynccontextmanager
async def _guard_teardown(context, seal):
    """Seal native cancellation before each SDK context starts unwinding."""
    async with context as value:
        try:
            yield value
        finally:
            seal()


class _McpSchemaFailure(Exception):
    """The configured tool or its payload did not satisfy the search contract."""


class _McpPermissionFailure(Exception):
    """The listed tool did not satisfy the read-only execution gate."""


def _validated_host_config(cfg: dict[str, Any], options: Any, timeout: float) -> tuple[list[str], str, str, str, dict[str, Any], tuple[str, ...], int]:
    """Validate the trusted host composition before any optional import or spawn."""
    command = cfg.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
        or not os.path.isabs(command[0])
    ):
        raise _McpSchemaFailure("invalid command")

    tool = cfg.get("tool")
    query_arg = cfg.get("query_arg", "query")
    count_arg = cfg.get("count_arg", "count")
    if not all(isinstance(value, str) and value for value in (tool, query_arg, count_arg)):
        raise _McpSchemaFailure("invalid tool arguments")
    if query_arg == count_arg:
        raise _McpSchemaFailure("query and count arguments overlap")

    fixed_arguments = cfg.get("arguments", {})
    if not isinstance(fixed_arguments, dict):
        raise _McpSchemaFailure("invalid fixed arguments")
    try:
        # The wire accepts JSON values only.  Reject host-composition mistakes
        # before starting an authorized executable.
        json.dumps(fixed_arguments, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _McpSchemaFailure("fixed arguments are not JSON") from exc

    shared_env_keys = cfg.get("shared_env_keys", ())
    if (
        isinstance(shared_env_keys, str)
        or not isinstance(shared_env_keys, Collection)
        or any(not isinstance(key, str) or not key for key in shared_env_keys)
    ):
        raise _McpSchemaFailure("invalid shared environment keys")

    query = getattr(options, "query", None)
    count = getattr(options, "count", None)
    if not isinstance(query, str) or not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise _McpSchemaFailure("invalid search options")
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0 or not math.isfinite(timeout):
        raise _McpSchemaFailure("invalid timeout")

    arguments = dict(fixed_arguments)
    arguments[query_arg] = query
    arguments[count_arg] = count
    return list(command), tool, query_arg, count_arg, arguments, tuple(shared_env_keys), count


def _validate_input_schema(schema: Any, arguments: dict[str, Any]) -> None:
    """Validate the exact call locally against the server-advertised schema."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise _McpSchemaFailure("tool input schema is not an object")
    try:
        from jsonschema.validators import validator_for
        from referencing import Registry

        validator_cls = validator_for(schema)
        validator_cls.check_schema(schema)
        # An explicit registry has no retrieval callback. Local resources and
        # fragments still resolve, but remote/relative refs (including nested
        # $id bases) fail closed without synchronous I/O outside our deadline.
        validator_cls(schema, registry=Registry()).validate(arguments)
    except _McpSchemaFailure:
        raise
    except Exception as exc:
        raise _McpSchemaFailure("tool input schema rejected arguments") from exc


def _rows_from_tool_result(result: Any) -> list[dict[str, Any]]:
    """Extract search rows from structured content or one text JSON value."""
    payload = result.structured_content
    if payload is None:
        content = result.content
        if not isinstance(content, list) or len(content) != 1:
            raise _McpSchemaFailure("tool result must contain one JSON value")
        block = content[0]
        if getattr(block, "type", None) != "text" or not isinstance(getattr(block, "text", None), str):
            raise _McpSchemaFailure("tool result is not text JSON")
        try:
            payload = json.loads(block.text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _McpSchemaFailure("tool result is not valid JSON") from exc

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("results", "items", "list"):
            if key in payload:
                rows = payload[key]
                break
        if rows is None:
            raise _McpSchemaFailure("tool result has no result rows")
    else:
        raise _McpSchemaFailure("tool result has invalid shape")

    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _McpSchemaFailure("tool result rows are invalid")
    return [dict(row) for row in rows]


def _is_connection_failure(exc: BaseException, mcp_error_type: type[BaseException], connection_closed: Any) -> bool:
    """Recognize transport closure without exposing an exception or stderr."""
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if isinstance(current, mcp_error_type):
            if getattr(getattr(current, "error", None), "code", None) == connection_closed:
                return True
        if type(current).__name__ in {
            "BrokenResourceError",
            "ClosedResourceError",
            "EndOfStream",
            "IncompleteReadError",
        }:
            return True
    return False


class McpSourceAdapter:
    """Execute one explicitly authorized, read-only MCP search tool over stdio."""

    async def fetch(
        self,
        cfg: dict[str, Any],
        options: Any,
        run_cmd: RunCmd,
        timeout: float,
    ) -> list[dict[str, Any]]:
        result = await self.fetch_result(cfg, options, run_cmd, timeout)
        return result.items

    async def fetch_result(
        self,
        cfg: dict[str, Any],
        options: Any,
        run_cmd: RunCmd,
        timeout: float,
    ) -> SourceFetchResult:
        # Caller cancellation and the request deadline share one delivery gate.
        # The SDK-owning task must not receive a second cancel during teardown.
        stop_sent = False
        deadline_expired = False
        teardown_started = False
        timer = None

        def seal():
            nonlocal teardown_started
            teardown_started = True
            if timer is not None:
                timer.cancel()

        operation = asyncio.create_task(
            self._fetch_result(cfg, options, run_cmd, timeout, seal))

        def stop(*, deadline=False):
            nonlocal stop_sent, deadline_expired
            if stop_sent or teardown_started or operation.done():
                return
            stop_sent = True
            deadline_expired = deadline
            operation.cancel()

        if (isinstance(timeout, (int, float)) and not isinstance(timeout, bool)
                and timeout > 0 and math.isfinite(timeout)):
            timer = asyncio.get_running_loop().call_later(
                timeout, lambda: stop(deadline=True))
        try:
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                stop()
                while not operation.done():
                    try:
                        await asyncio.shield(operation)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        break
                if not operation.cancelled():
                    try:
                        operation.result()
                    except Exception as exc:
                        raise asyncio.CancelledError from exc
                if deadline_expired and not asyncio.current_task().cancelling():
                    from .community_sources import SourceFetchResult, SourceOutcome
                    return SourceFetchResult(
                        outcome=SourceOutcome.TIMEOUT,
                        detail="mcp_stdio.timeout",
                    )
                raise
        finally:
            if timer is not None:
                timer.cancel()

    async def _fetch_result(
        self,
        cfg: dict[str, Any],
        options: Any,
        run_cmd: RunCmd,
        timeout: float,
        seal,
    ) -> SourceFetchResult:
        # Method-local imports avoid both the optional-SDK import at module load
        # and the community_sources -> mcp_source registration cycle.
        from .community_sources import SourceFetchResult, SourceOutcome

        del run_cmd  # MCP lifecycle and subprocess ownership belong to the SDK.

        if cfg.get("authorized") is not True:
            return SourceFetchResult(
                outcome=SourceOutcome.PERMISSION_DENIED,
                detail="mcp_stdio.source_not_authorized",
            )

        try:
            command, tool_name, _query_arg, _count_arg, arguments, shared_env_keys, count = _validated_host_config(
                cfg, options, timeout
            )
        except _McpSchemaFailure:
            return SourceFetchResult(
                outcome=SourceOutcome.SCHEMA_ERROR,
                detail="mcp_stdio.invalid_source_configuration",
            )

        try:
            from mcp.client.session import ClientSession
            from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS, StdioServerParameters, stdio_client
            from mcp.shared.exceptions import MCPError
            from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT, PaginatedRequestParams
            from pydantic import ValidationError

            from .configured_sources import isolated_environment
        except ImportError:
            return SourceFetchResult(
                outcome=SourceOutcome.ERROR,
                detail="mcp_stdio.sdk_unavailable",
            )

        detail_prefix = "mcp_stdio"
        try:
            with _safe_sdk_logs():
                with tempfile.TemporaryDirectory(prefix="wrr-mcp-") as temporary:
                    root = Path(temporary)
                    home = root / "home"
                    cwd = root / "cwd"
                    home.mkdir(mode=0o700)
                    cwd.mkdir(mode=0o700)

                    isolated = isolated_environment(shared_env_keys, home=home)
                    # SDK 2 merges its own default inherited variables over an
                    # explicitly supplied env.  Override every such key, then
                    # apply the controlled environment, without mutating SDK
                    # globals or the host process environment.
                    sdk_env = {key: "" for key in DEFAULT_INHERITED_ENV_VARS}
                    sdk_env.update(isolated)
                    sdk_env["HOME"] = str(home)

                    parameters = StdioServerParameters(
                        command=sys.executable,
                        args=["-I", "-S", str(Path(__file__).with_name("_source_supervisor.py")),
                              str(root / "exit.json"), *command],
                        env=sdk_env,
                        cwd=cwd,
                    )
                    with open(os.devnull, "w", encoding="utf-8") as errlog:
                        async with _guard_teardown(
                            stdio_client(parameters, errlog=errlog), seal,
                        ) as (read_stream, write_stream):
                            async with _guard_teardown(ClientSession(
                                read_stream,
                                write_stream,
                                read_timeout_seconds=float(timeout),
                            ), seal) as session:
                                await session.initialize()

                                matches = []
                                cursor = None
                                seen_cursors: set[str] = set()
                                while True:
                                    params = PaginatedRequestParams(cursor=cursor) if cursor is not None else None
                                    listed = await session.list_tools(params=params)
                                    matches.extend(tool for tool in listed.tools if tool.name == tool_name)
                                    cursor = listed.next_cursor
                                    if cursor is None:
                                        break
                                    if not isinstance(cursor, str) or cursor in seen_cursors:
                                        raise _McpSchemaFailure("invalid tools/list pagination")
                                    seen_cursors.add(cursor)

                                if len(matches) != 1:
                                    raise _McpSchemaFailure("configured tool is not listed exactly once")
                                tool = matches[0]
                                if tool.annotations is None or tool.annotations.read_only_hint is not True:
                                    raise _McpPermissionFailure("configured tool is not read-only")
                                _validate_input_schema(tool.input_schema, arguments)

                                try:
                                    called = await session.call_tool(
                                        tool_name,
                                        arguments,
                                        read_timeout_seconds=float(timeout),
                                    )
                                except (ValidationError, RuntimeError) as exc:
                                    raise _McpSchemaFailure("tool result violated its declared schema") from exc
                                if called.is_error:
                                    return SourceFetchResult(
                                        outcome=SourceOutcome.ERROR,
                                        detail=f"{detail_prefix}.tool_error",
                                    )
                                rows = _rows_from_tool_result(called)
                                return SourceFetchResult(
                                    items=rows[:count],
                                    outcome=SourceOutcome.READY if rows else SourceOutcome.EMPTY,
                                    detail=detail_prefix,
                                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return SourceFetchResult(
                outcome=SourceOutcome.TIMEOUT,
                detail=f"{detail_prefix}.timeout",
            )
        except _McpPermissionFailure:
            return SourceFetchResult(
                outcome=SourceOutcome.PERMISSION_DENIED,
                detail=f"{detail_prefix}.permission_denied",
            )
        except _McpSchemaFailure:
            return SourceFetchResult(
                outcome=SourceOutcome.SCHEMA_ERROR,
                detail=f"{detail_prefix}.schema_error",
            )
        except ValidationError:
            return SourceFetchResult(
                outcome=SourceOutcome.SCHEMA_ERROR,
                detail=f"{detail_prefix}.schema_error",
            )
        except MCPError as exc:
            error_code = getattr(getattr(exc, "error", None), "code", None)
            if error_code == REQUEST_TIMEOUT:
                return SourceFetchResult(
                    outcome=SourceOutcome.TIMEOUT,
                    detail=f"{detail_prefix}.timeout",
                )
            if _is_connection_failure(exc, MCPError, CONNECTION_CLOSED):
                return SourceFetchResult(
                    outcome=SourceOutcome.BRIDGE_DISCONNECTED,
                    detail=f"{detail_prefix}.disconnected",
                )
            return SourceFetchResult(
                outcome=SourceOutcome.ERROR,
                detail=f"{detail_prefix}.protocol_error",
            )
        except (OSError, ValueError):
            return SourceFetchResult(
                outcome=SourceOutcome.ERROR,
                detail=f"{detail_prefix}.startup_error",
            )
        except Exception as exc:
            # AnyIO task groups wrap exceptions raised inside the SDK contexts.
            # Classify only homogeneous leaves; a teardown failure must not be
            # hidden behind a permission/schema verdict.
            leaves = []
            pending = [exc]
            while pending:
                current = pending.pop()
                if isinstance(current, BaseExceptionGroup):
                    pending.extend(current.exceptions)
                else:
                    leaves.append(current)
            if leaves and all(isinstance(item, _McpPermissionFailure) for item in leaves):
                return SourceFetchResult(
                    outcome=SourceOutcome.PERMISSION_DENIED,
                    detail=f"{detail_prefix}.permission_denied",
                )
            if leaves and all(isinstance(item, (_McpSchemaFailure, ValidationError)) for item in leaves):
                return SourceFetchResult(
                    outcome=SourceOutcome.SCHEMA_ERROR,
                    detail=f"{detail_prefix}.schema_error",
                )
            if leaves and all(
                isinstance(item, MCPError)
                and getattr(item.error, "code", None) == REQUEST_TIMEOUT
                for item in leaves
            ):
                return SourceFetchResult(
                    outcome=SourceOutcome.TIMEOUT,
                    detail=f"{detail_prefix}.timeout",
                )
            if _is_connection_failure(exc, MCPError, CONNECTION_CLOSED):
                return SourceFetchResult(
                    outcome=SourceOutcome.BRIDGE_DISCONNECTED,
                    detail=f"{detail_prefix}.disconnected",
                )
            return SourceFetchResult(
                outcome=SourceOutcome.ERROR,
                detail=f"{detail_prefix}.client_failure",
            )
