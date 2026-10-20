# Jev / TypeSafe：WRR 路由与验证能力、成本及边界

> 决策建议：将 Jev 放在“有界语义判断”层，不让它拥有来源发现、权限、安全、预算或执行权；常规路径合并为检索前、检索后两次判断。[推断，依据：官方原子问题原则、WRR 当前合同、OMP judge 合同][O1][W1][J1]

- 调研日期：2026-10-19；WRR 对照版本：v6.4.0。[W2]
- 本轮只读文档、源码和既有记录，没有运行测试、安装包或发起新的 Jev 质量/延迟试验。
- “官方能力”来自本轮实际读取的 TypeSafe 文档；“实测”均注明是仓库既有记录，不是本轮复测。
- `[推断]` 是 WRR 的设计/估算建议；`[未核验]` 表示未直接取得对应原始材料。
- 私有 pi-jevflow 通过本机已有 GitHub CLI 认证读取固定提交；公开 URL 的 404 不代表仓库不存在。[P1]

## 1. 官方能力表：已经找到公开文档

本轮找到了 `docs.typesafe.ai` 的 API、models、images、confidence、已知缺陷及 legal 文档，不需要用第三方营销材料推定协议。[O1][O2][O3][O4][O5][O6]

### 1.1 端点、模型、输入与问题

| 字段/能力 | 官方公开合同 | 对 WRR 的含义 |
|---|---|---|
| HTTP 端点 | `POST https://api.typesafe.ai/v1/systemone`，Bearer API key，JSON body | 不是 chat completions；adapter 需要显式转换。[O2] |
| 顶层字段 | `model`、`state`、`questions` 必填；响应含 `model`、`answers`、`usage` | 保留实际模型版本与 usage，不只记请求 alias。[O2] |
| `state` | string、JSON object、JSON array | 可传 query、来源描述符、摘要，不需要整段 agent transcript。[O2] |
| 稳定模型 | `jev-1.13.0`；本轮读取时 `jev-latest` 指向它 | 回归阈值建议绑定版本；alias 会迁移。[O3] |
| 预览模型 | `jev-1.14.0` 于 2026-10-14 发布；`jev-preview` 指向它 | “最新发布日期”不等于稳定 alias 已切换。[O3] |
| `noul` | yes/no 概率，返回 `noul: 0..1`，不是布尔值 | 直连 wire type 应写 `noul`，不是 OMP 的 `bool`。[O2][J1] |
| `choice` | 选项 → rubric 的 map；至多 255 项；返回选项、全分布、confidence | 单次只选一个，不是多选集合；保留 `none/unknown`。[O2][O7] |
| `score` | 2–10 个有序 rubric levels；返回概率加权位置、分布、legend、confidence | 0–3 score 不是概率，不能直接套 `noul` 的 0.5 阈值。[O2] |
| `instructions` | string/object/array；criteria 也可结构化 | 完整问题必须写在 instructions，问题 ID 不参与推理。[O2][O7] |
| `bool` | 官方 HTTP primitives 使用 `noul`，未列 `bool` wire type | `bool` 是 OMP 抽象名；跨宿主适配必须归一化。[O2][J1] |
| 自由文本生成 | 不生成解释、query、URL 或报告 | 下一步 query 必须由宿主先提出，再让 Jev 选择。[O1][O5][H2] |
| 图片 | 1.13/1.14 有实验性图片接口；Box 需要 1.14 | 不应写成“完全不支持图片”，也不应写成成熟生产能力。[O4] |
| 音频/视频 | models 文档不接受，先转换为文本/结构化字段 | WRR 不应把媒体原文件直接交给文本判官。[O3] |

### 1.2 大小、批量、价格、限流及延迟

