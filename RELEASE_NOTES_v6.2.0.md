# web-research-router v6.2.0

## Summary

WRR v6.2.0 adds **DeepSeek Search** as a 13th registered engine. DeepSeek opened
its in-house search index as a raw JSON API on 2026-10-17
(`POST https://api.deepseek.com/v1/tools/search`). The engine is wired as a
primary web source with the same RRF weight as Exa and runs in parallel with it
in every mode. No router, fusion, or schema changes; the change is additive.

## Scope

One new engine family + routing-table and documentation updates. No Fetch API
(`/v1/tools/fetch`) `extract` action, no retry policy, no v6 descriptor-runtime
changes beyond the new manifest.

## Changes

### Engine — `wrr/engines/deepseek.py`, `wrr/engines/builtin/deepseek/engine.yaml`
- `DeepSeekEngine` (`name="deepseek"`, tier 1, actions: `search`).
- Auth: `Authorization: Bearer $DEEPSEEK_API_KEY`; missing key raises
  `EngineError` before any request (same contract as Exa/Brave).
- Request: `{"query", "count", "need_content": false}`. Query is stripped and
  truncated to the API's 128-Unicode-character limit (INFO log); `count`
  clamped to 1–50.
- Errors follow the Brave pattern: HTTP 429 → `RateLimitError`, httpx timeout →
  `EngineTimeoutError`, other HTTP/transport errors → `EngineError`.
- Timeout: `config.DEEPSEEK_TIMEOUT` = `WRR_DEEPSEEK_TIMEOUT` (default 10 s),
  registered in `ENGINE_TIMEOUT`.
- Result mapping to `SearchResult`:
  - `snippet` ← `summary[:EXA_SEARCH_TEXT_MAX]` (500), falling back to the short
    `snippet` when `summary` is empty. Same length cap as Exa's `text`.
  - `highlights` ← `[]` (DeepSeek has no independent highlights field; the
    short snippet is **not** copied in).
  - `date` (RFC 3339 or null) → `source_ts`; `freshness_score` 1.0 when dated,
    0.5 (unknown) otherwise.
  - `rerank_score` / `authority` are not stored; logged at DEBUG only.
- `health_check()` reports `DEEPSEEK_API_KEY` presence with repair steps.

### Routing — `wrr/config.py`
- `MODE_DISPATCH`: `deepseek` inserted immediately after `exa` in all 8 modes
  (discovery, broad, grounding, research, academic, platform, recovery, local).
- `_W_DEFAULT["deepseek"] = 1.0`; `local` mode overrides to 0.3 (same as exa /
  brave).

### Registry — `wrr/registry.py`
- `default_registry()` registers `DeepSeekEngine()` right after `ExaEngine()`.

### Tests
- New `tests/unit/test_deepseek_payloads.py`: request payload, timeout, summary
  truncation, empty highlights, date → `source_ts`, null-date fallback, query
  truncation / count clamp, missing-key short-circuit.
- `tests/unit/test_plugin_entry.py`: `_r3_context` and the real-provider gate
  fixtures now model the 3-engine grounding set (`exa`, `deepseek`, `brave`);
  search-count assertions updated 6→9 and 8→12.

### Docs / metadata
- `README.md`: engine count 12→13, mode table, dependency table, v6.2.0 section.
- `plugin.yaml`, `pyproject.toml`, `__init__.py`, `wrr/__init__.py`: version
  6.1.2 → 6.2.0.
- `.env.example`: `DEEPSEEK_API_KEY` entry (with the local 1Password CLI
  retrieval command, no secret).
- `docs/architecture/*` diagrams are pinned to revision `22d65e7` (6.1.2) and
  were not regenerated.

## Operational notes
- Billing: $0.75 per 1,000 **successful** requests (HTTP 200 with non-empty
  `results`); empty results, failures, and timeouts are free. Rate limit 5 QPS
  per account. The router issues exactly one Search request per engine call.
- Cost impact: every web-mode search now adds one DeepSeek request alongside Exa.
- Per the official guide, 408 / 429 / 5xx are retryable; WRR does not retry
  (consistent with Exa / Brave).

## Verification
- `WRR_V6_ROUTER=0 .venv/bin/pytest tests/unit -o addopts= -q -p no:cacheprovider`
  — see commit message for the final count.
- Live smoke (`DEEPSEEK_API_KEY` from 1Password, MacBook clone): direct API call
  returned 3 results (`rerank_score` 2.33–2.40, `authority` 3–4, dated); router
  `grounding` search fused `exa` + `deepseek` results (see commit message).

## Upgrade
- Set `DEEPSEEK_API_KEY` in the shell / `~/.hermes/.env` /
  `~/.config/wrr/env.d/default.env`. Without it the engine fails closed per
  request (`EngineError`), is isolated by the router fan-out, and the other
  engines in the mode continue — identical to a missing `EXA_API_KEY`.
- `wrr-cli.py doctor --engine deepseek` reports key presence.
