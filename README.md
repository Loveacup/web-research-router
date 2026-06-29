# Web Research Router (WRR)

Semantic search router with mode-based routing, 11 engines, and Reciprocal Rank Fusion.

## Architecture

```
query → classify_intent(mode) → parallel engines → RRF fusion → ranked results
```

### 6 routing modes

| Mode | Use case | Engines |
|------|----------|---------|
| discovery | "what's out there" | exa + brave + github + community |
| grounding | "what's the fact" | exa + brave |
| research | deep investigation | exa (deep) + brave + academic |
| academic | papers only | openalex + semantic-scholar + arxiv |
| local | search my stuff | supermemory + session + qmd + obsidian |
| recovery | everything failed | searxng |

### 11 engines

**Public-web (7):** Exa, Brave, GitHub, Community (OpenCLI), Academic (OpenAlex+Semantic Scholar+arXiv), Skill, SearXNG
**Local (4):** Supermemory, Session, QMD, Obsidian

## Quick start

```bash
# Install as Hermes plugin
ln -sf ~/code/web-research-router ~/.hermes/plugins/wrr

# CLI
wrr-cli.py doctor          # 引擎 + 全量依赖自检
wrr-cli.py doctor --json   # JSON 输出
wrr search "your query"    # 搜索（需 Hermes runtime）
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
PYTHONPATH=. pytest -q  # 287 tests
```

## License

MIT
