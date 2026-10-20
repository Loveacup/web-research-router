# WRR 重构：四个宿主的搜索与判官接入面

## 决策摘要

优先采用“统一内核 + 宿主薄适配”。宿主适配器提供可调用能力，内核负责候选选择、结果筛选和质量判定。[推断，依据：O1、O3、H2、P1、C1]

这里的“薄”指不复制宿主的凭据解析、模型目录和 fallback 规则。
适配器仍需处理宿主特有的取消、结果格式、授权和生命周期。[推断，依据：O1、O2、H1、P1、C2]

- **OMP**：能读取当前 `web`、`judge` 角色及 fallback 配置。能委托原生搜索，也有导出的 judge resolver；不能把 `web/exa` 当成普通 chat completion。[O1、O2、O3、O4]
- **Hermes**：新版已有 `WebSearchProvider` backend plugin。可优先注册 `wrr` backend，避免覆盖整个 `web_search` 工具；旧版才考虑现有 override 接法。[H1、H2、L2]
- **Pi**：有真正的 `classifier` 模型类型与 `modelRegistry.classify()`。可复用 Jev 凭据；未发现与 OMP 等价的 `web`/`judge` 命名角色。[P1、P2、P3]
- **Claude Code**：原生 WebSearch backend 不可配置，官方推荐额外 MCP 搜索工具。当前官方文档还新增了 mods，可截获工具并复用宿主模型调用；这仍不等于原生 Jev judge role。[C1、C2、C3、C4]

## 核验范围与证据等级

调研日期为 2026-10-19。报告区分三类证据：本机事实、已读取的一手文档/源码，以及标注为“[推断]”的架构含义。
没有执行搜索验收、安装依赖、运行测试或修改配置。

### 本机实际可见范围

- `which hermes pi omp` 只返回 `/opt/homebrew/bin/omp`；本次进程的 PATH 中没有 `hermes` 和 `pi`。[本机命令输出，2026-10-19]
- `realpath /opt/homebrew/bin/omp` 返回 `/opt/homebrew/Cellar/omp/18.12.0/bin/omp`。远端 OMP `main` 的 package.json 为 18.12.2，二者不能混用为同一个版本。[本机命令输出；O4]
- `~/.hermes/config.yaml:1–4` 只有 `vibe-island`、`orca-status` 的启用记录。目录存在不证明 Hermes CLI 或 Jev 已安装。[L4]
- `~/.pi/agent/` 存在数据库、sessions 和 extensions；未读取凭据表。配置目录存在也不证明当前 PATH 有可运行的 Pi。[本机目录：`/Users/alexcai/.pi/agent/`]
- 本机根目录、`code`、`.cache` 等限定路径的名称搜索没有定位到 Hermes core 源码。采用专用文件发现工具代替 shell `find`；未访问 macmini，也未把该结果写成“macmini 没有安装”。[本机文件发现结果]

### 固定源码版本

- OMP 上游源码固定到 `9d5d4ac5b50ac2bb5866e4202efc0cf785dfc57a`。[O4]
- Pi 上游源码固定到 `1402973a9dbe8c627341e04c9744c54953e285ca`。[P1]
- Hermes catalog 固定到 `5fc490e2b34123212d73ef316086f939567a725a`。[H4]
- 私有 `Loveacup/pi-jevflow` 固定到 `be91bb962899dac5bc92fcad99c3b350dcb6cccf`。通过已登录 `gh api` 读取，匿名 raw URL 返回 404；报告中的链接需要仓库权限。[P4]
- `omp://` 和 `cfg://` 是本次 OMP 安装提供的内部证据。`cfg://` 只读，下面仅记录必要键名与非敏感值。[O5]

## 1. OMP

### 1.1 当前 web role 与 fallback 的真实语义

**事实 OMP-1：搜索选择已经由模型角色控制。**
`cfg://modelRoles` 显示 `web: web/exa`、`judge: typesafe/jev-latest`，来源均为 global config。
`cfg://retry.fallbackChains` 的 `web` 是有序 selector 数组。
数组同时包含 `web/*` 引擎和带 grounding 的 chat 模型。[O5]

本机链包含以下引擎 selector，按其在原配置中的相对顺序列出：

```text
web/parallel, web/perplexity, web/zai, web/tinyfish,
web/jina, web/kagi, web/tavily, web/firecrawl, web/brave,
web/kimi, web/synthetic, web/ollama, web/searxng,
web/startpage, web/duckduckgo, web/ecosia,
web/google, web/mojeek, web/public
```

上述列表省略了链中穿插的 chat selector；不应把这份摘录用于重建完整执行链。[O5]

**事实 OMP-2：配置链不等于当前可执行链。**
`web_search` 先用 `roleCandidatePool("web", …)` 收集合格模型。
随后 `resolveRoleChain("web", …)` 解析角色和 fallback。
显式候选与自动候选采用不同的 availability 检查；缺少凭据的自动候选会跳过。[O1]

`modelRoles.web` 配置后，首个可匹配 selector 为 primary。
`retry.fallbackChains.web` 一旦设置，即使为空数组，也替换默认 fallback。
重复模型会去重。`web/hosted` 还会按当前 session model 展开。[O1]

**事实 OMP-3：原生搜索是顺序 fallback。**
每个 provider 返回第一个可接受结果后停止，不会自动执行跨 provider RRF 融合。
带 grounding 的 chat 回答必须有 sources 或 citations；纯 answer 会被拒绝。
部分 query constraints 若会滤空全部结果，宿主会放宽约束并附注说明。[O1]