| 字段/上限 | 已核验的公开信息 | 未知/边界 |
|---|---|---|
| 默认 request context | `state + 全部 questions`：64k tokens | 官方 models 页以 64k 表述；不能用字符数代替 token 数。[O3] |
| 默认 question context | `state + 任一 question`：32k tokens | state 即使小于 request 上限，也可能使单问题超限。[O3] |
| 长上下文 | header `TypeSafe-Danger-Zone-Long-Context: true` 将两项上限提高到 262k | 默认关闭；官方称更慢、准确性更低，不建议常规 WRR 请求启用。[O3] |
| HTTP body | 32 MiB；UTF-8 无 BOM；state/instructions/criteria 各至多 64 层嵌套 | 不是可使用 32 MiB 文本上下文；token 限制仍独立存在。[O2] |
| 数值 | 只接受有限数；NaN/Infinity/过大数值拒绝 | ID、超大精确数字应传字符串。[O2] |
| 单 state 多问题 | 同一 state 只摄入一次；所有问题独立并行评估 | 独立意味着问题 B 看不到问题 A 的答案。[O1][O3][O8] |
| 问题数量 | 没有独立数量上限，须同时满足两种 context 限制 | 新增问题仍增加输入 tokens，不是绝对免费。[O3] |
| 多 state 服务端 batch | 已读公开 API 只定义单 state + questions；未找到独立多 state batch endpoint | OMP `judge_batch` 是宿主编排，不能称 TypeSafe 原生单请求多 state。[O2][J1][J2] |
| 输入价格 | 1.13/1.14 均 `$0.042 / million input tokens`，即 `$42 / billion` | 是官网标价，不是本账户结算凭证。[O3] |
| 输出价格 | 免费；usage 仍报告 output_tokens | 无需因 output token 数直接加价，但不能忽略输入问题成本。[O2][O3] |
| 账单字段 | TypeSafe 直连公开响应列 usage.input_tokens/output_tokens | 未找到直连响应美元账单字段；WRR 既有实测也未返回价格。[O2][W3] |
| 限流 | 按 organization 的 tokens/sec、requests/sec；具体值在 console | 未找到适用于所有账户的固定 RPS/TPS 数字。[O3] |
| 超限/过载 | `429` / `529`；SDK 默认退避重试，支持 retry-after | 热路径必须另设重试/总时限合同，不能继承 SDK 默认后宣称一次请求。[O2][O3] |
| 官方延迟 SLA | 已读公开能力文档强调快速和并行 | 本轮未找到公开 p50/p95/p99 或强制 SLA；约 500ms 来自 WRR 样本。[O1][W3] |

### 1.3 图片不能由 models 表的 “Text” 一列否定

models 表列的是默认文本使用；images 专页明确 Jev 1.13 与 1.14 都可实验性接收图片，需 `TypeSafe-Danger-Zone-Images: true`。[O3][O4]

- 图片在 `assets` map 中上传，state/instructions/criteria 通过 reference 引用；每个 asset 必须被引用。[O4]
- PNG/JPEG 静态图；每边 280–1568 px，总像素 78,400–2,458,624；单图文件至多 16 MiB。[O4]
- 请求体、全部处理后图片、引用展开后的请求分别有 32 MiB 限制；重复引用会重复计入展开内容。[O4]
- 图片按输入 tokens 同价计费；官方尚未给图片 token 估算指南；无单独固定张数上限。[O4]
- “Dreamer-ready” 明确不是 production-ready，格式、限额与准确性都可能变化；WRR 默认路由不应依赖它。[O4][推断]

### 1.4 数据处理和隐私

| 问题 | 官方文字所支持的结论 | 不可推导的结论 |
|---|---|---|
| 是否训练 | models 说不使用客户 request/response 训练；Privacy Policy 说不训练或 fine-tune 用户 Input | 不代表输入不被收集、缓存或由服务供应商处理。[O3][O6][O9] |
| 默认保留期限 | Privacy Policy 为合理必要期间；DPA 为处理目的/法律所需期间 | 未找到所有账户统一 0 天/7 天/30 天固定保留承诺。[O9][O10] |
| ZDR | Legal 页说 enterprise 客户可联系销售取得 ZDR | 不能假设普通 API key 自动 ZDR。[O6] |
| 处理地点 | Privacy Policy 明确服务在美国托管 | 不应承诺本地处理、欧盟驻留或不会跨境。[O9] |
| 第三方处理 | Input 可向 service providers 披露；DPA 引用 subprocessors 清单 | 不代表“任何第三方都不会见到输入”。[O9][O10] |

[推断] WRR 的“本机有什么就调用什么”只描述能力发现，不构成外发授权；本地笔记、登录态结果、private repo 内容应先受本地 policy gate 约束，不能让远端 Jev 决定自己能否接收它们。[O9][O10][W1]

## 2. 三个参考实现：调用模式对照

### 2.1 核验与版本边界

- WRR：读取工作区 `wrr/search_filter.py` 和 v6.3/v6.4 release notes；以工作区路径引用，不伪造 commit。[W1][W2][W3]
- Hermes：读取固定提交 `b8f69fe68b0efaa006f9baede46c3849aaf789f6` 的 client/search/rerank；不是运行时安装版本声明。[H1][H2][H3]
- 本机按要求执行 `find /Users/alexcai -maxdepth 6 -type d -name jevkit`，受到 CloudStorage 超时/失效挂载影响，未得可用路径；这不能证明本机没有 Jev。[本轮观察]
- 已改用指定 Hermes 上游固定提交源码；仓库既有 review 也说明安装快照不含 `.git`，文件匹配不等于整仓 HEAD。[W4]
- pi-jevflow：通过认证读取私有仓库固定提交 `be91bb962899dac5bc92fcad99c3b350dcb6cccf` 三个指定 lib；不把 Obsidian 笔记当源码替代。[P1][P2][P3]

