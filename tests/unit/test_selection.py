"""P1 Slice 2: offline descriptor selection decisions."""
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from wrr.schemas import DecisionContext, DecisionSnapshot, SearchOptions
from wrr.selection import descriptor_selection_plan


def _context(**overrides):
    values = {
        "snapshot_version": "ctx-v1",
        "built_at": 100.0,
        "expires_at": 200.0,
        "runtime": "standalone",
        "profile": "default",
        "registry_source": "v6-report",
        "routable_descriptor_ids": ("exa", "qmd"),
        "bridged_provider_ids": ("exa", "local_qmd"),
        "missing_provider_ids": ("brave",),
        "adapter_errors": (),
        "descriptor_reasons": (),
        "descriptor_provider_aliases": (("qmd", "local_qmd"),),
        "config_fingerprint": "cfg-1",
    }
    values.update(overrides)
    return DecisionContext(**values)


def _plan(*names, explicit_provider=None):
    return DecisionSnapshot(
        source="legacy",
        mode=None if explicit_provider else "grounding",
        mode_reason="explicit_provider" if explicit_provider else "sentinel",
        explicit_provider=explicit_provider,
        engine_names=tuple(names),
        weights=(
            ()
            if explicit_provider
            else tuple((name, float(index + 1) / 10) for index, name in enumerate(names))
        ),
    )


def test_decision_context_is_frozen_hashable_and_canonical():
    first = _context(
        routable_descriptor_ids=("qmd", "exa", "exa"),
        bridged_provider_ids=("local_qmd", "exa", "exa"),
        adapter_errors=(("brave", "bad"), ("academic", "missing")),
        descriptor_reasons=(("qmd", ("z", "a", "a")),),
        descriptor_provider_aliases=(("qmd", "local_qmd"), ("qmd", "local_qmd")),
    )
    second = _context(
        routable_descriptor_ids=("exa", "qmd"),
        bridged_provider_ids=("exa", "local_qmd"),
        adapter_errors=(("academic", "missing"), ("brave", "bad")),
        descriptor_reasons=(("qmd", ("a", "z")),),
    )

    assert first == second
    assert hash(first) == hash(second)
    assert first in {first}
    assert first.routable_descriptor_ids == ("exa", "qmd")
    assert first.descriptor_reasons == (("qmd", ("a", "z")),)
    with pytest.raises(FrozenInstanceError):
        first.profile = "other"


def test_decision_context_rejects_invalid_expiry_and_conflicting_aliases():
    with pytest.raises(ValueError, match="expires_at"):
        _context(built_at=201.0, expires_at=200.0)
    with pytest.raises(ValueError, match="alias"):
        _context(descriptor_provider_aliases=(("qmd", "local_qmd"), ("qmd", "qmd")))


def test_descriptor_plan_filters_without_adding_and_preserves_order(monkeypatch):
    sentinel = _plan("brave", "exa", "academic")
    calls = []
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: calls.append(options) or sentinel,
    )
    context = _context(bridged_provider_ids=("exa", "community"))

    decision = descriptor_selection_plan(SearchOptions("q"), context, evaluated_at=150.0)

    assert calls and len(calls) == 1
    assert decision.executable is True
    assert decision.status == "selected"
    assert decision.selected_provider_ids == ("exa",)
    assert decision.selected_weights == (("exa", 0.2),)
    assert "community" not in decision.selected_provider_ids
    assert tuple(item[0] for item in decision.blocked) == ("brave", "academic")


def test_descriptor_alias_attributes_reason_to_legacy_provider(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("local_qmd"),
    )
    context = _context(
        bridged_provider_ids=(),
        missing_provider_ids=("local_qmd",),
        descriptor_reasons=(("qmd", ("binary_missing", "not_configured")),),
    )

    decision = descriptor_selection_plan(SearchOptions("q"), context, evaluated_at=150.0)

    assert decision.executable is False
    assert decision.status == "empty"
    assert decision.blocked == (
        ("local_qmd", "descriptor_blocked", ("binary_missing", "not_configured")),
    )


def test_explicit_provider_selected_when_bridged(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("exa", explicit_provider="exa"),
    )

    decision = descriptor_selection_plan(SearchOptions("q", provider="exa"), _context(), evaluated_at=150.0)

    assert decision.executable is True
    assert decision.status == "selected"
    assert decision.explicit_provider == "exa"
    assert decision.explicit_provider_status == "selected"
    assert decision.selected_provider_ids == ("exa",)


