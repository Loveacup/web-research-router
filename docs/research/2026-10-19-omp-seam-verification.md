# OMP 18.12.0 接缝核验与切片 4 烟测

日期：2026-10-19。范围：设计 §6、§11 / 计划 T4.0–T4.2。按 T4.0 核验 → RPC → 扩展 → 烟测顺序完成。

## 1. 核验方法与边界

- `/opt/homebrew/bin/omp --help` 实际输出 `omp v18.12.0`，支持 `-p`、`--mode json`、`--no-session`、`--no-extensions` 与显式 `-e`；`--no-extensions` 仍加载显式 `-e` 路径。
- 临时扩展 `/tmp/wrr-omp-seam-probe.mjs` 注册 `wrr_seam_judge` 及同名 `web_search`，只发送合成公开 state 与公开查询。试验文件已删除。
- 没有读取、打印或传出任何凭据，没有修改 `cfg://` 或全局配置。扩展只把宿主 registry 交给原生 resolver；RPC 不携带宿主凭据。
- 试验命令：

```sh
/opt/homebrew/bin/omp --no-extensions -e /tmp/wrr-omp-seam-probe.mjs \
  --no-session --no-skills --no-rules --no-lsp --no-title \
  --tools web_search --auto-approve --max-time 120 -p \
  'Call wrr_seam_judge exactly once, then call web_search exactly once with query "Python asyncio documentation". Return both exact tool outputs. Do not use any other tools.'
```

## 2. T4.0 假设结论

| 假设 | 结论 | 直接证据 |
|---|---|---|
| 单文件发行版可 import `@oh-my-pi/pi-coding-agent/judgment` | 成立 | 临时扩展动态 import 后成功取得 `resolveJudge`，随后真实 judgment 成功。 |
| 扩展可复用 judge role 完成判断 | 成立 | `resolveJudge({settings: pi.pi.settings, registry: ctx.modelRegistry, sessionModel: ctx.model, sessionId, purpose})`；真实返回 `provider=typesafe`、`api=typesafe`、`model=jev-1.13.0`、`blue.noul=0.88`。 |
| 原生 resolver 使用 eval 表层的 `bool` | 不成立 | 首次试验传 `type:"bool"`，得到 `judgment: every judge candidate failed: typesafe/jev-latest API error (400): {"detail":{"error_type":"api_usage_error","message":"Invalid request."}}`；改成原生 `type:"noul"` 成功。最初探针把整个调用失败标成 `import:false`，该标记不能证明 import 失败；第二次分离 import 状态确认 import 成功。 |
| 同名 `web_search` 的 `ctx.invokeTool` 委托原生链而非递归 | 成立 | `typeof ctx.invokeTool === "function"`；真实返回 `provider:"exa"`、`sourceCount:1`；response keys 为 `provider,answer,sources,requestId`。 |
| 原生结果带实际 provider 元数据 | 成立 | `native.details.response.provider === "exa"`。不是扩展推测或静态 provider 表。 |
| 取消 signal 能传到 judge / 原生搜索 | 成立（实际预取消 + 内存管道在途取消） | 预先 abort 的 signal 传给 `judge.judge(request,{signal})` 抛 `AbortError: The operation was aborted.`；传给 `ctx.invokeTool(params,{signal})` 抛普通 `Error: Operation aborted`。不能仅凭 `error.name === "AbortError"` 识别取消；实现以 controller.signal.aborted 为准。在途 RPC judge/search 的 cancel 与断管由单测覆盖；未人工操作终端 Ctrl-C。 |

第二次探针直接写 stderr 的结构化记录（非模型概括）：

```json
{"import":true,"model":"jev-1.13.0","backend":"typesafe","provider":"typesafe","answers":{"blue":{"type":"noul","noul":0.88}},"cancel":{"name":"AbortError","message":"The operation was aborted."}}
{"invokeTool":"function","outerSignal":true,"provider":"exa","responseKeys":["provider","answer","sources","requestId"],"sourceCount":1,"cancel":{"name":"Error","message":"Operation aborted"}}
```

### 实现路径决定

