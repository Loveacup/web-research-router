"""社区搜索聚合引擎（Phase 1）。

整合 OpenCLI 社区渠道 + last30days 技能，多源并行搜索 → 统一评分 → 去重 → 排序。

源家族：
  - OpenCLI 渠道（agent-reach 复用浏览器登录态）：reddit / twitter / xiaohongshu
    调用 `opencli <chan> search <query> -f json --limit N`，返回结构化 JSON 数组。
  - last30days（重型研究 CLI，按需启用）：last30days_en / last30days_cn
    调用 `python3 <last30days.py> --emit json --quick <query>`，解析其 clusters。

评分（与既有引擎一致的三维加权）：
    score = 0.40*engagement + 0.35*recency + 0.25*quality
  - engagement：点赞/分数（对数压缩，按源用不同上限归一）
  - recency   ：时间衰减（≤24h=1.0, 7d=0.7, 30d=0.3, >30d=0）
  - quality   ：评论/互动比例（comments/engagement）

去重：URL 规范化相等 或 标题 Jaccard 相似度 > 阈值。各源**独立失败**互不影响。

注：OpenCLI 渠道是经实测的快速核心；last30days 为重型工具（常超预算被跳过），
默认仅在 site:news.ycombinator.com|zhihu.com|weibo.com 触发或研究意图关键词时启用。
"""
import asyncio
import math
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .base import SearchEngine
from . import _fusion
from .community_sources import SOURCE_ADAPTERS as _SOURCE_ADAPTERS
from .. import config
from ..errors import EngineError
from ..schemas import SearchOptions, SearchResult, EngineCheckResult

_Scored = Tuple[float, SearchResult]

_L30_EN = os.environ.get("WRR_LAST30DAYS_EN") or os.path.expanduser(
    "~/code/last30days-skill/skills/last30days/scripts/last30days.py")
_L30_CN = os.environ.get("WRR_LAST30DAYS_CN") or os.path.expanduser(
    "~/code/last30days-skill-cn/skills/last30days/scripts/last30days.py")
_L30_EN_PYTHON = os.environ.get("WRR_LAST30DAYS_EN_PYTHON") or shutil.which("python3.12") or "python3"
_L30_CN_PYTHON = os.environ.get("WRR_LAST30DAYS_CN_PYTHON") or "python3"

# 源定义（字段名对齐各 CLI 实测 Output columns）。
COMMUNITY_SOURCES: Dict[str, Dict[str, Any]] = {
    "reddit": {
        "kind": "opencli", "cli": ["opencli", "reddit", "search"],
        "engagement": "score", "comments": "comments", "time": "created_utc",
        "title": "title", "url": "url", "snippet": "selftext", "eng_max": 10000,
    },
    "twitter": {
        "kind": "opencli", "cli": ["opencli", "twitter", "search"],
        "engagement": "likes", "comments": "replies", "time": "created_at",
        "title": "text", "url": "url", "snippet": "text", "eng_max": 10000,
    },
    "xiaohongshu": {
        "kind": "opencli", "cli": ["opencli", "xiaohongshu", "search"],
        "engagement": "likes", "comments": None, "time": "published_at",
        "title": "title", "url": "url", "snippet": "title", "eng_max": 10000,
    },
    "aihot_rss": {
        "kind": "rss",
        "feed_url": config.AIHOT_RSS_FEED,
        "engagement": None, "comments": None, "time": "published_at", "eng_max": 1000,
    },
    "wechat_rss": {
        "kind": "rss",
        "feed_url": config.WECHAT_RSS_FEEDS[0] if config.WECHAT_RSS_FEEDS else "",
        "feed_urls": config.WECHAT_RSS_FEEDS,
        "engagement": None, "comments": None, "time": "published_at", "eng_max": 1000,
    },
    "v2ex": {
        "kind": "opencli", "cli": ["opencli", "v2ex", "search"],
        "engagement": "replies", "comments": "replies", "time": "created",
        "title": "title", "url": "url", "snippet": "content", "eng_max": 1000,
    },
    "hackernews": {
        "kind": "opencli", "cli": ["opencli", "hackernews", "search"],
        "cli_extra_args": ["--sort", "date"],
        "backup_commands": [["opencli", "hackernews", "top"]],
        "backup_filter_by_query": True,
        "engagement": "score", "comments": "comments", "time": "time",
        "title": "title", "url": "url", "snippet": "title", "eng_max": 1000,
    },
    "last30days_en": {
        "kind": "last30days", "cli": [_L30_EN_PYTHON, _L30_EN],
        "engagement": "score", "comments": None, "time": None, "eng_max": 100,
    },
    "last30days_cn": {
        "kind": "last30days", "cli": [_L30_CN_PYTHON, _L30_CN],
        "engagement": "score", "comments": None, "time": None, "eng_max": 100,
    },
}


