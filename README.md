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
wrr-cli.py search "your query"
wrr-cli.py doctor
wrr doctor --json
```

## Dependencies

| Engine | Dependency | Install |
|--------|-----------|---------|
| Community | last30days-skill / last30days-skill-cn | `git clone` |
| Community | OpenCLI (Agent-Reach) | `npm install -g opencli` |
| Academic | paper-search-mcp (optional) | `git clone + pip install` |
| Exa | `EXA_API_KEY` env var | [exa.ai](https://exa.ai) |
| Brave | `BRAVE_API_KEY` env var | [brave.com/search/api](https://brave.com/search/api/) |
| GitHub | `GITHUB_TOKEN` env var | [github.com/settings/tokens](https://github.com/settings/tokens) |

## Testing

```bash
PYTHONPATH=. pytest -q  # 275 tests
```

## License

MIT
