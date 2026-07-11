"""Offline descriptor selection planning.

This module consumes immutable observations only. It never discovers registries,
loads adapters, reads environment state, executes routes, or calls engines.
"""
from __future__ import annotations

from .router import legacy_selection_plan
from .schemas import (
    DecisionContext,
    DescriptorSelectionDecision,
    DecisionSnapshot,
)


def descriptor_selection_plan(
    options,
    context: DecisionContext,
    *,
    evaluated_at: float,
) -> DescriptorSelectionDecision:
    """Filter one legacy plan through an immutable descriptor context.

    The returned value is decision-only. ``executable`` describes whether the
    descriptor plan has a usable selection; this function never executes it.
    """
    legacy_plan = legacy_selection_plan(options)
    candidates = legacy_plan.engine_names

    requested_provider = getattr(options, "provider", None)
    if requested_provider != legacy_plan.explicit_provider:
        mismatched_provider = legacy_plan.explicit_provider or requested_provider or "unknown"
        return _decision(
            context,
            legacy_plan,
            executable=False,
            status="blocked",
            selected=(),
            selected_weights=(),
            blocked=((
                mismatched_provider,
                "legacy_plan_mismatch",
                (f"requested:{requested_provider or 'none'}",),
            ),),
            explicit_status="blocked",
        )

    if evaluated_at >= context.expires_at:
        expired_blocked: tuple[tuple[str, str, tuple[str, ...]], ...] = tuple(
            (provider, "context_expired", ()) for provider in candidates
        )
        return _decision(
            context,
            legacy_plan,
            executable=False,
            status="expired",
            selected=(),
            selected_weights=(),
            blocked=expired_blocked,
            explicit_status=("blocked" if legacy_plan.explicit_provider else None),
        )

    bridged = set(context.bridged_provider_ids)
    weight_by_provider = dict(legacy_plan.weights)
    selected: list[str] = []
    selected_weights: list[tuple[str, float]] = []
    blocked: list[tuple[str, str, tuple[str, ...]]] = []

    for provider in candidates:
        classification = _blocked_classification(provider, context)
        if classification is None and provider in bridged:
            selected.append(provider)
            if provider in weight_by_provider:
                selected_weights.append((provider, weight_by_provider[provider]))
            continue
        if classification is None:
            classification = ("not_bridged", ())
        code, reasons = classification
        blocked.append((provider, code, reasons))

    explicit_status = None
    if legacy_plan.explicit_provider:
        explicit_status = "selected" if selected else "blocked"
        status = explicit_status
    else:
        status = "selected" if selected else "empty"

    return _decision(
        context,
        legacy_plan,
        executable=bool(selected),
        status=status,
        selected=tuple(selected),
        selected_weights=tuple(selected_weights),
        blocked=tuple(blocked),
        explicit_status=explicit_status,
    )


def _blocked_classification(
    provider: str,
    context: DecisionContext,
) -> tuple[str, tuple[str, ...]] | None:
    adapter_errors = dict(context.adapter_errors)
    descriptor_reasons = dict(context.descriptor_reasons)
    descriptor_ids = _descriptor_ids_for_provider(provider, context)

    error_messages = tuple(
        adapter_errors[descriptor_id]
        for descriptor_id in descriptor_ids
        if descriptor_id in adapter_errors
    )
    if error_messages:
        return "adapter_error", tuple(dict.fromkeys(error_messages))

    reasons = tuple(sorted({
        reason
        for descriptor_id in descriptor_ids
        for reason in descriptor_reasons.get(descriptor_id, ())
    }))
    if reasons:
        return "descriptor_blocked", reasons

    if provider in context.missing_provider_ids:
        return "missing_provider", ()

    routable = set(context.routable_descriptor_ids)
    if not any(descriptor_id in routable for descriptor_id in descriptor_ids):
        return "descriptor_blocked", ("not_routable",)
    return None


def _descriptor_ids_for_provider(
    provider: str,
    context: DecisionContext,
) -> tuple[str, ...]:
    aliases = tuple(
        descriptor_id
        for descriptor_id, provider_id in context.descriptor_provider_aliases
        if provider_id == provider
    )
    return aliases or (provider,)


def _decision(
    context: DecisionContext,
    legacy_plan: DecisionSnapshot,
    *,
    executable: bool,
    status: str,
    selected: tuple[str, ...],
    selected_weights: tuple[tuple[str, float], ...],
    blocked: tuple[tuple[str, str, tuple[str, ...]], ...],
    explicit_status: str | None,
) -> DescriptorSelectionDecision:
    return DescriptorSelectionDecision(
        context_snapshot_version=context.snapshot_version,
        config_fingerprint=context.config_fingerprint,
        legacy_plan=legacy_plan,
        executable=executable,
        status=status,
        selected_provider_ids=selected,
        selected_weights=selected_weights,
        blocked=blocked,
        explicit_provider=legacy_plan.explicit_provider,
        explicit_provider_status=explicit_status,
        reasons=tuple(dict.fromkeys(item[1] for item in blocked)),
    )