| 维度 | WRR v6.4 当前 | Hermes jevkit | pi-jevflow 固定提交 |
|---|---|---|---|
| 决策位置 | 检索/RRF 后筛候选 | 候选 rerank + 充分性 + next query | 生成前选物理模型，再选 thinking。[W1][H2][P1] |
| 接入方式 | Python httpx 原生异步直连固定 TypeSafe | 同步 client + 池化连接；支持多个 Jev gateway | Pi registry `findOfType` / `classify`，不自行复制 transport。[H1][P1] |
| 问题类型 | 每候选 rel/inj 两个 `noul` | rel/inj/answerable `noul`；enough `noul`；next_query `choice` | modelChoice 和 thinking 两个 `choice`。[W1][H2][H3][P1] |
| state 内容 | query、today、P0..Pn 标题/URL/snippet | 匿名候选、query、日期；第二轮加 tried queries/已选摘要 | 最新用户 prompt、最多两条近期用户请求、hasImage；catalog 白名单 metadata 放 criteria。[W1][H2][P1] |
| 批量 | 至多一个 request，最多20候选/40问题 | 每批至多60候选、最多8批线程并行；再一次 search gate 判断 | 模型与 thinking 可能两次串行；continuation/retry 可复用 turn snapshot。[W1][H3][P1] |
| 尺寸控制 | query1500字符、片段900字符、转义 state≤60000字符 | state≤60000字符；片段900，按 JSON 编码预算分批 | classifier context≤24000 UTF-8 bytes；metadata≤16000 bytes。[W1][H1][H3][P1] |
| 超时/重试 | 默认2秒/最大3秒且受剩余deadline；无重试/重定向 | ask 默认4秒总预算、retries=1；搜索 gate 默认6秒 | 整池决策8秒 timer，abort + race；criteria研究是另一预算域。[W2][H1][H2][P1][P3] |
| 判定失败 | 原子保留原结果，标记 fail_open，不宣称通过 | 退到 local-only/unjudged/unknown，允许 partial | 当前合格模型优先，再配置 fallback，再池内首项；记录状态。[W1][H2][H3][P1] |
| 最终执行权 | 原 SearchResult 不改写，不补搜 | 只选现有 ID/query；agent 负责实际检索和写作 | 宿主调用物理模型；候选池/认证/容量约束在代码。[W2][H2][P1][P2] |

### 2.2 WRR 当前合同值得保留的部分

相关性与注入分别询问：`rel >= 0.5`、`inj < 0.5` 才保留；相关性降序，同分保留输入顺序，返回原始 `SearchResult` 对象。[W1]

- query 和所有完整候选先做启发式隐私检查；包括不会上传的 highlights、溢出候选及截断隐藏部分，任一命中则跳过整批。[W1]
- 最多20个候选；未评估 overflow 保留并显式记录，不能因“返回了结果”就当作通过判官。[W1]
- 严格验证答案 key 集合、`type=noul`、数值有限且落在0..1；只允许有限 model/usage metadata 输出。[W1]
- timeout、网络、认证、HTTP、畸形回复全原子 fail-open；有效判定全部拒绝则返回空列表，与“判官不可用”区分。[W1][W2]
- Jev 不是 provider，不增加独立来源票数；相关不等于真实、时效合适或证据充分。[W2]

对 WRR 重构的含义：[推断，依据 W1/W2]

1. 复用“结果身份不变 + 显式未评估 + 失败状态”合同，而不是让判官重新生成来源对象。[W1][W2]
2. 将可替换 judgment transport 与固定安全/预算 policy 分开，保留现有隐私前置门。[W1][J1]
3. 不照搬当前“所有故障均原子原样返回”作为全层通用策略：路由失败要选确定性来源，充分性失败必须 unknown，安全授权失败应拒绝外发。[W1][H2][推断]

### 2.3 Hermes：一轮搜索并不只需一次判官

`rerank.py` 在一批中放 answerable，以及每候选独立的 relevance/injection 问题；大列表最多8批并行。`search.py` 随后基于已选 passages 发第二次请求：enough，以及宿主已提出的最多5条 candidate query 中选下一条。[H2][H3]

- next_query 包含 `none`，避免闭集强迫选择无增益查询；Jev 不生成新 query。[H2]
- `gate` 默认 top_k=6、max_rounds=3；轮数和 reading_failed 由代码控制，非 Jev 自主循环。[H2]
- 第二次判断没成功不冒充 evidence enough；状态可为 partial、unknown 或 evidence_thin。[H2]
- client 校验 choice 分布与 argmax、score期望等一致性，支持实际版本/usage/latency metadata。[H1]
- client transport 内还有断开连接重建路径，加上 ask 的 retries；“一个逻辑 ask”不等于一个物理 POST。[H1]

`latency_ms` 需谨慎：rerank 用并行 replies 最大值；search `_add_usage` 对两阶段 latency 又取 max，而 usage 相加。因此这个字段不应被直接当作整个串行 search gate 的墙钟耗时。[H2][H3]

