# web-research-router v6.0.0

## Summary

WRR v6.0.0 promotes web-research-router from a legacy Hermes search plugin into a versioned control plane for search routing, runtime/env detection, engine manifests, dependency health, and safe migration from the v5 mode/RRF route to a v6 descriptor-backed registry.

The live Hermes gateway rollout has completed through the default-switch stage: `WRR_V6_ROUTER=1` enables the v6 descriptor-backed registry as the active gateway path, while standalone/CLI compatibility remains preserved.

## Highlights

- **v6 control plane**
  - Runtime detection and env snapshot support.
  - Manifest discovery and `EngineDescriptor` registry.
  - v6 doctor JSON with `runtime`, `env`, `discovered`, `resolved`, `health`, `summary`, and `trust` sections.
  - v6 install/update planning commands with dry-run gates.

- **Dependency lifecycle and health gates**
  - Required dependencies affect engine `health` and `routable` state.
  - Repo revision / update / refresh-deps visibility.
  - Health state cache and TTL support.
  - Project plugin/env trust boundary with explicit `--trust-project`.

- **Migration bridge**
  - Descriptor-backed registry can bridge into the legacy execution path.
  - Legacy CLI and legacy `doctor --json` shape remain compatible.
  - v6 routing remains opt-in for standalone/CLI contexts, while Hermes gateway deployments can enable it through `WRR_V6_ROUTER=1`.

- **Packaging and Hermes plugin loading**
  - Package version: `6.0.0`.
  - Plugin version: `6.0.0`.
  - Console script: `wrr = wrr._cli:main`.
  - Wheel/sdist include `wrr/_cli.py` and all 11 builtin `engine.yaml` manifests.
  - Hermes directory plugin exports `register(ctx)` and registers `web_search`, `web_fetch`, and `web_similar` under the `wrr` toolset.

- **OpenCLI resilience**
  - Community/OpenCLI engine now performs daemon/extension readiness preflight.
  - Auto-restarts `opencli daemon` when possible.
  - Falls back cleanly instead of silently hanging on disconnected daemon/extension state.

## Validation evidence

Release checks run on 2026-07-01:

```bash
env -u WRR_V6_ROUTER python -m pytest tests/unit/ -q
# passed: full unit suite, one PytestUnknownMarkWarning for integration marker

uvx --from build pyproject-build --wheel --sdist
# Successfully built web_research_router-6.0.0-py3-none-any.whl
# Successfully built web_research_router-6.0.0.tar.gz
```

Artifact inspection:

```text
wheel_has_cli True
wheel_engine_yaml_count 11
console script: wrr = wrr._cli:main
sdist_has_cli True
sdist_engine_yaml_count 11
```

Internal release gates completed before tagging:

- Shadow A/B release gate: passed.
- Release matrix gate: passed.
- Live gateway staged rollout through default switch: passed with no release blocker.

## Compatibility notes

- Requires Python `>=3.10`.
- Standalone CLI v6 control-plane behavior remains opt-in via `--v6` where applicable.
- Legacy-compatible CLI examples and legacy `doctor --json` consumers are preserved.
- Hermes gateway default-switch is controlled by environment (`WRR_V6_ROUTER=1`) rather than a hard-coded package default.
- Test/CI environments that need legacy fake registry behavior should explicitly unset `WRR_V6_ROUTER`:

```bash
env -u WRR_V6_ROUTER python -m pytest tests/unit/ -q
```

## Known warnings / follow-up

- Build currently emits setuptools deprecation warnings for `project.license` table and license classifier. This is not a v6.0.0 blocker, but should be cleaned before 2027-02-18 by switching to a simple SPDX license expression.
- PyPI publishing is not included in this GitHub release and should go through a separate release publishing gate.
- AI CLI search engines are not part of v6.0.0; they remain v6.1 candidates requiring trust/sandbox/anti-recursion design.
