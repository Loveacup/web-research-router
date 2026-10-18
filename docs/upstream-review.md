# WRR 优化前的上游与能力检查

## 每次优化的固定步骤

AlexCai 于 2026-10-18 明确要求：每次优化 WRR，既检查已吸收能力的项目有没有上游更新，也检查 GitHub 热门项目有没有值得吸收的新能力。

1. **已有来源检查**：从 `wrr/deps.py`、engine manifests、实际 adapter 调用及安装来源识别上游；不只查 Python 依赖。记录实际使用 revision/version、上次已审阅 revision、当前 upstream revision、变更及调用合同。安装目录 HEAD 不等于 WRR 的版本锁定。
2. **GitHub 新能力扫描**：兼顾成熟热门项目和近期新项目；记录检索时间、热度、活跃度、许可证与一手来源。按 WRR 的能力缺口筛选，不按 star 数直接决定接入。
3. **工程判断**（本项目执行方式）：将发现分类为兼容风险、可吸收能力、暂不采用；说明落点、与已有能力的重复、认证/费用/隐私边界以及验证方法。来源相同的多个包装器不自动算独立证据。
4. **记录与验证**：在本文件追加检查记录，并在实际升级的 release notes 中记录采用/未采用决定与验证结果。查到更新不等于已经升级；帮助页、静态源码和模拟输入不等于真实平台可用。

检查是优化任务的前置步骤，不新增 cron、自动升级服务或后台采集；不得因为有新版本就自行拉取到共享工具工作树、安装或改变登录态。候选能力仍须遵守项目的方案、授权和验收边界。

## 2026-10-18 检查记录

基线：WRR `0a802e0`（6.2.0）。本节记录前置研究，当时未改运行时代码；后续实施见文末「6.3.0 实施记录」。共享外部工具未升级。

### 已有上游

通过 macmini 上各仓库的 `git fetch origin` 与 revision 比较取得下表。fetch 只更新远端引用，未切换工作树；提交数不是能力增量或落后程度评分。OpenCLI 安装版本来自本机 `opencli --version`，不代表 macmini 版本。

