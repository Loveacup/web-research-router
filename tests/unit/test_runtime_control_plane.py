"""Tests for wrr.runtime.control_plane unified env preparation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wrr.engines.loader import EngineDiscovery
from wrr.runtime.control_plane import (
    filter_env,
    prepare_control_plane_env,
    required_env_names,
)


def test_required_env_names_collects_required_primary_and_aliases(fake_discovery):
    """required_env_names extracts primary + aliases from required env items."""
    fake_discovery.manifest.requirements = {
        "env": [
            {"name": "GITHUB_TOKEN", "required": True},
            {"env": "EXA_API_KEY", "aliases": ["EXA_KEY"], "required": True},
            {"name": "OPTIONAL_VAR", "required": False},
        ]
    }
    discoveries = [fake_discovery]
    names = required_env_names(discoveries)
    assert names == frozenset({"GITHUB_TOKEN", "EXA_API_KEY", "EXA_KEY"})
    assert "OPTIONAL_VAR" not in names


def test_filter_env_only_allows_required_names():
    """filter_env returns only keys present in both env and names."""
    env = {
        "GITHUB_TOKEN": "ghp_abc",
        "EXA_API_KEY": "exa_xyz",
        "UNRELATED_SECRET": "should_not_leak",
        "PATH": "/usr/bin",
    }
    required = frozenset({"GITHUB_TOKEN", "EXA_API_KEY"})
    filtered = filter_env(env, required)
    assert filtered == {"GITHUB_TOKEN": "ghp_abc", "EXA_API_KEY": "exa_xyz"}
    assert "UNRELATED_SECRET" not in filtered
    assert "PATH" not in filtered


def test_prepare_control_plane_env_loads_filtered_process_env_without_leaking_unrelated_secret(
    tmp_path,
    fake_discovery,
):
    """prepare_control_plane_env filters process_env to required names only."""
    fake_discovery.manifest.requirements = {
        "env": [{"name": "WRR_REQUIRED_VAR", "required": True}]
    }
    process_env = {
        "WRR_REQUIRED_VAR": "visible_value",
        "GITHUB_TOKEN": "should_not_leak",
        "PATH": "/usr/bin",
    }
    control = prepare_control_plane_env(
        runtime_hint="standalone",
        cwd=tmp_path,
        process_env=process_env,
        discoveries=[fake_discovery],
        trust_project=True,
    )
    assert control.required_env == frozenset({"WRR_REQUIRED_VAR"})
    assert control.overrides == {"WRR_REQUIRED_VAR": "visible_value"}
    assert "GITHUB_TOKEN" not in control.overrides
    assert "PATH" not in control.overrides
    assert "WRR_REQUIRED_VAR" in control.env.values
    # Value is visible because trust_project=True allows secrets
    env_val = control.env.values["WRR_REQUIRED_VAR"]
    assert env_val.value == "visible_value"


def test_prepare_control_plane_env_reuses_supplied_discoveries(tmp_path, fake_discovery):
    """prepare_control_plane_env accepts pre-discovered plugins."""
    fake_discovery.manifest.id = "custom-engine"
    fake_discovery.manifest.requirements = {"env": []}
    control = prepare_control_plane_env(
        runtime_hint="standalone",
        cwd=tmp_path,
        process_env={},
        discoveries=[fake_discovery],
    )
    assert len(control.discoveries) == 1
    assert control.discoveries[0].manifest.id == "custom-engine"


@pytest.fixture
def fake_discovery():
    """Minimal valid EngineDiscovery for testing."""
    from unittest.mock import MagicMock

    from wrr.engines.loader import EngineDiscovery

    manifest = MagicMock()
    manifest.id = "fake-engine"
    manifest.requirements = {"env": []}

    discovery = EngineDiscovery(
        path=Path("/fake/plugin.py"),
        source="file",
        manifest=manifest,
        trust_level="builtin",
        valid=True,
        errors=(),
    )
    return discovery