1. 使用宿主 `resolveJudge`，因此 Evidence 的 bridge backend 为 `host`，实际模型为 `jev-1.13.0`。不启用扩展直连备用路径；未来安装缺少该 export 时以 `host.judge=false` 交给 Python `resolve_judge`，其按 WRR 自有 TypeSafe 配置 → NoJudge 的顺序解析。
2. 注册同名 `web_search` wrapper。该入口通过本次执行的 `ctx.invokeTool` 注入 `host:web`；递归标记为 `WRR-Call-Id` 与 `hop=1`，带标记的调用直接委托原生，不重入 WRR。
3. `wrr_search` 是新工具名，按宿主合同没有 `ctx.invokeTool`；它仍用同一 RPC 和宿主 judge，但设置 `host.search=false`。不借用其他调用/会话的 context，也不伪称它能任意委托 `web_search`。需要宿主链的调用使用 `web_search`。
4. bool/noul 适配仅在扩展：原生 resolver 用 `noul`；可见 bool alias 输入/答案在扩展侧规范化。Python 协议始终使用 `noul/choice/score`。

## 3. RPC 与兼容边界

- `wrr search --rpc` 第一行请求为 `{query,count,sources,deadline_ms,host:{judge,search}}`；后续单行 JSON 复用字符串 id 关联 `judge` / `host_search` 请求与 `{id,result}` / `{id,error:{reason}}` 回包。
- 扩展向内核发 `{method:"cancel"}` 取消整个搜索。内核在单个请求超时/被取消时回发 `{id,method:"cancel"}`，扩展取消对应 controller，避免过期 judge / 搜索继续运行。
- 最终消息为 `{result:Evidence}`；非法请求为稳定错误码，不序列化原始异常、provider stderr 或凭据。断管按取消处理；晚到的已过期 reply 被忽略。
- `RpcHostJudge.backend="host"`，复用 `parse_response` 做 typed 验证、无重试，软预算 3 秒且受流水线的阶段预算和总 deadline 约束；失败/超时/取消沿用 `JudgeError` 语义。
- `RpcHostSearch` 描述符固定 `id=host:web, transport=host, privacy_class=public`；实际返回 provider 动态更新同一 inventory 中的 family，缺失时为 `host-web:opaque`。单测覆盖 Exa 与宿主 Exa 不重复计独立 family / RRF 票，以及 opaque 不计独立 family。
- `wrr_fetch`、`wrr_similar`、fetch handoff、skill handoff 的执行路径未重构；skill recommendation 仍走原隔离 CLI 调用。

## 4. 测试

```sh
.venv/bin/python -m pytest tests/unit/test_core_*.py -q -p no:cacheprovider
# 首轮全部通过。
.venv/bin/python -m pytest tests/unit/test_core_*.py -q -p no:cacheprovider -o addopts=
# 最终：210 passed in 4.20s
```

`-o addopts=` 只去掉仓库默认附加 quiet 参数，使最终汇总可见。新增 `tests/unit/test_core_rpc.py` 用 `asyncio.StreamReader.feed_data` 模拟双向内存管道，覆盖真实协议往返、3 次判断、类型/概率验证、provider family、乱序响应、错误分类、超时与逐请求取消、全局在途取消、EOF/非法 id、无宿主能力以及非法初始请求。

对 `tests` 执行 `grep omp.mjs` 及 `grep wrr_search|WRR_SHARED_ENV_KEYS|wrrExtension` 均无匹配：仓库没有现成 OMP JS 扩展测试需要同步或运行。JS 的实际加载与执行由下述本机 OMP 烟测覆盖。

## 5. 新会话真实烟测

使用显式扩展、无持久 session、无规则/skills 自动发现；没有复用现有对话。最终一次采用 `--mode json` 捕获 `tool_execution_end.result.details`，而不是依赖模型复述 Evidence：

```sh
/opt/homebrew/bin/omp --no-extensions \
  -e /Users/alexcai/code/web-research-router/integrations/omp.mjs \
  --no-session --no-skills --no-rules --no-lsp --no-title \
  --tools web_search --auto-approve --max-time 120 --mode json -p \
  'Call web_search exactly once with query "Python asyncio documentation" and count 2. Do not specify sources. After the tool returns, say only done. Do not retry or call other tools.'
```

进程 exit 0；捕获到一次真实 `tool_execution_end` Evidence：

