"""P1 Slice 2b: production DecisionContext builder contracts."""
from dataclasses import replace

import pytest

from wrr.engines.adapter_bridge import compare_legacy_registry_bridge
from wrr.engines.registry import EngineRegistry as V6EngineRegistry, RegistryReport
from wrr.registry import default_registry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env
from wrr.selection_context import build_decision_context


def _reports(tmp_path):
    runtime = detect_runtime(explicit="standalone", cwd=tmp_path, env={})
    env = load_env(runtime, overrides={}, env_files=[])
    registry = V6EngineRegistry(
        runtime=runtime,
        env=env,
        include_builtin=True,
        state_file=tmp_path / "state.json",
    )
    resolved = registry.resolve()
    # The production builder consumes one full report and a bridge built from
    # exactly that report's routable descriptors.
    report = registry.report(health_mode="light")
    bridge = compare_legacy_registry_bridge(default_registry(), report.routable)
    # A resolve-only bridge is used only to prove actual adapter aliases,
    # including descriptors that are not currently routable (for example qmd).
    all_resolved_bridge = compare_legacy_registry_bridge(default_registry(), resolved)
    return runtime, report, bridge, all_resolved_bridge


def test_bridge_report_records_actual_descriptor_provider_aliases(tmp_path):
    _, _, _, bridge = _reports(tmp_path)

    assert ("qmd", "local_qmd") in bridge.descriptor_provider_aliases
    assert ("exa", "exa") in bridge.descriptor_provider_aliases
    assert bridge.to_dict()["descriptor_provider_aliases"] == [
        list(item) for item in bridge.descriptor_provider_aliases
    ]


def test_build_context_uses_full_report_and_bridge_truth(tmp_path):
    runtime, report, bridge, _ = _reports(tmp_path)

    context = build_decision_context(
        report,
        bridge,
        runtime=runtime.name,
        profile="default",
        registry_source="builtin-test",
        snapshot_version="ctx-v1",
        built_at=100.0,
        ttl_sec=30.0,
    )

    assert context.built_at == 100.0
    assert context.expires_at == 130.0
    assert context.runtime == "standalone"
    assert context.profile == "default"
    assert context.registry_source == "builtin-test"
    assert context.routable_descriptor_ids == tuple(sorted(
        descriptor.id for descriptor in report.routable
    ))
    assert set(context.bridged_provider_ids) == set(bridge.bridged_provider_ids)
    assert context.missing_provider_ids == bridge.missing_provider_ids
    assert context.descriptor_provider_aliases == bridge.descriptor_provider_aliases
    assert {item[0] for item in context.descriptor_reasons} == {
        descriptor.id for descriptor in report.resolved if not descriptor.routable
    }
    assert all(reasons for _, reasons in context.descriptor_reasons)
    assert len(context.config_fingerprint) == 64


def test_builder_is_deterministic_and_rejects_invalid_ttl(tmp_path):
    runtime, report, bridge, _ = _reports(tmp_path)
    kwargs = dict(
        runtime=runtime.name,
        profile="default",
        registry_source="builtin-test",
        snapshot_version="ctx-v1",
        built_at=100.0,
        ttl_sec=30.0,
    )

    first = build_decision_context(report, bridge, **kwargs)
    second = build_decision_context(report, bridge, **kwargs)

    assert first == second
    assert hash(first) == hash(second)
    with pytest.raises(ValueError, match="ttl_sec"):
        build_decision_context(report, bridge, **{**kwargs, "ttl_sec": 0.0})
    with pytest.raises(ValueError, match="expires_at"):
        build_decision_context(
            report,
            bridge,
            **{**kwargs, "built_at": 1e308, "ttl_sec": 1e308},
        )


def test_builder_rejects_bridge_not_built_from_report_routable(tmp_path):
    runtime, report, _, all_resolved_bridge = _reports(tmp_path)

    with pytest.raises(ValueError, match="must match registry_report.routable"):
        build_decision_context(
            report,
            all_resolved_bridge,
            runtime=runtime.name,
            profile="default",
            registry_source="builtin-test",
            snapshot_version="ctx-v1",
            built_at=100.0,
            ttl_sec=30.0,
        )


