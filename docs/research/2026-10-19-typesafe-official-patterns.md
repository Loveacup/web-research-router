# TypeSafe 官方 patterns、cookbooks、SDK 与 agent skill：WRR 设计补充研究

> 优先修订选源与充分性合同：多来源用 per-item Noul；Choice 保留给互斥分类。检索后的“原始候选够用”不能直接证明“实际交付集合够用”。Python 热路径保留 httpx，采用官方问题设计，暂不替换 transport。[推断，依据：多选函数参数、问题独立性、SDK 默认策略][C7][P5][S4]

- 日期：2026-10-19。对照设计稿：`docs/superpowers/specs/2026-10-19-wrr-core-design.md` 第 4.2、4.5、5 节。
- 范围：官方架构、指定 cookbooks、官方 SDK、agent skill。API 价格、上限、legal、jaggedness 沿用[已有研究](2026-10-19-jev-capability-and-cost.md)，不重做能力表。
- 官方事实均来自实际读取的 `docs.typesafe.ai` Markdown；`[推断]` 是 WRR 建议；`[未核验]` 是缺少直接证据的事项。
- Cookbook 数字是官方发布样本，不是本轮实测，不代表 WRR 质量或延迟承诺。[C1][C2][C3][C8][C9]
- 本轮未改设计稿、未安装包、未运行测试，也未调用付费 TypeSafe inference。

## 1. Top 5：直接进入设计修订

1. **保留 per-source Noul，明确多选语义。** 官方 `function_calling` 对 `list[Literal]` 使用每成员一个 Noul；单个 Choice 的概率用于竞争排名，不能当各来源独立适配率。[推断][C7][C5]
2. **增加不确定性处理，但不统一加 confidence 字段。** `evidence_shape`、`next_action` 读取 Choice confidence；Noul 用概率不确定区间，不存在官方返回的独立 confidence。[推断][P2][P6][C9]
3. **把 state、instructions、criteria 写成可审阅的 JSON 合同。** 保留来源和片段的命名字段；用反引号路径明确每题评估对象。复杂 rubric 使用 `what/not_for/examples`，短题仍可用字符串。[推断][P5][P7][P8]
4. **修正 enough 的证据范围。** 同请求的问题互相看不到答案；原始集合的 enough 只能描述原始集合。被注入门剔除、未评估、私密或截断的材料不能悄悄成为“交付集合已充分”的证明。[推断][P5][C2][C5]
5. **Python 保留受控 httpx transport。** 官方 SDK 默认每 HTTP operation 10 秒、最多重试 2 次、retry budget 30 秒；与设计稿单次软预算 1 秒不同。借鉴 typed question/response，而不继承默认重试或新增 httpx2 依赖。[推断][S4][S5][S3]

## 2. 官方 patterns：采用什么，保留什么边界

### 2.1 总原则与问题拆解

官方将 TypeSafe 定位为普通软件工作流中的判断组件。代码保留控制流、确定性规则和副作用；模型只处理有界语义判断。[P0][P5]

| 官方页面 | 已读到的具体做法 | 对 WRR 的结论 |
|---|---|---|
| Patterns 总页 | atomic decisions 组合出系统行为；列出 fan-out、confidence routing、composite scoring、intent routing | `[推断]` plan/execute/judge/recover 是代码工作流，符合方向；不能让 Jev 自主决定无限补搜。[P0] |
| Intent routing | 同一请求：1 个 intent Choice + 1 个 complexity Score；代码分支到数据库、专门 LLM 或人工 | `[推断]` evidence_shape 可用 Choice；来源集合不能因此变成单 handler 选择。[P1] |
| Confidence routing | answer 决定做什么，confidence 决定是否执行；风险不同，门不同 | `[推断]` 分类不清时走默认计划；补搜动作不清时不凭 argmax 消耗剩余预算。[P2] |
| Speculative fan-out | 同 state 的分支问题一次提出；代码忽略未使用分支答案 | `[推断]` evidence_shape、source_fit、freshness 可并问，但 source_fit 不能隐式读取 evidence_shape 答案。[P3] |
| Composite scoring | 分维度独立评分，归一化后由代码加权；保留各原始维度 | `[推断]` 质量可以分维度记录；不能用相关性高分补偿注入或隐私违规。[P4][P5] |

官方认为“拆问题”是设计重点：每题明确、狭窄、具体，避免宽泛问题把多个判断压成一个答案。[P5]

- `source_fit` 宜问“此来源的已声明能力是否适合此 query”，而不问“是否执行此来源”。执行还受授权、预算和数量上限约束。[推断][P5]
- `rel` 宜明确“主题涉及”还是“含可用于直接回答的事实”。官方 RAG cookbook 把这两个信号拆开，避免混用阈值。[推断][C2]
- `enough` 宜写清“哪些证据、回答哪类需求、只依据哪些文本”。不能依赖模型权重补出缺失事实。[推断][P5][P7]
- `next_action` 是一个有界行动分类，可以保留；行动的合法性、触发条件和实际执行仍由代码决定。[推断][P5][P1]

### 2.2 State：命名 JSON、必要上下文、明确关系

官方推荐大多数请求使用 object。字段名称能说明材料和关系；简单单文本场景可使用 string。每个 request 只有一个共享 state。[P7]

官方没有 WRR 专用的固定 state schema。以下结构是依据官方规则整理的设计建议，不是 SDK 必填字段。[推断][P7][P5]

```json
{
  "request": {
    "query": "...",
    "today": "2026-10-19",
    "constraints": {"language": "zh", "freshness": "..."}
  },
  "sources": [
    {
      "id": "source_A",
      "description": "...",
      "capabilities": ["search"],
      "limitations": ["..."]
    }
  ]
}
```

