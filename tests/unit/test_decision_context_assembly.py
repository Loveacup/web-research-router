"""P1 Slice B: same-source control-plane DecisionContext assembly contracts."""
from __future__ import annotations

import wrr.runtime.decision_context_assembly as assembly


import pytest


class _FakeRuntime:
    name = "standalone"


class _FakeDiscovery:
    def __init__(self, source):
        self.source = source


class _FakeControl:
    def __init__(self, discoveries):
        self.runtime = _FakeRuntime()
        self.env = object()
        self.plugin_paths = ("plugins",)
        self.discoveries = discoveries


class _FakeReport:
    def __init__(self, routable, discovered):
        self._routable = routable
        self.discovered = discovered
        self.routable_reads = 0

    @property
    def routable(self):
        self.routable_reads += 1
        return self._routable


class _FakeRegistry:
    def __init__(self, calls, report, **kwargs):
        calls["registry_kwargs"] = kwargs
        self._report = report
        self.report_calls = []

    def report(self, *, health_mode):
        self.report_calls.append(health_mode)
        return self._report


def _wire(monkeypatch, *, legacy_registry, routable=("routable-sentinel",), discovered=()):
    calls = {"prepare": 0, "compare": [], "build": []}
    discoveries = ("discovery-sentinel",)
    control = _FakeControl(discoveries)
    report = _FakeReport(routable, discovered)
    registry_holder = {}

    def fake_prepare(**kwargs):
        calls["prepare"] += 1
        calls["prepare_kwargs"] = kwargs
        return control

    def fake_registry(**kwargs):
        registry = _FakeRegistry(calls, report, **kwargs)
        registry_holder["registry"] = registry
        return registry

    def fake_compare(legacy, descriptors, **kwargs):
        calls["compare"].append((legacy, descriptors))
        return "bridge-sentinel"

    def fake_build(report_arg, bridge_arg, **kwargs):
        calls["build"].append((report_arg, bridge_arg, kwargs))
        return "context-sentinel"

    monkeypatch.setattr(assembly, "prepare_control_plane_env", fake_prepare)
    monkeypatch.setattr(assembly, "EngineRegistry", fake_registry)
    monkeypatch.setattr(assembly, "compare_legacy_registry_bridge", fake_compare)
    monkeypatch.setattr(assembly, "build_decision_context", fake_build)
    return calls, control, report, registry_holder


def test_assembly_runs_single_prepare_report_and_injects_legacy(monkeypatch):
    legacy = object()
    calls, control, report, registry_holder = _wire(monkeypatch, legacy_registry=legacy)

    result = assembly.build_control_plane_decision_context(legacy)

    assert result == "context-sentinel"
    # 1. prepare exactly once.
    assert calls["prepare"] == 1
    # 2. registry constructed with the same discoveries; no second discovery.
    assert calls["registry_kwargs"]["discoveries"] is control.discoveries
    # 3. report(health_mode="auto") exactly once.
    assert registry_holder["registry"].report_calls == ["auto"]
    # captured routable exactly once.
    assert report.routable_reads == 1
    # 4. injected legacy registry identity + captured routable identity.
    (bridge_legacy, bridge_routable), = calls["compare"]
    assert bridge_legacy is legacy
    assert bridge_routable is report._routable


class _CountingClock:
    def __init__(self):
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return 100.0


def test_build_receives_same_report_bridge_and_reads_clock_once(monkeypatch):
    legacy = object()
    calls, control, report, _ = _wire(monkeypatch, legacy_registry=legacy)
    clock = _CountingClock()

    assembly.build_control_plane_decision_context(
        legacy, profile="prod", ttl_sec=42.0, clock=clock
    )

    (report_arg, bridge_arg, kwargs), = calls["build"]
    # 5. same report/bridge objects flow into the builder.
    assert report_arg is report
    assert bridge_arg == "bridge-sentinel"
    assert kwargs["runtime"] == control.runtime.name
    assert kwargs["profile"] == "prod"
    assert kwargs["ttl_sec"] == 42.0
    # built_at reads the injected clock exactly once.
    assert kwargs["built_at"] == 100.0
    assert clock.reads == 1


def test_registry_source_stably_summarizes_discovery_sources(monkeypatch):
    legacy = object()
    discovered = (
        _FakeDiscovery("project"),
        _FakeDiscovery("builtin"),
        _FakeDiscovery("builtin"),
    )
    calls, _, _, _ = _wire(monkeypatch, legacy_registry=legacy, discovered=discovered)

    assembly.build_control_plane_decision_context(legacy)

    (_, _, kwargs), = calls["build"]
    assert kwargs["registry_source"] == "builtin+project"


def test_registry_source_is_unknown_when_no_discoveries(monkeypatch):
    legacy = object()
    calls, _, _, _ = _wire(monkeypatch, legacy_registry=legacy, discovered=())

    assembly.build_control_plane_decision_context(legacy)

    (_, _, kwargs), = calls["build"]
    assert kwargs["registry_source"] == "unknown"


def test_snapshot_version_prefers_explicit_else_mints_unique(monkeypatch):
    legacy = object()
    calls, _, _, _ = _wire(monkeypatch, legacy_registry=legacy)

    assembly.build_control_plane_decision_context(legacy, snapshot_version="pinned")
    assembly.build_control_plane_decision_context(legacy)
    assembly.build_control_plane_decision_context(legacy)

    explicit = calls["build"][0][2]["snapshot_version"]
    first_generated = calls["build"][1][2]["snapshot_version"]
    second_generated = calls["build"][2][2]["snapshot_version"]

    assert explicit == "pinned"
    assert first_generated and second_generated
    assert first_generated != second_generated


class _Boom(Exception):
    pass


@pytest.mark.parametrize("stage", ["prepare", "report", "compare", "build"])
def test_any_stage_failure_propagates_without_partial_context(monkeypatch, stage):
    legacy = object()
    calls, control, report, registry_holder = _wire(monkeypatch, legacy_registry=legacy)

    def boom(*args, **kwargs):
        raise _Boom(stage)

    if stage == "prepare":
        monkeypatch.setattr(assembly, "prepare_control_plane_env", boom)
    elif stage == "report":
        monkeypatch.setattr(
            _FakeRegistry, "report", lambda self, *, health_mode: boom()
        )
    elif stage == "compare":
        monkeypatch.setattr(assembly, "compare_legacy_registry_bridge", boom)
    else:
        monkeypatch.setattr(assembly, "build_decision_context", boom)

    with pytest.raises(_Boom, match=stage):
        assembly.build_control_plane_decision_context(legacy)


def test_module_does_not_touch_cache_router_or_live_recovery():
    from pathlib import Path

    source = (
        Path(assembly.__file__).read_text()
    )
    forbidden = (
        "CachedDecisionContextProvider",
        "route_search_v5",
        "get_registry",
        "register(",
        "live_recovery",
        "health_mode=\"live\"",
        "health_mode='live'",
        "threading",
        "global ",
    )
    assert all(token not in source for token in forbidden)
