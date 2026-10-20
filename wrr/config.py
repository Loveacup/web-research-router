"""WRR 配置常量。集中管理 fallback 顺序、超时、预算、引擎参数。

从单文件 __init__.py 搬迁而来（v4.0）。所有 volatile 阈值集中于此，便于调参。
"""
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Mapping
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def _env_float(key: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _env_int(key: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _env_bool_from_resolver(key: str, env_resolver: Callable[[str], Optional[str]], default: bool = False) -> bool:
    """从自定义 env_resolver 解析布尔环境变量（用于测试 monkeypatch）。"""
    raw = env_resolver(key)
    if raw is None or raw == "":
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


# ── Fallback 顺序（与历史 extension.ts / v3 __init__.py 一致）─────────────
SEARCH_FALLBACK_ORDER = ("exa", "deepseek", "brave", "github", "community", "searxng")
EXTRACT_FALLBACK_ORDER = ("exa", "brave")   # web_fetch：exa 干净正文 → brave 裸抓取
SIMILAR_PROVIDERS = ("exa",)                 # findSimilar 仅 Exa 支持

# ── 单引擎超时（秒）──────────────────────────────────────────────────
GITHUB_TIMEOUT = _env_float("WRR_GITHUB_TIMEOUT", 20.0)
GITHUB_CLIENT_TIMEOUT = _env_float("WRR_GITHUB_CLIENT_TIMEOUT", GITHUB_TIMEOUT)
GITHUB_ACTIVITY_LOOKUP_TIMEOUT = _env_float("WRR_GITHUB_ACTIVITY_TIMEOUT", 3.0)
GITHUB_ACTIVITY_CONCURRENCY = _env_int("WRR_GITHUB_ACTIVITY_CONCURRENCY", 5)
GITHUB_ACTIVITY_LOOKUP = _env_bool("WRR_GITHUB_ACTIVITY_LOOKUP", True)

EXA_TIMEOUT = _env_float("WRR_EXA_TIMEOUT", 30.0)
DEEPSEEK_TIMEOUT = _env_float("WRR_DEEPSEEK_TIMEOUT", 10.0)

ENGINE_TIMEOUT = {
    "exa": EXA_TIMEOUT,
    "deepseek": DEEPSEEK_TIMEOUT,
    "brave": 10.0,
    "searxng": 10.0,
    "github": GITHUB_TIMEOUT,
    "community": 20.0,
}
DEFAULT_ENGINE_TIMEOUT = 15.0

# ── 预算：fallback 链总响应上限（秒）──────────────────────────────────
TOTAL_BUDGET_SECONDS = 10.0          # search / similar：交互式，求快
# extract 例外：Exa /contents 正文抽取端点可能慢到接近引擎超时（30s），
# 10s 总预算会把 exa 卡在 timeout 并跳过 brave 兜底（budget exceeded）。
# 给 extract 单独的宽预算：容纳 exa 30s engine timeout + brave 10s 兜底。
EXTRACT_BUDGET_SECONDS = 40.0
OPERATION_BUDGET_SECONDS = {
    "search": TOTAL_BUDGET_SECONDS,
    "similar": TOTAL_BUDGET_SECONDS,
    "extract": EXTRACT_BUDGET_SECONDS,
}


# ── 结果数量 / 字符上限 ──────────────────────────────────────────────
DEFAULT_SEARCH_COUNT = 10
MAX_SEARCH_COUNT = 20
DEFAULT_MAX_CHARACTERS = 5000
MAX_MAX_CHARACTERS = 50000

# ── SearXNG：默认引擎(google/ddg/startpage)已失效，仅 bing/baidu 存活；
#    必须显式 language，否则跨语言噪音（见 references/searxng-engine-diagnostics.md）──
SEARXNG_ENGINES = "bing,baidu"
SEARXNG_LANGUAGE = "zh-CN"

# ── Exa：search 随结果带 highlights（官方推荐），作为 citation 源片段 ──
EXA_SEARCH_TEXT_MAX = 500          # search 结果 snippet 上限
EXA_SEARCH_TYPE = "auto"           # neural / keyword / auto
EXA_WITH_HIGHLIGHTS = True

# ── Exa 模式自动路由（质量优先）──────────────────────────────────────
# 查询分类 → 模式选择
EXA_MODE_ROUTING = {
    "factual": "fast",        # 简单事实查询 → 快速响应
    "standard": "auto",       # 标准搜索 → 自动平衡
    "research": "deep-lite",  # 深度研究 → 深度模式
    "academic": "deep",       # 学术/技术深度 → 最深模式
}

# 查询分类关键词
EXA_ACADEMIC_KEYWORDS = ["论文", "综述", "survey", "methodology", "algorithm",
                       "paper", "arxiv", "doi", "citation", "preprint",
                       "peer review", "literature", "state of the art"]
EXA_RESEARCH_KEYWORDS = ["深度", "详细", "全面", "比较", "对比", "分析", "research", "overview", "comparison",
                         "comprehensive", "analysis", "deep", "in-depth", "compare",
                         "优化", "进展", "演进", "设计", "方案", "架构", "实现", "改进",
                         "vs", "versus", "取舍", "选型", "最佳实践", "最新进展"]
EXA_FACTUAL_KEYWORDS = ["什么时候", "多少", "是谁", "日期", "版本", "release", "launch", "price"]

# 各模式超时（秒）
EXA_MODE_TIMEOUT = {
    "fast": _env_float("WRR_EXA_MODE_FAST_TIMEOUT", 3.0),
    "auto": _env_float("WRR_EXA_MODE_AUTO_TIMEOUT", 5.0),
    "deep-lite": _env_float("WRR_EXA_MODE_DEEPLITE_TIMEOUT", 8.0),
    "deep": _env_float("WRR_EXA_MODE_DEEP_TIMEOUT", max(15.0, EXA_TIMEOUT / 2)),
}

# ── GitHub 引擎（P1 轻量版：activity + popularity + freshness）─────────
GITHUB_SCORE_WEIGHTS = (0.40, 0.35, 0.25)     # (activity, popularity, freshness)




def github_fast_mode(env_resolver: Optional[Callable[[str], Optional[str]]] = None) -> bool:
    """查询 GITHUB_FAST_MODE 环境变量（跳过 activity lookup）。

    支持运行时可测试的 env_resolver 注入（用于 monkeypatch）。
    """
    if env_resolver is None:
        env_resolver = os.environ.get
    return (_env_bool_from_resolver("GITHUB_FAST_MODE", env_resolver)
            or _env_bool_from_resolver("WRR_GITHUB_FAST_MODE", env_resolver))




# ── 社区聚合引擎（Phase 1：OpenCLI 渠道 + last30days）─────────────────
COMMUNITY_SCORE_WEIGHTS = (0.40, 0.35, 0.25)   # (engagement, recency, quality)
COMMUNITY_SOURCE_TIMEOUT = 20.0                # 每源超时（秒）；OpenCLI/browser-backed sources can exceed 15s
COMMUNITY_TOTAL_TIMEOUT = 15.0                 # 总超时（秒）
COMMUNITY_DEFAULT_SOURCES = ("reddit", "twitter", "xiaohongshu")
COMMUNITY_INCLUDE_LAST30DAYS = False           # 重型源；默认仅 site:hn/zhihu/weibo 或研究关键词时启用
COMMUNITY_DEDUP_THRESHOLD = 0.80               # 标题相似度去重阈值

# Community RSS source defaults (P3-1: AI HOT, P3-2: WeChat RSS)
AIHOT_RSS_FEED: str = os.environ.get("WRR_AIHOT_RSS_FEED", "https://aihot.virxact.com/feed.xml")
WECHAT_RSS_FEEDS: Tuple[str, ...] = tuple(
    filter(None, os.environ.get("WRR_WECHAT_RSS_FEEDS", "").split(","))
)
AIHOT_KEYWORDS = ("aihot", "ai hot", "今天 ai", "ai 日报", "ai 热点", "ai 圈", "ai 资讯",
                  "ai 动态", "ai 新闻", "ai 行业", "模型发布", "产品发布")
WECHAT_KEYWORDS = ("wechat", "微信", "公众号", "weixin", "we-mp-rss")

# P3-3: early-news 模式触发词（AI/热点/早报/新闻意图）。只保留与 AI 强相关的
# 复合词，避免误伤 `site:news.ycombinator.com` 等社区过滤查询。
EARLY_NEWS_KEYWORDS = (
    "ai 早报", "ai 早報", "ai 热点", "ai hot", "aihot", "ai 圈", "ai 资讯",
    "ai 新闻", "ai 动态", "ai 行业", "ai news", "latest ai", "最新 ai", "今天 ai",
    "模型发布", "产品发布", "最新发布", "今日发布",
)


def early_news_triggered(query: str) -> bool:
    """P3-3: 查询是否含早期新闻/AI热点/早报意图。"""
    q = (query or "").lower()
    return any(kw.lower() in q for kw in EARLY_NEWS_KEYWORDS)



# ── Policy-sensitive 机器信号 ─────────────────────────────────────────
# 识别政策/法规/监管/政府/法律等高风险查询，仅输出布尔信号供机器消费（如
# formatter 输出契约）；不改变任何路由选择。
# 含 false-positive guards：RL 的 "policy gradient"、办公
# "legal pad"、普通 timeout 等不得触发。
POLICY_SENSITIVE_CN_KEYWORDS = ("政策", "法规", "法律", "监管", "政府", "合规",
                                "立法", "条例", "法案", "规章", "备案", "执法")
POLICY_SENSITIVE_EN_KEYWORDS = ("policy", "policies", "regulation", "regulations",
                                "regulatory", "legislation", "legislative",
                                "legal", "government", "governmental",
                                "compliance", "statute", "statutory")
# guard 短语：命中即先剔除，避免其中的关键词被误判为政策语义
POLICY_SENSITIVE_GUARD_PHRASES = ("policy gradient", "legal pad", "legal-size",
                                  "legal size", "legal paper", "legally blonde")


def policy_sensitive_triggered(query: str) -> bool:
    """查询是否触及政策/法规/监管/政府/法律等高风险语义（纯机器信号）。

    仅输出布尔信号；不改变路由结果。
    英文关键词按词边界匹配（避免 substring 误伤），中文按子串匹配；guard
    短语先行剔除，规避 "policy gradient" / "legal pad" 等假阳性。
    """
    import re
    q = (query or "").lower()
    for phrase in POLICY_SENSITIVE_GUARD_PHRASES:
        if phrase in q:
            q = q.replace(phrase, " ")
    if any(re.search(rf"\b{re.escape(kw)}\b", q, flags=re.ASCII)
           for kw in POLICY_SENSITIVE_EN_KEYWORDS):
        return True
    return any(kw in q for kw in POLICY_SENSITIVE_CN_KEYWORDS)


USER_AGENT = "wrr/7.0"

# ── MCP 备份：保留 mcp_* 工具作为备份，默认不自动调用，仅在输出附 hint ──
BACKUP_HINT = (
    "需要更强单引擎能力时可显式调用 MCP 备份工具："
    "mcp_exa_* / mcp_brave_search_* / mcp_tavily_* / mcp_searxng_*"
)



RRF_K = 60                                     # Cormack 2009 默认，业界标准
FUSION_DEFAULTS = {
    "rrf_k": RRF_K,
    "dedup_key": "canonical_url",
    "weights_merge_policy": "multiply",
}

# ── 本地搜索层配置（v5.2）────────────────────────────────────────────
LOCAL_MAX_RESULTS_PER_ENGINE = 10       # 单引擎结果上限（与 count 取 min）

# Obsidian 文件扫描限流，避免全盘扫描。
LOCAL_OBSIDIAN_MAX_FILES = 2000         # 单次扫描文件数上限
LOCAL_OBSIDIAN_MAX_BYTES = 65536        # 单文件读取前缀字节上限
LOCAL_OBSIDIAN_EXCLUDE_DIRS = (".git", ".obsidian", "node_modules", ".trash",
                               ".smart-env", "attachments")


def obsidian_vault_paths() -> List[Path]:
    """解析 Obsidian vault 路径列表。

    来源：env WRR_OBSIDIAN_VAULTS（os.pathsep 分隔，支持 ~ 展开）。
    doctor 不假定默认 vault 存在；未配置 → 返回空列表（health_check fail）。
    """
    raw = get_env("WRR_OBSIDIAN_VAULTS")
    if not raw:
        return []
    return [Path(os.path.expanduser(p.strip()))
            for p in raw.split(os.pathsep) if p.strip()]



# ── 学术引擎（v5.0）──────────────────────────────────────────────────
ACADEMIC_SCORE_WEIGHTS = (0.35, 0.25, 0.20, 0.20)  # velocity/authority/recency/relevance
ACADEMIC_DEFAULT_SOURCES = ("openalex", "semantic_scholar")
ACADEMIC_INCLUDE_ARXIV = False
ACADEMIC_SOURCE_TIMEOUT = 8.0
ACADEMIC_RECENCY_HALFLIFE_DAYS = 365.0
OPENALEX_MAILTO = "anyisouth@gmail.com"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
EUROPE_PMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SEMANTIC_SCHOLAR_API = "https://api.semanticscholar.org/graph/v1/paper/search"
ARXIV_API = "http://export.arxiv.org/api/query"

# ── Skill 发现引擎（v2，P2b）────────────────────────────────────────
SKILL_CODE_SEARCH_PATH = "filename:SKILL.md"
SKILL_EXCLUDE_DIRS = ("template/", "spec/", ".system/", "examples/")
# 可信来源：skill 主题的厂商/工具所有者发布的官方仓库；多个 repo: 限定在一次 code search 中为 OR。
SKILL_TRUSTED_SOURCES = tuple(
    s.strip() for s in os.environ.get(
        "WRR_SKILL_TRUSTED_SOURCES",
        "anthropics/skills,kepano/obsidian-skills,firecrawl/cli,vercel-labs/skills",
    ).split(",") if s.strip()
)
SKILL_GENERAL_PER_PAGE = 50
SKILL_TRUSTED_PER_PAGE = 20
SKILL_PATH_PER_PAGE = 30       # 反馈词目录名检索
SKILL_REPO_PER_PAGE = 10       # 反馈词仓库检索（按 star）
SKILL_ENRICH_TOP_K = 6        # 只为前 K 组取 SKILL.md
SKILL_REPO_META_CAP = 10      # 仓库元数据（stars）调用上限（仅非可信仓库）
SKILL_RESERVED_SLOTS = 2      # K 截断外预留补全槽（选择性可信 → 仓库通道）
SKILL_SELECTIVE_TRUSTED_MAX = 3  # 可信检索命中 ≤3 条时豁免证据门

# ── GitHub v5 评分（加法式：4 维 + maintenance，不改 v4 三维）──────────
GITHUB_SCORE_WEIGHTS_V5 = (0.30, 0.30, 0.20, 0.20)  # activity/popularity/freshness/maintenance
GITHUB_NEGLECT_RATIO = 0.10                      # open_issues/stars >10% → 被忽视


_SOURCE_ENV: ContextVar[Mapping[str, str] | None] = ContextVar("wrr_source_env", default=None)
_SOURCE_DEADLINE: ContextVar[float | None] = ContextVar("wrr_source_deadline", default=None)


@contextmanager
def source_environment(env: Mapping[str, str], *, deadline: float | None = None):
    """Bind credentials and a monotonic deadline, including worker threads."""
    token = _SOURCE_ENV.set(env)
    deadline_token = _SOURCE_DEADLINE.set(deadline)
    try:
        yield
    finally:
        _SOURCE_ENV.reset(token)
        _SOURCE_DEADLINE.reset(deadline_token)


def source_timeout(default: float) -> float:
    """Use the core's remaining budget, retaining legacy budgets outside it."""
    deadline = _SOURCE_DEADLINE.get()
    return default if deadline is None else max(0.001, deadline - time.monotonic())


def get_env(key: str) -> Optional[str]:
    """Read the current source snapshot, or the lazy host environment."""
    env = _SOURCE_ENV.get()
    return (os.environ if env is None else env).get(key)


def budget_for(operation: str) -> float:
    """按操作返回 fallback 链总预算（未知操作回退 TOTAL_BUDGET_SECONDS）。"""
    return OPERATION_BUDGET_SECONDS.get(operation, TOTAL_BUDGET_SECONDS)
