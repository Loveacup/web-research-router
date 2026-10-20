# WRR 跨宿主能力协调与维护：工程设计对照

> **已被 [2026-10-19 WRR 7 设计稿](superpowers/specs/2026-10-19-wrr-core-design.md)取代。**
> 下文保留为历史工程对照。当前搜索、Jev 默认策略和删除范围以新设计及 [README](../README.md) 为准。

> **状态：设计提案，以下原始基线未实施；后续有限实现见增量节，不是发布说明或执行授权。**
> 用户已确认产品方向；具体协议、模块、选型及实施排期仍需设计审查。
> 本次对照源码基线：`22d65e75ce7aeff846e20c7f2d7a9335d12c876f`（README 标注版本 `6.1.2`）。后续代码变化须重新核验，不能把本表当实时健康报告。

## 文档权威与阅读入口

- **Obsidian（OB）L1 是需求权威，L2 是方案与完整能力／维护矩阵权威；L3 承接待实施阶段与验收。** 本文是可随仓库分发的工程对照，不另立第二份需求或方案正文。发生差异时先回到 OB L1/L2 裁决，再同步本表。
- 没有 OB 的读者也可通过本文的需求边界、相对源码链接和验收门理解工程影响。需要完整讨论时，在 OB 中检索 `WRR` / `Web Research Router` 的 L1、L2、L3、当前 CQI 与历史 v5 Provider 矩阵；浏览器候选来源还包括 vault 相对路径 `00-Inbox/调研_浏览器自动化工具清单_20260703.md`。历史清单不是当前执行证据。本文不依赖某台机器的私有绝对路径。
- 仓库现有能力从 [README](../README.md) 与发布说明读取；浏览器支线保持 [browser-harness 设计门](../references/browser-harness-fallback-design.md) 原有状态。本文是**新增设计支线／待排期**，不删除旧路线、不重写旧 campaign 状态，也不解冻 Stage C。
- 下文用 **Confirmed requirements（已确认需求）**、**Proposed design（建议方案）**、**Current implementation（当前实现）** 分离三类陈述。记录了方向，不等于批准了每项技术方案。

OB 项目相对根目录为 `20-Areas/20_技术项目/web-research-router（WRR）/`，对应正文：

- L1：`00_L1_需求基准线/WRR-需求基准线.md`。
- L2：`10_L2_共维实现方式/10_架构总纲/WRR-统一内核多宿主协调与维护方案.md`。
- L3：`20_L3_Agent执行层/STDD_项目控制面.md`。
- 路线：`30_派生路线图/ROADMAP_落地路线图.md`。

上述为 vault 相对路径，不依赖个人 home 目录；仓库读者无需具备该 vault 即可阅读下文工程对照。

## 1. Confirmed requirements — 已确认需求

| 主题 | 已确认边界 |
|---|---|
| 跨宿主 | WRR 面向不同 Agent CLI；OMP 为首要新增宿主，Hermes 适配不能中断或退化。 |
| 统一内核 | 一个业务内核，入口侧宿主薄适配、出口侧能力薄适配；不分别维护 OMP/Hermes 路由与质量逻辑。 |
| 能力协调 | WRR 成为搜索、网页获取工具与 skill 的协调层；WRR 优先，宿主内置能力保留为显式备用。 |
| 独立配置 | WRR 公共配置独立，宿主覆盖须显式；凭据按授权共享，不默认借用 Hermes 凭据。 |
| 灵活接入 | 可复用协议通过配置接入，非标准接口用薄 adapter；不承诺任意工具零代码即插即用。 |
| Skill 边界 | 首期推荐／转交给宿主执行，预留结果回传契约；不先做完整 Agent 编排。 |
| 可持续维护 | 协助下游工具／skill 更新及 WRR 自更新；维护契约与接入契约共同设计。 |

以上方向无需再次询问；后续审查聚焦尚未确定的工程决策。

## 2. Proposed design — 分层、协议与宿主兼容

