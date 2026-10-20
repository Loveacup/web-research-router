# 多来源搜索路由、质量验证与判官：学术依据

> 建议先读第 1 节的组件映射，再按需查看论文卡片；重构决策不要只看论文的单一最高分。
> 调研日期：2026-10-19。范围：经典 federated search、检索/模型/工具路由、搜索 Agent、QPP、事实核验与判官安全。
> 本文是方向研究，不是已实施架构；所有「对 WRR 的含义」均标为 `[推断]`，不代表论文直接验证过 WRR 或 Jev。

## 0. 证据口径与结论边界

- 共收录 **43 篇**论文/综述，按原始论文页面、作者 PDF、ACL Anthology、NIST 或 DOI 元数据核验；每张卡片的原文链接也是该卡片事实陈述的来源。
- 年份区分「首次预印本」和「会议出版」；例如 Self-RAG 是 2023 预印本 / ICLR 2024，RouterRetriever 是 2024 预印本 / AAAI 2025，不能混成同一年份。
- 数字仅摘录已读取的摘要或方法/结果节；不同数据集、模型、成本口径之间不直接横向比较；没有可靠可摘数字时明确不摘录。
- 本文核验了论文元数据、摘要，以及关键方法段落；**没有复现实验，没有评测 Jev，也没有证据证明 `jev-latest` 已达到文中 GPT-4、LLaMA 或专用 evaluator 的能力**。
- 主线证据支持「资源描述 → 选源 → 检索 → 合并 → 质量反馈」，但不支持「一个模型的一个分数即可同时保证相关、真实、安全、完整」。依据：[Federated Search](https://doi.org/10.1561/1500000010)、[QPP-GenRE](https://arxiv.org/abs/2404.01012)、[FActScore](https://arxiv.org/abs/2305.14251)、[Greshake et al.](https://arxiv.org/abs/2302.12173)。

## 1. 机制 → WRR 组件映射

下表全部是 `[推断]` 的组件建议；「论文机制」列链接到相应直接证据，不是建议已经被实现的声明。

| 论文机制 | WRR 组件/决策 | 可借鉴机制 | 不应照搬的点 |
|---|---|---|---|
| [GlOSS / CORI](https://doi.org/10.1145/215206.215328) 的 resource representation | 来源描述 registry | 描述搜索域、查询约束、结果形态，而不只是 provider 名称 | 不要求开放 Web API 提供完整词频/库规模 |
| [Query-based sampling](https://doi.org/10.1145/382979.383040) | 可用来源的经验能力画像 | 用真实调用与历史结果补充声明能力 | 不把几百次探测当每次启动成本；遵守登录、隐私、计费边界 |
| [ReDDE](https://www.cs.cmu.edu/~lsi/Sigir_2003.pdf) 的 relevant-document yield | 路由目标与离线标签 | 预测实际能拿回多少有用结果，而不只判「主题像不像」 | 库内相关文档多，不等于该 API 的 top-k 好 |
| [RouterRetriever](https://arxiv.org/abs/2409.02685) 的专家路由 | Jev 选源候选集 | 候选随本机能力增删，决策与 adapter 解耦 | embedding expert 不等于 API/CLI/MCP 传输方式 |
| [Adaptive-RAG](https://aclanthology.org/2024.naacl-long.389/) | 检索策略选择 | 单次检索、并行多源、迭代补搜分别决策 | 「不检索」不能覆盖用户明确要求实时搜索的任务 |
| [CRAG](https://arxiv.org/abs/2401.15884) evaluator | Jev 质量门 | 结果好则接受、差则换源、模糊则补证据 | 不把所有问题都降成一个 relevance 阈值 |
| [QPP-GenRE](https://arxiv.org/abs/2404.01012) | 结果级标签与集合级质量估计 | 先判每条是否有用，再汇总有用率/排序质量代理 | 没有全库标注时不得宣称已测得真实 Recall |
| [RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf) | 确定性 merge baseline | 不需要跨来源原始分数可比 | 重复后端、转载和多次改写不是独立票源 |
| [SSL merging](https://doi.org/10.1145/944012.944017) | 可选分数校准层 | 用共同文档建立局部分数到统一分数的映射 | 没有分数/交集训练样本时不要强套线性回归 |
| [ToolRet](https://arxiv.org/abs/2503.01763) | 工具描述检索与 shortlist | 先缩小工具池，再判可执行候选；单独测 tool recall | 通用语义相似度高不意味着选到了能完成任务的工具 |
| [Gorilla](https://arxiv.org/abs/2305.15334) | 来源调用契约 | 使用当前工具 schema/帮助信息，不凭模型记忆造参数 | 不让判官直接生成任意 shell 命令 |
| [RouteLLM / Hybrid LLM](https://arxiv.org/abs/2404.14618) | 质量—成本策略 | 在质量约束下选择来源；阈值可由任务配置决定 | 生成模型偏好标签不能直接拿来训搜索源路由 |
| [FrugalGPT](https://arxiv.org/abs/2305.05176) | 补搜/升级状态机 | 先执行，观察质量，再决定是否继续付费 | 质量优先不等于永远先走最便宜但低覆盖的来源 |
| [SAFE / FActScore](https://arxiv.org/abs/2403.18802) | 可选 claim verification | 原子声明、搜索证据、支持关系分别记录 | 搜索结果筛选不能冒充整篇答案事实核验 |
| [FairEval](https://arxiv.org/abs/2305.17926) | judge 校准集 | 位置交换、来源名盲化、人工抽查 | 不把强制输出类型当作无偏或准确的证明 |
| [Indirect prompt injection](https://arxiv.org/abs/2302.12173) | 确定性权限/数据边界 | 检索文本不拥有更改系统策略与调用权限的权力 | Jev 的 injection 检测不是沙箱或安全证明 |

### 1.1 应明确分开的五种决策

1. **可调用性**：本机是否存在该命令/宿主能力、是否授权、当前 schema 是什么；这是运行时事实，不是语义模型可臆测的内容。[推断；依据：Gorilla](https://arxiv.org/abs/2305.15334)
2. **先验选源**：针对当前 query，哪些已确认可用来源最可能有帮助；输入是 query + 来源描述，不应预先引用尚未检索到的结果。[推断；依据：CORI](https://doi.org/10.1145/215206.215328)
3. **检索后筛选**：每条结果是否提供具体帮助、是否为当前任务需要的时间与证据类型；可以做批量判定。[推断；依据：QPP-GenRE](https://arxiv.org/abs/2404.01012)
4. **集合充分性**：是否覆盖必要子问题、是否存在矛盾、下一次调用预计能补什么；不同于「已保留结果数量达到 k」。[推断；依据：CRAG](https://arxiv.org/abs/2401.15884)、[DeepResearcher](https://arxiv.org/abs/2504.03160)
5. **事实与安全边界**：声明被证据支持，不等于证据为真；判为无注入，不等于结果能安全获得工具权限。[推断；依据：FActScore](https://arxiv.org/abs/2305.14251)、[Indirect prompt injection](https://arxiv.org/abs/2302.12173)

## 2. 经典 federated search：不要把已解决的分层问题重新混在一起

### P01. The Effectiveness of GlOSS for the Text Database Discovery Problem — 1994

- 原文：[作者 PDF](https://www.cs.cmu.edu/~tomasic/doc/1994/GravanoGarciaTomasicSIGMOD1994.pdf)；[SIGMOD DOI](https://doi.org/10.1145/191839.191869)。作者：Gravano、García-Molina、Tomasic。
- 一句话机制：用各数据库的词项文档频率与规模估计布尔查询结果量，再选择值得提交查询的数据库。
- 数字/口径：实验涉及 **6 个数据库**；原文扫描 PDF 数字 OCR 丢失较多，因此不引用成功率。
- 对 WRR 的含义 `[推断]`：可借鉴「先搜来源描述，再搜内容」与不同用户目标对应不同选源标准；不照搬词项独立假设或「估计命中数 = 用户价值」。
- 证据边界：早期方法聚焦 Boolean AND 与库发现，并非现代自然语言质量判官。

### P02. GlOSS: Text-Source Discovery over the Internet — 1999

- 原文：[TODS / DOI](https://doi.org/10.1145/320248.320252)。这是 GlOSS 的后续完整论文，不是 1994 论文的同名引用。
- 一句话机制：来源输出紧凑统计摘要，中央服务排序来源；bGlOSS、vGlOSS、hGlOSS 分别处理布尔、向量空间与分布式组织。
- 数字/口径：本文不摘统一提升数字；原文贡献包含有效性与元数据存储成本分析。
- 对 WRR 的含义 `[推断]`：可借鉴统一来源描述与可更新摘要；不照搬「所有来源都愿意导出统计元数据」的合作式假设。
- 证据边界：API、Skill、CLI、MCP 是接入形式，不天然对应互不重叠的内容库。

### P03. Searching Distributed Collections with Inference Networks（CORI）— 1995

- 原文：[SIGIR 1995 DOI](https://doi.org/10.1145/215206.215328)；[学术会议元数据与摘要](https://sigmod.org/publications/dblp/db/conf/sigir/CallanLC95.html)。作者：Callan、Lu、Croft。
- 一句话机制：把 collection ranking、collection selection、result merging 置于 inference-network 框架；CORI 以集合词频等统计表示来源。
- 数字/口径：不摘单一准确率；1995 原文与 **2017 SIGIR Forum 重印**应区分。
- 对 WRR 的含义 `[推断]`：可借鉴选源分数与结果合并分数分离；不照搬复杂关键词权重，也不要把来源匹配分直接当每个文档的真实性分。
- 补充：CORI merge 是来源得分与文档得分的启发式组合；相关描述可在 [ReDDE 原文第 2 节](https://www.cs.cmu.edu/~lsi/Sigir_2003.pdf)交叉核验。

### P04. Query-Based Sampling of Text Databases — 2001

- 原文：[TOIS / DOI](https://doi.org/10.1145/382979.383040)。作者：Callan、Connell。
- 一句话机制：正常发查询、观察返回文档，构建来源内容画像，无需来源配合提供索引统计。
- 数字/口径：[ReDDE 第 2 节](https://www.cs.cmu.edu/~lsi/Sigir_2003.pdf)概括此类采样可用约 **75 次查询、300 篇文档**获得 unigram 描述；这不是当前 Web API 的建议配额。
- 对 WRR 的含义 `[推断]`：可借鉴黑盒来源的经验画像与历史查询复用；不在启动时为每个来源密集探测，不把历史可用性永久当实时可用性。
- 证据边界：内容采样与「扫描本机已安装工具」是两类发现；后者仍需工程层实现。

### P05. Relevant Document Distribution Estimation Method for Resource Selection（ReDDE）— 2003

- 原文：[Si、Callan 作者 PDF，SIGIR 2003](https://www.cs.cmu.edu/~lsi/Sigir_2003.pdf)。
- 一句话机制：在 centralized sample database 中检索，并结合各库规模估计相关文档分布，避免 CORI 在大小极不均衡来源中失真。
- 数字/口径：论文在 **5 个 testbeds** 比较资源选择；并展示「资源选择更好但合并后的文档排序更差」的反例，进而提出 modified ReDDE。
- 对 WRR 的含义 `[推断]`：可借鉴以实际可返回的有用结果量为路由目标；不把总体索引规模、品牌知名度或总命中量当 top-k 质量。
- 关键提醒：资源层目标与最终输出层目标相关，但不等价；应分层测量。

### P06. A Semisupervised Learning Method to Merge Search Engine Results（SSL）— 2003

- 原文：[TOIS / DOI](https://doi.org/10.1145/944012.944017)；前身是 [Using Sampled Data and Regression to Merge Search Engine Results，SIGIR 2002](https://doi.org/10.1145/564376.564382)。此处按一条方法谱系计数。
- 一句话机制：共同采样文档同时具有来源特定分数与中央样本分数，用它们学习跨来源分数归一化模型。
- 数字/口径：摘要报告在多种条件优于 CORI merge，本文不摘未核验的表格增幅；此处 SSL 指 semi-supervised learning。
- 对 WRR 的含义 `[推断]`：可借鉴有共同锚点时再校准分数；如果来源只给排序、没有稳定分数或共同文档，优先用 rank fusion，而不是伪造可比小数。
- 不适用点：来源的时间漂移、API 排序变动会破坏旧校准关系；论文不能证明一次校准永久有效。

### P07. Federated Search — 2011

- 原文：[Shokouhi、Si，DOI](https://doi.org/10.1561/1500000010)；[作者所在大学存档摘要](https://docs.lib.purdue.edu/ccpubs/402/)。出版社页面直连返回 403，元数据与摘要由大学存档核验。
- 一句话机制：综述将问题系统拆为 collection representation、collection selection、result merging，并讨论合作与非合作搜索环境。
- 数字/口径：Foundations and Trends in Information Retrieval **5(1): 1–102**；不是实验结果数字。
- 对 WRR 的含义 `[推断]`：可借鉴三段式核心架构，adapter 只负责来源契约；不因为换成 LLM/Jev 就把三个问题合并成一个无法定位失误的 prompt。
- 证据边界：**全文未逐页核验**，本文只把已读取摘要与可交叉核验的经典论文作为结论依据。

### P08. Overview of the TREC 2013 Federated Web Search Track（FedWeb）— 2013

- 原文：[NIST PDF](https://trec.nist.gov/pubs/trec22/papers/FEDERATED.OVERVIEW.pdf)；[NIST proceedings](https://pages.nist.gov/trec-browser/trec22/federated/proceedings/)。作者：Demeester、Trieschnigg、Nguyen、Hiemstra。
- 一句话机制：用真实异构 Web search engines，分别评估先验资源排序和检索结果合并，显式惩罚重复内容。
- 数字/口径：**157 个引擎、24 类来源**；200 个 topics 中 50 个有完整页面评判；资源选择与合并主指标均为 nDCG@20，合并还报告 P@10、ERR@20。
- 对 WRR 的含义 `[推断]`：可借鉴同一查询跨来源可回放记录、分离 snippet/page judgments，以及重复惩罚；不照搬历史引擎名单或把当年成绩当今天 provider 排名。
- 关键证据：论文中 nsRRF 的 nDCG@20 在不惩罚重复时为 **0.439**、惩罚后 **0.257**，显示重复可显著夸大融合成绩。

### P09. Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods — 2009

- 原文：[Cormack、Clarke、Büttcher，SIGIR 作者 PDF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)。
- 一句话机制：对文档在多个排名中的 `1 / (k + rank)` 求和，不要求不同检索器原始分数可比较。
- 数字/口径：原文 pilot 选定 **k=60** 后保持不变；多组 TREC / pilot 实验较对比方法平均约 **4%–5%** 改善，不是所有数据集都优于每个单系统。
- 对 WRR 的含义 `[推断]`：可借鉴简单、确定、无训练的融合基线；不把 k=60 当理论最优，不让多个共享同一后端的 adapter 重复投票。
- 证据边界：RRF 解决排序融合，不验证内容正确性，也不决定何时停止搜索。

## 3. LLM 时代：检索策略路由、专家选择与多查询融合

### P10. Adaptive-RAG: Learning to Adapt Retrieval-Augmented Large Language Models through Question Complexity — 2024

- 原文：[Jeong et al.，NAACL 2024](https://aclanthology.org/2024.naacl-long.389/)；[arXiv](https://arxiv.org/abs/2403.14403)。
- 一句话机制：小模型根据问题复杂度，在不检索、单步 RAG、多步迭代 RAG 间选择，训练标签来自实际模型结果与数据集偏置。
- 数字/口径：明确为 **3 类策略**；本文不摘跨数据集汇总增益。
- 对 WRR 的含义 `[推断]`：可借鉴「搜几轮」与「搜哪个源」分开的策略层；不把问题看起来简单当作实时信息已知，更不能绕过用户明确搜索要求。
- 不适用点：原文自动标签服务于其 QA 分布，不能直接替换 WRR 的中文、社区、代码、新闻来源标签。

### P11. Self-RAG: Learning to Retrieve, Generate, and Critique through Self-Reflection — 2023 / ICLR 2024

- 原文：[Asai et al.，arXiv](https://arxiv.org/abs/2310.11511)；[ICLR 2024](https://proceedings.iclr.cc/paper_files/paper/2024/hash/25f7be9694d7b32d5cc670927b8091e1-Abstract-Conference.html)。
- 一句话机制：训练模型产生 reflection tokens，按需触发检索，并分别评价检索片段及生成内容。
- 数字/口径：论文报告 **7B、13B** 模型；不将模型规模当成本/延迟基准。
- 对 WRR 的含义 `[推断]`：可借鉴 relevance、support、utility 分开的判断对象；不照搬需要专门训练生成模型的特殊 token 协议到所有宿主。
- 不适用点：外置 Jev 判定是可替换控制面；Self-RAG 是经过训练的模型行为，两者机制不能画等号。

### P12. Corrective Retrieval Augmented Generation（CRAG）— 2024

- 原文：[Yan et al.，arXiv](https://arxiv.org/abs/2401.15884)；[方法全文](https://arxiv.org/html/2401.15884v3)。
- 一句话机制：轻量 retrieval evaluator 给文档质量信号，触发 correct / incorrect / ambiguous 路径，必要时 web search，并 decompose-then-recompose 去噪。
- 数字/口径：实验覆盖 **4 个**短/长文本生成数据集；不摘未逐表核验的百分比。
- 对 WRR 的含义 `[推断]`：最直接可借鉴「判结果 → 选择下一动作」的质量门；不把 ambiguous 强行归为成功或失败，亦不把静态库失败后的 web fallback 照搬成固定 provider fallback。
- 关键差异：WRR 的可用来源本来就可能都是 Web；纠错动作可以是换源、改写、阅读全文、补一手证据，不一定只是再搜一次。

### P13. RouterRetriever: Routing over a Mixture of Expert Embedding Models — 2024 / AAAI 2025

- 原文：[Lee et al.，arXiv](https://arxiv.org/abs/2409.02685)。
- 一句话机制：为各个 query 选择适合的领域 embedding expert，专家可增加或移除而无需重训整个路由系统。
- 数字/口径：BEIR 上相对 MSMARCO-trained model **+2.1 absolute nDCG@10**、相对 multi-task model **+3.2**；不能解释为 WRR 提升 2.1%。
- 对 WRR 的含义 `[推断]`：可借鉴来源池动态变化及经验路由画像；不把「领域相似」当唯一选择信号，还需可执行性、时效、检索深度与返回证据类型。
- 不适用点：选择 embedding model 与选择独立搜索服务的成本/权限/质量分布不同。

### P14. MoR: Better Handling Diverse Queries with a Mixture of Sparse, Dense, and Human Retrievers — 2025

- 原文：[Kalra et al.，arXiv](https://arxiv.org/abs/2506.15862)。
- 一句话机制：对不同 query 零样本加权融合异构 retrievers，利用互补信号，并讨论把非 oracle 的人类信息源纳入检索。
- 数字/口径：摘要报告总计 **0.8B** 参数的组合，相对单个 retriever 平均 **+10.8%**、相对更大 7B 模型 **+3.9%**；此处保留作者的相对增益口径。
- 对 WRR 的含义 `[推断]`：可借鉴按查询动态选择互补来源，而非 provider 永久全序排名；不把多个相同后端入口误认为互补专家。
- 不适用点：需要 embedding/语料内部结构的几何信号，不一定能从黑盒 Web API 或 CLI 拿到。

### P15. RAG-Fusion: a New Take on Retrieval-Augmented Generation — 2024

- 原文：[Rackauckas，arXiv](https://arxiv.org/abs/2402.03367)；[全文](https://arxiv.org/html/2402.03367v2)。注意论文作者不是把该做法推广开的 Raudaschl。
- 一句话机制：围绕原始问题生成多条查询，分别检索，用 RRF 聚合后再生成答案。
- 数字/口径：工业产品问答案例、人工评价；本文不据此声称大规模通用基准的稳定胜率。
- 对 WRR 的含义 `[推断]`：可借鉴必要时多视角查询与 rank fusion；不无条件扩写所有问题，先判断改写是否保持原始信息需求。
- 关键负面证据：摘要明确指出改写相关性不足会使答案偏题；更多查询不是单调增益。

### P16. Evaluating RAG-Fusion with RAGElo: an Automated Elo-based Framework — 2024

- 原文：[Rackauckas、Câmara、Zavrel，arXiv](https://arxiv.org/abs/2406.14783)，LLM4Eval @ SIGIR 2024。
- 一句话机制：合成与真实领域查询、LLM 文档/答案评分及 Elo 竞赛，结合专家标注比较 RAG 变体。
- 数字/口径：论文报告与专家评分为中等正相关；RAG-Fusion **completeness 更好但 precision 更差**，不将其合并为单一「质量更好」。
- 对 WRR 的含义 `[推断]`：可借鉴成对比较和专家抽检；不让 Jev 自己生成测试题、自己打分、自己宣布成功而没有独立人工锚点。
- 不适用点：Elo 是相对排序，不给出绝对质量门的通过阈值。

### P17. Agentic Retrieval-Augmented Generation: A Survey on Agentic RAG — 2025

- 原文：[Singh et al.，arXiv](https://arxiv.org/abs/2501.09136)。
- 一句话机制：按 Agent 数量、控制结构、自主性、知识组织等维度归纳动态检索中的规划、反思、工具调用与协作。
- 数字/口径：综述，不把框架示意图视为实验胜出证据。
- 对 WRR 的含义 `[推断]`：可借鉴控制循环与 retrieval tool 解耦；不因「Agentic」标签就引入多 Agent，更不把搜索路由器直接升级成完整写报告 Agent。
- 文献边界：本文用该综述和 P10–P16 覆盖「query routing in RAG」方向；**未核验到一个公认且唯一同名的《Query Routing in RAG》综述**，不编造书目。

## 4. 工具/来源选择与 LLM 路由：能执行、会选择、知道何时升级

### P18. Toolformer: Language Models Can Teach Themselves to Use Tools — 2023

- 原文：[Schick et al.，arXiv](https://arxiv.org/abs/2302.04761)。
- 一句话机制：用少量 API 示范产生并筛选自监督工具调用样本，使模型学习何时调用、调用哪个工具、传什么参数及如何利用结果。
- 数字/口径：包含计算器、QA、**两个搜索引擎**、翻译与日历；不摘统一提升率。
- 对 WRR 的含义 `[推断]`：可借鉴「工具有用性以实际任务收益衡量」；不照搬 next-token loss 改善作为 WRR 搜索质量目标，也不要求宿主微调基础模型。
- 证据边界：Toolformer 不是本机发现协议，不解决未知 CLI 的权限与 schema 发现。

### P19. Gorilla: Large Language Model Connected with Massive APIs — 2023

- 原文：[Patil et al.，arXiv](https://arxiv.org/abs/2305.15334)。
- 一句话机制：API 文档检索配合微调模型，减少调用幻觉，并在测试时文档更新后适应 API 变化。
- 数字/口径：APIBench 覆盖 HuggingFace、TorchHub、TensorHub；本文不摘跨平台汇总准确率。
- 对 WRR 的含义 `[推断]`：可借鉴从实时帮助/schema 构建可执行描述；不让 Jev 仅凭自然语言名直接拼接 shell，也不让来源说明覆盖宿主执行政策。
- 不适用点：文档中存在 API，不等于本机已安装、已登录或账户可用。

### P20. ToolLLM: Facilitating Large Language Models to Master 16000+ Real-world APIs — 2023

- 原文：[Qin et al.，arXiv](https://arxiv.org/abs/2307.16789)。
- 一句话机制：ToolBench 构建 API 指令与调用路径，神经 API retriever 推荐候选，DFS-based decision tree 探索方案，ToolEval 测工具使用。
- 数字/口径：**16,464 APIs、49 categories**；不要误写成「4,949 类」，这是搜索摘要中容易发生的数字混淆。
- 对 WRR 的含义 `[推断]`：可借鉴候选召回与最终执行分离、记录完整调用轨迹；不照搬为通用 API 解题设计的深度树搜索到每次简单搜索。
- 不适用点：外部 API 活性变化会使历史 benchmark 的通过率失真，必须区分模型失败与环境失败。

### P21. AnyTool: Self-Reflective, Hierarchical Agents for Large-Scale API Calls — 2024

- 原文：[Du、Wei、Zhang，arXiv](https://arxiv.org/abs/2402.04253)。
- 一句话机制：层次化 API retriever + solver + self-reflection，候选方案不可行时重新激活检索；同时修正会抬高 pass rate 的旧评测协议。
- 数字/口径：处理 **16,000+ APIs**；摘要报告相对 ToolLLM 的 average pass rate **+35.4%**，本文不擅自改写成百分点。
- 对 WRR 的含义 `[推断]`：可借鉴「失败反馈改变下一轮候选」与评估真实完成；不照搬假定工具池中必有解的前提。
- 不适用点：多层 Agent 检索对几十个本机搜索来源可能过重；先做平坦、可审计的候选选择。

### P22. Retrieval Models Aren't Tool-Savvy: Benchmarking Tool Retrieval for Large Language Models（ToolRet）— 2025

- 原文：[Shi et al.，arXiv](https://arxiv.org/abs/2503.01763)；[ACL Findings 2025](https://aclanthology.org/2025.findings-acl.1258/)。
- 一句话机制：单独评估大工具池中的 tool retrieval，证明普通 IR benchmark 强不代表工具召回强，且召回差会降低下游任务通过率。
- 数字/口径：**7.6k tasks、43k tools、6 类检索模型**，另提供 **200k+** 训练实例。
- 对 WRR 的含义 `[推断]`：可借鉴来源 shortlist 的 Recall@k 与下游有用率同时测；不把「描述 embedding 最近」当作充分的工具选择策略。
- 不适用点：WRR 来源少时不必先训练 tool retriever；需要先保留候选遗漏可观测性。

### P23. RouteLLM: Learning to Route LLMs with Preference Data — 2024

- 原文：[Ong et al.，arXiv](https://arxiv.org/abs/2406.18665)。
- 一句话机制：用人类偏好与数据增强训练 router，在强弱 LLM 间做质量—成本选择，并研究模型对替换后的迁移。
- 数字/口径：摘要报告部分设置下成本减少 **超过 2 倍**且质量不下降；不将其解释为所有任务成本减半。
- 对 WRR 的含义 `[推断]`：可借鉴 pairwise 来源比较标签和成本质量曲线；不把回答偏好预测直接等同于搜索源相关性预测。
- 不适用点：同一 query 的来源价值取决于本机实际返回结果、地区、时间与登录态，不能只用静态模型能力标签。

### P24. FrugalGPT: How to Use Large Language Models While Reducing Cost and Improving Performance — 2023

- 原文：[Chen、Zaharia、Zou，arXiv](https://arxiv.org/abs/2305.05176)。
- 一句话机制：学习 LLM cascade，结合回答质量估计决定是否接受当前答案或继续调用下一模型。
- 数字/口径：特定实验可在匹配最佳单模型效果时 **最多降低 98% 成本**，或同成本较 GPT-4 准确率提高 **4%**；不能迁移为 WRR 承诺。
- 对 WRR 的含义 `[推断]`：可借鉴检索后再决定升级的 cascade；不照搬固定「便宜优先」而牺牲用户指定一手/实时来源的要求。
- 关键差异：WRR 可以把继续动作限制为少数结构化选项，由确定性执行器落实预算。

### P25. Hybrid LLM: Cost-Efficient and Quality-Aware Query Routing — 2024

- 原文：[Ding et al.，arXiv](https://arxiv.org/abs/2404.14618)。
- 一句话机制：按预测 query difficulty 与目标质量在小/大模型间路由，测试时可调质量水平。
- 数字/口径：实验中大模型调用可 **最多减少 40%**，同时不降低回答质量；这是模型路由实验，而非 API 健康检查收益。
- 对 WRR 的含义 `[推断]`：可借鉴在质量约束内优化预算、把阈值与硬编码 mode 分开；不把便宜判官的自评分默认当校准后的正确概率。
- 不适用点：不同搜索来源不一定能按「弱→强」排成一条链，通常是覆盖域互补。

### P26. Doing More with Less: A Survey on Routing Strategies for Resource Optimisation in Large Language Model-Based Systems — 2025

- 原文：[Varangot-Reille et al.，arXiv](https://arxiv.org/abs/2502.00409)。部分版本标题为 *Doing More with Less — Implementing Routing Strategies ...: An Extended Survey*，引用时需附版本。
- 一句话机制：把路由形式化为质量与资源约束问题，讨论在流程何时路由、如何路由，以及非金钱成本。
- 数字/口径：综述，无单个系统的通用节省比例。
- 对 WRR 的含义 `[推断]`：可借鉴预算同时包括调用价、延迟、上下文与失败成本；不把模型选择、检索器选择、工具选择混用同一套标签和指标。
- 不适用点：目标函数框架可迁移，具体分类器与成本参数必须重新测量。

### P27. Dynamic Model Routing and Cascading for Efficient LLM Inference: A Survey — 2026

- 原文：[Moslem、Kelleher，arXiv](https://arxiv.org/abs/2603.04445)。
- 一句话机制：按「何时决策、使用什么信息、如何计算」组织 routing 与 cascading，强调实际系统往往组合多种策略。
- 数字/口径：综述，本文不摘其引用系统的二手数字。
- 对 WRR 的含义 `[推断]`：可借鉴把 pre-search 路由与 post-search 质量反馈作为同一策略的不同决策点；不因 Jev 已支持 typed judgment 就省略数据分布外与校准评估。
- 证据边界：这是独立 LLM 池的 inference survey，不是本机多协议搜索接入规范。

## 5. 搜索 Agent：主要证据在「何时搜、读什么、何时停」，不是多 provider 竞选

### P28. Search-o1: Agentic Search-Enhanced Large Reasoning Models — 2025

- 原文：[Li et al.，arXiv](https://arxiv.org/abs/2501.05366)；[方法第 4.3 节](https://arxiv.org/html/2501.05366)。
- 一句话机制：推理中发现知识缺口时动态检索，独立 Reason-in-Documents 模块处理冗长页面后再注入主推理链。
- 数字/口径：实现使用 **Bing top-10 + Jina Reader**；涵盖 6 个 open-domain QA benchmarks，不能把它说成在多个搜索 provider 间学习路由。
- 对 WRR 的含义 `[推断]`：可借鉴检索与页面证据压缩分离、保留检索意图；不把完整长推理链交给便宜判官，也不照搬无最终答案时回退纯模型回答的策略来伪装搜索成功。
- 关键边界：这是 inference-time 编排；不要把其收益误写为 Search-o1 自己进行了 RL 训练。

### P29. Search-R1: Training LLMs to Reason and Leverage Search Engines with Reinforcement Learning — 2025

- 原文：[Jin et al.，arXiv](https://arxiv.org/abs/2503.09516)；[实验/终止机制](https://arxiv.org/html/2503.09516)。
- 一句话机制：RL 优化多轮 reasoning/search 轨迹，mask 检索内容 token 的训练损失，以结果奖励学习查询与检索时机。
- 数字/口径：7 个 QA datasets；摘要报告 Qwen2.5-7B / 3B 相对 RAG baselines 改善 **41% / 20%**；主实验为 **2018 Wikipedia + E5，top-3 passages**。
- 对 WRR 的含义 `[推断]`：可借鉴将搜索结果视为外部 observation、让下一动作依赖实际结果；不声称其主实验已验证 Exa/Brave/CLI/MCP 跨 provider 决策。
- 停止机制：达到最大 action 数或模型输出最终答案；这不等于经过外部独立证据充分性验证。

### P30. WebThinker: Empowering Large Reasoning Models with Deep Research Capability — 2025

- 原文：[Li et al.，arXiv](https://arxiv.org/abs/2504.21776)；[附录 A.2.1](https://arxiv.org/html/2504.21776v2)。
- 一句话机制：Deep Web Explorer 执行搜索、网页导航与信息抽取，主模型交错 think/search/draft，并以 iterative online DPO 改善工具使用。
- 数字/口径：问题求解路径取 **top-10** 网页；评估含 GPQA、GAIA、WebWalkerQA、HLE 与报告任务，本文不摘统一平均分。
- 对 WRR 的含义 `[推断]`：可借鉴让来源返回面向检索目标的证据，并区别 search 与 page read；不照搬自主写报告功能进入宿主无关搜索库。
- 停止机制：相关推理路径到 EOS 或 token 上限；未因此证明覆盖了所有必须回答的子问题。

### P31. DeepResearcher: Scaling Deep Research via Reinforcement Learning in Real-world Environments — 2025

- 原文：[Zheng et al.，arXiv](https://arxiv.org/abs/2504.03160)；[Web Search Tool / Web Browsing Agent 方法节](https://arxiv.org/html/2504.03160)。
- 一句话机制：在真实 Web 环境端到端 RL，搜索结果有结构化 title/URL/snippet，浏览 Agent 带短期记忆并逐段决定继续读还是停止。
- 数字/口径：相对 prompt-engineering baselines 最多 **+28.9 points**，相对 RAG-based RL 最多 **+7.2 points**；搜索 top-k 在实现中仍固定，例如 10。
- 对 WRR 的含义 `[推断]`：可借鉴真实环境故障/噪声测试、证据记忆与跨来源核对；不把论文观察到的 emergent cross-validation 当作一个确定性完整性保证。
- 停止机制：主模型认为信息充分时生成最终回答；浏览器子 Agent 的停止与研究任务整体停止是两层决定。

### P32. WebSailor: Navigating Super-human Reasoning for Web Agent — 2025

- 原文：[Li et al.，arXiv](https://arxiv.org/abs/2507.02592)；[第 2 节与附录 A.1](https://arxiv.org/html/2507.02592v1)。
- 一句话机制：高不确定性任务合成、RFT cold start、DUPO agentic RL，使 ReAct Agent 学习通过 search/visit 逐步缩小信息不确定性。
- 数字/口径：search 调 **Google top-10**，visit 用 **Jina + Qwen2.5-72B** 摘要；原文 32B 模型 BrowseComp-en 为 **10.5**，72B 为 **12.0**，不能笼统说全面达到专有 DeepResearch。
- 对 WRR 的含义 `[推断]`：可借鉴把「缩小未解决问题」作为动作价值而非堆搜索次数；不照搬昂贵训练流水线，也不将相同 Google 的多查询误算为多来源交叉验证。
- 停止机制：action space 包括 search、visit、final answer；选 final answer 即终止，不是独立校准的充分性阈值。

### 5.1 对 WRR 的归纳

- `[推断]` 这些研究支持 **search/query、visit/read、finish** 至少三种动作分开建模，而不是每次都无差别多源 fan-out。[依据：WebSailor 方法节](https://arxiv.org/html/2507.02592v1)
- `[推断]` 论文中的「选择来源」常指选择网页或固定 retriever 内的文档，不一定是从 Exa、Brave、Skill、CLI、MCP 中选一个；跨 provider 路由仍需 WRR 自己的离线/线上证据。[依据：Search-R1 实验设置](https://arxiv.org/html/2503.09516)、[Search-o1 实现](https://arxiv.org/html/2501.05366)
- `[推断]` 推荐宿主负责长程计划、WRR 负责有预算的检索与显式质量反馈；避免在 Hermes/OMP/Pi/Claude Code 内重复嵌套一套完整研究 Agent。[依据：Agentic RAG survey](https://arxiv.org/abs/2501.09136)

## 6. 质量验证、LLM 判官与检索内容安全

### P33. Predicting Query Performance（Clarity Score）— 2002

- 原文：[Cronen-Townsend、Zhou、Croft，SIGIR DOI](https://doi.org/10.1145/564376.564429)；[作者 PDF](https://ciir.cs.umass.edu/pubfiles/ir-250.pdf)。
- 一句话机制：用 query language model 与 collection language model 的相对熵衡量结果语言的一致性/歧义，预测平均检索效果。
- 数字/口径：原文在多组 TREC 集合分析 clarity 与 average precision 的相关性；不摘单一通用相关系数。
- 对 WRR 的含义 `[推断]`：可借鉴检索后观察结果是否聚焦；不把 clarity 当真实性，也不把经典 clarity 错称为无需初次检索的纯 pre-retrieval 指标。
- 适用边界：Web API 常不给 corpus statistics，照搬原公式的前提通常不满足。

### P34. Query Performance Prediction using Relevance Judgments Generated by Large Language Models（QPP-GenRE）— 2024

- 原文：[Meng et al.，SIGIR 2024 / arXiv](https://arxiv.org/abs/2404.01012)；[方法及实验说明](https://arxiv.org/html/2404.01012v1)。
- 一句话机制：先让 LLM 独立判断 top-n query-document relevance，再用 pseudo-labels 计算所需 IR 指标，而不是直接猜一个总质量分。
- 数字/口径：TREC-DL **2019–2022**；top-10 judging 已可达到强 QPP，约 **100–200** 深度趋于饱和；原文指出直接让 LLM 输出指标值明显更差。
- 对 WRR 的含义 `[推断]`：这是把 Jev 批量结果判断转成质量门最直接的理论依据；不把仅有 top-k 的 proxy 当真实 Recall/nDCG，尤其不能拿未知全集的理想排序做归一化。
- 重要限制：论文发现零/少样本 LLaMA 判断不足，并采用人工相关性标签微调；**不能据此直接推定零样本 Jev 同样可靠**。

### P35. Is ChatGPT Good at Search? Investigating Large Language Models as Re-Ranking Agents（RankGPT）— 2023

- 原文：[Sun et al.，arXiv](https://arxiv.org/abs/2304.09542)；[EMNLP 2023 PDF](https://aclanthology.org/2023.emnlp-main.923.pdf)。
- 一句话机制：通过指令让 LLM 输出候选 passage 排列，使用 listwise / sliding-window reranking，并以 permutation distillation 蒸馏。
- 数字/口径：摘要报告 **440M** distilled model 在 BEIR 上优于 **3B** supervised model；不同版本的规模表述需以引用版本为准。
- 对 WRR 的含义 `[推断]`：可借鉴 rerank 不必依赖不同来源的原始分数；不要求 typed judge 直接完成长 permutation，先做 pointwise 筛选再保留 RRF 基线更容易校准。
- 不适用点：相关性重排仍不是 claim verification；来源可信、时间有效、支持关系应另判。

### P36. Judging LLM-as-a-Judge with MT-Bench and Chatbot Arena — 2023

- 原文：[Zheng et al.，arXiv](https://arxiv.org/abs/2306.05685)。
- 一句话机制：用强 LLM 近似人类开放回答偏好，系统分析 position、verbosity、self-enhancement biases 与推理限制。
- 数字/口径：GPT-4 judge 与人类偏好 **超过 80% agreement**；公开约 **3K expert votes、30K preference conversations**。
- 对 WRR 的含义 `[推断]`：可借鉴明确 rubric、独立人工对照与偏差分析；不把「80% 人类偏好一致」改写成「事实核验准确率 80%」或「Jev 准确率 80%」。
- 不适用点：chat answer preference 与搜索文档 usefulness / source selection 的任务分布不同。

### P37. Large Language Models are not Fair Evaluators（FairEval）— 2023

- 原文：[Wang et al.，arXiv](https://arxiv.org/abs/2305.17926)。
- 一句话机制：改变候选位置即可改变评判结果；通过 balanced position calibration、多证据与人工介入缓解。
- 数字/口径：特定排列操纵下，ChatGPT evaluator 可让 Vicuna-13B 在 **80 个问题中的 66 个**胜过 ChatGPT；不是 Vicuna 实际能力的无偏估计。
- 对 WRR 的含义 `[推断]`：可借鉴交换顺序复测校准、来源名盲化与不确定案例复审；不把 score/bool/choice 的 schema 合法性误认成评判无偏。
- 适用边界：独立逐条 judging 可减少某些比较位置效应，但不会自动消除长度、品牌与证据表达偏差。

### P38. Long-form Factuality in Large Language Models（SAFE）— 2024

- 原文：[Wei et al.，arXiv](https://arxiv.org/abs/2403.18802)。
- 一句话机制：分解长回答为单个事实，对每个事实多步 Google search 与支持性判断，使用考虑用户期望长度的聚合指标。
- 数字/口径：约 **16k facts** 上与众包标注一致 **72%**；随机 **100 个分歧案例**中 SAFE 获胜 **76%**；报告比人工便宜 **20+ 倍**。
- 对 WRR 的含义 `[推断]`：可借鉴 claim→query→evidence→supported 的链；不把「SAFE 胜过部分众包标注」解读成无误，更不把长度偏好项当真实世界全部事实的 recall。
- 不适用点：完整 SAFE 可多次搜索每个事实，应该作为宿主可选核验流程，不必每次 WRR 搜索强制执行。

### P39. FActScore: Fine-grained Atomic Evaluation of Factual Precision in Long Form Text Generation — 2023

- 原文：[Min et al.，arXiv](https://arxiv.org/abs/2305.14251)。
- 一句话机制：把生成文本拆成 atomic facts，计算可靠知识源支持的事实比例，避免混合真假长文被一个 bool 掩盖。
- 数字/口径：原始人物传记实验中 ChatGPT FActScore **58%**；自动估计器误差 **小于 2%**；这不是每条事实检测准确率 98%。
- 对 WRR 的含义 `[推断]`：可借鉴分开判「文档相关」与「某个声明有证据支持」；不把 URL 出现、域名权威或搜索结果摘要复述当作已经完成 entailment。
- 不适用点：某可靠知识源支持该说法不等于其永远正确；时间、版本、冲突证据仍需记录。

### P40. RARR: Researching and Revising What Language Models Say, Using Language Models — 2022 / ACL 2023

- 原文：[Gao et al.，arXiv，首次 2022](https://arxiv.org/abs/2210.08726)；[ACL 2023](https://aclanthology.org/2023.acl-long.910/)。不能按某聚合站抓取日期误标首次预印本年份。
- 一句话机制：对既有生成内容补做证据检索与 attribution，并最小化修改地修正未支持的内容。
- 数字/口径：本文不摘未逐表核验的归因增益；原文强调只需少量示例、LLM 与标准 Web search。
- 对 WRR 的含义 `[推断]`：可借鉴返回未支持声明与证据缺口，供宿主定向补搜；不在搜索库里悄悄改写用户答案或把重写后的流畅性当事实提升。
- 边界：RARR 是 attribution/revision，不是 RankGPT 的 reranking；搜索摘要中有混淆两者的风险。

### P41. BertPE: A BERT-Based Pre-retrieval Estimator for Query Performance Prediction — 2024

- 原文：[Khodabakhsh、Zarrinkalam、Arabzadeh，ECIR 2024 DOI](https://doi.org/10.1007/978-3-031-56063-7_27)；[出版摘要](https://dl.acm.org/doi/10.1007/978-3-031-56063-7_27)。
- 一句话机制：通过微调 query 的 contextualized BERT embeddings 学习检索性能，配合 synthetic relevance judgments 处理不同稀疏程度的评测标签。
- 数字/口径：实验涉及 MS MARCO V1 的 **4 组 query sets**；本文不摘未逐表核验的相关系数。
- 对 WRR 的含义 `[推断]`：可借鉴尚未发起搜索时预测初始预算/困难度；不因为某 query 先验容易就跳过实际返回结果的 post-retrieval 质量门。
- 分类边界：**pre-retrieval QPP** 只用 query/预先可得语料特征；**post-retrieval QPP** 再用实际排名/文档。BertPE 属于前者，P33 clarity 与 P34 QPP-GenRE 属于后者。[分类依据](https://arxiv.org/html/2404.01012v1)

### 6.1 两项跨越质量与安全的必要补充

**FreshLLMs: Refreshing Large Language Models with Search Engine Augmentation — 2023**

- 原文：[Vu et al.，arXiv](https://arxiv.org/abs/2310.03214)；机制：FreshQA 覆盖快速变化知识、错误前提等，FreshPrompt 注入最新搜索证据，区分 correctness 与 hallucination。
- 数字：**50K+ 人工 judgments**；本文不摘会随数据版本变化的题目总数。
- 对 WRR 的含义 `[推断]`：把 freshness、query premise、证据顺序作为独立测试维度；不认为网页抓取时间新就代表事实有效时间新，也不把 `today` 注入 prompt 当作新鲜度证明。

**Not What You've Signed Up For: Compromising Real-World LLM-Integrated Applications with Indirect Prompt Injection — 2023**

- 原文：[Greshake et al.，arXiv](https://arxiv.org/abs/2302.12173)；机制：攻击者在很可能被检索的数据里嵌入指令，越过 data/instruction 边界，控制行为、工具调用或外传数据。
- 数字：原文展示真实与合成应用攻击，本文不摘不存在统一口径的「通用检测成功率」。
- 对 WRR 的含义 `[推断]`：判官只接收隔离数据、结果不能扩权、执行器不信任内容中的命令；不把一个 injection=false label 当作完整安全防护。

> 计数说明：编号 P01–P41 为 41 条独立研究谱系；本节另外 2 篇，共 **43 篇**。P06 的 2002 前身不重复计数。

## 7. 对 WRR 重构的含义：Jev 应做可校准决策层，而非隐形总控

### 7.1 已有实现与文献的差距

- 已核验当前 [`wrr/search_filter.py`](../../wrr/search_filter.py#L1-L7) 自称 bounded、optional、selection-not-retrieval，并明确不是完整检测器；这与「路由、质量门、事实核验」三个新职责不能混为一谈。
- 已核验当前同一 state 下的 [`rel_i` / `inj_i` 两类问题](../../wrr/search_filter.py#L274-L287)，以及 [`_MAX_CANDIDATES=20`、片段长度上限](../../wrr/search_filter.py#L26-L31)；它们是有界片段筛选，不是全量网页质量验证。
- `[推断]` 可复用「一个共享证据 state，多个明确问题」的结构，但应分别记录 source choice、item usefulness、set sufficiency；QPP-GenRE 支持的是先有细粒度标签再聚合，而非一个万能分。[依据](https://arxiv.org/abs/2404.01012)
- `[推断]` typed judgment 只约束输出空间；实际可用源白名单、授权、参数校验、预算与执行边界应由确定性代码提供。论文没有验证 Jev，也没有授权模型替代这些运行时事实。[依据：Gorilla](https://arxiv.org/abs/2305.15334)、[Greshake et al.](https://arxiv.org/abs/2302.12173)

### 7.2 建议的最小判断对象

| 对象 | 适合的判断问题 `[推断]` | 不应声称的问题 | 学术依据 |
|---|---|---|---|
| query + available source descriptions | 哪个候选最可能提供该信息需求的证据？ | 哪个未调用来源此刻一定能成功？ | [CORI](https://doi.org/10.1145/215206.215328)、[ToolRet](https://arxiv.org/abs/2503.01763) |
| query + 单条原始结果 | 是否含具体、直接有用的信息？ | 仅看 snippet 就判断整页全部内容真实 | [QPP-GenRE](https://arxiv.org/abs/2404.01012) |
| query + evidence set + required facets | 哪个必要子问题尚无支持？ | 已有 5 条相关结果，所以任务完成 | [CRAG](https://arxiv.org/abs/2401.15884)、[SAFE](https://arxiv.org/abs/2403.18802) |
| claim + dated evidence passages | 现有证据是否支持/反驳/无法判断？ | 该声明在所有现实语境中绝对正确 | [FActScore](https://arxiv.org/abs/2305.14251)、[FreshLLMs](https://arxiv.org/abs/2310.03214) |
| last result + unresolved facets + feasible actions | 接受、换源、改写、读全文，哪步最有用？ | 判官输出任意工具/命令就直接执行 | [FrugalGPT](https://arxiv.org/abs/2305.05176)、[WebSailor](https://arxiv.org/abs/2507.02592) |

`[推断]` 这里描述的是判断语义，不是 Jev API schema；具体 `choice/bool/score`、batch 行为和宿主模型角色应由单独的接入研究核验。不要把旧实现的 `noul` 请求样式自动当作所有宿主的稳定公共 API。[本地现有请求形态](../../wrr/search_filter.py#L274-L292)

## 8. 评估设计：防止路由器自证正确

以下为 `[推断]` 的实验建议，本任务未运行任何实验。

1. **分开建标签**：source shortlist 是否漏掉有用源、返回结果是否有用、证据是否支持答案、任务最终是否完成，各有人工锚点；不能只看 API HTTP 200。[FedWeb](https://trec.nist.gov/pubs/trec22/papers/FEDERATED.OVERVIEW.pdf)、[ToolRet](https://arxiv.org/abs/2503.01763)
2. **保留便宜强基线**：固定最佳可用单源、简单静态路由、全可用源受限 fan-out + RRF、Jev 路由 + 相同 merge，分别比较；额外搜索与判官成本必须计入。[RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)、[Routing survey](https://arxiv.org/abs/2502.00409)
3. **校准 judge 而非只校验 JSON**：人工 relevance 集测 false positive/false negative；候选换序、隐藏 provider 名、长短摘要配对；分别评估中文、时效、技术文档、社区内容。[FairEval](https://arxiv.org/abs/2305.17926)、[Zheng et al.](https://arxiv.org/abs/2306.05685)
4. **分开未知与失败**：未判、片段截断、不可访问、登录失效、质量差、证据冲突分别记录；未知不能直接累计为质量通过。[当前筛选显式边界](../../wrr/search_filter.py#L1-L7)、[CRAG](https://arxiv.org/abs/2401.15884)
5. **评估实际环境**：做来源缺失、索引重叠、陈旧内容、错误前提、恶意片段的可回放任务；既看平均分，也看失败类型。[DeepResearcher](https://arxiv.org/abs/2504.03160)、[FreshLLMs](https://arxiv.org/abs/2310.03214)、[Greshake et al.](https://arxiv.org/abs/2302.12173)

| 层 | 优先指标 `[推断]` | 限制与来源 |
|---|---|---|
| 资源选择 | source Recall@k、top-source utility、相对 oracle 的可获得收益损失 | oracle 仅限已回放来源，不能说覆盖全网；[ReDDE](https://www.cs.cmu.edu/~lsi/Sigir_2003.pdf) |
| 合并排序 | 人工标注 nDCG@k、P@k / MRR、重复惩罚后有效结果数 | source nDCG 与 document nDCG 是不同对象；[FedWeb](https://trec.nist.gov/pubs/trec22/papers/FEDERATED.OVERVIEW.pdf) |
| 在线质量代理 | judged useful fraction、必要 facets 的证据覆盖、未判比例 | 这些是代理，不命名为真实 Recall；[QPP-GenRE](https://arxiv.org/abs/2404.01012) |
| 判官可靠性 | 与人工一致性、误拒/误收、位置稳定性、分域校准 | 不从 MT-Bench 搬来 80% 的预期；[Zheng et al.](https://arxiv.org/abs/2306.05685) |
| 事实/安全/成本 | claim support precision、freshness 合规、注入任务影响、总成本与 p95 latency | 安全率与相关性不合并；[FActScore](https://arxiv.org/abs/2305.14251)、[Greshake et al.](https://arxiv.org/abs/2302.12173)、[Routing survey](https://arxiv.org/abs/2502.00409) |

## 9. 关键发现 Top 5

1. **核心骨架不是新问题：保留资源描述、选源、结果合并的分层，替换决策实现，而不是一切压进一个 prompt。** `[推断]` Jev 可替换关键词决策，但不能替代来源契约与执行事实。[Federated Search](https://doi.org/10.1561/1500000010)、[CORI](https://doi.org/10.1145/215206.215328)
2. **最值得迁移的是 CRAG + QPP-GenRE：先逐条判有用性，再判是否补搜。** 直接让判官猜总分缺乏可诊断性；零样本 Jev 的可靠性仍须验证。[CRAG](https://arxiv.org/abs/2401.15884)、[QPP-GenRE](https://arxiv.org/abs/2404.01012)
3. **RRF 适合作为稳定底座，不该因引入 Jev 就删掉。** 但必须控制重复内容与共享后端投票，且 relevance rerank 不能冒充事实核验。[RRF](https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf)、[FedWeb](https://trec.nist.gov/pubs/trec22/papers/FEDERATED.OVERVIEW.pdf)
4. **大多数 Search Agent 论文没有证明多 provider 自动选源。** 它们更强的证据是动态查询、搜索/浏览分工与轨迹训练；`final answer` 终止也不等于证据充分性保证。[Search-R1](https://arxiv.org/html/2503.09516)、[WebSailor](https://arxiv.org/html/2507.02592v1)
5. **typed judge 不消除偏差，也不能充当安全边界。** 需要独立人工校准、证据支持关系与确定性权限隔离；不同质量维度应显式分开。[FairEval](https://arxiv.org/abs/2305.17926)、[FActScore](https://arxiv.org/abs/2305.14251)、[Greshake et al.](https://arxiv.org/abs/2302.12173)

## 10. 仍需确认的问题

1. **Jev 的任务内可靠性**：`jev-latest` 对中文来源选择、截断片段、事实支持与注入识别的实际误差和稳定性是多少？现有论文不能代答。[需要本地人工校准；相关方法：QPP-GenRE](https://arxiv.org/abs/2404.01012)
2. **来源是否真有互补性**：Exa、宿主 web role、MCP、CLI 入口是否最终共享引擎或同一批页面？若不去重，RRF 及「多源一致」可能被高估。[需实测；依据：FedWeb](https://trec.nist.gov/pubs/trec22/papers/FEDERATED.OVERVIEW.pdf)
3. **停止标准服务哪个产物**：WRR 只交付搜索结果，还是承担答案充分性？若没有宿主提供必要 facets/claims，路由器无法严格判断整项研究已经完成。[架构契约待定；依据：SAFE](https://arxiv.org/abs/2403.18802)
4. **经验画像如何衰减**：登录态、地区、计费与索引变动后，多少历史记录仍可用于选源？声明能力、实时可用性与经验质量需要不同有效期。[待实测；依据：Query-based sampling](https://doi.org/10.1145/382979.383040)、[Gorilla](https://arxiv.org/abs/2305.15334)
5. **独立评估集从哪里来**：应采样真实 Hermes/OMP/Pi/Claude Code 搜索任务，并保留人评与时间快照，避免新路由器只在自己合成和自己裁判的任务上获胜。[设计建议；依据：RAGElo](https://arxiv.org/abs/2406.14783)、[FreshLLMs](https://arxiv.org/abs/2310.03214)