def test_builder_rejects_duplicate_bridge_descriptor_ids(tmp_path):
    runtime, report, bridge, _ = _reports(tmp_path)
    assert bridge.v6_descriptor_ids
    corrupted = replace(
        bridge,
        v6_descriptor_ids=(
            *bridge.v6_descriptor_ids,
            bridge.v6_descriptor_ids[0],
        ),
    )

    with pytest.raises(
        ValueError,
        match="bridge report contains duplicate descriptor ids",
    ):
        build_decision_context(
            report,
            corrupted,
            runtime=runtime.name,
            profile="default",
            registry_source="builtin-test",
            snapshot_version="ctx-v1",
            built_at=100.0,
            ttl_sec=30.0,
        )


def test_bridge_rejects_duplicate_descriptor_ids(tmp_path):
    _, report, _, _ = _reports(tmp_path)
    descriptor = report.resolved[0]

    with pytest.raises(ValueError, match="duplicate descriptor id"):
        compare_legacy_registry_bridge(default_registry(), (descriptor, descriptor))


def test_bridge_collision_is_error_not_silent_overwrite(tmp_path):
    _, report, _, _ = _reports(tmp_path)
    descriptor = next(item for item in report.resolved if item.id == "exa")
    first = replace(descriptor, id="exa-a", routable=True)
    second = replace(descriptor, id="exa-b", routable=True)

    bridge = compare_legacy_registry_bridge(default_registry(), (first, second))

    assert bridge.registry.names().count("exa") == 1
    assert "exa-b" in bridge.adapter_errors
    assert bridge.descriptor_provider_aliases == (
        ("exa-a", "exa"),
        ("exa-b", "exa"),
    )


def test_failed_adapter_keeps_intended_provider_alias(tmp_path, monkeypatch):
    import wrr.engines.adapter_bridge as bridge_module
    from wrr.engines.base import SearchEngine

    _, report, _, _ = _reports(tmp_path)
    descriptor = report.resolved[0]

    class BrokenAdapter(SearchEngine):
        name = "renamed-provider"

        def __init__(self):
            raise RuntimeError("constructor boom")

    monkeypatch.setattr(bridge_module, "_load_adapter_class", lambda adapter: BrokenAdapter)

    bridge = compare_legacy_registry_bridge(default_registry(), (descriptor,))

    assert bridge.descriptor_provider_aliases == (
        (descriptor.id, "renamed-provider"),
    )
    assert descriptor.id in bridge.adapter_errors
    assert bridge.bridged_provider_ids == ()


def test_bridge_loads_and_constructs_adapter_once(tmp_path, monkeypatch):
    import wrr.engines.adapter_bridge as bridge_module
    from wrr.engines.base import SearchEngine

    _, report, _, _ = _reports(tmp_path)
    descriptor = report.resolved[0]
    counts = {"load": 0, "init": 0}

    class CountingAdapter(SearchEngine):
        name = "counting-provider"

        def __init__(self):
            counts["init"] += 1

    def load_once(adapter):
        counts["load"] += 1
        return CountingAdapter

    monkeypatch.setattr(bridge_module, "_load_adapter_class", load_once)

    bridge = compare_legacy_registry_bridge(default_registry(), (descriptor,))

    assert counts == {"load": 1, "init": 1}
    assert bridge.bridged_provider_ids == ("counting-provider",)


def test_fingerprint_ignores_unapproved_keyword_like_constants(monkeypatch):
    import wrr.config as config_module
    from wrr.selection_context import _selection_config_fingerprint

    before = _selection_config_fingerprint()
    monkeypatch.setattr(
        config_module,
        "UNRELATED_KEYWORDS",
        ("must-not-enter-selection-fingerprint",),
        raising=False,
    )

    assert _selection_config_fingerprint() == before


def test_builder_does_not_connect_to_router_or_execute_search():
    from pathlib import Path

    source = Path(__file__).parents[2].joinpath("wrr", "selection_context.py").read_text()
    forbidden = (
        "legacy_selection_plan",
        "descriptor_selection_plan",
        "route_search_v5",
        "get_registry",
        "default_registry_v6_shadow",
        ".search(",
        "os.environ",
        "inspect.getsource",
    )
    assert all(token not in source for token in forbidden)
