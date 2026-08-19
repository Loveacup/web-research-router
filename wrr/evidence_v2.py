"""Pure Decision Evidence v2 projection.

The helper verifies in-memory context/comparison identity, then drops every raw
context identifier before constructing the v2 wire object. It performs no I/O,
configuration, routing, or evidence persistence.
"""
from __future__ import annotations

from wrr.runtime.decision_context_provider import DecisionContextObservation
from wrr.schemas import (
    DecisionContext,
    DecisionEvidence,
    DecisionEvidenceV2,
    ShadowComparison,
    ShadowComparisonEvidenceV2,
    _V2_PROJECTION_TOKEN,
)


def _validate_raw_comparison(comparison: ShadowComparison) -> None:
    if type(comparison.code) is not str or type(comparison.safe) is not bool:
        raise ValueError("raw shadow fields must use exact types")
    for field_name in (
        "legacy_provider_ids", "descriptor_provider_ids",
        "omitted_provider_ids", "added_provider_ids", "reasons",
    ):
        values = getattr(comparison, field_name)
        if type(values) is not tuple or any(type(value) is not str for value in values):
            raise ValueError("raw shadow fields must use exact types")
    if type(comparison.context_snapshot_version) is not str or type(
        comparison.config_fingerprint
    ) is not str:
        raise ValueError("raw shadow fields must use exact types")

    legacy = comparison.legacy_provider_ids
    descriptor = comparison.descriptor_provider_ids
    omitted = tuple(provider for provider in legacy if provider not in set(descriptor))
    added = tuple(provider for provider in descriptor if provider not in set(legacy))
    groups = (legacy, descriptor, comparison.omitted_provider_ids, comparison.added_provider_ids)
    if any(len(group) != len(set(group)) for group in groups):
        raise ValueError("invalid raw shadow comparison")
    if omitted != comparison.omitted_provider_ids or added != comparison.added_provider_ids:
        raise ValueError("invalid raw shadow comparison")
    if comparison.safe != comparison.code.startswith("E"):
        raise ValueError("invalid raw shadow comparison")

    if comparison.code == "E0":
        valid = legacy == descriptor and not omitted and not added and not comparison.reasons
    elif comparison.code == "E1":
        ordered = sorted(omitted, key=lambda provider: (-len(provider), provider))
        owners = set()
        for reason in comparison.reasons:
            owner = next(
                (provider for provider in ordered if reason.startswith(provider + ":")),
                None,
            )
            if owner is not None and len(reason) > len(owner) + 1:
                owners.add(owner)
        valid = (
            set(descriptor) < set(legacy)
            and not added and bool(omitted) and owners == set(omitted)
        )
    elif comparison.code == "E2":
        valid = (
            legacy != descriptor
            and set(legacy) == set(descriptor)
            and not omitted and not added
            and comparison.reasons == ("order_only_mismatch",)
        )
    elif comparison.code == "U1":
        valid = (
            not added and bool(omitted)
            and comparison.reasons == ("unexplained_omission",)
        )
    elif comparison.code == "U2":
        valid = bool(added) and comparison.reasons == ("unsafe_addition",)
    elif comparison.code == "U3":
        valid = comparison.reasons == ("nondeterministic_selection",)
    else:  # E3/U4 have no v2 policy owner.
        valid = False
    if not valid:
        raise ValueError("invalid raw shadow comparison")


def project_decision_evidence_v2(
    base: DecisionEvidence,
    observation: DecisionContextObservation,
    *,
    comparison_status: str,
    execution_protection: str,
) -> DecisionEvidenceV2:
    """Project trusted in-memory v1 fields into the privacy-bounded v2 wire."""

    if type(base) is not DecisionEvidence:
        raise TypeError("base must be an exact DecisionEvidence")
    if type(observation) is not DecisionContextObservation:
        raise TypeError("observation must be an exact DecisionContextObservation")
    if type(observation.status) is not str or (
        observation.cohort_id is not None
        and type(observation.cohort_id) is not str
    ):
        raise ValueError("observation status/cohort must use exact types")

    has_context = observation.context is not None
    has_cohort = observation.cohort_id is not None
    coherent = (
        (observation.status == "cold" and not has_context and not has_cohort)
        or (observation.status == "available" and has_context and has_cohort)
        or (observation.status == "refresh_failed" and has_context == has_cohort)
    )
    if not coherent:
        raise ValueError("incoherent decision context observation")
    if has_context:
        context = observation.context
        if type(context) is not DecisionContext or type(context.snapshot_version) is not str or type(
            context.config_fingerprint
        ) is not str:
            raise ValueError("observed context fields must use exact types")

    sanitized = None
    comparison = base.shadow_comparison
    context = observation.context
    if comparison_status == "compared":
        if type(comparison) is not ShadowComparison or context is None:
            raise ValueError("compared projection requires context and shadow comparison")
        _validate_raw_comparison(comparison)
        if comparison.context_snapshot_version != context.snapshot_version:
            raise ValueError("comparison snapshot does not match observed context")
        if comparison.config_fingerprint != context.config_fingerprint:
            raise ValueError("comparison config does not match observed context")
        sanitized = ShadowComparisonEvidenceV2(
            code=comparison.code,
            safe=comparison.safe,
            legacy_provider_count=len(comparison.legacy_provider_ids),
            descriptor_provider_count=len(comparison.descriptor_provider_ids),
            omitted_provider_count=len(comparison.omitted_provider_ids),
            added_provider_count=len(comparison.added_provider_ids),
            reasons_complete=True,
        )
    elif comparison is not None:
        raise ValueError("non-compared projection cannot carry shadow comparison")

    return DecisionEvidenceV2(
        request_key=base.request_key,
        recorded_at=base.recorded_at,
        context_status=observation.status,
        comparison_status=comparison_status,
        execution_protection=execution_protection,
        context_cohort_id=observation.cohort_id,
        mode=base.mode,
        terminal=base.terminal,
        outcome=base.outcome,
        actual_provider=base.actual_provider,
        result_count=base.result_count,
        quality_verdict=base.quality_verdict,
        route_elapsed_ms=base.route_elapsed_ms,
        shadow_comparison=sanitized,
        _projection_token=_V2_PROJECTION_TOKEN,
    )
