"""Decision Evidence v2 schema/projection contracts (V2-3a)."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest

from wrr.evidence_v2 import project_decision_evidence_v2
from wrr.runtime.decision_context_provider import (
    CachedDecisionContextProvider,
    DecisionContextObservation,
)
from wrr.schemas import (
    DECISION_EVIDENCE_SCHEMA_VERSION,
    DecisionContext,
    DecisionEvidence,
    DecisionEvidenceV2,
    ShadowComparison,
    ShadowComparisonEvidenceV2,
)


def _context() -> DecisionContext:
    return DecisionContext(
        snapshot_version="low-entropy-secret",
        built_at=1.0,
        expires_at=2.0,
        runtime="standalone",
        profile="default",
        registry_source="test",
        routable_descriptor_ids=("exa",),
        bridged_provider_ids=("exa",),
        missing_provider_ids=(),
        adapter_errors=(),
        descriptor_reasons=(),
        descriptor_provider_aliases=(),
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )


def _shadow() -> ShadowComparison:
    return ShadowComparison(
        code="E0",
        safe=True,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=("exa",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )


def _base(shadow: ShadowComparison | None = None) -> DecisionEvidence:
    return DecisionEvidence(
        request_key="00000000-0000-4000-8000-000000000001",
        recorded_at="2026-08-19T00:00:00.000000Z",
        mode="grounding",
        terminal="routed",
        outcome="success",
        actual_provider="rrf:grounding",
        result_count=2,
        quality_verdict="complete",
        route_elapsed_ms=5.0,
        shadow_comparison=shadow,
    )


def _available_observation(
    context: DecisionContext | None = None,
) -> DecisionContextObservation:
    provider = CachedDecisionContextProvider(lambda: context or _context())
    provider.refresh()
    return provider.observe()


def test_compared_projection_emits_v2_with_only_opaque_cohort_identity():
    observation = _available_observation()

    evidence = project_decision_evidence_v2(
        replace(_base(_shadow()), actual_provider="credential:abcdef0123456789"),
        observation,
        comparison_status="compared",
        execution_protection="not_required",
    )
    payload = evidence.to_dict()
    rendered = json.dumps(payload, sort_keys=True)

    assert isinstance(evidence, DecisionEvidenceV2)
    assert DECISION_EVIDENCE_SCHEMA_VERSION == 1
    assert _base().to_dict()["schema_version"] == 1
    assert payload["schema_version"] == 2
    assert payload["context_status"] == "available"
    assert payload["comparison_status"] == "compared"
    assert payload["execution_protection"] == "not_required"
    assert payload["context_cohort_id"] == str(observation.cohort_id)
    assert "actual_provider" not in payload
    assert "context_snapshot_version" not in payload["shadow_comparison"]
    assert "config_fingerprint" not in payload["shadow_comparison"]
    assert "legacy_provider_ids" not in payload["shadow_comparison"]
    assert "descriptor_provider_ids" not in payload["shadow_comparison"]
    assert "reasons" not in payload["shadow_comparison"]
    assert "low-entropy-secret" not in rendered
    assert "credential-shaped-value" not in rendered
    assert "credential:abcdef0123456789" not in rendered


def test_cold_projection_is_unobservable_without_context_or_cohort():
    observation = DecisionContextObservation(None, "cold", None)

    evidence = project_decision_evidence_v2(
        _base(),
        observation,
        comparison_status="context_unavailable",
        execution_protection="unobservable",
    )
    payload = evidence.to_dict()

    assert payload["context_status"] == "cold"
    assert payload["comparison_status"] == "context_unavailable"
    assert payload["execution_protection"] == "unobservable"
    assert payload["context_cohort_id"] is None
    assert "shadow_comparison" not in payload


def test_projection_rejects_raw_context_mismatch_before_serialization():
    observation = _available_observation()
    shadow = _shadow()
    shadow = ShadowComparison(
        code=shadow.code,
        safe=shadow.safe,
        legacy_provider_ids=shadow.legacy_provider_ids,
        descriptor_provider_ids=shadow.descriptor_provider_ids,
        context_snapshot_version="different-secret",
        config_fingerprint=shadow.config_fingerprint,
    )

    with pytest.raises(ValueError, match="snapshot does not match"):
        project_decision_evidence_v2(
            _base(shadow),
            observation,
            comparison_status="compared",
            execution_protection="not_required",
        )


def test_v2_nested_u4_is_rejected_while_v1_default_remains_one():
    shadow = ShadowComparison(
        code="U4",
        safe=False,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=(),
        omitted_provider_ids=("exa",),
        reasons=("fallback_unprotected",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )
    observation = _available_observation()

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(shadow),
            observation,
            comparison_status="compared",
            execution_protection="unprotected_error",
        )

    assert DECISION_EVIDENCE_SCHEMA_VERSION == 1


def test_v2_raw_e3_is_rejected_until_a_versioned_policy_exists():
    shadow = ShadowComparison(
        code="E3",
        safe=True,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=("brave",),
        omitted_provider_ids=("exa",),
        added_provider_ids=("brave",),
        reasons=("policy_v1",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(shadow),
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )


@pytest.mark.parametrize(
    ("outcome", "count", "provider", "message"),
    [
        ("success", 0, "rrf:grounding", "success requires"),
        ("empty", 1, "rrf:grounding", "empty requires"),
        ("error", 1, None, "error requires"),
        ("error", 0, "rrf:grounding", "error requires"),
    ],
)
def test_v2_rejects_contradictory_outcome_rows(outcome, count, provider, message):
    base = DecisionEvidence(
        request_key="00000000-0000-4000-8000-000000000002",
        recorded_at="2026-08-19T00:00:00.000000Z",
        mode="grounding",
        terminal="routed",
        outcome=outcome,
        actual_provider=provider,
        result_count=count,
        quality_verdict="failed" if outcome == "error" else "complete",
        route_elapsed_ms=5.0,
        shadow_comparison=None,
    )

    with pytest.raises(ValueError, match=message):
        project_decision_evidence_v2(
            base,
            DecisionContextObservation(None, "cold", None),
            comparison_status="context_unavailable",
            execution_protection="unobservable",
        )


@pytest.mark.parametrize(
    ("outcome", "count", "provider", "protection"),
    [
        ("success", 2, "rrf:grounding", "protected_by_legacy"),
        ("empty", 0, "rrf:grounding", "unprotected_empty"),
        ("error", 0, None, "unprotected_error"),
    ],
)
def test_v2_empty_descriptor_protection_truth_table(outcome, count, provider, protection):
    shadow = ShadowComparison(
        code="E1",
        safe=True,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=(),
        omitted_provider_ids=("exa",),
        reasons=("exa:health:unhealthy",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )
    base = DecisionEvidence(
        request_key="00000000-0000-4000-8000-000000000003",
        recorded_at="2026-08-19T00:00:00.000000Z",
        mode="grounding",
        terminal="routed" if outcome != "error" else "all_engines_failed",
        outcome=outcome,
        actual_provider=provider,
        result_count=count,
        quality_verdict="failed" if outcome == "error" else "complete",
        route_elapsed_ms=5.0,
        shadow_comparison=shadow,
    )

    evidence = project_decision_evidence_v2(
        base,
        _available_observation(),
        comparison_status="compared",
        execution_protection=protection,
    )

    assert evidence.execution_protection == protection


def test_v2_rejects_cross_field_shadow_semantic_spoof():
    spoofed = ShadowComparison(
        code="E0",
        safe=True,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=("brave",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(spoofed),
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )


def test_v2_e1_projection_rejects_ambiguous_incomplete_raw_reasons():
    ambiguous = ShadowComparison(
        code="E1",
        safe=True,
        legacy_provider_ids=("a", "a:b", "exa"),
        descriptor_provider_ids=("exa",),
        omitted_provider_ids=("a", "a:b"),
        reasons=("a:b:blocked",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(ambiguous),
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )


def test_v2_rejects_noncanonical_cohort_uuid():
    with pytest.raises(ValueError, match="canonical UUIDv4"):
        project_decision_evidence_v2(
            _base(),
            DecisionContextObservation(
                _context(), "available", "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"
            ),
            comparison_status="comparison_failed",
            execution_protection="unobservable",
        )


def test_v2_rejects_shadow_subclass_that_could_override_wire_projection():
    class ShadowSubclass(ShadowComparisonEvidenceV2):
        def to_dict(self):
            return {"secret": "smuggled"}

    shadow = ShadowSubclass(
        code="E0",
        safe=True,
        legacy_provider_count=1,
        descriptor_provider_count=1,
    )
    observation = _available_observation()

    with pytest.raises(ValueError, match="factory-owned projection"):
        DecisionEvidenceV2(
            request_key="00000000-0000-4000-8000-000000000004",
            recorded_at="2026-08-19T00:00:00.000000Z",
            context_status="available",
            comparison_status="compared",
            execution_protection="not_required",
            context_cohort_id=observation.cohort_id,
            mode="grounding",
            terminal="routed",
            outcome="success",
            actual_provider="rrf:grounding",
            result_count=1,
            quality_verdict="complete",
            shadow_comparison=shadow,
        )


def test_v2_rejects_plain_caller_supplied_uuid_even_when_canonical():
    with pytest.raises(ValueError, match="factory-owned projection"):
        DecisionEvidenceV2(
            request_key="00000000-0000-4000-8000-000000000005",
            recorded_at="2026-08-19T00:00:00.000000Z",
            context_status="available",
            comparison_status="comparison_failed",
            execution_protection="unobservable",
            context_cohort_id="11111111-1111-4111-8111-111111111111",
            mode="grounding",
            terminal="routed",
            outcome="success",
            actual_provider="rrf:grounding",
            result_count=1,
            quality_verdict="complete",
        )


def test_v2_projection_rejects_extra_raw_reason_tokens():
    shadow = ShadowComparison(
        code="U3",
        safe=False,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=("exa",),
        reasons=(
            "nondeterministic_selection",
            "low-entropy-secret",
            "abcdef0123456789credential-shaped-value",
        ),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(shadow),
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )


def test_v2_serialization_revalidates_cohort_after_object_tampering():
    evidence = project_decision_evidence_v2(
        _base(_shadow()),
        _available_observation(),
        comparison_status="compared",
        execution_protection="not_required",
    )
    object.__setattr__(evidence, "context_cohort_id", "raw-provider:secret-reason")

    with pytest.raises(ValueError, match="canonical UUIDv4"):
        evidence.to_dict()


def test_v2_serialization_ignores_instance_to_dict_smuggling_and_revalidates_counts():
    evidence = project_decision_evidence_v2(
        _base(_shadow()),
        _available_observation(),
        comparison_status="compared",
        execution_protection="not_required",
    )
    shadow = evidence.shadow_comparison
    assert shadow is not None
    object.__setattr__(shadow, "to_dict", lambda: {"provider": "secret-provider"})

    payload = evidence.to_dict()

    assert payload["shadow_comparison"]["legacy_provider_count"] == 1
    assert "secret-provider" not in json.dumps(payload)

    object.__setattr__(shadow, "legacy_provider_count", -1)
    with pytest.raises(ValueError, match="non-negative int"):
        evidence.to_dict()


@pytest.mark.parametrize(
    "observation",
    [
        DecisionContextObservation(None, "available", "11111111-1111-4111-8111-111111111111"),
        DecisionContextObservation(_context(), "cold", None),
        DecisionContextObservation(None, "refresh_failed", "11111111-1111-4111-8111-111111111111"),
        DecisionContextObservation(_context(), "refresh_failed", None),
    ],
)
def test_v2_projection_rejects_incoherent_observation_tuple(observation):
    with pytest.raises(ValueError, match="incoherent decision context observation"):
        project_decision_evidence_v2(
            _base(),
            observation,
            comparison_status="comparison_failed",
            execution_protection="unobservable",
        )


def test_v2_shadow_counts_must_obey_set_cardinality_equation():
    with pytest.raises(ValueError, match="v2 shadow semantics"):
        ShadowComparisonEvidenceV2(
            code="U1",
            safe=False,
            legacy_provider_count=0,
            descriptor_provider_count=999,
            omitted_provider_count=1,
            added_provider_count=0,
        )


@pytest.mark.parametrize(
    ("code", "reasons", "protection"),
    [
        ("E2", (), "not_required"),
        ("E2", ("wrong",), "not_required"),
        ("E2", ("order_only_mismatch", "extra"), "not_required"),
        ("U1", (), "protected_by_legacy"),
        ("U1", ("wrong",), "protected_by_legacy"),
        ("U1", ("unexplained_omission", "extra"), "protected_by_legacy"),
        ("U2", (), "not_required"),
        ("U2", ("wrong",), "not_required"),
        ("U2", ("unsafe_addition", "extra"), "not_required"),
        ("U3", (), "not_required"),
        ("U3", ("wrong",), "not_required"),
        ("U3", ("nondeterministic_selection", "extra"), "not_required"),
    ],
)
def test_v2_projection_requires_exact_raw_reason_contract(code, reasons, protection):
    values = {
        "code": code,
        "safe": code.startswith("E"),
        "legacy_provider_ids": ("exa", "brave"),
        "descriptor_provider_ids": ("exa", "brave"),
        "omitted_provider_ids": (),
        "added_provider_ids": (),
    }
    if code == "E2":
        values["descriptor_provider_ids"] = ("brave", "exa")
    elif code == "U1":
        values.update({
            "legacy_provider_ids": ("exa",),
            "descriptor_provider_ids": (),
            "omitted_provider_ids": ("exa",),
        })
    elif code == "U2":
        values.update({
            "descriptor_provider_ids": ("exa", "brave", "rogue"),
            "added_provider_ids": ("rogue",),
        })
    shadow = ShadowComparison(
        **values,
        reasons=reasons,
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    with pytest.raises(ValueError, match="invalid raw shadow comparison"):
        project_decision_evidence_v2(
            _base(shadow),
            _available_observation(),
            comparison_status="compared",
            execution_protection=protection,
        )


@pytest.mark.parametrize(
    ("code", "reasons", "protection"),
    [
        ("E2", ("order_only_mismatch",), "not_required"),
        ("U1", ("unexplained_omission",), "protected_by_legacy"),
        ("U2", ("unsafe_addition",), "not_required"),
        ("U3", ("nondeterministic_selection",), "not_required"),
    ],
)
def test_v2_projection_accepts_exact_canonical_reason_contract(code, reasons, protection):
    values = {
        "code": code,
        "safe": code.startswith("E"),
        "legacy_provider_ids": ("exa", "brave"),
        "descriptor_provider_ids": ("exa", "brave"),
        "omitted_provider_ids": (),
        "added_provider_ids": (),
    }
    if code == "E2":
        values["descriptor_provider_ids"] = ("brave", "exa")
    elif code == "U1":
        values.update({
            "legacy_provider_ids": ("exa",),
            "descriptor_provider_ids": (),
            "omitted_provider_ids": ("exa",),
        })
    elif code == "U2":
        values.update({
            "descriptor_provider_ids": ("exa", "brave", "rogue"),
            "added_provider_ids": ("rogue",),
        })
    shadow = ShadowComparison(
        **values,
        reasons=reasons,
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )

    evidence = project_decision_evidence_v2(
        _base(shadow),
        _available_observation(),
        comparison_status="compared",
        execution_protection=protection,
    )

    assert evidence.to_dict()["shadow_comparison"]["code"] == code


def test_v2_serialization_rejects_string_subclass_enum_spoofing():
    class SpoofedMode(str):
        def __hash__(self):
            return hash("grounding")

        def __eq__(self, other):
            return other == "grounding"

    evidence = project_decision_evidence_v2(
        _base(_shadow()),
        _available_observation(),
        comparison_status="compared",
        execution_protection="not_required",
    )
    object.__setattr__(evidence, "mode", SpoofedMode("raw-provider:secret-reason"))

    with pytest.raises(ValueError, match="mode must be exact str"):
        evidence.to_dict()


def test_v2_projection_rejects_raw_reason_tuple_subclass_spoofing():
    class SpoofedReasons(tuple):
        def __eq__(self, _other):
            return True

    raw = ShadowComparison(
        code="U3",
        safe=False,
        legacy_provider_ids=("exa",),
        descriptor_provider_ids=("exa",),
        reasons=("nondeterministic_selection",),
        context_snapshot_version="low-entropy-secret",
        config_fingerprint="abcdef0123456789credential-shaped-value",
    )
    base = _base(raw)
    object.__setattr__(raw, "reasons", SpoofedReasons(("raw-provider:secret-reason",)))

    with pytest.raises(ValueError, match="raw shadow fields must use exact types"):
        project_decision_evidence_v2(
            base,
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )


def test_v2_projection_rejects_raw_context_string_subclass_spoofing():
    class SpoofedContext(str):
        def __eq__(self, _other):
            return True

        def __ne__(self, _other):
            return False

    raw = _shadow()
    object.__setattr__(
        raw,
        "context_snapshot_version",
        SpoofedContext("different-snapshot-secret"),
    )

    with pytest.raises(ValueError, match="raw shadow fields must use exact types"):
        project_decision_evidence_v2(
            _base(raw),
            _available_observation(),
            comparison_status="compared",
            execution_protection="not_required",
        )