建议路径为：**宿主薄适配 → 统一业务内核 → 能力执行 adapter**；维护流程独立于查询。

| 层 | 建议职责 | 边界 |
|---|---|---|
| 宿主 | 工具注册、身份与取消传递、结果呈现、获准 skill 交接 | 不复制路由、质量或 fallback 规则 |
| 内核 | 请求与配置解析、路由、质量判定、错误和备用策略 | 相同请求＋配置＋能力上下文具有一致契约与策略，不要求不同配置的联网结果相同 |
| 执行 adapter | 复用专业搜索、抓取、浏览器能力，规范结果 | 不重造浏览器；不从 manifest 任意执行 shell |
| 维护 | 来源与所有权、兼容验证、候选更新、切换及回退 | 不在普通 search/fetch 中自动安装、更新或重启 |

**OMP 优先路径**：native extension `pi.registerTool` → WRR CLI JSON。`integrations/omp.mjs` 已实现进程级显式加载；新会话已跑通 Exa search → fetch、三工具注册/错误回传、取消及卸载。CLI/Hermes search 共用已有 v5 mode router；这不代表 Hermes 受保护 campaign 所在默认 profile 已做新会话验收，也不代表完整 MH-01/03 通过。MCP 仍按具名协议合同补充，不造中央 daemon。

**Hermes 兼容门**：保留 `web_search` / `web_fetch` / `web_similar` 合同与既有行为基线。默认值映射、错误和输出变更须单独评审；不能靠统一入口悄悄改变原有行为。Stage S 的 shadow／legacy 执行边界继续保留，Stage C 仍需独立审批；这与 README 中已发布的 v6 descriptor 路由默认开关不是同一个切换门。Campaign 不是 OMP 接入的前置条件。

### Search / fetch / skill handoff 契约

三种操作分别定义内容合同，共享 request identity、调用链、deadline/cancel、预算、错误分类、诊断和证据：

- **Search**：条目、来源 URL、执行者、摘要与搜索诊断。摘要是线索，不能冒充已获取正文。
- **Fetch**：正文及内容类型、来源 URL、获取时间、提取方式、完整性／截断、挑战页及权限需求。HTTP 200 不证明正文有效。
- **Skill handoff**：推荐技能、理由、输入、权限需求、预期产物及回传关联标识；由宿主决定并执行。推荐／加载不等于授权运行技能包中的任意命令。
- 执行成功、`partial`、质量达标分别表达；空正文、挑战页与截断保留明确缺口，质量阈值是可审查策略而非客观事实。
- 取消传至内核、adapter 与子进程；不能取消远端工作时如实报告，取消后不继续 fallback。
- fallback 公开原因、实际执行者及质量变化；宿主备用必须指向明确的原生工具，用调用链标识、禁止回入与跳数限制防止 WRR → host → WRR 递归。

**抓取安全建议**：默认阻断本地、私网、链路本地及元数据地址；校验协议、DNS、每次重定向并防 DNS 重绑定。合法内网按显式授权开放，授权机制待审查。继续遵守已有浏览器支线的域名、登录与凭据边界。

## 3. Proposed design — 来源分类与能力矩阵

能力目录应分别记录 `discovered` / `configured` / `authorized` / `routable` / `executed`；发现、配置成功甚至健康检查通过，都不等于本次已获授权或已执行。每条记录还需来源、能力、协议、owner、版本／pin、健康与兼容证据。Skill 是宿主方法，不是可执行 engine；Python adapter 的加载本身也可能执行代码，须受来源信任控制。

下表是从讨论清单到工程入口的对照，**不是已集成名单或实时 readiness 表**；完整矩阵由 OB L2 维护。