因此，“调用 OMP web role”与“让 WRR 逐个调度 OMP 链里所有来源”是两种能力。
前者可以直接委托；后者需要单候选调用、availability 和 provenance 的明确合同。[推断，依据：O1]

### 1.2 `web/exa` 是什么

**事实 OMP-4：`web/*` 是目录里的 search kind。**
`web/exa` 以 model selector 形式出现在 `/model`，但执行路径是搜索 adapter。
OMP 同时接受 catalog 声明了 `webSearch` grounding 的 chat 模型。
`web-search` 是内建 API，不能在 `models.yml` 中当作任意自定义 chat transport 声明。[O1、O6]

Exa adapter 的两种执行路径：

| 条件 | 执行方式 | 输出 |
| --- | --- | --- |
| 存在 `EXA_API_KEY` 或 Exa stored credential | POST `https://api.exa.ai/search` | `answer`、`sources`、`requestId` |
| 没有 Exa 凭据 | JSON-RPC `tools/call` → `https://mcp.exa.ai/mcp`，工具 `web_search_exa` | 同一归一化结构 |

Exa `answer` 由最多 3 条结果 summary 合成；不能据此认为发生了额外 LLM synthesis。[O1]

**事实 OMP-5：搜索结果同时提供文本和结构化 details。**
工具返回 `content: [{type:"text", text:…}]`。
`details.response` 是统一的 `SearchResponse`；`details.error` 可携带失败。
文本会截短 snippet/citation，程序调用宜优先读取 details。[O1]

provider 不可用或全部失败时，工具通常返回 `Error: …` 文本与 error details，而非抛出异常。
取消在 fallback 内会重新抛出；适配器不能仅凭 Promise resolve 判定搜索成功。[O1]

### 1.3 扩展怎样读取并委托 web role

**事实 OMP-6：扩展可读取有效配置与当前模型。**
文档要求从 `@oh-my-pi/pi-coding-agent/config/registry` 导入 `lookup(id)`。
配置 handle 的 `get(pi.pi.settings)` 读取合并后的有效值。
字符串路径式 `settings.get/set/override` 已在 18.3 移除。[O2]

可读取的键包括：

```text
modelRoles
retry.fallbackChains
providers.webSearchTimeoutSeconds
providers.fetch
exa.enabled
exa.searchDelayMs
searxng.endpoint / token / basicUsername / basicPassword
searxng.categories / language / engines / safesearch
```

此处是配置访问面的列举，不建议把密码读取后传给 WRR Python。[O2、O5、O7]

扩展 `ctx.models.resolve("@web")` 可以解析角色；`ctx.modelRegistry` 可访问宿主 registry。
`ctx.models.current()` 会跟随用户的 `/model` 切换。
完整 fallback 解析应沿用宿主 resolver，不能只把 primary 与字符串数组拼接。[O2、O1]

**事实 OMP-7：同名原生工具有安全的委托入口。**
当扩展用 `pi.registerTool()` 重新注册 `web_search` 时，执行 context 可提供 `ctx.invokeTool(params, options)`。
该方法调用同名原生 built-in，默认继承 signal、进度和 provider metadata。
委托仅限同名工具；新增 `wrr_search` 不会获得任意调用 `web_search` 的权限。[O2]

因此，保留 `wrr_search` 名字并假定 `ctx.invokeTool` 能调用原生搜索，是错误设计。[推断，依据：O2]

源码另导出 `WebSearchTool`、`webSearchCustomTool` 和 `getSearchTools()`。
内部 `executeSearch()` 本身没有导出，不宜在设计中假设可以直接 import。[O3]

[推断] OMP 的优先实现可选两条：

1. 同名包装 `web_search`，由 `ctx.invokeTool` 把原生链作为一个 host capability。
2. 保留 WRR 独立工具，通过导出的 custom-tool/search API 构造宿主调用；明确携带 registry、session id、active model 和 signal。

第 2 条需要针对本机编译安装检查 import 与 context 兼容性。本次只核验源码和文档，没有运行扩展。[O2、O3、O4]

### 1.4 judge role 是否能复用

**事实 OMP-8：eval 有正式 typed-judgment helper。**
`judge(state, questions)` 使用 session 的 `judge` role。
同一 state 的独立问题放在一次调用里；多个 states 用 `judge_batch`。
问题支持 `choice`、`bool`、`score`，其中 `.bool` 是概率，不是 Python/JS 布尔值。[O8]

`judge_batch` 默认 concurrency 为 32，任务由 host 持有。
支持 drain、状态、取消与 attach；单项失败和整个批次失败分开表达。
`completion` 是无工具的 stateless chat 调用，只公开 default/smol/slow tier；不应拿来替代 native Jev。[O8]

**事实 OMP-9：扩展也有源码导出的 judge resolver。**
package.json 明确导出 `@oh-my-pi/pi-coding-agent/judgment`。
`resolveJudge(deps)` 返回 `ChainJudge`；调用形式为 `judge.judge({state, questions}, {signal})`。
依赖包括 `settings`、`registry`、`purpose`，可带 session id、cache、usage journal 和 telemetry。[O4、O9]

judge resolver 使用宿主 registry 的 `getApiKey`、credential resolver 和 model headers。
因此适配器不需要读取 TypeSafe token 后另开一套认证。[O9]

