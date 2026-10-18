"""Community CLI adapters with versioned wire-format and failure handling.

Commands run through the injected runner; adapters never launch browsers,
change credentials, or relax the caller's environment/permission boundaries.
"""
import asyncio
import enum
import json
from dataclasses import dataclass, field
import re
from pathlib import Path
from urllib.parse import urlsplit
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
      - EMPTY          ：调用成功且结构化结果为空，可继续常规 backup。
      - UNAUTHENTICATED：命中登录墙 / AUTH_REQUIRED，需要人工重新登录。
      - SOFT_BLOCKED   ：403 / 验证码 / 限流等软封锁。

    UNAUTHENTICATED / SOFT_BLOCKED 是「立即返回」信号：不再尝试 backup，
    并由上层 CommunityEngine 的 source-local TTL breaker 决定是否短期跳过。
    """

    READY = "ready"
    EMPTY = "empty"
    UNAUTHENTICATED = "unauthenticated"
    SOFT_BLOCKED = "soft_blocked"
    TIMEOUT = "timeout"
    BRIDGE_DISCONNECTED = "bridge_disconnected"
    NETWORK_ERROR = "network_error"
    PLATFORM_ERROR = "platform_error"
    SCHEMA_ERROR = "schema_error"
    ERROR = "error"
    PERMISSION_DENIED = "permission_denied"
    SKIPPED = "skipped"
    CUTOFF = "cutoff"


@dataclass
class SourceFetchResult:
    """单源抓取结果封装：结构化条目 + 可靠性分类。"""

    items: List[Dict[str, Any]] = field(default_factory=list)
    outcome: SourceOutcome = SourceOutcome.EMPTY
    detail: Optional[str] = None


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
_BRIDGE_MARKERS = (
    "browser_connect", "extension disconnected", "extension not connected",
    "browser bridge extension not connected", "daemon not running",
)
_NETWORK_MARKERS = (
    "econnreset", "enetunreach", "ehostunreach", "network error",
    "connection reset", "connection refused", "dns lookup", "timed out",
)
_PLATFORM_MARKERS = (
    "selector not found", "element not found", "page redesign",
    "unexpected page", "unsupported page layout",
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


def _classify_failure(rc: Any, out: str, err: str) -> Optional[SourceOutcome]:
    """把非阻断失败归一为稳定的请求级诊断类别。"""
    if rc is None:
        return SourceOutcome.TIMEOUT
    text = "\n".join(
        part for part in (err, out if rc != 0 else "") if part
    ).lower()
    if any(marker in text for marker in _BRIDGE_MARKERS):
        return SourceOutcome.BRIDGE_DISCONNECTED
    if any(marker in text for marker in _NETWORK_MARKERS):
        return SourceOutcome.NETWORK_ERROR
    if any(marker in text for marker in _PLATFORM_MARKERS):
        return SourceOutcome.PLATFORM_ERROR
    if rc != 0:
        return SourceOutcome.ERROR
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


def _failure_envelope(data: Any) -> Optional[SourceOutcome]:
    """Classify explicit failures without scanning successful source content."""
    if not isinstance(data, dict) or "ok" not in data:
        return None
    if type(data["ok"]) is not bool:
        return SourceOutcome.SCHEMA_ERROR
    if data["ok"]:
        return None
    error = data.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return SourceOutcome.SCHEMA_ERROR
    code = error["code"].lower()
    if code in ("auth_required", "login_wall", "auth-failed"):
        return SourceOutcome.UNAUTHENTICATED
    if code in ("rate_limit", "rate_limited", "soft_blocked", "captcha"):
        return SourceOutcome.SOFT_BLOCKED
    if code in ("timeout", "cdp_timeout"):
        return SourceOutcome.TIMEOUT
    if code == "browser_connect":
        return SourceOutcome.BRIDGE_DISCONNECTED
    if code == "selector":
        return SourceOutcome.PLATFORM_ERROR
    text = " ".join(str(error.get(key) or "") for key in ("code", "message"))
    return (_classify_block(1, text, "")
            or _classify_failure(1, text, "")
            or SourceOutcome.ERROR)


def _http_url(value: Any) -> str:
    """Accept absolute HTTP(S) URLs, never opaque item/candidate identifiers."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() in ("http", "https") and parsed.hostname
                and not any(char.isspace() for char in value)):
            return value
    except ValueError:
        pass
    return ""


