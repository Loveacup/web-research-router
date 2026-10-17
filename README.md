# Web Research Router (WRR)

Semantic search router with mode-based routing, 12 registered engines (11 routed + Tavily backfill), and Reciprocal Rank Fusion.

**Current version:** 6.1.2 — v6.1 Engine Health Policy is released. Production search still executes on the legacy v5 mode/RRF data plane (Stage S); the v6 descriptor registry participates only as a shadow selection and evidence source. The optional D7-S5 campaign sampler is packaged inside the plugin, disabled by default, and runs only when both `campaign.enabled` and `campaign_sampler.enabled` are explicitly true.

## Design scope and progress

For the confirmed cross-host direction, proposed shared core, OMP-first integration
with Hermes compatibility, tool/skill coordination, and maintenance/self-update
acceptance gates, see the [portable engineering design comparison](docs/unified-capability-maintenance-proposal.md).
Implementation is proceeding in bounded, independently reviewed slices; it is
not a released or fully accepted cross-host feature set. Obsidian L1/L2 remain
the requirements/design authority, with current evidence in the L3 rolling plan.
The next contract work also covers network-skill trigger coordination (Obsidian
MH-07 / NR-01–08): one collection path for a plain article, without implicit
browser use, delegation, or report files. This coordination is not implemented.

## Architecture

Interactive diagrams (generated with archify from the code at this revision): `docs/architecture/wrr-components.html` (component map) and `docs/architecture/wrr-web-search.html` (Hermes `web_search` request sequence). Sources: the `*.json` specs beside them.


### Runtime paths

| Entrypoint | Router | Registry used for execution |
|---|---|---|
| Hermes plugin `web_search` | v5 `route_search_v5` with Stage S on (`__init__.py` sets `stage_s_enabled=True`) | legacy `default_registry()`; `WRR_V6_ROUTER` does **not** change execution |
| `wrr search --route-mode <mode>` | v5 `route_search_v5`, Stage S off | legacy by default; v6 descriptor-backed registry when `WRR_V6_ROUTER=1` |
| `wrr search` / `fetch` / `similar` (no `--route-mode`) | v4 sequential `route()` — first non-empty engine wins, no RRF | legacy |

### v6 descriptor bridge (shadow, opt-in)

When Stage S is on (the packaged Hermes path) the v6 registry builds a
`DecisionContext` that is compared against the legacy selection plan
(`wrr/selection_shadow.py`). The comparison is recorded in `RouteTrace` and the
decision-evidence sink; it never changes which engines run. Only Stage-S-off
callers with `WRR_V6_ROUTER=1` execute through the descriptor-backed registry
(`wrr/router.py:_route_registry`). Stage C (descriptor-driven production
execution) is not enabled in this release.

### Agent-Reach provenance

Agent-Reach is the provenance and diagnostic reference for the OpenCLI daemon/extension stack.
Runtime community search depends on the **opencli binary + daemon + browser extension bridge**,
not on importing Agent-Reach code.

### 8 routing modes

Mode is chosen by `resolve_mode()`: explicit `route_mode` → legacy `mode` →
`classify_intent(query)` (keyword rules, no LLM; priority recovery → local →
broad → platform → academic → research → discovery → grounding). Base engines
and RRF weights come from `wrr/config.py` `MODE_DISPATCH` / `MODE_WEIGHTS`;
query triggers may append `github`, `skill`, `academic`, or `community`.

| Mode | Use case | Base engines (in order) | Notable weights |
|------|----------|-------------------------|-----------------|
| discovery | "what's out there" | exa, brave, community | community 0.50 |
| grounding | "what's the fact" | exa, brave | — |
| research | deep investigation | exa (`deep-lite`), brave, community, academic | community 0.35, academic 0.30 |
| academic | papers | academic (OpenAlex + Semantic Scholar; arXiv off by default), exa, community | academic 1.0 |
| platform | platform/community questions | community, exa, brave | community 1.0 |
| broad | exploratory queries | exa, brave, community, searxng | community 0.55, searxng 0.30 |
| local | search my stuff | supermemory, session, qmd, obsidian, exa, brave | local 1.0/0.9/0.9/0.8, web 0.3 |
| recovery | primary mode returned nothing | brave, exa, searxng | gated by `recovery_allowed(runtime)` |

### 12 engines

- **Public-web (8):** Exa, Brave, GitHub, Community (OpenCLI + Last30Days + RSS), Academic (OpenAlex + Semantic Scholar, optional arXiv), Skill, SearXNG, Tavily
- **Local (4):** Supermemory, Session, QMD, Obsidian

