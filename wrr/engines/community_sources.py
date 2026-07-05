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
from typing import Any, Awaitable, Callable, Dict, List, Protocol, Tuple, runtime_checkable

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

    _BROWSER_CONNECT_MARKERS = (
        "BROWSER_CONNECT",
        "Browser Bridge extension not connected",
        "extension not connected",
        "extension: disconnected",
    )

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


# kind → 适配器实例（无状态，可复用）
SOURCE_ADAPTERS: Dict[str, CommunitySourceAdapter] = {
    "opencli": OpenCliSourceAdapter(),
    "last30days": Last30DaysSourceAdapter(),
}
