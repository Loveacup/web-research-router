# 面向 Agent 的开源搜索路由、元搜索与深度研究框架

> 研究快照：2026-10-19。用途：为 WRR 重新架构提供机制对照，不是采购排名。
> 核验层级：GitHub API 元数据、固定 commit 的 README/关键源码、官方在线文档；未运行这些项目、未验证其在线搜索质量。
> 标记：`[推断]` 是本文设计判断；`[未核验]` 是尚无足够源码或实验支撑的能力；“未见”只指本次列出的核验范围。

## 1. 先看结论与研究边界

1. **最接近 WRR 的不是深度研究 UI，而是 `agent-search-mcp` 的证据合同和 Agent-Reach 的能力体检。** 前者把 provider family、质量门、停止原因和部分失败显式化；后者把 channel 与多个本机 backend 分开，并探测可用性。[R1] [R2]
2. **Jev 已有真实检索应用，不只是可行性设想。** GPT Researcher 已用 Jev 选择抓取后的上下文；Hermes Jev Skills 已把结果筛选、证据充分性和下一查询选择拆成 typed decisions。[R3] [R12]
3. **“auto”至少有四种不同含义。** Exa 是同供应商内搜索模式；Tavily 是参数推断；OpenRouter 是 native/外部引擎选择；Agent-Reach 是本机后端选择。把它们当同一种路由会造成重复规划与重复计费。[D1] [D2] [D6] [R1]
4. **连接数、搜索命中数、引用数都不等于独立证据数。** `agent-search-mcp` 明确区分 adapter 与 upstream family；SearXNG 的多引擎共识评分本身没有事实核验含义。[R2] [R7]
5. **不存在已核验的“一套项目完整覆盖本机 API、skill、CLI、MCP 和四个宿主”的直接替代品。** `[推断]` 应借机制、保留 WRR 的能力描述与宿主 seam，而不是移植某个完整研究框架。[R1] [R2] [R4] [R13] [R16]

### 研究口径

- “来源抽象”区分 `engine/provider/connector/tool/skill`，同时区分底层来源与调用 transport。[R1] [R2] [R8]
- “验证”区分运行健康、结果相关性、证据充分性、引用可追溯性、事实支持关系；下文不把前四项自动升级成最后一项。[R2] [R3] [R10] [R12]
- “本地发现”只在检查机器已有命令、宿主 registry、凭据存在性或已连接服务时算成立；列出内置 engine 不算扫描本机能力。[R1] [R11] [R13]
- 官方 SDK 开源不表示其搜索后端开源；Claude Code 与 OpenRouter 后端也不因此被归类为 OSS。[D1] [D6] [D7]
- `pi-jevflow` 是用户指定的源码参考，但其 README 明确未授予额外开源许可；不能按 MIT 项目复制。[R11]

## 2. 仓库与活跃度快照

stars 是查询时 GitHub API 的 `stargazers_count`；日期是默认分支最近 commit 的 **committer date（UTC）**，不是 `pushed_at`。
每行 commit 链接可定位对应 README、代码与许可；stars 会变化，不能据此推定代码质量或实际使用量。
Onyx 的 GitHub API 返回 `NOASSERTION`，以下按根 LICENSE 拆分；Claude Code 的公开仓库不代表实现代码获得 OSS 授权。[R8] [R27]