| 来源类别 | 工具／来源 | 当前状态或依据 | 建议角色与验收缺口 |
|---|---|---|---|
| 公共网页搜索 | Exa、Brave、SearXNG | README 已有搜索引擎底座；本次未跑线上调用 | 复用搜索链，逐源验证 search/fetch 合同与权限 |
| 公共搜索候选底座 | Tavily | 讨论列为执行链候选；本次未确认默认注册或生产可路由状态 | 核验代码、配置与执行证据后入目录，不补写为已集成 |
| 开发者／学术 | GitHub、Academic（OpenAlex / Semantic Scholar / arXiv） | README 已列能力；本次未跑线上调用 | 保留专业来源及对应质量信号 |
| 社区平台 | Community（OpenCLI） | 已有链路依赖 binary＋daemon＋browser extension bridge | 逐来源验证，不把桥接存在当各站点可用 |
| 本地知识 | Supermemory、Session、QMD、Obsidian | README 已列本地引擎 | 保留本地检索；宿主依赖与访问授权显式化 |
| 专业正文获取 | Firecrawl | 优先评估候选，非本轮已接入 | 代表性正文、挑战页、截断及权限测试后再选型 |
| 抓取／浏览器候选 | Scrapling、Crawl4AI、Browser-Use、browser-harness | 候选；browser-harness 只有已提交 seam／禁用 scaffold，未接真实浏览器 | 按缺口选代表工具，不一次接全，不重造浏览器 |
| 历史／CQI 专项来源 | weixin-search-mcp / Sogou、last30days、xhs-crawler、视频分析 | 历史清单需逐项核验；last30days 有已有 adapter 文档，不代表当前运行可用 | 复核入口与宿主交接；视频分析不能用旧 `bilibili-video-analyzer` 名称冒充当前入口 |
| 维护技能 | opencli-adapter-author / autofix | 维护候选／方法，不是普通查询热路径 | 由宿主在授权维护阶段调用，验证产物后切换 |
| 来源／诊断参考 | Agent-Reach | README 明确为 provenance / diagnostic reference | 不列必需 runtime，不要求导入其代码 |
| 设计参考 | AnySearch / free-search 等 | 参考而非已接入实现 | 借鉴协议或治理设计，不计入执行覆盖率 |

接入合同与维护合同同时记录；可复用协议仍要通过能力、权限、健康和兼容门，manifest 不能成为任意 shell DSL。

## 4. Proposed design — 独立配置与凭据

普通设置优先级建议：**请求覆盖 → 显式宿主覆盖 → WRR 公共配置 → 默认值**。这是拟议规则，尚非全路径现状。权限是上限，请求覆盖不能提权；凭据以引用和授权共享处理，日志不输出秘密。

需要设计版本化配置及宿主默认映射，移除对 Hermes 私有环境的隐式依赖，同时保留经审查的兼容迁移路径。具体配置路径、字段、秘密后端、旧凭据迁移与弃用窗口均待决；本文不提供可直接部署的未来配置。

## 5. Proposed design — 维护与自更新

### 所有权与维护合同

owner 分为 **WRR 私有／宿主管理／外部共享**。WRR 可协助后两类更新，但须所有者授权、影响宿主确认及兼容验证；“非私有”不等于永久不支持。调用原生包管理器或已有安装机制，不另造包管理器。

维护一期优先修正明确目标选择、trust、ownership、dirty 保护、pin 与结果报告真实性；search/fetch 验收不能代替 `update --apply` 安全验收。

### 建议状态机与门

分别报告 `available` / `downloaded` / `installed` / `verified` / `activated`；fetch 成功不等于工作树升级，安装完成也不等于已验证激活。

1. 检查来源、owner、影响范围、版本 pin／摘要和本地修改；先给出维护计划及权限需求。
2. 隔离准备候选版本。候选测试本身可能执行第三方代码，须隔离权限与凭据，不能把“测试”当作免授权路径。
3. 审查权限和内容变化，分别完成 OMP、Hermes 兼容验收；skill 指纹覆盖 `SKILL.md`、scripts、references **整个包**。
4. 对上游基线、本地修改和候选版本做三方比较；保留 dirty 差异，禁止覆盖本地修改。
5. 获准后切换，读取实际版本／配置／目标状态验证效果；失败则按预先验证的恢复入口处置。

