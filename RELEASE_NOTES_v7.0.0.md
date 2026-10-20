# WRR 7.0.0

发布日期：2026-10-20。

WRR 7 清洁切换到 `wrr/core` 搜索内核。CLI、OMP 与 Hermes backend 共用来源发现、选源、执行、融合、质量判断与有限恢复。fetch/similar 本轮保持原有语义。

**评估状态：未跑评估集（T7.3）。** 本版本没有选源召回、筛选误杀、充分性 Brier 或 p95 延迟达标结论。单元测试与真实宿主搜索不能替代评估集。

## 变更

- `wrr search` 仅执行新内核；输出 `plan/runs/results/quality/diagnostics`。
- Jev 在有宿主 judge 或 WRR 授权环境中的 TypeSafe key 时默认启用。无 judge 时返回确定性默认计划，并标记 `plan.judge=none`。
- 常规路径最多 3 次 judge；恢复路径最多 4 次，至多补搜 1 轮。显式来源仍接受候选筛选与交付充分性判断，但不自动补搜。
- 候选选择改为 `uncertain_tiered_v1`：安全已评候选按高分（rel>=.65，降序）、不确定（.35<=rel<.65，保留 RRF 原序）、低分（rel<.35，降序）分层，同分稳定，top-k 前不再因 rel 删除。诊断保留空 `dropped_relevance_ids`，区分低置信保留与 top-k 截断，不宣称已验证相关；可能增加噪声。非公开／敏感／overflow 透传及注入硬拒、suspect 充分性排除不变；只有全安全拒绝才 `all_rejected`。全低分仍接受独立交付充分性判断和至多一轮补搜，不新增云调用；不改冻结协议、历史指标，不据此宣称验收达标。
- 修复 skill 恢复来源的真实就绪判定：GitHub code search 必需的 `GITHUB_TOKEN` 改为必需依赖及必需轻探针，缺 token 不再标为可路由；直接调用时缺 token／GitHub 认证失败明确保留为 `auth_error`，不伪装空结果。两条原始公开 query 已用真实 adapter／授权环境复验，证据见 `evals/runs/20261020T-skill-readiness-smoke/summary.json`；不据此宣称延迟或相关性验收通过。
- 隐私门在执行前生效：非 `local:` query 命中 `_sensitive` 时，在任何 judge/adapter 调用前终止，返回 `gate=sensitive_query`、`judge=skipped:sensitive_query`、`quality.executed=blocked/relevance=skipped`、空 `sources/runs/results`、零 judge calls/posts；显式来源和精确书目不可绕过，也不补搜。`local:` 保留本地搜索，即使显式请求 web 来源仍只走 local。原 query 保留于 Evidence 顶层，错误/诊断不复制内容；CLI/RPC 退出码 1，Hermes/OMP 标为错误。敏感检测为启发式；公开 prompt-injection 研究或引用攻击文本不是一律禁搜的理由。私密、本地、登录态及敏感候选仍不进入云端 state。
- 修复 URL 熵启发式将普通主机／路径组件跨 `/` 拼接后误判敏感：按组件检查，保留单段 token、query／fragment、凭据、解码及非公开 URL 防线；tune45 仅报告可判候选与理论上限，冻结证据缺 Jev 分数，不宣称反事实排序收益。
- **用户 2026-10-21 批准：仅公开候选改为形状式隐私筛选。** 独立 `candidate_sensitive` 替代候选路径的 `_sensitive`；查询门原样保持，命中仍 blocked、执行前零外发，local/authenticated 仍不进 Jev。公开候选中的概念词、arXiv/DOI/版本标识与普通裸编号不再按词汇或宽泛电话规则误拦；token、长随机凭据赋值、Authorization/Bearer 实值、私钥头、已知用户 key、真实联系形状、Luhn 卡号、本地路径、非公开/带凭据 URL 与 URL 单组件高熵继续拦。highlights 只筛不发。嵌入 URL 的既有 13 条诊断区分私网/内部 scheme 示例与提取边界误报，修正 Markdown/引用边界，不整体关闭 URL 防线；非 HTTP/未知来源仍保守不外发。这是隐私口径变更与误报修正，不是新质量验收；按用户后续简化要求不补 tune45 Jev 分、不作 capacity 反事实报告，也不触碰 validation。
- 形状规则红绿回归：公开概念/编号先观察到 7 个失败，修复后指定 `test_search_filter.py` 与 `test_core*.py` 共 **669 passed**。仅一次真实公开搜索（DeepSeek/academic 成功，Exa 超时），3 Jev posts、input/output tokens=10546/879；最终规则在该批 16 条完整原始候选上离线复筛，敏感数旧 1 → 新 0。真实调用为中间规则，新增小写 hex URL ID 拦截经红测撤回，不能将最终复筛冒充最终版本实跑；证据与限制见 `evals/runs/20261021T-public-screen-shape-smoke/summary.json`。
- 明确的正向 `site:hostname[/optional-path]` 新增独立确定性解析和 URL hostname 边界过滤，在融合前及恢复轮执行；不依赖 provider 或 rel 判断，阈值不变。路径仅保留为 provider query 提示，诊断披露 `site_path_hints` / `site_path_soft_hint:not_hard_enforced`，不保证精确路径或路径前缀，同域其他路径不被硬过滤。诊断披露排除条数，筛后零结果为 `site_filtered_empty`，不是模型 `all_rejected`。负 site／OR／分组／多个不同 site／完整 URL／userinfo／端口／非法编码明确报语法错误。普通平台偏好和 `official` 不映射硬域；019/020 原文的非标准 `site.hostname` 只提示争议，不自动当 `site:`。路径回归先红后绿；一次真实公开 Python 文档路径烟测见 `evals/runs/20261020T-site-path-development-smoke/summary.json`，仅为 posthoc 开发证据，不改冻结协议、gold、阈值或历史报告，不宣称新验收通过。
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