eval bridge 还把外部 `bool` 映射为库内部 `noul`，再映射回答案。
扩展直接调用 judge module 时需要沿用这份类型映射，不能照抄 eval payload 而忽略库类型。[O10]

[推断] 可以实现 OMP `HostJudge`，复用当前 `modelRoles.judge`。
源码面已经具备；本机 18.12.0 单文件发行版能否无额外依赖 import 该 subpath，仍需运行验证。
这与“只能在 eval 里用 judge”不同，也不等于已经验证可直接上线。[O4、O9、O11]

### 1.5 超时、设备与 skills

**事实 OMP-10：搜索超时是每个 provider 的上限。**
`providers.webSearchTimeoutSeconds` 当前为 60 秒，文档最大 300 秒。
超时后前进到下一个 provider；这不是整条链的总 deadline。
AbortSignal 从工具一路传给 provider。[O1、O5、O7]

`providers.fetch` 独立选择 `auto/native/trafilatura/lynx/parallel/firecrawl/jina`。
SearXNG endpoint 可来自配置或 `SEARXNG_ENDPOINT`；Basic auth 优先于 bearer token。
`exa.enabled` 与 `exa.searchDelayMs` 分别控制启用和请求间隔。[O1、O7]

**事实 OMP-11：`loadMode` 和 `xd://` 决定暴露方式，不是另一种 provider。**
新工具默认 discoverable；显式 `loadMode: "essential"` 会常驻工具目录。
`read xd://<device>` 获取 schema，`write` 传 JSON 参数后由 mounted tool 执行。
设备的 approval、updates、结果和 error 标志都会保留。[O2、O12]

`pi.on("tool_call")` 可以阻断或修改输入，但 handler 默认有 30 秒 active-work 超时且 fail-closed。
慢搜索不宜全部塞入 pre-exec handler；真正执行宜放在 tool 的 execute 中。[O2；后半句为推断]

**事实 OMP-12：skills 是文件能力包。**
OMP 在启动时发现名称、描述与路径，正文通过 `skill://` 按需读取。
常规 provider 只扫描 `skills/<name>/SKILL.md` 一层。
skill 元数据不等于“可自动执行的搜索函数”。[O13]

### 1.6 与现有 WRR OMP 接法的差异

当前 `integrations/omp.mjs:560–616` 注册的是 `wrr_search/wrr_fetch/wrr_similar`，全部显式 essential。
执行器 spawn Python `wrr-cli.py`，没有调用 `ctx.models`、原生 search 或 judge role。[L1]

`childEnvironment():10–19` 使用环境变量白名单；额外变量由 `WRR_SHARED_ENV_KEYS` 显式加入。
`invokeCli():70–85` 把 signal 转成 SIGINT，并在 grace 后 SIGKILL；另有 180 秒总超时。
这些本地事实说明目前并未复用 OMP provider 配置。[L1]

现有 fetch fallback 在 `:637–657` 生成 host `read` 的 handoff proposal。
`wrr_host_fetch_handoff` 的说明明确表示 accept 不执行 read。
推荐、接受、宿主执行、回传证据是分开的状态。[L1]

### 对 WRR 重构的含义：OMP

1. [推断] 把原生 web chain 作为一个 `HostSearch`，先完整保留用户角色选择与认证，再决定是否开放链内逐来源调度。[O1、O2]
2. [推断] 用 `HostJudge` 回调复用 judge role；Python 内核只接收问题和答案，不持有 OMP token。[O9、O10]
3. 不应照搬：不要复制 OMP provider catalog；不要把 essential 当成所有来源的默认暴露方式；不要把第一条可用搜索等同于质量验证。[O1、O2、O13]

## 2. Hermes

### 2.1 工具插件与 backend 插件是两个接入面

**事实 Hermes-1：原生插件以 manifest + `register(ctx)` 注册。**
`plugin.yaml` 可声明 `provides_tools`、hooks、Python 依赖、环境要求和 capability。
`ctx.register_tool(name, toolset, schema, handler, …)` 注册模型可调用工具。
`ctx.register_hook()` 则订阅生命周期事件。[H1]

**事实 Hermes-2：覆盖 built-in 需要双重同意。**
代码要指定 `override=True`；非 bundled plugin 还要 operator grant。
旧配置键是 `plugins.entries.<id>.allow_tool_override: true`。
当前文档推荐 manifest `capabilities: [tools.override]` 的 consent 流程；旧键仍可用但已标为 deprecated。[H1]

没有 grant 时会抛 `PluginToolOverrideError`，loader 可停用插件而让 Hermes 继续。
因此“插件安装成功”不能证明 `web_search` 已被接管。[H1]

**事实 Hermes-3：当前 Hermes 有专用 WebSearchProvider API。**
`ctx.register_web_search_provider(instance)` 把 backend 放进 web registry。
发现位置包括 bundled `plugins/web/<name>/`、用户 `~/.hermes/plugins/web/<name>/` 与 pip entry point。
backend manifest 使用 `kind: backend` 和 `provides_web_providers`。[H2]

provider 需要 `name`、廉价的 `is_available()`，以及支持能力对应的 search/extract 方法。
`is_available()` 不允许做网络请求，因为工具设置界面也会调用。
`search(query, limit)` 的公开基本签名没有 AbortSignal 参数。[H2]

**事实 Hermes-4：原生配置支持按能力选 backend。**
`web.search_backend` 与 `web.extract_backend` 分别覆盖 `web.backend`。
未设置时按环境中的 API key/URL 自动选择。
内置来源包括 Firecrawl、SearXNG、Tavily、Perplexity、Exa、Parallel、Brave、DDGS 等。[H2、H3]

