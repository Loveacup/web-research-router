"""Control plane environment preparation for WRR v6."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from wrr.engines.loader import EngineDiscovery, discover_engine_plugins
from wrr.runtime.detect import RuntimeInfo, detect_runtime
from wrr.runtime.env import EnvSnapshot, load_env


@dataclass(frozen=True)
class ControlPlaneEnv:
    """Snapshot of control plane environment state."""

    runtime: RuntimeInfo
    plugin_paths: tuple[Path, ...]
    discoveries: tuple[EngineDiscovery, ...]
    required_env: frozenset[str]
    overrides: dict[str, str]
    env: EnvSnapshot


def prepare_control_plane_env(
    *,
    runtime: RuntimeInfo | None = None,
    runtime_hint: str | None = None,
    cwd: str | Path | None = None,
    process_env: Mapping[str, str] | None = None,
    env_files: Sequence[str | Path] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
    include_builtin: bool = True,
    trust_project: bool = False,
    discoveries: Iterable[EngineDiscovery] | None = None,
) -> ControlPlaneEnv:
    """
    Prepare unified control plane environment.

    Resolves runtime, discovers plugins (or reuses supplied discoveries),
    collects required env names, filters process env, loads final env snapshot.

    Args:
        runtime: Explicit RuntimeInfo; if None, detect from runtime_hint/cwd/env.
        runtime_hint: Runtime mode hint for detection ("editable", "standalone", etc.).
        cwd: Working directory; defaults to Path.cwd().
        process_env: Process environment dict; defaults to os.environ.
        env_files: Paths to .env files to load.
        plugin_paths: Plugin search paths; defaults to [cwd/plugins/engines].
        include_builtin: Whether to include built-in engines.
        trust_project: Trust project-local plugins and .env files.
        discoveries: Pre-discovered engine plugins; if provided, skips discovery.

    Returns:
        ControlPlaneEnv with resolved runtime, discoveries, filtered env.
    """
    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
    source_env = os.environ if process_env is None else process_env

    if runtime is None:
        runtime = detect_runtime(
            explicit=runtime_hint,
            cwd=resolved_cwd,
            env=source_env,
        )

    paths = (
        tuple(Path(p) for p in plugin_paths)
        if plugin_paths is not None
        else (resolved_cwd / "plugins" / "engines",)
    )

    discoveries_tuple = (
        tuple(discoveries)
        if discoveries is not None
        else tuple(
            discover_engine_plugins(
                paths,
                include_builtin=include_builtin,
                trust_project=trust_project,
            )
        )
    )

    required = required_env_names(discoveries_tuple)
    overrides = filter_env(source_env, required)
    env_snapshot = load_env(
        runtime,
        overrides=overrides,
        env_files=env_files,
        trust_project=trust_project,
    )

    return ControlPlaneEnv(
        runtime=runtime,
        plugin_paths=paths,
        discoveries=discoveries_tuple,
        required_env=required,
        overrides=overrides,
        env=env_snapshot,
    )


def required_env_names(discoveries: Iterable[EngineDiscovery]) -> frozenset[str]:
    """
    Collect all required environment variable names from discoveries.

    Traverses each discovery's manifest requirements.env, extracts primary
    names and aliases for required=True items.

    Args:
        discoveries: Engine discovery results.

    Returns:
        frozenset of required env var names (primary + aliases).
    """
    names: set[str] = set()
    for discovery in discoveries:
        manifest = discovery.manifest
        if not discovery.valid or manifest is None:
            continue
        for requirement in _env_requirements(manifest.requirements):
            if not bool(requirement.get("required", True)):
                continue
            names.update(_env_names(requirement))
    return frozenset(names)


def filter_env(env: Mapping[str, str], names: frozenset[str] | set[str]) -> dict[str, str]:
    """
    Filter environment dict to only include required names.

    Args:
        env: Source environment mapping.
        names: Allowed variable names.

    Returns:
        Dict with only keys present in both env and names.
    """
    return {name: env[name] for name in names if name in env}


def _env_requirements(requirements: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Extract env requirements list from manifest requirements."""
    env_requirements = requirements.get("env")
    if not isinstance(env_requirements, list):
        return []
    return [item for item in env_requirements if isinstance(item, Mapping)]


def _env_names(item: Mapping[str, Any]) -> list[str]:
    """Extract primary name and aliases from env requirement item."""
    primary = item.get("env") or item.get("name")
    names = [str(primary)] if primary else []
    aliases = item.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(str(alias) for alias in aliases if alias)
    return names
