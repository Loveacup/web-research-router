# Web Research Router（WRR）7

WRR 7 为 Agent 宿主提供统一的搜索证据内核。来源发现、Jev 选源、并行检索、融合、质量判断和有限恢复共用 `wrr/core`。

**版本：7.0.0。评估状态：未跑评估集。** 契约测试和宿主烟测不代表已达到评估指标。变更与迁移见 [7.0.0 发布说明](RELEASE_NOTES_v7.0.0.md)；需求裁决与接口合同见 [2026-10-19 设计稿](docs/superpowers/specs/2026-10-19-wrr-core-design.md)。

## 快速开始

需要 Python 3.11 或以上版本。

```bash
python3 -m pip install .
wrr inventory --json
wrr search "what causes the seasons on earth" --json
wrr fetch https://www.python.org/ --max-chars 5000 --json
```

仓库入口也支持同样的子命令：

```bash
./wrr-cli.py search "Python official documentation" --sources exa,deepseek --count 5 --json
./wrr-cli.py search "transformer architecture" --exa-mode fast --json
./wrr-cli.py similar https://www.python.org/ --count 5 --json
./wrr-cli.py doctor --v6 --runtime standalone --json
```

`--sources` 指定来源 ID，最多 4 个；`--provider` 是单来源快捷方式。`--exa-mode` 仅控制 Exa API search type。旧 `--route-mode` 和 `--mode` 已移除。

### 凭据与环境

进程环境优先；`--env` 或 `WRR_ENV` 可显式选择凭据文件。未指定时，独立 CLI 使用 `~/.config/wrr/env.d/default.env`，Hermes 使用自己的授权环境文件。项目级来源及凭据需要显式信任。可用变量见 [.env.example](.env.example)。

- `EXA_API_KEY`、`DEEPSEEK_API_KEY`：主力搜索来源。
- `TYPESAFE_API_KEY`：无宿主 judge 时使用 TypeSafe Jev。
- `BRAVE_API_KEY`、`TAVILY_API_KEY`、`GITHUB_TOKEN`：对应来源凭据。
- `SEARXNG_URL`、`WRR_COMMUNITY_CONFIG`：自建搜索与显式配置来源。

WRR 不扫描其他宿主的密钥文件，也不把凭据写进 evidence。

## 架构：6 段

搜索只通过新内核执行。fetch/similar 继续使用独立的顺序 fallback，不进入搜索流水线。

| 段 | 输入与职责 | 关键约束 |
|---|---|---|
| 1. inventory | 读取 builtin/user/project manifests、显式配置来源及宿主能力；形成 `SourceDescriptor` | 信任、凭据、健康与执行器共同决定来源是否可路由；热路径不做 live probe |
| 2. plan | 确定性门先裁剪；Jev 判断证据形态、时效需求与每来源适合度 | 显式来源优先；主力 Exa + DeepSeek 可用时保留，并记录 `forced:primary` |
| 3. execute | 按共享 deadline 并发调用来源；记录各来源状态和耗时 | 单源失败不丢弃其他结果；取消返回 `cancelled` |
| 4. fuse | 以规范化 URL 聚合，并用 Reciprocal Rank Fusion（RRF，k=60）排序 | 同 `upstream_family` 只取最大贡献，避免重复计票 |
| 5. judge/select | 筛选公开候选，选取 top-k，再判断交付集合充分性 | 相关性与注入风险分开判断；保留原始证据身份，不生成链接 |
| 6. recover/evidence | 条件满足时补搜至多 1 轮；返回统一证据对象 | 只选未尝试且未被拒绝的来源；输出 `plan/runs/results/quality/diagnostics` |

默认交付 8 条结果。常规路径最多 3 次 judge 调用：选源、候选筛选、交付充分性。恢复后只再筛选新增候选，总上限为 4 次；每次逻辑判断最多 1 个 POST，不做应用层重试。

显式来源跳过选源 judge，但仍执行候选与交付判断，且不自动补搜。精确 DOI、`references:`、`citations:` 走 academic；本地意图走 local。这两类确定性门全程不调用云端 judge。

### Jev 默认策略与隐私门

**有可用 judge 即默认启用。** 优先使用宿主 judge；否则使用 WRR 授权环境中的 `TYPESAFE_API_KEY`。两者都没有时使用确定性默认计划，并标记 `plan.judge=none`。旧 `WRR_JEV_FILTER` 开关已移除。

云端外发内容限 query 与公开候选的 title/url/snippet。选源判断还读取公开的来源画像，不读取来源凭据。

- `authenticated`、`local` 结果不进入云端 state，保留并标记为未判断。
- 敏感 query 跳过云端判断；走确定性默认计划。
- 敏感候选与非公开 URL 逐条排除；其他公开候选仍可判断。
- judge 故障保留原结果并披露降级；有效判断全部拒绝时，不额外补搜。
- 高熵 URL 路径段可能被保守启发式排除；尚未用评估集校准。

`quality` 区分执行状态、已判断范围和充分性。多个检索来源不等于多个独立出版者，也不构成事实核验。

## 来源接入：3 档

| 档 | 适用接口 | 接入内容 | 当前状态 |
|---|---|---|---|
| A. 声明式 | 输出 JSON/NDJSON 的 CLI、HTTP JSON API、MCP search tool | `engine.yaml` 声明命令/URL/tool、字段映射、profile、privacy_class、upstream_family 和凭据变量名 | 已有执行器及契约测试；模型请求不能修改命令 |
| B. adapter | 分页、签名、HTML 解析或多步调用 | manifest 加 Python adapter；复用 `SourceAdapter` 合同 | 内置 Exa、DeepSeek、Brave、Tavily、academic、GitHub、community、local 等来源 |
| C. remote | 另一台设备上已接好的来源 | 设计采用 `peers.yaml`、HTTP 搜索与取消接口 | 设计已记录；当前 checkout 尚无 `wrr serve` 或 remote 执行器 |

