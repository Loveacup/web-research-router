"""Install report generation for the WRR v6 control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from wrr.engines.loader import EngineDiscovery, discover_engine_plugins
from wrr.runtime.detect import RuntimeInfo, detect_runtime
from wrr.runtime.env import EnvSnapshot, load_env


@dataclass(frozen=True)
class InstallReport:
    dry_run: bool
    runtime: RuntimeInfo
    config_target: Path
    env: EnvSnapshot
    missing_required_env: tuple[dict[str, Any], ...]
    planned_writes: tuple[dict[str, Any], ...]
    trust_project: bool
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "runtime": self.runtime.to_dict(),
            "config_target": str(self.config_target),
            "env_candidates": [candidate.to_dict() for candidate in self.env.candidates],
            "missing_required_env": list(self.missing_required_env),
            "planned_writes": list(self.planned_writes),
            "trust": {"project": self.trust_project},
            "summary": dict(self.summary),
        }


def install(
    *,
    dry_run: bool = True,
    runtime_hint: str | None = None,
    trust_project: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    env_files: Sequence[str | Path] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
) -> InstallReport:
    """Return the v6 install plan.

    P0 is intentionally report-only. ``dry_run=False`` records the target and
    planned writes but still does not write files.
    """

    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
    process_env = os.environ if env is None else env
    runtime = detect_runtime(explicit=runtime_hint, cwd=resolved_cwd, env=process_env)
    paths = tuple(plugin_paths or (resolved_cwd / "plugins" / "engines",))
    discoveries = tuple(discover_engine_plugins(paths, include_builtin=True))
    required_env = _required_env(discoveries)
    snapshot = load_env(
        runtime,
        overrides=_filtered_env(process_env, required_env),
        env_files=env_files,
        trust_project=trust_project,
    )
    missing = _missing_required_env(discoveries, snapshot)
    config_target = Path.home() / ".config" / "wrr" / "config.yaml"
    planned_writes = ({"path": str(config_target), "action": "create_or_update_config"},)

    return InstallReport(
        dry_run=dry_run,
        runtime=runtime,
        config_target=config_target,
        env=snapshot,
        missing_required_env=tuple(missing),
        planned_writes=() if dry_run else planned_writes,
        trust_project=trust_project,
        summary={
            "status": "dry_run" if dry_run else "report_only",
            "writes_performed": 0,
            "discovered": len(discoveries),
            "missing_required_env": len(missing),
        },
    )


def _required_env(discoveries: Iterable[EngineDiscovery]) -> set[str]:
    names: set[str] = set()
    for discovery in discoveries:
        manifest = discovery.manifest
        if not discovery.valid or manifest is None:
            continue
        for requirement in _env_requirements(manifest.requirements):
            if not bool(requirement.get("required", True)):
                continue
            names.update(_env_names(requirement))
    return names


def _missing_required_env(
    discoveries: Iterable[EngineDiscovery],
    env: EnvSnapshot,
) -> list[dict[str, Any]]:
    missing: list[dict[str, Any]] = []
    for discovery in discoveries:
        manifest = discovery.manifest
        if not discovery.valid or manifest is None:
            continue
        for requirement in _env_requirements(manifest.requirements):
            if not bool(requirement.get("required", True)):
                continue
            names = _env_names(requirement)
            if not any(name in env.values for name in names):
                missing.append(
                    {
                        "engine_id": manifest.id,
                        "env": names[0] if names else None,
                        "aliases": names[1:],
                        "source": discovery.source,
                        "trust_level": discovery.trust_level,
                    }
                )
    return missing


def _env_requirements(requirements: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    env_requirements = requirements.get("env")
    if not isinstance(env_requirements, list):
        return []
    return [item for item in env_requirements if isinstance(item, Mapping)]


def _env_names(item: Mapping[str, Any]) -> list[str]:
    primary = item.get("env") or item.get("name")
    names = [str(primary)] if primary else []
    aliases = item.get("aliases", [])
    if isinstance(aliases, list):
        names.extend(str(alias) for alias in aliases if alias)
    return names


def _filtered_env(env: Mapping[str, str], names: set[str]) -> dict[str, str]:
    return {name: env[name] for name in names if name in env}