client 注释记录同题连接方式测量：fresh urllib 522ms、复用 http.client 245ms、httpx 189ms；这是上游作者记录，本轮未独立复测，也不是 TypeSafe SLA。[H1]

对 WRR 重构的含义：[推断，依据 H1/H2/H3]

1. 借鉴“候选筛选”和“集合充分性”分离，以及 `none/unknown/partial` 的显式决策状态。[H2]
2. 单 state 多问题减少重复传 state；但如果充分性必须针对“刚筛出的集合”，就是真依赖，不能假称一次调用能互读答案。[O1][H2]
3. 不照搬480候选线程扇出、默认重试和3轮搜索；WRR 热路径应给每层独立请求上限，并以端到端墙钟记录成本。[H1][H2][H3]

### 2.4 pi-jevflow：Jev 推荐，代码约束

`routePool` 先用 registry 校验池：物理 chat、已有认证、非 `pi-virtual`、输入模态和 thinking 支持；再用 `findOfType("classifier", "typesafe", "jev-latest")` 取得判官。[P1]

模型选择 criteria 来自 catalog 白名单和既有 routing hint；不序列化可能带凭据/headers/URL 的完整 model 对象。instructions 明确 catalog 是事实而非实测质量，不能从名称或价格臆测质量排名。[P1]

- 最新 prompt最多12000字符，前两条用户请求各最多2000；图片仅变为 hasImage 标记，生成候选受 image能力过滤，不等于把图片发给 classifier。[P1]
- 多候选时先选 model；若 thinking=auto 且该模型有多个 level，再选 effort。第二问使用最终模型可用 levels，因此串行是有实际依赖。[P1]
- winner 严格检查 stopReason、唯一问题、选项属于集合、有限confidence、完整概率分布及argmax，畸形输出不参与执行。[P1]
- direct 路径跳过 classifier；continuation/retry 复用本轮 pool snapshot，不因设置变更中途换池。[P1]
- dispatch前重新确认所选模型可用；不能把 availability fallback 伪装成 classifier vote。[P1]

亲和纯策略的参数是应用策略，不是 Jev 官方能力：[P2]

| 机制 | 源码规则 | WRR 可借鉴与不可照搬 |
|---|---|---|
| provider affinity | context usage≥25%时，新自动候选限制在当前provider | 借鉴执行状态约束；搜索无相同 KV/cache 语境，不照搬25%。[P2][推断] |
| headroom | 新目标要求已用tokens≤目标context×0.65；保留当前identity供压缩/恢复 | 借鉴先过滤硬约束；搜索容量用deadline、响应大小、认证范围表达。[P2][推断] |
| confidence gate | 非有限或confidence<0.60时保持当前模型 | 借鉴低置信度不乱切；0.60不能直接当WRR经验最优值。[P2][推断] |
| hysteresis | 同provider降级/未知方向须连续两轮相同建议；已知强度升级可立即 | 搜索仅在同任务有状态连续轮次考虑迟滞，单次query不必引入历史状态。[P2][推断] |
| unknown usage | 沿用既有affinity mode，不因短暂缺值解锁 | 借鉴 unknown不扩权，尤其登录态与来源权限。[P2][推断] |

`jev-router-criteria.ts` 是能力资料研究引擎，不是 Jev 自行发现自己能调用哪些模型：[P3]

- 受限抓取 + 单并发生成模型归纳；`complete(..., maxRetries:0)`，不是 classifier 的廉价 typed-judgment 调用。[P3]
- 默认16页/16次归纳、总输入131072 bytes、总输出19200 tokens；是资源预算，不是美元 hard cap。[P3]
- edited/verified 资料默认保护；opt-in刷新也只写 candidates，不直接覆盖已认可条目。[P3]
- 源引用与精确identity存在校验，但源码明确 provenance不证明entailment，产物仍是draft。[P3]

对 WRR 重构的含义：[推断，依据 P1/P2/P3]

1. 借鉴“硬过滤 → 判官推荐 → 确定性约束 → dispatch再确认”，Jev不应发明本机capability。[P1][P2]
2. 将来源能力资料与路由质量观测分开；官方宣称、实测、人工编辑应带状态，不自动合并成“可信最优”。[P3]
3. 不照搬模型强度排序、两轮降级或8秒判官预算；这是有状态生成模型路由，不是通用搜索策略。[P1][P2]

## 3. OMP 原生 judge：抽象合同与 fallback 的真实边界

### 3.1 schema 及后端

`xd://eval/judge` 与 `omp://tools/eval.md` 已直接读取。`judge(state, questions)` 返回 `{id: answer}`；每题见同一个state，独立评估。[J1][J2]

