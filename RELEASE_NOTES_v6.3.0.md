# web-research-router v6.3.0

## 本轮范围

2026-10-18：先适配已吸收上游，再吸收源码中的正文抽取、学术检索和来源归因能力。Exa / DeepSeek 双主力不变；没有切换 Stage S 的生产执行平面，没有新增后台升级或采集服务。

## 升级内容

### 1. 社区上游合同

- `community_sources.py` 同时解析 OpenCLI v1 列表和 v2 `{ok,data}`；rc=0 的失败 envelope 仍按真实错误分类。认证失败、验证码/限流停止后备尝试；结构错误不冒充空结果。
- last30days 支持 `results[]`、原始平台数组、`ranked_candidates`、`items_by_source`；cluster ID 只在解析到真实 HTTP(S) URL 后成为结果。
- EN 传递真实的有界 `--plan` 和对应 `--search`，不清除宿主标识。无法表达的显式来源范围跳过，不扩大检索范围。dev.to/Techmeme 只在安装版具备对应源码模块时成为候选。
- 新增定向 Bilibili、知乎、微博搜索，不加入无条件默认列表。删除无效的 OpenCLI V2EX search；V2EX 单平台请求改由 web engine 的站内查询执行，跨平台问题保留其他来源。

### 2. 正文抽取

- `BraveEngine.extract()` 从正则去标签切换为 `trafilatura>=2.3.1` 的本地文章正文提取，实际方法标记为 `httpx.trafilatura`。
- 沿用现有 `fetch_html` 的 DNS/IP、重定向、TLS 与授权边界；解析器不额外下载页面或资源。
- 实跑发现 precision 模式会删除空 inline 节点后的正文：PEP 20 的 `<pre><span></span>` 导致核心箴言丢失。现在先无损展开空 inline 标记，再做文章提取；保留代码块/引文内容，不回退为整页文本拼接。
- 仍拒绝空正文/已识别挑战页；完整性未知仍为 `unknown`/partial，截断仍为 `truncated`，不因换解析器就宣称全文完整。

### 3. 学术检索与证据边界

- 精确 DOI（裸 DOI、`doi:`、DOI URL）只查 Crossref 并核对返回标识；未知或不匹配不转为模糊标题检索。
- `references:<DOI|W-ID|OpenAlex URL>` / `citations:...` 验证 seed 后查询一跳边，最多 25 条，并核对引用方向。
- 生医学术查询增加 Europe PMC；普通主源无结果时允许有界 Crossref 元数据补源。原有 OpenAlex / Semantic Scholar 与可选 arXiv 保留；来源故障隔离。
- 保留日期精度及撤稿证据，不把只有年份/月的元数据变成精确发布日期；DOI 命中及引用计数不是论文论点核验。
- 健康检查面向实际 native APIs，401/429 不算健康。删除 manifest 和依赖目录中未使用的 paper-search-mcp 服务项；源码来源保留在上游检查记录中，不因缺少参考仓库而降级原生引擎。
- 可选 `OPENALEX_API_KEY` 仅经 Authorization header 发送。

### 4. 路由与成本边界

- 自动精确文献查询只走 academic，不调用付费通用搜索，也不以无关 web recovery 替代未知 DOI。显式 provider/mode 保持优先。
- RRF 对同 URL 的同一已知检索家族取最大贡献，不重复累加 GitHub/Skill 或已知社区包装源。路由质量合并已知 engine alias；这不是出版者独立性或事实交叉印证证明。
- 自动恢复只尝试未运行引擎；恢复、多样性补源共享剩余总预算。预算耗尽不启动新的 provider，也不人为给最后一次请求补足 100 ms。

## Jev 决定

对现有 `jevkit` 调用合同和 search gate 阅读后，实际调用 `jev-1.13.0` 验证了 3 个合成公开样例：学术选源、官方证据短名单、证据不足判断；耗时分别为 572 / 490 / 568 ms。未发送私密资料。

优先考虑公开证据短名单及规则无法消歧的选源；本轮**不默认加入 WRR 热路径**。精确 DOI、显式站点与失败/预算逻辑继续确定性执行。3 个样例不是质量基准；API 未返回账单价格，不承诺提速、降本比例。

## 源码与未采用项

完整 revision、许可证、实际阅读源码链接、采纳/暂缓决定见 [upstream-review.md](docs/upstream-review.md#630-实施记录)。采用 trafilatura 包，其余在 WRR 现有接口内独立实现，未 vendoring 上游代码。

Firecrawl/Crawl4AI 本轮未接入：先采用无新增服务的本地抽取，未声称已完成三者质量/成本基准。CN 热榜不是关键词搜索替代；机构数据库不假定访问授权。低星项目作为能力线索，不包装成热门项目。

## 实际验证

- 最终全量单测：`WRR_V6_ROUTER=0 .venv/bin/pytest tests/unit -q -o addopts= -p no:cacheprovider -o faulthandler_timeout=60 --tb=short` → **1649 passed, 15 skipped，102.43 s**。覆盖上游 envelope/plan、文章边界、精确标识与引用方向、同家族计票、恢复及预算。删除了随依赖退役而失效的固定清单数量断言，没有把旧数量重新钉成新数量。
- 本机安装的 6.3.0 CLI：真实抓取 PEP 20，保留 `Beautiful is better than ugly.`，返回 `httpx.trafilatura`、`completeness=unknown`。使用系统已有代理的显式 `WRR_FETCH_PROXY`，没有放宽非公网地址检查。
- 真实 Crossref：`10.1038/nature14539` 返回《Deep learning》及匹配 DOI；未知 DOI 返回无结果错误。自动选路的 diagnostics 只有 academic，`mode_reason=academic_lookup`。
- 真实 OpenAlex：同一 DOI 的 references 和 citations 都返回相应方向的论文；真实 Europe PMC 生医学术查询有结果。
- 实际 native doctor：OpenAlex、Crossref、Europe PMC 可用，Semantic Scholar 返回 429，整体诚实标为 `warn (3/4)`。移除未用 MCP 依赖后，academic 的 v6 静态状态为 configured/routable，不再因参考仓库缺失而降级。
- 本机真实 Bilibili 定向查询返回 2 条视频。macmini 旧 EN `3f7c8c5` 接受生成的 plan，Hacker News 查询返回并解析 6 条；仅运行查询，没有升级上游工作树。
- 本机安装元数据与 `wrr.__version__` 均为 6.3.0。editable 安装后的旧根目录 egg-info 已用 setuptools 重新生成。

OpenCLI v2 错误/结果合同的离线回归不等于 v2 实际登录验证；未声称知乎/微博、目标新版 last30days、机构源或完整跨宿主部署已验收。

## 升级方式与部署边界

安装新增依赖：`python -m pip install -e .`（开发环境可用 `'.[dev]'`）。非交互宿主需确保 last30days 找到 Python 3.12+，或显式设置 `WRR_LAST30DAYS_EN_PYTHON`。

本轮代码在本机工作树实现并安装验证；没有自动部署到 macmini，没有替换共享 OpenCLI/last30days 工作树，没有提交或推送。架构图维持已明确标注的 v6.1.2 历史快照；当前行为以 README、本记录和源码为准。
