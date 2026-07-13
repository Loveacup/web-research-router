"""Same-source control-plane DecisionContext assembly for WRR v6.

This module wires one control-plane observation into a single immutable
``DecisionContext``. It prepares the control-plane environment exactly once,
builds a v6 ``EngineRegistry`` from that same preparation (no second discovery),
takes one ``report(health_mode="auto")``, captures its routable descriptors once,
bridges the injected legacy registry against exactly those routable descriptors,
and builds the context from that same report/bridge pair.

It performs no caching, routing, registration, background work, or live probing;
any failure in preparation/report/bridge/build propagates without a partial
context.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from wrr.engines.adapter_bridge import compare_legacy_registry_bridge
from wrr.engines.registry import EngineRegistry
from wrr.runtime.control_plane import prepare_control_plane_env
from wrr.runtime.state import DEFAULT_HEALTH_TTL_SEC
from wrr.schemas import DecisionContext
from wrr.selection_context import build_decision_context


def build_control_plane_decision_context(
    legacy_registry,
    *,
    profile: str = "default",
    ttl_sec: float = DEFAULT_HEALTH_TTL_SEC,
    clock=time.time,
    snapshot_version: str | None = None,
    runtime_hint: str | None = None,
    cwd: str | Path | None = None,
    process_env: Mapping[str, str] | None = None,
    env_files: Sequence[str | Path] | None = None,
    plugin_paths=None,
    trust_project: bool = False,
) -> DecisionContext:
    """Assemble one DecisionContext from a single same-source control observation."""
    control = prepare_control_plane_env(
        runtime_hint=runtime_hint,
        cwd=cwd,
        process_env=process_env,
        env_files=env_files,
        plugin_paths=plugin_paths,
        trust_project=trust_project,
    )

    registry = EngineRegistry(
        runtime=control.runtime,
        env=control.env,
        plugin_paths=control.plugin_paths,
        discoveries=control.discoveries,
        trust_project=trust_project,
    )

    report = registry.report(health_mode="auto")
    routable = report.routable

    bridge = compare_legacy_registry_bridge(legacy_registry, routable)

    return build_decision_context(
        report,
        bridge,
        runtime=control.runtime.name,
        profile=profile,
        registry_source=_registry_source(report),
        snapshot_version=_resolve_snapshot_version(snapshot_version),
        built_at=float(clock()),
        ttl_sec=ttl_sec,
    )


def _registry_source(report) -> str:
    """Stably summarize the source set of the report's discoveries (empty -> unknown)."""
    sources = sorted({discovery.source for discovery in report.discovered})
    return "+".join(sources) if sources else "unknown"


def _resolve_snapshot_version(snapshot_version: str | None) -> str:
    """Prefer an explicit non-empty value; otherwise mint a fresh unique one."""
    if snapshot_version:
        return snapshot_version
    return f"cp-{uuid.uuid4().hex}"
