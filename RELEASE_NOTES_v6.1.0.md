# web-research-router v6.1.0

## Summary

WRR v6.1 refines the v6 control plane with a formal **EngineHealthPolicy**: a
tiered health model that keeps the search hot path fast and side-effect-aware,
while confining live probing and bounded recovery to deep doctor runs.

This release also includes final installed-CLI hardening found during real `wrr`
smoke testing: last30days JSON parsing, V2EX capability boundaries, and
OpenCLI/browser-backed community timeout stability.

## Scope

EngineHealthPolicy H1–H5 plus release-gate CLI hardening. No legacy registry/deps
migration, no gateway restart, no AI CLI Search.

## Highlights

### H1 — Engine health schema contract
- v6 doctor `health` reports a stable taxonomy:
  `unknown / healthy / degraded / unhealthy / disabled / cooldown`.
- Failure categories are surfaced so consumers can distinguish e.g.
  `daemon_disconnected` from a hard engine failure.

### H2 — Routable hot-path guard
- `search`, `routable()`, and `auto` routing read only static / light /
  cached-live health.
- Monkeypatch tests prove the hot path never triggers a live probe or a recovery
  restart. The shadow bridge uses `routable()` (policy-filtered) rather than
  `resolve()`.

### H3 — OpenCLI health policy
- Community OpenCLI engines use a stdout matcher for live health.
- `rc=0 + Extension: disconnected` maps to `daemon_disconnected` and is **not**
  routable; search will not attempt to repair it.
- Daemon preflight checks were removed from the search hot path.

### H4 — Deep doctor / live_recovery
- `doctor --v6 --deep` opts into live probes.
- When a manifest declares `health.recovery`, deep doctor performs a **bounded**
  recovery: status → restart once → status.
- A failed recovery opens the circuit / cooldown — no unbounded restart loop.
- Recovery is explicit (`--deep` / `live_recovery`), never general auto-healing.

### H5 — Docs / QA
- README gains a **v6.1 Engine Health Policy** section documenting light vs deep
  tiers, the hot-path red line, and OpenCLI disconnected remediation.
- Release notes and Obsidian/qmd evidence were updated with real smoke results.

### Installed CLI hardening
- `last30days` parsing now tolerates agent-facing CLIs that print human progress
  logs before the final JSON payload.
- Chinese `last30days` platform-array output (`weibo`, `xiaohongshu`, `bilibili`,
  `zhihu`, `wechat`, etc.) is mapped into WRR community results.
- V2EX is no longer advertised as a community full-text search source: OpenCLI
  exposes V2EX hot/latest/node/topic APIs, but not `search`. Auto routing now
  falls through to external web engines for `site:v2ex.com` queries.
- Community-triggered search gets a community-sized timeout budget so
  OpenCLI/browser-backed Reddit/XHS/Twitter paths are less flaky under release
  gate conditions.

## Final QA matrix

| Gate | Evidence |
|---|---|
| Schema contract | `doctor --v6 --json` contains `runtime/env/discovered/resolved/health/summary/trust` |
| Health taxonomy | tests assert `unknown/healthy/degraded/unhealthy/disabled/cooldown` + failure categories |
| Hot-path red line | monkeypatch tests prove auto/routable/search run no live probe or recovery |
| OpenCLI disconnected | `rc=0 + Extension: disconnected` → `daemon_disconnected`, not routable |
| Deep doctor recovery | `--deep` / `live_recovery` runs status → restart once → status |
| Recovery bounded failure | failed recovery opens circuit / cooldown, no infinite restart |
| Legacy compatibility | legacy `doctor --json` shape gains no v6 `health` top-level |
| Non-live regression | `tests/unit -k 'not openalex_live_single_source'` passes |
| Installed CLI smoke | `wrr` installed CLI round1–round5 passed after hardening |
| Package build | wheel/sdist build succeeds for `web-research-router==6.1.0` |

## Future roadmap note

OpenCLI browser-harness fallback is intentionally **not** part of v6.1. It is a
v6.x candidate for cases where OpenCLI site adapters are unavailable or have
capability gaps, and should get its own L1/L2 design gate.

A preliminary design gate spec defining the adapter seam (Interface / Adapter /
Seam), hot-path side-effect-free policy, and test plan exists at
`references/browser-harness-fallback-design.md`. The spec is a **design
contract** only; no implementation code references it at runtime.

## Non-goals

- No legacy registry / deps / runtime state migration.
- No gateway restart.
- No AI CLI Search.
- No broad recovery framework; recovery stays deep-doctor / `live_recovery` only.