# ── 评分（纯函数，单测可直接调用）────────────────────────────────────
def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


_RELATIVE_TIME_RE = re.compile(
    r"\b(\d+)\s*(minute|hour|day|week|month|year)s?\s*ago\b", re.IGNORECASE
)
_MONTH_NAME_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y")
_SHORT_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y")
_RELATIVE_TIME_DELTAS = {
    "minute": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
    "month": "days",
    "year": "days",
}


def _parse_time(time_val) -> Optional[datetime]:
    """解析 Unix 秒/毫秒、ISO 8601 与常见搜索摘要中的英文日期。

    自然语言日期只接受带年份的绝对日期，避免把 ``Apr 28`` 这类缺少
    年份的内容错误归为当年；相对日期（如 ``2 hours ago``）以当前 UTC
    时间为基准。所有成功结果都归一化为带 UTC 时区的 datetime。
    """
    if isinstance(time_val, datetime):
        return time_val if time_val.tzinfo else time_val.replace(tzinfo=timezone.utc)
    if isinstance(time_val, (int, float)) and time_val > 0:
        ts = time_val / 1000 if time_val > 1e11 else time_val
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(time_val, str) or not time_val.strip():
        return None

    value = time_val.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    relative = _RELATIVE_TIME_RE.search(value)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        # Month/year use deliberately coarse day approximations for ranking only.
        kwargs = {_RELATIVE_TIME_DELTAS[unit]: amount * ({"month": 30, "year": 365}.get(unit, 1))}
        return datetime.now(timezone.utc) - timedelta(**kwargs)

    for fmt in _MONTH_NAME_DATE_FORMATS + _SHORT_DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        # Date strings found in snippets may be versions or unrelated numbers.
        if 1990 <= dt.year <= datetime.now(timezone.utc).year + 1:
            return dt.replace(tzinfo=timezone.utc)
    return None


def _recency_score(created: Optional[datetime], now: Optional[datetime] = None) -> float:
    """时间衰减：≤24h=1.0, ≤7d=0.7, ≤30d=0.3, 更旧=0；未知时间给中等分 0.5。"""
    if created is None:
        return 0.5
    now = now or datetime.now(timezone.utc)
    if isinstance(now, (int, float)):
        now = datetime.fromtimestamp(now, tz=timezone.utc)
    elif isinstance(now, datetime) and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # 防御性归一化 created（不依赖上游 _parse_time）
    if isinstance(created, (int, float)):
        try:
            ts = created / 1000 if created > 1e11 else created
            created = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return 0.5
    elif isinstance(created, datetime) and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_hours = (now - created).total_seconds() / 3600
    if age_hours <= 24:
        return 1.0
    if age_hours <= 168:
        return 0.7
    if age_hours <= 720:
        return 0.3
    return 0.0


def _engagement_score(value: int, max_ref: int = 1000) -> float:
    """参与度对数压缩到 [0,1]。"""
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1 + value) / math.log10(1 + max_ref))


def _quality_score(comments: int, engagement: int) -> float:
    """质量：评论/互动比例（20% 评论率 → 1.0）。"""
    if engagement <= 0:
        return 0.0
    return min(1.0, (comments / engagement) * 5)


def calculate_score(item: Dict[str, Any], source_config: Dict[str, Any],
                    now: Optional[datetime] = None) -> float:
    """统一三维加权综合分。权重取自 config.COMMUNITY_SCORE_WEIGHTS。"""
    eng = _to_int(item.get(source_config["engagement"], 0))
    cfield = source_config.get("comments")
    com = _to_int(item.get(cfield, 0)) if cfield else 0
    tval = item.get(source_config["time"]) if source_config.get("time") else None
    w_e, w_r, w_q = config.COMMUNITY_SCORE_WEIGHTS
    return (w_e * _engagement_score(eng, source_config.get("eng_max", 1000))
            + w_r * _recency_score(_parse_time(tval), now)
            + w_q * _quality_score(com, eng))


# ── 去重 ─────────────────────────────────────────────────────────────
def _normalize_url(url: str) -> str:
    url = (url or "").lower().strip().rstrip("/")
    url = re.sub(r"[?#].*$", "", url)
    return url


def _similarity(a: str, b: str) -> float:
    """标题 Jaccard 词集相似度（按 \\w+ 分词，忽略标点/emoji，CJK 友好）。"""
    a_set = set(re.findall(r"\w+", (a or "").lower()))
    b_set = set(re.findall(r"\w+", (b or "").lower()))
    if not a_set or not b_set:
        return 0.0
    return len(a_set & b_set) / len(a_set | b_set)