**自更新建议**：由外部监督者负责切换，运行中 WRR 实例不覆盖自身。代码、配置、状态格式、外部共享资源分别设计回退；远端服务、凭据变更或不可逆迁移不能承诺自动完整回滚。

维护路径与查询路径隔离；doctor 不因新增维护能力变成自动更新器。现有显式 deep doctor 的有限恢复规则仍按现有合同执行，本提案不扩大该权限。

## 6. Current implementation — 真实差距与证据

本表是基线代码／文档的静态对照，**不是 E2E、维护安全认证或线上健康证明**。下列源码相对链接可直接从仓库打开；行号仅适用于本次基线。

| 领域 | 已知实现证据 | 与提案的差距／后续验证 |
|---|---|---|
| CLI 凭据 | [`wrr/_cli.py`](../wrr/_cli.py) 36–71：候选含 `~/.hermes/.env`，加载首个存在文件 | 仍有宿主耦合；独立配置与授权共享需要迁移设计 |
| 统一入口 | 同文件 147–154：特定搜索条件调用 `route_search_v5`；README 单列 Hermes descriptor 路径与 CLI migration gate | 先核对默认策略与兼容语义；共享仓库／包不等于统一业务路径 |
| 正文提取 | [`wrr/engines/brave.py`](../wrr/engines/brave.py) 53–62：HTTP 请求、剥标签、折叠空白、截断 | 不能当作完整专业正文能力；挑战页、完整性及 SSRF 合同待补证 |
| Manifest 与 adapter | [`loader.py`](../wrr/engines/loader.py)、[`adapter_bridge.py`](../wrr/engines/adapter_bridge.py)：发现与 Python `module:Class` 桥，现有加载授权门 | 处于兼容迁移体系；manifest 存在不等于任意工具生产自动接入，不意味着完整能力协调目录已实现 |
| Skill | 讨论审计记录为寻找 `SKILL.md` 的 discovery | 不构成安装／执行／更新系统；完整宿主 handoff 与回传未在本轮验证 |
| OMP | README `--profile-matrix` 包含 `omp`，属于 control-plane readiness | 不等于 native extension 或真实 search → fetch 已验收；本轮未运行 OMP |
| 更新信任 | [`wrr/cli/update.py`](../wrr/cli/update.py) 110–124：trust 检查主要约束 clone | 已有目录 refresh 需同等信任与 owner 边界验证 |
| Pin 与更新报告 | 同文件 150–154：clone 未落实 pin；已有目录 fetch 后仅有 pin 才 checkout | 无 pin fetch 不代表工作树升级；状态机与固定版本验收尚有缺口 |
| 目标与 dirty | 同文件 168–181：env/path/default_path 可选已有目录 | 该执行器未验证所有权／本地修改；不能宣称共享更新安全 |
| Install 写入边界 | [`wrr/cli/install.py`](../wrr/cli/install.py) 57–62 定位 report-only；85–97 可按 `dry_run` 转交 refresh | 内部组合存在写入与报告不一致风险，待专门回归复核；未执行任何更新来测试 |
| 浏览器 fallback | [既有设计门](../references/browser-harness-fallback-design.md) 标 Slice 1 已实现，真实浏览器未 wired | 不能把 seam 当作实际备用链，更不能扩大既有查询副作用边界 |

## 7. 分期验收与状态报告

以下 A–D 是**新增设计支线的建议实施阶段**，不是已有 Stage S / Stage C 的重命名。各阶段应记录版本、请求／配置／能力上下文、执行路径、产物位置及失败原因；契约测试、真实宿主调用、维护演练分别报告 **PASS / PARTIAL / NOT_RUN**。doctor 不替代 E2E。

