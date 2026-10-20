# WRR 重构方向综合（2026-10-19）

> 状态：方向备选，待用户决策。输入为同日四份研究：
> [同类开源项目](2026-10-19-oss-search-routers.md)、[学术依据](2026-10-19-academic-routing-verification.md)、
> [宿主接入面](2026-10-19-host-integration-surfaces.md)、[Jev 能力与成本](2026-10-19-jev-capability-and-cost.md)。
> 标 `[推断]` 的是架构建议；其余有来源链接的事实见各研究文档。

## 1. 四份研究收敛出的共同结论

1. **分层不变，决策实现换掉。** 经典 federated search 的「来源描述 → 选源 → 结果合并」分层（CORI / Shokouhi&Si）在 LLM 时代仍成立；Jev 替代的是关键词 `classify_intent` 与 8 modes 这种语义规则，不替代来源契约、授权事实与 RRF 基线。
2. **Jev 建议，代码掌权。** WRR 现有 filter、hermes-jev-skills、pi-jevflow 三个实现一致：可用集、授权、预算、显式 override、安全硬门由确定性代码裁剪，Jev 只在合法候选中判断；失败回退到确定性默认，不 fail-open 安全门。
3. **常规路径 2 次 Jev，约 +1 s，约 $0.0005/次。** 检索前 1 次（意图 choice + 每来源 bool，同 state 合并），检索后 1 次（每候选 relevance/injection + 原始集合 enough + next_action）。依赖筛选后集合再评价才需第 3 次。官方：输入 $0.042/Mtok、输出免费、64k/request、32k/question。
4. **宿主原生能力是注入依赖，不是 WRR 重建对象。** OMP 已有 `modelRoles.web` + `fallbackChains.web`（search-kind provider 目录）与 `modelRoles.judge`（`typesafe/jev-latest`），扩展可用同名 `web_search` wrapper 经 `ctx.invokeTool` 委托原生链、经 `@oh-my-pi/pi-coding-agent/judgment` 的 `resolveJudge` 复用 judge role；Pi 有 `modelRegistry.classify` + virtual model；Hermes 推荐 `WebSearchProvider` backend plugin 而非 override `web_search`；Claude Code 有 MCP 与新版 mod 两条路。
5. **「自动路由」必须拆成四层并各自命名**：vendor 内 auto 参数、query→provider 参数编译、宿主 native-first、本机 backend 选择。Agent-Reach 的 channel→backend 体检 + agent-search-mcp 的 evidence contract（provider family、quality gate、stop reason、部分失败）+ Hermes/GPT Researcher 的 typed judgment 是最贴近 WRR 的组合；完整深度研究框架不适合迁入。
6. **质量是分阶段的状态，不是一个 confidence 数。** 运行成功 / snippet 相关 / 正文支持 / 集合充分是四个判定；`unknown`、`unjudged`、`insufficient`、部分失败必须保留。CRAG（好/差/模糊三路）+ QPP-GenRE（先逐条判有用再汇总）是最值得迁移的两篇。

## 2. 现有代码库对照

