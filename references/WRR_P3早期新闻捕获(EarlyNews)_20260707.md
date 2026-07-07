---
status: 树苗
type: 规划
priority: 正常
aliases: [WRR P3 Early News Capture, WRR-P3-early-news, WRR早期新闻捕获]
tags: [type/规划, status/树苗, src/原创, topic/工程, ai/web-research-router]
related: "[[WRR]]"
created: 2026-07-07 22:40
modified: 2026-07-07 22:40
---

# WRR P3 早期新闻捕获（Early News Capture）

当前状态：[[web-research-router]] v6.1.1 已发布（P0/P1/P2 全部完成，已提交并 tag）。本规划作为 P3 阶段的 STDD/控制面文档，用于确认设计、记录调研、作为后续执行的母文。

## 一、Plan（计划）

P3 目标：为 WRR 增加**早期新闻 / 热点捕获**能力，分三路推进。

| 路线 | 目标 | 预期收益 | 优先级 |
|---|---|---|---|
| P3-1 AI HOT 源适配器 | 接入 aihot.virxact.com 的中文 AI 资讯（无需 API Key） | 补强社区引擎对中文 AI 圈动态的覆盖 | 高 |
| P3-2 WeChat RSS 源 | 不直接爬取微信，而是通过 RSSHub / WeRSS / wewe-rss 生成的 RSS 接入公众号内容 | 获取中文高质量长文源，避开反爬 | 中 |
| P3-3 early-news 路由模式 | 新增聚合模式，把 AI HOT + WeChat + HN / Twitter / Reddit 等作为 early-signal 源 | 支持"今天 AI 圈发生了什么"类查询 | 中 |

范围边界：
- **纯加法**：不动 `router.py` 分发核心、`registry.py` 默认注册表、`deps.py`。
- **不引入浏览器自动化**：WeChat 走 RSS 而不是 [[Scrapling]] / Sogou 反爬。
- **无新增 API Key**：AI HOT 公开；WeChat RSS 依赖用户已有订阅。
- **可测试**：所有新增源走现有 `community_sources.py` adapter seam，保持可 monkeypatch 测试。
- **side-effect-free**：adapter 只读 HTTP/RSS，不启动浏览器、不写状态。

## 二、Process（过程/调研）

### 2.1 AI HOT

- 官网：aihot.virxact.com
- 提供 Skill / RSS / REST API / OpenAPI 3.1 接入，**无需 API Key**。
- 已存在社区生态：
  - khazix-skills/aihot（SKILL.md）
  - jing7ao/aihot-mcp（npm，TypeScript）
  - aihotradar-mcp（PyPI，Python MCP）
- 内容：每日 AI 热点、精选、模型发布、行业动态（中文为主）。

### 2.2 WeChat

- 历史结论：Scrapling 对 Sogou WeChat 搜索不可用（加密链接 + antispider）。
- 可行方案：[[RSSHub]] / [[WeRSS]] / [[wewe-rss]] / [[we-mp-rss]] 将公众号转为 RSS，然后作为 RSS 源接入。
- 推荐首选：WeRSS 或 wewe-rss（私有化部署），输出稳定、可订阅。

### 2.3 WRR 当前架构适配点

- `wrr/engines/community_sources.py` 已定义 `CommunitySourceAdapter` seam，支持 `opencli` 和 `last30days` 两种 adapter。
- 新增 adapter 只需实现 `fetch(cfg, options, run_cmd, timeout)`，并在 `SOURCE_ADAPTERS` 注册。
- community engine 通过 builtin `engine.yaml` manifest 声明 source 配置。
- routing 侧可在 `config.py` 增加触发词，或在 `resolve_mode` 中新增 `early-news` 分支。

## 三、Result（设计草案）

### 3.1 P3-1 AI HOT 源适配器

1. 在 `wrr/engines/community_sources.py` 中新增 `AihotSourceAdapter`：
   - 调用 `aihot.virxact.com` 的公开 RSS / API 端点（如 `/feed` 或 `/api/daily`）。
   - 解析为 `List[Dict[str, Any]]`，字段：`title`, `url`, `snippet`, `score`, `published_at`, `sources`。
   - 使用 `httpx.AsyncClient` 而不是 `run_cmd`，但保持 `run_cmd` 参数签名兼容。

