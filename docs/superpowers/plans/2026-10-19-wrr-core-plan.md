# WRR 7 实施计划（任务图）

依据：[设计稿 rev 2](../specs/2026-10-19-wrr-core-design.md)，用户 2026-10-19 批准。每个任务：产物、验收、阻塞关系。任务编号 T<切片>.<序>。
约束：产品代码只在批准后的本计划范围内；每个任务完成后跑该任务的测试，整体套件在切片末跑一次；不动 `fetch` 语义。

## 切片 1：内核合同（无阻塞，可并行 T1.1–T1.3）

| 任务 | 产物 | 验收 |
|---|---|---|
| T1.1 contracts | `wrr/core/contracts.py`：`SourceDescriptor`、`SourceProfile`、`SourceRun`、`Candidate`、`Plan`、`Quality`、`Evidence`、枚举 `Transport/Kind/PrivacyClass/Provision/Health` | 契约测试：Evidence 可 round-trip JSON；`results[i].url` 必来自某 `SourceRun`（校验函数） |
| T1.2 judge 抽象 | `wrr/core/judge.py`：`Question`（noul/choice/score）、`Answer`、`JudgeResponse`、`Judge` Protocol、`NoJudge`、`ReplayJudge`（测试用）、`TypeSafeJudge`（httpx，无重试，绝对 deadline，复用 `search_filter` 的 passage/敏感启发式）、`resolve_judge()` | 离线 transport 测试：wire body `{model,state,questions}`、`noul` 映射、超时/畸形 → `JudgeError`，不 fail-open |
| T1.3 rubric | `wrr/core/rubric.py`：plan/候选/交付三组问题构造函数（JSON state + criteria）、阈值常量、`PlanPolicy`/`SelectPolicy` 纯函数 | 回放测试：固定 answers → 固定 plan/select 输出；accept/uncertain/reject 三区；forced:primary 记录 |

## 切片 2：inventory（阻塞于 T1.1）

| 任务 | 产物 | 验收 |
|---|---|---|
| T2.1 manifest v2 | `loader.py` 增量字段：`transport`、`upstream_family`、`privacy_class`、`profile`、`capabilities.freshness`；builtin manifests 补齐 | 全部 builtin manifest 通过 schema；缺 family 的 manifest 报错 |
| T2.2 inventory | `wrr/core/inventory.py`：合并 manifests + configured sources + MCP + `HostCapabilities` → `SourceDescriptor[]`；provision/health 两轴；routable 判定；TTL 缓存 | `wrr inventory --json` 输出；unhealthy/cooldown 不 routable；project 级未 trust 不 authorized |
| T2.3 A 档执行器 | `wrr/core/exec_declarative.py`：移植 `configured_sources.py`（命令隔离、字段映射）扩展到 HTTP JSON 与 MCP tool | 一份 YAML 接 `bili search`（或任一本机 JSON CLI）出结果，不改 .py |

实现约定（T2.1–T2.2）：`schema_version: 1` 保持兼容；新增可选顶层 `source` 块，
包含 `transport/kind/upstream_family/privacy_class`，以及可选的 `profile/primary/fresh`。
缺块的旧 manifest 仍可加载，inventory 输出 `source_block=missing` 与推断默认值；
存在但无效的块拒绝加载。`wrr inventory --json` 返回 `sources`、`routable` 与按
`provision/health` 分组的 `summary`；不带 `--json` 输出表格。支持 `--runtime`、
`--env`、`--trust-project`。发现 builtin、用户配置目录的 `engines/`、
项目 `plugins/engines/`、显式 `WRR_COMMUNITY_CONFIG` 与宿主注入来源；热路径仅
复用 registry 的 light 检查和有效期内缓存，不执行 live probe 或恢复。

## 切片 3：流水线（阻塞于 T1.*、T2.2）

| 任务 | 产物 | 验收 |
|---|---|---|
| T3.1 plan | `wrr/core/plan.py`：确定性门 + 检索前 judge + policy | DOI（含 URL）/local 全程零 judge；显式 sources 只跳过选源 judge；回放下 plan 确定 |
| T3.2 execute | `wrr/core/execute.py`：并发、deadline、取消链、`SourceRun` | 取消返回 `cancelled`；部分失败保留 |
| T3.3 fuse | 移植 `_fusion.py` 到 family 去重 | 同 URL 同 family 取最大贡献 |
| T3.4 judge①/select/judge②/recover | `wrr/core/pipeline.py` | 回放下 posts ≤3 常规、≤4 恢复；private 不进 state；inj 中间区标记 |
| T3.5 adapter 移植 | 现有 engines → `SourceAdapter` 接口 | 每 adapter 一条 mock 测试 |
| T3.6 CLI 切换 | `wrr search` 走新内核；`--sources`、`--json` | 真实 Exa+DeepSeek 实跑一次 |

## 切片 4：OMP（阻塞于 T3.6；T4.0 可提前）

| 任务 | 产物 | 验收 |
|---|---|---|
| T4.0 假设核验 | 试验扩展：import `judgment` subpath、`resolveJudge`、`ctx.invokeTool` 原生 `web_search` 元数据 | 记录到 `docs/research/`；决定 HostJudge 实现路径 |
| T4.1 RPC | 内核 `--rpc` 模式 NDJSON：`judge/host_search/progress/cancel` | 单测：扩展侧 mock 往返 |
| T4.2 扩展重写 | `integrations/omp.mjs`：同名 `web_search` wrapper、HostJudge、HostSearch、hop 防护 | 新会话烟测 `plan.judge=host` |

## 切片 5：peers（阻塞于 T3.6）

| 任务 | 产物 | 验收 |
|---|---|---|
| T5.1 serve | `wrr serve`：`/v1/inventory`、`/v1/search`、`/v1/cancel`；仅 tailnet/loopback；bearer | 拒绝 0.0.0.0；无 token 401 |
| T5.2 remote transport | `peers.yaml`；remote `SourceDescriptor`；hop 防护；family 跨设备去重 | 本机 ↔ macmini 真实合并搜索，`runs[]` 可辨设备 |

## 切片 6：其他宿主（阻塞于 T3.6）

| T6.1 Hermes backend plugin | `WebSearchProvider`；旧 override 兼容 | macmini 实跑 |
| T6.2 Pi extension | classifier judge + RPC | 本机装 pi 后烟测 |
| T6.3 Claude Code MCP | stdio server | `claude mcp add` 后一次调用 |

## 切片 7：清洁与发布（阻塞于全部）

| T7.1 删除清单 | 设计 §7 | 测试套件绿；`grep MODE_DISPATCH` 为空 |
| T7.2 文档 | README、7.0.0 notes、L1 取代项、proposal 标注 | — |
| T7.3 评估集 | 抽样、脱敏、初标、跑分 | 达设计 §8 通过线或记录差距 |