| OMP问题 | 请求schema | 返回 | TypeSafe适配注意 |
|---|---|---|---|
| choice | `type:"choice"`、instructions、`criteria:{label:rubric|null}`，至少2项 | choice、probabilities、confidence | 保留闭集标签，不把label当解释。[J1] |
| bool | `type:"bool"`、instructions，可选true/false criteria | `bool: P(yes)` | 直连对应noul，不是JSON boolean。[J1][O2] |
| score | `type:"score"`、instructions、从低到高的criteria数组，至少2级 | score、probabilities、confidence | OMP抽象与TypeSafe传输上限需同时满足。[J1][O2] |

`modelRoles.judge` 决定使用哪个判官；不是所有 OMP judge 调用都固定走 TypeSafe。`typesafe` API kind 才把 System One body 发到 `{baseUrl}/v1/systemone`，也支持其他System One兼容server。[J2][J3]

`modelRoles.web` / `retry.fallbackChains.web` 是搜索执行角色，`modelRoles.judge` / `retry.fallbackChains.judge` 是判断角色；不应由 Python WRR 硬编码一套与宿主冲突的凭据/provider优先序。[J3][J4][推断]

### 3.2 tiny/smol 不是无条件故障回退

helper摘要说“有凭据用TypeSafe，否则tiny/smol”，但更细的 models 文档有 native-only tail policy：[J1][J3]

1. judge role接受native judge、tiny及chat，fallback可以显式配置 `@tiny/@smol/@default`；空数组表示没有fallback。[J4]
2. 模型选择/能力过滤后，若没有兼容native，合格prompted chat以及兼容session fallback可以回答；图片请求跳过不兼容模型，不会静默丢图。[J3]
3. 一旦走native judge，运行失败后只fall through其他兼容native，不会落到prompted chat。[J3]
4. 因而“TypeSafe服务超时 → 必然tiny/smol续判”不是已核验合同；必须区分无可用native与已选native执行失败。[J3][推断]

[推断] WRR的host adapter应透传宿主判官失败，并执行自己的确定性fallback；不能为了“自动验证总成功”在Python侧偷偷补一个昂贵chat调用。[J3][W1]

### 3.3 judge_batch 的并发与成本显示

- 多个states用 `judge_batch`，不是循环 `judge`；同一题集应用于每个state，由宿主持有，跨cell存活。[J1][J2]
- 默认 `concurrency=32`、`retries=1`、`min_ok=1`；completion与judgment共享进程级32请求semaphore。[J1][J2]
- `drain`取得逐项answers/error；`status()`含done/total/failed/cost/running/model；item失败保留，整体失败条件另处理。[J1]
- `cost`是宿主暴露的成本统计，不是TypeSafe直连账单；本轮没有运行batch，未核验本账户显示值、计价覆盖范围或provider账单对账。[J1][未核验]
- 图片可传local path或ImageContent；缺兼容判官会明确失败，不会当作纯文本成功。[J1][J3]

对 WRR 重构的含义：[推断，依据 J1/J2/J3/J4]

1. OMP内优先复用typed judge和宿主role，让Python核心只依赖稳定judgment接口而非OMP内部eval实现。[J1][J4]
2. 一次搜索的多个候选优先同state多问题；离线评估/多query才考虑judge_batch，避免每候选重复query/state。[J1][O3]
3. 不照搬默认32并发/一次重试作为WRR热路径预算；进程semaphore也不是组织RPS/TPS保证。[J1][J2][O3]

## 4. 已有实测、usage 与可估成本

### 4.1 WRR记录不是质量benchmark

| 既有样例 | 记录结果 | 墙钟 | input/output | 按当前官网价推算输入成本 |
|---|---|---:|---:|---:|
| v6.3临床试验选源 | academic，confidence1.0 | 572ms | 357/38 | $0.000014994 |
| v6.3官方说明vs博客 | 官方来源，覆盖度0.9 | 490ms | 461/60 | $0.000019362 |
| v6.3仅轶事充分性 | 充分度0.12 | 568ms | 328/21 | $0.000013776 |
| v6.4受控3候选筛选 | 3→1，jev-1.13.0 | 494.9ms | 759/112 | $0.000031878 |
| v6.4 Exa→Jev | 5→2 | 整次3454.14ms | 1766/184 | $0.000074172 |
| v6.4 Exa+DeepSeek→RRF→Jev | 5→3 | 整次5121.74ms | 1632/184 | $0.000068544 |

来源：延迟与usage为仓库既有记录[W2][W3]；美元列是 `[推断] input_tokens × 0.042 / 1,000,000`，不是历史账单，也不包含搜索provider、网关或生成模型费用。[O3]

不能把后三行“整次搜索耗时”称为纯Jev耗时；也不能用少量合成样例推断准确率、p95、端到端省钱或召回提升。[W2][W3]

### 4.2 本机Obsidian已有结论

`JevFlow质量评估与成本边界.md` 为candidate/种子笔记，reviewed=false；应保留其证据等级，而非当正式质量结论。[N1]