Hermes 的自动发现主要针对其 backend registry 和凭据。
官方文档没有证明该机制会自动发现任意 CLI、OMP role 或宿主 skill。[H2、H3]

**事实 Hermes-5：search backend 有固定结果 envelope。**
成功结构为 `{"success":true,"data":{"web":[…]}}`。
每项字段包括 `title/url/description/position`。
工具层负责 JSON 序列化，支持等待 coroutine provider。[H2]

### 2.2 模型、Jev 与 skills

**事实 Hermes-6：`ctx.llm` 可以复用宿主模型认证。**
`complete/complete_structured` 有同步与异步版本，默认跟随用户当前模型。
host 持有 OAuth、refresh 与凭据池，插件不需要得到 token。
指定 provider/model/profile 等 override 需要对应授权。[H5]

`complete_structured` 请求 JSON 并本地解析，必要时按 jsonschema 检查。
这不是 TypeSafe 的 native choice/noul/score 概率合同。
不能把普通 chat JSON extraction 宣称为 Jev calibrated judgment。[H5；最后一句为推断]

插件还能注册 auxiliary task，通过 `auxiliary.<task>` 接受 operator 配置。
该能力可复用宿主的辅助 chat 路由，但本次未发现 Hermes 官方 `modelRoles.judge` 或 native Jev classifier role。[H5]

**事实 Hermes-7：Jev 可以“工具 + 使用 skill”集成。**
Hermes 官方 catalog 的 community 条目 `jev-typesafe` 指向 `ajensenwaud/hermes-jev-plugin`。
固定 commit 暴露 `jev_evaluate/jev_check/jev_route/jev_score`。
bundled `jev:jev-questions` 通过 `skill_view()` 加载。[H4、H6]

该插件要求独立 `TYPESAFE_API_KEY`，installer 可保存到 `~/.hermes/.env`。
技能正文负责问题设计，实际 HTTP 在 client.py；skill 自身没有变成宿主 native model role。[H6]

**事实 Hermes-8：另有“Jev 推荐 skill”的 hook 插件。**
`jev-skill-router` 用 `pre_llm_call` 读 live roster，最多推荐一个 skill。
先对 roster 做 choice 与 gate，再读 shortlist 的 SKILL.md excerpt 判断 fits。
最终注入 `<skill_relevance>`，不代替 agent 执行 skill。[H7]

其默认 mode 为 off；缺 key、超时或错误时不注入内容。
TypeSafe、OpenRouter 和 Vercel Gateway 是插件自己的 backend 选择。
这说明“Jev skill 集成”至少有工具说明与 hook 推荐两种含义。[H7]

### 2.3 与现有 WRR 插件的对照

仓库 `plugin.yaml:1–15` 声明 `web_search/web_fetch/web_similar` 和 `pre_llm_call`。
`__init__.py:434–457` 用 `toolset="wrr"`、`is_async=True`、`override=True` 接管 `web_search`。
这属于工具覆盖，不属于新版 WebSearchProvider backend 注册。[L2]

当前 manifest 没有声明 `tools.override` capability。
新版 Hermes 的 consent 路径可能要求显式 grant；不能仅凭旧实现中的 `override=True` 推断授权已完成。[L2、H1]

`hermes-overlay/.../SKILL.md:74–90` 只绑定 `web_search/web_fetch/web_similar`。
正文明确要求以 live schema 为准，不硬编码 provider/fallback。
这是提示词适配，不能替代插件注册或 provider 健康检查。[L3]

### 对 WRR 重构的含义：Hermes

1. [推断] 新宿主优先实现 `wrr` backend，由 `web.search_backend` 选择；只在需要完整自定义 schema 时注册独立 `wrr_search`。[H2、L2]
2. [推断] 把 Jev 能力声明成可探测的 judge capability；不存在 native judge 时才走 WRR 自有 TypeSafe 凭据，不冒充 `ctx.llm` 原生 Jev。[H5、H6]
3. 不应照搬：不要继续把 override 视为无条件可用；不要把 `pre_llm_call` 推荐当作搜索已执行；不要从基本 provider 签名推断取消已端到端传递。[H1、H2、H7]

## 3. Pi coding agent

### 3.1 当前项目与扩展面

**事实 Pi-1：当前上游已使用新仓库和包名。**
旧 `badlogic/pi-mono` 的当前文档指向 `earendil-works/pi`。
包名为 `@earendil-works/pi-coding-agent`，不能用旧 `@mariozechner/*` 示例代表所有当前 API。[P1]

**事实 Pi-2：extension 是进程内 TS/JS 模块。**
default factory 接受 `ExtensionAPI`，可注册工具、commands、events、providers 与 virtual models。
常规路径包括 `~/.pi/agent/extensions/` 与项目 extension 目录。
可用 `pi --extension path` 显式加载，生命周期资源需在 session_shutdown 清理。[P1]

**事实 Pi-3：工具注册、组合与取消都有 API。**
`pi.registerTool()` 定义名称、TypeBox 参数、execute 和结果。
工具可调用 `ctx.executeTool(name,args,{signal,onUpdate})`；嵌套调用仍经过 validation 和 tool hooks。
`ctx.signal` 用于 active turn 的嵌套工作，idle event/command 不一定有 operation signal。[P1]