Tavily is registered but not in any mode; it is only used as a bounded diversity
backfill for `grounding` / `research` when Brave fails (rate-limit / timeout / empty).

## Quick start

Requires Python >= 3.11 (`pyproject.toml` enforces this). `./wrr-cli.py` selects a supported repository virtual environment or `python3.11+` on `PATH`; if none exists, it exits 126 with an actionable error.

```bash
# Install as Hermes plugin
ln -sf ~/code/web-research-router ~/.hermes/plugins/wrr-hermes

# Legacy-compatible CLI examples (requires Python >=3.11)
./wrr-cli.py doctor          # 引擎 + 全量依赖自检
./wrr-cli.py doctor --json   # legacy JSON 输出，迁移窗口内 schema 保持不变
./wrr-cli.py search "your query" --provider exa --count 5
./wrr-cli.py fetch "https://example.com" --provider exa --max-chars 2000
./wrr-cli.py similar "https://example.com" --provider exa --count 5
wrr search "your query" --route-mode research   # standalone CLI (wrr._cli:main); v5 mode routing needs --route-mode
```

### OMP native extension

Load the repository-owned adapter for one session; no OMP profile or skill entry
is changed:

```bash
omp --extension ./integrations/omp.mjs
```

It registers `wrr_search`, `wrr_fetch`, and `wrr_similar`, using the same Python
CLI and routing policy as the standalone entrypoint. Removing the extension
argument removes these tools from the next session. The adapter uses the repo
`.venv/bin/python`; `WRR_PYTHON` can explicitly select another interpreter.

Provider credential variables are not inherited automatically. After authorizing
specific keys for this invocation, list their names in `WRR_SHARED_ENV_KEYS`
(comma-separated); values stay in the subprocess environment. Alternatively,
`WRR_ENV` explicitly selects a credential file. Do not point it at another host's
private file without authorization.

Earlier bounded OMP session evidence recorded registration/unload, Exa
search→fetch, and cancellation of a controlled subprocess tree. That historical
evidence does not accept the current delivery or the later H3-S skill policy.
Current local checks cover the real Hermes loader/registry with a fixture
sentinel (not the builtin provider), and real handlers with a controlled catalog
subprocess and loopback HTTP. The OMP-side check uses the real adapter and CLI
with **fake pi/zod**, not a real OMP session: it does not verify host schema
normalization, dispatch, lifecycle or unload. These checks exercise per-request
private-origin denial/grant/denial and cancellation, not live providers or a
successful `similar` path. H3-S policy was not enabled. These are bounded local
checks, not full dual-host acceptance or repository-shipped JavaScript tests.

### Experimental OMP skill handoff (MH-04 H3-S)

The current [adapter](integrations/omp.mjs) contains a recommendation/observation
contract, not a skill executor. An absolute `WRR_SKILL_HANDOFF_POLICY` selects a
private schema-v1 policy with an allowlist, search query and isolated child
settings. `skill_handoff: "request"` requires `provider=skill`, `count=1`, and no
mode. A usable policy is required; acceptance only returns `read` with the exact
`skill://NAME` target. A matching tool-call ID, successful read result and hash
of `details.resolvedPath` are needed before a host report is accepted. This is a
**path-string hash**, not a skill-package/content attestation. Reports are
unverified `received` / `declined` / `aborted` statements, never execution proof.

`WRR_SKILL_HANDOFF_ENFORCE=1` additionally enables the private tool-call gate:
one exact policy query per process (not reset on session changes), permitted
handoff transitions and the bound skill read; other tool calls are blocked.
Requesting enforcement without a usable policy blocks tool calls rather than
silently falling back. A policy alone does not enable that enforcement gate.
The skill state is bounded by a five-minute deadline and 32-entry capacity;
only one accepted/read-active entry is allowed. Session start/switch/branch/tree/
shutdown events clear skill state/timers/reservations and abort in-flight skill
searches. Those searches use a policy-selected interpreter, filtered environment,
credential fd 3 and a non-detached child; this is not a general OS sandbox.

H3-S remains experimental and **not accepted in a real host**. Its event hooks
and policy checks are source contracts, not proof of actual OMP enforcement.
The historical H2 HOLD remains unchanged; the separate fetch handoff below
remains BLOCKED. No policy/credential payload or activation authorization is
provided by this documentation.

### Explicit configured community sources

`WRR_COMMUNITY_CONFIG` selects one absolute, operator-owned JSON file that is
not group/world writable. Unset means no additional source discovery or host
credential lookup. Configured sources use the existing Community engine and
`search --provider community`; they do not add another router.