- 笔记记录12个合成case的criteria A/B：11/12模型选择变化、9/12thinking变化、48次显式分类；只证明人工摘要影响选择，不证明任务完成质量提升。[N1]
- 笔记说quality runner只有可注入模拟链路，没有真实transport；simulationOnly是可信约定，不是沙箱。[N1]
- 请求上限不是账单hard cap；provider/SDK内部重试和中止后服务端处理都可能产生额外成本。[N1]
- confidence不是任务成功率、生成质量或费用保证；catalog价格0/缺失也不等于免费。[N1]
- `[未核验]` 上述pilot、ZH03及费用审核的Windows原始报告未在本轮取得；已核验的是本机笔记确实如此记录，不能升级为本轮重现实验。[N1]

## 5. 路由问题类型 × 成本 / 延迟 / 兜底

### 5.1 估算口径

以下全部是 `[推断]` 设计预算，不是新实测：普通文本、5–20候选、紧凑descriptor；state为规划token范围，billable input还含questions/criteria，实际以usage为准。[O2][O3][W1]

一层先按约500ms规划参考值，来自本机既有490–572ms样本；单层建议软预算750–1000ms、共享整次deadline，不声称能覆盖尾延迟。[W3][推断]

价格只采用直连官方 `$0.042/Mtok`；1k/5k/10k/20k输入tokens分别约 `$0.000042/$0.00021/$0.00042/$0.00084`，不是搜索全成本。[O3][推断]

| 类别 | 建议问题与state | state / 总输入token规划 | 一次搜索调用数 | 增量延迟参考 | 输入成本规划 | 失败时确定性兜底 |
|---|---|---|---|---|---|---|
| (a) 意图/模式 | 一个choice；query+显式site/DOI/任务约束；含unknown | 0.5–1.5k / 1–2k | 0或1；与(b)合并 | 合并后一层约500ms | $0.000042–0.000084 | 显式site/DOI优先；否则默认通用web策略，不猜private访问。 |
| (b) 来源适配 | 每个available source一个bool，或best primary choice；state含query+能力短描述 | 1–4k / 2–6k | 与(a)同一次；不要每来源一次 | 不再串行加一层 | $0.000084–0.000252 | 仅从已发现且可用的允许集合，按固定优先级/预算取源。 |
| (c) 相关性/注入 | 每候选两个bool；需分级效用时另用score；state含query+摘要 | 3–10k / 5–15k | 通常1 | 约500ms；沿用显式deadline | $0.00021–0.00063 | 保留原排序并标unjudged/degraded；本地安全硬门不能fail-open。 |
| (d) 集合充分性/补搜 | enough bool + next_action choice；附证据集、已搜范围、宿主候选actions | 2–8k / 3–10k | 0或1；可与(c)对“原始集合”同问 | 合并零额外串行；依赖筛选后集合则再约500ms | $0.000126–0.00042 | unknown≠enough；代码按剩余budget决定固定一次补搜或返回insufficient。 |
| (e) 来源质量比较 | 同题实际结果的A/B/tie/insufficient choice，或每source效用score | 2–8k / 3–12k | 默认0；可并入后验同state问题 | 合并通常不增层；独立再约500ms | $0.000126–0.000504 | 不自动永久改全局权重；保留稳定来源/结果排序。 |

表中调用数/延迟是逻辑设计，不含provider内部重试；成本范围不能简单按每行相加，因为(a)+(b)、(c)+(d)/(e)可共享一次state。[O3][O8][推断]

### 5.2 每类问题的适用边界

(a) 模式分类：[推断] 不必保留8 modes作为不可变产品概念；让choice表达用户所需证据形态，而不是让Jev猜旧关键词分支。精确DOI、显式来源、deadline仍由解析器处理。[W3][O5]

(b) 来源适配：[推断] 多源允许同时适合时，bool-per-source比单choice更自然；单choice概率之和为1，不能把各概率当独立可用性概率。硬资格必须先由本地过滤。[O2][P1]

(c) 候选筛选：[推断] relevance、answer utility、injection是不同维度；高相关不能覆盖高注入风险，也不能证明事实正确。按固定索引回收原始候选，不让模型生成URL。[W1][W2][O5]

(d) 集合充分性：[推断] 同一请求不能问“基于本次rel筛选最终留下的结果是否足够”，因为问题互相独立。可问原始有界集合的充分性，或付出真实第二次串行判断。[O1][O8][H2]

(e) 质量比较：[推断] 需要实际返回材料与同样的query/截止时间/格式预算；只给“Exa擅长语义”之类描述，得到的是descriptor适配，不是来源质量benchmark。[P1][P3][N1]

### 5.3 建议的默认调用拓扑

