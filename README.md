# Web Research Router (WRR)

Semantic search router with mode-based routing, 11 engines, and Reciprocal Rank Fusion.

**Current version:** 6.1.1 — v6.1 Engine Health Policy is released and the v6 descriptor-backed router is the default path (`WRR_V6_ROUTER=1`). P1 control-plane hardening (profile matrix, diagnostics, recovery runtime gate, OpenCLI status strict matching, GitHub fast-mode dynamic switching, academic client reuse) is documented in `RELEASE_NOTES_v6.1.1.md`.

## Architecture

### Runtime modes

**Hermes plugin `web_search`** uses the v6 descriptor-backed registry when
`WRR_V6_ROUTER=1`, falling back to the v5 mode/RRF route when the variable is unset.

### v6 descriptor bridge (default as of v6.1)

v6.1 completed the S3 default switch: the descriptor-backed registry is the live
default path when `WRR_V6_ROUTER=1` (set in the launch environment). The legacy
v5 mode/RRF route remains available by unsetting `WRR_V6_ROUTER`.

### Agent-Reach provenance

Agent-Reach is the provenance and diagnostic reference for the OpenCLI daemon/extension stack.
Runtime community search depends on the **opencli binary + daemon + browser extension bridge**,
not on importing Agent-Reach code.

### 8 routing modes

| Mode | Use case | Engines |
|------|----------|---------|
| discovery | "what's out there" | exa + brave + github + community |
| grounding | "what's the fact" | exa + brave |
| research | deep investigation | exa (deep) + brave + academic |
| academic | papers only | openalex + semantic-scholar + arxiv |
| platform | platform/community-specific questions | github + community |
| broad | broad practical interest / exploratory queries | exa + brave + community |
| local | search my stuff | supermemory + session + qmd + obsidian |
| recovery | everything failed | searxng |

### 11 engines

- **Public-web (7):** Exa, Brave, GitHub, Community (OpenCLI), Academic (OpenAlex+Semantic Scholar+arXiv), Skill, SearXNG
- **Local (4):** Supermemory, Session, QMD, Obsidian

## Quick start

Requires Python >= 3.10 (`pyproject.toml` enforces this). On macOS, `/usr/bin/env python3` may resolve to Python 3.9; for direct script usage prefer a 3.10+ environment or call `python3.10 ./wrr-cli.py ...`.

```bash
# Install as Hermes plugin
ln -sf ~/code/web-research-router ~/.hermes/plugins/wrr-hermes

# Legacy-compatible CLI examples (run inside Python >=3.10)
./wrr-cli.py doctor          # 引擎 + 全量依赖自检
./wrr-cli.py doctor --json   # legacy JSON 输出，迁移窗口内 schema 保持不变
./wrr-cli.py search "your query" --provider exa --count 5
./wrr-cli.py fetch "https://example.com" --provider exa --max-chars 2000
./wrr-cli.py similar "https://example.com" --provider exa --count 5
wrr search "your query"      # Hermes runtime tool entrypoint
```

## Packaging & install

Three interchangeable entrypoints share one codebase at package version `6.1.1`:

```bash
# 1) pip install — exposes the `wrr` console script ([project.scripts] wrr = wrr._cli:main).
#    Verify the install with the v6 standalone runtime doctor:
pip install .
wrr doctor --v6 --json --runtime standalone

# 2) Direct script — still works without install, on any Python >= 3.10:
./wrr-cli.py doctor --json
./wrr-cli.py search "your query" --provider exa --count 5

# 3) Hermes plugin — plugin.yaml `entry: __init__.py` registers the wrr toolset;
#    plugin.yaml `version` is kept aligned with the package version (6.1.1).
ln -sf ~/code/web-research-router ~/.hermes/plugins/wrr-hermes
```

Notes:
- The v6 migration gate stays **opt-in**: pip/console install does not flip the
  default router to v6: legacy `doctor`/`search` behavior is unchanged unless you
  pass `--v6` (see below).
- Wheel verification should confirm the built-in engine manifests are packaged:
  `pyproject.toml` ships `engines/builtin/*/engine.yaml` via `[tool.*] package-data`
  (`wrr = ["engines/builtin/*/engine.yaml"]`). After `python -m build`, inspect the
  wheel (`unzip -l dist/*.whl | grep engine.yaml`) to ensure every builtin engine
  manifest is present before publishing.

## v6 CLI migration gate

v6.0 introduced the control-plane CLI; v6.1 completed the S3 default switch and the
Engine Health Policy. Legacy `doctor` behavior and old JSON consumers still work
when `WRR_V6_ROUTER` is unset.

```bash
# v6 doctor JSON: new shape with runtime/env/discovered/resolved/health/summary/trust
./wrr-cli.py doctor --v6 --json

# Deep health — live probes + bounded recovery for engines that declare it
./wrr-cli.py doctor --v6 --deep --json --runtime standalone
```

### v6.1 Engine Health Policy (released)

v6.1 splits engine health checks into two tiers so the search hot path stays fast
and side-effect free:

- **Light health (default).** `doctor --v6`, `routable()`, `auto` routing, and
  every `search` call read only static / light / cached-live health. They never
  run a live network probe and never restart a daemon. If a cached live result is
  absent, the engine is treated by policy, not re-probed on the hot path.
- **Deep health (`--deep`).** `doctor --v6 --deep` opts into live probes. When a
  manifest declares `health.recovery`, deep doctor may perform a **bounded**
  recovery (status → restart once → status). A failed recovery opens the circuit /
  cooldown; there is no unbounded restart loop. Recovery is *not* general
  auto-healing — it happens only under `--deep` / an explicit `live_recovery` mode.

