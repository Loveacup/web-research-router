"""Explicit configured-source loading and isolated subprocess execution.

This module is deliberately host-driven: model request data cannot select a
configuration file, grant a source, or alter the configured command.
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from collections.abc import Collection, Mapping
from typing import Any

from wrr import config
from wrr.errors import EngineError

_CONFIG_ENV = "WRR_COMMUNITY_CONFIG"
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_SOURCES = 64
_MAX_COMMAND_PARTS = 64
_MAX_COMMAND_PART_LENGTH = 4096
_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_ROOT_FIELDS = frozenset(
    {"schema_version", "sources", "default_sources", "authorized_sources"}
)
_COMMON_SOURCE_FIELDS = frozenset(
    {
        "kind",
        "command",
        "shared_env_keys",
        "title",
        "url",
        "snippet",
        "engagement",
        "comments",
        "time",
        "eng_max",
    }
)
_MCP_SOURCE_FIELDS = _COMMON_SOURCE_FIELDS | {
    "tool",
    "query_arg",
    "count_arg",
    "arguments",
}
_MAPPING_FIELDS = ("title", "url", "snippet", "engagement", "comments", "time")

_SAFE_PUBLIC_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM")
_CONTROL_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "SHELL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "ENV",
        "BASH_ENV",
        "CDPATH",
        "IFS",
        "VIRTUAL_ENV",
    }
)
_CONTROL_ENV_PREFIXES = ("PYTHON", "LD_", "DYLD_", "GIT_", "WRR_", "CONDA_")
_TIMEOUT_STDERR = "configured source timed out"
_START_STDERR = "configured source failed to start"
_PROCESS_GROUP_KILL_WAIT = 1.0


def _configuration_error(reason: str) -> EngineError:
    return EngineError(f"community source configuration rejected: {reason}")

def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON number")



def _read_explicit_config(path_text: str) -> Any:
    path = Path(path_text)
    if not path.is_absolute():
        raise _configuration_error("path must be absolute")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _configuration_error("file must not be a symbolic link") from None
        raise _configuration_error("file is unavailable") from None

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _configuration_error("file must be regular")
        if info.st_uid != os.getuid():
            raise _configuration_error("file owner is not the current user")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _configuration_error("file permissions are unsafe")
        if info.st_size > _MAX_CONFIG_BYTES:
            raise _configuration_error("file is too large")

        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise _configuration_error("file is too large")
    except OSError:
        raise _configuration_error("file could not be read") from None
    finally:
        os.close(fd)

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _configuration_error("file is not valid JSON") from None


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise _configuration_error(f"{field} must be a string list")
    if len(value) != len(set(value)):
        raise _configuration_error(f"{field} contains duplicates")
    return value


def _valid_shared_env_keys(value: Any) -> tuple[str, ...]:
    keys = _string_list(value, "shared_env_keys")
    for key in keys:
        upper = key.upper()
        if not _ENV_NAME.fullmatch(key):
            raise _configuration_error("shared_env_keys contains an invalid name")
        if upper in _CONTROL_ENV_NAMES or upper.startswith(_CONTROL_ENV_PREFIXES):
            raise _configuration_error("shared_env_keys contains a process-control name")
    return tuple(keys)


def _normalize_command(value: Any) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > _MAX_COMMAND_PARTS
        or any(type(part) is not str or not part or "\x00" in part for part in value)
        or any(len(part) > _MAX_COMMAND_PART_LENGTH for part in value)
    ):
        raise _configuration_error("command must be a bounded non-empty string list")
    if not os.path.isabs(value[0]):
        raise _configuration_error("command executable must be absolute")
    return list(value)


def _normalize_source(source_id: str, value: Any, authorized: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _configuration_error("source entry must be an object")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in {"mcp", "opencli"}:
        raise _configuration_error("source kind is unsupported")
    allowed = _MCP_SOURCE_FIELDS if kind == "mcp" else _COMMON_SOURCE_FIELDS
    unknown = set(value) - allowed
    if unknown:
        raise _configuration_error("source contains unknown fields")

    command = _normalize_command(value.get("command"))
    shared_env_keys = _valid_shared_env_keys(value.get("shared_env_keys", []))
    normalized: dict[str, Any] = {
        "kind": kind,
        "command": command,
        "shared_env_keys": shared_env_keys,
        "authorized": source_id in authorized,
    }
    if kind == "opencli":
        normalized["cli"] = list(command)

    for field in _MAPPING_FIELDS:
        if field not in value:
            continue
        field_value = value[field]
        if field_value is not None and (type(field_value) is not str or not field_value):
            raise _configuration_error(f"{field} must be a non-empty string or null")
        normalized[field] = field_value

    if "eng_max" in value:
        eng_max = value["eng_max"]
        try:
            valid_limit = (
                not isinstance(eng_max, bool)
                and isinstance(eng_max, (int, float))
                and math.isfinite(float(eng_max))
                and eng_max > 0
            )
        except OverflowError:
            valid_limit = False
        if not valid_limit:
            raise _configuration_error("eng_max must be a positive finite number")
        normalized["eng_max"] = eng_max

    if kind == "mcp":
        tool = value.get("tool")
        if type(tool) is not str or not tool:
            raise _configuration_error("MCP tool must be a non-empty string")
        query_arg = value.get("query_arg", "query")
        count_arg = value.get("count_arg", "count")
        if type(query_arg) is not str or not query_arg:
            raise _configuration_error("MCP query_arg must be a non-empty string")
        if type(count_arg) is not str or not count_arg:
            raise _configuration_error("MCP count_arg must be a non-empty string")
        arguments = value.get("arguments", {})
        if not isinstance(arguments, dict) or any(type(key) is not str for key in arguments):
            raise _configuration_error("MCP arguments must be an object with string keys")
        normalized.update(
            tool=tool,
            query_arg=query_arg,
            count_arg=count_arg,
            arguments=arguments,
        )

    return normalized


def load_source_config(
    reserved: Collection[str],
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...] | None]:
    """Load the single explicitly selected configured-source file.

    Unset ``WRR_COMMUNITY_CONFIG`` is the compatibility path and performs no
    filesystem lookup. Invalid or unsafe explicit files fail closed.
    """

    path_text = config.get_env(_CONFIG_ENV) if env is None else env.get(_CONFIG_ENV)
    if path_text is None:
        return {}, None
    if not path_text:
        raise _configuration_error("path must be absolute")

    document = _read_explicit_config(path_text)
    if not isinstance(document, Mapping):
        raise _configuration_error("top level must be an object")
    if set(document) - _ROOT_FIELDS:
        raise _configuration_error("top level contains unknown fields")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise _configuration_error("schema_version must be 1")

    sources_value = document.get("sources")
    if not isinstance(sources_value, Mapping) or len(sources_value) > _MAX_SOURCES:
        raise _configuration_error("sources must be a bounded object")

    reserved_ids = set(reserved)
    source_ids: set[str] = set()
    for source_id in sources_value:
        if type(source_id) is not str or not _SOURCE_ID.fullmatch(source_id):
            raise _configuration_error("source ID is invalid")
        if source_id in reserved_ids:
            raise _configuration_error("source ID is reserved")
        source_ids.add(source_id)

    authorized_list = _string_list(document.get("authorized_sources", []), "authorized_sources")
    authorized = set(authorized_list)
    if not authorized <= source_ids:
        raise _configuration_error("authorized_sources contains an unknown ID")

    default_sources: tuple[str, ...] | None = None
    if "default_sources" in document:
        defaults = _string_list(document["default_sources"], "default_sources")
        if not set(defaults) <= source_ids:
            raise _configuration_error("default_sources contains an unknown ID")
        default_sources = tuple(defaults)

    sources = {
        source_id: _normalize_source(source_id, value, authorized)
        for source_id, value in sources_value.items()
    }
    return sources, default_sources


def isolated_environment(shared_env_keys: Collection[str], home: Path) -> dict[str, str]:
    """Build a minimal subprocess environment with explicit sharing only."""

    if isinstance(shared_env_keys, (str, bytes)):
        raise _configuration_error("shared_env_keys must be a string collection")
    keys = list(shared_env_keys)
    if any(type(key) is not str for key in keys) or len(keys) != len(set(keys)):
        raise _configuration_error("shared_env_keys must contain unique strings")
    normalized_keys = _valid_shared_env_keys(keys)

    home = Path(home)
    if not home.is_absolute():
        raise _configuration_error("isolated HOME must be absolute")
    env = {"HOME": str(home), "PATH": os.defpath}
    for key in _SAFE_PUBLIC_ENV:
        value = config.get_env(key)
        if value is not None:
            env[key] = value
    for key in normalized_keys:
        value = config.get_env(key)
        if value is not None:
            env[key] = value
    return env


async def _reap_process_group(process: asyncio.subprocess.Process) -> None:
    """Ask the live supervisor to terminate its group; never signal stale PGIDs."""
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    async def discard(stream: asyncio.StreamReader | None) -> None:
        if stream is not None:
            while await stream.read(64 * 1024):
                pass

    # communicate() was cancelled before entering cleanup. Keep both pipes
    # flowing until EOF, without retaining output from a terminating source.
    pending = asyncio.gather(discard(process.stdout), discard(process.stderr), process.wait())
    try:
        await asyncio.wait_for(pending, _PROCESS_GROUP_KILL_WAIT)
    except asyncio.TimeoutError as exc:
        raise EngineError("configured source cleanup did not complete") from exc


async def _finish_cleanup(process: asyncio.subprocess.Process) -> None:
    cleanup = asyncio.create_task(_reap_process_group(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def run_isolated_command(
    cli: list[str],
    timeout: float,
    *,
    shared_env_keys: Collection[str] = (),
) -> tuple[int | None, str, str]:
    """Run literal argv in a temporary HOME/cwd and an owned POSIX session.

    Timeout is represented by ``(None, "", stable_reason)``. Cancellation is
    propagated after the complete owned process group has been terminated.
    """

    if os.name != "posix":
        raise EngineError("configured source process isolation requires POSIX")
    command = _normalize_command(cli)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise EngineError("configured source timeout must be positive")
    if not math.isfinite(float(timeout)) or timeout <= 0:
        raise EngineError("configured source timeout must be positive")

    with tempfile.TemporaryDirectory(prefix="wrr-configured-source-") as temporary:
        root = Path(temporary)
        home = root / "home"
        work = root / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        env = isolated_environment(shared_env_keys, home)
        receipt = root / "exit.json"
        supervised = [
            sys.executable, "-I", "-S",
            str(Path(__file__).with_name("_source_supervisor.py")),
            str(receipt), *command,
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *supervised,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work,
                env=env,
                start_new_session=True,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError):
            return 127, "", _START_STDERR

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=float(timeout)
            )
        except asyncio.TimeoutError:
            await _finish_cleanup(process)
            return None, "", _TIMEOUT_STDERR
        except asyncio.CancelledError:
            await _finish_cleanup(process)
            raise
        else:
            try:
                returncode = json.loads(receipt.read_text())["returncode"]
            except (OSError, ValueError, KeyError):
                returncode = 127
            await _finish_cleanup(process)
            return (
                returncode,
                stdout_bytes.decode("utf-8", errors="replace"),
                stderr_bytes.decode("utf-8", errors="replace"),
            )
