"""Shared runtime/environment and search construction for in-process hosts and CLI."""
from __future__ import annotations

import os
from typing import Sequence

from wrr.runtime.detect import RuntimeInfo, detect_runtime
from wrr.runtime.env import EnvSnapshot, execution_env, load_env

from .contracts import Evidence


def prepare_environment(*, runtime: str | None = None, env_path: str | None = None,
                        trust_project: bool = False) -> tuple[RuntimeInfo, EnvSnapshot, dict[str, str]]:
    """Resolve one authorized snapshot without mutating the host environment."""
    overrides = dict(os.environ)
    identity = detect_runtime(explicit=runtime)
    selected = env_path or overrides.get("WRR_ENV")
    snapshot = load_env(identity, overrides=overrides,
                        env_files=[selected] if selected else None,
                        trust_project=trust_project)
    if selected and not any(candidate.loaded for candidate in snapshot.candidates):
        raise OSError("selected environment unavailable")
    return identity, snapshot, execution_env(snapshot, overrides=overrides)


async def search_evidence(query: str, *, count: int = 8, runtime: str | None = None,
                          env_path: str | None = None, trust_project: bool = False,
                          exa_mode: str | None = None,
                          sources: Sequence[str] | None = None) -> Evidence:
    """Run the same inventory, adapters, judge and policy for every local host."""
    from .inventory import build_inventory
    from .judge import resolve_judge
    from .pipeline import search
    from .sources import build_adapters

    identity, snapshot, env = prepare_environment(
        runtime=runtime, env_path=env_path, trust_project=trust_project,
    )
    inventory = build_inventory(runtime=identity, env=snapshot, trust_project=trust_project)
    adapters = build_adapters(inventory, env=env, exa_mode=exa_mode)
    judge = resolve_judge(env=env)
    return await search(query, inventory=inventory, adapters=adapters, judge=judge,
                        count=count, sources=sources)
