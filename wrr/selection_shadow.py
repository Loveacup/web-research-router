"""Pure Stage-S descriptor-selection comparison.

This module performs no discovery, adapter loading, routing, search, telemetry, or I/O.
"""
from __future__ import annotations

from .schemas import (
    DecisionContext,
    DecisionSnapshot,
    DescriptorSelectionDecision,
    ShadowComparison,
)
from .selection import descriptor_selection_plan


class ShadowComparisonUnavailable(ValueError):
    """Raised when the supplied observation cannot produce a valid comparison."""

    _REASONS = frozenset({"context_expired", "context_mismatch"})

    def __init__(self, reason: str) -> None:
        if reason not in self._REASONS:
            raise ValueError("unsupported shadow unavailable reason")
        self.reason = reason
        super().__init__(reason)


def compare_shadow_selection(
    options,
    context: DecisionContext,
    *,
    evaluated_at: float,
    legacy_plan: DecisionSnapshot | None = None,
) -> ShadowComparison:
    """Evaluate twice and compare without changing execution."""

    decision = descriptor_selection_plan(options, context, evaluated_at=evaluated_at)
    if any(
        blocked_code == "legacy_plan_mismatch"
        for _, blocked_code, _ in decision.blocked
    ):
        raise ShadowComparisonUnavailable("context_mismatch")
    if decision.status == "expired":
        raise ShadowComparisonUnavailable("context_expired")
    repeated = descriptor_selection_plan(options, context, evaluated_at=evaluated_at)
    return compare_shadow_decisions(
        decision,
        repeated,
        expected_legacy_plan=legacy_plan,
    )


def compare_shadow_decisions(
    decision: DescriptorSelectionDecision,
    repeated: DescriptorSelectionDecision,
    *,
    expected_legacy_plan: DecisionSnapshot | None = None,
) -> ShadowComparison:
    """Classify two evaluations of one immutable selection observation.

    E3 is deliberately reserved: without an explicit versioned allowlist this
    comparator never emits it. U4 is an execution/fallback outcome and is not
    emitted by this pre-execution function.
    """

    effective_legacy = expected_legacy_plan or decision.legacy_plan
    legacy = effective_legacy.engine_names
    descriptor = decision.selected_provider_ids
    omitted = tuple(provider for provider in legacy if provider not in descriptor)
    added = tuple(provider for provider in descriptor if provider not in legacy)

    if decision != repeated or decision.legacy_plan != effective_legacy:
        code = "U3"
        safe = False
        reasons: tuple[str, ...] = ("nondeterministic_selection",)
    elif legacy == descriptor:
        code = "E0"
        safe = True
        reasons = ()
    elif not omitted and not added:
        code = "E2"
        safe = True
        reasons = ("order_only_mismatch",)
    elif not added:
        blocked_by_provider = {
            provider: (blocked_code, blocked_reasons)
            for provider, blocked_code, blocked_reasons in decision.blocked
        }
        justified = all(
            provider in blocked_by_provider
            and blocked_by_provider[provider][0] == "descriptor_blocked"
            and bool(blocked_by_provider[provider][1])
            for provider in omitted
        )
        if justified:
            code = "E1"
            safe = True
            reasons = tuple(
                f"{provider}:{reason}"
                for provider in omitted
                for reason in blocked_by_provider[provider][1]
            )
        else:
            code = "U1"
            safe = False
            reasons = ("unexplained_omission",)
    else:
        code = "U2"
        safe = False
        reasons = ("unsafe_addition",)

    return ShadowComparison(
        code=code,
        safe=safe,
        legacy_provider_ids=legacy,
        descriptor_provider_ids=descriptor,
        omitted_provider_ids=omitted,
        added_provider_ids=added,
        reasons=reasons,
        context_snapshot_version=decision.context_snapshot_version,
        config_fingerprint=decision.config_fingerprint,
    )
