"""Persistent WRR runtime state for health cache and circuit breakers."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from .detect import RuntimeInfo


SCHEMA_VERSION = 1
DEFAULT_HEALTH_TTL_SEC = 300
DEFAULT_BREAKER_COOLDOWN_SEC = 60
DEFAULT_BREAKER_FAILURE_THRESHOLD = 3


@dataclass(frozen=True)
class CachedHealth:
    engine_id: str
    capability: str
    health: dict[str, Any]
    recorded_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "capability": self.capability,
            "health": dict(self.health),
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class CircuitBreakerStatus:
    engine_id: str
    state: str
    failures: int = 0
    opened_at: float | None = None
    cooldown_until: float | None = None
    last_failure_reason: str | None = None

    @property
    def open(self) -> bool:
        return self.state == "open" and (
            self.cooldown_until is None or time.time() < self.cooldown_until
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "state": self.state,
            "failures": self.failures,
            "opened_at": self.opened_at,
            "cooldown_until": self.cooldown_until,
            "last_failure_reason": self.last_failure_reason,
        }


@dataclass
class WrrState:
    schema_version: int = SCHEMA_VERSION
    updated_at: float = field(default_factory=time.time)
    health_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    circuit_breakers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "health_cache": dict(self.health_cache),
            "circuit_breakers": dict(self.circuit_breakers),
        }


def state_path(runtime: RuntimeInfo | None = None) -> Path:
    """Return the state path, honoring WRR_STATE_PATH for tests and callers."""

    override = os.environ.get("WRR_STATE_PATH")
    if override:
        return Path(override).expanduser()
    if runtime is not None and runtime.data_roots:
        return runtime.data_roots[-1] / "state.json"
    return Path.home() / ".cache" / "wrr" / "state.json"


def load_state(path: str | Path | None = None) -> WrrState:
    resolved = Path(path).expanduser() if path is not None else state_path()
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WrrState()
    if not isinstance(payload, Mapping):
        return WrrState()
    return WrrState(
        schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
        updated_at=float(payload.get("updated_at") or time.time()),
        health_cache=_dict_section(payload.get("health_cache")),
        circuit_breakers=_dict_section(payload.get("circuit_breakers")),
    )


def save_state_atomic(state: WrrState, path: str | Path | None = None) -> None:
    resolved = Path(path).expanduser() if path is not None else state_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(resolved.parent),
        prefix=f".{resolved.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, resolved)


def get_cached_health(
    engine_id: str,
    capability: str,
    ttl_sec: int | float = DEFAULT_HEALTH_TTL_SEC,
    *,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    state = load_state(path)
    raw = state.health_cache.get(_cache_key(engine_id, capability))
    if not isinstance(raw, Mapping):
        return None
    recorded_at = float(raw.get("recorded_at") or 0)
    expires_at = float(raw.get("expires_at") or 0)
    now = time.time()
    if now >= expires_at or now - recorded_at > ttl_sec:
        return None
    health = raw.get("health")
    return dict(health) if isinstance(health, Mapping) else None


def set_cached_health(
    engine_id: str,
    capability: str,
    health: Mapping[str, Any],
    ttl_sec: int | float = DEFAULT_HEALTH_TTL_SEC,
    *,
    path: str | Path | None = None,
) -> None:
    state = load_state(path)
    now = time.time()
    state.health_cache[_cache_key(engine_id, capability)] = CachedHealth(
        engine_id=engine_id,
        capability=capability,
        health=dict(health),
        recorded_at=now,
        expires_at=now + float(ttl_sec),
    ).to_dict()
    save_state_atomic(state, path)


def record_engine_failure(
    engine_id: str,
    reason: str,
    *,
    path: str | Path | None = None,
    failure_threshold: int = DEFAULT_BREAKER_FAILURE_THRESHOLD,
    cooldown_sec: int | float = DEFAULT_BREAKER_COOLDOWN_SEC,
) -> CircuitBreakerStatus:
    state = load_state(path)
    now = time.time()
    previous = _breaker_from_raw(engine_id, state.circuit_breakers.get(engine_id))
    failures = previous.failures + 1
    opened = failures >= failure_threshold
    status = CircuitBreakerStatus(
        engine_id=engine_id,
        state="open" if opened else "closed",
        failures=failures,
        opened_at=now if opened else previous.opened_at,
        cooldown_until=now + float(cooldown_sec) if opened else previous.cooldown_until,
        last_failure_reason=reason,
    )
    state.circuit_breakers[engine_id] = status.to_dict()
    save_state_atomic(state, path)
    return status


def circuit_status(
    engine_id: str,
    *,
    path: str | Path | None = None,
) -> CircuitBreakerStatus:
    state = load_state(path)
    status = _breaker_from_raw(engine_id, state.circuit_breakers.get(engine_id))
    if status.state == "open" and status.cooldown_until is not None and time.time() >= status.cooldown_until:
        status = CircuitBreakerStatus(engine_id=engine_id, state="closed")
        state.circuit_breakers[engine_id] = status.to_dict()
        save_state_atomic(state, path)
    return status


def reset_circuit(
    engine_id: str,
    *,
    path: str | Path | None = None,
) -> CircuitBreakerStatus:
    state = load_state(path)
    status = CircuitBreakerStatus(engine_id=engine_id, state="closed")
    state.circuit_breakers[engine_id] = status.to_dict()
    save_state_atomic(state, path)
    return status


def _cache_key(engine_id: str, capability: str) -> str:
    return f"{engine_id}:{capability}"


def _dict_section(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): dict(raw) for key, raw in value.items() if isinstance(raw, Mapping)}


def _breaker_from_raw(engine_id: str, raw: Any) -> CircuitBreakerStatus:
    if not isinstance(raw, Mapping):
        return CircuitBreakerStatus(engine_id=engine_id, state="closed")
    return CircuitBreakerStatus(
        engine_id=engine_id,
        state=str(raw.get("state") or "closed"),
        failures=int(raw.get("failures") or 0),
        opened_at=_optional_float(raw.get("opened_at")),
        cooldown_until=_optional_float(raw.get("cooldown_until")),
        last_failure_reason=str(raw["last_failure_reason"]) if raw.get("last_failure_reason") else None,
    )


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
