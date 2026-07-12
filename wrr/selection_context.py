"""Build immutable descriptor-selection context from production reports.

This module is control-plane only: it converts already-computed registry and bridge
reports into a DecisionContext. It does not discover, probe, bridge, route, or search.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter

from . import config
from .engines.adapter_bridge import LegacyRegistryBridgeReport
from .engines.registry import RegistryReport
from .schemas import DecisionContext


_SELECTION_CONFIG_CONSTANTS = (
    "GITHUB_TRIGGER",
    "COMMUNITY_TRIGGER_SITES",
    "PRACTICAL_KEYWORDS",
    "ACADEMIC_KEYWORDS",
    "SKILL_KEYWORDS",
    "LOCAL_MEMORY_KEYWORDS",
    "LOCAL_NOTES_KEYWORDS",
    "LOCAL_SESSION_KEYWORDS",
    "LOCAL_SCOPE_KEYWORDS",
    "RECOVERY_KEYWORDS",
    "BROAD_INTEREST_KEYWORDS",
    "RESEARCH_KEYWORDS",
    "DISCOVERY_KEYWORDS",
)


def build_decision_context(
    registry_report: RegistryReport,
    bridge_report: LegacyRegistryBridgeReport,
    *,
    runtime: str,
    profile: str,
    registry_source: str,
    snapshot_version: str,
    built_at: float,
    ttl_sec: float,
) -> DecisionContext:
    """Build a fail-closed DecisionContext from one registry/bridge observation.

    The bridge must have been built from exactly ``registry_report.routable``.
    This prevents resolve-only descriptors from being accidentally authorized.
    """
    if not math.isfinite(built_at):
        raise ValueError("built_at must be finite")
    if not math.isfinite(ttl_sec) or ttl_sec <= 0:
        raise ValueError("ttl_sec must be finite and greater than zero")

    resolved_ids = tuple(descriptor.id for descriptor in registry_report.resolved)
    if len(set(resolved_ids)) != len(resolved_ids):
        raise ValueError("registry_report contains duplicate descriptor ids")

    routable_ids = tuple(descriptor.id for descriptor in registry_report.routable)
    bridge_descriptor_ids = tuple(bridge_report.v6_descriptor_ids)
    if len(set(routable_ids)) != len(routable_ids):
        raise ValueError("registry_report.routable contains duplicate descriptor ids")
    if len(set(bridge_descriptor_ids)) != len(bridge_descriptor_ids):
        raise ValueError("bridge report contains duplicate descriptor ids")
    if Counter(bridge_descriptor_ids) != Counter(routable_ids):
        raise ValueError("bridge descriptors must match registry_report.routable")

    aliases = tuple(bridge_report.descriptor_provider_aliases)
    alias_descriptor_ids = tuple(descriptor_id for descriptor_id, _ in aliases)
    if len(set(alias_descriptor_ids)) != len(alias_descriptor_ids):
        raise ValueError("bridge aliases contain duplicate descriptor ids")
    if not set(alias_descriptor_ids) <= set(routable_ids):
        raise ValueError("bridge aliases must reference routable descriptors")
    adapter_error_ids = set(bridge_report.adapter_errors)
    if not adapter_error_ids <= set(routable_ids):
        raise ValueError("adapter errors must reference routable descriptors")
    if set(routable_ids) != set(alias_descriptor_ids) | adapter_error_ids:
        raise ValueError("every bridge descriptor must have alias or adapter error truth")
    successful_alias_providers = tuple(
        provider_id
        for descriptor_id, provider_id in aliases
        if descriptor_id not in adapter_error_ids
    )
    if Counter(successful_alias_providers) != Counter(bridge_report.bridged_provider_ids):
        raise ValueError("successful bridge aliases must match bridged provider ids")

    descriptor_reasons = []
    for descriptor in registry_report.resolved:
        if descriptor.routable:
            continue
        reasons = tuple(dict.fromkeys(
            (*descriptor.resolve_reasons, *descriptor.routable_reasons)
        ))
        descriptor_reasons.append(
            (descriptor.id, reasons or ("not_routable",))
        )

    expires_at = float(built_at + ttl_sec)
    if not math.isfinite(expires_at) or expires_at <= built_at:
        raise ValueError("expires_at must be finite and greater than built_at")

    return DecisionContext(
        snapshot_version=snapshot_version,
        built_at=float(built_at),
        expires_at=expires_at,
        runtime=runtime,
        profile=profile,
        registry_source=registry_source,
        routable_descriptor_ids=routable_ids,
        bridged_provider_ids=tuple(bridge_report.bridged_provider_ids),
        missing_provider_ids=tuple(bridge_report.missing_provider_ids),
        adapter_errors=tuple(sorted(bridge_report.adapter_errors.items())),
        descriptor_reasons=tuple(descriptor_reasons),
        descriptor_provider_aliases=aliases,
        config_fingerprint=_selection_config_fingerprint(),
    )


def _selection_config_fingerprint() -> str:
    trigger_constants = {
        name: getattr(config, name)
        for name in _SELECTION_CONFIG_CONSTANTS
    }
    payload = {
        "trigger_constants": trigger_constants,
        "mode_dispatch": {
            mode: list(providers)
            for mode, providers in sorted(config.MODE_DISPATCH.items())
        },
        "mode_weights": {
            mode: {
                provider: float(weight)
                for provider, weight in sorted(weights.items())
            }
            for mode, weights in sorted(config.MODE_WEIGHTS.items())
        },
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
