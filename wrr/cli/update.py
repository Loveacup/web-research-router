"""Dependency update planning for the WRR v6 control plane."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from wrr.engines.loader import EngineDiscovery, RepoRequirement, discover_engine_plugins


TRUST_REMOTE_CLONE_LEVELS = {"builtin", "user", "entry_point"}


@dataclass(frozen=True)
class ManagedRepoResult:
    engine_id: str
    repo: RepoRequirement
    path: Path
    action: str
    status: str
    dry_run: bool
    message: str
    trust_level: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "repo": self.repo.to_dict(),
            "path": str(self.path),
            "action": self.action,
            "status": self.status,
            "dry_run": self.dry_run,
            "message": self.message,
            "trust_level": self.trust_level,
        }


@dataclass(frozen=True)
class UpdateReport:
    dry_run: bool
    trust_project: bool
    repos: tuple[ManagedRepoResult, ...]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "trust": {"project": self.trust_project},
            "repos": [item.to_dict() for item in self.repos],
            "summary": dict(self.summary),
        }


def update(
    *,
    dry_run: bool = True,
    trust_project: bool = False,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    plugin_paths: Iterable[str | Path] | None = None,
    discoveries: Sequence[EngineDiscovery] | None = None,
) -> UpdateReport:
    resolved_cwd = Path.cwd() if cwd is None else Path(cwd)
    process_env = os.environ if env is None else env
    paths = tuple(plugin_paths or (resolved_cwd / "plugins" / "engines",))
    discovered = tuple(discoveries) if discoveries is not None else tuple(
        discover_engine_plugins(paths, include_builtin=True, trust_project=trust_project)
    )
    results: list[ManagedRepoResult] = []
    for discovery in discovered:
        manifest = discovery.manifest
        if not discovery.valid or manifest is None:
            continue
        for requirement in manifest.repo_requirements:
            results.append(
                install_managed_repo(
                    requirement,
                    engine_id=manifest.id,
                    trust_level=discovery.trust_level,
                    trust_project=trust_project,
                    dry_run=dry_run,
                    env=process_env,
                )
            )
    return UpdateReport(
        dry_run=dry_run,
        trust_project=trust_project,
        repos=tuple(results),
        summary=_summarize(results),
    )


def install_managed_repo(
    requirement: RepoRequirement,
    *,
    engine_id: str,
    trust_level: str,
    trust_project: bool = False,
    dry_run: bool = True,
    env: Mapping[str, str] | None = None,
) -> ManagedRepoResult:
    process_env = os.environ if env is None else env
    path = resolve_repo_path(requirement, env=process_env)
    exists = path.exists()
    action = "refresh" if exists else "clone"

    if action == "clone" and requirement.remote:
        allowed = trust_level in TRUST_REMOTE_CLONE_LEVELS or (
            trust_level == "project" and trust_project
        )
        if not allowed:
            return ManagedRepoResult(
                engine_id=engine_id,
                repo=requirement,
                path=path,
                action=action,
                status="refused",
                dry_run=dry_run,
                message="project_remote_clone_refused",
                trust_level=trust_level,
            )

    if dry_run:
        return ManagedRepoResult(
            engine_id=engine_id,
            repo=requirement,
            path=path,
            action=action,
            status="planned",
            dry_run=True,
            message=f"would_{action}",
            trust_level=trust_level,
        )

    if action == "clone":
        if not requirement.remote:
            return ManagedRepoResult(
                engine_id=engine_id,
                repo=requirement,
                path=path,
                action=action,
                status="skipped",
                dry_run=False,
                message="repo_remote_missing",
                trust_level=trust_level,
            )
        status, message = _run_git(["git", "clone", requirement.remote, str(path)])
    else:
        status, message = _run_git(["git", "-C", str(path), "fetch", "--tags", "--prune"])
        if status == "updated" and requirement.pin:
            status, message = _run_git(["git", "-C", str(path), "checkout", requirement.pin])

    return ManagedRepoResult(
        engine_id=engine_id,
        repo=requirement,
        path=path,
        action=action,
        status=status,
        dry_run=False,
        message=message,
        trust_level=trust_level,
    )


def resolve_repo_path(
    requirement: RepoRequirement,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    process_env = os.environ if env is None else env
    if requirement.env and process_env.get(requirement.env):
        return Path(str(process_env[requirement.env])).expanduser()
    if requirement.path:
        return Path(requirement.path).expanduser()
    if requirement.default_path:
        return _cache_overridden_path(requirement.default_path, env=process_env)
    cache_root = Path(process_env.get("WRR_CACHE_DIR") or "~/.cache/wrr").expanduser()
    return cache_root / "deps" / requirement.name


def _cache_overridden_path(raw_path: str, *, env: Mapping[str, str]) -> Path:
    expanded = Path(raw_path).expanduser()
    override = env.get("WRR_CACHE_DIR")
    if not override:
        return expanded
    raw = str(raw_path)
    if raw.startswith("~/.cache/wrr/"):
        suffix = raw.removeprefix("~/.cache/wrr/")
        return Path(override).expanduser() / suffix
    if raw == "~/.cache/wrr":
        return Path(override).expanduser()
    return expanded


def _run_git(args: list[str]) -> tuple[str, str]:
    try:
        completed = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "failed", str(exc)
    if completed.returncode == 0:
        return "updated", (completed.stdout or completed.stderr).strip()
    return "failed", (completed.stderr or completed.stdout).strip()


def _summarize(results: Sequence[ManagedRepoResult]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    status = "fail" if counts.get("failed") or counts.get("refused") else "ok"
    return {
        "status": status,
        "repos": len(results),
        "planned": counts.get("planned", 0),
        "updated": counts.get("updated", 0),
        "refused": counts.get("refused", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
    }
