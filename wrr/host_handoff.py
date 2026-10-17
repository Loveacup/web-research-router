"""Validate and qualify explicit host-native fetch returns.

This module never invokes a host tool or routes back into WRR.  A host adapter owns
recommend/accept/reject state and submits one accepted return for evaluation here.
"""
from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from .extraction import make_extraction, qualify_extraction
from .schemas import ExtractOptions, RouteQuality

_SCHEMA_VERSION = 1
_MAX_CONTENT_CHARS = 50_000
_EXECUTORS = frozenset({"omp:read"})
_CONTENT_KIND = "unknown"
_REASON_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REQUIRED_KEYS = frozenset({
    "schema_version", "correlation_id", "url", "executor", "content",
    "content_kind", "content_type", "source_complete", "max_characters",
    "fallback_reason", "hop", "max_hops",
})


class HostHandoffError(ValueError):
    """Safe validation error for an invalid host-return envelope."""


def _exact_str(value: Any, field: str) -> str:
    if type(value) is not str:
        raise HostHandoffError(f"{field} must be a string")
    return value


def _uuid4(value: Any) -> str:
    text = _exact_str(value, "correlation_id")
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise HostHandoffError("correlation_id must be UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != text:
        raise HostHandoffError("correlation_id must be canonical UUIDv4")
    return text


def _http_url(value: Any) -> str:
    text = _exact_str(value, "url")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise HostHandoffError("url must be an absolute HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HostHandoffError("url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or port is not None and not 0 < port < 65536:
        raise HostHandoffError("url contains unsupported authority fields")
    return text


def evaluate_host_fetch(payload: Any) -> dict[str, Any]:
    """Return a stable fetch result for one accepted, associated host return."""
    if type(payload) is not dict:
        raise HostHandoffError("host return must be an object")
    if set(payload) != _REQUIRED_KEYS:
        raise HostHandoffError("host return fields do not match schema_version 1")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != _SCHEMA_VERSION:
        raise HostHandoffError("unsupported host return schema_version")

    correlation_id = _uuid4(payload["correlation_id"])
    url = _http_url(payload["url"])
    executor = _exact_str(payload["executor"], "executor")
    if executor not in _EXECUTORS:
        raise HostHandoffError("executor is not supported")
    content = _exact_str(payload["content"], "content")
    if len(content) > _MAX_CONTENT_CHARS:
        raise HostHandoffError("content exceeds the supported limit")
    content_kind = _exact_str(payload["content_kind"], "content_kind")
    if content_kind != _CONTENT_KIND:
        raise HostHandoffError("host-reported content_kind is not trusted")
    content_type = _exact_str(payload["content_type"], "content_type")
    if content_type != "text/markdown":
        raise HostHandoffError("host return content_type must be text/markdown")
    if payload["source_complete"] is not False:
        raise HostHandoffError("host-reported completeness is not trusted")
    max_characters = payload["max_characters"]
    if type(max_characters) is not int or not 1 <= max_characters <= _MAX_CONTENT_CHARS:
        raise HostHandoffError("max_characters is out of range")
    fallback_reason = _exact_str(payload["fallback_reason"], "fallback_reason")
    if not _REASON_RE.fullmatch(fallback_reason):
        raise HostHandoffError("fallback_reason is not a safe token")
    if type(payload["hop"]) is not int or payload["hop"] != 1:
        raise HostHandoffError("host handoff hop must equal 1")
    if type(payload["max_hops"]) is not int or payload["max_hops"] != 1:
        raise HostHandoffError("host handoff max_hops must equal 1")

    extraction = make_extraction(
        ExtractOptions(url=url, max_characters=max_characters),
        content,
        method=f"host:{executor}",
        content_type=content_type,
        source_complete=payload["source_complete"],
        content_kind=content_kind,
    )
    quality = qualify_extraction(
        extraction,
        RouteQuality(
            verdict="complete",
            expected_sources=[executor],
            successful_sources=[executor],
            independent_source_count=1,
            min_required=1,
        ),
    )
    if "host_return_unverified" not in quality.reasons:
        quality.reasons.append("host_return_unverified")
    ok = quality.verdict != "failed"
    return {
        "operation": "host_fetch_return",
        "ok": ok,
        "handoff_status": "completed",
        "execution_success": ok,
        "correlation_id": correlation_id,
        "url": url,
        "actual_executor": executor,
        "fallback_reason": fallback_reason,
        "hop": 1,
        "max_hops": 1,
        "result": extraction.to_dict(),
        "quality": quality.to_dict(),
    }