Pi 的 `exposure` 有 direct/model-only/codemode/deferred/hidden。
这与 OMP 的 `loadMode` 不同；同一注册对象不宜不加转换就跨两者复用。[P1；后半句为推断]

### 3.2 virtual model 与 classifier

**事实 Pi-4：virtual model 是正式选择/分发层。**
`pi.registerVirtualModel({provider,id,route(request,ctx)})` 可显示在 `/model`。
route 返回物理模型与 thinkingLevel；不能返回另一个 virtual model。
request 带 `reason/previous/failed/state/messages/signal`。[P2]

选择的虚拟模型与实际分发模型分开记录。
状态跟随 session branch，并能跨 compaction 保留。
continuation/retry 可沿用 previous/failed，避免每次工具后续都重新分类。[P2]

**事实 Pi-5：classifier 不必伪装成 chat 模型。**
官方示例使用 `ctx.modelRegistry.findOfType("classifier","typesafe","jev-latest")`。
然后调用 `modelRegistry.classify(model,{state,questions},{signal})`。
判断 `stopReason` 与答案类型后才使用概率。[P3]

provider 注册也区分 chat、image、classifier。
若自定义 provider 覆盖完整 models 列表，会替换这些类型的目录；只改 endpoint 不宜复制模型表。[P5]

**事实 Pi-6：pi-jevflow 已复用这一 API。**
私有固定版本 `lib/jev-router-pool.ts:58–68` 通过 find、chat 类型、认证、图像和 thinking 支持过滤候选。
`:203–230` 解析 TypeSafe classifier，构造 ClassifierContext，并调用 registry.classify。
`:204–205,235` 合并 generation/request AbortSignal，整池判断预算为 8 秒。[P4]

同文件 `:242–262` 先选 model，再按选中模型支持的 levels 选 thinking。
入口 `extensions/pi-jevflow.ts` 创建 main/subagent 两个虚拟模型定义，通过 ownership helper 注册。
这是宿主模型路由；WRR 可借鉴 registry 与取消机制，不应复制其 chat 模型分层任务。[P4]

### 3.3 原生搜索和 MCP

**事实 Pi-7：当前 core tool 集合没有原生 web search。**
`src/core/tools/index.ts` 的 ToolName 为 read/bash/powershell/edit/write/grep/find/ls。
coding 默认工具为 read/bash/edit/write；没有 WebSearch 或 web provider config。[P6]

该结论限定为已读 core factory，不声称用户安装的 extension/MCP 没有搜索。
也不应把 provider 的 chat endpoint 配置视为搜索来源目录。[P5、P6]

**事实 Pi-8：当前 Pi 已支持 MCP。**
官方 docs/mcp.md 支持 stdio 和 Streamable HTTP，不支持旧 SSE。
配置位于 `~/.pi/agent/mcp.json` 与 `.pi/mcp.json`；project 配置受 trust 控制。
extension 还可 `pi.registerMcpServer()`。[P1、P7]

MCP `timeout` 单位是秒，默认 60，progress 会重置该计时。
stdio 用 command/args/env/cwd；HTTP 用 url/headers/oauth。
变量支持 `${VAR}`；OAuth 由宿主保存与刷新。[P7]

### 对 WRR 重构的含义：Pi

1. [推断] 优先 extension：工具调用 Python 内核，Jev 判断反向委托 `modelRegistry.classify`，沿用宿主认证。[P1、P3、P4]
2. [推断] MCP 可作为无 Pi 定制的搜索入口；若要求复用 registry classifier，extension 比纯外部 MCP 更直接。[P1、P7]
3. 不应照搬：不要把 OMP role/loadMode API 写进 Pi adapter；不要为实现搜索路由而修改用户当前 chat virtual-model 选择。[P1、P2、P6]

## 4. Claude Code

### 4.1 内置搜索的配置边界

**事实 Claude-1：WebSearch backend 不可配置。**
官方 Tools reference 明确要求：若要其他 provider，添加暴露 search tool 的 MCP server。
WebSearch 返回 titles/URLs，不下载结果正文；一次调用内部可能发出最多 8 次搜索。[C1]

工具参数支持 allowed_domains 或 blocked_domains，二者不能同时使用。
权限规则仅接受裸 `WebSearch`，不接受 provider/domain specifier。[C1]

**事实 Claude-2：WebFetch 会丢失原始内容信息。**
WebFetch 抓取 HTML 后转换 Markdown，通常再用单独模型按 prompt 提取。
转换步骤不可配置；因此结果并非原始整页。
支持 cache TTL 与 deadline 环境变量；默认页面下载 deadline 为 5 分钟。[C1]

### 4.2 MCP、plugins、skills 与 settings hooks

**事实 Claude-3：MCP 是官方通用自定义工具入口。**
支持本机 stdio 和远端 HTTP；旧 SSE 标为 deprecated。
可通过 `claude mcp add`、`.mcp.json` 或 plugin 提供 server。
CLI 配置不能注册 SDK in-process server；该形态属于 Agent SDK host。[C2]

stdio 凭据可通过 env，HTTP 通过 headers/headersHelper/OAuth。
插件或项目配置对 credential env 有特殊限制，不应假设进程自动得到全部宿主密钥。[C2]

**事实 Claude-4：MCP timeout 与 Pi 语义不同。**
每 server `timeout` 单位为毫秒，是每次调用的 hard wall-clock limit。
覆盖 `MCP_TOOL_TIMEOUT`；progress 不延长这个 hard limit。
另有 transport idle timeout；stdio 退出有 SIGINT→SIGTERM→SIGKILL 的清理流程。[C2]