A/B 档来源无需修改内核路由或宿主扩展。manifest 的 profile 供选源判断，privacy_class 控制云端边界，upstream_family 控制融合计票。用户来源沿用 loader 的信任分级；项目来源需要 `--trust-project`。无可执行入口的纯 skill 文档不能伪装成搜索结果源。

community 的 OpenCLI 来源依赖本机 CLI、daemon 与浏览器扩展桥。Agent-Reach 作为出处与诊断参考；WRR 不在运行时导入 Agent-Reach 源码。

## 宿主接入

| 宿主 | 搜索入口 | judge | 当前实现边界 |
|---|---|---|---|
| OMP | `integrations/omp.mjs` 注册同名 `web_search` wrapper，经 NDJSON RPC 调内核 | 宿主 `resolveJudge` 优先，失败时按内核规则回退 | 原生 web 能力经 `ctx.invokeTool` 注入为 `host:web`；凭据不出宿主进程 |
| Hermes | `hermes-plugin/` 的 `web-wrr` backend；保留 Hermes 原生 `web_search` 工具 | TypeSafe 或无 judge | 只接管 search backend；extract backend 不变 |
| CLI | `wrr search` / `wrr inventory` | TypeSafe 或无 judge | 与 Hermes 共用环境快照及搜索构造 |
| Pi / Claude Code | 设计中的 extension / MCP server | 设计中的宿主 classifier / TypeSafe | 当前 checkout 未提供这两种宿主入口，不列为已验收能力 |

### OMP

```bash
omp --no-extensions -e integrations/omp.mjs --no-session --mode json \
  -p "用 web_search 搜 what causes the seasons on earth，只回 plan.judge 和 sources"
```

模型可见工具名为 `web_search`。`wrr_fetch`、`wrr_similar` 及 skill handoff 仍由扩展调用 `wrr-cli.py fetch/similar`；这两条路径本次保留。

### Hermes：从旧 override 迁移

先把 WRR 安装到 Hermes 使用的 Python 环境。后端插件需要 Hermes 提供 `register_web_search_provider`。

```bash
ln -s ~/code/web-research-router/hermes-plugin ~/.hermes/plugins/web-wrr
cp ~/.hermes/config.yaml ~/.hermes/config.yaml.before-wrr7
hermes plugins disable wrr-hermes
hermes plugins enable web-wrr
hermes config set web.search_backend wrr
hermes plugins doctor web-wrr --ci
```

已有 `web-wrr` symlink 时无需重复创建。不要把仓库根目录注册为旧 tool plugin：根 `__init__.py` 与 `plugin.yaml` 已删除。保持 `web.backend` 和 `web.extract_backend` 原值，例如 Exa。长期运行的 gateway 需要插件热重载或由管理员安排重启。

Hermes 后端返回 `data.web`，同时保留 `data.wrr.plan`、`data.wrr.quality`、`data.wrr.diagnostics`。后端不覆盖工具，不执行正文抓取。

## fetch、similar 与维护

`wrr fetch` 保留 Exa → Brave 顺序 fallback、正文提取质量和网络安全策略；`wrr similar` 保留 Exa findSimilar。显式 provider 禁止跨 provider fallback。`wrr/router.py` 和最小 legacy registry 仍供这两项能力及 doctor 使用，不再包含 search 编排。

OMP 原生 fetch handoff 的完整安全验收仍未完成；实验选项不等于已获授权的通用浏览器或原生抓取旁路。WRR 未接管宿主原生工具的 DNS 与重定向策略。

```bash
wrr doctor --v6 --runtime standalone --json
wrr doctor --v6 --profile-matrix --json
wrr install --dry-run --runtime standalone --json
wrr update --help
wrr test unit
```

`install` 仅报告计划。`update --apply` 只在显式授权的 WRR 私有目标之外准备隔离候选，不直接激活或更新共享工具。独立的 release supervisor 仍保留；该维护入口不依赖已删除的搜索取证机制。

每次修改已消费的上游能力前，检查调用、输出、认证与兼容性变化；评估新来源时同时检查出处、许可证、成本和运维风险。记录入口为 [上游检查记录](docs/upstream-review.md)。检查上游不构成自动升级授权。

## 测试与评估状态

```bash
.venv/bin/python -m pytest tests/unit -q -p no:cacheprovider -o addopts=
```

本轮删除只约束旧 search mode、shadow、Stage S、campaign/admission 和旧 Hermes override 的测试。保留新内核回放合同，以及 fetch/similar、doctor、deps、loader、配置来源与隐私启发式的消费者行为测试。

**未跑评估集（T7.3）。** 尚无本轮选源召回、筛选误杀、充分性 Brier 或 p95 延迟达标结论。Pi、Claude Code、remote 设备互通与 OMP fetch handoff 也不得由单元测试或双宿主搜索烟测推定为已验收。

历史 [v6.0.0](RELEASE_NOTES_v6.0.0.md) 至 [v6.4.0](RELEASE_NOTES_v6.4.0.md) 发布说明保留原样，不能当作 WRR 7 当前配置指南。

## 许可证

MIT
