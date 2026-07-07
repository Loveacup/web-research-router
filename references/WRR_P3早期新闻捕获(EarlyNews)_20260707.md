---
status: 树苗
type: 规划
priority: 正常
aliases: [WRR P3 Early News Capture, WRR-P3-early-news, WRR早期新闻捕获]
tags: [type/规划, status/树苗, src/原创, topic/工程, ai/web-research-router]
related: "[[WRR]]"
created: 2026-07-07 22:40
modified: 2026-07-07 23:10
---

# WRR P3 早期新闻捕获（Early News Capture）

当前状态：[[web-research-router]] v6.1.1 已发布（P0/P1/P2 全部完成，已提交并 tag）。**P3-1 已实现并提交**。本规划作为 P3 阶段的 STDD/控制面文档，用于确认设计、记录调研、作为后续执行的母文。

## 一、Plan（计划）

P3 目标：为 WRR 增加**早期新闻 / 热点捕获**能力，分三路推进。

| 路线 | 目标 | 预期收益 | 优先级 | 状态 |
|---|---|---|---|---|
| P3-1 AI HOT 源适配器 | 接入 aihot.virxact.com 的中文 AI 资讯（无需 API Key） | 补强社区引擎对中文 AI 圈动态的覆盖 | 高 | ✅ 已提交 |
| P3-2 WeChat RSS 源 | 不直接爬取微信，而是通过 RSSHub / WeRSS / wewe-rss 生成的 RSS 接入公众号内容 | 获取中文高质量长文源，避开反爬 | 中 | 🔄 规划中 |
| P3-3 early-news 路由模式 | 新增聚合模式，把 AI HOT + WeChat + HN / Twitter / Reddit 等作为 early-signal 源 | 支持"今天 AI 圈发生了什么"类查询 | 中 | 🔄 规划中 |

范围边界：
- **纯加法**：不动 `router.py` 分发核心、`registry.py` 默认注册表、`deps.py`。
- **不引入浏览器自动化**：WeChat 走 RSS 而不是 [[Scrapling]] / Sogou 反爬。
- **无新增 API Key**：AI HOT 公开；WeChat RSS 依赖用户已有订阅。
- **可测试**：所有新增源走现有 `community_sources.py` adapter seam，保持可 monkeypatch 测试。
- **side-effect-free**：adapter 只读 HTTP/RSS，不启动浏览器、不写状态。

## 二、Process（过程/调研）

### 2.1 AI HOT 接口调研（已验证）

- 官网：aihot.virxact.com
- 提供 Skill / RSS / REST API / OpenAPI 3.1 接入，**无需 API Key**。
- 已存在社区生态：
  - khazix-skills/aihot（SKILL.md）
  - jing7ao/aihot-mcp（npm，TypeScript）
  - aihotradar-mcp（PyPI，Python MCP）
- **已实测 RSS 端点**（P3-1 实现）：
  - 精选：`https://aihot.virxact.com/feed.xml`
  - 全部动态：`https://aihot.virxact.com/feed/all.xml`
  - 日报：`https://aihot.virxact.com/feed/daily.xml`
  - 论文：`https://aihot.virxact.com/feed/category/paper.xml`
- 公开 API：
  - `https://aihot.virxact.com/api/public/items?mode=selected|all&since=...&category=...&q=...`
  - `https://aihot.virxact.com/api/public/daily`
  - `https://aihot.virxact.com/api/public/dailies`

### 2.2 WeChat

- 历史结论：Scrapling 对 Sogou WeChat 搜索不可用（加密链接 + antispider）。
- 可行方案：[[RSSHub]] / [[WeRSS]] / [[wewe-rss]] / [[we-mp-rss]] 将公众号转为 RSS，然后作为 RSS 源接入。
- 推荐首选：WeRSS 或 wewe-rss（私有化部署），输出稳定、可订阅。

### 2.3 WRR 当前架构适配点

- `wrr/engines/community_sources.py` 已定义 `CommunitySourceAdapter` seam，支持 `opencli` 和 `last30days` 两种 adapter。
- 新增 adapter 只需实现 `fetch(cfg, options, run_cmd, timeout)`，并在 `SOURCE_ADAPTERS` 注册。
- community engine 通过 `COMMUNITY_SOURCES` 字典声明 source 配置。
- routing 侧可在 `config.py` 增加触发词，或在 `resolve_mode` 中新增 `early-news` 分支。

