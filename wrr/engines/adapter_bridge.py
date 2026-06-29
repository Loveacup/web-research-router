"""Bridge v6 engine descriptors back to legacy SearchEngine instances."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib
from typing import Iterable

from wrr.engines.base import SearchEngine
from wrr.engines.registry import EngineDescriptor
from wrr.registry import EngineRegistry


DEFAULT_INTENTIONAL_GAPS: tuple[str, ...] = ()


class AdapterBridgeError(ValueError):
    """Raised when a descriptor cannot be mapped to a legacy adapter."""


@dataclass(frozen=True)
class LegacyRegistryBridgeReport:
    """Shadow report for comparing legacy providers with v6-backed adapters."""

    registry: EngineRegistry
    v5_provider_ids: tuple[str, ...]
    v6_descriptor_ids: tuple[str, ...]
    bridged_provider_ids: tuple[str, ...]
    documented_intentional_gaps: tuple[str, ...] = DEFAULT_INTENTIONAL_GAPS
    intentional_gap_ids: tuple[str, ...] = ()
    missing_provider_ids: tuple[str, ...] = ()
    unexpected_provider_ids: tuple[str, ...] = ()
    adapter_errors: dict[str, str] = field(default_factory=dict)

    @property
    def parity(self) -> bool:
        return not self.missing_provider_ids and not self.unexpected_provider_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "parity": self.parity,
            "v5_provider_ids": list(self.v5_provider_ids),
            "v6_descriptor_ids": list(self.v6_descriptor_ids),
            "bridged_provider_ids": list(self.bridged_provider_ids),
            "documented_intentional_gaps": list(self.documented_intentional_gaps),
            "intentional_gap_ids": list(self.intentional_gap_ids),
            "missing_provider_ids": list(self.missing_provider_ids),
            "unexpected_provider_ids": list(self.unexpected_provider_ids),
            "adapter_errors": dict(self.adapter_errors),
        }


def engine_from_descriptor(descriptor: EngineDescriptor) -> SearchEngine:
    """Instantiate the legacy adapter declared by a v6 descriptor."""

    adapter = descriptor.manifest.adapter
    if not adapter:
        raise AdapterBridgeError(f"{descriptor.id}: missing adapter")
    if not descriptor.adapter_load_allowed:
        raise AdapterBridgeError(f"{descriptor.id}: adapter load not allowed")

    adapter_cls = _load_adapter_class(adapter)
    engine = adapter_cls()
    if not isinstance(engine, SearchEngine):
        raise AdapterBridgeError(
            f"{descriptor.id}: adapter {adapter} did not create a SearchEngine"
        )
    return engine


def registry_from_descriptors(
    descriptors: Iterable[EngineDescriptor],
) -> tuple[EngineRegistry, dict[str, str]]:
    """Build a legacy registry from v6 descriptors and collect bridge errors."""

    registry = EngineRegistry()
    errors: dict[str, str] = {}
    for descriptor in descriptors:
        try:
            registry.register(engine_from_descriptor(descriptor))
        except AdapterBridgeError as exc:
            errors[descriptor.id] = str(exc)
    return registry, errors


def compare_legacy_registry_bridge(
    legacy_registry: EngineRegistry,
    descriptors: Iterable[EngineDescriptor],
    *,
    intentional_gaps: Iterable[str] = DEFAULT_INTENTIONAL_GAPS,
) -> LegacyRegistryBridgeReport:
    """Compare legacy v5 provider ids with the v6 descriptor-backed registry."""

    descriptor_tuple = tuple(descriptors)
    bridged_registry, adapter_errors = registry_from_descriptors(descriptor_tuple)

    v5_ids = tuple(legacy_registry.names())
    v6_ids = tuple(descriptor.id for descriptor in descriptor_tuple)
    bridged_ids = tuple(bridged_registry.names())
    documented = tuple(intentional_gaps)

    v5_set = set(v5_ids)
    bridged_set = set(bridged_ids)
    documented_set = set(documented)
    raw_missing = v5_set - bridged_set

    return LegacyRegistryBridgeReport(
        registry=bridged_registry,
        v5_provider_ids=v5_ids,
        v6_descriptor_ids=v6_ids,
        bridged_provider_ids=bridged_ids,
        documented_intentional_gaps=documented,
        intentional_gap_ids=tuple(sorted(raw_missing & documented_set)),
        missing_provider_ids=tuple(sorted(raw_missing - documented_set)),
        unexpected_provider_ids=tuple(sorted(bridged_set - v5_set)),
        adapter_errors=adapter_errors,
    )


def _load_adapter_class(adapter: str):
    module_name, separator, class_name = adapter.partition(":")
    if not separator or not module_name or not class_name:
        raise AdapterBridgeError(f"invalid adapter path: {adapter}")

    module = importlib.import_module(module_name)
    target = module
    for attr in class_name.split("."):
        target = getattr(target, attr)
    if not isinstance(target, type):
        raise AdapterBridgeError(f"adapter target is not a class: {adapter}")
    return target