检索后另建 state，按字段保留片段身份；不把整份 `SearchResult`、宿主 transcript 或凭据对象直接序列化。[推断][P7][P5]

```json
{
  "request": {"query": "...", "today": "2026-10-19"},
  "evidence_shape": "fact_lookup",
  "passages": [
    {
      "id": "P0",
      "title": "...",
      "url": "https://example.org/...",
      "snippet": "...",
      "source_type": "official_documentation",
      "clipped": false
    }
  ],
  "coverage": {"evaluated_ids": ["P0"], "omitted_count": 0}
}
```

- 每题指向 `` `passages[0].snippet` `` 或 `` `sources[0]` ``；官方明确推荐带反引号的 dot-and-index path。[P5]
- `source_type` 是事实 metadata，不等于正确性保证；官方 RAG 示例把 passage 的 id/title/text/source_type 一起发送。[推断][C2]
- `coverage`、`clipped` 是 WRR 提议字段，用于防止模型把省略当不存在；具体效果 `[未核验]`，需后续评估。[推断][P5]
- 同一请求内问题都看同一份 state。不能用“每题只看各自片段”描述协议隔离；路径只是显式语义指向。[P7][P5]

### 2.3 Confidence 门：不能把 probability 当 confidence

Choice 和 Score 返回 confidence，Noul 只返回 P(yes)。confidence 来自概率分布的形状，不能代表整个搜索工作流正确率。[P6]

| 类型 | 官方定义 | WRR 用法建议 |
|---|---|---|
| Noul | `p` 接近 0.5 表示不确定；无独立 confidence | `[推断]` source_fit/enough 留出 uncertain 状态；0.49 与 0.51 不宜产生无说明的相反强结论。[P6][C9] |
| Choice | `confidence=(p_max-1/n)/(1-1/n)` | `[推断]` evidence_shape/next_action 保留 probabilities 和 confidence；低 confidence 时走确定性 fallback。[P6][P2] |
| Score | confidence 还考虑概率离峰值 level 的距离 | `[推断]` 若增加分维度 Score，不能把 score 当 P(yes)，也不能直接套 Choice 公式。[P6][P4] |
| 派生 Noul confidence | 可自行计算 `abs(2*p-1)` | `[推断]` 若桥接要统一字段，标 `derived`，不能伪装成官方返回值。[P6] |

官方示例阈值有意不同：intent routing 用 0.5；银行示例使用 0.6 floor、转账 >0.85；confidence 页面另示例 >0.9 后确认执行。[P1][P2][P6]

`[推断]` WRR 不直接抄这些数值。阈值按错误代价、来源召回与评估集校准；尤其不能把“模型有把握”当“允许外发”。[P6][P5]

### 2.4 Speculative fan-out：可合并独立问题，不能消除真实依赖

官方 ticket 示例同时问 5 题：category Choice、bug severity Score、repro steps Noul、refund Noul、frustration Score；类别不适用时忽略分支答案。[P3]

- 检索前可同时问 evidence_shape、每来源 fit、freshness；source_fit 必须直接依据 query 和 descriptor，不读取本次分类结果。[推断][P3][P5]
- 若 fit 的问法依赖 evidence_shape，可显式写条件前提，或把已有分类结果放入下一请求；不能假定模型内部串行传递。[推断][P5]
- 检索后可同时问 rel、inj、answerability、原始集合 coverage；代码在相同数据范围内组合。[推断][P3][C5]
- 对“刚刚筛出的集合”重新判断 enough，需要新 state；fan-out 不会让 enough 看见 rel/inj 的返回值。[推断][P5]
- “增加问题通常几乎不增延迟”是官方模式说明，不是无限题数、零 token 成本或 WRR 的时延保证。[P3][C8]

### 2.5 JSON instructions / criteria：适合边界澄清，不必全量结构化

`instructions`、Choice 选项描述、Score levels、Noul 的 true/false 描述均支持 JSON object/array；简单明确的短句仍可保留 string。[P8][P5]

```json
{
  "type": "noul",
  "instructions": {
    "question": "Does `sources[0]` provide evidence useful for `request.query`?",
    "focus": "Use the declared capabilities and limitations only.",
    "not_assumed": "Do not infer availability or quality from the source name."
  },
  "criteria": {
    "true": {"what": "Can retrieve the required evidence shape."},
    "false": {"what": "Only shares a topic or lacks the required capability."}
  }
}
```

`[推断]` evidence_shape 的各选项宜统一使用 `what/not_for/examples`；例如 academic 与 fact_lookup 的区别写进 rubric，不让品牌名或问题 ID 承担含义。[P8][P5]

## 3. Cookbooks：逐篇取舍与官方报告数字

### 3.1 Re-ranking：per-item Noul，不是单 Choice 分布

- **问题类型与数量：** 每个 query-candidate pair 1 个 Noul；40 queries × 30 candidates，共 1,200 请求。示例用 12 workers 并发。[C1]
- **State：** `{query_excerpt, candidate_passage}`。每个请求只含一个 pair，没有看到其他候选。[C1]
- **Rubric：** true 是建立 query 所引用的具体法律命题；false 是仅同主题或同法理。返回 noul 作为排序分数。[C1]
- **阈值/confidence：** 按 noul 降序重排，不设置接纳阈值，也不使用独立 confidence。重排保留原 shortlist。[C1]
- **官方数字：** 3,565 passages、40 queries、BM25 top-30 包含 gold 的比例 100%；top-1 5%→18%，top-5 15%→35%，top-10 38%→62%。[C1]
- **官方资源记录：** 1,200 calls、1,536,002 input tokens、25,200 output tokens，历史示例估价 $0.0645；使用 `jev-1.12`。[C1]
- **WRR 含义：** `[推断]` 采用“具体证据 vs 同主题”的 contrastive rubric；不照搬 1,200 次调用。官方也建议真实应用把同 pair 的多个问题合并。[C1][P3]
- **限制：** rerank 无法找回未进入 shortlist 的来源或片段；选源召回与重排质量必须分开评估。[推断][C1]