def deduplicate(results: List[SearchResult]) -> List[SearchResult]:
    """去重：URL 规范化相等 或 标题相似度 > 阈值。保留先到者（已按分降序）。"""
    unique: List[SearchResult] = []
    seen_urls = set()
    for r in results:
        nu = _normalize_url(r.url)
        if nu and nu in seen_urls:
            continue
        if any(_similarity(r.title, k.title) > config.COMMUNITY_DEDUP_THRESHOLD
               for k in unique):
            continue
        if nu:
            seen_urls.add(nu)
        unique.append(r)
    return unique


# ── 子进程（集中一处，便于单测 monkeypatch）──────────────────────────
async def _run_cmd(cli: List[str], timeout: float) -> Tuple[Optional[int], str, str]:
    """运行命令，返回 (returncode, stdout, stderr)；超时/异常返回 (None, '', '').

    自动在 PATH 头部注入 ~/.local/bin，确保非交互 shell 能找到 agent-reach/opencli。
    """
    env = os.environ.copy()
    local_bin = os.path.expanduser("~/.local/bin")
    current_path = env.get("PATH", "")
    parts = current_path.split(os.pathsep)
    if local_bin not in parts:
        env["PATH"] = os.pathsep.join([local_bin] + parts)
    else:
        env["PATH"] = current_path
    try:
        proc = await asyncio.create_subprocess_exec(
            *cli, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env)
    except (FileNotFoundError, OSError):
        return (None, "", "")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return (None, "", "")
    return (proc.returncode,
            out.decode("utf-8", errors="ignore"),
            err.decode("utf-8", errors="ignore"))


async def _probe_opencli_status(timeout: float = 2.0) -> Tuple[bool, str]:
    """Fast read-only probe of OpenCLI daemon + extension status."""
    rc, out, err = await _run_cmd(["opencli", "daemon", "status"], timeout=timeout)
    text = (out + err).lower()
    if rc != 0 or not text.strip():
        return (False, "opencli daemon not running")
    if "daemon: not running" in text or "not running" in text:
        return (False, "opencli daemon not running")
    disconnected_markers = ("disconnected", "not connected", "browser_connect",
                            "browser bridge extension not connected")
    if any(m in text for m in disconnected_markers):
        return (False, "OpenCLI browser extension not connected")
    if "daemon: running" in text and "extension: connected" in text:
        return (True, "opencli ready")
    return (False, f"opencli status unknown: {(out + err)[:200]}")


