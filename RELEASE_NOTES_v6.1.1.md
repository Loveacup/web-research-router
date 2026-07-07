# web-research-router v6.1.1

## Summary

WRR v6.1.1 ships the P1 control-plane and runtime hardening items identified
after v6.1.0: per-profile doctor matrix, search/doctor diagnostic traces,
runtime-aware recovery gating, GitHub fast-mode dynamic switching, OpenCLI
status hardening, and academic HTTP client reuse. All changes are additive or
hardening; no legacy registry/deps/runtime state migration is required.

## Scope

P1 items: profile matrix, diagnostics, runtime gates, engine hardening. No v6.2
features, no new engine families, no broad recovery framework.

## Highlights

### P1-1 — `doctor --profile-matrix` (control-plane only)
- `wrr-cli.py doctor --v6 --profile-matrix --json` prints a per-profile
  readiness matrix across `hermes`, `claude_code`, `codex`, and `omp`.
- Profile evaluation runs only inside the doctor control plane; it does not
  trigger search, extract, live recovery, or daemon restarts.
- `--deep` is rejected; `--engine` and `--tier` are rejected under
  `--profile-matrix` after the concern fix.
- JSON output filters raw secrets and unrelated environment variables; only the
  required env names are emitted.

### P1-2 — search/doctor diagnostic traces
- `RouterResult` carries a `RouteTrace` with `mode`, `mode_reason`,
  `selected_engines`, per-engine `DiagnosticEvent`s, and elapsed/total budget ms.
- v6 `doctor --v6` reports include `state_file`, `health_cache_age_ms`, and
  `health_cache_expires_at` using a read-only cache-age accessor.
- JSON output preserves `diagnostics`; human output stays compact.

### P1-3 — `GITHUB_FAST_MODE` dynamic switching
- `config.GITHUB_FAST_MODE` is replaced by `config.github_fast_mode(env_resolver)`.
- Callers and tests can now flip the fast-mode decision at runtime without
  reloading the module or mutating the environment.

### P1-4 — GitHub activity lookup concurrency
- GitHubEngine activity lookup is bounded by `config.GITHUB_ACTIVITY_CONCURRENCY`
  and uses `asyncio.gather` with a semaphore.
- Fast mode still skips activity lookup entirely; slow mode is now
  concurrency-limited instead of unbounded.

### P1-5 — OpenCLI daemon status strict matching
- `_probe_opencli_status` now requires `daemon: running` and
  `extension: connected` in the status text; the previous substring match could
  have accepted `extension: not connected`.
- `not running` and `disconnected` markers are explicitly rejected.

### P1-6 — Academic engine client reuse
- `AcademicEngine.search()` now creates a single `httpx.AsyncClient` and passes it
  to OpenAlex, Semantic Scholar, and arXiv fetchers instead of opening one
  client per source per request.
- `_fetch_openalex`, `_fetch_s2`, and `_fetch_arxiv` keep backward-compatible
  optional `client` parameters.

### P1-7 — Recovery runtime gate
- `config.recovery_allowed(runtime_name)` decides whether the search router may
  run the recovery fallback after a primary-mode failure.
- Default allowed runtimes: `hermes`, `claude_code`, `codex`, `omp`.
  `standalone` and `unknown` are blocked by default to avoid surprise extra API
  usage in one-shot / cron-like contexts.
- `WRR_RECOVERY_ALLOWED_RUNTIMES` overrides the default set.
- When blocked, the router raises `AllEnginesFailedError` with
  `mode_reason="recovery_blocked"` instead of silently falling back.

## QA matrix

| Gate | Evidence |
|---|---|
| Profile matrix control-plane-only | `doctor_profile_matrix` never calls `_dispatch` / search / extract / recovery |
| Profile matrix CLI constraints | `--engine` and `--tier` rejected under `--profile-matrix` |
| Profile matrix secret filtering | JSON output excludes `GITHUB_TOKEN`/`AWS_SECRET`/`UNRELATED_VAR` |
| Diagnostic trace shape | `RouterResult.diagnostics` contains mode/mode_reason/engines/events/timing |
| Doctor cache age | `health_cache_age_ms` / `health_cache_expires_at` present without live probe |
| GitHub fast mode function | `config.github_fast_mode(env_resolver=...)` is runtime switchable |
| GitHub activity concurrency | semaphore limits to `GITHUB_ACTIVITY_CONCURRENCY` |
| OpenCLI status strict | `daemon: running` + `extension: connected` required |
| Academic client reuse | one `httpx.AsyncClient` per `AcademicEngine.search()` call |
| Recovery runtime gate | allowed runtimes recover; blocked runtimes raise `AllEnginesFailedError` |
| Unit test regression | `WRR_V6_ROUTER=0 pytest tests/unit -q` passes |
| Hot-path red line | `registry.py:default_registry()` and `deps.py` unchanged by P1 |

## Non-goals

- No v6.2 engine families.
- No legacy registry / deps / runtime state migration.
- No gateway restart.
- No broad auto-recovery; recovery remains gated by runtime and opt-in deep doctor.
