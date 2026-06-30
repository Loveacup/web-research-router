# Web Research Router (WRR)

Semantic search router with mode-based routing, 11 engines, and Reciprocal Rank Fusion.

## Architecture

Hermes `web_search` / runtime tool entrypoint uses the v5 mode/RRF route:

```
query → classify_intent(mode) → parallel engines → RRF fusion → ranked results
```

The standalone legacy-compatible `wrr-cli.py search` command still uses the serial fallback route for compatibility unless a future migration step changes it.

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

Three interchangeable entrypoints share one codebase at package version `6.0.0`:

```bash
# 1) pip install — exposes the `wrr` console script ([project.scripts] wrr = wrr._cli:main).
#    Verify the install with the v6 standalone runtime doctor:
pip install .
wrr doctor --v6 --json --runtime standalone

# 2) Direct script — still works without install, on any Python >= 3.10:
./wrr-cli.py doctor --json
./wrr-cli.py search "your query" --provider exa --count 5

# 3) Hermes plugin — plugin.yaml `entry: __init__.py` registers the wrr toolset;
#    plugin.yaml `version` is kept aligned with the package version (6.0.0).
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

v6 control-plane CLI is opt-in during the compatibility window. Old `doctor`
behavior and old JSON consumers remain supported until the default switch is
announced.

```bash
# v6 doctor JSON: new shape with runtime/env/discovered/resolved/health/summary/trust
./wrr-cli.py doctor --v6 --json

# Trust project-level plugins and project env files only when explicitly needed
./wrr-cli.py doctor --v6 --trust-project --json

# v6 install planning is report-only; --dry-run is currently required
# (non-dry-run install exits 2 until a future migration step enables writes)
./wrr-cli.py install --dry-run --runtime codex --json
./wrr-cli.py install --dry-run --runtime hermes --refresh-deps --json

# v6 dependency update defaults to dry-run; --apply is explicit
./wrr-cli.py update --dry-run --json
```

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
| `agent_reach` | [Panniantong/Agent-Reach](https://github.com/Panniantong/Agent-Reach) | ✅ |

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

```bash
PYTHONPATH=. pytest -q
```

## License

MIT
