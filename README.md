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
- 非 `local:` 敏感 query 仍使用原 `_sensitive` 严格门：命中即在 judge/adapter 调用前 `blocked`、零外发；显式来源和书目门不能绕过。`local:` 仍只走本地。
- 仅 `privacy_class=public` 候选改用独立 `candidate_sensitive` 形状检查（**用户 2026-10-21 批准**）。命中者逐条保留并标 `unjudged:sensitive`，不进入候选或交付 judge state，其他公开候选照常判断。highlights 全文仍参与检查，但从不外发。
- judge 故障保留原结果并披露降级；只有安全硬拒绝导致全空时才标记 `all_rejected` 并停止补搜，低 rel 不触发该分支。
- 候选仍拦已知 token 形状（sk-/ghp_/xox/AKIA/AIza/JWT）、凭据字段与长随机值赋值、Authorization/Bearer 实值、PEM 私钥头、已知用户 key 值、邮箱／带分隔电话号码／身份证／Luhn 通过的卡号、本地路径，以及非公开、userinfo 或带凭据参数的 URL。URL 熵按单组件检查，保留 query/fragment 检查与百分号解码；公开嵌入链接按 Markdown/引用边界提取，不因链接本身而拒绝。
- 候选中的 password/credentials/API key 等概念词、无随机实值的占位赋值、arXiv/DOI/版本号、年份/价格/issue 号及不通过 Luhn 的裸长数字不再单凭词汇或数字串拒绝；不使用主题词表推断敏感。以上不改变查询门，也不表示隐私识别完备或相关性质量已验收。
- 本次形状规则开发烟测：一次真实公开查询 `retrieval augmented generation survey arXiv papers`，DeepSeek/academic 成功、Exa 超时，16 条公开候选；对同批完整原始材料，最终规则离线复筛的 `unjudged:sensitive` 为旧 1 → 新 0。真实运行使用中间版本（1 → 2，新增两条小写 hex 对象 ID 误拦，随后已修复并回归），因此不把最终复筛写成最终版本实跑。3 次 Jev posts，input/output tokens=10546/879，交付 8 条；完整边界见 `evals/runs/20261021T-public-screen-shape-smoke/summary.json`。没有 tune/validation 打分或相关性验收结论。
- 候选选择策略 `uncertain_tiered_v1`：安全已评候选 `rel>=.65` 按分数降序，`.35<=rel<.65` 保持输入 RRF 原序，`rel<.35` 按分数降序；同分稳定。全部先保留，再截取 top-k；私密、敏感和 overflow 候选仍按既有未评规则透传。`inj>=.5` 与本地注入硬拒不变，suspect 不计充分性。
- `diagnostics.selection` 区分 `low_confidence_retained_ids` / `uncertain_retained_ids` 与 `topk_truncated_ids`；兼容字段 `dropped_relevance_ids` 为空，不把低分保留称为已验证相关。全低分仍由独立 delivered judge 判断，可 insufficient 或触发一轮补搜；可能增加噪声，不新增云调用，也不代表相关性验收通过。
- 本次开发烟测使用一次真实公开查询 `Python asyncio TaskGroup documentation site:python.org`：Exa + DeepSeek 均成功，交付 5 条域内结果（4 条高分、1 条不确定保留），3 次 judge calls/posts，耗时 2469 ms；无低分候选，不能由此验证全低分质量。全低分保留、independent delivered insufficient 与一轮补搜由公开 pipeline 回归覆盖；此为开发证据，不是正式留出或新验收。

`quality` 区分执行状态、已判断范围和充分性。多个检索来源不等于多个独立出版者，也不构成事实核验。

### 显式站点约束

`wrr search "Python documentation site:python.org" --json` 将未加引号的正向 `site:hostname` 或 `site:hostname/optional-path` 的 **hostname** 作为确定性来源硬门：只接受该 URL hostname 本身及其点分隔子域。userinfo、路径、查询参数中的域名不算匹配；`python.org.evil.test` 不匹配 `python.org`。保留原 URL，不生成或重写链接。

过滤在融合和候选 judge 之前执行，恢复轮同样执行；无 judge 或 judge 故障也不能绕过。`diagnostics.constraints` 披露 `site_hosts`、按来源计的 `excluded_by_source` 与 `excluded_count`（供应商结果条数，非去重 URL 数）。筛后零候选时返回空结果、`relevance=unjudged`、充分性 `enough=null/scope=site_filtered_empty`，不声称模型判定 `all_rejected`，也不向模型请求空集合充分性。

仅支持一个正向站点（hostname 大小写不敏感，可重复同一站点）。可选路径使用 ASCII URI path 字符或有效 UTF-8 百分号编码；例如 `site:github.com/NousResearch/hermes-agent/releases`、`site:douyin.com/video` 不再整次报错。路径保留在原 query 供 provider 处理，诊断记录 `site_path_hints` 和 `site_path_soft_hint:not_hard_enforced`；**不保证精确路径或路径前缀覆盖，同域其他路径仍可交付**。负向 `-site:`、`NOT`、`OR`/`|`、分组、多个不同站点、完整 URL、userinfo、端口、通配符、路径内 query/fragment、反斜杠和非法编码明确报 `site_constraint_syntax`，不猜测布尔含义。带引号的文字不是操作符。来源继续使用原 query 或既有 keyword-only 转换；当前 adapter/SearchOptions 没有可安全复用的域过滤参数，因此不依赖供应商遵守 `site:` 来保证域约束交付。

普通 Reddit/GitHub 等来源偏好及 `official` 不映射为硬域限制。评估 019/020 的原文分别使用非标准 `site.news.ycombinator.com` / `site.reddit.com`；这不是 `site:`，不会被自动改写，诊断给出 `nonstandard_site_syntax:not_enforced_use_site_colon_hostname`。`source_labels_v2` 对它们的 hard 标注存在语法争议，且仍为 assistant 复核、`user_reviewed=false`；本修复不改标签、不宣称这两条或旧合同误杀率已改善。

公开查询 `Python asyncio TaskGroup documentation site:python.org` 的真实 Exa + DeepSeek 烟测交付 5 条 `docs.python.org` 结果，证据见 [烟测摘要](evals/runs/20261020T-site-constraint-smoke/summary.json)。该次供应商均返回域内候选、硬门排除数为 0；域外及欺骗 URL 的排除由真实 pipeline + ReplayJudge 回归覆盖，不能把烟测当成评估集通过。

路径支持的 [开发烟测](evals/runs/20261020T-site-path-development-smoke/summary.json) 使用一次真实公开 `site:docs.python.org/3/library/asyncio-task.html` 查询：Exa 交付 1 条目标文档，DeepSeek 为空，排除数 0；域外、欺骗域名和恢复过滤由针对性回归覆盖。此为 posthoc 开发证据，不重跑 holdout，不改变冻结 protocol/gold/阈值、历史报告或旧 21.60% 口径，也不是新验收。


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