**事实 Claude-5：plugin 可以统一分发 skill、hook 和 MCP。**
manifest 在 `.claude-plugin/plugin.json`。
components 包括 skills、agents、hooks、MCP，以及当前新增的 hooks module/mod。
skill 通过现有 Skill 工具加载，不会独立增加原生搜索 provider。[C3、C1]

**事实 Claude-6：settings hooks 能检查、改输入、改结果。**
PreToolUse 可修改 updatedInput 或阻断。
PostToolUse 可用 updatedToolOutput 替换结果；当前旧字段 updatedMCPToolOutput 只针对 MCP。
这不等于有一个可配置的 WebSearch provider registry。[C5]

command/http hook 超时默认不一定阻断工具。
需要 gate 时可配置 `onFailure:"block"`；hook 类型和事件不同，超时行为也不同。
不能把“用了 Jev hook”自动等同于可靠强制验证。[C5]

### 4.3 当前新增的 mods：不能沿用旧认知

**事实 Claude-7：当前文档已有进程内 mods。**
CLI 从 v2.1.287 起默认支持 mods。
`on('tool.call', …)` 可调用 next 保留原工具，也能直接返回 result 接管工具调用。
替代 built-in 时，result 要符合该 build 的工具结果类型。[C4、C6]

因此，当前第三方可在工具调用层接管 WebSearch。
这与“把内置搜索 backend 配成 Exa”不同；后者仍没有配置项。[C1、C6]

**事实 Claude-8：mods 能注册工具并复用宿主模型。**
`$.tool.register()` 新工具显示为 `mcp__<plugin>__<name>`。
`$.model.complete` 是无工具独立调用，`$.model.fork` 带当前会话快照。
两者使用 session credentials，费用计入用户 plan/API/cloud provider。[C7]

`$.model.complete` 有 model、maxTokens、timeoutMs 等参数。
当前文档没有提供 TypeSafe classifier 或 OMP 式 judge role。
原生模型复用可以做 chat 验证，但不能据此声明支持 Jev native probability API。[C7]

**事实 Claude-9：mod 可组合 MCP 与取消。**
`$.mcp.call` 可调用已连接 server，`next.signal` 随事件放弃而 abort。
文件、process 和 HTTP 访问走 mods API；module 本身没有 Node filesystem/network API。
运行权限仍属于用户，组织策略还可禁用或限制 mod。[C4、C7]

### 对 WRR 重构的含义：Claude Code

1. [推断] 默认发布 MCP + skill 的 plugin，保留内置 WebSearch 作显式补充；需要只走 WRR 时可通过用户权限设置禁用裸 WebSearch。[C1、C2、C3]
2. [推断] 新版可选 mod 薄适配，以 `$.mcp.call` 或 process 调用统一内核；需要原生同名替换时使用 tool.call 接管，按 build 固定结果格式。[C6、C7]
3. 不应照搬：不要借用 Claude 登录 token 调 TypeSafe；不要假设普通 hook 能短路并返回任意工具结果；不要把 chat complete 的输出当 Jev probabilities。[C2、C5、C7]

## 5. 宿主对照表

下表的推荐列是 [推断]。其余列以各宿主一手文档与源码为依据。

| 宿主 | 工具注册方式 | 原生搜索 provider 配置 | 原生 judge/模型角色可否复用 | 取消/超时传递 | 凭据传递方式 | 推荐接入形态 |
| --- | --- | --- | --- | --- | --- | --- |
| OMP | `pi.registerTool`；同名 wrapper 可 `ctx.invokeTool` | `modelRoles.web` + `retry.fallbackChains.web`；fetch 独立 | 能复用 `judge` role：eval helper 或导出 resolveJudge；web 是 search kind/grounded chat | execute AbortSignal；原生逐 provider 60 秒，最大 300 秒；总 deadline 需上层管 | registry/AuthStorage 留在 host；跨 Python 用回调，不导出 token | extension + Python 内核；native HostSearch/HostJudge。[O1、O2、O9] |
| Hermes | `ctx.register_tool` 或 `register_web_search_provider` | `web.search_backend/extract_backend/backend`；按凭据自动发现 | `ctx.llm`/auxiliary 可复用 chat auth；未确认 native Jev role；Jev community plugin 自带 key | async handler 可用；provider 基本签名未给 cancellation；HTTP deadline 需 backend 自管 | host ctx.llm 不交 token；搜索/Jev plugin 常用 env 与 `.env` | 新版 backend plugin；旧版有授权的 tool plugin。[H1、H2、H5、H6] |
| Pi | `pi.registerTool`；`ctx.executeTool`；`registerMcpServer` | 已读 core 没有 web provider；搜索来自 extension/MCP | `modelRegistry.classify` + classifier model 能复用 Jev；没有已核验的 OMP 式角色 | request/ctx signal；MCP timeout 默认 60 秒、progress 重置 | model registry 解析 provider auth；MCP env/headers/OAuth | extension，MCP 作通用分发。[P1、P2、P3、P6、P7] |
| Claude Code | MCP；新版 mod `$.tool.register`；`tool.call` 可接管 | WebSearch backend 不可改；domain filters/permissions 不选 provider | mod complete/fork 可复用 Claude auth；未提供 native Jev role | mod next.signal；MCP 毫秒 hard timeout + idle timer；settings hook 行为分类型 | MCP env/headers/OAuth；mod 自有模型调用用 session auth | MCP + skill plugin；有版本要求时选 mod。[C1、C2、C3、C5、C7] |

