"""社区源适配器接缝（Seam，Slice 1）。

把 CommunityEngine 内联的单源抓取（原 `_fetch_opencli` / `_fetch_last30days`）
抽成显式适配器（Adapter），为后续 v6.x 路线图的浏览器兜底源留出接口（Interface）
与接缝（Seam）。**本切片只做结构化重构，不引入任何浏览器自动化。**

设计契约：
  - 适配器仅封装既有 CLI 命令构造 + JSON 解析，行为与重构前等价。
  - `run_cmd` 由调用方注入（保留 community 模块级 `_run_cmd` 的可 monkeypatch 语义），
    适配器自身不持有子进程实现，也不导入 community（避免循环依赖）。
  - 禁止在本模块引入任何浏览器自动化框架或浏览器启动逻辑。
"""
import asyncio
import enum
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

try:
    import xml.etree.ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover
    ET = None  # type: ignore

# run_cmd 注入契约：(cli, timeout) -> (returncode, stdout, stderr)
RunCmd = Callable[[List[str], float], Awaitable[Tuple[Any, str, str]]]


class SourceOutcome(enum.Enum):
    """单源抓取的可靠性分类（P0 reliability slice）。

    仅刻画「这次抓取发生了什么」，不含任何恢复/重启动作：
      - READY          ：成功且拿到结构化条目。
      - EMPTY          ：调用成功但无结果（或普通失败），可继续常规 backup。
      - UNAUTHENTICATED：命中登录墙 / AUTH_REQUIRED，需要人工重新登录。
      - SOFT_BLOCKED   ：403 / 验证码 / 限流等软封锁。

    UNAUTHENTICATED / SOFT_BLOCKED 是「立即返回」信号：不再尝试 backup，
    并由上层 CommunityEngine 的 source-local TTL breaker 决定是否短期跳过。
    """

    READY = "ready"
    EMPTY = "empty"
    UNAUTHENTICATED = "unauthenticated"
    SOFT_BLOCKED = "soft_blocked"


@dataclass
class SourceFetchResult:
    """单源抓取结果封装：结构化条目 + 可靠性分类。"""

    items: List[Dict[str, Any]] = field(default_factory=list)
    outcome: SourceOutcome = SourceOutcome.EMPTY


# 从 stderr（必要时含 stdout）识别登录墙 / 软封锁的标记（全小写子串匹配）。
_UNAUTH_MARKERS = (
    "auth_required", "auth required", "authentication required",
    "login required", "login wall", "please log in", "please login",
    "not logged in", "sign in required", "unauthenticated", "unauthorized",
)
_SOFTBLOCK_MARKERS = (
    "captcha", "验证码", "rate limit", "ratelimit", "rate-limit",
    "too many requests", "429", "403", "forbidden",
    "soft block", "soft-block", "temporarily blocked", "blocked by",
)


def _classify_block(rc: Any, out: str, err: str) -> Optional[SourceOutcome]:
    """从 exit/stderr/stdout 识别 UNAUTHENTICATED / SOFT_BLOCKED。

    成功（rc==0）的 stdout 只可能是结构化载荷，不纳入阻断判定，避免 JSON
    内容里出现 ``403``/``forbidden`` 等字样被误判。仅在失败（rc!=0）时才把
    stdout 一并纳入扫描。返回 None 表示不是阻断类结果。
    """
    text = (err or "").lower()
    if rc != 0:
        text += "\n" + (out or "").lower()
    if not text.strip():
        return None
    if any(m in text for m in _UNAUTH_MARKERS):
        return SourceOutcome.UNAUTHENTICATED
    if any(m in text for m in _SOFTBLOCK_MARKERS):
        return SourceOutcome.SOFT_BLOCKED
    return None


def _json_object_from_stdout(out: str) -> Any:
    """Parse JSON payload from agent-facing CLIs that also print progress.

    last30days variants may write human logs to stdout before the final JSON
    object. Search paths should consume the structured payload instead of
    treating mixed stdout as a hard failure.
    """
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        pass
    start = out.find("{")
    end = out.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object found", out, 0)
    return json.loads(out[start:end + 1])


