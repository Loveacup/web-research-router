"""引擎注册表 + 共享单例。"""
from typing import Dict, List, Optional

from .engines.base import SearchEngine
from .engines.exa import ExaEngine
from .engines.brave import BraveEngine
from .engines.searxng import SearxngEngine
from .engines.github import GitHubEngine
from .engines.community import CommunityEngine
from .engines.academic import AcademicEngine          # v5.0
from .engines.skill_discovery import SkillDiscoveryEngine  # v5.0
from .engines.local_supermemory import LocalSupermemoryEngine  # v5.2
from .engines.local_session import LocalSessionEngine          # v5.2
from .engines.local_qmd import LocalQmdEngine                  # v5.2
from .engines.local_obsidian import LocalObsidianEngine        # v5.2


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: Dict[str, SearchEngine] = {}

    def register(self, engine: SearchEngine) -> None:
        self._engines[engine.name] = engine

    def get(self, name: str) -> Optional[SearchEngine]:
        return self._engines.get(name)

    def names(self) -> List[str]:
        return list(self._engines.keys())

    def all(self) -> List[SearchEngine]:
        """返回所有已注册引擎实例。"""
        return list(self._engines.values())

    def doctor_targets(self) -> List[SearchEngine]:
        """返回 doctor 检查目标引擎列表。"""
        return self.all()


def default_registry() -> EngineRegistry:
    reg = EngineRegistry()
    reg.register(ExaEngine())
    reg.register(BraveEngine())
    reg.register(SearxngEngine())
    reg.register(GitHubEngine())
    reg.register(CommunityEngine())
    reg.register(AcademicEngine())          # v5.0
    reg.register(SkillDiscoveryEngine())    # v5.0
    reg.register(LocalSupermemoryEngine())  # v5.2 本地层
    reg.register(LocalSessionEngine())      # v5.2 本地层
    reg.register(LocalQmdEngine())          # v5.2 本地层
    reg.register(LocalObsidianEngine())     # v5.2 本地层
    return reg


def default_registry_v6_shadow(**kwargs):
    """Return a v6 descriptor-backed legacy registry parity report.

    This helper is opt-in shadow mode only. ``default_registry()`` remains the
    legacy source of truth for normal routing, doctor, and dependency behavior.

    When ``env=`` is omitted, mirrors doctor_v6/install: discovers manifests, computes
    required env names, filters process_env to those names, and passes the filtered
    overrides to load_env(). This ensures API keys supplied via process environment
    are visible to the v6 registry without leaking unrelated env vars.
    """
    import os
    from pathlib import Path

    from .engines.adapter_bridge import compare_legacy_registry_bridge
    from .engines.registry import EngineRegistry as V6EngineRegistry
    from .runtime.control_plane import prepare_control_plane_env
    from .runtime.detect import detect_runtime

    intentional_gaps = kwargs.pop("intentional_gaps", None)
    cwd = kwargs.pop("cwd", None)
    runtime = kwargs.pop("runtime", None) or detect_runtime(cwd=cwd)
    env = kwargs.pop("env", None)
    process_env = kwargs.pop("process_env", None)
    env_files = kwargs.pop("env_files", None)
    trust_project = kwargs.pop("trust_project", False)
    plugin_paths = kwargs.pop("plugin_paths", None)

    if env is None:
        if process_env is None:
            process_env = os.environ
        control = prepare_control_plane_env(
            runtime=runtime,
            cwd=cwd,
            process_env=process_env,
            env_files=env_files,
            plugin_paths=plugin_paths,
            include_builtin=True,
            trust_project=trust_project,
        )
        runtime = control.runtime
        env = control.env
        if plugin_paths is None:
            plugin_paths = control.plugin_paths

    registry_kwargs = dict(kwargs)
    if plugin_paths is not None:
        registry_kwargs["plugin_paths"] = plugin_paths
    if trust_project:
        registry_kwargs["trust_project"] = trust_project
    v6_registry = V6EngineRegistry(runtime=runtime, env=env, **registry_kwargs)
    # H2 policy: only bridge descriptors the routability evaluator marks routable
    # (auto-mode health: cached live or light/static fallback, never live probes),
    # not raw resolve() output.
    descriptors = v6_registry.routable()
    compare_kwargs = {}
    if intentional_gaps is not None:
        compare_kwargs["intentional_gaps"] = intentional_gaps
    return compare_legacy_registry_bridge(default_registry(), descriptors, **compare_kwargs)


_SHARED: Optional[EngineRegistry] = None


def get_registry() -> EngineRegistry:
    """进程内共享注册表（引擎构造无网络副作用，懒加载即可）。"""
    global _SHARED
    if _SHARED is None:
        _SHARED = default_registry()
    return _SHARED