### 3.2 Classifying RAG passages：主题、可用事实、前提冲突、注入分开问

- **问题类型与数量：** 每 passage 一个 request，内含 4 个 Noul：is_relevant、contains_answer_evidence、contradicts_query_premise、contains_prompt_injection。[C2]
- **State：** query + passage `{id,title,text,source_type}`；6 queries 各检索 top-12；4 workers 并发，非单次全候选 batch。[C2]
- **路由顺序：** injection >0.70 排除；contradiction >0.70 进入 conflict；relevance <0.45 排除；evidence >0.55 接受；否则排除。[C2]
- **Confidence：** 使用 Noul probability，未加独立 confidence 门。数字是该 corpus 的示例配置，官方明确不能当通用默认。[C2]
- **官方样本：** 81 passages，其中 80 来自固定版本 Supabase auth docs、1 个植入 injection；6 queries 中 2 个带错误前提。[C2]
- **官方结果：** injection 片段 similarity 排第 1、rel=0.71、inj=0.99，最终排除；纠正前提的片段排第 7、contra=0.92、rel=0.49、evidence=0.51，保留为 conflict。[C2]
- **另一 query：** 12 passages 中 4 个进入 evidence；所处原始 ranks 包括 8、9、11。示例不是全量质量 benchmark。[C2]
- **WRR 含义：** `[推断]` 不把反驳 query 错误前提的材料误杀。若当前 rel 已含直接可用事实，可先明确这一含义，再决定是否单独增加 evidence 题，避免同义题重复。[C2][P5]
- **范围边界：** `[推断]` conflict metadata 可以辅助下游；本轮不建议顺势增加答案生成或全文核验模块。[C2][P5]

### 3.3 Skill suggestion：182→3→至多 1，rank 与绝对适配检查分离

- **请求 1：** 1 个 Choice 覆盖全部 182 skills，另有 3 个 Noul 检查请求是否需要行动/流程；共 4 题。[C3]
- **请求 1 state：** `{request,recent_context:""}`；skill 的短 description 放在 Choice criteria，非把完整 roster 塞进 state。[C3]
- **第一道门：** `prose_suffices` 反向处理，与其他 2 个 Noul 取 mean；mean <0.30 时不建议 skill。[C3]
- **Shortlist：** 取 Choice probabilities top-3；182 个短描述平均 54 字符、最多 60 字符。[C3]
- **请求 2：** 1 个 top-3 Choice + 每候选 1 个 fits Noul，共 4 题；criteria 用完整 description + SKILL.md 前 700 字符。[C3]
- **第二道门：** `max(fits)<0.30` 时放弃全部；否则返回 Choice winner。示例没有显式检查 `fits[winner]>=0.30`。[C3]
- **Confidence：** 代码使用概率排序与绝对 Noul threshold，没有对 Choice confidence 再设门；不能把“rank→recheck”描述为“两次 confidence gate”。[C3]
- **官方数据：** 488 requests，315 有唯一覆盖 skill、173 无覆盖；171 个 distinct skills 被覆盖；与 `claude-haiku-4-5-20251001` 对照。[C3]
- **官方结果：** wrong load 16.8%→7.3%；needless load 9.8%→4.0%；oracle 分别 2.5%、1.2%。37 个原错例修好、7 个原对例被建议带错。[C3]
- **限制：** covered requests 由 Claude 根据 SKILL.md 生成；这些输入比真实用户输入容易。实验使用 `jev-1.12`，不是 WRR 当前模型验证。[C3]

**是否适合 WRR 选源？** `[推断]` 适合“大目录短画像初筛，再读少数候选详情”的思路；不适合直接替换当前“最多 4 个来源”的集合选择。[C3][C7]

- WRR 画像已完整且来源不多时，直接 per-source Noul 更简单；没有必要为了模仿 cookbook 固定增加一个串行请求。[推断][C3][P3]
- 目录扩张且描述被截断时，可以研究 rank→recheck；第二步仍按来源逐项 Noul 选择子集，不能只返回一个 Choice winner。[推断][C3][C7]
- 采用两阶段后会挤占“前 1 次、后 1 次、恢复 1 次”的总调用预算。当前设计不能在不改预算合同的情况下承诺两阶段选源。[推断][C3][P5]
- 若输出单 winner，验证 winner 本身的 fit；不照搬“其他项 fit 高就让 winner 通过”的示例组合。[推断][C3]

### 3.4 Citation check：先精确检查，再判关系

- **问题类型与数量：** 每个通过 quote 定位的 citation 1 个 Choice；选项 supports/contradicts/says_nothing。[C4]
- **State：** `{claim,section}`。先由代码归一化空白和引号，再在源文档找 quote；找不到直接 fabricated，不调用模型。[C4]
- **阈值：** Choice confidence ≥0.8 才自动接受该 verdict；低于 0.8 交人工确认。该门覆盖正反结论，不只 supports。[C4]
- **官方数字：** RFC 7519，58,365 字符、45 sections、8 citations；4 verified、1 fabricated、1 contradicted、2 unsupported。[C4]
- **具体数值：** verified confidence ≥0.93；contradicted=0.99；2 个 unsupported 分别 0.27、0.56，需 review。[C4]
- **WRR 含义：** `[推断]` URL/引用存在性与语义支持关系分开；snippet relevance 不能冒充 citation verified。设计非目标中的全文事实核验继续不做。[C4]
- **限制：** 示例精确匹配会把轻微改写或截断 quote 标 fabricated；不能泛化成所有引用形式的最终真值。[C4]

