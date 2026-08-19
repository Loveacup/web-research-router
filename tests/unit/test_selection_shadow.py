"""P1 Slice 3a: pure descriptor-selection shadow comparison contracts."""
from dataclasses import replace

import pytest

import wrr.selection_shadow as shadow_module
from wrr.schemas import DecisionContext, SearchOptions
from wrr.selection import descriptor_selection_plan
from wrr.selection_shadow import (
    ShadowComparisonUnavailable,
    compare_shadow_decisions,
    compare_shadow_selection,
)


def _context(**overrides):
    values = dict(
        snapshot_version="ctx-v1",
        built_at=100.0,
        expires_at=200.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa", "brave"),
        bridged_provider_ids=("exa", "brave"),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(("exa", "exa"), ("brave", "brave")),
        config_fingerprint="cfg-v1",
    )
    values.update(overrides)
    return DecisionContext(**values)


def test_exact_legacy_descriptor_selection_is_e0():
    comparison = compare_shadow_selection(
        SearchOptions("what is python"),
        _context(),
        evaluated_at=150.0,
    )

    assert comparison.code == "E0"
    assert comparison.safe is True
    assert comparison.legacy_provider_ids == ("exa", "brave")
    assert comparison.descriptor_provider_ids == ("exa", "brave")
    assert comparison.omitted_provider_ids == ()
    assert comparison.added_provider_ids == ()


def test_justified_descriptor_subset_is_e1():
    comparison = compare_shadow_selection(
        SearchOptions("what is python"),
        _context(
            routable_descriptor_ids=("exa",),
            bridged_provider_ids=("exa",),
            descriptor_reasons=(("brave", ("health:unhealthy",)),),
        ),
        evaluated_at=150.0,
    )

    assert comparison.code == "E1"
    assert comparison.safe is True
    assert comparison.descriptor_provider_ids == ("exa",)
    assert comparison.omitted_provider_ids == ("brave",)
    assert comparison.reasons == ("brave:health:unhealthy",)


def test_unexplained_omission_is_u1():
    comparison = compare_shadow_selection(
        SearchOptions("what is python"),
        _context(
            routable_descriptor_ids=("exa",),
            bridged_provider_ids=("exa",),
            missing_provider_ids=("brave",),
        ),
        evaluated_at=150.0,
    )

    assert comparison.code == "U1"
    assert comparison.safe is False


def test_order_only_mismatch_is_e2_and_addition_is_u2():
    decision = descriptor_selection_plan(
        SearchOptions("what is python"),
        _context(),
        evaluated_at=150.0,
    )
    reordered = replace(
        decision,
        selected_provider_ids=("brave", "exa"),
        selected_weights=tuple(reversed(decision.selected_weights)),
    )
    addition = replace(
        decision,
        selected_provider_ids=("exa", "brave", "rogue"),
    )

    assert compare_shadow_decisions(reordered, reordered).code == "E2"
    unsafe = compare_shadow_decisions(addition, addition)
    assert unsafe.code == "U2"
    assert unsafe.added_provider_ids == ("rogue",)


def test_repeated_evaluation_mismatch_is_u3():
    decision = descriptor_selection_plan(
        SearchOptions("what is python"),
        _context(),
        evaluated_at=150.0,
    )
    changed = replace(
        decision,
        selected_provider_ids=("brave", "exa"),
        selected_weights=tuple(reversed(decision.selected_weights)),
    )
    changed_route_plan = replace(
        decision.legacy_plan,
        engine_names=("brave", "exa"),
        weights=tuple(reversed(decision.legacy_plan.weights)),
    )

    comparison = compare_shadow_decisions(decision, changed)
    assert comparison.code == "U3"
    assert comparison.safe is False
    route_drift = compare_shadow_decisions(
        decision,
        decision,
        expected_legacy_plan=changed_route_plan,
    )
    assert route_drift.code == "U3"


def test_expired_context_raises_closed_reason():
    with pytest.raises(ShadowComparisonUnavailable) as exc:
        compare_shadow_selection(
            SearchOptions("what is python"),
            _context(expires_at=149.0),
            evaluated_at=150.0,
        )

    assert exc.value.reason == "context_expired"
    assert str(exc.value) == "context_expired"


def test_legacy_plan_mismatch_raises_distinct_closed_reason(monkeypatch):
    decision = descriptor_selection_plan(
        SearchOptions("what is python"),
        _context(),
        evaluated_at=150.0,
    )
    mismatched = replace(
        decision,
        status="blocked",
        blocked=(("exa", "legacy_plan_mismatch", ("requested:brave",)),),
    )
    monkeypatch.setattr(
        shadow_module,
        "descriptor_selection_plan",
        lambda *_args, **_kwargs: mismatched,
    )

    with pytest.raises(ShadowComparisonUnavailable) as exc:
        compare_shadow_selection(
            SearchOptions("what is python"),
            _context(),
            evaluated_at=150.0,
        )

    assert exc.value.reason == "context_mismatch"
    assert str(exc.value) == "context_mismatch"


def test_shadow_unavailable_reason_vocabulary_is_closed():
    with pytest.raises(ValueError, match="unsupported shadow unavailable reason"):
        ShadowComparisonUnavailable("secret_exception_text")


def test_shadow_comparator_has_no_control_plane_or_io_dependencies():
    from pathlib import Path

    source = Path(__file__).parents[2].joinpath("wrr", "selection_shadow.py").read_text()
    forbidden = (
        "default_registry",
        "EngineRegistry(",
        "build_decision_context",
        "discover_engine_plugins",
        "os.environ",
        "open(",
        "jsonl",
        "RouteTrace",
        "route_search_v5",
    )
    assert all(token not in source for token in forbidden)
