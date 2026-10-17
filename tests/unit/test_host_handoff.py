import uuid

import pytest

from wrr.host_handoff import HostHandoffError, evaluate_host_fetch


def _payload(**overrides):
    payload = {
        "schema_version": 1,
        "correlation_id": str(uuid.uuid4()),
        "url": "https://fixture.invalid/article",
        "executor": "omp:read",
        "content": "HOST_FALLBACK_CITRUS_SENTENCE\nA complete controlled article body.",
        "content_kind": "unknown",
        "content_type": "text/markdown",
        "source_complete": False,
        "max_characters": 5000,
        "fallback_reason": "wrr_all_engines_failed",
        "hop": 1,
        "max_hops": 1,
    }
    payload.update(overrides)
    return payload


def test_host_return_preserves_association_without_promoting_unverified_body():
    payload = _payload()

    result = evaluate_host_fetch(payload)

    assert result["ok"] is True
    assert result["execution_success"] is True
    assert result["correlation_id"] == payload["correlation_id"]
    assert result["actual_executor"] == "omp:read"
    assert result["fallback_reason"] == "wrr_all_engines_failed"
    assert result["result"]["text"].startswith("HOST_FALLBACK_CITRUS_SENTENCE")
    assert result["result"]["content_kind"] == "unknown"
    assert result["quality"]["verdict"] == "degraded"
    assert "host_return_unverified" in result["quality"]["reasons"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"content": "   "}, "empty_body"),
        ({"content": "Just a moment. Enable JavaScript and cookies."}, "challenge_page"),
    ],
)
def test_non_body_host_return_never_reports_execution_success(overrides, reason):
    result = evaluate_host_fetch(_payload(**overrides))

    assert result["ok"] is False
    assert result["execution_success"] is False
    assert result["quality"]["verdict"] == "failed"
    assert reason in result["quality"]["reasons"]


def test_truncated_host_return_is_usable_but_not_complete():
    result = evaluate_host_fetch(_payload(content="x" * 20, max_characters=10))

    assert result["ok"] is True
    assert result["result"]["text"] == "x" * 10
    assert result["result"]["completeness"] == "truncated"
    assert result["quality"]["verdict"] == "degraded"
    assert "content_truncated" in result["quality"]["reasons"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_version": 2},
        {"correlation_id": "not-a-uuid"},
        {"url": "file:///tmp/article"},
        {"url": "https://user:secret@fixture.invalid/article"},
        {"executor": "wrr:fetch"},
        {"content": "x" * 50_001},
        {"content_kind": "body"},
        {"content_type": "text/plain"},
        {"source_complete": True},
        {"max_characters": 0},
        {"fallback_reason": "private reason"},
        {"hop": 2},
        {"max_hops": 2},
    ],
)
def test_invalid_or_recursive_host_return_fails_closed(overrides):
    with pytest.raises(HostHandoffError):
        evaluate_host_fetch(_payload(**overrides))


def test_unknown_fields_fail_closed():
    payload = _payload(extra="not allowed")

    with pytest.raises(HostHandoffError):
        evaluate_host_fetch(payload)