| 阶段 | 交付与可裁决门 | 本轮状态 |
|---|---|---|
| A 文档／契约／基线 | 需求与方案分层；冻结合同前记录 Hermes 基线、默认差异与维护风险；文档审查不能代替运行基线 | PARTIAL：本次仅工程文档对照；契约冻结和运行基线 NOT_RUN |
| B 统一入口＋OMP | 新行为先 RED 再实现；相同上下文策略等价；OMP 真实 search → fetch，Hermes 三工具回归；默认配置、凭据隔离、取消、递归与备用测试 | NOT_RUN，待排期／实施授权 |
| C 抓取＋skill 交接 | 代表性抓取与宿主真实交接／回传；挑战页、partial、截断、摘要冒充正文、SSRF／重定向／DNS 重绑定及授权测试 | NOT_RUN，待排期／实施授权；不代表解冻已有 Stage C |
| D 维护＋自更新 | 先修 trust/owner/dirty/pin/报告；再验证候选失败隔离、共享安装授权、全包 skill 三方比较、双宿主切换回读与分层回退 | NOT_RUN，待排期／实施授权 |

维护验收还需证明：发现不能自动执行；取消不会继续备用；候选失败不改变运行版本；本地修改不被覆盖；未获授权不能切换共享资源；不可逆迁移在切换前明确提示恢复限制。既有 campaign、Stage C 审批和证据门保持原状。

## 8. 待决事项

- search/fetch/handoff 的具体 schema、错误码、request identity、取消与子进程生命周期协议。
- CLI 与 Hermes 默认值兼容映射、质量阈值、行为迁移及弃用窗口。
- Firecrawl 等代表工具的首期选型、协议复用范围、MCP 接入时机及支持矩阵。
- 宿主原生备用的显式授权粒度、递归防护实现、不能取消远端工作的报告合同。
- 内网访问授权策略、凭据共享范围、独立配置版本化与旧凭据迁移。
- 维护权限预设、owner 判定、共享资源影响确认、候选隔离方式和来源 pin／摘要验证。
- 外部自更新监督者、各层回退边界、不可逆迁移处理及各阶段排期。

这些是待设计审查的技术决策，不撤回已确认的统一内核、OMP 优先／Hermes 不退化、独立配置与可持续维护方向。

## 9. 2026-09-22 接手增量（非发布声明）

- L1 新增 **MH-07 / NR-01～NR-08**：由 WRR 与宿主既有技能暴露合同共同解决网络 skill 触发争抢，不新增路由 skill。职责、选择政策与行为矩阵只在 OB L2 的“网络 skill 触发协调设计”；本文不复制第二份权威正文。
- 普通博客直接总结的验收为一条正文采集路径，browser/delegation/新增研究文档均为零。Agent Reach 平台专用、Browser Use 交互专用、Firecrawl/Defuddle runtime 唯一默认及有理由替代、Research 产物/后台意图分离均纳入合同。此协调尚未实施；未修改任何 skill/runtime。
- 用户已选择先修已有 report-only 合同。`wrr/cli/install.py` 的依赖委派固定为 planning；新增真实本地 Git clone/refresh 防写回归。显式 updater 的 trust/owner/dirty/pin/状态问题仍开放，第 6 节为修复前静态基线，不得将该窄修复夸大为完整维护验收。
- 当前执行与结果仅见 OB `20_L3_Agent执行层/10_当前迭代/WRR-接手整理与滚动实施计划-20260922.md`，由 `STDD_项目控制面.md` 路由。第 6/7 节表格是首轮静态基线；现已完成 CLI 默认查询对齐、单文件凭据选择及 descriptor 热路径二次读取封口，OMP 进程级 adapter 有真实新会话证据。完整正文质量、Hermes 隔离新会话、技能协调及维护仍未验收，不进入 Stage C 或生产依赖更新。
- 第5轮维护一期将 updater 收紧为**显式授权、固定提交、独立 Git 候选准备**，原运行目标不再原地更新。当前接口和限制见 [README](../README.md)；权威决定仍在 OB L2，实态与失败记录在 L3。`downloaded` 与 pin 回读不等于消费者验证或 activation；外部监督、自更新、双宿主切换及回退尚未验收。第6节 clone/refresh 表保留为修复前历史基线，不作为当前行为说明。

