"""P2-T3 dependency truth manifest bridge tests."""

from __future__ import annotations

from wrr.deps import (
    DEPENDENCY_MANIFEST,
    DepRegistry,
    DepType,
    builtin_manifest_legacy_deps,
    compare_manifest_bridge_to_legacy,
)
from wrr.engines.loader import discover_engine_plugins


def _by_id(deps):
    return {dep.id: dep for dep in deps}


def test_builtin_local_v5_providers_have_manifests():
    discoveries = discover_engine_plugins(include_builtin=True)
    ids = {item.engine_id for item in discoveries if item.valid}

    assert {"local_supermemory", "local_session", "local_obsidian"} <= ids


def test_manifest_bridge_does_not_drop_required_legacy_dependencies():
    report = compare_manifest_bridge_to_legacy()

    assert report.no_required_dependency_disappears is True
    assert report.required_missing_ids == ()
    assert set(_by_id(builtin_manifest_legacy_deps())) >= {
        dep.id for dep in DEPENDENCY_MANIFEST if dep.required
    }


def test_manifest_bridge_aligns_core_legacy_dependency_types():
    report = compare_manifest_bridge_to_legacy()
    alignment = report.type_alignment

    for dep_type in (
        DepType.ENV_VAR,
        DepType.GIT_REPO,
        DepType.CLI_TOOL,
        DepType.DOCKER,
        DepType.HERMES_TOOL,
    ):
        assert alignment[dep_type.value]["missing"] == ()

    assert alignment[DepType.PYTHON_PKG.value]["missing"] == ()
    assert report.intentional_type_gaps[DepType.PYTHON_PKG.value].startswith(
        "legacy enum exists"
    )


def test_manifest_derived_view_preserves_requiredness_for_v5_required_deps():
    manifest_deps = _by_id(builtin_manifest_legacy_deps())

    for legacy_dep in DEPENDENCY_MANIFEST:
        if legacy_dep.required:
            assert manifest_deps[legacy_dep.id].required is True


def test_default_legacy_deps_behavior_is_unchanged():
    registry = DepRegistry.get()

    assert len(DEPENDENCY_MANIFEST) == 13
    assert len(registry.all) == 13
    assert set(registry.all) == {dep.id for dep in DEPENDENCY_MANIFEST}
    assert "obsidian_vaults" not in registry.all