| 项目 / GitHub | stars | 最近 commit 日期 UTC | 固定 commit | 许可证 / 解释 |
|---|---:|---|---|---|
| [Agent-Reach](https://github.com/Panniantong/Agent-Reach) | 99,174 | 2026-10-07 | [94f06c1](https://github.com/Panniantong/Agent-Reach/commit/94f06c1969dfc1834001269d79d3ad0972d9dee6) | MIT |
| [agent-search-mcp](https://github.com/lennney/agent-search-mcp) | 112 | 2026-09-16 | [dd91577](https://github.com/lennney/agent-search-mcp/commit/dd91577d6972d0d60cae09673d9239a00cf8861d) | Apache-2.0 |
| [GPT Researcher](https://github.com/assafelovic/gpt-researcher) | 30,163 | 2026-10-10 | [3582a68](https://github.com/assafelovic/gpt-researcher/commit/3582a6841fe7dc801e2a34e34ee408641aea9bce) | Apache-2.0 |
| [LangChain open_deep_research](https://github.com/langchain-ai/open_deep_research) | 12,681 | 2026-08-10 | [1b7d2e8](https://github.com/langchain-ai/open_deep_research/commit/1b7d2e80db9faa586165c60e09096dbbfd483a64) | MIT |
| [smolagents / open_deep_research](https://github.com/huggingface/smolagents/tree/96f33faaf028479119ec8d34507b47694cf14e34/examples/open_deep_research) | 29,918 | 2026-10-06 | [96f33fa](https://github.com/huggingface/smolagents/commit/96f33faaf028479119ec8d34507b47694cf14e34) | Apache-2.0；stars/date 是 smolagents 整库 |
| [Perplexica → Vane](https://github.com/ItzCrazyKns/Vane) | 37,392 | 2026-09-01 | [348feca](https://github.com/ItzCrazyKns/Vane/commit/348feca3e378fb4157b217724ed508dc707f853f) | MIT；旧仓库地址重定向 |
| [SearXNG](https://github.com/searxng/searxng) | 38,573 | 2026-10-17 | [1c10abe](https://github.com/searxng/searxng/commit/1c10abe2d3e4ab7ddcc4e9da1a7d07dc7ca7f8f4) | AGPL-3.0 |
| [Onyx / 原 Danswer](https://github.com/onyx-dot-app/onyx) | 32,570 | 2026-10-18 | [f189d49](https://github.com/onyx-dot-app/onyx/commit/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a) | 普通目录 MIT Expat；`ee` 为 Onyx Enterprise License |
| [Khoj](https://github.com/khoj-ai/khoj) | 37,741 | 2026-08-02 | [ae229ca](https://github.com/khoj-ai/khoj/commit/ae229ca894c0b80ad84664afcfdde523b5e87057) | AGPL-3.0 |
| [node-DeepResearch](https://github.com/jina-ai/node-DeepResearch) | 5,237 | 2026-05-01 | [fd323b5](https://github.com/jina-ai/node-DeepResearch/commit/fd323b521a51264d497bec333bfb997da1bf3210) | Apache-2.0 |
| [pi-jevflow](https://github.com/Loveacup/pi-jevflow) | 0 | 2026-10-14 | [be91bb9](https://github.com/Loveacup/pi-jevflow/commit/be91bb962899dac5bc92fcad99c3b350dcb6cccf) | 无额外开源许可；README 称私有仓库 |
| [hermes-jev-skills](https://github.com/kerpopule/hermes-jev-skills) | 1,139 | 2026-10-16 | [b8f69fe](https://github.com/kerpopule/hermes-jev-skills/commit/b8f69fe68b0efaa006f9baede46c3849aaf789f6) | MIT |
| [AnySearch Skill](https://github.com/anysearch-ai/anysearch-skill) | 6,628 | 2026-09-17 | [9b91ee2](https://github.com/anysearch-ai/anysearch-skill/commit/9b91ee215164de60d61c7d63789c891a1948b039) | Apache-2.0；客户端 |
| [AnySearch MCP](https://github.com/anysearch-ai/anysearch-mcp-server) | 1,964 | 2026-09-17 | [e3f2701](https://github.com/anysearch-ai/anysearch-mcp-server/commit/e3f2701a21e61419ebd140358f0b1abb1f5ded04) | Apache-2.0；接口/接入文档不等于服务后端 |
| [free-search-mcp](https://github.com/sweetcornna/free-search-mcp) | 147 | 2026-10-18 | [8c1a931](https://github.com/sweetcornna/free-search-mcp/commit/8c1a9313a9d48d243b5b2dfcab5b4ee8506db830) | MIT |
| [dsh-free-search](https://github.com/DDDMUC/dsh-free-search) | 490 | 2026-10-18 | [4e7d7a7](https://github.com/DDDMUC/dsh-free-search/commit/4e7d7a77ce69dc465cf03cc30f7df3bc67db6a1d) | MIT；另一个同名近似项目，避免混淆 |
| [web-search-plus-plugin](https://github.com/robbyczgw-cla/web-search-plus-plugin) | 101 | 2026-10-10 | [2f6f8bc](https://github.com/robbyczgw-cla/web-search-plus-plugin/commit/2f6f8bc478b007718b20846086eae1f76283597c) | MIT |
| [dsh-web-tools](https://github.com/A3Boy/dsh-web-tools) | 36 | 2026-10-18 | [bb68082](https://github.com/A3Boy/dsh-web-tools/commit/bb68082d1274dc0d28ed5921bb08c7b5070fae7f) | MIT |
| [agent-web-search](https://github.com/JerryLiu369/agent-web-search) | 11 | 2026-10-02 | [c549bf3](https://github.com/JerryLiu369/agent-web-search/commit/c549bf3eb8681e347833a3d927c79231294236ed) | MIT |
| [MetaSearchMCP](https://github.com/gefsikatsinelou/MetaSearchMCP) | 60 | 2026-10-19 | [3ae8a3d](https://github.com/gefsikatsinelou/MetaSearchMCP/commit/3ae8a3d4eff07eee5b5db660370ae0a7b683a582) | MIT |
| [Neuronto ARD](https://github.com/neuronto/agentic-resource-discovery) | 60 | 2026-10-03 | [78d9f90](https://github.com/neuronto/agentic-resource-discovery/commit/78d9f90f9bf5332b3978c7ca91a4cedce5d83ef4) | Apache-2.0 |
| [Tavily Python SDK](https://github.com/tavily-ai/tavily-python) | 1,424 | 2026-10-06 | [46505f1](https://github.com/tavily-ai/tavily-python/commit/46505f119e6ac0f30d63a4dce0ed9f923705ad97) | MIT；SDK，不是搜索后端 |
| [Exa Python SDK](https://github.com/exa-labs/exa-py) | 234 | 2026-10-13 | [9d599f8](https://github.com/exa-labs/exa-py/commit/9d599f8570647442a4ad83f30a3e2270266d9927) | MIT；SDK |
| [Parallel Python SDK](https://github.com/parallel-web/parallel-sdk-python) | 29 | 2026-09-29 | [5b3a1d2](https://github.com/parallel-web/parallel-sdk-python/commit/5b3a1d2b3fe8a484c1be317c6363a920768071f1) | MIT；SDK |
| [Linkup Python SDK](https://github.com/LinkupPlatform/linkup-python-sdk) | 46 | 2026-10-08 | [dacef2c](https://github.com/LinkupPlatform/linkup-python-sdk/commit/dacef2c451af6889d9b9e38fb9c8eeb57ac016a5) | MIT；SDK |
| [Firecrawl](https://github.com/firecrawl/firecrawl) | 192,848 | 2026-10-19 | [d0f3339](https://github.com/firecrawl/firecrawl/commit/d0f33391ca9a87f05066c61246141c60c5e98d7e) | 根仓库 AGPL-3.0；子包许可需分别核查 |
| [Jina Reader](https://github.com/jina-ai/reader) | 12,164 | 2026-05-22 | [1574bfd](https://github.com/jina-ai/reader/commit/1574bfd380d249c86c82db4dace0d9c8fe17e2b1) | Apache-2.0；开源分支不含 SaaS 全部存储层 |
| [OpenRouter Python SDK](https://github.com/OpenRouterTeam/python-sdk) | 168 | 2026-10-18 | [4d6c1b7](https://github.com/OpenRouterTeam/python-sdk/commit/4d6c1b7d1753820751bfa8ea0c4d3e972bbf8042) | Apache-2.0；不是 web plugin 后端源码 |
| [Claude Code 公开仓库](https://github.com/anthropics/claude-code) | 152,286 | 2026-10-18 | [ef7bfdc](https://github.com/anthropics/claude-code/commit/ef7bfdca55d906929fe6b782025b93758c325074) | GitHub SPDX=null；不据此认定 OSS |
| [Codex](https://github.com/openai/codex) | 129,810 | 2026-10-19 | [89461ea](https://github.com/openai/codex/commit/89461ea6833832b0551ce24ee32205824d5c108d) | Apache-2.0；搜索 hosted backend 不在此许可结论内 |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | 107,441 | 2026-10-16 | [ed96fae](https://github.com/google-gemini/gemini-cli/commit/ed96faedfd5571f055c44f2c9509d62321d1138d) | Apache-2.0；Google Search backend 非此仓库 |

### GitHub 搜索补充项目的选择依据

实际执行了 `gh search repos`，包含 `search router agent`、`meta search mcp`、`multi-provider web search`、`federated search agent` 与放宽后的 `federated search`。
`federated search agent` 没有返回结果，因此放宽查询，没有把空结果解释为不存在同类实现。
补充选择以下五个近期有提交、机制各异的项目；活跃度只依据上表 commit，未宣称生产成熟：[R17] [R18] [R19] [R20] [R21]

- `web-search-plus-plugin`：意图路由、canonical-source rerank、自动流量授权门。
- `dsh-web-tools`：SearchHints 到 provider-native 参数的编译层。
- `agent-web-search`：model-native grounding 与传统搜索共用结果合同。
- `MetaSearchMCP`：catalog/tags 选择、并发执行与独立可选 ranking。
- `Neuronto ARD`：搜索可调用工具本身，适合研究发现平面，不是网页搜索引擎替代品。

## 3. 核心同类项目

### 3.1 Agent-Reach：本机能力层比搜索聚合层更值得借鉴

- **抽象 / 路由**：`channel → 有序 backend 列表`；例如 GitHub 用 `gh`、Bilibili 用 `bili-cli/OpenCLI`、Exa 经 `mcporter`；由 skill 告诉 Agent 实际调用上游，不把所有数据读取重新包装一遍。[R1]
- **融合 / 验证**：没有统一跨 channel 搜索融合器；`doctor` 负责 backend 健康和修复建议，不能把 doctor 成功当成返回内容真实完整。[R1]
- **宿主 / 发现**：CLI + `SKILL.md`，适用于能执行 shell 的宿主；探测已装工具与可用 backend，公开 `active_backend`；部分平台为避免 cookie 读取或远端副作用而不做默认 live probe。[R1]
- **对 WRR 重构的含义**：`[推断]` 借鉴 capability inventory、backend 优先序、静态/真实健康区分；不要把安装器、登录流程、平台运营逻辑都并入搜索请求热路径。[R1]

### 3.2 agent-search-mcp：最直接的多源路由参照

- **抽象 / 路由**：静态 `provider-catalog` 与 runtime executor registry 分离；free-first waterfall，每阶段检查 count、relevance、confidence、provider-family 覆盖，再决定升级。[R2]
- **融合**：URL 去重、来源权重、独立 family corroboration；DuckDuckGo 与 Bing 等相关 upstream 不因 adapter 名称不同就重复增加独立源数；semantic dedup/rerank 为 opt-in。[R2]
- **验证**：机器可读 `quality_gate`、`stop_reason`、`partialFailures`；规则质量门不等于 LLM 事实核验；项目中 LLM judge benchmark 与默认运行路由是两回事。[R2]
- **宿主 / 发现**：MCP stdio/HTTP、CLI `fasm`、独立 routing skill；`search://capabilities` 与 health resources；发现自己的注册 provider/凭据，不扫描任意本机 skill。[R2]
- **对 WRR 重构的含义**：`[推断]` 借统一证据包、provider-family、预算分层；不要照搬固定英文/中文 waterfall 作为下一代智能路由核心。[R2]

### 3.3 GPT Researcher：已落地 Jev 的抓取后筛选

- **抽象 / 路由**：retriever plugins，配置可用 `RETRIEVER=tavily,mcp`；planner 生成研究子问题，执行阶段并发检索/抓取；不是自动发现全部本机来源的 router。[R3] [R3a]
- **融合**：按 URL 避免重复抓取，合并各子问题选出的上下文，再由 writer 成文；不是把所有 retriever 原始 score 直接相加。[R3]
- **验证**：`select_context()` 在 scrape 后分块，以 Jev `usefulness` score + 阈值选段；无 key/失败可回 BM25；支持 embeddings/none；引用保留不等于逐条 claim 已核验。[R3]
- **证据限定**：文档报告 28 任务 replay，Jev 保留段落相关率 73%、embeddings 46%；这是作者在固定抓取页上的筛选实验，本次未复现，不能外推为 WRR 搜索准确率。[R3]
- **宿主 / 发现**：Python API、CLI、Web 应用，另有 `gptr-mcp` server；MCP configs 显式提供，未见通用本机能力扫描。[R3] [R3a]
- **对 WRR 重构的含义**：`[推断]` 借“搜索候选 → 抓取 → usefulness gate”的阶段分离及阈值拒收；不要直接继承每千字符一请求的并发形态，OMP 的单 state 多问题需单独评估。[R3]

### 3.4 LangChain open_deep_research：研究组织不等于来源自动路由

- **抽象 / 路由**：`SearchAPI` 枚举实际为 `anthropic/openai/tavily/none`；研究 LLM 调工具，supervisor 分发研究单元；模型有 research、summary、compression、final 等配置角色。[R4]
- **融合 / 验证**：聚合研究结果、压缩后写报告；有离线 Deep Research Bench 接口，但不是每次查询都经过独立事实 judge。[R4]
- **宿主 / 发现**：LangGraph 应用与平台 UI；`MultiServerMCPClient.get_tools()` 从显式配置 MCP 取工具，并按 allowlist 过滤及避免名字冲突；不扫描宿主所有工具。[R4]
- **对 WRR 重构的含义**：`[推断]` 借 hosted/native search 与 MCP 共用工具集合、分角色计算；不要把 LangGraph 状态机和 supervisor 全套引入只需一次检索的调用。[R4]

### 3.5 Hugging Face smolagents 版本：以可操作浏览工具代替复杂来源层

- **抽象 / 路由**：示例由 `CodeAgent` manager 管 `ToolCallingAgent` search_agent；后者拿 `GoogleSearchTool(provider="serper")`、visit、分页、find、archive 与文件 inspector。[R5]
- **融合 / 验证**：LLM 规划、多步访问并利用 observation；本示例未见统一跨 provider rank fusion 或在线独立 judge；GAIA scorer 属于离线评测。[R5]
- **宿主 / 发现**：Python CLI/Gradio 示例；smolagents 本体能从 MCP 导入 tools，但这个 open_deep_research 示例并没有自动扫描本机 MCP/CLI。[R5]
- **对 WRR 重构的含义**：`[推断]` 借“小而明确的工具合同 + 可重复观察”；不要把 CodeAgent 任意代码执行权限、宽泛 imports 当成来源插件执行模型。[R5]

### 3.6 Perplexica / Vane：LLM 多标签决策与两级筛选

- **身份变化**：原 `ItzCrazyKns/Perplexica` 当前重定向到 `ItzCrazyKns/Vane`；README 表示搜索现由 SearXNG 提供，Tavily/Exa 仍写为计划，不能算已实现多 API 直连。[R6]
- **抽象 / 路由**：LLM 结构化 classifier 输出 skipSearch、academicSearch、discussionSearch、personalSearch 等布尔项；研究动作由 registry 暴露。[R6]
- **融合 / 验证**：speed/balanced 对 snippet 以 embedding similarity 筛选和语义去重；quality 让 LLM 从候选选至多三个再抓取；这是相关性和来源偏好，不是引用事实逐条核验。[R6]
- **宿主 / 发现**：自托管 Web UI + Search API；当前核验代码未见通用宿主 plugin/MCP 能力发现。[R6]
- **对 WRR 重构的含义**：`[推断]` classifier 的多布尔结构可迁移为 Jev 多问题；不要照搬 embedding 故障后设 similarity=1 的表示，必须明确 unknown/未评分。[R6]

### 3.7 SearXNG：权重元搜索的精确机制

- **抽象 / 路由**：Python engine modules + YAML 配置；按 engines、categories、语言与用户搜索语法确定查询来源，然后向选中的引擎发请求；不是 LLM 语义路由。[R7]
- **融合**：`ResultContainer` 按结果 hash 合并、积累 `engines/positions`；普通 priority 下，源码可写为 `score = n × product(engine_weights) × sum(1/position)`，`n=len(positions)`，不是标准 `sum(w/(k+rank))` RRF。[R7]
- **额外排序**：`high` priority 每次位置贡献 weight，`low` 不增加；算分后还按 category/template 分组处理。因此不能仅凭公式复刻最终列表。[R7]
- **验证 / 发现**：有 engine 错误、超时和运行状态机制；权重与共同命中不证明页面事实正确；engine 来自配置，不探测本机任意 CLI/skill。[R7]
- **宿主**：HTTP Search API 可供各类客户端使用；MCP 通常由外部 wrapper 提供，不是此处核验的原生核心合同。[R7]
- **对 WRR 重构的含义**：`[推断]` 借成熟 engine 接口、时间预算与错误隔离；不要照搬多引擎权重乘积，尤其不能把共享 upstream 的多个结果视为独立证据。[R7] [R2]

### 3.8 Onyx / Danswer：connector、live federation、web provider 是三层

- **抽象**：普通 connector 把外部系统内容、metadata、ACL 摄入索引；federated connector 在 query-time 搜原系统；web search/content provider 是另外的独立接口。[R8]
- **路由**：内部搜索可用 LLM 推断 source/time scope，范围受已连接来源与用户/agent 限制；web search 则是数据库里一个 active provider，添加第二个不自动形成 fallback 链。[R8]
- **融合**：内部检索多 query lanes 并发，weighted RRF，`sum(weight/(50+rank))`，按 `document_id/chunk_id` 去重，再合并 section、LLM selection。[R8]
- **验证**：ACL 在取证前处理，LLM selection 选择相关 section；当前文档明确该内部路径没有独立 reranking model，不应沿用旧 Danswer 印象描述。[R8]
- **边界**：当前 federated 框架声明通用，但核验 feature map 说明实际 product 只有 Slack；不要把 registry 结构误当成多个已落地 federated 来源。[R8]
- **宿主 / 发现**：HTTP API、MCP server 可让 Claude Code/Codex 等访问；来源是管理员/用户连接配置，不是本机程序扫描。[R8]
- **对 WRR 重构的含义**：`[推断]` 借统一 evidence/chunk 合同、scope 受可用集约束、ACL 与质量分离；不要照搬企业级索引/权限基础设施，也不要误认 web provider 已实现智能多源路由。[R8]

### 3.9 Khoj：LLM 生成 query，来源选择仍然是固定回退

- **抽象 / 路由**：LLM 推断 online/documents，`/online` 可强制；online_search 生成子查询后，按已配置 key/URL 组装 Serper → Exa → Firecrawl → Google → SearXNG 列表。[R9]
- **执行 / 融合**：每个 provider 内子查询 `asyncio.gather`；provider 外层顺序尝试，遇到非空 response 即 break；不是全部 provider 并发融合。[R9]
- **验证**：可自动读取 top pages 并抽取相关信息，结果带在线 references；本次核验路径未见独立在线 LLM truth judge。[R9]
- **宿主 / 发现**：自托管服务及 Web/客户端接口；通过 env 确认预定义 provider，未见任意本机 skill/CLI inventory。[R9]
- **对 WRR 重构的含义**：`[推断]` 借“是否需要联网”和“用哪个来源”分离；不要以首个非空为成功标准，应由证据质量门决定是否停止。[R9] [R2]

### 3.10 Jina node-DeepResearch：最完整的答案评估循环之一

- **抽象 / 路由**：LLM 选 `search/visit/reflect/answer`；底层 `searchProvider` 仍在 `jina/arxiv/duck/brave/serper` 等 switch 分支选择，不能称为自动跨供应商智能路由。[R10]
- **融合**：URL 池过滤、`rankURLs`、每 hostname 保留 top 2；知识与访问记录随研究循环积累，控制继续搜索/访问的候选量。[R10]
- **验证**：question evaluator 选择 definitive/freshness/plurality/completeness 等检查，answer evaluator 不通过就继续；原问题还有 strict 检查和预算边界。[R10]
- **限制**：这些是 LLM 对问题/答案/知识的评价；`[未核验]` 未证明每个最终 claim 都被独立重新打开引用并核对。[R10]
- **宿主 / 发现**：CLI、OpenAI-compatible `/v1/chat/completions`；provider 来自配置，没有通用本机能力发现。[R10]
- **对 WRR 重构的含义**：`[推断]` 借多维 sufficiency 与“失败后补证据”，用 Jev 接管封闭判断；不要把生成完整答案和 token-heavy 研究循环设为每次 search 的默认工作量。[R10]

## 4. Skills、MCP 与本机发现样本

### 4.1 pi-jevflow：模型路由的控制面参照，非可自由复制 OSS

- **抽象 / 路由**：Pi virtual models `jev/auto`、`jev/subauto` 与两个独立模型池；Jev classifier 做模型/thinking 决策，显式物理模型优先。[R11]
- **融合 / 验证**：不检索网页、不融合 SERP；confidence、provider affinity、headroom 与连续相同建议约束实际切换；有 explain、人工纠正、回放记录。[R11]
- **发现**：用 Pi `ModelRegistry` 的 find/findOfType/hasConfiguredAuth/getProviderAuthStatus 做本地 pool-check，不触发 classify/complete/fetch；明确其无法知道真实质量、价格和完整上下文占用。[R11]
- **宿主**：Pi extension/package；不是 Hermes/OMP 通用插件。README 记载的测试通过数是作者快照，本次没有运行验证。[R11]
- **对 WRR 重构的含义**：`[推断]` 借“宿主 registry 是可用性事实源”“建议与执行分开”“手动覆盖更高优先级”；不要移植模型切换专用的上下文亲和阈值，也不要复制无授权代码。[R11]

### 4.2 hermes-jev-skills：搜索 judgment 层与跨宿主 plain skill

- **抽象 / 路由**：plain `SKILL.md` + `jev` CLI；Hermes plugin 使用公开 hooks；安装器识别 Hermes、Claude Code、Codex。搜索模块本身不 fetch，也不写 query。[R12] [R12a]
- **筛选 / 验证**：先批量给结果 relevance/injection 判断，再判断 sufficiency，并从调用者写的候选 queries 中选一个；Jev 不能生成下一条 query。[R12]
- **状态合同**：`answer/search_more/propose_queries/answer_from_what_we_have/unknown`；unknown 表示没有问到 Jev，不等于证据弱；空 shortlist 时跳过 sufficiency 数字、转下一 query 选择。[R12]
- **发现 / 宿主**：model catalog 根据本地已配置 provider/key 名与登录状态过滤；search gate 接调用者带来的 evidence；Hermes 可 transform tool result，plain skills 可用于其他宿主但不继承全部 Hermes hooks。[R12] [R12a]
- **对 WRR 重构的含义**：`[推断]` 借显式 unknown、opaque evidence IDs、本地隐私门、查询候选 choice；不要把默认 fail-open 的搜索结果返回误报为“通过质量验证”。[R12]

### 4.3 AnySearch Skill：跨运行时可移植，不是本机多源 router

- **抽象 / 路由**：对统一 AnySearch 远端 API 的 skill；支持 general、vertical、batch、extract；四种 runtime CLI（Python/Node/PowerShell/Bash）共享请求资料。[R13]
- **发现**：安装验证检查可用 runtime，以 `doc` 命令验证并写 `runtime.conf`；正常调用复用缓存的 Command，不每次重新探测。[R13]
- **融合 / 验证**：服务返回搜索结果；服务内部来源选择、融合权重和独立 judge `[未核验]`，客户端 README 不足以证明。[R13]
- **宿主**：任何支持 Agent Skills 且可调用相应 runtime 的宿主；运行时发现只发现自身执行环境，不发现别的本机来源。[R13]
- **对 WRR 重构的含义**：`[推断]` 借跨 runtime launcher 与离线 doc smoke 的分工；不要为适配每个宿主复制同一 provider HTTP 业务代码，也不要默认自动注册账户。[R13]

### 4.4 AnySearch MCP：统一远端 transport 的覆盖面

- **抽象 / 路由**：统一远端 `https://api.anysearch.com/mcp`，支持 general/vertical/batch/extract；Streamable HTTP，文档分别给 OMP/Hermes/Claude Code/Codex 等宿主配置。[R14]
- **融合 / 验证**：搜索后端细节仍 `[未核验]`；MCP schema 提供调用合同，不自动提供“结果事实已验证”保证。[R14]
- **本地发现**：客户端连接远端 server 后可发现 server tools；不是发现本机其他 API/skill/CLI，也不意味着不同宿主的 header 插值与 secret 存储语法相同。[R14]
- **对 WRR 重构的含义**：`[推断]` 借一个服务合同、多份极薄宿主注册；不要把宿主设置写入器与 provider execution 混在一起。[R14]

### 4.5 free-search-mcp：RRF、抓取与有界 research brief

- **抽象 / 路由**：engine 分组树；默认小 keyless pool，category 路由到垂直 engine，语言/时效增加合适来源；显式 engines 则只跑指定来源。[R15]
- **融合 / 验证**：多引擎并发、RRF、标题 fuzzy 与 host-canonical 去重；CAPTCHA/login wall/off-topic 被 bench 并换 reserve；research 抓 top N 输出证据 brief，默认不需要 LLM。[R15]
- **宿主 / 发现**：MCP stdio/HTTP、Claude Code/Codex plugin、Hermes plugin、verified-research skill；`engines()` 列自己支持的源；已登录 Codex CLI 凭据复用是显式路径，不等于普遍自动授权。[R15]
- **对 WRR 重构的含义**：`[推断]` 借来源状态可见性、时间 provenance 和轻量 evidence brief；不要把账号 OAuth 提取/自动登录或 title fuzzy 去重直接推广到 WRR 全部来源。[R15]

### 4.6 dsh-free-search：另一个“free-search”，宿主 seam 很清晰

- **抽象 / 路由**：注册进 DSH `ctx.web`；Auto 按语言/时间选 pool，Multi 并发对应语言池全部已启用源；Ordered fallback 加类型化冷却。[R16]
- **融合 / 验证**：URL 合并、跨源命中优先、同站以 eTLD+1 判断；research 可候选重排、分层 fetch、段落选窗，模型判断可选；未证明跨源命中天然独立。[R16]
- **宿主 / 发现**：DSH 原生插件、设置页、命令和测试工具；识别内建 provider 的 key/设置；没有通用 Hermes/OMP/Pi 的本机 inventory。[R16]
- **限制**：README 的“永不直接失败”受 fallbackOn 与所有来源失败现实约束，不能作为真实性/可用性保证；文中个别免费/付费表项存在重复或口径变化。[R16]
- **对 WRR 重构的含义**：`[推断]` 借全局 disable 对所有路径一致生效、provider 与实际 engine 身份分离；不要照搬静默降级及整池并发默认值。[R16]

## 5. GitHub 搜索补充的五个活跃项目

### 5.1 web-search-plus-plugin：规则路由已开始主动简化

- **抽象 / 路由**：OpenClaw plugin 的 search/extract providers；当前按 intent table 选第一个 configured/enabled/auto-allowed provider；某些来源需显式 `auto_allow=true`。[R17]
- **融合 / 验证**：canonical primary-source rerank、镜像过滤、域名 head cap、确定性 diversity；health 与 shadow-quality 只观察，README 明确旧 adaptive samples 已不再改变路由分数。[R17]
- **宿主 / 发现**：原生 OpenClaw tools + setup CLI；运行 key 读显式 plugin config，不自动扫描系统 secrets；local provider 安装存在也不自动获得流量。[R17]
- **对 WRR 重构的含义**：`[推断]` 借 configured/healthy/authorized 分离与 authority/diversity 可解释输出；不要继承一大套意图关键词表，也不要把 shadow 观测当已生效策略。[R17]

### 5.2 dsh-web-tools：统一意图，不抹平 provider 原生能力

- **抽象 / 路由**：SearchHints 规范化意图，再编译到 Exa category、freshness、域名策略、地域等原生参数；Ordered/Round-Robin/Random 选择起始源，类型化 failover。[R18]
- **融合 / 验证**：主要是顺序/弹性调度而非默认全源 RRF；支持原生 fetch → generic HTTP；浏览器平台验证会区分登录墙、search-restricted 和真实无内容。[R18]
- **宿主 / 发现**：DSH 插件、custom provider 模板、credentials service；自定义来源加入同一调度；并非扫描任意本机 skill。[R18]
- **对 WRR 重构的含义**：`[推断]` 借 capability-aware 参数编译、原生平台证据与通用网页 fallback 区分；不要用 indexed web result 冒充已拿到平台原生帖子/评论，也不要接管用户浏览器登录。[R18]

### 5.3 agent-web-search：把 grounding 模型也当来源

- **抽象 / 路由**：provider-neutral contract 覆盖 DDGS、搜索 API 与 Gemini/Grok/Responses/Messages 等 model-native grounding；显式选择 provider 并发运行；Exa/Parallel 可按 key 状态选择 API 或 MCP transport。[R19]
- **融合 / 验证**：返回统一 `results`，model-backed provider 还可返回 `answer`；保留一个来源成功不受另一个失败影响；统一 provenance 不等于其合成 answer 已经独立核验。[R19]
- **宿主 / 发现**：MCP stdio/HTTP、CLI、Python API、Hermes native plugin、Agent Skill；providers env 在启动时解析；未见广义本机 capability 扫描。[R19]
- **对 WRR 重构的含义**：`[推断]` 借 transport 与逻辑 provider 分离、raw evidence 与 synthesized answer 分离；不要将“搜索结果”和“别的模型生成的结论”混成同等可信 snippet。[R19]

### 5.4 MetaSearchMCP：catalog/tags 与执行层分离

- **抽象 / 路由**：Pydantic contracts、catalog、orchestrator、merge、ranking 分层；按 provider 名或 tags(any/all) 过滤后并发，Google 有单独 provider fallback chain。[R20]
- **融合 / 验证**：URL canonicalization 去重；consensus/relevance rerank 通过 `RANK_RESULTS` opt-in；本次 README 核验未见独立 LLM fact judge。[R20]
- **宿主 / 发现**：FastAPI、stdio MCP、CLI setup；`/providers` 暴露 catalog、`/health` 列已配置 provider；不是本机任意可执行程序发现。[R20]
- **对 WRR 重构的含义**：`[推断]` 借 contract/catalog/executor 的单向边界和 tags；不要把很长的垂直来源清单全部放进每轮 Agent 工具 schema。[R20]

### 5.5 Neuronto ARD：发现平面，不是搜索结果平面

- **抽象 / 路由**：检索“可调用资源”及实际工具 schema；ARD manifest/registry federation，auto 模式并发查询 registry，并在硬时间预算内返回。[R21]
- **融合 / 验证**：registry 间 RRF，本库检索 BM25+dense；握手 MCP、调用 `tools/list` 验证工具 schema，周期 liveness probe；relevance 明确不是 trust/safety 评分。[R21]
- **宿主 / 发现**：HTTP/MCP；验证的是远端可调用服务，不是本机程序；manifest 可从实际 MCP/OpenAPI/llms.txt 证据生成。[R21]
- **对 WRR 重构的含义**：`[推断]` 借“声明能力 ≠ 实测能力”、manifest 证据来源与更新时间；不要在未授权时从公共 registry 自动安装/执行陌生 server。[R21]

## 6. 官方 SDK 的 auto routing：多数不是跨来源路由

### 6.1 Tavily

- **抽象 / 自动化**：单 Tavily provider 的 Search/Extract/Crawl/Research；`auto_parameters=true` 推断 topic/search_depth 等，显式参数覆盖自动值；include_answer/raw_content/max_results 仍手动控制。[D1] [R22]
- **融合 / 验证**：服务返回 query-relevant chunks、score 与可选 raw content/answer；SDK 还支持 feedback；独立 truth judge 与底层跨引擎融合算法 `[未核验]`。[D1] [R22]
- **宿主 / 发现**：Python/JS SDK、MCP 等工具接入；不发现本机其他 providers；auto 可能选择 advanced，需保留费用约束。[D1] [R22]
- **对 WRR 重构的含义**：`[推断]` Jev 负责 provider choice，Tavily 可负责 provider 内参数优化；不要让两层自动化都默认为可无限升级成本。[D1]

### 6.2 Exa

- **抽象 / 自动化**：`type=auto` 为供应商内搜索模式；当前文档还有 fast/instant/deep-lite/deep/deep-reasoning；Agent API 可 `effort=auto` 做更长研究，不是选择 Brave/Tavily 的 router。[D2] [R23]
- **融合 / 验证**：highlights 为抽取式证据，深模式增加计划/综合，outputSchema 约束结果形状；不能由 schema 保证事实正确，内部核验算法未全部公开。[D2]
- **宿主 / 发现**：SDK、`web_search` agent tool helper、MCP/远端 agent；不扫描本机技能；旧 neural/autoprompt 文档与新 API 表述需避免混用。[D2] [R23]
- **对 WRR 重构的含义**：`[推断]` 借 highlights/context budget 与同步搜索/长任务区分；不要把 Exa auto 再命名为 WRR 全局智能路由。[D2]

### 6.3 Parallel

- **抽象 / 自动化**：Search 接收 `search_queries` 与可选 `objective`，返回 ranked excerpts；当前 stable v1 默认 advanced，可选 turbo/fast/basic；Task 的 processor 由调用方显式选，不应误读成自动跨供应商调度。[D3] [R24]
- **融合 / 验证**：服务内部压缩相关 excerpts，Task 面向综合研究；官方公开评测使用 LLM judge 不表示每次 Search 在线跑同一 judge。[D3]
- **宿主 / 发现**：typed SDK、Search/Extract agent 工具与 MCP 等服务接口；没有本机能力 inventory；官方建议交互等待场景考虑 Responses，异步准确性任务用 Task。[D3]
- **对 WRR 重构的含义**：`[推断]` 借 objective 独立于 query、同步/异步能力标签；不要把旧 `/v1beta` 示例机械改 URL 而不改请求结构。[D3]

### 6.4 Linkup

- **抽象 / 自动化**：当前 search overview 有 flash/fast/standard/deep；前两者不调用 LLM 做检索计划，standard 单轮 agentic，deep 多轮 search/scrape/evaluate；depth 是显式选择。[D4] [R25]
- **融合 / 验证**：searchResults/sourcedAnswer/structured 三种输出；引用与 sources 可返回，但服务 judge 细节 `[未核验]`；当前 overview 比旧 endpoint 页面列出的模式更多，应以接入版本 schema 为准。[D4]
- **宿主 / 发现**：SDK + hosted/local MCP；MCP 文档当前只暴露 standard/deep，与 REST 的四种深度不同；没有本机来源发现。[D4]
- **对 WRR 重构的含义**：`[推断]` adapter manifest 应按 transport 声明实际支持参数，而不是只按 vendor 声明一次；不要从“有 agentic search”推导“自动选所有来源”。[D4]

### 6.5 Firecrawl

- **抽象 / 自动化**：Search 可连带 scrapeOptions 抓取每条结果；`/agent` 按 prompt 自主搜索、导航、提取，URL 可省略；不是开放的跨本机来源 broker。[D5] [R26]
- **融合 / 验证**：Search highlights/full-page content，Agent schema 与 maxCredits、异步 job/webhook；schema 是结构验证，事实核验具体算法 `[未核验]`。[D5]
- **宿主 / 发现**：SDK、CLI+Skill、MCP；来源是 Firecrawl 服务能力；自托管代码与 hosted agent 的能力等价性需单独核验，不从根仓库开源推出。[D5] [R26]
- **对 WRR 重构的含义**：`[推断]` 借 search/fetch/research 三类能力及 cancel/budget 语义；不要把数分钟的 agent job 藏在普通 search 的 timeout 内。[D5]

### 6.6 Jina Reader / DeepSearch

- **抽象 / 自动化**：Reader `x-engine=auto` 是 curl/browser 抓取策略；`s.jina.ai` 会搜索并读取结果；DeepSearch 是另一个 reasoning/search grounding 层，不是一个自动发现 CLI 的统一 SDK。[R28] [R10]
- **融合 / 验证**：Reader 让标题/URL/snippet 升级为正文；DeepResearch 的 evaluator 见 3.10；读取成功、内容格式干净仍不能证明声明被来源支持。[R28] [R10]
- **宿主 / 发现**：HTTP、OpenAI-compatible DeepSearch、SDK/工具 wrapper；本次未见 Jina 官方通用跨供应商本机发现机制。[R28] [R10]
- **对 WRR 重构的含义**：`[推断]` 借抓取策略作为 provider 内部能力而非顶层搜索 mode；不要把 reader 的 auto 与 Jev route choice 合并成模糊的“自动”。[R28]

## 7. 原生 grounding 接口：宿主能力也应进入候选集

### 7.1 OpenRouter web plugin → web search server tool

- **抽象 / 路由**：旧 `plugins:[{id:"web"}]` / `:online` 已被官方标 deprecated；新 `tools:[{type:"openrouter:web_search"}]` 由模型决定 0–N 次调用。[D6]
- **auto 规则**：endpoint 广告原生搜索能力时走 native，否则 Exa；也可显式 parallel/firecrawl/perplexity/exa；这是具体可核验的自动选择规则，不是任意 LLM 学习 router。[D6]
- **融合 / 验证**：标准 `url_citation`、extractive highlights、max_uses/max_total_results；未公开的后端不应描述成独立事实 judge；native capability 应读 endpoint `native_tools`。[D6]
- **宿主 / 发现**：一个 OpenRouter API 合同跨模型；发现平台 endpoint 能力，不扫描本机工具；上表 SDK 的 stars/license 不代表搜索 backend 开源。[D6]
- **对 WRR 重构的含义**：`[推断]` 借 native-first 的能力协商和每请求总调用上限；不要同时开旧 plugin 与新 server tool 导致参数覆盖或重复检索。[D6]

### 7.2 Claude Code WebSearch 与 Anthropic API 工具不是同一外部接口

- **宿主工具**：Claude Code `WebSearch` 访问 Anthropic backend，返回 titles/URLs，后续 `WebFetch` 再取页；allowed_domains 与 blocked_domains 互斥；backend 不可配置，换来源需加 MCP tool。[D7]
- **API 工具**：Anthropic 提供 versioned `web_search_20250305/20260209/20260318`；新版本支持 dynamic filtering、响应包含控制；结果含 encrypted_content 与结构化 citations，多轮须保持原 content blocks。[D8]
- **路由 / 质量**：模型决定何时搜索；dynamic filtering 减少进入上下文的无关信息，不等于 claim verification；本机 capability 扫描不是此工具职责。[D7] [D8]
- **对 WRR 重构的含义**：`[推断]` 将 native WebSearch 看成宿主注入 capability，而非从 Python 进程假装能直接调用；保留 native opaque handle，不强行 flatten 丢掉多轮恢复所需数据。[D7] [D8]

### 7.3 Codex web_search

- **抽象 / 路由**：hosted web_search，与本地 sandbox 命令网络分离；当前模式 disabled/cached/indexed/live，cached 为常规默认，full access 可默认 live；`--search` 请求 live。[D9]
- **验证 / 接口**：cached 用维护索引减少任意 live page 访问，不消除 injection 风险；搜索活动为 `web_search` transcript/JSON items；不应假设它遵守本地 shell proxy/domain allowlist。[D9]
- **宿主 / 发现**：Codex 内建；custom provider 有 `supports_standalone_web_search` 能力旗标，但还要求 provider/model/runtime 支持，不能仅改一个 flag 就视为可调用。[D9]
- **对 WRR 重构的含义**：`[推断]` 借缓存/实时 freshness capability 与网络权限边界声明；不要通过读 Codex 登录凭据绕开宿主权限、计费和能力协商。[D9]

### 7.4 Gemini Google Search grounding

- **抽象 / 路由**：Google Search 作为原生 tool；模型判断是否搜索并生成一个或多个 query，融合后给 grounded response；不是用户可替换 provider 的元搜索 registry。[D10]
- **证据接口**：Interactions 返回 google_search_call/result 与文本 url_citation annotations；GenerateContent 返回 groundingMetadata，包括 webSearchQueries、groundingChunks、groundingSupports 的文本段到来源索引映射。两种 API 不能共用一份硬编码 schema。[D10] [D10a]
- **验证 / 发现**：sources/citation spans 可追溯，不证明每句事实正确；没有本机 CLI/skill 发现；Gemini CLI OSS 与 hosted Search backend 的开源边界不同。[D10] [R30]
- **对 WRR 重构的含义**：`[推断]` 借标准 evidence spans/query trace；adapter 要保留 API flavor，不应把 groundingMetadata/annotations 的位置写死为一个全局 parser。[D10]

## 8. 对 WRR 的综合含义

### 8.1 建议采用的四层边界

1. **发现层**：inventory 汇总宿主注入工具、显式配置 API、可执行 CLI、已连接 MCP；区分 installed/configured/authorized/healthy，先不调用 Jev 判断物理事实。`[推断]` [R1] [R11] [R17]
2. **决策层**：Jev 从“已经可用且获授权”的来源集合选 provider/能力/深度；强约束、预算和显式用户选择先确定性裁剪，Jev 不能创造未安装工具或授权。`[推断]` [R11] [R12] [D6]
3. **证据层**：adapter 输出统一 evidence，保留 logical provider、transport、upstream family、原始 rank、fetch 状态、时间来源；执行失败与质量不通过分开。`[推断]` [R2] [R15] [R19]
4. **验收层**：snippet usefulness/injection → 必要时 fetch → 支持关系/充分性判断 → 决定停止或升级；允许“不足”和“未判断”，不以补足固定 top-K 为成功。`[推断]` [R3] [R10] [R12]

### 8.2 相对现有 WRR，优先重组而非另起一套事实源

- 现有 `wrr/engines/registry.py` 已声明 discovery/resolve 不导入 adapter，并有 static/light/live/recovery 与健康状态；`[推断]` 可演化成唯一 inventory，不再复制 provider 表到各宿主 wrapper。[本地源码](../../wrr/engines/registry.py)
- 现有 `wrr/search_filter.py` 已说明借鉴 Hermes JevKit/GPT Researcher，且明确 selection ≠ retrieval；`[推断]` 下一步应扩充 typed decision 合同和阶段证据，而非再添加平行 reranker。[本地源码](../../wrr/search_filter.py)[R3] [R12]
- `[推断]` OMP 的宿主 web/judge 角色应作为注入依赖：WRR 不自行重建宿主 provider keys/catalog；独立 CLI 才使用自己的配置来源。这是架构建议，OMP 具体调用 seam 由宿主专项研究核验。[R11] [R19]
- `[推断]` API/MCP/CLI/skill 是调用机制，不应等价为四种搜索质量。相同 Exa 经过 MCP/API 仍是同一 upstream；skill 能描述工作流，却不天然拥有结构化执行合同。[R1] [R2] [R13] [R19]

## 9. 可借鉴机制矩阵（机制 × 项目）

`●` 已核验明确机制；`△` 部分/限定范围；`—` 本次未见；不是质量评分。各列依据对应章节及引用，不把空格当成全仓库不存在。

| 机制 | Agent-Reach | agent-search-mcp | GPT Researcher | Onyx | node-DeepResearch | Hermes Jev | pi-jevflow | free-search-mcp |
|---|---|---|---|---|---|---|---|---|
| 本机工具/宿主 registry 发现 | ● channel backend | △ 内置 provider | — | — | — | △ 宿主/模型 | ● Pi registry | △ 显式账户路径 |
| 声明与 runtime executor 分离 | △ channels | ● catalog/registry | ● retriever | ● connector/provider | △ provider switch | ● skill/CLI/hooks | ● pool/registry | ● engines |
| 授权与存在分离 | ● 安装/login 边界 | ● paid policy | △ keys/config | ● ACL/OAuth | △ keys/config | ● privacy/pools | ● configured auth | ● opt-in account engines |
| 内容质量不足才升级 | — doctor 非内容 | ● quality gate | △ 上下文选择 | △ LLM selection | ● answer eval | ● sufficiency | 不适用 | △ research/fetch |
| 独立 upstream family 建模 | — | ● | — | — | — | — | 不适用 | 本次未见 |
| RRF/确定性 rank fusion | — | △ 多信号 scorer | — | ● weighted RRF | △ URL rank | — | 不适用 | ● RRF |
| Jev typed judgment | — | — 默认路径 | ● usefulness | — | — 通用 LLM eval | ● 多阶段 | ● 模型 route | 本次未见 |
| 明确 unknown / 未判断 | △ 未 probe | ● failures/stop | △ fallback | △ scope 状态 | △ budget/eval | ● | ● confidence/保持 | △ bench/provenance |
| 抓取后再评证据 | — 由上游负责 | △ enrichment | ● | ● chunk selection | ● | △ 调用者供 evidence | 不适用 | ● research brief |
| 原生宿主薄适配 + 通用入口 | ● CLI/skill | ● MCP/CLI/skill | ● API/MCP/CLI | ● API/MCP | ● CLI/OpenAI API | ● CLI/skill/Hermes | △ Pi-only | ● 多插件/MCP |

| 补充机制 | 已核验项目 | WRR 应借鉴什么 | 不应照搬什么 |
|---|---|---|---|
| Query intent → native 参数编译 | dsh-web-tools、Tavily auto_parameters | 保留 provider 能力而非最低公分母 | 自动参数覆盖用户约束 [R18] [D1] |
| Native-first 协商 | OpenRouter | 从 endpoint capability 决定是否 native | 以模型名称猜能力 [D6] |
| 可观测但不自动影响策略 | web-search-plus-plugin | 分开 health/shadow/policy | 把采样观察说成已自适应 [R17] |
| 工具 schema 真实探测 | Neuronto ARD | 声明、handshake、liveness 分级 | 未经授权执行公共 registry 内容 [R21] |
| 按 transport 区分能力 | AnySearch、Linkup、agent-web-search | 同 provider 多 transport 能力投影 | REST/MCP 参数完全等同假设 [R13] [R14] [D4] [R19] |
| 缓存索引与 live 访问边界 | Codex、Jina Reader | freshness/permission 进入 manifest | shell 网络许可代表 hosted search 许可 [D9] [R28] |

## 10. 关键发现 Top 5

1. **最强组合是 Agent-Reach 的 discovery、agent-search-mcp 的 evidence contract、Hermes/GPT Researcher 的 judgment。** `[推断]` 三者解决不同问题，比迁移完整深度研究框架更贴近 WRR。[R1] [R2] [R3] [R12]
2. **Jev 应替代语义规则决策，不替代确定性安全/能力事实。** `[推断]` 可用集、授权、总预算、explicit override 由本地执行器保障，Jev 只从合法候选中判断。[R11] [R12] [R17]
3. **质量应是有阶段和证据的状态，不是一个漂亮 confidence 数。** 来源运行成功、snippet 相关、正文支持、证据充分是不同判定；空、unknown、部分失败必须保留。[R2] [R3] [R12]
4. **“自动路由”需要拆解语义。** vendor 内模式、query 参数、宿主 native-first、本机后端选择都叫 auto，但不能互换；manifest 必须说明自动化发生在哪一层。[D1] [D2] [D6] [R1]
5. **跨宿主应共享核心合同，而不是共享一份万能提示词。** `[推断]` MCP/CLI/plugin/skill 保持薄适配，宿主原生能力由宿主注入，逻辑 provider 与 transport/upstream family 分开。[R2] [R13] [R14] [R19]

## 11. 仍需确认的问题

1. OMP 的 web/judge 原生角色在扩展内能否直接异步调用，并保留取消、预算、native citation handle？若只能从 Agent eval 调用，Python CLI 的桥接边界需要专门定义。[未核验；宿主专项，不从竞品推定]
2. Jev 在 WRR query 分布上的 choice/score/sufficiency 校准怎样？现有 GPT Researcher 的 28 任务 filter replay、Hermes 小样本时延不代表 WRR 结果质量。[R3] [R12]
3. 哪些 provider/transport 真的独立？需要维护有出处的 upstream family catalog，不能只按 vendor/域名去重，也不能把所有共享搜索后端一概认成完全相关。[R2] [R19]
4. 对 skill 来源，能否取得可执行、可取消、可结构化返回的合同？只有 `SKILL.md` 时应由宿主执行，还是显式转换成 workflow adapter？目前参考项目没有统一答案。[R1] [R13]
5. 官方 hosted 搜索的内部验证、账户额度、服务条款与自托管等价性哪些可被实证？不得以 SDK 开源、匿名可调用或出现 citations 代替这些核验。[R22] [R23] [R26] [D6] [D8]

## 12. 一手来源索引

下列固定 commit 与上表一致；在线官方文档按研究日读取。项目自己的实验数字只作作者报告，不作本文复现实验。

[R1]: https://github.com/Panniantong/Agent-Reach/blob/94f06c1969dfc1834001269d79d3ad0972d9dee6/README.md
[R2]: https://github.com/lennney/agent-search-mcp/blob/dd91577d6972d0d60cae09673d9239a00cf8861d/docs/architecture.md
[R3]: https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/docs/docs/gpt-researcher/gptr/context-filter.md
[R4]: https://github.com/langchain-ai/open_deep_research/tree/1b7d2e80db9faa586165c60e09096dbbfd483a64/src/open_deep_research
[R5]: https://github.com/huggingface/smolagents/tree/96f33faaf028479119ec8d34507b47694cf14e34/examples/open_deep_research
[R6]: https://github.com/ItzCrazyKns/Vane/tree/348feca3e378fb4157b217724ed508dc707f853f/src/lib/agents/search
[R7]: https://github.com/searxng/searxng/blob/1c10abe2d3e4ab7ddcc4e9da1a7d07dc7ca7f8f4/searx/results.py
[R8]: https://github.com/onyx-dot-app/onyx/tree/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a/.agents/feature-map/components
[R9]: https://github.com/khoj-ai/khoj/blob/ae229ca894c0b80ad84664afcfdde523b5e87057/src/khoj/processor/tools/online_search.py
[R10]: https://github.com/jina-ai/node-DeepResearch/tree/fd323b521a51264d497bec333bfb997da1bf3210/src
[R11]: https://github.com/Loveacup/pi-jevflow/tree/be91bb962899dac5bc92fcad99c3b350dcb6cccf
[R12]: https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/docs/search-loop.md
[R13]: https://github.com/anysearch-ai/anysearch-skill/blob/9b91ee215164de60d61c7d63789c891a1948b039/README.md
[R14]: https://github.com/anysearch-ai/anysearch-mcp-server/blob/e3f2701a21e61419ebd140358f0b1abb1f5ded04/README.md
[R15]: https://github.com/sweetcornna/free-search-mcp/blob/8c1a9313a9d48d243b5b2dfcab5b4ee8506db830/README.md
[R16]: https://github.com/DDDMUC/dsh-free-search/blob/4e7d7a77ce69dc465cf03cc30f7df3bc67db6a1d/README.md
[R17]: https://github.com/robbyczgw-cla/web-search-plus-plugin/blob/2f6f8bc478b007718b20846086eae1f76283597c/README.md
[R18]: https://github.com/A3Boy/dsh-web-tools/blob/bb68082d1274dc0d28ed5921bb08c7b5070fae7f/README.md
[R19]: https://github.com/JerryLiu369/agent-web-search/blob/c549bf3eb8681e347833a3d927c79231294236ed/README.md
[R20]: https://github.com/gefsikatsinelou/MetaSearchMCP/blob/3ae8a3d4eff07eee5b5db660370ae0a7b683a582/README.md
[R21]: https://github.com/neuronto/agentic-resource-discovery/blob/78d9f90f9bf5332b3978c7ca91a4cedce5d83ef4/README.md
[R22]: https://github.com/tavily-ai/tavily-python/blob/46505f119e6ac0f30d63a4dce0ed9f923705ad97/README.md
[R23]: https://github.com/exa-labs/exa-py/blob/9d599f8570647442a4ad83f30a3e2270266d9927/README.md
[R24]: https://github.com/parallel-web/parallel-sdk-python/tree/5b3a1d2b3fe8a484c1be317c6363a920768071f1
[R25]: https://github.com/LinkupPlatform/linkup-python-sdk/blob/dacef2c451af6889d9b9e38fb9c8eeb57ac016a5/README.md
[R26]: https://github.com/firecrawl/firecrawl/blob/d0f33391ca9a87f05066c61246141c60c5e98d7e/README.md
[R27]: https://github.com/anthropics/claude-code/tree/ef7bfdca55d906929fe6b782025b93758c325074
[R28]: https://github.com/jina-ai/reader/blob/1574bfd380d249c86c82db4dace0d9c8fe17e2b1/README.md
[R30]: https://github.com/google-gemini/gemini-cli/tree/ed96faedfd5571f055c44f2c9509d62321d1138d
[D1]: https://docs.tavily.com/documentation/api-reference/endpoint/search
[D2]: https://exa.ai/docs/reference/search-api-guide-for-coding-agents
[D3]: https://docs.parallel.ai/search/migrate-to-parallel
[D4]: https://docs.linkup.so/pages/documentation/endpoints/search/overview
[D5]: https://docs.firecrawl.dev/features/agent
[D6]: https://openrouter.ai/docs/guides/features/server-tools/web-search
[D7]: https://code.claude.com/docs/en/tools-reference
[D8]: https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
[D9]: https://developers.openai.com/codex/web-search/
[D10]: https://ai.google.dev/gemini-api/docs/google-search
[R3a]: https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/README.md
[R12a]: https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/README.md
[D10a]: https://ai.google.dev/gemini-api/docs/generate-content/google-search

### 补充的精确路径

- GPT Researcher hybrid retriever/MCP 与宿主入口：[固定 README](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/README.md)。
- Hermes 插件 hooks、宿主安装识别与本机模型发现：[固定 README](https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/README.md)。
- SearXNG engine weight 配置：[settings_engines.rst](https://github.com/searxng/searxng/blob/1c10abe2d3e4ab7ddcc4e9da1a7d07dc7ca7f8f4/docs/admin/settings/settings_engines.rst)。
- Onyx 的非统一许可证：[根 LICENSE](https://github.com/onyx-dot-app/onyx/blob/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a/LICENSE)；[internal-search](https://github.com/onyx-dot-app/onyx/blob/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a/.agents/feature-map/components/internal-search.md)；[federated-search](https://github.com/onyx-dot-app/onyx/blob/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a/.agents/feature-map/components/federated-search.md)；[web-search](https://github.com/onyx-dot-app/onyx/blob/f189d4987e54008ba0ffbf9d7d3f6b30c178eb1a/.agents/feature-map/components/web-search.md)。
- Vane 的双阶段筛选：[classifier.ts](https://github.com/ItzCrazyKns/Vane/blob/348feca3e378fb4157b217724ed508dc707f853f/src/lib/agents/search/classifier.ts)；[baseSearch.ts](https://github.com/ItzCrazyKns/Vane/blob/348feca3e378fb4157b217724ed508dc707f853f/src/lib/agents/search/researcher/actions/search/baseSearch.ts)。
- Pi 本地检查严格不触发云端调用：[jev-router-pool-check.ts](https://github.com/Loveacup/pi-jevflow/blob/be91bb962899dac5bc92fcad99c3b350dcb6cccf/lib/jev-router-pool-check.ts)。
- Parallel 异步 processor 与交互接口区别：[Processors](https://docs.parallel.ai/task-api/core-concepts/choose-a-processor)；Linkup MCP 参数子集：[官方 MCP](https://docs.linkup.so/pages/integrations/mcp/mcp)。
- Firecrawl search/fetch 组合：[官方 Search](https://docs.firecrawl.dev/features/search)；OpenRouter 旧接口迁移：[旧 plugin 官方页](https://openrouter.ai/docs/guides/features/plugins/web-search)。