The root fields are `schema_version: 1`, `sources`, `default_sources`, and
`authorized_sources`. `sources` maps custom, non-builtin IDs to:

- `kind`: `opencli` or `mcp`.
- `command`: literal argv, beginning with an absolute executable path.
- `shared_env_keys`: explicitly allowed environment variable names; empty by default.
- Optional existing result-field mappings: `title`, `url`, `snippet`,
  `engagement`, `comments`, `time`, and positive finite `eng_max`.
- MCP additionally requires `tool`; `query_arg` and `count_arg` default to
  `query` and `count`, with optional fixed JSON `arguments`.

Configuration does not grant execution: the source ID must also appear in
`authorized_sources`. `default_sources` selects configured IDs only for the
existing default-community selection branch. Explicit platform routing remains
unchanged. Model query arguments cannot change argv or grant a source.

MCP requires the optional `mcp` package extra (`pip install '.[mcp]'`) and uses
the official SDK 2.x over stdio. The exact configured tool must be listed and
declare `readOnlyHint=true`; that declaration alone is not an execution grant.
Configured commands run per request in temporary HOME/cwd directories with
filtered environments. This is credential isolation, not an OS sandbox against
an authorized executable. Process-group supervision is POSIX-only.

OMP forwards the explicit config path. For an environment value to reach a
configured child through OMP, authorize its name in both `WRR_SHARED_ENV_KEYS`
and the source's `shared_env_keys`. Neither config files nor diagnostics should
contain secret values. OMP permits eight seconds for configured-source teardown;
the unconfigured path retains its existing grace period.

The controlled MCP/OpenCLI fixtures validate protocol and CLI contracts, not
live platform/browser access or production Hermes deployment. Protocol
acceptance remains subject to the current WRR L3 verdict; a passing unit suite
alone is not full multi-host acceptance.

### Fetch content evidence

Fetch results expose `content_kind`, `content_type`, `completeness`,
`extraction_method`, `fetched_at`, and `partial` (CLI `result`; Hermes
`details.extraction`). Provider success is not proof of full-page completeness.
Locally clipped bodies are `truncated`; unattested upstream completeness remains
`unknown`. Both are partial and lower the overall quality to `degraded` without
automatically collecting a second body.

Empty bodies, recognized challenge pages, and explicit summaries are rejected
with a readable fallback reason. An explicit provider still disables fallback.
Exa reports `exa.contents`; the existing Brave extraction fallback reports its
actual method, `httpx.html_tag_strip`, rather than claiming professional article
extraction. This content contract does not establish SSRF or redirect safety.

### Direct HTTP fetch authorization

The Brave extraction path now rejects non-public destinations by default.
It validates DNS answers, pins an address for each original origin, and checks
every redirect. Original origins own separate HTTP pools, preserving Host/TLS
identity even when different hostnames resolve to the same IP. Environment
proxies are not inherited by this direct transport.

For a currently authorized private target, explicitly select `brave` and pass
`--allow-private-origin SCHEME://HOST:PORT` on that invocation. Hermes/OMP expose
the corresponding `allow_private_origin` argument. The grant must match the
target's exact origin; it does not authorize redirects to another origin and is
never saved or inferred from a URL. Omitting it on the next call denies private
access again. Policy denial terminates the chain rather than trying another
collector.

If the system resolver returns fake-IP answers (for example Surge/Clash
enhanced mode, `198.18.0.0/15`), every public page is rejected as non-public.
Set `WRR_FETCH_PROXY=http://HOST:PORT` in WRR's own env file to opt into proxy
mode: names are resolved through DNS-over-HTTPS via that proxy
(`WRR_FETCH_DOH_URL`, default `https://1.1.1.1/dns-query`), every answer must
pass the same public check, and the proxy is asked to `CONNECT` to the checked
IP literal while TLS still verifies the original hostname. Private-origin grants
never use the proxy.

This is the local HTTP executor's boundary, not proof of Exa's remote DNS or
redirect policy. A local-network grant cannot be passed to an implicit or remote
executor. Full isolated Hermes runtime acceptance remains pending.

### Experimental OMP host fetch handoff — blocked

The OMP extension contains an opt-in `host_fallback: "request"` handoff and a
`wrr_host_fetch_handoff` return tool. WRR does not invoke native `read`: it can
recommend a one-hop target, the host explicitly accepts and runs `read`, then
returns content with the same ephemeral correlation ID. Returned content is
always `content_kind=unknown`, completeness unknown, and quality `degraded`
with `host_return_unverified`; model-mediated metadata cannot attest a complete
body.