### 3.5 Semantic find：Choice 排名必须配 exists

- **问题类型与数量：** 每 query 1 个 Choice 对 218 line IDs 排名，另有 1 个 exists Noul，共 2 题、1 请求。[C5]
- **State：** 带 `L000|...` ID 的完整文档，查询写入 instructions；同文档重复 query 时 state 不变。[C5]
- **阈值：** exists ≥0.70 为 answered；<0.35 为 absent；其间 partially addressed。Choice probabilities 不作为答案存在率。[C5]
- **官方数字：** 218 lines、43,980 字符，4 个 query；2 个直接答案 exists=0.98/0.97；缺答案 0.14；部分回答 0.46。[C5]
- **关键负例：** arbitration 的“最近似行”Choice probability=0.86，但 exists=0.14；Choice 强排名不能证明存在答案。[C5]
- **WRR 含义：** `[推断]` 如有任何 Choice-based rank，必须另判是否存在适用项；per-item Noul 则允许所有来源同时低。[C5][C7]
- **Answerable 题：** `[推断]` 可新增“公开、已提供的片段中是否至少存在可用回答依据”，作为 presence 信号；这仍弱于多面问题的完整 enough。[C5][C2]

### 3.6 LLM guardrails：assessment 与 policy 分离

- **问题类型与数量：** 每 message 4 个 hazard Noul + 1 个 severity Score，共 5 题；输入和输出各有对应 battery。[C6]
- **State：** 单条 message 文本；输入 10 条、输出 5 条，共 15 个示例。并非通用安全数据集成绩。[C6]
- **阈值：** strict：review≥0.35、action≥0.70；permissive：review≥0.35、action≥0.85；severity≥2.0 把 review 升为 block。[C6]
- **组合顺序：** support > block > review > pass；由代码定义优先级，hazard 与 severity 不混成一个平均分。[C6]
- **官方数值：** 同一 jailbreak=0.74、severity=0.51，在 strict 下 block，在 permissive 下 review；无需重新 inference。[C6]
- **WRR 含义：** `[推断]` 注入门独立于 rel；阈值、action、failure policy 集中审阅。低注入概率不等于已获得外发权限。[C6][P5]
- **不照搬：** `[推断]` WRR 不扩充为医疗或自伤审核产品；只取“多个明确 hazard + 确定性 policy”的结构。[C6]

### 3.7 Function calling：N 选子集的直接官方依据

- **问题类型与数量：** 每 command 1 次请求，54 题，覆盖 10 个 functions 的 handler 与全部闭集参数；28 个可填参数。[C7]
- **State：** 自然语言 command；function 与参数描述由 spec 构造成 questions/criteria；返回后只读取选中 function 的参数答案。[C7]
- **类型映射：** 单值 `Literal`→Choice；`list[Literal]`→每成员一个 Noul；bool→Noul；可选参数另问 stated。[C7]
- **阈值/confidence：** 页面报告整次调用的最弱 judgment confidence；未公开 dispatcher 内每类参数的具体布尔切分阈值，记 `[未核验]`。[C7]
- **官方数字：** 14 commands；比较 NVDA/AMD/MSFT 成功选出 3 个、排除另外 3 个；示例 call confidence=0.94。[C7]
- **语义限制：** 非闭集 int/free text/date 保留函数默认值，不尝试让 TypeSafe 生成参数。[C7]
- **WRR 含义：** `[推断]` 从 N 来源选可变子集，最贴近官方 set 参数；保留每来源 fit Noul，再由代码排序/限额/保留主力。[C7]

**选源结论：** 官方同时展示 Choice 排名和 per-item Noul，两者解决不同问题。WRR 的多来源集合更像 per-item Noul；Choice 适合 evidence_shape、next_action 或确实只选 1 个 handler。[推断][C7][C3][C5]

### 3.8 Parallel questions：同 state 合并，速度对比是串行基线

- **问题类型与数量：** 8 Noul + 2 Choice + 3 Score，共 13 题；每种策略重复 5 次。[C8]
- **State：** `{article:{source,text}}`，固定 GDPR Wikipedia revision，53,777 字符；比较 1 请求全题与 13 请求单题。[C8]
- **阈值/confidence：** 研究 batching 的答案变化，不设生产动作门；追踪 Noul、Choice top probability、归一化 Score。[C8]
- **官方数字：** 1 请求平均 $0.000497/0.27 秒；13 个串行请求平均 $0.006090/2.71 秒；12.2 倍便宜、10.0 倍快。[C8]
- **答案稳定性：** Choice、Score、6 个 Noul 的标准差为 0；另 2 个 Noul 存在采样变化。不能把标题“答案不变”写成位级确定性承诺。[C8]
- **WRR 含义：** `[推断]` 同一批公开候选的独立题可以合并，减少重复上下文；10 倍速度不能外推到原本已并行的请求。[C8][P3]

### 3.9 Self-consistency: nouls：uncertain 比 0.5 硬切更诚实

- **问题类型与数量：** 同一个 auto-insurance claim，14 个 Noul/请求；每条件 15 次，并给 state 加 fresh uid。[C9]
- **State：** policy、claim、adjuster_notes、claim_history 的嵌套 JSON；对照 LLM 收到相同 claim 的序列化文本。[C9]
- **阈值：** p<0.30→no；0.30≤p≤0.70→uncertain；p>0.70→yes；中间区间交人工，保留原始概率。[C9]
- **官方数字：** TypeSafe 平均逐题标准差 0.0102；covered 的 p 为 0.43–0.53，跨越 0.5；该样本平均 latency=111ms。[C9]
- **限制：** fresh uid 使实验无法区分无关字段敏感性与同输入运行变化；uncertainty band 不是校准保证或最优阈值。[C9]
- **WRR 含义：** `[推断]` enough 可以输出 uncertain，由代码决定预算内补搜或保守返回；不靠重复调用投票来强制稳定。[C9][P5]