| 现有模块 | 研究结论下的去向 |
|---|---|
| `wrr/router.py` v5 `route_search_v5` + `classify_intent` + `MODE_DISPATCH/MODE_WEIGHTS`（8 modes） | **替换**：意图/证据形态改为 Jev choice；来源适配改为 per-source bool；确定性前置门（DOI、显式 site/provider、local intent、deadline）保留为解析器 |
| `wrr/engines/registry.py` v6 descriptor registry（shadow）、`selection_shadow.py`、Stage S/C、campaign sampler | **收编/删除**：registry 的 discovery/health/trust 分级演化为唯一 inventory（installed/configured/authorized/healthy）；shadow 比较、Stage 门、campaign 删除 |
| `wrr/search_filter.py`（rel/inj 双问） | **扩展**：成为检索后判断层，加 answer-utility、enough、next_action；保留原结果身份、unjudged 标记、fail-open 语义 |
| `_fusion.py` RRF + family 去重 | **保留**为确定性合并基线；upstream family 目录需有出处（Exa 经 MCP/API 仍是同一 upstream） |
| 引擎 adapter（exa、deepseek、brave、academic、github、community/opencli、local_*、mcp_source、configured_sources、skill_discovery） | **保留并移植**到新 adapter 合同（logical provider / transport / upstream family / evidence 字段） |
| `integrations/omp.mjs`（spawn CLI，注册 `wrr_search/fetch/similar`，skill handoff 状态机） | **重写**：同名 `web_search` wrapper + `HostSearch`/`HostJudge` 注入；skill handoff 简化为推荐+交接合同 |
| Hermes `__init__.py` override `web_search` | **改为** backend plugin（目标版本确认后），旧 override 作兼容 |
| `doctor.py`、`deps.py`、`requirements.py` | 并入 inventory 探测（static/light/live 三级），不在 search 热路径运行 |

## 3. 三个方向备选

### A. 新内核 + 清洁切换（推荐）`[推断]`

- 新建 `wrr/core`（Python）：`inventory`（发现/授权/健康）→ `plan`（确定性门 + Jev 检索前判断）→ `execute`（adapter 并发、deadline/cancel/预算）→ `fuse`（RRF + family）→ `judge`（Jev 检索后判断）→ `evidence`（统一输出 + 阶段状态）。
- 宿主薄适配：OMP extension（native HostSearch/HostJudge）、Hermes backend plugin、Pi extension（classifier 注入）、Claude Code MCP（+ 可选 mod）、独立 CLI。
- Judge 抽象：`HostJudge`（OMP judge role / Pi classifier）→ 无宿主 judge 时 WRR 自有 TypeSafe key → 无 key 时 0 次 Jev 走确定性默认。
- 删除：v5 modes、v6 shadow、Stage S/C、campaign、`wrr_similar` 独立工具（归入 search 的 provider 能力）。
- 代价：adapter 移植 + 四宿主适配，约 18k 行中预计一半重写或删除；需要新评估集（真实四宿主 query 抽样 + 人评）。

### B. 渐进替换

- 保留 v5 数据面；`classify_intent` 换成 Jev choice；`search_filter` 加 enough/next_action；OMP 加 HostJudge 桥。
- 优点：风险低、Hermes 行为基线不动。缺点：8 modes/权重表、shadow registry、Stage 门继续存在，「本机有什么用什么」的 inventory 仍是两套（legacy registry + v6）。

### C. 宿主原生壳

- 不保留内核，每个宿主用原生搜索 + Jev 判断 + 共享 evidence schema。
- 优点：最薄。缺点：丢失 academic、community/opencli、local 引擎与跨宿主一致合同；Claude Code/Hermes 原生搜索不可配，质量上限受宿主限制。与「质量优先、整合本机来源」冲突。

## 4. 需要用户裁决的事项

1. 方向 A / B / C。
2. 内核语言：Python（adapter、Hermes 现成）vs TypeScript（OMP/Pi/CC 原生，Hermes 需跨进程）。
3. WRR 交付边界：只交付「排序后证据 + 阶段质量状态 + 充分性信号」，还是承担补搜循环/研究产物。
4. Jev 默认策略：有 judge（宿主或自有 key）即默认开启 + 本地隐私门，还是维持显式开关。

## 5. 定向后首批需核验的技术点（不是用户决策）

- 本机 OMP 18.12.0 单文件发行版能否 import `judgment` subpath 并在扩展内调用 `resolveJudge`；`ctx.invokeTool` 委托原生 `web_search` 的取消与 usage 归属。
- Python 内核 ↔ 宿主 judge 的桥接协议（stdio JSON-RPC 事件 vs 直接 TypeSafe）。
- upstream family 目录：Exa/宿主 web 链/MCP/CLI 哪些共享后端。
- Jev 在中文 query、截断片段上的 choice/bool 校准样本（固定模型版本）。
