"""WRR P1 3b.2: route_search_v5 ↔ DecisionEvidence integration (selection-only).

Stage-S evidence is minted, attached to ``result.diagnostics.decision_evidence``
and best-effort recorded to an injected sink — without ever changing the legacy
routing result, the raised exception, or leaking query/result/error text.

RED-first. Fake engines/registry only; no network, no live engines.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
import json
import uuid

import pytest

import wrr.router as router_mod

from conftest import FakeEngine, mk_results
from wrr.registry import EngineRegistry
from wrr.router import route_search_v5
from wrr.schemas import (
    SearchOptions,
    DecisionContext,
    DecisionEvidence,
    DecisionEvidenceV2,
    ShadowComparison,
)
from wrr.runtime.decision_context_provider import CachedDecisionContextProvider
from wrr.errors import AllEnginesFailedError
from wrr.selection_shadow import ShadowComparisonUnavailable


def run(coro):
    return asyncio.run(coro)


def _reg(*engines):
    r = EngineRegistry()
    for e in engines:
        r.register(e)
    return r


def _full_reg():
    return _reg(*[FakeEngine(n, search_results=mk_results(2))
                  for n in ("exa", "brave", "searxng", "github",
                            "community", "academic", "skill")])


def _shadow_context(expires_at=200.0):
    return DecisionContext(
        snapshot_version="ctx-v1",
        built_at=100.0,
        expires_at=expires_at,
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


def _empty_descriptor_context():
    return replace(_shadow_context(), routable_descriptor_ids=())


class SpySink:
    """Captures every recorded evidence object by identity."""

    def __init__(self):
        self.records = []

    def record(self, evidence):
        self.records.append(evidence)


class RaisingSink:
    """Records then raises — must never disturb routing."""

    def __init__(self):
        self.records = []

    def record(self, evidence):
        self.records.append(evidence)
        raise RuntimeError("sink boom")


def _is_uuid4(value):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


# ── warm success: exact object in sink + uuid4 + E0 + no query ───────
def test_warm_success_records_exact_object_uuid4_e0_no_query():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))

    ev = result.diagnostics.decision_evidence
    assert isinstance(ev, DecisionEvidence)
    assert len(sink.records) == 1
    assert sink.records[0] is ev                     # exact same object recorded
    assert _is_uuid4(ev.request_key)
    assert ev.stage == "S"
    assert ev.terminal == "routed"
    assert ev.outcome == "success"
    assert ev.mode == "grounding"
    assert ev.actual_provider == "rrf:grounding"
    assert ev.shadow_comparison is not None
    assert ev.shadow_comparison.code == "E0"
    # legacy result unchanged
    assert result.actual_provider == "rrf:grounding"
    assert result.payload
    # privacy: no query text anywhere in the serialized record
    assert "what is python" not in json.dumps(ev.to_dict())


def test_opt_in_v2_with_complete_observation_records_v2_e0():
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert sink.records == [evidence]
    assert evidence.context_status == "available"
    assert evidence.comparison_status == "compared"
    assert evidence.execution_protection == "not_required"
    assert evidence.shadow_comparison is not None
    assert evidence.shadow_comparison.code == "E0"


def test_v2_route_trace_ignores_instance_serializer_override():
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
    ))
    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    object.__setattr__(evidence, "to_dict", lambda: {"secret": "credential"})

    payload = result.diagnostics.to_dict()["decision_evidence"]

    assert payload["schema_version"] == 2
    assert "secret" not in payload


@pytest.mark.parametrize("reason", ["context_expired", "context_mismatch"])
def test_opt_in_v2_maps_closed_comparison_unavailable_reason(monkeypatch, reason):
    def unavailable(*_args, **_kwargs):
        raise ShadowComparisonUnavailable(reason)

    monkeypatch.setattr("wrr.selection_shadow.compare_shadow_selection", unavailable)
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.context_status == "available"
    assert evidence.comparison_status == reason
    assert evidence.execution_protection == "unobservable"
    assert evidence.shadow_comparison is None


def test_comparison_exception_reason_property_cannot_break_legacy_execution(monkeypatch):
    class HostileComparisonError(RuntimeError):
        @property
        def reason(self):
            raise RuntimeError("reason property boom")

    def unavailable(*_args, **_kwargs):
        raise HostileComparisonError("comparison boom")

    monkeypatch.setattr("wrr.selection_shadow.compare_shadow_selection", unavailable)
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
    ))

    assert result.payload
    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.comparison_status == "comparison_failed"


@pytest.mark.parametrize(
    ("code", "descriptor", "omitted", "added", "reasons"),
    [
        ("E2", ("brave", "exa"), (), (), ("order_only_mismatch",)),
        ("U1", ("exa",), ("brave",), (), ("unexplained_omission",)),
        ("U2", ("exa", "brave", "rogue"), (), ("rogue",), ("unsafe_addition",)),
        ("U3", ("exa", "brave"), (), (), ("nondeterministic_selection",)),
    ],
)
def test_opt_in_v2_preserves_all_remaining_comparison_codes(
    monkeypatch, code, descriptor, omitted, added, reasons
):
    comparison = ShadowComparison(
        code=code,
        safe=code.startswith("E"),
        legacy_provider_ids=("exa", "brave"),
        descriptor_provider_ids=descriptor,
        omitted_provider_ids=omitted,
        added_provider_ids=added,
        reasons=reasons,
        context_snapshot_version="ctx-v1",
        config_fingerprint="cfg-v1",
    )
    monkeypatch.setattr(
        "wrr.selection_shadow.compare_shadow_selection",
        lambda *_args, **_kwargs: comparison,
    )
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.comparison_status == "compared"
    assert evidence.shadow_comparison.code == code


def test_v2_request_without_observation_falls_back_to_v1():
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    assert type(result.diagnostics.decision_evidence) is DecisionEvidence
    assert sink.records == [result.diagnostics.decision_evidence]


def test_v2_request_with_partial_observation_falls_back_to_v1():
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=object(),
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    assert type(result.diagnostics.decision_evidence) is DecisionEvidence
    assert sink.records == [result.diagnostics.decision_evidence]


def test_v2_request_with_hostile_observation_status_falls_back_to_v1():
    class HostileStatus:
        def __eq__(self, _other):
            raise RuntimeError("hostile equality")

    observation = router_mod.DecisionContextObservation(None, HostileStatus(), None)
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=observation,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    assert result.payload
    assert type(result.diagnostics.decision_evidence) is DecisionEvidence
    assert sink.records == [result.diagnostics.decision_evidence]


def test_complete_observation_keeps_v1_when_version_not_opted_in():
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))

    assert type(result.diagnostics.decision_evidence) is DecisionEvidence
    assert sink.records == [result.diagnostics.decision_evidence]


def test_opt_in_v2_cold_observation_records_context_unavailable():
    provider = CachedDecisionContextProvider(_shadow_context)
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.context_status == "cold"
    assert evidence.comparison_status == "context_unavailable"
    assert evidence.execution_protection == "unobservable"


def test_opt_in_v2_failed_observation_without_last_good_records_build_failed():
    def fail_builder():
        raise RuntimeError("builder failed")

    provider = CachedDecisionContextProvider(fail_builder)
    with pytest.raises(RuntimeError, match="builder failed"):
        provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.context_status == "refresh_failed"
    assert evidence.comparison_status == "context_build_failed"
    assert evidence.execution_protection == "unobservable"


def test_opt_in_v2_refresh_failed_with_last_good_still_compares():
    should_fail = False

    def builder():
        if should_fail:
            raise RuntimeError("refresh failed")
        return _shadow_context()

    provider = CachedDecisionContextProvider(builder)
    provider.refresh()
    should_fail = True
    with pytest.raises(RuntimeError, match="refresh failed"):
        provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.context_status == "refresh_failed"
    assert evidence.comparison_status == "compared"
    assert evidence.context_cohort_id is not None


def test_opt_in_v2_records_successful_legacy_protection_for_empty_descriptor():
    provider = CachedDecisionContextProvider(_empty_descriptor_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.shadow_comparison.code == "E1"
    assert evidence.shadow_comparison.descriptor_provider_count == 0
    assert evidence.outcome == "success"
    assert evidence.execution_protection == "protected_by_legacy"


def test_opt_in_v2_records_unprotected_empty_for_empty_descriptor(monkeypatch):
    async def empty_dispatch(*_args, **_kwargs):
        return [], [], []

    monkeypatch.setattr("wrr.router._dispatch", empty_dispatch)
    provider = CachedDecisionContextProvider(_empty_descriptor_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.outcome == "empty"
    assert evidence.execution_protection == "unprotected_empty"


def test_opt_in_v2_keeps_not_required_for_empty_outcome_with_descriptor(monkeypatch):
    async def empty_dispatch(*_args, **_kwargs):
        return [], [], []

    monkeypatch.setattr("wrr.router._dispatch", empty_dispatch)
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()

    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context_observation=provider.observe(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_version=2,
        decision_evidence_sink=sink,
    ))

    evidence = result.diagnostics.decision_evidence
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.outcome == "empty"
    assert evidence.shadow_comparison.descriptor_provider_count > 0
    assert evidence.execution_protection == "not_required"


# ── cold success (no context) → evidence attached, shadow None ───────
def test_cold_success_attaches_evidence_shadow_none():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))

    ev = result.diagnostics.decision_evidence
    assert isinstance(ev, DecisionEvidence)
    assert ev.shadow_comparison is None
    assert ev.terminal == "routed"
    assert ev.outcome == "success"
    assert len(sink.records) == 1


# ── sink=None still attaches evidence ────────────────────────────────
def test_sink_none_still_attaches_evidence():
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
    ))
    assert isinstance(result.diagnostics.decision_evidence, DecisionEvidence)


# ── Stage S OFF: never mint / attach / record even with a sink ───────
def test_stage_s_off_explicit_false_no_evidence():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=False,
        decision_evidence_sink=sink,
    ))
    assert result.diagnostics.decision_evidence is None
    assert sink.records == []


def test_stage_s_off_default_none_no_context_no_evidence():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_evidence_sink=sink,
    ))
    assert result.diagnostics.decision_evidence is None
    assert sink.records == []


# ── raising sink preserves result + keeps attachment ─────────────────
def test_raising_sink_preserves_result_and_attachment():
    sink = RaisingSink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    assert result.actual_provider == "rrf:grounding"
    assert isinstance(result.diagnostics.decision_evidence, DecisionEvidence)
    assert len(sink.records) == 1        # attempted exactly once


# ── explicit provider success ────────────────────────────────────────
def test_explicit_provider_success_evidence():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("q", provider="brave"),
        _reg(FakeEngine("brave", search_results=mk_results(2))),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    ev = result.diagnostics.decision_evidence
    assert ev.terminal == "explicit_provider"
    assert ev.mode is None
    assert ev.actual_provider == "brave"
    assert ev.outcome == "success"
    assert len(sink.records) == 1
    assert sink.records[0] is ev


# ── recovery fallback success ────────────────────────────────────────
def test_recovery_success_evidence():
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", search_results=[]),
        FakeEngine("brave", search_results=[]),
        FakeEngine("searxng", search_results=mk_results(2)),
    )
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        reg,
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    ev = result.diagnostics.decision_evidence
    assert result.actual_provider == "rrf:recovery"
    assert ev.terminal == "recovery"
    assert ev.mode == "recovery"
    assert ev.actual_provider == "rrf:recovery"
    assert ev.outcome == "success"
    assert len(sink.records) == 1


# ── degraded_success accepted and recorded ───────────────────────────
def test_degraded_success_accepted_and_recorded():
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", search_results=mk_results(2)),
        FakeEngine("brave", search_results=mk_results(2)),
        FakeEngine("community", search_results=mk_results(2)),
        FakeEngine("academic", error="academic down"),
    )
    result = run(route_search_v5(
        SearchOptions("深度分析 ai", count=10),
        reg,
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    ev = result.diagnostics.decision_evidence
    assert result.quality.verdict == "degraded_success"
    assert ev.quality_verdict == "degraded_success"
    assert len(sink.records) == 1
    assert sink.records[0] is ev


# ── recovery_blocked: one error record + identical exception ─────────
def test_recovery_blocked_records_one_error_and_reraises(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: False)
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", search_results=[]),
        FakeEngine("brave", search_results=[]),
    )
    with pytest.raises(AllEnginesFailedError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    assert "recovery is blocked" in str(excinfo.value)
    assert len(sink.records) == 1
    ev = sink.records[0]
    assert ev.terminal == "recovery_blocked"
    assert ev.outcome == "error"
    assert ev.actual_provider is None
    assert ev.result_count == 0
    assert ev.quality_verdict == "failed"
    assert ev.mode == "grounding"


# ── final all-engines-failed: one error record ───────────────────────
def test_all_fail_records_one_error_all_engines_failed(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: True)
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", error="down"),
        FakeEngine("brave", error="down"),
        FakeEngine("searxng", error="down"),
    )
    with pytest.raises(AllEnginesFailedError):
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    assert len(sink.records) == 1
    ev = sink.records[0]
    assert ev.terminal == "all_engines_failed"
    assert ev.outcome == "error"
    assert ev.actual_provider is None
    assert ev.result_count == 0


def test_opt_in_v2_records_error_without_changing_raised_exception(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: True)
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", error="down"),
        FakeEngine("brave", error="down"),
        FakeEngine("searxng", error="down"),
    )

    with pytest.raises(AllEnginesFailedError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            decision_context_observation=provider.observe(),
            shadow_evaluated_at=150.0,
            stage_s_enabled=True,
            decision_evidence_version=2,
            decision_evidence_sink=sink,
        ))

    assert "down" in str(excinfo.value)
    assert len(sink.records) == 1
    evidence = sink.records[0]
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.outcome == "error"
    assert evidence.terminal == "all_engines_failed"
    assert evidence.comparison_status == "compared"
    assert evidence.execution_protection == "not_required"


def test_opt_in_v2_records_unprotected_error_for_empty_descriptor(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: True)
    provider = CachedDecisionContextProvider(_empty_descriptor_context)
    provider.refresh()
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", error="down"),
        FakeEngine("brave", error="down"),
        FakeEngine("searxng", error="down"),
    )

    with pytest.raises(AllEnginesFailedError):
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            decision_context_observation=provider.observe(),
            shadow_evaluated_at=150.0,
            stage_s_enabled=True,
            decision_evidence_version=2,
            decision_evidence_sink=sink,
        ))

    evidence = sink.records[0]
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.shadow_comparison.descriptor_provider_count == 0
    assert evidence.outcome == "error"
    assert evidence.execution_protection == "unprotected_error"


# ── unexpected _dispatch exception: identity re-raise + execution_error
def test_unexpected_dispatch_exception_reraised_by_identity(monkeypatch):
    boom = RuntimeError("dispatch boom")

    async def fake_dispatch(*a, **k):
        raise boom

    monkeypatch.setattr("wrr.router._dispatch", fake_dispatch)
    sink = SpySink()
    with pytest.raises(RuntimeError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    assert excinfo.value is boom                       # identical exception
    assert len(sink.records) == 1
    ev = sink.records[0]
    assert ev.terminal == "execution_error"
    assert ev.outcome == "error"
    assert ev.quality_verdict == "failed"


# ── evidence constructor failure preserves result ───────────────────
def test_evidence_constructor_failure_preserves_result(monkeypatch):
    def boom(*a, **k):
        raise ValueError("cannot build evidence")

    monkeypatch.setattr("wrr.router.DecisionEvidence", boom)
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    assert result.actual_provider == "rrf:grounding"
    assert result.payload
    assert result.diagnostics.decision_evidence is None
    assert sink.records == []


# ── synthetic empty payload → empty outcome ──────────────────────────
def test_synthetic_empty_payload_produces_empty_outcome(monkeypatch):
    async def empty_dispatch(registry, engine_names, options, weights, mode, budget):
        return [], [], []

    monkeypatch.setattr("wrr.router._dispatch", empty_dispatch)
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    ev = result.diagnostics.decision_evidence
    assert ev.outcome == "empty"
    assert ev.result_count == 0
    assert ev.terminal == "routed"
    assert len(sink.records) == 1


# ── planning raises: identity re-raise + one execution_error, mode/shadow None ─
def test_planning_exception_reraised_one_execution_error_mode_shadow_none(monkeypatch):
    boom = RuntimeError("planning boom")

    def fake_plan(options):
        raise boom

    monkeypatch.setattr("wrr.router.legacy_selection_plan", fake_plan)
    sink = SpySink()
    with pytest.raises(RuntimeError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            decision_context=_shadow_context(),
            shadow_evaluated_at=150.0,
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    assert excinfo.value is boom                        # identical exception object
    assert len(sink.records) == 1
    ev = sink.records[0]
    assert ev.terminal == "execution_error"
    assert ev.outcome == "error"
    assert ev.mode is None                              # no plan → mode None
    assert ev.shadow_comparison is None                # comparison never reached
    assert ev.quality_verdict == "failed"


def test_opt_in_v2_planning_error_records_comparison_failed(monkeypatch):
    boom = RuntimeError("planning boom")

    def fake_plan(_options):
        raise boom

    monkeypatch.setattr("wrr.router.legacy_selection_plan", fake_plan)
    provider = CachedDecisionContextProvider(_shadow_context)
    provider.refresh()
    sink = SpySink()

    with pytest.raises(RuntimeError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            decision_context_observation=provider.observe(),
            stage_s_enabled=True,
            decision_evidence_version=2,
            decision_evidence_sink=sink,
        ))

    assert excinfo.value is boom
    evidence = sink.records[0]
    assert type(evidence) is DecisionEvidenceV2
    assert evidence.context_status == "available"
    assert evidence.comparison_status == "comparison_failed"
    assert evidence.execution_protection == "unobservable"
    assert evidence.shadow_comparison is None


def test_planning_all_engines_error_is_execution_error(monkeypatch):
    boom = AllEnginesFailedError("planning failed before execution")

    def fake_plan(options):
        raise boom

    monkeypatch.setattr("wrr.router.legacy_selection_plan", fake_plan)
    sink = SpySink()
    with pytest.raises(AllEnginesFailedError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            _full_reg(),
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    assert excinfo.value is boom
    assert len(sink.records) == 1
    assert sink.records[0].terminal == "execution_error"
    assert sink.records[0].mode is None


def test_uuid_failure_only_disables_evidence(monkeypatch):
    def fail_uuid():
        raise RuntimeError("uuid unavailable")

    monkeypatch.setattr("wrr.router._uuid.uuid4", fail_uuid)
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    assert result.actual_provider == "rrf:grounding"
    assert result.payload
    assert result.diagnostics is not None
    assert result.diagnostics.decision_evidence is None
    assert sink.records == []


# ── expected all-fail exceptions carry NO injected metadata on the object ─────
def test_recovery_blocked_exception_has_no_injected_metadata(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: False)
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", search_results=[]),
        FakeEngine("brave", search_results=[]),
    )
    with pytest.raises(AllEnginesFailedError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    exc = excinfo.value
    assert not hasattr(exc, "_wrr_decision_terminal")
    assert "_wrr_decision_terminal" not in vars(exc)
    assert len(sink.records) == 1
    assert sink.records[0].terminal == "recovery_blocked"


def test_all_fail_exception_has_no_injected_metadata(monkeypatch):
    monkeypatch.setattr("wrr.router.config.recovery_allowed", lambda *a, **k: True)
    sink = SpySink()
    reg = _reg(
        FakeEngine("exa", error="down"),
        FakeEngine("brave", error="down"),
        FakeEngine("searxng", error="down"),
    )
    with pytest.raises(AllEnginesFailedError) as excinfo:
        run(route_search_v5(
            SearchOptions("what is python", count=5),
            reg,
            stage_s_enabled=True,
            decision_evidence_sink=sink,
        ))
    exc = excinfo.value
    assert not hasattr(exc, "_wrr_decision_terminal")
    assert "_wrr_decision_terminal" not in vars(exc)
    assert len(sink.records) == 1
    assert sink.records[0].terminal == "all_engines_failed"


# ── source guard: router source must never attach metadata to exceptions ─────
def test_router_source_forbids_wrr_decision_terminal():
    source = inspect.getsource(router_mod)
    assert "_wrr_decision_terminal" not in source


# ── no duplicate records on success ──────────────────────────────────
def test_no_duplicate_records_on_success():
    sink = SpySink()
    result = run(route_search_v5(
        SearchOptions("what is python", count=5),
        _full_reg(),
        decision_context=_shadow_context(),
        shadow_evaluated_at=150.0,
        stage_s_enabled=True,
        decision_evidence_sink=sink,
    ))
    assert len(sink.records) == 1
    assert sink.records[0] is result.diagnostics.decision_evidence
