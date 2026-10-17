"""DeepSeek Search 引擎（自研搜索引擎，纯 JSON API，与 Exa 同级并行）。

  search → POST https://api.deepseek.com/v1/tools/search

映射：snippet ← summary[:EXA_SEARCH_TEXT_MAX]（面向模型的长摘要，与 Exa 的 text 截断
等长）；highlights 留空（上游无独立 highlights 字段）；date → source_ts。
rerank_score / authority 不进 schema，仅记日志。
query 硬限 128 Unicode 字符，超长截断。
"""
import logging
from datetime import datetime, timezone
from typing import List

import httpx

from .base import SearchEngine
from .. import config
from ..errors import EngineError, EngineTimeoutError, RateLimitError
from ..schemas import SearchOptions, SearchResult, EngineCheckResult

log = logging.getLogger(__name__)

DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
DEEPSEEK_QUERY_MAX = 128      # API 限制：1–128 Unicode 字符
DEEPSEEK_COUNT_MAX = 50       # API 限制：1–50


def _parse_ts(val) -> float:
    """RFC 3339 → unix 时间戳；null/无法解析 → 0。"""
    if not val or not isinstance(val, str):
        return 0.0
    try:
        dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _to_results(items: List[dict]) -> List[SearchResult]:
    out = []
    for r in items:
        ts = _parse_ts(r.get("date"))
        out.append(SearchResult(
            title=r.get("title", "") or "",
            url=r.get("url", "") or "",
            snippet=(r.get("summary", "") or r.get("snippet", "") or "")[:config.EXA_SEARCH_TEXT_MAX],
            source_ts=ts,
            freshness_score=1.0 if ts > 0 else 0.5,
        ))
    return out


class DeepSeekEngine(SearchEngine):
    name = "deepseek"
    tier = 1

    def _key(self) -> str:
        key = config.get_env("DEEPSEEK_API_KEY")
        if not key:
            raise EngineError("DEEPSEEK_API_KEY not set")
        return key

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        key = self._key()
        query = options.query.strip()
        if len(query) > DEEPSEEK_QUERY_MAX:
            log.info("deepseek: query truncated %d → %d chars", len(query), DEEPSEEK_QUERY_MAX)
            query = query[:DEEPSEEK_QUERY_MAX]
        count = max(1, min(options.count, DEEPSEEK_COUNT_MAX))
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    DEEPSEEK_SEARCH_URL,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"query": query, "count": count, "need_content": False},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                raise RateLimitError(f"DeepSeek rate limited (HTTP {status})") from e
            raise EngineError(f"DeepSeek search failed (HTTP {status})") from e
        except httpx.TimeoutException as e:
            raise EngineTimeoutError(f"DeepSeek timeout: {e}") from e
        except httpx.HTTPError as e:
            raise EngineError(f"DeepSeek transport error: {e}") from e
        items = data.get("results", []) or []
        if items and log.isEnabledFor(logging.DEBUG):
            log.debug("deepseek: %d results, rerank=%s authority=%s", len(items),
                      [round(float(r.get("rerank_score", 0) or 0), 2) for r in items],
                      [r.get("authority", -1) for r in items])
        return _to_results(items)

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        if not config.get_env("DEEPSEEK_API_KEY"):
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary="DEEPSEEK_API_KEY not configured",
                requirements=["env:DEEPSEEK_API_KEY"],
                repair=[
                    "Set DEEPSEEK_API_KEY in your shell or ~/.hermes/.env:",
                    "  export DEEPSEEK_API_KEY=your_key_here",
                    "Rerun: wrr-cli.py doctor --engine deepseek",
                ],
                evidence={"env.DEEPSEEK_API_KEY": "missing"},
            )
        return EngineCheckResult(
            engine=self.name,
            status="ok",
            tier=self.tier,
            summary="DEEPSEEK_API_KEY configured",
            active_backend="deepseek-search-api",
            evidence={"env.DEEPSEEK_API_KEY": "present"},
        )