1. **确定性前置门，0次Jev**：发现source、认证/只读权限、query显式约束、隐私分类、deadline和请求预算。[P1][W1][推断]
2. **检索前1次**：意图choice + 各来源适配bool，同时询问；代码组合成执行计划，不能在题中引用另一题输出。[O1][O8][推断]
3. **检索后1次**：候选rel/inj + 原始结果集enough + 有限next_action；代码负责冲突处理、阈值、来源身份和预算。[O1][W1][H2][推断]
4. **仅必要时额外1次**：若必须评价筛选后的集合，或补搜后再评价；轮数上限预先固定。[H2][推断]
5. **故障或隐私不允许时0次/截断后续**：返回明确fallback/unknown，不另开无限judge/chat链。[W1][J3][推断]

常规路径约2次、参考增量约1秒；一次额外依赖判断约1.5秒。若总输入2k+10k，估算Jev输入成本约 `$0.000504/搜索`，1000次约 `$0.504`；这些不是吞吐/尾延迟/费用保证。[O3][W3][推断]

[推断] 单次判断仍有网络固定成本；比起继续降低极小美元成本，更应优先控制层数、外发内容和错误补搜所引发的搜索/生成费用。[H1][W3][N1]

## 6. Jev适合做 / 不适合做

### 6.1 适合：有输入证据、闭集答案、可逆行动

1. query的意图/证据形态分类，以及有界可用来源的语义适配判断。[O7][P1]
2. 已检索片段的回答效用、相关性与注入风险分开判断，结果留有概率与未评估标记。[W1][H3]
3. 已给定证据集是否包含回答必需内容，低置信时返回unknown，不自动变成完整事实结论。[H2][O5]
4. 从宿主预先提供的候选query/action中选择，包含none、stop、insufficient。[H2][O7]
5. 可复现样本上的pairwise比较/有序rubric，辅助路由评估而非替代真实任务评分。[O2][N1][推断]

### 6.2 不适合：必须精确、必须本地或需要生成

1. 发现本机安装/登录态、认证可用性、网络权限、来源可执行命令；这些要真实探测与代码约束。[P1][W1][推断]
2. 金额、计数、日期比较、deadline、配额运算；官方承认数学/数字/日期不足，留在代码。[O5]
3. 生成新query、URL、引用或报告；Jev不生成文本，强行串choice模拟生成会慢且质量差。[O5][H2]
4. 作为完整安全边界、证明内容真实、保证检索穷尽或替代领域专家的多步论证。[O5][W2][推断]
5. 未授权敏感数据的“先发出去再判断能否外发”，或默认离线可用；直连是远端服务，普通账户不自动ZDR。[O2][O6][O9]

### 6.3 五类主要风险及约束

| 风险 | 已有证据 | WRR应保留的约束 |
|---|---|---|
| 延迟叠加 | WRR约500ms样本；Hermes两阶段；Pi最多两次串行 | 合并独立问题；共享deadline；分别记录Jev与端到端墙钟。[W3][H2][P1][推断] |
| 离线/故障 | 远端HTTP、401/429/529、无native时/失败时不同fallback | 明确确定性fallback；缓存结果不得伪装当次live验证。[O2][J3][推断] |
| 判官偏差 | 英语最佳；长杂乱state准确性下降；choice顺序偏差 | 中文独立样本、短state、离线顺序扰动、版本固定；不把confidence当成功率。[O3][O5][N1] |
| 注入影响判官 | 官方说state默认不当敌对；Hermes注释记录exfil注入曾低于0.5 | 本地权限/外发硬门；分开风险题；fail-open结果不贴安全背书。[O5][H3][W2] |
| 隐私/账单 | 美国托管；非固定保留；SDK重试；费用估算≠账单 | 最小公开state、显式opt-in、禁泄露凭据、请求/预算账本与实际usage分离。[O9][O10][H1][N1][推断] |

## 7. 关键发现 Top 5

1. **官方资料足够定义真实合同**：`noul/choice/score`、64k/32k默认context、输入 `$0.042/Mtok`、输出免费；`bool`属于OMP抽象，不是TypeSafe原始字段。[O2][O3][J1]
2. **“便宜”不等于可以无限加层**：WRR既有样本约500ms；同state多问题才是主要杠杆，真依赖仍需第二次调用。[W3][O1][O8]
3. **三个实现共同显示“Jev建议，代码掌权”**：WRR保留原结果身份，Hermes只选已有query，Pi先过滤可执行候选再套亲和/容量约束。[W1][H2][P1][P2]
4. **OMP已有judge合同应复用，但fallback需精确理解**：selected native失败不能假设必落tiny/smol；batch默认并发/重试也不是WRR预算。[J1][J2][J3]
5. **没有证据支持默认接管所有质量/安全决策**：中文、choice顺序、敌对state和大context有官方限制；笔记pilot证明选择变化而非任务质量提升，普通账户也不自动ZDR。[O3][O5][O6][N1]

## 8. 仍需确认的问题

