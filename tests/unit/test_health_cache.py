"""R3 health cache and live/light registry behavior tests."""

from __future__ import annotations

import json
import sys
import time

from wrr.doctor import doctor_v6
from wrr.engines.registry import EngineRegistry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env
from wrr.runtime.state import load_state, save_state_atomic


def _manifest(engine_id: str, marker_path: str):
    return {
        "schema_version": 1,
        "id": engine_id,
        "name": engine_id.title(),
        "kind": "web_api",
        "adapter": f"wrr.engines.{engine_id}:{engine_id.title()}Engine",
        "capabilities": {"actions": ["search"], "domains": ["web"]},
        "routing": {"modes": ["auto", "web"], "weight": 1.0},
        "requirements": {"env": [], "binaries": [], "repos": []},
        "health": {
            "checks": [
                {"type": "env_present", "env": "OPTIONAL_KEY", "required": False},
                {
                    "type": "live_probe",
                    "level": "live",
                    "required": True,
                    "command": [
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            f"Path({marker_path!r}).write_text('ran', encoding='utf-8')"
                        ),
                    ],
                },
            ]
        },
        "requires_capabilities": {},
    }


def _plugin(tmp_path, engine_id="probe"):
    marker = tmp_path / f"{engine_id}.marker"
    plugin_dir = tmp_path / "plugins" / "engines" / engine_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "engine.yaml").write_text(
        json.dumps(_manifest(engine_id, str(marker))),
        encoding="utf-8",
    )
    return tmp_path / "plugins" / "engines", marker


def _registry(tmp_path, state_file, plugin_paths, *, ttl=300):
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    return EngineRegistry(
        runtime=runtime,
        env=load_env(runtime, overrides={}, env_files=[]),
        plugin_paths=[plugin_paths],
        include_builtin=False,
        trust_project=True,
        state_file=state_file,
        health_ttl_sec=ttl,
    )


def test_light_health_skips_live_probe(tmp_path):
    plugin_paths, marker = _plugin(tmp_path)
    registry = _registry(tmp_path, tmp_path / "state.json", plugin_paths)

    health = registry.health(mode="light")[0]

    assert health.status == "degraded"
    assert [check.type for check in health.checks] == ["env_present"]
    assert not marker.exists()


def test_live_health_is_cached_until_ttl_expires(tmp_path):
    plugin_paths, marker = _plugin(tmp_path)
    state_file = tmp_path / "state.json"

    first = _registry(tmp_path, state_file, plugin_paths, ttl=60).health(mode="live")[0]
    assert first.status == "degraded"
    assert marker.read_text(encoding="utf-8") == "ran"

    marker.unlink()
    second = _registry(tmp_path, state_file, plugin_paths, ttl=60).health(mode="live")[0]
    assert second.to_dict() == first.to_dict()
    assert not marker.exists()

    state = load_state(state_file)
    state.health_cache["probe:live"]["recorded_at"] = time.time() - 120
    state.health_cache["probe:live"]["expires_at"] = time.time() - 1
    save_state_atomic(state, state_file)

    third = _registry(tmp_path, state_file, plugin_paths, ttl=60).health(mode="live")[0]
    assert third.status == "degraded"
    assert marker.exists()


def test_auto_health_reuses_live_cache_but_does_not_run_live_probe(tmp_path):
    plugin_paths, marker = _plugin(tmp_path)
    state_file = tmp_path / "state.json"
    live = _registry(tmp_path, state_file, plugin_paths).health(mode="live")[0]
    marker.unlink()

    auto = _registry(tmp_path, state_file, plugin_paths).health(mode="auto")[0]

    assert auto.to_dict() == live.to_dict()
    assert not marker.exists()


def test_circuit_breaker_state_survives_registry_instances(tmp_path):
    plugin_paths, _marker = _plugin(tmp_path)
    state_file = tmp_path / "state.json"
    first = _registry(tmp_path, state_file, plugin_paths)
    for _ in range(3):
        first.record_engine_failure("probe", "timeout")

    report = _registry(tmp_path, state_file, plugin_paths).report()
    descriptor = report.resolved[0]

    assert descriptor.health.status == "unhealthy"
    assert descriptor.health.checks[0].type == "circuit_breaker"
    assert descriptor.routable is False


def test_doctor_v6_deep_runs_live_health(tmp_path, monkeypatch):
    plugin_paths, marker = _plugin(tmp_path)
    monkeypatch.setenv("WRR_STATE_PATH", str(tmp_path / "doctor-state.json"))

    payload = doctor_v6(
        runtime_hint="standalone",
        cwd=tmp_path,
        env={},
        plugin_paths=[plugin_paths],
        deep=True,
        trust_project=True,
    ).to_dict()

    probe = next(item for item in payload["health"] if item["engine_id"] == "probe")
    assert probe["checks"][1]["type"] == "live_probe"
    assert marker.exists()