## 4. 官方 SDK：Python 保留 httpx；TS 可作为直连备选

### 4.1 包、版本与 async 用法

Python 包名为 `typesafe-sdk`，import 为 `typesafe_sdk`；官方 changelog 最新列出 v0.7.5（2026-10-14）。这不是本机已安装版本或 PyPI 当前最新版本的证明。[S0][S8]

官方提供同步 `TypeSafeClient` 和异步 `AsyncTypeSafeClient`，问题可用 `Noul/Choice/Score` 对象或 raw dictionaries。[S0][S1][S6]

```python
from typesafe_sdk import AsyncTypeSafeClient, Noul, RetryPolicy

async def evaluate(state, questions, budget_s):
    async with AsyncTypeSafeClient(
        model="jev-latest",
        timeout=budget_s,
        retry=RetryPolicy(max_retries=0, timeout=budget_s),
        compression=False,
    ) as client:
        return await client.system_one(state=state, questions=questions)
```

以上展示 API 配置方式；未运行。正式长驻进程应复用 client，不在每次 evaluate 中新建；外层仍需 WRR 的绝对 deadline 与取消管理。[推断][S0][S3][S9]

### 4.2 默认超时、重试、环境变量

| 项目 | 官方 SDK 默认/合同 | WRR 判断 |
|---|---|---|
| HTTP operation timeout | 10.0 秒；提供 http_client 时可继承其 timeout | `[推断]` 不能把它当 end-to-end deadline；显式传剩余预算。[S5][S9] |
| `RetryPolicy.max_retries` | 2，指首次后再试 2 次；0 关闭 | `[推断]` 热路径设 0；否则“judge_calls≤3”不等于最多 3 个 POST。[S4] |
| retry total budget | 30.0 秒，含首次尝试和延迟；None 不限；到重试预算前停止并抛最后错误 | `[推断]` 不能继承此默认，更不能以此替代宿主绝对 deadline。[S4] |
| retry statuses | 408、429、500–599；连接和 timeout 默认可重试 | `[推断]` 故障类别可参考，执行策略由 WRR 固定。[S4] |
| backoff | initial=0.5 秒，max=5.0 秒，jitter=0.25 | `[推断]` 与 1 秒软预算冲突；关闭隐式退避。[S4] |
| Retry-After | 默认遵守 Retry-After 与 retry-after-ms | `[推断]` 非热路径才讨论允许的等待；搜索不无限等服务器建议。[S4] |

环境变量为 `TYPESAFE_API_KEY`、`TYPESAFE_BASE_URL`、`TYPESAFE_DEFAULT_MODEL`、`TYPESAFE_LOG_LEVEL`；默认 root 为 `https://api.typesafe.ai`、model 为 `jev-latest`。[S5][S1]

Cookbooks 里出现的 `TYPESAFE_ENDPOINT` 是示例代码自行读取后传给 base_url，不是 SDK 官方默认变量；不能照抄为 WRR 配置合同。[C1][C2][C3][S5]

### 4.3 Responses、usage、异常与严格性

- `SystemOneResponse` 是 Pydantic model，含 answers、按类型的 nouls/choices/scores、model、usage；支持自定义 response_model。[S7][S1]
- Usage 公开字段为 `input_tokens`、`output_tokens`，可为 None；不能把缺值直接记成“已知 0”，也不能声称必有美元 cost。[S7]
- HTTP 错误有 status/body/headers/endpoint/request_id；`x-typesafe-request-id` 可作诊断关联。[S10]
- 认证、权限、限流、5xx、连接、timeout、response validation 均有专门异常；validation error 含 field_path。[S10]
- SDK forward compatibility 会警告并跳过不认识的 answer kind；WRR 仍须检查预期 answer key、type、数值与完整性，不能将“SDK 解码成功”当合同全满足。[S3][推断]

`[推断]` 第 5 节的 `Judge` 返回值不宜只剩 `dict[str,Answer]`；应保留 answers 与有限 model/usage/request metadata，再分别输出到 evidence/diagnostics。[S7][S10]

### 4.4 连接复用、依赖重量、取消与 batch helper

- 官方明确推荐复用 client；默认最多 100 connections，空闲 keepalive 30 秒，默认 HTTP/1.1；HTTP/2 为可选 extra。[S3]
- 默认对 ≥1 KiB request bodies 使用 zstd；Python <3.14 可装 zstd extra，也可 `compression=False`。第三方 gateway 不一定支持该压缩。[S3][S8]
- SDK async transport 类型为 `httpx2.AsyncClient`，不是 WRR 当前 `httpx.AsyncClient`；传入的 http_client 在 SDK 关闭时也会被关闭。[S9]
- Python `system_one` 文档没有 AbortSignal/signal 参数。可以 await，但 task cancellation 穿透重试、连接池清理与取消后不再 POST 的端到端行为，本轮 `[未核验]`。[S9][S4]
- 已读 Python public client 仅列 system_one、models.list、assets、aclose，没有多 state batch helper；应用并发由 cookbook 的 thread pool 自行实现。不能把“未列出”扩大成源码绝无内部 helper。[S9][C1][C2]