## 三、Result（设计草案 + 已落地）

### 3.1 P3-1 AI HOT 源适配器 ✅ 已提交

实现摘要：
- 在 `wrr/engines/community_sources.py` 中新增 `RssSourceAdapter`（通用 RSS 适配器）。
- 通过 `cfg["feed_url"]` 获取 RSS XML，解析 `<item>` 为 `title`, `url`, `snippet`, `published_at`, `category`, `sources`。
- 注入 `cfg["client"]` 支持测试；生产环境使用 `httpx.AsyncClient`。
- 注册 `SOURCE_ADAPTERS["rss"] = RssSourceAdapter()`。
- 在 `wrr/engines/community.py` 新增 `aihot_rss` 源配置，默认 feed 为 `https://aihot.virxact.com/feed.xml`。
- 在 `wrr/config.py` 新增 `AIHOT_RSS_FEED` 与 `AIHOT_KEYWORDS` 触发词。
- `_detect_sources` 在命中 AI HOT 关键词时启用 `aihot_rss`。
- 新增测试 `tests/unit/test_rss_source_adapter.py`（5 个测试全部通过）。

关键字段映射：

| RSS 字段 | item 字段 | 说明 |
|---|---|---|
| `title` | `title` | 标题（CDATA 已解析） |
| `link` | `url` | 链接 |
| `description` | `snippet` | 摘要；AI HOT 的 `via AI HOT` 尾巴被截断 |
| `pubDate` | `published_at` | 解析为 ISO 8601 |
| `category` | `category` | 分类 |
| `author` | `sources` | 作为单元素列表 |

### 3.2 P3-2 WeChat RSS 源（框架已落地）

- 同 `RssSourceAdapter` 复用，新增 `wechat_rss` source。
- 用户通过 `WRR_WECHAT_RSS_FEEDS` 环境变量提供逗号分隔 feed URL 列表。
- 未配置时 `wechat_rss` 源 feed_url 为空，adapter 返回空列表，不阻塞其他源。
- 触发条件：查询含 `wechat`/`微信`/`公众号`/`weixin`/`we-mp-rss` **或** 用户已配置 feed。
- 测试已覆盖 WeChat RSS 解析（同 `test_rss_source_adapter.py`）。

### 3.3 P3-3 early-news 路由模式（待规划）

当前方案：
- 在 `wrr/config.py` 中新增 `EARLY_NEWS_KEYWORDS` 触发词（"今天 AI 圈"、"AI 日报"、"AI 热点"、"early news"、"发生了什么"、"今日热点"等）。
- 在 `classify_intent` / `resolve_mode` 中增加 `early-news` 分支：命中关键词 → 模式 `early-news`。
- 该模式使用社区引擎，但只启用 `aihot_rss`、`wechat_rss`、`last30days` 等 early-signal 源。
- 保持 `community` 模式不变；`early-news` 是其子集/特化。

### 3.4 关键接口/数据契约

```python
class RssSourceAdapter:
    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        ...
```

返回字段统一：

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | `str` | 标题 |
| `url` | `str` | 链接 |
| `snippet` | `str` | 摘要/描述 |
| `score` | `float` | 可选，默认 0 |
| `published_at` | `str` | 可选，ISO 8601 |
| `sources` | `List[str]` | 可选，如 AI HOT 的多信源 |

### 3.5 风险与假设

| 风险 | 缓解 |
|---|---|
| aihot.virxact.com API 未稳定/变更 | 使用 RSS 优先；API 调用做失败回退；字段缺失时容错 |
| WeChat RSS 源失效/反爬升级 | 用户自行维护 feed；adapter 失败不阻塞其他源 |
| early-news 模式与 community 模式重叠 | 用关键词和触发词区分；默认 community 模式不动 |
| 测试依赖外部网络 | 所有 adapter 使用可注入的 HTTP client |
| RSS 结果不相关于查询 | 社区引擎已有按源聚合；后续可加入 query-filter 后处理 |

### 3.6 改动文件（P3-1 已提交）

- `wrr/engines/community_sources.py`（+95 行：RssSourceAdapter）
- `wrr/engines/community.py`（+19 行：aihot_rss / wechat_rss 源 + 触发逻辑）
- `wrr/config.py`（+11 行：配置与触发词）
- `tests/unit/test_rss_source_adapter.py`（新增，5 测试）

