"""R3 runtime state persistence tests."""

from __future__ import annotations

import time

from wrr.runtime.detect import detect_runtime
from wrr.runtime.state import (
    circuit_status,
    get_cached_health,
    load_state,
    record_engine_failure,
    save_state_atomic,
    set_cached_health,
    state_path,
)


def test_state_path_honors_wrr_state_path(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setenv("WRR_STATE_PATH", str(path))
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})

    assert state_path(runtime) == path


def test_save_state_atomic_round_trips(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state = load_state(path)
    state.health_cache["exa:live"] = {
        "engine_id": "exa",
        "capability": "live",
        "health": {"engine_id": "exa", "status": "healthy", "checks": []},
        "recorded_at": time.time(),
        "expires_at": time.time() + 60,
    }

    save_state_atomic(state, path)

    loaded = load_state(path)
    assert loaded.schema_version == 1
    assert loaded.health_cache["exa:live"]["health"]["status"] == "healthy"


def test_cached_health_respects_ttl(tmp_path):
    path = tmp_path / "state.json"
    set_cached_health(
        "exa",
        "live",
        {"engine_id": "exa", "status": "healthy", "checks": []},
        ttl_sec=1,
        path=path,
    )

    assert get_cached_health("exa", "live", ttl_sec=60, path=path)["status"] == "healthy"

    state = load_state(path)
    state.health_cache["exa:live"]["recorded_at"] = time.time() - 120
    state.health_cache["exa:live"]["expires_at"] = time.time() + 120
    save_state_atomic(state, path)

    assert get_cached_health("exa", "live", ttl_sec=10, path=path) is None


def test_circuit_breaker_persists_and_cools_down(tmp_path):
    path = tmp_path / "state.json"

    opened = record_engine_failure(
        "exa",
        "timeout",
        path=path,
        failure_threshold=1,
        cooldown_sec=60,
    )
    loaded = circuit_status("exa", path=path)

    assert opened.open is True
    assert loaded.open is True
    assert loaded.last_failure_reason == "timeout"

    state = load_state(path)
    state.circuit_breakers["exa"]["cooldown_until"] = time.time() - 1
    save_state_atomic(state, path)

    assert circuit_status("exa", path=path).open is False
