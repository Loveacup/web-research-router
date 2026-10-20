"""Descriptor-to-adapter bridge and doctor registry contracts."""

from wrr.engines.adapter_bridge import (
    DEFAULT_INTENTIONAL_GAPS,
    engine_from_descriptor,
    registry_from_descriptors,
)
from wrr.engines.base import SearchEngine
from wrr.engines.registry import EngineRegistry as V6EngineRegistry
from wrr.registry import default_registry
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env


V5_PROVIDER_IDS = {
    "exa",
    "deepseek",
    "brave",
    "tavily",
    "searxng",
    "github",
    "community",
    "academic",
    "skill",
    "local_supermemory",
    "local_session",
    "local_qmd",
    "local_obsidian",
}


def _runtime(tmp_path):
    return detect_runtime(explicit="standalone", cwd=tmp_path, env={})


def _env(runtime):
    return load_env(runtime, overrides={}, env_files=[])


def _v6_descriptors(tmp_path):
    runtime = _runtime(tmp_path)
    registry = V6EngineRegistry(
        runtime=runtime,
        env=_env(runtime),
        include_builtin=True,
    )
    return registry.resolve()


def test_engine_from_descriptor_instantiates_legacy_adapter(tmp_path):
    descriptor = next(item for item in _v6_descriptors(tmp_path) if item.id == "exa")

    engine = engine_from_descriptor(descriptor)

    assert isinstance(engine, SearchEngine)
    assert engine.name == "exa"


def test_tavily_descriptor_discoverable_and_instantiates_legacy_adapter(tmp_path):
    """v6 builtin discovery exposes a Tavily descriptor whose adapter bridges
    cleanly to the legacy SearchEngine (name ``tavily``)."""
    descriptors = _v6_descriptors(tmp_path)

    descriptor = next(item for item in descriptors if item.id == "tavily")
    engine = engine_from_descriptor(descriptor)

    assert isinstance(engine, SearchEngine)
    assert engine.name == "tavily"
    # Tavily is a real provider, never a documented intentional gap.
    assert "tavily" not in DEFAULT_INTENTIONAL_GAPS


def test_bridge_registry_uses_legacy_provider_names(tmp_path):
    descriptors = _v6_descriptors(tmp_path)

    registry, errors = registry_from_descriptors(descriptors)

    assert errors == {}
    assert "qmd" in {descriptor.id for descriptor in descriptors}
    assert "local_qmd" in registry.names()
    assert "qmd" not in registry.names()


def test_default_registry_behavior_is_unchanged():
    registry = default_registry()

    assert set(registry.names()) == V5_PROVIDER_IDS
    assert {engine.name for engine in registry.all()} == V5_PROVIDER_IDS