The provider-ordering and no-opt-in compatibility defects found in the first
review are repaired: target eligibility runs before provider/credential checks
only for explicit requests, and its result remains internal to the OMP adapter.
Cycle 2 also closes the WRR-observed terminal-denial defect: a terminal
`FetchPolicyError` is preserved as a closed typed reason, emitted only for an
explicit host-fallback metadata request, and vetoed by the OMP adapter before
handoff state or a correlation ID is created. Omitting `host_fallback` preserves
the existing CLI/tool result shape.

This still is **not an accepted capability**. One separate authorization
boundary remains: eligibility validates one URL/DNS snapshot, while native
`read` later receives the original hostname and independently resolves it and
follows redirects. The bounded WRR fix neither controls nor proves that
host-native DNS/redirect enforcement.

Independent contract and safety reconciliation rejected the separate summary
concern as a blocker under the frozen trust amendment: returned text remains
`content_kind=unknown`, incomplete, partial, degraded, and
`host_return_unverified`, so `execution_success=true` does not attest body or
complete quality. Changing summary classification requires a prior contract
amendment and a trusted classification source.

No dangerous target was executed, and this does not claim that OMP `read`
actually permits one. The bounded cycle-2 evidence is recorded in
`E/mh02-cycle2-final-verification.json` and the independent final contract and
security reviews in the private L3 evidence root. Complete MH-02 and Hermes
acceptance remain blocked.

## Packaging & install

Three interchangeable entrypoints share one codebase at package version `6.1.2`:

```bash
# 1) pip install — exposes the `wrr` console script ([project.scripts] wrr = wrr._cli:main).
#    Verify the install with the v6 standalone runtime doctor:
pip install .
wrr doctor --v6 --json --runtime standalone

# 2) Direct script — bootstraps a repository venv or any Python >= 3.11:
./wrr-cli.py doctor --json
./wrr-cli.py search "your query" --provider exa --count 5

# 3) Hermes plugin — plugin.yaml `entry: __init__.py` registers the wrr toolset;
#    plugin.yaml `version` is kept aligned with the package version (6.1.2).
ln -sf ~/code/web-research-router ~/.hermes/plugins/wrr-hermes
```

Notes:
- CLI and Hermes search now use the same v5 intent/mode routing by default;
  explicit `--provider` still selects only that provider. `--route-mode` sets
  WRR policy, while `--exa-mode` (and its legacy `--mode` alias) sets Exa's mode.
  This policy alignment is not proof of completed OMP integration or credential isolation.
- CLI credentials come from the existing process environment, then one selected
  file: `--env`, `WRR_ENV`, or `~/.config/wrr/env.d/default.env`, in that order.
  Existing process values win. WRR no longer probes `~/.hermes/.env` implicitly;
  sharing that file requires explicitly selecting it. A missing/unreadable
  selected file exits with code 2 and never falls back to another credential file.
- `wrr install` remains report-only and requires `--dry-run`. The Python
  `wrr.cli.install.install()` report API also only plans dependency refreshes,
  even with `dry_run=False, refresh_deps=True`; it never clones, fetches, or
  checks out dependencies.
- `wrr update --apply --owned-repo /absolute/target --candidate-root /absolute/candidates`
  prepares an isolated Git candidate; it never updates the target in place.
  `--owned-repo` selects an exact declared target and attests current maintenance
  authority over that WRR-private target. It is not permission to take over a
  host-managed or shared installation. Project manifests also need `--trust-project`.
  The candidate root must already exist outside every discovered target, belong
  to the current user, and not be group/world writable. This POSIX-only write
  path pins directories by file descriptor; rebinding the output path fails
  without redirecting writes into a live target. Symlinked paths, dirty targets
  (including ignored files), hidden index flags, mismatched origins and
  non-full-commit pins are refused. Existing repositories with clean/process
  filters or submodules require owner review instead of executing their local
  configuration during admission. Only local/file and HTTPS Git sources are supported.
  Success reports `downloaded`, an actual pinned revision, and `activated=false`;
  it does not run candidate code, install dependencies, verify host compatibility,
  activate, or roll back. A finite external supervisor now exists in the checkout
  (see below); its existence does not complete dual-host activation acceptance.
  Dry-run describes preparation without promising admission.
