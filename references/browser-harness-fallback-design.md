# Browser-Harness Fallback Design Gate (v6.x)

> **Status**: Slice 1 implemented. Adapter seam and disabled policy scaffold are committed; no real browser automation or fallback wiring.
> **Owner**: WRR Architecture Review
> **Seam placement**: CommunityEngine sub-source adapter, not `_fetch_opencli()` fallback.

## 1. Motivation

Current community search depends on `opencli <source> search` for sites like Reddit, Twitter, and Xiaohongshu. When OpenCLI is unavailable (daemon disconnected, extension not connected, site adapter missing), search gracefully reports failure at the community engine level — leaving behind a result gap instead of a usable fallback.

A future browser-harness fallback would fill this gap without introducing side effects on the search hot path.

## 2. Design Constraints (hard gates)

- **Hot path is side-effect free.** No browser launch, extension install, login, daemon restart, or credential extraction during `wrr search`.
- **Only deep doctor may do live recovery.** `wrr doctor --deep` is the single boundary for bounded `daemon restart`, live probes, and recovery attempts.
- **Failure isolation at the source level.** If BrowserHarness fails for one source (e.g., Reddit), other community sources continue.
- **No credential extraction.** BrowserHarness must never extract or persist API keys, tokens, or session cookies from browser state.
- **Domain allowlist.** Only predefined domains/sources; no open-ended browser access.

## 3. Adapter Seam

```python
class CommunitySourceAdapter(Protocol):
    """A single community source: opencli, last30days, or future browser-harness."""

    source: str

    async def search(
        self, query: str, limit: int, timeout: float
    ) -> list[dict]: ...

    async def health(self, deep: bool = False) -> EngineCheckResult: ...
```

| Adapter | Source | Transport | Status |
|---|---|---|---|
| `OpenCliSourceAdapter` | opencli | subprocess `opencli <source> search` | implemented |
| `Last30DaysSourceAdapter` | last30days | subprocess `python last30days.py --emit json` | implemented |
| `BrowserHarnessSourceAdapter` | browser_harness | browser-automation (future) | design-only / not implemented |

## 4. Routing and Fallback Chain

Fallback chains must distinguish source transports:

```
community:opencli:reddit       (normal path)
community:browser_harness:reddit  (fallback path)
community:opencli:twitter      … etc.
```

Not a flattened `community` label that hides the transport.

## 5. Policy Gates (design-only, not implemented)

- **Activation**: Only when manifest/doctor indicates `opencli daemon_disconnected` AND source-level policy allows.
- **Per-source timeout**: Must not drag down the whole community engine timeout budget.
- **Structured output normalization**: BrowserHarness outputs must conform to the same result shape as OpenCLI's `-f json`.
- **V2EX remains unsupported**: V2EX has no full-text search endpoint; browser-harness is not a substitute for non-existent API capability.

## 6. Test Plan (design gate, not functional tests)

- `test_browser_harness_not_in_hot_path`: assert no `BrowserHarnessSourceAdapter` or `browser_harness` string in search/routing hot-path files.
- `test_browser_harness_seam_is_sub_adapter`: assert `BrowserHarnessSourceAdapter` does not appear in `_fetch_opencli` or `_run_cmd`.
- `test_browser_harness_design_doc_exists`: assert this spec exists and contains key Interface/Adapter/Seam terms.

## 7. Non-Goals for v6.1

- No `BrowserHarnessSourceAdapter` implementation.
- No browser-automation dependency (Playwright, Puppeteer, Selenium).
- No `_fetch_opencli()` fallback wiring.
- No `community:opencli:* → community:browser_harness:*` auto-switch.