@runtime_checkable
class CommunitySourceAdapter(Protocol):
    """单源抓取接口：给定源配置 + 查询 → 原始 item dict 列表。

    实现须自我隔离失败为返回值（空列表），不得抛异常到聚合层以外。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        ...


class OpenCliSourceAdapter:
    """OpenCLI 渠道适配器（reddit / twitter / xiaohongshu / v2ex / hackernews）。

    命令形状与解析逐字节保留自原 `_fetch_opencli`：
      `<cli...> <query> -f json --limit <min(count,20)>`，严格 json.loads。
    支持 `backup_commands` 配置：当 primary 失败/空时依次执行 backup CLI。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        """向后兼容入口：返回裸 item 列表（丢弃 outcome 分类）。"""
        result = await self.fetch_result(cfg, options, run_cmd, timeout)
        return result.items

    async def fetch_result(self, cfg: Dict[str, Any], options: Any,
                           run_cmd: RunCmd, timeout: float) -> SourceFetchResult:
        """带可靠性分类的抓取：命令形状/解析与既有 `fetch` 逐字节等价。

        新增语义：任一命令返回登录墙 / 软封锁标记时立即短路返回对应 outcome，
        不再尝试 backup；普通 empty / 失败仍照旧继续 backup。
        """
        commands = [cfg["cli"]] + cfg.get("backup_commands", [])
        for idx, cli in enumerate(commands):
            is_backup = idx > 0
            if is_backup and not options.query:
                continue
            # backup 命令通常不含 <query> 位置参数；仍用 options.query 做本地标题过滤。
            # 只有 primary search 接收 query；top/new 等 backup 命令没有位置参数。
            call = cli + ([] if is_backup else [options.query])
            # 支持 cli_extra_args（如 HN 的 --sort date）
            if not is_backup and cfg.get("cli_extra_args"):
                call.extend(cfg["cli_extra_args"])
            call.extend(["-f", "json", "--limit", str(min(options.count * 3 if is_backup else options.count, 20))])
            rc, out, err = await run_cmd(call, timeout)
            blocked = _classify_block(rc, out, err)
            if blocked is not None:
                return SourceFetchResult(items=[], outcome=blocked)  # 立即返回
            if rc != 0 or not out.strip():
                continue
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else (data.get("results") or data.get("items") or [])
            if not items:
                continue
            if is_backup and cfg.get("backup_filter_by_query"):
                q = (options.query or "").lower()
                items = [it for it in items if q in str(it.get(cfg.get("title", "title"), "")).lower()]
            # P3-2: HN 时效回填
            if cli[:3] == ["opencli", "hackernews", "search"]:
                items = await self._enrich_hn_time(items)
            return SourceFetchResult(items=items[:options.count],
                                     outcome=SourceOutcome.READY)
        return SourceFetchResult(items=[], outcome=SourceOutcome.EMPTY)

    async def _enrich_hn_time(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Best-effort 回填 HN item 的 time 字段（从 Firebase API）。

        仅处理有效 id（URL 格式 https://news.ycombinator.com/item?id=<num>）。
        并发限制 5、超时 1.5s、失败保持原 item。
        """
        if httpx is None:
            return items

        import re
        id_pattern = re.compile(r"news\.ycombinator\.com/item\?id=(\d+)")

        # OpenCLI search 返回 item.id；top 兼容旧形状时再从 URL 回退提取。
        tasks = []
        seen_ids = set()
        for i, item in enumerate(items):
            raw_id = item.get("id")
            item_id = str(raw_id) if isinstance(raw_id, (int, str)) else ""
            if not item_id.isdigit():
                match = id_pattern.search(str(item.get("url", "")))
                item_id = match.group(1) if match else ""
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            # OpenCLI HN search 行只含 id/title；构造官方 item 的 canonical URL，
            # 让聚合层不会因缺 url 丢弃这条已检索到的 story。
            if not str(item.get("url") or "").strip():
                item["url"] = f"https://news.ycombinator.com/item?id={item_id}"
            tasks.append((item_id, i))

        if not tasks:
            return items

        # 并发回填（限制 5 并发、单请求超时 1.5s）
        sem = asyncio.Semaphore(5)

        async def fetch_time(item_id: str, idx: int):
            async with sem:
                try:
                    async def _do_fetch():
                        async with httpx.AsyncClient(timeout=1.5) as client:
                            resp = await client.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
                            resp.raise_for_status()
                            data = resp.json()
                            if "time" in data and data["time"]:
                                items[idx]["time"] = data["time"]
                    await asyncio.wait_for(_do_fetch(), timeout=1.5)
                except Exception:
                    pass  # best-effort,失败保持原样

        await asyncio.gather(*[fetch_time(tid, tidx) for tid, tidx in tasks], return_exceptions=True)
        return items


class Last30DaysSourceAdapter:
    """last30days 重型研究 CLI 适配器（last30days_en / last30days_cn）。

    命令形状与 clusters/平台数组解析逐字节保留自原 `_fetch_last30days`：
      `<cli...> --emit json --quick <query>`，容忍混合 stdout（进度日志 + JSON）。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        cli = cfg["cli"] + ["--emit", "json", "--quick", options.query]
        rc, out, _err = await run_cmd(cli, timeout)
        if rc != 0 or not out.strip():
            return []
        try:
            data = _json_object_from_stdout(out)
        except json.JSONDecodeError:
            return []
        items: List[Dict[str, Any]] = []
        for c in (data.get("clusters") or []):
            ids = c.get("representative_ids") or c.get("candidate_ids") or []
            url = ids[0] if ids else ""
            title = c.get("title") or ""
            if not (title and url):
                continue
            items.append({
                "title": title, "url": url,
                "snippet": "sources: " + ", ".join(c.get("sources") or []),
                "score": c.get("score") or 0,
            })
        if items:
            return items
        for platform in ("weibo", "xiaohongshu", "bilibili", "zhihu", "wechat",
                         "baidu", "douyin", "toutiao"):
            for row in (data.get(platform) or []):
                title = row.get("title") or row.get("text") or row.get("why_relevant") or ""
                url = row.get("url") or row.get("link") or ""
                if not (title and url):
                    continue
                snippet = row.get("snippet") or row.get("description") or row.get("why_relevant") or ""
                items.append({
                    "title": title,
                    "url": url,
                    "snippet": snippet,
                    "score": row.get("score") or row.get("relevance") or 0,
                })
        return items


class RssSourceAdapter:
    """通用 RSS 源适配器。

    用于接入公开 RSS feed（如 AI HOT 精选/日报、WeChat 公众号 RSS）。
    通过 `cfg["feed_url"]` 获取 XML，解析 `<item>` 列表。
    适配器自我隔离失败：网络/解析异常返回空列表。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        feed_url = cfg.get("feed_url")
        if not feed_url:
            return []
        if ET is None:
            return []
        try:
            client = cfg.get("client")
            if client is not None:
                r = await client.get(feed_url, headers={"User-Agent": "wrr/4.0"})
                r.raise_for_status()
                text = r.text
            elif httpx is not None:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.get(feed_url, headers={"User-Agent": "wrr/4.0"})
                    r.raise_for_status()
                    text = r.text
            else:
                return []
        except Exception:
            return []
        return self._parse_rss(text)

    def _parse_rss(self, text: str) -> List[Dict[str, Any]]:
        if not text or not ET:
            return []
        try:
            root = ET.fromstring(text.encode("utf-8"))
        except ET.ParseError:
            return []
        channel = root.find("channel")
        if channel is None:
            return []
        items: List[Dict[str, Any]] = []
        for item in channel.findall("item"):
            title = self._text(item, "title")
            link = self._text(item, "link")
            desc = self._text(item, "description")
            if not (title and link):
                continue
            snippet = desc or ""
            # AI HOT 的 description 通常包含 via AI HOT 链接和原文链接，截断到更干净的摘要
            if "via AI HOT" in snippet:
                snippet = snippet.split("via AI HOT")[0].strip()
            items.append({
                "title": title,
                "url": link,
                "snippet": snippet,
                "published_at": self._parse_pub_date(self._text(item, "pubDate")),
                "category": self._text(item, "category"),
                "sources": [self._text(item, "author")] if self._text(item, "author") else None,
            })
        return items

    def _text(self, item: Any, tag: str) -> Optional[str]:
        el = item.find(tag)
        if el is None or not el.text:
            return None
        return el.text.strip()

    def _parse_pub_date(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        # RSS pubDate 常见格式：Mon, 06 Jul 2026 17:45:30 GMT
        # 直接保留原字符串，让下游 _parse_time 处理 ISO / 秒 / 字符串回退
        try:
            from datetime import datetime
            dt = datetime.strptime(value, "%a, %d %b %Y %H:%M:%S %Z")
            return dt.isoformat()
        except ValueError:
            return value


# kind → 适配器实例（无状态，可复用）
SOURCE_ADAPTERS: Dict[str, CommunitySourceAdapter] = {
    "opencli": OpenCliSourceAdapter(),
    "last30days": Last30DaysSourceAdapter(),
    "rss": RssSourceAdapter(),
}