### 统一内核最少需要区分的能力

[推断] 能力发现宜输出以下事实，避免从“已安装”直接跳到“质量足够”：[O1、H2、P4、C2]

1. `search`：候选 scope 是一个宿主链，还是单个来源；是否返回 answer、sources、citations。
2. `judge`：native typed judgment、prompted chat JSON，或暂不可用；记录实际 backend。
3. `fetch`：原始/提取/模型摘要、截断与 completeness；不可把三者混写成正文。
4. `execution`：凭据留在何处、signal、hard deadline、progress、最大输出量。
5. `skill`：只提供说明、推荐交接，还是具有明确可执行入口；不可把 SKILL.md 当 RPC endpoint。

这些字段属于重构建议，不是四个宿主已经共同实现的标准。[推断，依据同上]

## 6. 关键发现 Top 5

1. **OMP 已经拥有 WRR 想复用的原生搜索目录与 fallback。** `web/exa` 是 search-kind selector；委托原生工具比复制 provider 规则更接近用户当前配置。[O1、O2、O5]
2. **OMP judge 可以从扩展侧复用。** 除 eval helper 外，package exports 里存在 judgment resolver；但本机编译发行版的导入与 usage 归属尚未运行验证。[O4、O8、O9]
3. **Hermes 的推荐接法已经变化。** 当前专用 WebSearchProvider plugin 比旧 `web_search` override 更薄；现有 WRR manifest 的授权声明要结合目标 Hermes 版本处理。[H1、H2、L2]
4. **Pi 已有 native classifier 与 virtual model 分层。** pi-jevflow 正在调用 registry.classify；WRR 可复用这一判官入口，不必接管用户的 chat 路由。[P2、P3、P4]
5. **Claude Code 同时存在稳定 MCP 路径与新版 mod 路径。** backend 仍不可配置，但 mod 已能接管工具并调用宿主模型；不能继续断言第三方只能使用外部 hooks。[C1、C4、C6、C7]

## 7. 仍需确认的问题

1. **本机 OMP 18.12.0 的运行合同**：[未核验] judgment/search subpath 在单文件发行版中的实际 import、settings scope、usage journal 与 telemetry 绑定；原生 wrapper 的中断和 auth refresh。已经核验文档及上游源码，没有加载试验扩展。[O4、O9、O11]
2. **macmini Hermes 的版本与插件状态**：[未核验] 未访问远端安装、源码、web backend、allow_tool_override grant 或 Jev key。当前机器只有 Hermes 配置/skills 痕迹；不能据此替代 macmini 状态。[L4、H1、H2]
3. **Hermes native Jev 与取消**：[未核验] 已读 ctx.llm 是 chat/structured lane，已读 WebSearchProvider 基本签名不含 cancellation；需要目标版本源码确认扩展 context 和 native classifier 是否另有接口。[H2、H5]
4. **Pi 实际安装状态**：[未核验] PATH 没有 pi；没有运行 pi-jevflow。报告核验的是固定源码、官方示例和私有仓库；classifier catalog/凭据可用性需要目标进程探测。[P3、P4、P5]
5. **Claude Code 目标版本与跨边界执行**：[未核验] mods 是否在用户使用的 CLI/Desktop/IDE build 中可用、组织是否允许；MCP 请求取消能否让 Python/CLI 子进程真正退出；skill 来源如何提供可执行且可审计入口。[C2、C4、C7]

## 来源索引

### OMP

