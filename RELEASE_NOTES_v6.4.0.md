# v6.4.0 — 可选 Jev 搜索结果筛选

在 v6.3.0 上游适配、学术检索与正文提取改进之上，新增检索后筛选，不新增搜索引擎。Exa、DeepSeek 仍是主力检索来源；Jev 只判断已获得的候选，不生成链接、补搜或写研究报告。

## 运行合同

- `wrr/search_filter.py` 原生异步调用固定 TypeSafe `/v1/systemone`，使用 `jev-latest`。每次搜索最多一个批量请求，无重试、重定向、候选网页抓取或模型下载。
- 在单引擎结果或 RRF 合并之后、Stage S 决策证据生成之前执行。每候选分别询问相关性与指令注入风险；相关性至少 0.5、注入风险低于 0.5 才保留，另有有限本地指令筛查。相关性降序，相同分数保留原顺序。
- 最多评估 20 条；query 上限 1,500 字符、每候选投影片段 900 字符，转义 JSON state 不超过 60,000 字符。完整输入先检查敏感内容，再截断；未评估溢出项保留原序并明确标记，不宣称已通过模型筛选。
- 返回原 `SearchResult` 对象，不改写 URL、摘要、highlights、时间、RRF 或来源归因。Jev 不增加独立检索来源票数；原有来源数量也不是事实充分性证明。
- 合法判定全部拒绝时返回空结果，质量为 `insufficient`，不触发恢复搜索。超时、HTTP/网络故障或畸形回复原子地保留原结果，标记 `fail_open`；原本 complete 的质量改为既有合同的 `degraded_success`，决策证据继续生成。

## 配置与隐私边界

默认关闭。仅在授权的 WRR 环境中同时提供以下设置才会调用：

```sh
export WRR_JEV_FILTER=1
# TYPESAFE_API_KEY 由已有的安全凭据注入流程提供，不粘贴进命令历史或项目文件。
export WRR_JEV_FILTER_TIMEOUT=2
wrr search "Why does Earth's axial tilt cause the seasons?" --route-mode grounding --count 5 --json
```

超时默认 2 秒，最多 3 秒，且受整次搜索剩余截止时间限制。只提供 key 不会自动启用。无需安装 Hermes Jev Skills、GPT Researcher、LangChain 或其他重排框架。

- 只允许公开 web/academic provider 范围。精确 DOI/引用关系查询、本地意图、含成功登录态或自定义来源的结果绕过筛选，包括 github、skill、community、本地引擎。
- query 与完整候选标题、URL、摘要及 highlights 先做启发式敏感检查；highlights 本身不发送。任一候选命中即整批跳过，不先截短或移除敏感部分后继续上传。
- 检查包括本地/私有 URL 形态、路径、凭据赋值、联系信息和短值凭据查询参数，并做 Unicode 规范化与有限百分号解码。普通公开 URL、安装命令、一般说明文字不能仅因形似路径/指令而被拒绝。
- `diagnostics.search_filter` 给出状态、固定原因、数量、位置编号、分数及有限模型/usage 字段，不回显 query、URL、候选文本或远端原始错误。

这些规则不是完整隐私或提示注入防护。公开 URL 不保证内容公开；保守检查可能误报。相关性高也不意味着内容正确、时效合适或证据足够。失败保留的结果没有因此获得安全背书。

## 源码吸收，而非整套搬入

| 来源 | 本轮采用 | 不采用 |
|---|---|---|
| [Hermes Jev Skills `b8f69fe`](https://github.com/kerpopule/hermes-jev-skills/tree/b8f69fe68b0efaa006f9baede46c3849aaf789f6) / MIT | 相关性与注入分开判定、匿名位置、完整输入先筛隐私、稳定排序、显式未评估记录 | 多批线程扇出、第二次充分性/下一轮问题调用、vision/assets；不运行时导入宿主 skill |
| [GPT Researcher `3582a68`](https://github.com/assafelovic/gpt-researcher/blob/3582a6841fe7dc801e2a34e34ee408641aea9bce/gpt_researcher/context/jev_filter.py) / Apache-2.0 | 区分“同主题但无助回答”和“直接答案/有用支撑”；保留原引用身份 | 每块一请求、并发 64、长超时与重试、自动按 key 启用、递归研究与报告流程 |

GPT Researcher 的原生 Jev 模块筛选的是抓取后的正文块；WRR 筛选搜索候选摘要，不能把前者的质量或成本宣传直接移植到本项目。其 0–3 `score` 也未当作 WRR `noul` 概率使用。

本轮同时复查已吸收的 Agent Search MCP，并窄扫描 rerankers、FlashRank；结论、许可证、修订号、安装快照与源码差异记录在 [上游检查文档](docs/upstream-review.md#640-jev-搜索筛选上游与-gpt-researcher-对照)。未升级共享工具。

## 真实调用验证

下表是单次功能 smoke，不是基准测试、价格结算或准确率评测。使用已有授权 key，仅在验证进程注入；未复制到项目文件或另行持久化。

| 路径 | 输入 → 保留 | 实测耗时 | 可观察结果 |
|---|---:|---:|---|
| 原生筛选，3 条受控公开样例 | 3 → 1 | 494.9 ms | 保留有用说明，丢弃无关内容与注入指令；模型 `jev-1.13.0`，usage 759 input / 112 output |
| 真实 Exa → Jev，TaskGroup 问题 | 5 → 2 | 整次 3,454.14 ms | `status=ok`；独立来源仍为 1；usage 1,766 / 184 |
| 真实 Exa + DeepSeek → RRF → Jev，四季成因 | 5 → 3 | 整次 5,121.74 ms | 两个主力各成功检索 5 条，RRF 截至 5 条后筛选；usage 1,632 / 184 |
| 真实 Exa + DeepSeek，另一次 TaskGroup 查询 | 6 → 6 | 整次 2,465.31 ms | 某候选命中敏感启发式，`skipped/sensitive_candidate`，发送/评估均为 0；不据此断言原文一定敏感 |

并行实跑中 Brave 没有配置 key，路由如实保留 `degraded_success`；Jev 不掩盖来源失败，也不充当第三个来源。一次请求少取几条结果不等于已经证明端到端提速、降本或提高召回率。

安装后的 CLI 已实跑；本机 editable 包元数据与 `wrr.__version__` 均为 6.4.0。开发验证阶段没有提交、推送、跨宿主部署或全局启用；后续授权部署结果另记于本文。

## 回归验证

- 定向集：DeepSeek 既有合同 + Jev 筛选 + 路由集成，**150 passed，0.89 秒**。
- 完整单元集：**1795 passed、15 skipped，94.32 秒**。

```sh
WRR_V6_ROUTER=0 .venv/bin/pytest tests/unit -q -o addopts= -p no:cacheprovider -o faulthandler_timeout=60 --tb=short
```

覆盖原对象保留、稳定排序、全拒绝、重复结果只评一次、无额外恢复、精确书目/本地/非公开范围绕过、完整输入先筛隐私、凭据 URL 参数、普通文档近似指令、Unicode 大小边界、截止时间与取消、401/403/429/503/重定向、畸形/缺失/非有限得分，以及 Stage S 证据一致性。

集成验证期间修复了三个具体问题：Windows 盘符模式误匹配普通 HTTPS URL；旧引擎测试直接替换 HTTPX 客户端后泄漏至后续用例；筛选故障的质量状态必须使用既有 `degraded_success`，否则 Stage S 证据校验拒绝记录。未通过放宽生产异常处理、取消重定向限制或关闭隐私检查来让测试通过。