- The optional fixed campaign sampler is plugin-owned, disabled by default, and
  supervised by the Hermes plugin lifecycle. It calls the registered WRR production
  handler directly and stops on drain/close/disable or the first failed sample;
  Hermes core is not modified.
- The v6 migration gate stays **opt-in**: installation does not activate
  descriptor execution. The existing `doctor --v6` control-plane path remains
  separate from the shared v5 query policy described above.
- Wheel verification should confirm the built-in engine manifests are packaged:
  `pyproject.toml` ships `engines/builtin/*/engine.yaml` via `[tool.*] package-data`
  (`wrr = ["engines/builtin/*/engine.yaml"]`). After `python -m build`, inspect the
  wheel (`unzip -l dist/*.whl | grep engine.yaml`) to ensure every builtin engine
  manifest is present before publishing.

### Distribution and bounded release supervision

The pip package exposes `wrr` and packages `wrr*` plus builtin engine manifests.
The repository [OMP adapter](integrations/omp.mjs) and
[supervisor launcher](scripts/wrr_release_supervisor) with its
[isolated bootstrap](scripts/wrr_release_supervisor.py) are checkout attachments,
not pip-installed commands/assets. The supervisor implementation lives in
[`wrr/cli/release_supervisor.py`](wrr/cli/release_supervisor.py), but there is no
supervisor console entrypoint in [pyproject.toml](pyproject.toml). This describes
packaging configuration, not a newly built/inspected wheel.

The supervisor is a **finite, POSIX, externally authorized** release selector,
not an updater daemon or in-process self-overwrite. Its launcher uses the
checkout's `.venv/bin/python -I -S`. Safe inspection/planning entries are:

```bash
./scripts/wrr_release_supervisor --help
wrr install --dry-run
```

`install` is report-only; the supervisor has **no report-only/dry-run operation**.
Its `execute()` refuses without explicit apply. Actual activate/finalize/rollback/
recover operations require separate external authorization, matching action and
transaction, expiry, input digests, exact release identities and allowed writes;
these examples do not authorize them or supply an execution payload.

Release identity binds path, origin, revision, package version and tree digest,
not just a version label or a pin claim. Receipts bind verification evidence to
the candidate and distinguish `isolated_fixture` from `real_host`. Activation
requires pre-check and impact receipts (no config/state/shared-resource change,
no migration, rollback compatible), and remains `provisional` with
`real_host_verified=false`. Finalization checks the current state-bound activation
receipt, unchanged release/config identities and post-switch OMP/Hermes PASS
receipts tied to that activation's transaction/generation. Fixture finalization
is only `finalized_fixture_only`; it cannot become `finalized_real_host` by
renaming evidence. The supervisor validates supplied receipts, not the truth of
host execution by running hosts itself. It does not install dependencies, run
consumer commands, or promise rollback of migrations/shared resources. Full
real-host activation acceptance and release approval remain outstanding.

### Behavior migration notes

- [Skill discovery](wrr/engines/skill_discovery.py) now ranks primarily by query
  relevance with provenance and frontmatter completeness, not the old
  license/recency engineering-quality score. License still contributes to
  frontmatter completeness; this is not license validation or execution trust.
- CLI env selection is WRR-owned and explicit as described above; the OMP adapter
  uses its own filtered child environment. No implicit Hermes credential sharing.
- Update candidates and fixed revisions are preparation artifacts, not activation
  or consumer verification. Supervisor receipts do not relax Stage C or release gates.

## v6 CLI migration gate

v6.0 introduced the control-plane CLI; v6.1 added the Engine Health Policy. The
v6.0 S3 default switch was later superseded by Stage S: production execution is
legacy-only and the descriptor registry is shadow/evidence only (see "Runtime
paths" above). Legacy `doctor` behavior and old JSON consumers still work
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
  across the built-in doctor profiles (`default_doctor_profiles()`: default,
  editable install, standalone, S2 codex, S3 hermes, cron worker, Hermes
  runtime); control-plane only; `--engine` / `--tier` / `--deep` rejected.
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

The `dev` extra now explicitly includes `jsonschema` and `referencing`, used by
contract tests; `mcp` remains optional and is not pulled in by `dev`. Keep separate
environment results for `.[dev]` (base/optional-dependency behavior) and
`.[dev,mcp]` (MCP SDK paths). A skipped MCP test is **NOT_RUN**, not PASS, and a
passing targeted run is not a fresh full-suite or full-matrix result. The latest
closeout targeted run recorded 64 passed, zero skipped; it does not establish
both dependency environments or real-host/live-provider acceptance. This
documentation-only follow-up does not rerun tests or install either extra.

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