@runtime_checkable
class CommunitySourceAdapter(Protocol):
    """单源抓取接口：给定源配置 + 查询 → 原始 item dict 列表。

    实现须自我隔离失败为返回值（空列表），不得抛异常到聚合层以外。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        ...


class OpenCliSourceAdapter:
    """OpenCLI keyword searches; supports v1 rows and v2 result envelopes.

    Auth/soft-block failures stop immediately; ordinary failures may try the
    source's explicitly configured backup command.
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        """向后兼容入口：返回裸 item 列表（丢弃 outcome 分类）。"""
        result = await self.fetch_result(cfg, options, run_cmd, timeout)
        return result.items

    async def fetch_result(self, cfg: Dict[str, Any], options: Any,
                           run_cmd: RunCmd, timeout: float) -> SourceFetchResult:
        """Fetch rows and retain failures, including rc=0 error envelopes."""
        commands = [cfg["cli"]] + cfg.get("backup_commands", [])
        last_failure: Optional[SourceOutcome] = None
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
            # Parse explicit envelopes even when a wrapper loses the exit code.
            try:
                data = json.loads(out) if out.strip() else None
            except json.JSONDecodeError:
                data = None
            envelope_failure = _failure_envelope(data)
            blocked = _classify_block(rc, out, err)
            if blocked is not None:
                return SourceFetchResult(items=[], outcome=blocked)
            if envelope_failure is not None:
                if envelope_failure in (SourceOutcome.UNAUTHENTICATED, SourceOutcome.SOFT_BLOCKED):
                    return SourceFetchResult(items=[], outcome=envelope_failure)
                last_failure = envelope_failure
                continue
            failure = _classify_failure(rc, out, err)
            if failure is not None:
                last_failure = failure
                continue
            if isinstance(data, dict) and data.get("ok") is True:
                data = data.get("data")
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict) and ("results" in data or "items" in data):
                items = data.get("results") if "results" in data else data["items"]
            else:
                items = None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                last_failure = SourceOutcome.SCHEMA_ERROR
                continue
            if not items:
                continue
            if is_backup and cfg.get("backup_filter_by_query"):
                q = (options.query or "").lower()
                items = [it for it in items if q in str(it.get(cfg.get("title", "title"), "")).lower()]
            if not items:
                continue
            # P3-2: HN 时效回填
            if cli[:3] == ["opencli", "hackernews", "search"]:
                items = await self._enrich_hn_time(items)
            return SourceFetchResult(items=items[:options.count],
                                     outcome=SourceOutcome.READY)
        return SourceFetchResult(
            items=[], outcome=last_failure or SourceOutcome.EMPTY)

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
    """Normalize agent results, debug candidates, and legacy EN/CN arrays."""

    _PLATFORMS = (
        "reddit", "x", "twitter", "youtube", "hackernews", "digg", "techmeme",
        "devto", "bluesky", "truthsocial", "stocktwits", "dripstack", "arxiv",
        "github", "polymarket", "grounding", "perplexity", "weibo",
        "xiaohongshu", "bilibili", "zhihu", "wechat", "baidu", "douyin", "toutiao",
    )
    _STATUS_OUTCOMES = {
        "auth-failed": SourceOutcome.UNAUTHENTICATED,
        "rate-limited": SourceOutcome.SOFT_BLOCKED,
        "timeout": SourceOutcome.TIMEOUT,
        "unreachable": SourceOutcome.NETWORK_ERROR,
        "schema-drift": SourceOutcome.SCHEMA_ERROR,
        "payment-required": SourceOutcome.PERMISSION_DENIED,
        "error": SourceOutcome.ERROR,
        "skipped-unconfigured": SourceOutcome.SKIPPED,
    }

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        return (await self.fetch_result(cfg, options, run_cmd, timeout)).items

    def _query_plan(self, query: str, optional_sources: Tuple[str, ...] = ()) -> Optional[Dict[str, Any]]:
        """A literal retrieval plan, not LLM expansion or invented entity handles.

        Contract: upstream 0341304 lib/planner.py validate_external_plan.
        EN does not implement arbitrary site filtering or Chinese-only sources.
        """
        q = query.lower()
        domain_sources = {
            "reddit.com": "reddit", "x.com": "x", "twitter.com": "x",
            "news.ycombinator.com": "hackernews", "youtube.com": "youtube",
            "youtu.be": "youtube", "dev.to": "devto", "techmeme.com": "techmeme",
            "github.com": "github",
        }
        sources = []
        sites = re.findall(r"(?<!\S)site:([^\s]+)", q)
        for site in sites:
            source = domain_sources.get(site.removeprefix("www."))
            if not source or (source in ("techmeme", "devto") and source not in optional_sources):
                return None
            if source not in sources:
                sources.append(source)
        if len(sources) > 2:
            return None
        if not sources:
            if re.search(r"知乎|微博|小红书|哔哩哔哩|b站|\b(?:zhihu|weibo|xiaohongshu|bilibili|v2ex)\b", q):
                return None
            for pattern, source in (
                (r"\breddit\b", "reddit"), (r"\b(?:twitter|tweets?)\b", "x"),
                (r"\b(?:hackernews|hacker news)\b", "hackernews"),
                (r"\byoutube\b", "youtube"), (r"\bdev\.to\b", "devto"),
                (r"\btechmeme\b", "techmeme"), (r"\bgithub\b", "github"),
            ):
                if re.search(pattern, q):
                    sources.append(source)
            if len(sources) > 2:
                return None
            if any(source in ("techmeme", "devto") and source not in optional_sources for source in sources):
                return None
        if not sources:
            if ("techmeme" in optional_sources
                    and re.search(r"\b(?:news|headlines|breaking)\b|新闻|资讯", q)
                    and re.search(r"\b(?:ai|tech|technology|apple|google|microsoft|openai)\b|人工智能|科技", q)):
                sources = ["techmeme", "hackernews"]
            elif ("devto" in optional_sources
                    and re.search(r"\b(?:python|javascript|typescript|programming|coding|developer|software|api)\b|编程|开发", q)):
                sources = ["hackernews", "devto"]
            else:
                sources = ["hackernews", "reddit"]
        search_query = re.sub(r"(?<!\S)site:[^\s]+", "", query, flags=re.IGNORECASE).strip()
        if not search_query:
            return None
        return {
            "intent": "factual", "freshness_mode": "balanced_recent", "cluster_mode": "none",
            "subqueries": [{"search_query": search_query, "ranking_query": query,
                            "sources": sources, "weight": 1.0}],
        }

    async def fetch_result(self, cfg: Dict[str, Any], options: Any,
                           run_cmd: RunCmd, timeout: float) -> SourceFetchResult:
        cli = cfg["cli"] + ["--emit", "json", "--quick"]
        if cfg.get("query_plan"):
            # New sources are optional on old installs. This only checks code
            # presence, never imports upstream code or probes credentials.
            lib = Path(cfg["cli"][-1]).expanduser().parent / "lib"
            optional_sources = tuple(source for source in ("devto", "techmeme")
                                     if (lib / f"{source}.py").is_file())
            plan = self._query_plan(options.query, optional_sources)
            if plan is None:
                return SourceFetchResult(outcome=SourceOutcome.SKIPPED,
                                         detail="unsupported_source_scope")
            # --search also makes old sanitizers fail closed if a planned
            # source is unavailable, instead of widening to unrelated sources.
            cli.extend(["--plan", json.dumps(plan, ensure_ascii=False),
                        "--search", ",".join(plan["subqueries"][0]["sources"])])
        cli.append(options.query)
        rc, out, err = await run_cmd(cli, timeout)
        blocked = _classify_block(rc, out, err)
        if blocked is not None:
            return SourceFetchResult(outcome=blocked)
        try:
            data = _json_object_from_stdout(out)
        except json.JSONDecodeError:
            return SourceFetchResult(outcome=_classify_failure(rc, out, err)
                                     or SourceOutcome.SCHEMA_ERROR)
        failure = _failure_envelope(data) or _classify_failure(rc, out, err)
        if failure is not None:
            return SourceFetchResult(outcome=failure)
        if not isinstance(data, dict):
            return SourceFetchResult(outcome=SourceOutcome.SCHEMA_ERROR)
        if data.get("error") or data.get("success") is False:
            return SourceFetchResult(outcome=SourceOutcome.ERROR)
        try:
            items = self._parse_items(data)
        except (TypeError, ValueError):
            return SourceFetchResult(outcome=SourceOutcome.SCHEMA_ERROR)
        if items:
            return SourceFetchResult(items=items, outcome=SourceOutcome.READY)
        status = data.get("source_status") or {}
        if not isinstance(status, dict):
            return SourceFetchResult(outcome=SourceOutcome.SCHEMA_ERROR)
        for value in status.values():
            state = value.get("state") if isinstance(value, dict) else value
            if isinstance(state, str) and state in self._STATUS_OUTCOMES:
                return SourceFetchResult(outcome=self._STATUS_OUTCOMES[state])
        if data.get("errors_by_source"):
            return SourceFetchResult(outcome=SourceOutcome.ERROR)
        return SourceFetchResult(outcome=SourceOutcome.EMPTY)

    @staticmethod
    def _rows(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise ValueError("expected object array")
        return value

    @classmethod
    def _normalize(cls, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = _http_url(row.get("url") or row.get("link"))
        title = row.get("title") or row.get("text") or row.get("why_relevant")
        if not isinstance(title, str) or not title.strip() or not url:
            return None
        score = row.get("score") or row.get("final_score") or row.get("relevance") or 0
        if isinstance(row.get("relevance_score"), (int, float)):
            score = row["relevance_score"] * 100
        return {
            "title": title, "url": url,
            # Source summaries remain snippets, never verified claim fields.
            "snippet": row.get("summary") or row.get("snippet") or row.get("description")
                       or row.get("body") or row.get("why_relevant") or "",
            "score": score, "published_at": row.get("published_at") or row.get("date"),
            "_source": (row.get("source") or row.get("_source"))
                       if (row.get("source") or row.get("_source")) in cls._PLATFORMS else None,
        }

    def _parse_items(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        if "results" in data:
            rows = self._rows(data["results"])
            items = [item for row in rows if (item := self._normalize(row))]
            if rows and not items:
                raise ValueError("no usable normalized results")
            return items
        rows: List[Dict[str, Any]] = []
        recognized = False
        for key in ("ranked_candidates",) + self._PLATFORMS:
            if key in data:
                recognized = True
                for row in self._rows(data[key]):
                    rows.append(row if key == "ranked_candidates" else dict(row, _source=key))
        if "items_by_source" in data:
            recognized = True
            grouped = data["items_by_source"]
            if not isinstance(grouped, dict):
                raise ValueError("expected source map")
            for source, values in grouped.items():
                rows.extend(dict(row, _source=source) for row in self._rows(values))
        clusters = self._rows(data.get("clusters", []))
        recognized = recognized or "clusters" in data
        by_id = {}
        for row in rows:
            for key in ("candidate_id", "item_id", "id"):
                value = row.get(key)
                if isinstance(value, (str, int)):
                    by_id[str(value)] = row
        items = []
        seen_urls = set()
        for cluster in clusters:
            ids = cluster.get("representative_ids") or cluster.get("candidate_ids") or []
            if not isinstance(ids, list) or any(not isinstance(value, (str, int)) for value in ids):
                raise ValueError("expected cluster identifiers")
            for value in ids:
                row = by_id.get(str(value))
                # Old exports sometimes used an absolute URL as the ID.
                if row is None and _http_url(value):
                    row = {"url": value, "title": cluster.get("title"),
                           "score": cluster.get("score")}
                item = self._normalize(row) if row else None
                if item:
                    item["title"] = cluster.get("title") or item["title"]
                    if item["url"] not in seen_urls:
                        items.append(item)
                        seen_urls.add(item["url"])
                    break
        for row in rows:
            item = self._normalize(row)
            if item and item["url"] not in seen_urls:
                items.append(item)
                seen_urls.add(item["url"])
        if not recognized or ((rows or clusters) and not items):
            raise ValueError("unrecognized or unusable result structure")
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


from .mcp_source import McpSourceAdapter

# kind → 适配器实例（无状态，可复用）
SOURCE_ADAPTERS: Dict[str, CommunitySourceAdapter] = {
    "opencli": OpenCliSourceAdapter(),
    "last30days": Last30DaysSourceAdapter(),
    "rss": RssSourceAdapter(),
    "mcp": McpSourceAdapter(),
}