**OpenCLI disconnected remediation.** A community OpenCLI engine reporting
`rc=0 + Extension: disconnected` maps to `daemon_disconnected` and is **not**
routable — search will not try to repair it. To recover, connect the Chrome /
OpenCLI extension, then re-run deep doctor to re-probe and (if configured) restart:

```bash
# Light health snapshot — no live probes, safe on the hot path
./wrr-cli.py doctor --v6 --json --runtime standalone

# Deep health — live probes + bounded recovery for engines that declare it
./wrr-cli.py doctor --v6 --deep --json --runtime standalone
```

### v6.x OpenCLI browser-harness fallback (Slice 1 implemented, not wired)

A future browser-harness fallback may fill community gaps when OpenCLI is unavailable.
Slice 1 (committed) introduces the source adapter seam (`wrr/engines/community_sources.py`)
and a disabled policy scaffold (`wrr/engines/community_policy.py`). No real browser
automation is wired into the search hot path; the fallback remains a v6.x candidate.

### v6.1.1 — P1 control-plane hardening (released)

- `wrr-cli.py doctor --v6 --profile-matrix --json` — per-profile readiness
  across `hermes` / `claude_code` / `codex` / `omp`; control-plane only;
  `--engine` / `--tier` / `--deep` rejected.
- `RouterResult.diagnostics` carries `RouteTrace` (mode / mode_reason /
  engines / events / timing) for every search call.
- `config.recovery_allowed(runtime)` gates the search recovery fallback.
  Default allowed: `hermes`, `claude_code`, `codex`, `omp`. Override with
  `WRR_RECOVERY_ALLOWED_RUNTIMES=hermes,omp,...`.
- `config.github_fast_mode(env_resolver=...)` — runtime-switchable fast-mode
  decision (no module reload required).
- OpenCLI daemon status now requires `daemon: running` + `extension: connected`;
  `not running` / `disconnected` are explicit failure markers.
- `AcademicEngine.search()` reuses one `httpx.AsyncClient` across OpenAlex /
  Semantic Scholar / arXiv.

See `RELEASE_NOTES_v6.1.1.md` for the full QA matrix.

## Dependencies (13 total)

Run `wrr-cli.py doctor` for self-check.

### Environment variables (4)

| ID | Source | Required |
|----|--------|----------|
| `exa_api_key` | [exa.ai](https://exa.ai) | ✅ |
| `brave_api_key` | [brave.com/search/api](https://brave.com/search/api/) | ✅ |
| `github_token` | [github.com/settings/tokens](https://github.com/settings/tokens) | ✅ |
| `searxng_url` | [github.com/searxng/searxng](https://github.com/searxng/searxng) | 可选 |

### Git repositories (4)

| ID | Source | Required |
|----|--------|----------|
| `last30days_en` | [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | ✅ |
| `last30days_cn` | [Jesseovo/last30days-skill-cn](https://github.com/Jesseovo/last30days-skill-cn) | ✅ |
| `paper_search_mcp` | [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp) | 可选 |
| `agent_reach` | [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) | 参考 |

### CLI tools (2)

| ID | Source | Required |
|----|--------|----------|
| `opencli` | [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) | ✅ |
| `qmd` | [github.com/qmd/qmd](https://github.com/qmd/qmd) | ✅ |

### Docker containers (1)

| ID | Source | Required |
|----|--------|----------|
| `searxng` | [github.com/searxng/searxng](https://github.com/searxng/searxng) | 可选 |

### Hermes built-in tools (2)

| ID | Source |
|----|--------|
| `supermemory` | [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com/docs) |
| `session_search` | [hermes-agent.nousresearch.com](https://hermes-agent.nousresearch.com/docs) |

## Testing

### Offline Stage-S evidence gate

```bash
wrr evidence-gate --path /path/to/decision-evidence.jsonl --json
wrr evidence-gate --path /path/to/decision-evidence.jsonl --mode grounding --json
```

The command is offline and read-only: `--path` is required, there is no default
live path, and it does not load `.env`, providers, runtime configuration, or
network clients. Exit codes are `0` for full readiness, `1` for a valid
`NOT_READY` report, and `2` for usage or file/output failure.

Input is bounded and fail-closed: regular files only, 64 MiB per file, 64 KiB
per line, 100,000 rows, and 128 items per provider/reason list. FIFO/socket
paths are opened non-blocking and rejected before evaluation.

Schema-v1 evidence cannot prove execution/fallback protection (`U4`), so its
full Stage-C `status` remains `NOT_READY` even when the observable
`selection_status` passes. The report is evidence for review, never an automatic
rollout authorization.

```bash
# Default environment uses v6 descriptor router.
# Use WRR_V6_ROUTER=0 for legacy registry tests with FakeEngine.
WRR_V6_ROUTER=0 PYTHONPATH=. pytest tests/unit -q

# Live OpenAlex integration is explicit and does not affect the unit verdict.
WRR_LIVE=1 PYTHONPATH=. pytest tests/integration/test_academic_live.py -q
```

| Gate | Command |
|---|---|
| Unit tests | `WRR_V6_ROUTER=0 pytest tests/unit -q` |
| OpenAlex live integration | `WRR_LIVE=1 pytest tests/integration/test_academic_live.py -q` |
| Installed CLI smoke | `wrr doctor --v6 --json --runtime standalone` |
| Deep health | `wrr doctor --v6 --deep --json --runtime standalone` |

## License

MIT