| 上游 | 观测到的使用版本 | 本次检查上游 | 结论 |
|---|---|---|---|
| [last30days-skill](https://github.com/mvanhorn/last30days-skill/compare/3f7c8c5...0341304) | macmini `3f7c8c5` | `0341304`，ahead 446 | 升级前适配新的 agent JSON `results[]` 和条件性 `--plan` 门 |
| [last30days-skill-cn](https://github.com/Jesseovo/last30days-skill-cn/compare/47c1913...171aa8f) | macmini `47c1913` | `171aa8f`，ahead 19 | 保留原查询入口；候选为 `--hot` 热榜及可选全球来源，需独立的行为合同 |
| [Agent-Reach](https://github.com/Panniantong/Agent-Reach/compare/a19a171...94f06c1) | macmini `a19a171` | `94f06c1`，ahead 4 | 此范围主要为文档/维护，无须据此改 WRR runtime |
| [OpenCLI](https://github.com/jackwener/OpenCLI/releases/tag/v2.0.0) | 本机 1.8.5 | v2.0.0 | JSON envelope 改变；适配后才能升级 |
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | macmini 未在约定路径安装 | main `cab1b5c`；tag v0.1.4 `c8b6421` | 候选为 OpenAlex 引用/被引关系；机构源不可当作已获授权 |

这不是全部依赖的更新认证：QMD、SearXNG、Hermes 等未在本轮完成版本差分，后续相关优化仍须检查；依赖清单不封闭。

### 优先兼容风险

- **OpenCLI v2 JSON**：[上游 `emitResult`](https://github.com/jackwener/OpenCLI/blob/v2.0.0/src/command-output.ts) 返回 `{ok:true,data:...}`；WRR `OpenCliSourceAdapter` 只读顶层数组或 `results` / `items`。实际调用 WRR adapter、注入包含一条结果的模拟 v2 envelope，输出 `outcome=empty, items=0`。这是离线合同重放，不是真实 v2 平台调用。修复时还须处理失败 envelope，不能仅无条件读取 `data`。
- **last30days EN JSON**：[上游 schema](https://github.com/mvanhorn/last30days-skill/blob/0341304f8ecb307ca0a939e48ecd002761ef23b2/skills/last30days/scripts/lib/schema.py) 的 agent profile 输出 `results[]`；当前 `Last30DaysSourceAdapter` 只读 cluster IDs 和固定中文平台数组。注入包含有效 URL 的模拟 agent JSON，实际 adapter 返回零条。另须核验 cluster candidate ID 不能直接当 URL 使用。
- **last30days EN 执行前置条件**：[上游执行入口](https://github.com/mvanhorn/last30days-skill/blob/0341304f8ecb307ca0a939e48ecd002761ef23b2/skills/last30days/scripts/last30days.py) 在部分 agent-host 环境要求 `--plan`。WRR 目前只传 `--emit json --quick <query>`。本轮未在真实宿主复现该门，不以清除宿主标识绕过它。
- **V2EX 已有配置不匹配**：本机实际执行 `opencli v2ex --help`，只列出 hot/latest/node/topic 等，没有 `search`；WRR 配置却调用 `opencli v2ex search`。不能把热榜替代品冒充查询搜索；需核对运行机器是否有自定义 adapter，或选择真实支持检索的渠道。

### 已有项目可吸收能力

- last30days EN：统一结果 schema，以及 Techmeme、dev.to 等公开来源；StockTwits 可作金融话题候选。发现/钻取/时效核验属于额外工作流，不能直接塞进既有短时搜索预算。依据：[source registry](https://github.com/mvanhorn/last30days-skill/blob/0341304f8ecb307ca0a939e48ecd002761ef23b2/skills/last30days/scripts/lib/pipeline.py)。
- last30days CN：热榜和跨平台话题聚合，独立于关键词查询。依据：[本轮比较范围](https://github.com/Jesseovo/last30days-skill-cn/compare/47c1913...171aa8f)。
- OpenCLI：本机帮助页确认 bilibili、zhihu、weibo、youtube、jike、tieba、hupu、zsxq 有 search 命令，但未测试登录或结果质量；Bluesky 的 search 是用户搜索，不应当作帖子搜索。本机 `linuxdo` 未列出 search。优先评估前三个中文渠道，勿仅凭 catalog 宣称可用。
- paper-search-mcp：[OpenAlex 引用关系](https://github.com/openags/paper-search-mcp/blob/cab1b5cc276a0cd004a0044003926a6440088049/docs/OPENALEX_RELATIONSHIPS.md) 可增强 `academic.py`；WoS/Scopus 依赖机构资格，[上游也标记了验证边界](https://github.com/openags/paper-search-mcp/blob/cab1b5cc276a0cd004a0044003926a6440088049/docs/INSTITUTIONAL_CONNECTORS.md)。

### GitHub 新能力候选

检索同时覆盖近期创建和近期活跃仓库。以下是研究候选，不是已经接受的集成计划；热度来自研究时 GitHub 元数据快照，不宣称近期 star 增长排名。低星项目列作能力线索，不标成热门。

| 项目 | 观测热度 | 值得评估的缺口 | 工程判断 |
|---|---:|---|---|
| [Firecrawl](https://github.com/firecrawl/firecrawl) | 192,459 stars | 搜索后正文获取、结构化抓取 | 优先评估抽取链；避免继续叠加同质通用搜索。AGPL，自托管/托管 API 的许可证与费用分别核查 |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | 85,719 stars | 动态网页及自托管正文提取 | 与 Firecrawl 对照实测后选型，不默认两套一起上；Apache-2.0，浏览器运维是新增成本 |
| [DonSeTch](https://github.com/dondai44423/donsetch) | 850 stars | 多源共识、无 key 检索、抓取升级策略 | 借鉴方法而非立即嵌套聚合器；AGPL，须核验来源独立性与接口稳定性 |
| [browser-search](https://github.com/Johell1NS/browser-search) | 535 stars | 浏览器正文提取 | 仅看 extract 能力；SearXNG 已有，反爬升级不得绕过授权/访问边界 |
| [Nature Academic Search](https://github.com/wp-a/nature-academic-search) | 326 stars | 引文核验、PubMed/临床试验等学术补充 | 适合 academic 增强；先验证实际来源和引用正确率 |
| [Agent Search MCP](https://github.com/lennney/agent-search-mcp) | 111 stars | provider-family 归因与质量门 | 与现有 RRF/质量合同对照；同源包装不重复计票 |
| [Linkup MCP](https://github.com/LinkupPlatform/linkup-mcp-server) | 29 stars | 域名/日期过滤、深度检索 | 按预算候选；Streamable HTTP 不是当前 stdio MCP 的直接替代 |
| [Parallel Search MCP](https://github.com/parallel-web/search-mcp) | 26 stars | search/fetch 与有限匿名入口 | 需验证限额、可靠性及 HTTP MCP 传输；不把免费入口视作稳定生产承诺 |

star 元数据来源为各仓库对应 `https://api.github.com/repos/<owner>/<repo>`。这八项尚未安装或做真实 WRR 接入测试。GPT Researcher 等整套研究报告应用不直接作为另一个 `SearchResult` 后端；可另行借鉴方法，但不替换 WRR 的职责。

### 本轮建议次序与验收门

1. 先处理 OpenCLI/last30days 合同风险及 V2EX 命令不匹配；隔离验证旧/目标版本、失败分类与真实查询，再决定上游升级。
2. 再评估 Bilibili/知乎/微博社区来源、Techmeme/dev.to 的独立增量；检查认证、时延、去重和查询相关性，不只看返回条数。
3. 最后对照 Firecrawl/Crawl4AI 的正文质量与成本，评估引用核验/来源独立性；不默认增加更多通用搜索主力。

以上为前置研究阶段的结论，不代表候选均已接入。实际采用与验证如下。

## 6.3.0 实施记录

### 已采用的源码能力

| 来源 / revision / 许可证 | 实际阅读的源码 | WRR 落点与边界 |
|---|---|---|
| OpenCLI `74fe1dc11c26d44b2a01102fd341aa55ae407774` / Apache-2.0 | [`command-output.ts`](https://github.com/jackwener/OpenCLI/blob/74fe1dc11c26d44b2a01102fd341aa55ae407774/src/command-output.ts)、[`errors.ts`](https://github.com/jackwener/OpenCLI/blob/74fe1dc11c26d44b2a01102fd341aa55ae407774/src/errors.ts) 与 bilibili/zhihu/weibo search adapters | v1/v2 输出、失败 envelope 和定向社区来源；不自动登录或升级 CLI |
| last30days EN `0341304f8ecb307ca0a939e48ecd002761ef23b2` / MIT | [`planner.py`](https://github.com/mvanhorn/last30days-skill/blob/0341304f8ecb307ca0a939e48ecd002761ef23b2/skills/last30days/scripts/lib/planner.py)、`schema.py`、`pipeline.py` | 真正的 `--plan`/`--search`、新旧结果解析、candidate ID 解析；dev.to/Techmeme 只在安装版具备模块时选用 |
| trafilatura `e2f85c965662c7df26b73290ef175b2c2a5fdca6` / Apache-2.0 | [`core.py`](https://github.com/adbar/trafilatura/blob/e2f85c965662c7df26b73290ef175b2c2a5fdca6/trafilatura/core.py)、`main_extractor.py`、`htmlprocessing.py`、`settings.py` | 安装 Python 包 2.3.1，替换正则去标签；保持既有安全传输，修复空 inline 节点尾文本丢失 |
| paper-search-mcp `cab1b5cc276a0cd004a0044003926a6440088049` / MIT | [`openalex_relations.py`](https://github.com/openags/paper-search-mcp/blob/cab1b5cc276a0cd004a0044003926a6440088049/paper_search_mcp/academic_platforms/openalex_relations.py) | 原生实现一跳 references/citations，核对 seed 和边方向；未安装 MCP 项目 |
| Nature Academic Search `725c2ec894a2f40b7d3f811528b9cc7e4ebff6c8` / MIT | [`europe_pmc.py`](https://github.com/wp-a/nature-academic-search/blob/725c2ec894a2f40b7d3f811528b9cc7e4ebff6c8/src/nature_academic_search/sources/europe_pmc.py)、[`crossref.py`](https://github.com/wp-a/nature-academic-search/blob/725c2ec894a2f40b7d3f811528b9cc7e4ebff6c8/src/nature_academic_search/sources/crossref.py) | 生医学术补源、DOI 精确元数据核对；不把“DOI 存在”写成“论文支持论点” |
| Agent Search MCP `62b0e1304bd5fc15823eaea56da2be007641a7b1` / Apache-2.0 | [`provider-catalog.ts`](https://github.com/lennney/agent-search-mcp/blob/62b0e1304bd5fc15823eaea56da2be007641a7b1/src/engines/provider-catalog.ts)、[`search-evidence.ts`](https://github.com/lennney/agent-search-mcp/blob/62b0e1304bd5fc15823eaea56da2be007641a7b1/src/aggregation/search-evidence.ts) | 独立实现已知检索家族归并；同 URL 的 GitHub/Skill、已知社区包装来源不重复贡献 RRF 分。不是出版者独立性认证 |

除直接依赖 trafilatura 外，采用合同或方法后在现有 WRR seam 中实现；没有整段复制或 vendoring 上游源码。热门大项目用于能力选型，不以 star 数替代源码质量判断。

### 暂不采用

- Firecrawl/Crawl4AI：本轮选择本地正文抽取，避免新增托管 API 费用或浏览器运维。未声称已完成三者的质量/成本对照基准。
- last30days CN 热榜、StockTwits：热榜/发现不是关键词检索的等价替代，不混入现有 search 合同。
- 机构数据库、其他通用搜索聚合器：不假定机构访问资格，也不继续堆叠同质收费搜索。

### Jev 的实测与接入决定

阅读现有 `hermes-jev-skills` 的 `jevkit/client.py`、`jevkit/search.py` 后，使用已有凭据向 TypeSafe `systemone` 调用 `jev-1.13.0`；仅发送 3 个合成公开样例，未发送本地笔记、私密结果或用户凭据。

| 公开样例 | 观察结果 | 墙钟耗时 | 输入 / 输出 token |
|---|---|---:|---:|
| 临床试验问题选路 | 选择 academic，报告置信度 1.0 | 572 ms | 357 / 38 |
| 官方限流说明与博客之间选证据 | 选择官方来源，覆盖度 0.9 | 490 ms | 461 / 60 |
| 仅有轶事时判断死亡率证据充分性 | 充分度 0.12 | 568 ms | 328 / 21 |

这不是准确率或价格基准；返回结果不含账单价格，不能据此承诺省钱。优先候选是**公开候选证据短名单**与**规则无法消歧的选源**。精确 DOI、明确站点、deadline/failure 判断仍走确定性代码，不加模型调用。本轮未把 Jev 接入默认 WRR 热路径，也未新增 Jev 凭据配置或后台调用。

### 实际验证边界

- 本机已实跑 Bilibili 定向查询，返回 2 条真实视频；不能据此宣称知乎/微博或 OpenCLI v2 全平台已验证。
- macmini 原 last30days EN 安装接受生成的 plan；限定 Hacker News 的真实查询返回 6 条，当前 adapter 成功解析。非交互 SSH 需提供 Homebrew PATH 才能找到所需 Python 3.12+；未升级上游工作树。
- Crossref DOI 精确查询、未知 DOI、OpenAlex 两个引用方向、Europe PMC 生医查询均已实际调用；自动 DOI 路由只执行 academic。
- PEP 20 实际正文从丢失核心箴言修复为保留箴言；沿用已配置本机代理的显式 `WRR_FETCH_PROXY`，没有关闭非公网地址检查。
- OpenCLI v2 envelope、部分错误与截止时间边界由离线回归覆盖；这些测试不代替真实 v2 登录态、服务 SLA 或跨宿主部署验收。完整测试结果见 `RELEASE_NOTES_v6.3.0.md`。

## 6.4.0 Jev 搜索筛选：上游与 GPT Researcher 对照

2026-10-18，本轮只检查筛选相关上游；没有升级共享工具。延续前一轮检查，不把这次窄范围检查宣称为全部依赖重新认证。

| 来源 | 当前检查点 | 实际源码结论与采用决定 |
|---|---|---|
| Hermes Jev Skills / MIT | [`b8f69fe68b0efaa006f9baede46c3849aaf789f6`](https://github.com/kerpopule/hermes-jev-skills/tree/b8f69fe68b0efaa006f9baede46c3849aaf789f6)，1,138 stars | 阅读 `jevkit/search.py`、`rerank.py`、`privacy.py`、`client.py`。采用独立相关性/注入 `noul`、匿名位置编号、完整输入先筛隐私、稳定排序与未评估项披露。原生异步实现，不运行时导入宿主 skill |
| GPT Researcher / Apache-2.0 | [`3582a6841fe7dc801e2a34e34ee408641aea9bce`](https://github.com/assafelovic/gpt-researcher/tree/3582a6841fe7dc801e2a34e34ee408641aea9bce)，30,149 stars | [`context/jev_filter.py`](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/context/jev_filter.py) 已原生调用 Jev。采用“同主题但不能帮助回答”与“直接答案/有用支撑事实”的区别，不把其 0–3 score 当概率 |
| Agent Search MCP / Apache-2.0 | 已吸收 `62b0e13` → [`dd91577d6972d0d60cae09673d9239a00cf8861d`](https://github.com/lennney/agent-search-mcp/compare/62b0e1304bd5fc15823eaea56da2be007641a7b1...dd91577d6972d0d60cae09673d9239a00cf8861d)，111 stars | 9 个提交，差异为 changelog、依赖锁、发布策略和回归测试；没有 `src/` 变更。家族归因/排序方法无需因此改动，Jev 不新增独立来源票数 |

Hermes 安装目录是无 `.git` 的源码快照，不能报告“安装 HEAD”。实际 blob 比较：`search.py`、`rerank.py`、`privacy.py` 与当前上游一致；`client.py` 与 `98ce26aac5e1e4a764d982797c2c45e727e5a765` 一致。此基线到当前主要增加 image/assets/box/vision，不属于搜索筛选，本轮不吸收。文件匹配不等于整仓版本匹配。

### GPT Researcher：读取了什么，没搬什么

- [`context/select.py`](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/context/select.py)：有 key 时 auto 选择 Jev，失败可走关键词。WRR 不照搬自动按 key 启用或新 BM25 路径；显式开关，失败保留原有 RRF 结果。
- `JevContextCompressor` 是**抓取正文之后的分块上下文筛选**，不是搜索 provider。上游每块一请求、默认并发 64、30 秒及多次重试；WRR 改为检索合并后至多一个批量请求，共享剩余预算，不先抓所有正文。
- [`context/retriever.py`](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/context/retriever.py) 保留原 URL；WRR 直接返回原 SearchResult，避免让模型生成链接或改写引用。
- [`skills/researcher.py`](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/skills/researcher.py) 的 visited-URL 抑制不替代 WRR 的规范化去重；不采用 URL shuffle。`curator.py` 的聊天模型生成源记录、`deep_research.py` 的递归研究/报告流程均不接入。

Hermes 的本地规则也不整套照搬：其源码明确记录，裸匹配“run the following command”会误伤安装文档。WRR 保留普通操作说明；局部规则仅作有限补充，不宣称完整提示注入防护。相关性、注入风险、事实充分性是三件事，本轮只做前两项筛选，不推导“可以回答”。

### 本轮热门能力窄扫描

通过 GitHub 仓库搜索检查 `reranker stars:>1000`、近期创建的 reranker、`rerank in:name stars:>1000` 和 FlashRank；热度是查询快照，不是增长排名。

- [AnswerDotAI/rerankers](https://github.com/AnswerDotAI/rerankers/tree/5b9cbb073335b0246bc33e939600f51f934c460d)：1,641 stars，Apache-2.0。阅读 `results.py` 与 `models/ranker.py`；保留文档身份的原则有用，但没有理由为一次原生 HTTP 调用再增加统一模型框架。
- [FlashRank](https://github.com/PrithivirajDamodaran/FlashRank/tree/11f7a40d625f2de047c371c81c6f477386c18bb1)：1,010 stars，Apache-2.0。阅读 `Ranker.py`；会加载/下载模型并使用 ONNX 或 llama_cpp，新增模型、运行时及许可证核查成本。本轮不安装，不声称其准确率、时延或中文能力优劣。

没有依据上游硬编码价格或项目宣传承诺 WRR 的降本/准确率收益。本轮实跑和回归结果记录在 `RELEASE_NOTES_v6.4.0.md`。