## 四、验证

- `pytest tests/unit/test_rss_source_adapter.py` → **6 passed**（原 5 + blocker 回归 1 含 3 状态断言）
- `WRR_V6_ROUTER=0 pytest tests/unit -q` → **697 passed, 1 failed**
  - 失败项：`test_openalex_live_single_source`（外部 OpenAlex 429/timeout，与 P3-1 无关）
- 红线检查：`wrr/router.py`、`wrr/registry.py`、`wrr/deps.py` 自 v6.1.1 以来未改动（独立 git diff --name-only 取证）

### OMP 审计闭环（2026-07-07）

- **R1**（task_id=`omp-p31-audit`，bundle_only）：verdict=**blocker**
  - 唯一 evidence：criterion 6 不满足——`_detect_sources()` 在无 `WECHAT_RSS_FEEDS` 配置但查询含微信关键词时，`wechat_rss` 成为唯一 source → `RssSourceAdapter.fetch` 返回空 → `search()` 抛 `EngineError("community: all sources failed or returned no results")`。
  - ref：`wrr/engines/community.py:328-330`、`:338-339`、`:68-72`、`:254-264`、`wrr/engines/community_sources.py:143-145`
  - 处理：`omp-finish --reject`，round 计数 +1。
- **修复 commit**（`bcb73c2`）：将 `_detect_sources` 的 wechat_rss 触发改为 AND 短路——`if config.WECHAT_RSS_FEEDS and any(k in q for k in config.WECHAT_KEYWORDS)`。新增 `test_detect_sources_wechat_requires_both_keyword_and_feeds` 覆盖 keyword-only / feeds-only / both 三状态。
- **R3**（task_id=`omp-p31-r3`，bundle_only，scope 缩窄到 blocker 项）：verdict=**blocker（审计方法失效）**
  - OMP 在 scope 内确认 criterion 1（AND 短路语义正确，ref `community.py:331-332`）和 criterion 2（3 状态测试覆盖，ref `test_rss_source_adapter.py:157-168`）**通过**。
  - OMP 为取证 criterion 3（红线文件未改动）越界读取 `.git/logs/HEAD`、`.git/objects/...`、`.git/` grep —— 不在 allowed_paths 内，按"守 scope 即守独立"规则，越界部分作废。
  - 处理：`omp-finish --reject`（本轮 verdict 因越界污染不可采信）。
- **Hermes 独立取证 criterion 3**：
  ```bash
  git diff --name-only v6.1.1..HEAD
  # → references/WRR_P3早期新闻捕获(EarlyNews)_20260707.md
  # → tests/unit/test_rss_source_adapter.py
  # → wrr/config.py
  # → wrr/engines/community.py
  # → wrr/engines/community_sources.py
  # 三个红线文件均不在列表内，criterion 3 通过。
  ```
- **最终裁决**：P3-1 三个 criterion **全部通过**（criterion 1-2 由 OMP R3 在 scope 内独立裁决通过；criterion 3 由 Hermes 独立 git diff 取证通过）。P3-1 可视为审计闭合。
- **审计方法教训**（v0.7.x call-omp 实战新增）：
  - bundle_only 审计者取证"红线文件未改动"需要要么把红线文件加进 allowed_paths，要么由委派方在 evidence_bundle 中内联 `git diff --name-only` 输出。
  - R3 委派包应预填 `evidence_bundle.git_diff_name_only_path`，避免 OMP 自己跑 git 命令而越界。

## 五、Next step（下一步）

1. P3-1 审计闭合。
2. 启动 **P3-3 early-news 路由模式**（P3-2 框架已随 P3-1 落地）。
3. 非大节点不中断用户，但 P3-3 涉及 `classify_intent` 修改，需先确认本规划后再动手。
4. 后续 OMP 审计委派包默认在 evidence_bundle 中内联 `git diff --name-only` 和 `git status --short` 文本，避免审计者越界。

---

## 链接关系分析

- `[[web-research-router]]` → 定义与基线：本规划依附的仓库与版本基线
- `[[Scrapling]]` ⊗ 排除项：P3 明确不采用 Scrapling 直接爬取 WeChat
- `[[RSSHub]]` / `[[WeRSS]]` / `[[wewe-rss]]` → 外部 RSS 方案：WeChat 接入的推荐路径
- `[[WRR]]` → 项目入口：相关技术方案与项目入口
