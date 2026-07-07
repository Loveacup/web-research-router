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
import json
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
    """OpenCLI 渠道适配器（reddit / twitter / xiaohongshu / v2ex）。

    命令形状与解析逐字节保留自原 `_fetch_opencli`：
      `<cli...> <query> -f json --limit <min(count,20)>`，严格 json.loads。
    """

    async def fetch(self, cfg: Dict[str, Any], options: Any,
                    run_cmd: RunCmd, timeout: float) -> List[Dict[str, Any]]:
        cli = cfg["cli"] + [options.query, "-f", "json",
                            "--limit", str(min(options.count, 20))]
        rc, out, err = await run_cmd(cli, timeout)
        if rc != 0 or not out.strip():
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("results") or data.get("items") or []
        return []


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
