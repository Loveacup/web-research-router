"""Legacy registry bridge shadow-mode tests."""

from wrr.engines.adapter_bridge import (
    DEFAULT_INTENTIONAL_GAPS,
    engine_from_descriptor,
    registry_from_descriptors,
)
from wrr.engines.base import SearchEngine
from wrr.engines.registry import EngineRegistry as V6EngineRegistry
from wrr.registry import default_registry, default_registry_v6_shadow
from wrr.runtime.detect import detect_runtime
from wrr.runtime.env import load_env


V5_PROVIDER_IDS = {
    "exa",
    "brave",
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


def test_bridge_registry_uses_legacy_provider_names(tmp_path):
    descriptors = _v6_descriptors(tmp_path)

    registry, errors = registry_from_descriptors(descriptors)

    assert errors == {}
    assert "qmd" in {descriptor.id for descriptor in descriptors}
    assert "local_qmd" in registry.names()
    assert "qmd" not in registry.names()


def test_default_registry_v6_shadow_bridges_routable_descriptors(tmp_path):
    """H2: the shadow bridge consumes ``routable()``, not raw ``resolve()``.

    In an unconfigured standalone env only policy-routable descriptors (those with
    healthy light/static checks, no missing required env/binaries/repos) are
    bridged. Legacy providers that need API keys / binaries surface as ``missing``
    rather than being bridged.
    """

    runtime = _runtime(tmp_path)
    env = _env(runtime)
    state_file = tmp_path / "state.json"

    report = default_registry_v6_shadow(runtime=runtime, env=env, state_file=state_file)

    # Re-derive the expected routable policy output through the legacy bridge
    # naming rules (e.g. descriptor ``qmd`` -> legacy ``local_qmd``).
    v6_registry = V6EngineRegistry(
        runtime=runtime,
        env=env,
        include_builtin=True,
        state_file=state_file,
    )
    routable = v6_registry.routable()
    bridged_registry, bridge_errors = registry_from_descriptors(routable)
    expected_bridged = set(bridged_registry.names()) - set(DEFAULT_INTENTIONAL_GAPS)

    assert bridge_errors == {}
    assert set(report.v5_provider_ids) == V5_PROVIDER_IDS
    # Bridged providers equal the routable policy output, not all legacy providers.
    assert set(report.bridged_provider_ids) == expected_bridged
    # Routability filtered the set down: it is a strict subset of legacy providers.
    assert expected_bridged < V5_PROVIDER_IDS - set(DEFAULT_INTENTIONAL_GAPS)
    # Routable descriptors bridge cleanly; no adapter errors, nothing unexpected.
    assert report.adapter_errors == {}
    assert report.unexpected_provider_ids == ()
    # Non-routable legacy providers surface as missing (minus documented gaps).
    assert set(report.missing_provider_ids) == (
        V5_PROVIDER_IDS - expected_bridged - set(DEFAULT_INTENTIONAL_GAPS)
    )
    assert set(report.intentional_gap_ids) == (
        (V5_PROVIDER_IDS - expected_bridged) & set(DEFAULT_INTENTIONAL_GAPS)
    )
    # Parity now reflects routable-policy filtering rather than full legacy coverage.
    assert report.missing_provider_ids != ()
    assert report.parity is False
    assert report.to_dict()["parity"] is False


def test_default_registry_behavior_is_unchanged():
    registry = default_registry()

    assert set(registry.names()) == V5_PROVIDER_IDS
    assert {engine.name for engine in registry.all()} == V5_PROVIDER_IDS
