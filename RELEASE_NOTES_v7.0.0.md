# WRR 7.0.0

发布日期：2026-10-20。

WRR 7 清洁切换到 `wrr/core` 搜索内核。CLI、OMP 与 Hermes backend 共用来源发现、选源、执行、融合、质量判断与有限恢复。fetch/similar 本轮保持原有语义。

**评估状态：未跑评估集（T7.3）。** 本版本没有选源召回、筛选误杀、充分性 Brier 或 p95 延迟达标结论。单元测试与真实宿主搜索不能替代评估集。

## 变更

- `wrr search` 仅执行新内核；输出 `plan/runs/results/quality/diagnostics`。
- Jev 在有宿主 judge 或 WRR 授权环境中的 TypeSafe key 时默认启用。无 judge 时返回确定性默认计划，并标记 `plan.judge=none`。
- 常规路径最多 3 次 judge；恢复路径最多 4 次，至多补搜 1 轮。显式来源仍接受候选筛选与交付充分性判断，但不自动补搜。
- 隐私门保留：云端只接收 query、公开候选 title/url/snippet 与公开来源画像。敏感 query 跳过判断；私密、本地、登录态及敏感候选不进入云端 state。
- README 改写为 WRR 7 的架构、来源接入与宿主指南。旧工程提案顶部标记由 2026-10-19 设计稿取代。

## 删除清单

| 范围 | 删除内容 |
|---|---|
| 搜索路由 | v5 search、mode dispatch/weights/classifier、旧 RRF 编排、Stage S 与 shadow 选择代码 |
| 搜索取证 | `selection*.py`、`evidence_gate.py`、`evidence_v2.py`、runtime decision-context/evidence 与 campaign/admission/sampler |
| 旧 Hermes 插件 | 根 `__init__.py`、根 `plugin.yaml`、`wrr/tools/web_search.py`；不再 override Hermes 原生工具 |
| 旧接口与产物 | `--route-mode`、`--mode`、`evidence-gate`、`WRR_V6_ROUTER`、`WRR_JEV_FILTER`、旧 search schema/formatter、旧 overlay 及生成器、架构 HTML/PNG/JSON 快照 |
| 测试 | 只钉死上述旧行为的测试；混合测试保留 fetch/similar、doctor、deps、loader、adapter 与隐私启发式行为 |

历史 `RELEASE_NOTES_v6.0.0.md` 至 `RELEASE_NOTES_v6.4.0.md` 保持原样。

## 最小保留依赖

- `wrr/router.py` 仅保留 extract/similar 的顺序 fallback、预算、显式 provider 隔离、网络安全、正文质量与诊断。
- `wrr/registry.py` 保留 lazy registry 及 adapter 注册，供 fetch/similar 和旧 doctor 探测目标使用；删除 shadow factory。
- `wrr/schemas.py` 保留 adapter 的 Search/Extract/Similar 合同、fetch/similar 路由诊断与 `EngineCheckResult`。新搜索证据合同位于 `wrr/core/contracts.py`。
- `runtime/control_plane.py` 仍被 doctor/install 使用；`runtime/detect.py`、`env.py`、`state.py`、loader、配置来源、MCP 来源和全部 adapter 保留。
- release supervisor 与 `scripts/wrr_release_supervisor*` 不依赖已删除的搜索取证机制，继续保留。此维护入口不构成双宿主候选激活验收。

`search_filter.py` 保留供 core 复用的隐私启发式与片段投影。`fetch_transport.py`、`extraction.py`、fetch/similar formatter、doctor、deps、requirements、errors 和 `wrr/cli` 保留。


OMP skill handoff 的候选投影改为读取新 evidence 的 `plan.sources`、`runs` 与 `results.contributions`，只接受单一 skill 来源的成功结果；不保留旧 search JSON 兼容层。
## 迁移

### Hermes：`wrr-hermes` → `web-wrr`

Hermes 现在保留原生 `web_search`，通过 `WebSearchProvider` backend 调 WRR。先把 WRR 安装到 Hermes 使用的 Python 环境，再切换 search backend。

```bash
ln -s ~/code/web-research-router/hermes-plugin ~/.hermes/plugins/web-wrr
cp ~/.hermes/config.yaml ~/.hermes/config.yaml.before-wrr7
hermes plugins disable wrr-hermes
hermes plugins enable web-wrr
hermes config set web.search_backend wrr
hermes plugins doctor web-wrr --ci
```

已有 symlink 时不必重复创建。保持 `web.backend`、`web.extract_backend` 原值。长期运行的 gateway 需要插件热重载或管理员安排重启。旧根 tool plugin 已删除，不能重新启用 `wrr-hermes` 来恢复搜索。

后端保留 `data.web`，新增元数据位于 `data.wrr.plan/quality/diagnostics`；正文提取继续由 Hermes 原生 extract backend 负责。

### OMP：同名 wrapper

加载 `integrations/omp.mjs` 后，模型调用同名 `web_search`。扩展经 RPC 注入宿主 judge 与 `host:web`；`wrr_fetch`、`wrr_similar` 和 skill handoff 保持可用。

```bash
omp --no-extensions -e integrations/omp.mjs --no-session --mode json \
  -p "用 web_search 搜 what causes the seasons on earth，只回 plan.judge 和 sources"
```

### 参数与环境

- 删除 `--route-mode`；改用自动选源，或显式 `--sources exa,deepseek`。
- 删除旧 `--mode`；Exa search type 改用 `--exa-mode fast` 等值。
- 删除 `WRR_JEV_FILTER`。有可用 judge 即默认开启，无需另设开关；旧 `WRR_JEV_FILTER_TIMEOUT` 也不再控制新内核预算。
- 删除 `WRR_V6_ROUTER`。新内核是唯一搜索数据面，没有新旧路由切换。
- 删除离线 `evidence-gate` 子命令及 Stage S/campaign 工作流。

## 验证边界

本轮按 3 次提交推进，每次提交前执行完整 `tests/unit`。真实复验覆盖本机 OMP `web_search` 与 macmini Hermes 原生 `web_search` → WRR backend；具体结果见本轮交付回执。

未执行 T7.3 评估集。当前 checkout 尚未提供设计中的 remote peers / `wrr serve`、Pi extension 或 Claude Code MCP 入口。OMP 原生 fetch handoff 的完整安全验收仍未完成。这些事项不由本次删除工作推定通过。