```json
{"query":"Python asyncio documentation","plan":{"sources":["deepseek","exa","host:web"],"reasons":{"deepseek":"forced:primary","exa":"forced:primary","host:web":"fit:0.66","qmd":"rejected:0.16","local_obsidian":"rejected:0.14","skill":"rejected:0.12","academic":"rejected:0.11"},"judge":"host","evidence_shape":{"choice":"code","confidence":0.81},"freshness_required":0.13,"gate":null},"runs":[{"source_id":"deepseek","status":"ok","elapsed_ms":401,"count":2},{"source_id":"exa","status":"ok","elapsed_ms":771,"count":2},{"source_id":"host:web","status":"ok","elapsed_ms":1782,"count":2}],"results":[{"title":"asyncio — Asynchronous I/O","url":"https://docs.python.org/3.13/library/asyncio.html","snippet":"# asyncio — Asynchronous I/O¶\n\nasyncio is a library to write concurrent code using the async/await syntax.\n\nasyncio is used as a foundation for multiple Python asynchronous frameworks that provide high-performance network and web-servers, database connection libraries, distributed task queues, etc.\n\nasyncio is often a perfect fit for IO-bound and high-level structured network code.\n...\n- A Conceptual Overview of asyncio\n\nExplanation of the fundamentals of asyncio.\n\nasyncio provides a set of high","contributions":[{"source_id":"deepseek","rank":1}],"judgment":{"rel":0.74,"inj":0.02},"flags":[],"source_ts":1790784000,"rrf_score":0.016393},{"title":"asyncio — Asynchronous I/O — Python 3.14.7 documentation","url":"https://docs.python.org/3/library/asyncio.html","snippet":"asyncio — Asynchronous I/O — Python 3.15.0 documentation\n\n# `asyncio` — Asynchronous I/O¶\n\nHello World!\n\n```\nimport asyncio\n\nasync def main():\n    print('Hello ...')\n    await asyncio.sleep(1)\n    print('... World!')\n\nasyncio.run(main())\n\n```\n\nasyncio is a library to write concurrent code using the async/await syntax.\n\nasyncio is used as a foundation for multiple Python asynchronous frameworks that provide high-performance network and web-servers, database connection libraries, distributed task ","contributions":[{"source_id":"exa","rank":1},{"source_id":"host:web","rank":1}],"judgment":{"rel":0.74,"inj":0.03},"flags":[],"source_ts":null,"rrf_score":0.016393}],"quality":{"executed":"ok","relevance":"judged","sufficiency":{"enough":0.77,"next_action":"accept","confidence":0.77,"scope":"delivered"},"independent_families":2,"recovered":false},"diagnostics":{"deadline_ms":15000,"elapsed_ms":3543,"judge":{"calls":3,"posts":3,"model":"jev-1.13.0","backend":"host","usage":{"input_tokens":4932,"output_tokens":484},"failures":[]}}}
```

结果：`plan.judge=host`；来源 `deepseek, exa, host:web` 均为 `ok`；Jev `calls=3,posts=3`；`quality.executed=ok,relevance=judged`；总耗时 3543 ms。Exa 与 host:web 同 URL 两条 contribution 的 RRF 仍为 0.016393，独立 family 总数为 2，没有把宿主 Exa 当第三份独立证据。

另做显式 `sources:["host:web"]` 的单次烟测：host:web run `ok`、519 ms、1 条结果；`plan.judge=skipped:explicit_sources` 是确定性选源门的预期值；后续判断仍由宿主执行，`diagnostics.judge.backend=host,calls=2,posts=2,model=jev-1.13.0`，整次 1898 ms。

无阻塞未解决项。范围外未核验项：手工终端 Ctrl-C 的 UI 路径、宿主 usage journal/telemetry 的独立账目归属；不能由本次 Evidence 的 token 统计推断这些已被验证。

## 6. 依据

- [本机扩展 API](omp://extensions.md)：settings scope、execute context、同名 `ctx.invokeTool` 的权限与 signal 继承。
- [本机扩展加载](omp://extension-loading.md)：显式扩展与 host-bundled subpath import。
- [本机 web_search 合同](omp://tools/web_search.md)：`details.response.provider`、sources/citations、取消传播。
- [原生 judgment 源码](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/judgment/index.ts) 与 [eval bridge](https://github.com/can1357/oh-my-pi/blob/main/packages/coding-agent/src/eval/judgment-bridge.ts)：经 Context7 查询依赖和 bool/noul 边界，最终结论以本机 18.12.0 的运行结果为准。
- [宿主接缝前期研究](2026-10-19-host-integration-surfaces.md) §1；[设计](../superpowers/specs/2026-10-19-wrr-core-design.md) §6、§11。