## 10. 2026-09-24 第二周期增量（非发布声明）

- 证据完整性复核（EIR-01～03）经独立审查通过：第二周期父路线图重新准入有效；MH-02 真实 CLI→OMP adapter 字段绑定（`host_fallback.status=denied`、`reason=fetch_policy_denied`）只关闭 stub 自证问题，宿主原生 DNS/redirect 执行与完整 MH-02 仍 BLOCKED；MH-04 的宿主中介交接在源码上可达，但可达不等于技能执行。
- MH-04 skill 交接 successor 合同（H2）在第 3 次重生成后达到上限，结论 **HOLD，不进入 Build**；本仓源码未改，未运行 OMP、provider 或网络，未接触凭据。
  - 修订 2：合同审查 PASS_BUILD；安全审查 HOLD（6 项 P2），且不认同合同审查的 PASS：带凭据的 `wrr_search` 可无限重放；OMP 在 `--cwd` 生效前合并启动目录 `.env` 并加载上层 `AGENTS.md`；宿主版本未钉定（已装 18.2.11，审查依据 18.2.7 源码）；一次性凭据回执无 schema 且可重放；broker 凭据快照强制落盘；detached 的 WRR 子进程可在 OMP 退出后继续持有凭据。
  - 修订 3：补丁残留并行字段，合同审查 HOLD（5 项 P2：私有路径、prompt 字节、回执协议、私有面 allowlist、cancel 分派）；修订 3 的安全审查已取消，修订 2 六项问题的修复未经独立确认。
- 用户裁决：先回写文档，再开启 H3 新设计周期（重新计数，一次性整体重写合同，同时覆盖上述 6 项安全与 5 项合同问题）。真实运行另需凭据与宿主授权。完整 MH-04 与整体目标仍 BLOCKED。
- OB L2/L3 本轮回写暂未完成：本机当前会话对 Obsidian 目录无访问权限（macOS 隐私权限）。权限恢复后以 OB L2/L3 为准补写；本节仅为仓库侧工程对照，不作为第二份权威正文。

## 11. 当前交付收口增量（非发布、非执行授权）

本节对照 `e225ce17954537f894afd23c984b233ea4a54e52` 之上的当前工作树，
不是该提交已包含全部附件的声明。第 6/7 节保留历史基线，第 10 节的
**H2 HOLD 不改写为 PASS**。后续 H3-S 实现存在，不等于旧合同自动获准或真实宿主验收。
版本仍为 `6.1.2`，整体发布、既有 Stage C 与 campaign 门不变。

### 实现与行为迁移

- [OMP adapter](../integrations/omp.mjs) 已有 **MH-04 H3-S 实验合同**：
  绝对路径 `WRR_SKILL_HANDOFF_POLICY` 加载私有 schema-v1 policy；
  `skill_handoff=request` 限 `provider=skill`、`count=1`、无 mode。
  allowlist 匹配后推荐，accept 只给出精确 `skill://NAME` read 目标；
  同一 tool-call ID 的成功 read 与 `details.resolvedPath` 字符串哈希匹配后才能 report。
  该哈希不是技能内容／全包指纹；`received/declined/aborted` 是未验证宿主陈述，不证明技能执行。
- `WRR_SKILL_HANDOFF_ENFORCE=1` 才开启私有 tool-call gate：进程内一次精确 policy query、
  允许的交接转换及绑定 read，其他调用阻断；请求 enforce 但 policy 不可用则 fail closed。
  只有 policy 不等于 enforce。五分钟期限、32 条容量、至多一个 accepted/read-active 状态；
  session start/switch/branch/tree/shutdown 清理状态／timer／reservation 并取消在途 skill search，
  不重置进程级搜索额度。该 search 用指定解释器、过滤 env、fd 3 凭据与非 detached 子进程；
  不是通用 OS sandbox。上述生命周期与 enforce **尚未完成真实 OMP 验收**。
