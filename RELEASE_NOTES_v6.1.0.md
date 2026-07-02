# web-research-router v6.1.0 (candidate / pre-release notes)

> Status: **candidate / pre-release**. These notes describe the EngineHealthPolicy
> work only and do not constitute a version-bump or publishing commitment.

## Summary

WRR v6.1 refines the v6 control plane with a formal **EngineHealthPolicy**: a
tiered health model that keeps the search hot path fast and side-effect free,
while confining live probing and bounded recovery to deep doctor runs.

## Scope

EngineHealthPolicy H1–H5 only. No router refactor, no legacy registry/deps
migration, no publishing, no gateway restart, no AI CLI Search.

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
- These release notes.

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
| CLI smoke | legacy doctor, v6 doctor, v6 deep doctor produce parseable output |

## Non-goals

- No `router.py` changes.
- No legacy registry / deps / runtime state migration.
- No package metadata bump or publishing.
- No gateway restart.
- No AI CLI Search.
- No broad recovery framework; recovery stays deep-doctor / `live_recovery` only.