- **[O1]** 本机 [web_search 文档](omp://tools/web_search.md)，章节 Inputs、Outputs、Flow、Modes/Variants；含 provider 源路径、结果合同、role chain、Exa/SearXNG 与 timeout。
- **[O2]** 本机 [extensions 文档](omp://extensions.md)，行 291–308（typed settings）、340–432（ctx/models）、661–686（invokeTool）、609–727（tool/loadMode）、1273–1278（handler timeout）。
- **[O3]** [search/index.ts，固定 commit](https://github.com/can1357/oh-my-pi/blob/9d5d4ac5b50ac2bb5866e4202efc0cf785dfc57a/packages/coding-agent/src/web/search/index.ts)，ExecuteSearchOptions、内部 executeSearch、WebSearchTool、webSearchCustomTool。
- **[O4]** [package.json，固定 commit](https://github.com/can1357/oh-my-pi/blob/9d5d4ac5b50ac2bb5866e4202efc0cf785dfc57a/packages/coding-agent/package.json)，version 与 exports（judgment/web/search/config）。
- **[O5]** 本机只读 [modelRoles](cfg://modelRoles)、[fallbackChains](cfg://retry.fallbackChains) 和 [配置总表](cfg://)，本次会话有效值。
- **[O6]** 本机 [models 文档](omp://models.md)，Allowed provider/model api values；native judgment 与 search kind 的边界。
- **[O7]** 本机 [settings 文档](omp://settings.md)，行 950–977，providers.fetch、webSearchTimeoutSeconds、exa、searxng。
- **[O8]** 本机 [eval 文档](omp://tools/eval.md)，行 159–165、194–201；[judge helper 合同](xd://eval/judge)。
- **[O9]** [judgment/index.ts，固定 commit](https://github.com/can1357/oh-my-pi/blob/9d5d4ac5b50ac2bb5866e4202efc0cf785dfc57a/packages/coding-agent/src/judgment/index.ts)，JudgeDeps、resolveJudge、ChainJudge、registry credential resolution。
- **[O10]** [judgment-bridge.ts，固定 commit](https://github.com/can1357/oh-my-pi/blob/9d5d4ac5b50ac2bb5866e4202efc0cf785dfc57a/packages/coding-agent/src/eval/judgment-bridge.ts)，sessionJudge、bool/noul mapping、runEvalJudgment。
- **[O11]** 本机 [extension-loading 文档](omp://extension-loading.md)，行 274–285，host-bundled module loader 与 import resolution。
- **[O12]** 本机 [write 文档](omp://tools/write.md)，Writable internal resources and tool devices；`xd://` dispatch 保留 schema/result/approval。
- **[O13]** 本机 [skills 文档](omp://skills.md)，发现路径、优先级、skill URI 与按需加载。

### Hermes

- **[H1]** [Build a Hermes Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins)，Native plugin contract、Declaring capabilities、Overriding a built-in tool、Bundle skills。
- **[H2]** [Web Search Provider Plugins](https://hermes-agent.nousresearch.com/docs/developer-guide/web-search-provider-plugin)，discovery、ABC、response shape、tool dispatch。
- **[H3]** [Web Search & Extract](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-search)，backend 表、per-capability 配置和凭据。
- **[H4]** [Hermes catalog 固定 commit](https://github.com/NousResearch/hermes-agent/tree/5fc490e2b34123212d73ef316086f939567a725a/plugin-catalog)，已读 jev-typesafe.yaml、jev-skill-router.yaml。
- **[H5]** [Plugin LLM Access](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access)，complete/structured、task-routed auxiliary、trust gate、host-owned auth。
- **[H6]** [hermes-jev-plugin README，固定 commit](https://github.com/ajensenwaud/hermes-jev-plugin/blob/b3d29f71770a3447ad0b3db00658f64a8edc7dd3/README.md)，4 个工具、bundled skill、TypeSafe env 与 client 布局。
- **[H7]** [jev-skill-router README，固定 commit](https://github.com/DoGMaTiiC/hermes-jev/blob/5dddbeaac5be08281e6c3b372eeaba3639971f0a/plugins/jev-skill-router/README.md)，pre_llm_call、两阶段选择、后端、数据披露与 fail-open。

### Pi

- **[P1]** [Pi extensions，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/docs/extensions.md)；[当前 README](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/README.md)。
- **[P2]** [Virtual Models，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/docs/virtual-models.md)，selection/dispatch、request signal/state、classifier 调用。
- **[P3]** [jev-router.ts 官方示例，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/examples/extensions/jev-router.ts)，findOfType/classify/signal/stopReason。
- **[P4]** [pi-jevflow 私有固定 commit](https://github.com/Loveacup/pi-jevflow/tree/be91bb962899dac5bc92fcad99c3b350dcb6cccf)，已经认证读取 package.json、extensions/pi-jevflow.ts、lib/jev-router-pool.ts。
- **[P5]** [Custom Providers，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/docs/custom-provider.md)，classifier discriminant、auth 与 mixed-operation provider。
- **[P6]** [core/tools/index.ts，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/src/core/tools/index.ts)，ToolName、createCodingTools/createAllTools。
- **[P7]** [MCP Servers，固定 commit](https://github.com/earendil-works/pi/blob/1402973a9dbe8c627341e04c9744c54953e285ca/packages/coding-agent/docs/mcp.md)，配置、timeout、progress、环境变量、OAuth 与 teardown。

### Claude Code

- **[C1]** [Tools reference](https://code.claude.com/docs/en/tools-reference#websearch-tool-behavior)；同页 WebFetch tool behavior，明确 backend 不可配置、提取与缓存/deadline。
- **[C2]** [MCP](https://code.claude.com/docs/en/mcp)，transport、stdio teardown、timeout、env expansion、headersHelper、OAuth、SDK server 限制。
- **[C3]** [Plugins overview](https://code.claude.com/docs/en/plugins/overview)，manifest 与 component 分发。
- **[C4]** [Mods overview](https://code.claude.com/docs/en/plugins/mods/overview)，v2.1.287、运行范围、授权与 capabilities。
- **[C5]** [Hooks reference](https://code.claude.com/docs/en/hooks)，PreToolUse、PostToolUse、Timeouts、onFailure 与 updatedToolOutput。
- **[C6]** [Mods events](https://code.claude.com/docs/en/plugins/mods/events#guard-or-change-a-tool-call)，next、short-circuit、built-in result shape、managed hook 优先级。
- **[C7]** [Mods API](https://code.claude.com/docs/en/plugins/mods/api)，tool.register、model.complete/fork、next.signal、process/http/mcp 与 credential attribution。

### 本仓库与本机对照

- **[L1]** [integrations/omp.mjs](../../integrations/omp.mjs)：10–19（env）、25–125（subprocess/cancel/timeout）、537–620（工具注册）、637–657 与 662–747（host fetch handoff）。
- **[L2]** [plugin.yaml](../../plugin.yaml)：1–15；[__init__.py](../../__init__.py)：434–471，Hermes 注册与 pre_llm_call。
- **[L3]** [Hermes overlay](../../hermes-overlay/research/web-research-router/SKILL.md)：29–39、74–90，live schema 与宿主工具绑定。
- **[L4]** 本机 `/Users/alexcai/.hermes/config.yaml:1–4`；只包含 2 个插件启用记录，没有据此推断远端安装状态。