- [Skill discovery](../wrr/engines/skill_discovery.py) 已转向 query relevance＋出处＋frontmatter 完整度，
  不再沿用旧 license/recency 工程化质量评分；license 仍参与 frontmatter 完整度，不代表许可证核验或信任授权。
  [CLI env](../wrr/_cli.py) 使用进程值优先、`--env` / `WRR_ENV` / WRR 自有默认文件的独立选择，
  不隐式借用 Hermes 私有 env；OMP 另有过滤后的子进程环境。
- [Updater](../wrr/cli/update.py) 的固定提交候选仍是 `downloaded`、`activated=false`；
  候选准备不等于 activation、安装依赖或双宿主兼容验证。

### 有限监督器与分发边界

[Release supervisor](../wrr/cli/release_supervisor.py) 已存在，并非仍然全无实现：它是
checkout-only、POSIX、外部显式授权的有限 release selector，不是后台自动自更新。
[launcher](../scripts/wrr_release_supervisor) 使用 checkout `.venv/bin/python -I -S`；
安全说明入口为 `./scripts/wrr_release_supervisor --help`，另有 report-only 的
`wrr install --dry-run`。监督器本身**没有 dry-run/report-only 操作**，不传显式 apply 即拒绝。
本文不提供 activation payload，也不授予任何实际切换权限。

release identity 包含 path/origin/revision/version/tree digest；授权绑定 action、transaction、
有效期、输入摘要与允许写入集合。预检和 impact receipts 绑定候选，区分 `isolated_fixture` 与
`real_host`；不允许 config/state/shared-resource 变化或 migration，要求可回退。
activate 只给 `provisional`、`real_host_verified=false`。finalize 必须匹配当前 state-bound
activation receipt、release/config 身份及 transaction/generation 绑定的双宿主 post-switch PASS
receipts；fixture 只能 `finalized_fixture_only`，不能替代 `finalized_real_host`。
监督器校验外部证据，不自行运行宿主来证明 receipt 内容真实；不是任意迁移／共享资源回退保证。

[打包配置](../pyproject.toml) 的 pip 入口是 `wrr`，包发现为 `wrr*`；OMP JS adapter、监督 launcher
和隔离 bootstrap 是仓库附件，不是 pip console scripts/assets。Python 监督模块位于包内不等于
已分发完整 checkout 启动链。本轮未构建 wheel，不能将配置检查写成 wheel 实物验收。
`dev` 已补 `jsonschema`、`referencing`，`mcp` 仍 optional；`.[dev]` 与 `.[dev,mcp]` 必须分开报告，
skip 是 NOT_RUN 而非 PASS。已有定向 64 passed / 0 skipped 不等于新全量或双环境矩阵通过。

### 本地证据的有限覆盖与停止门

已有本地隔离检查覆盖真实 Hermes PluginManager/registry 的 override gate、ownership、unload/
reload（旧 handler 是 fixture sentinel，不是真实 builtin provider，也未启动模型）；真实 handler
经过受控 catalog subprocess 与 loopback HTTP 验证 search→fetch、精确 origin 授权不持久及取消回收。
OMP 侧仅真实 adapter＋CLI＋fake pi/zod，不证明真实宿主 schema normalization、dispatch、lifecycle
或 unload；未启用 H3-S policy。此方法说明不表示仓库已有可复现 JS 测试或永久验收工件。

当前增量的真实 OMP/live provider、similar 成功路径、H3-S 真实宿主 enforcement 均未验收；
有限本地证据不能继承历史 OMP 会话的 PASS。**Fetch handoff 的宿主原生 DNS/redirect 边界仍 BLOCKED**，
完整 MH-02/MH-04 和双宿主 activation 仍未完成。本次只同步两份文档，不重跑单测、不安装、
不联网、不读取凭据、不操作 OB、不激活服务、不更新版本或发布；下一步真实运行仍需独立授权。