2. 注册：
   ```python
   SOURCE_ADAPTERS: Dict[str, CommunitySourceAdapter] = {
       "opencli": OpenCliSourceAdapter(),
       "last30days": Last30DaysSourceAdapter(),
       "aihot": AihotSourceAdapter(),
   }
   ```

3. 在 community builtin engine manifest 中新增 `aihot` source 配置（默认禁用，仅在 `early-news` 模式或用户显式开启时启用）。

4. 测试：`tests/unit/test_aihot_source.py` 使用 `FakeAsyncClient` 模拟响应，断言解析字段和失败回退。

### 3.2 P3-2 WeChat RSS 源

1. 新增 `WechatRssSourceAdapter`：
   - 接受 `cfg["feed_url"]`，请求 RSS XML。
   - 解析 `<item>` 为 `title`, `url`, `snippet`（description 截断），`published_at`。
   - 失败时返回空列表，不阻塞其他源。

2. 配置：用户通过环境变量 `WRR_WECHAT_RSS_FEEDS` 提供 feed_url 列表；不提供时 adapter 为空操作。

3. 测试：`tests/unit/test_wechat_rss_source.py` 用 RSS XML 字符串 mock 响应。

### 3.3 P3-3 early-news 路由模式

1. 在 `wrr/config.py` 中：
   - 新增 `COMMUNITY_EARLY_NEWS_SOURCES = ("aihot", "wechat_rss", "hackernews", "twitter", "reddit")`。
   - 新增 `EARLY_NEWS_KEYWORDS` 触发词："今天 AI 圈"、"AI 日报"、"AI 热点"、"early news"、"发生了什么"、"今日热点"等。

2. 在 `resolve_mode` / `classify_intent` 中增加 `early-news` 分支：
   - 命中关键词 → 模式 `early-news`。
   - 该模式使用社区引擎，但只启用 `aihot`, `wechat_rss`, `last30days` 等 early-signal 源。

3. 保持 `community` 模式不变；`early-news` 是其子集/特化。

### 3.4 关键接口/数据契约

```python
class AihotSourceAdapter:
    async def fetch(self, cfg, options, run_cmd, timeout) -> List[Dict[str, Any]]:
        ...

class WechatRssSourceAdapter:
    async def fetch(self, cfg, options, run_cmd, timeout) -> List[Dict[str, Any]]:
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
| 测试依赖外部网络 | 所有 adapter 使用可注入的 HTTP client / run_cmd |

### 3.6 预计改动文件

- `wrr/engines/community_sources.py`（新增两个 adapter）
- `wrr/engines/community.py`（注册 source、处理 adapter 返回字段）
- `wrr/engines/builtin/community/engine.yaml`（新增 source 配置）
- `wrr/config.py`（新增触发词、early-news 配置）
- `wrr/router.py`（可选：仅在 `resolve_mode` 中增加 early-news 分支；纯加法）
- `tests/unit/test_aihot_source.py`（新增）
- `tests/unit/test_wechat_rss_source.py`（新增）
- `tests/unit/test_router_modes.py`（可能补充 early-news 模式断言）
- `RELEASE_NOTES_v6.1.1.md` 或 `RELEASE_NOTES_v6.2.0.md`（后续更新）

## 四、Next step（下一步）

1. 用户确认本规划后，启动 **P3-1 AI HOT 源适配器**实现。
2. 执行路径：Codex 规划（可选）→ CC 小粒度执行 → OMP 审计 → 测试验证。
3. 非大节点不中断用户，但 WeChat RSS 和 early-news 模式涉及设计取舍，需要在本规划确认后再动手。

---

## 链接关系分析

- `[[web-research-router]]` → 定义与基线：本规划依附的仓库与版本基线
- `[[Scrapling]]` ⊗ 排除项：P3 明确不采用 Scrapling 直接爬取 WeChat
- `[[RSSHub]]` / `[[WeRSS]]` / `[[wewe-rss]]` → 外部 RSS 方案：WeChat 接入的推荐路径
- `[[WRR]]` → 项目入口：相关技术方案与项目入口