1. 本账户实际RPS/TPS、是否ZDR、retention合同及美元hard cap是什么？官网公开资料不能替代console/合同；本轮不读取凭据或发起账户变更。[O3][O6][O10]
2. 中文/英文真实WRRquery在固定模型版本、固定候选、失败计入分母时，路由质量、误杀率、补搜收益和p95如何？当前少量样本不足回答。[W2][W3][O3][N1]
3. OMP扩展通过哪个受支持公开API将judgment交给Python核心？已确认eval用户面合同，未核验“外部Python进程直接调用宿主judge”的生产桥接协议。[J1][J2][未核验]
4. 检索后默认充分性应针对原始集合，还是必须针对筛选后集合？这决定常规2次还是3次判官；需要以实际召回/误判收益决定，不以“所有事一次完成”假设决定。[O1][H2][推断]
5. 现有0.5阈值、拟议choice confidence门、model alias升级及图片实验能力应如何版本化？需要独立回归样本与明确回退，不直接搬Pi的0.60或官网示例阈值。[W1][P2][O3][O4][O5]

## 来源索引

### TypeSafe一手公开文档

[O1]: https://docs.typesafe.ai/introduction.md
[O2]: https://docs.typesafe.ai/api
[O3]: https://docs.typesafe.ai/models
[O4]: https://docs.typesafe.ai/images
[O5]: https://docs.typesafe.ai/model-jaggedness/jev-1.13.md
[O6]: https://docs.typesafe.ai/legal
[O7]: https://docs.typesafe.ai/primitives.md
[O8]: https://docs.typesafe.ai/patterns/fan-out.md
[O9]: https://typesafe.ai/legal/privacy-policy
[O10]: https://typesafe.ai/legal/data-processing

补充：[Confidence：概率与confidence的区别、公式](https://docs.typesafe.ai/confidence.md)。Noul没有独立confidence；choice confidence由选项数及最高概率计算，不能混同质量评分。[O2]

### WRR本机一手来源

[W1]: ../../wrr/search_filter.py
[W2]: ../../RELEASE_NOTES_v6.4.0.md
[W3]: ../upstream-review.md#jev-的实测与接入决定
[W4]: ../upstream-review.md#640-jev-搜索筛选上游与-gpt-researcher-对照

补充：[v6.3.0 Jev决定](../../RELEASE_NOTES_v6.3.0.md#jev-决定)。源码重点：常量L26–31、回复验证L174–209、隐私门L239–255、单批构造L262–304、fail-open与筛选L306–353。[W1]

### Hermes固定源码

[H1]: https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/jevkit/client.py
[H2]: https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/jevkit/search.py
[H3]: https://github.com/kerpopule/hermes-jev-skills/blob/b8f69fe68b0efaa006f9baede46c3849aaf789f6/jevkit/rerank.py

### pi-jevflow固定源码（私有，需仓库权限）

[P1]: https://github.com/Loveacup/pi-jevflow/blob/be91bb962899dac5bc92fcad99c3b350dcb6cccf/lib/jev-router-pool.ts
[P2]: https://github.com/Loveacup/pi-jevflow/blob/be91bb962899dac5bc92fcad99c3b350dcb6cccf/lib/jev-router-affinity.ts
[P3]: https://github.com/Loveacup/pi-jevflow/blob/be91bb962899dac5bc92fcad99c3b350dcb6cccf/lib/jev-router-criteria.ts

源码定位：pool L58–68候选硬过滤、L89–109最小state/metadata、L111–128答案验证、L174–180 fallback、L203–262 classifier与串行问题；affinity L1–5阈值、L61–72候选约束、L84–122迟滞；criteria L85–91预算、L174–176保护、L209–218归纳调用、L245–249 candidate产出。[P1][P2][P3]

### OMP本机官方文档与用户只读笔记

[J1]: xd://eval/judge
[J2]: omp://tools/eval.md
[J3]: omp://models.md
[J4]: omp://settings.md
[N1]: file:///Users/alexcai/Obsidian/AlexCai/01-Staging/JevFlow质量评估与成本边界.md

OMP定位：models L149–184 native judge、图片及native-only fallback；settings L374–442 role与fallback；eval L173–182 judge/batch与共享semaphore。[J2][J3][J4]

补充只读笔记：[项目总览](file:///Users/alexcai/Obsidian/AlexCai/01-Staging/JevFlow项目总览.md)、[架构与数据流](file:///Users/alexcai/Obsidian/AlexCai/01-Staging/JevFlow架构与数据流.md)、[亲和路由与恢复](file:///Users/alexcai/Obsidian/AlexCai/01-Staging/JevFlow亲和路由与恢复.md)、[资料初始化与证据](file:///Users/alexcai/Obsidian/AlexCai/01-Staging/JevFlow资料初始化与证据.md)。它们属于历史candidate整理，不作为新生产验收。