def test_explicit_provider_blocked_without_substitution(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("brave", explicit_provider="brave"),
    )

    decision = descriptor_selection_plan(SearchOptions("q", provider="brave"), _context(), evaluated_at=150.0)

    assert decision.executable is False
    assert decision.status == "blocked"
    assert decision.explicit_provider_status == "blocked"
    assert decision.selected_provider_ids == ()
    assert decision.blocked == (("brave", "missing_provider", ()),)


def test_empty_and_expired_decisions_are_non_executing(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("academic", "brave"),
    )
    empty = descriptor_selection_plan(SearchOptions("q"), _context(bridged_provider_ids=()), evaluated_at=150.0)
    expired = descriptor_selection_plan(SearchOptions("q"), _context(), evaluated_at=200.0)

    assert empty.status == "empty" and empty.executable is False
    assert expired.status == "expired" and expired.executable is False
    assert tuple(item[1] for item in expired.blocked) == ("context_expired", "context_expired")
    assert expired.selected_provider_ids == ()


def test_bridged_provider_must_also_have_routable_descriptor(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("exa"),
    )
    context = _context(
        bridged_provider_ids=("exa",),
        routable_descriptor_ids=("qmd",),
    )

    decision = descriptor_selection_plan(SearchOptions("q"), context, evaluated_at=150.0)

    assert decision.executable is False
    assert decision.blocked == (("exa", "descriptor_blocked", ("not_routable",)),)


def test_alias_provider_id_cannot_bypass_descriptor_routability(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("local_qmd"),
    )
    context = _context(
        bridged_provider_ids=("local_qmd",),
        routable_descriptor_ids=("local_qmd",),
        descriptor_provider_aliases=(("qmd", "local_qmd"),),
    )

    decision = descriptor_selection_plan(SearchOptions("q"), context, evaluated_at=150.0)

    assert decision.executable is False
    assert decision.blocked == (
        ("local_qmd", "descriptor_blocked", ("not_routable",)),
    )


def test_decision_snapshot_rejects_duplicate_or_misaligned_candidates():
    with pytest.raises(ValueError, match="duplicate engine"):
        _plan("exa", "exa")
    with pytest.raises(ValueError, match="weights"):
        DecisionSnapshot(
            source="legacy",
            mode="grounding",
            mode_reason="sentinel",
            explicit_provider=None,
            engine_names=("exa",),
            weights=(("brave", 0.2),),
        )
    with pytest.raises(ValueError, match="explicit_provider"):
        DecisionSnapshot(
            source="legacy",
            mode=None,
            mode_reason="explicit_provider",
            explicit_provider="brave",
            engine_names=("exa",),
            weights=(),
        )


def test_options_and_explicit_snapshot_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("brave", explicit_provider="brave"),
    )

    decision = descriptor_selection_plan(
        SearchOptions("q", provider="exa"),
        _context(bridged_provider_ids=("brave",), routable_descriptor_ids=("brave",)),
        evaluated_at=150.0,
    )

    assert decision.executable is False
    assert decision.status == "blocked"
    assert decision.explicit_provider_status == "blocked"
    assert decision.blocked == (("brave", "legacy_plan_mismatch", ("requested:exa",)),)


def test_reason_priority_is_stable(monkeypatch):
    monkeypatch.setattr(
        "wrr.selection.legacy_selection_plan",
        lambda options: _plan("brave", "academic", "github", "community"),
    )
    context = _context(
        bridged_provider_ids=(),
        routable_descriptor_ids=("community",),
        missing_provider_ids=("brave", "academic", "github"),
        adapter_errors=(("brave", "adapter boom"),),
        descriptor_reasons=(("academic", ("auth_missing",)),),
    )

    decision = descriptor_selection_plan(SearchOptions("q"), context, evaluated_at=150.0)

    assert decision.blocked == (
        ("brave", "adapter_error", ("adapter boom",)),
        ("academic", "descriptor_blocked", ("auth_missing",)),
        ("github", "missing_provider", ()),
        ("community", "not_bridged", ()),
    )


def test_selection_module_is_offline_and_has_no_runtime_control_imports():
    source = Path(__file__).parents[2].joinpath("wrr", "selection.py").read_text()
    forbidden = (
        "adapter_bridge",
        "wrr.registry",
        "engines.registry",
        "os.environ",
        "route_search_v5",
        "_dispatch(",
        ".search(",
    )
    assert all(token not in source for token in forbidden)