固定 v0.7.5 的官方 package metadata 列出 8 个直接依赖：anyio、httpx2、packaging、pydantic、pydantic-core、tenacity、truststore（非 emscripten）、typing-extensions；requires-python≥3.10。依赖具体约束以[固定 tag 元数据](https://github.com/typesafe-ai/typesafe-sdk-python/blob/v0.7.5/pyproject.toml)为准；docs 同时确认 httpx2、Pydantic 与可选压缩/HTTP2。[S9][S6][S3]

### 4.5 对照 `wrr/search_filter.py`：保留 httpx 的明确理由

本地证据：`wrr/search_filter.py:212–223,263–312` 已有 async httpx、可注入 client、0 次应用重试、禁重定向、外层 `asyncio.timeout_at(deadline)`；自建 client 使用 `trust_env=False`。`pyproject.toml:22–26` 已依赖 httpx。

| 维度 | 换官方 SDK | 保留现有 httpx 的结论 |
|---|---|---|
| 依赖 | 增加 SDK/httpx2/Pydantic 等；能得到 typed classes | `[推断]` WRR 已有 httpx，不为一个窄 endpoint 增加第二 transport 栈。[S9][S6] |
| 重试 | 可精确设 max_retries=0，并非不可控 | `[推断]` 现状天然单 POST 更容易审阅；反对的是默认值，不是 SDK 无法配置。[S4] |
| deadline/取消 | async 可 await；无文档 signal 参数，跨桥行为未证明 | `[推断]` 保留绝对 deadline 与宿主取消边界，未来采用 SDK 也不得删除。[S9][S4] |
| 复用 | 有连接池；关闭 SDK 会关闭 supplied client | `[推断]` 沿用 caller-owned httpx client；把生命周期写进 Judge adapter，不照抄 SDK 所有权。[S3][S9] |
| 返回校验 | typed response、异常分类、未知 kind 可跳过 | `[推断]` 借鉴字段与错误分类；保留 WRR exact answer-set 与有限数值检查。[S7][S10][S3] |

**结论：** `[推断]` WRR 7 Python `TypeSafeJudge` 保留 httpx。新增 SDK 的收益主要是接口便利，尚未抵消第二 transport、默认策略与取消合同迁移成本；不因为“官方”二字强制换库。[S3][S4][S9]

### 4.6 JavaScript SDK：OMP/Pi 扩展可直连，但优先宿主 judge

- 包名 `@typesafe-ai/sdk`；Node.js≥22，提供 ESM、CommonJS、TypeScript declarations；页面 source 链接指向 v0.7.0，但本轮未查 npm 当前版本。[J0]
- 用 `new TypeSafeClient()`、`await client.systemOne({state,questions})`；choice helper 支持推导 typed answers。[J0]
- TypeSafeClientConfig 支持 apiKey/baseURL/defaultModel/custom fetch；显式参数优先环境变量，再到默认值。[J1]
- timeout 默认 10,000 毫秒/attempt，不含总重试预算；RetryPolicy 默认 maxRetries=2、500–5,000 毫秒退避、maxRetryAfterMs=60,000。[J1][J2]
- `[推断]` 若宿主缺少可用 judge API 且允许自有 key，TS 直连可行；配置 `retry:{maxRetries:0}`、显式 timeout 与总 deadline。[J0][J1][J2]
- `[未核验]` 按本任务只读 3 个 JS 页面，未取得 per-request AbortSignal 合同，也未验证 Bun/OMP/Pi 运行兼容性；不能因此承诺取消已满足。[J0][J1][J2]
- `[推断]` OMP/Pi 已有 registry/role 的方案仍优先；直接 SDK 不能绕过宿主凭据、后端选择与 usage 归属。不要把 key 放浏览器，dangerouslyAllowBrowser 默认 false。[J1][P5]

## 5. Agent skill 与 coding-agents：开发参考，不是运行依赖

### 5.1 官方 skill 的真实形态与安装

官方 skill 名为 `typesafe-ai`，是带 references 的 `SKILL.md` 目录；目标是帮助 coding agent 正确编写 TypeSafe 集成，不是可执行检索 CLI 或运行时 inference server。[A0][A1]

| 环境 | 官方安装方式 | 路径/范围说明 |
|---|---|---|
| Claude Code | `claude plugin marketplace add typesafe-ai/skills`；`claude plugin install typesafe@typesafe-ai` | plugin 安装；页面未给固定磁盘 cache 路径，记 `[未核验]`。[A0] |
| 其他 agents | `npx skills add typesafe-ai/skills --skill typesafe-ai` | 默认 project-local，`-g` 为 global；由安装器选择 agent，页面未承诺统一绝对路径。[A0] |
| 手动 | 复制完整 `skills/typesafe-ai`，包含 references | 放目标 agent 的 skills directory；上游入口为 `skills/typesafe-ai/SKILL.md`。[A0] |

官方提醒只用 1 种安装方式，避免重复副本；插件、skills.sh、手动目录各有对应更新方法。[A0]

官方 raw SKILL.md 已读：要求读取 live docs、按任务查 cookbook、明确 Noul 多标签/Choice 单选、保留代码控制权、对真实依赖采用第二请求。该 skill 没有提供通用搜索执行入口。[A0][P5][C7]

### 5.2 与 hermes-jev-skills 的重叠边界

- **官方部分：** typesafe-ai skill 是开发知识与设计指导，介绍 primitive、pattern、SDK 和 evaluation 思路。[A0][A1]
- **已有研究部分：** `hermes-jev-skills` 的 jevkit/client.py、search.py、rerank.py 是第三方 client 与检索 gate 实现，已有研究第 2.3 节已核验固定提交。
- **重叠：** `[推断]` 都涉及判断、筛选、ranking、问题 rubric；不能因此认定二者是相同发行物或可互换模块。[A0][C1][C7]
- **不重叠：** `[推断]` 官方 skill 不替代第三方运行代码；WRR 的产品 runtime 不需要任一 SKILL.md 才能 POST API。[A0][S0]
- **决策：** `[推断]` 可在开发者接入指南推荐官方 skill；不列为 WRR runtime dependency，不把纯 skill 纳入 SourceDescriptor 可执行检索候选。[A0][A1]

### 5.3 本机检查与 coding-agent 误区

本轮用目录读取和名称匹配检查 `~/.agents/skills`、`~/.claude/skills`、`~/.omp/agent/skills`；3 个一级目录均可读，未发现名称含 typesafe 或 jev 的条目。

这是与指定 `ls ... | grep -i -E 'typesafe|jev'` 等价的一级名称检查，不是插件缓存、project-local 或全盘安装审计；不能推导“本机任何位置都没安装”。[本轮观察；安装范围依据 A0]

官方 coding-agents 页面明确：Jev 不生成代码、文本或 agent 对话；不能通过把主模型设置为 jev-latest 来替代 Claude Code 等 coding LLM。[A1]

`[推断]` WRR 把 Jev 用于 routing/filtering/guardrails 符合官方用途；OMP/Pi 中 Jev role/registry 与主生成模型的角色必须分开说明。[A1]

## 6. 对设计稿的具体修订建议

### 6.1 应改：条目、官方依据、建议改法

下表仅给修订建议，不修改设计稿。`[推断]` 表示从官方合同映射出的 WRR 设计判断。

| 条目 | 官方依据 | 建议改法 |
|---|---|---|
| 4.2 state 仅写字段拼接 | object 命名关系、嵌套路径。[P7][P5] | `[推断]` 写出 request/sources 的 JSON schema；来源限制、能力字段分开，不仅一段 description。 |
| 4.2 source_fit 问法 | set 用 per-item Noul。[C7] | `[推断]` 保留每来源一题；问题完整引用指定来源和 query，true/false 区分适配与同主题。 |
| 4.2 evidence_shape 无 confidence 门 | intent routing 与 uncertainty policy。[P1][P2] | `[推断]` 低 confidence 与 unknown 同走默认计划；保留分布，不只看 label。 |
| 4.2 source_fit≥0.5 硬切 | Noul 0.5 附近不确定。[P6][C9] | `[推断]` 明确 accept/reject/uncertain；主力保留是代码 policy，不伪称每个保留项通过 judge。 |
| 4.2 source_fit 与 evidence_shape 同请求 | 题目独立，看不到其他答案。[P5][P3] | `[推断]` fit 直接对 query；若需要依赖 shape，显式条件化或另请求，不能隐式串行。 |
| 4.2 强制保留主力 | assessment 与执行 policy 分离。[P5][C6] | `[推断]` 将 forced/source_fit/default 的选择理由分别记录，避免解释成模型选择了全部来源。 |
| 4.5 passage 为混合字符串 | RAG 使用 id/title/text/source_type。[C2][P7] | `[推断]` 用字段化对象，保留 clipped、id 与实际模型看到的 snippet 范围。 |
| 4.5 rel 已定义直接帮助回答 | RAG 分 subject relevance 与 answer evidence。[C2] | `[推断]` 保留当前严格 rel 或拆 evidence 题，二选一明确命名；不要仅增同义题。 |
| 4.5 未显式保留前提冲突证据 | contradiction 在 evidence 前判。[C2] | `[推断]` rel rubric 明确“纠正错误前提也有用”；若评估仍误杀，再增 contra 维度与 conflict 标签。 |
| 4.5 缺 answerable/presence | Choice ranking 配 exists。[C5] | `[推断]` 同批加集合 answerable，明确“至少有可用依据”；与覆盖全部需求的 enough 区分。 |
| 4.5 enough 针对原始候选 | 同请求互不见答案。[P5][P7] | `[推断]` 改名 raw_candidate_sufficiency；不得直接赋给 delivered sufficiency。被过滤/截断影响时标 unknown。 |
| 4.5 需证明实际集合 enough | 第二请求用于新 state/真实依赖。[P5][C3] | `[推断]` 若接受性要求是实际返回集合，需筛后独立评估；同时重算调用预算，不能仍声称常规 2 次且所有路径≤3。 |
| 4.5 next_action argmax 直接触发恢复 | confidence 决定是否行动。[P2][P6] | `[推断]` 低 confidence→unknown/不执行额外动作；不新增自由文本 query。 |
| 4.5 authenticated/local 未读材料 | 只按已给上下文判断。[P5][P7] | `[推断]` public-only enough 的 scope 写入 quality；不能把 private/unjudged 的存在自动计为已充分。 |
| 4.5 inj<0.5 就放行 | hazard/uncertainty 分开处理。[C6][C9] | `[推断]` 注入可疑中间区间按安全 policy 处理；不用 relevance 高分抵消；具体数值后续校准。 |
| 5 Judge 只返回 dict | SDK 有 model/usage/request_id。[S7][S10] | `[推断]` 定义 JudgeResponse，含 answers 与有限 metadata；宿主与直连归一化，不丢失 usage。 |
| 5 只写 signal | Python 无 signal 参数。[S9] | `[推断]` 分别定义 TS AbortSignal→RPC cancel→Python task cancel；明确用户取消不得转成相关性 fail-open 成功。 |
| 5 1 秒预算未定义 retry | 默认 10 秒 operation/2 retries/30 秒 budget。[S4][S5] | `[推断]` 写清 no retry、单 POST、外层绝对 deadline；judge_calls 区分逻辑判断与物理请求。 |
| 5 未定 transport 依赖 | SDK 使用 httpx2、自有关闭语义。[S9][S3] | `[推断]` 保留 httpx 与 caller-owned 生命周期；官方 SDK 作为未来替代实现，非本次必选依赖。 |
| 全部 questions/thresholds 位置 | 官方 skill 要求集中审阅。[A0] | `[推断]` 用一个模块维护 rubric 与 policy constants；不同 stage 明确命名，不在各宿主复制。 |

### 6.2 已一致：无需因官方材料而推翻

| 设计现状 | 一致的官方模式 | 保留结论 |
|---|---|---|
| 确定性门先于 Jev | Use code when you can。[P5] | `[推断]` 保留 DOI、本地、授权、隐私、预算门；不交给模型自行扩权。 |
| 前后各批量问独立问题 | Speculative fan-out。[P3][C8] | `[推断]` 保留批量设计，明确独立性和实际依赖边界即可。 |
| evidence_shape/next_action 用 Choice | intent routing、handler selection。[P1][C7] | `[推断]` 类型恰当；补 confidence/unknown 路径，不改成生成文本。 |
| source_fit/rel/inj 用 bool 抽象 | Noul 评估真假条件。[C7][C2] | `[推断]` bool→noul 的 adapter 映射保留；不能在直连 wire 使用 bool。 |
| 恢复固定≤1轮、query 不生成 | 代码掌握 workflow 与副作用。[P5][A1] | `[推断]` 保留有界恢复，不从 cookbook 额外引入 agent loop。 |
| 原始结果身份、URL 来源不变 | rerank 只重排既有 shortlist。[C1] | `[推断]` 保留 selection-only 合同。 |
| 质量按阶段分开 | 独立判断再组合、保留原始信号。[P4][P5] | `[推断]` 保留 executed/relevance/sufficiency，不用一个总 confidence 覆盖失败。 |
| 纯 SKILL.md 仅 handoff | 官方 skill 是 coding guidance。[A0][A1] | `[推断]` 不将 TypeSafe skill 注册为检索来源。 |

## 7. 仍需确认：不把缺证据写成已支持

1. **实际交付集合的充分性预算。** `[未核验]` 当前 3-call ceiling 下如何同时满足严格筛后 enough 与恢复后再判，需要主设计明确取舍；官方独立性不允许省略此依赖。[P5][C3]
2. **取消与资源清理。** `[未核验]` Python SDK task cancellation、TS per-request AbortSignal，以及 RPC→Python→HTTP 的停止行为；本轮没有运行取消实验。[S9][J1][J2]
3. **生产阈值与质量数字。** `[未核验]` 中文 query、WRR 来源画像、snippet 截断下的 source_fit/rel/inj/enough 校准；cookbook 阈值和历史模型数字只作起点。[P6][C2][C9]
4. **Skill 全部安装位置与发行版本。** `[未核验]` project-local、Claude plugin cache、其他宿主目录是否已有副本；Python/JS 本机实际安装版本与当前 registry 最新版本未查询。[A0][S8][J0]
5. **Function calling dispatcher 内部门限。** `[未核验]` 指定 cookbook 正文未展开每个 bool/set/stated 的数值 threshold；不根据输出反推为 0.5。[C7]

## 8. 官方资料索引

以下页面均已实际读取；JS 按任务限定只读总页、TypeSafeClientConfig、RetryPolicy。正文所用 `consistency_noul` 的官方实际路径为 `consistency_noul_cookbook`。[C9]

### Patterns 与 concepts

[P0]: https://docs.typesafe.ai/patterns
[P1]: https://docs.typesafe.ai/patterns/intent-routing
[P2]: https://docs.typesafe.ai/patterns/confidence-routing
[P3]: https://docs.typesafe.ai/patterns/fan-out
[P4]: https://docs.typesafe.ai/patterns/composite-scoring
[P5]: https://docs.typesafe.ai/concepts/how-to-build-with-system-one
[P6]: https://docs.typesafe.ai/confidence
[P7]: https://docs.typesafe.ai/concepts/state
[P8]: https://docs.typesafe.ai/primitives/advanced

### Cookbooks

[C1]: https://docs.typesafe.ai/cookbooks/rerank_typesafe
[C2]: https://docs.typesafe.ai/cookbooks/classifying_rag_passages
[C3]: https://docs.typesafe.ai/cookbooks/skill_suggestion
[C4]: https://docs.typesafe.ai/cookbooks/citation_check
[C5]: https://docs.typesafe.ai/cookbooks/semantic_find
[C6]: https://docs.typesafe.ai/cookbooks/llm_guardrails
[C7]: https://docs.typesafe.ai/cookbooks/function_calling
[C8]: https://docs.typesafe.ai/cookbooks/parallel_questions
[C9]: https://docs.typesafe.ai/cookbooks/consistency_noul_cookbook

### SDK、skill 与 coding-agents

[S0]: https://docs.typesafe.ai/sdk/python
[S1]: https://docs.typesafe.ai/sdk/python/usage/basic
[S3]: https://docs.typesafe.ai/sdk/python/usage/advanced
[S4]: https://docs.typesafe.ai/sdk/python/api/retries
[S5]: https://docs.typesafe.ai/sdk/python/api/constants
[S6]: https://docs.typesafe.ai/sdk/python/api/types/questions
[S7]: https://docs.typesafe.ai/sdk/python/api/types/responses
[S8]: https://docs.typesafe.ai/sdk/python/changelog
[S9]: https://docs.typesafe.ai/sdk/python/api/clients/async
[S10]: https://docs.typesafe.ai/sdk/python/api/exceptions
[J0]: https://docs.typesafe.ai/sdk/javascript
[J1]: https://docs.typesafe.ai/sdk/javascript/api/interfaces/TypeSafeClientConfig
[J2]: https://docs.typesafe.ai/sdk/javascript/api/interfaces/RetryPolicy
[A0]: https://docs.typesafe.ai/agent-skill
[A1]: https://docs.typesafe.ai/introduction/coding-agents