class CommunityEngine(SearchEngine):
    name = "community"
    tier = 2  # 本地 CLI 依赖

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        # H3 (v6.1): 搜索热路径不再预检/重启 OpenCLI daemon。daemon/extension
        # 连接性由 v6 registry 的 live_probe 健康检查负责（离线缓存 → routable）。
        sources = self._detect_sources(options.query)
        if not sources:
            raise EngineError(
                "community: no supported source for query; "
                "V2EX full-text search is not available via community/OpenCLI — "
                "use auto routing or an external web engine with site:v2ex.com"
            )
        now = datetime.now(timezone.utc)
        gathered = await asyncio.gather(
            *[self._fetch_source(s, options, now) for s in sources],
            return_exceptions=True,
        )
        merged: List[_Scored] = []
        for res in gathered:
            if isinstance(res, Exception) or not res:
                continue                                  # 各源独立失败
            merged.extend(res)
        if not merged:
            raise EngineError("community: all sources failed or returned no results")
        merged.sort(key=lambda t: t[0], reverse=True)
        deduped = deduplicate([sr for _, sr in merged])
        return deduped[:options.count]

    # ── v5.0：跨子源 RRF 聚合（源内秩 → RRF）+ canonical 去重 ──────────
    async def search_rrf(self, options: SearchOptions) -> List[SearchResult]:
        """v5 聚合：各子源源内排名 → 跨源 RRF（_fusion）→ canonical_url 去重。

        替代 search() 的「线性合并不可比分」做法（研究报告 §2）。源内仍用
        calculate_score 排序，magnitude 不出源；跨源用秩融合，多源命中自动加分。
        """
        # H3 (v6.1): 同 search()，热路径不做 daemon 预检/重启。
        sources = self._detect_sources(options.query)
        if not sources:
            raise EngineError(
                "community: no supported source for query; "
                "V2EX full-text search is not available via community/OpenCLI — "
                "use auto routing or an external web engine with site:v2ex.com"
            )
        now = datetime.now(timezone.utc)
        gathered = await asyncio.gather(
            *[self._fetch_source(s, options, now) for s in sources],
            return_exceptions=True,
        )
        per_source: Dict[str, List[SearchResult]] = {}
        for src, res in zip(sources, gathered):
            if isinstance(res, Exception) or not res:
                continue                                  # 各源独立失败隔离
            ranked = sorted(res, key=lambda t: t[0], reverse=True)   # 源内秩
            per_source[src] = [sr for _, sr in ranked]
        if not per_source:
            raise EngineError("community: all sources failed or returned no results")
        fused = _fusion.rrf_fuse(per_source, k=config.RRF_K)
        deduped = _fusion.dedup_cluster([f["doc"] for f in fused],
                                        config.COMMUNITY_DEDUP_THRESHOLD)
        return deduped[:options.count]

    # ── 源选择 ───────────────────────────────────────────────────────
    def _detect_sources(self, query: str) -> List[str]:
        q = (query or "").lower()
        picked: List[str] = []

        def add(s):
            if s not in picked:
                picked.append(s)

        # site: 触发 → 精确子集
        if "site:reddit.com" in q:
            add("reddit")
        if "site:twitter.com" in q or "site:x.com" in q:
            add("twitter")
        if "site:news.ycombinator.com" in q:
            add("hackernews")
        if "site:zhihu.com" in q or "site:weibo.com" in q:
            add("last30days_cn")
        # 平台关键词
        if any(k in q for k in ("小红书", "xiaohongshu", "xhs")):
            add("xiaohongshu")

        # P3: AI HOT 中文 AI 资讯
        if any(k in q for k in config.AIHOT_KEYWORDS):
            add("aihot_rss")

        # P3: WeChat RSS（用户已配置 feed 且查询显式提及）。
        # H3 blocker (OMP 2026-07-07): 必须同时有 feed 配置 + 关键词触发，
        # 否则空 source 会让唯一源调用走 EngineError("all sources failed") 误报。
        if config.WECHAT_RSS_FEEDS and any(k in q for k in config.WECHAT_KEYWORDS):
            add("wechat_rss")

        # P3-3: early-news 路由模式——命中宽泛新闻/早报意图时，主动拉取 RSS 源
        # 以覆盖默认社区源（reddit/twitter/xiaohongshu）可能错过的早期中文资讯。
        if config.early_news_triggered(q):
            add("aihot_rss")
            if config.WECHAT_RSS_FEEDS:
                add("wechat_rss")

        # V2EX has hot/latest/node APIs but no full-text search endpoint.
        # Let auto routing fall through to web engines for site:v2ex.com instead
        # of pretending `opencli v2ex hot/latest` is query search.
        if "v2ex" in q:
            return []

        if not picked:
            picked = list(config.COMMUNITY_DEFAULT_SOURCES)

        # 研究意图 / 显式开关 → 追加 last30days 重型源
        research = any(k in q for k in ("trending", "30 days", "30天", "最近", "本周", "this week"))
        if config.COMMUNITY_INCLUDE_LAST30DAYS or research:
            add("last30days_en")
            add("last30days_cn")
        return picked

    # ── 单源抓取（适配器分派 + 超时 + 异常隔离）─────────────────────
    async def _fetch_source(self, source: str, options, now) -> List[_Scored]:
        cfg = COMMUNITY_SOURCES.get(source)
        if not cfg:
            return []
        adapter = _SOURCE_ADAPTERS.get(cfg["kind"])
        if adapter is None:
            return []
        # H3 refactor: opencli preflight removed from search hot path.
        # Daemon/extension health is checked by health_check() only.
        try:
            # 在调用点解析模块级 _run_cmd，保留单测 monkeypatch 语义。
            items = await adapter.fetch(
                cfg, options, _run_cmd, config.COMMUNITY_SOURCE_TIMEOUT)
        except Exception:
            return []
        out: List[_Scored] = []
        for it in items:
            scored = self._item_to_result(it, source, cfg, now)
            if scored:
                out.append(scored)
        return out

    def _item_to_result(self, item, source, cfg, now) -> Optional[_Scored]:
        title = str(item.get(cfg.get("title", "title")) or item.get("title")
                    or item.get("text") or "").strip()
        url = str(item.get(cfg.get("url", "url")) or item.get("url") or "").strip()
        if not title or not url:
            return None
        snippet_field = cfg.get("snippet", "snippet")
        snippet = str(item.get(snippet_field) or item.get("selftext")
                      or item.get("text") or title or "")
        sc = calculate_score(item, cfg, now)
        return (sc, SearchResult(title=title[:200], url=url,
                                 snippet=snippet[:500], source_tag=source))

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        """检查 opencli 是否可用。

        P0 (deep=False): shutil.which 检查存在性 + 2s daemon/extension status probe.
        P1 (deep=True): 执行 --version + daemon status + extension 连接检查
        """
        from ._probe import probe_command

        # 快速检查：which
        opencli_path = shutil.which("opencli")
        if not opencli_path:
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary="opencli command not found",
                details="Community engine requires opencli CLI tool",
                requirements=["command:opencli"],
                repair=[
                    "Install opencli:",
                    "  npm install -g opencli",
                    "Or add opencli to your PATH if already installed",
                    "Verify: which opencli",
                    "Rerun: wrr-cli.py doctor --engine community",
                ],
                evidence={"command.opencli": "missing"},
            )

        # Light/deep 共同：1s daemon/extension 状态探测
        ok, reason = await _probe_opencli_status(timeout=1.0)
        if not ok:
            return EngineCheckResult(
                engine=self.name,
                status="degraded" if not deep else "unhealthy",
                tier=self.tier,
                summary=reason,
                details="OpenCLI daemon or browser extension is not connected; community sources cannot be used until the extension is reconnected.",
                requirements=["command:opencli", "opencli:extension-connected"],
                repair=[
                    "Open Chrome/Chromium and ensure the OpenCLI extension is enabled.",
                    "Then run: opencli daemon restart",
                    "Or restart Chrome if the extension is already enabled.",
                ],
                evidence={"command.opencli": opencli_path, "opencli.status": reason},
            )

        conn_detail = "opencli ready"

        # Deep 检查：执行 --version + daemon/extension 状态
        if deep:
            probe_result = await probe_command("opencli", ("--version",), timeout=3.0)
            if probe_result.status == "timeout":
                return EngineCheckResult(
                    engine=self.name,
                    status="fail",
                    tier=self.tier,
                    summary="opencli command timeout",
                    details="opencli --version timed out after 3s",
                    requirements=["command:opencli"],
                    repair=[
                        "Check if opencli is working:",
                        "  opencli --version",
                        "Reinstall if needed:",
                        "  npm install -g opencli",
                    ],
                    evidence={"command.opencli": opencli_path, "probe": "timeout"},
                )
            elif probe_result.status in ("broken", "error"):
                return EngineCheckResult(
                    engine=self.name,
                    status="fail",
                    tier=self.tier,
                    summary="opencli command broken",
                    details=probe_result.error or "opencli --version failed",
                    requirements=["command:opencli"],
                    repair=[
                        "Check if opencli is working:",
                        "  opencli --version",
                        "Reinstall if needed:",
                        "  npm install -g opencli",
                    ],
                    evidence={"command.opencli": opencli_path, "probe": probe_result.status, "exit_code": probe_result.exit_code},
                )
            conn_detail = f"opencli ready ({probe_result.stdout.strip() or 'version ok'})"

        details = f"opencli found at: {opencli_path}; {conn_detail}"
        if config.COMMUNITY_INCLUDE_LAST30DAYS:
            l30_issues = []
            for label, path in [("last30days_en", _L30_EN), ("last30days_cn", _L30_CN)]:
                if not os.path.exists(path):
                    l30_issues.append(f"{label} not found: {path}")
                elif deep:
                    # deep mode: probe 脚本可执行性
                    try:
                        result = await probe_command("python3", (path, "--help"), timeout=5.0)
                        if result.status != "ok":
                            l30_issues.append(f"{label} probe failed: {result.status}")
                        else:
                            details += f", {label} OK"
                    except Exception:
                        l30_issues.append(f"{label}: probe error")

            if l30_issues:
                return EngineCheckResult(
                    engine=self.name,
                    status="warn",
                    tier=self.tier,
                    summary="opencli OK, last30days scripts missing",
                    details="; ".join(l30_issues),
                    requirements=["command:opencli", "script:last30days"],
                    repair=[
                        "Clone last30days skills if needed:",
                        f"  Expected: {_L30_EN}",
                        f"  Expected: {_L30_CN}",
                        "Or set COMMUNITY_INCLUDE_LAST30DAYS=False to disable",
                    ],
                    evidence={"command.opencli": "present", "last30days": "missing"},
                )

        return EngineCheckResult(
            engine=self.name,
            status="ok",
            tier=self.tier,
            summary="opencli available",
            details=details,
            active_backend="opencli",
            evidence={
                "command.opencli": opencli_path,
                **({"extension.connectivity": conn_detail} if deep else {}),
            },
        )
