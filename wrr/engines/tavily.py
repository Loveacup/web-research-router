"""Tavily 引擎：fail-closed native search adapter（POST /search）。

契约：仅 search + health_check。缺 key / HTTP 错误一律 fail-closed，绝不返回伪成功。
- 429/432 → RateLimitError（可触发 fallback）；其他 HTTP / transport error → EngineError。
- router backfill 是下一 slice，本文件不碰 MODE_DISPATCH/weights/route order。
"""
import httpx
from typing import List

from .base import SearchEngine
from .. import config
from ..errors import EngineError, RateLimitError
from ..schemas import SearchOptions, SearchResult, EngineCheckResult

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilyEngine(SearchEngine):
    name = "tavily"
    tier = 1

    def _key(self) -> str:
        key = config.get_env("TAVILY_API_KEY")
        if not key:
            raise EngineError("TAVILY_API_KEY not set")
        return key

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        key = self._key()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    TAVILY_SEARCH_URL,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"},
                    json={
                        "query": options.query,
                        "search_depth": "basic",
                        "max_results": min(options.count, 20),
                        "include_answer": False,
                        "include_raw_content": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (429, 432):
                raise RateLimitError(f"Tavily rate limited (HTTP {status})") from e
            raise EngineError(f"Tavily search failed (HTTP {status})") from e
        except httpx.HTTPError as e:
            # transport/timeout：fail-closed，不伪成功
            raise EngineError(f"Tavily transport error: {e}") from e

        return [SearchResult(title=r.get("title", "") or "",
                             url=r.get("url", "") or "",
                             snippet=r.get("content", "") or "")
                for r in data.get("results", [])]

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        """检查 TAVILY_API_KEY 是否存在（缺失 fail-closed）。"""
        key = config.get_env("TAVILY_API_KEY")
        if not key:
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary="TAVILY_API_KEY not configured",
                requirements=["env:TAVILY_API_KEY"],
                repair=[
                    "Set TAVILY_API_KEY in your shell or ~/.hermes/.env:",
                    "  export TAVILY_API_KEY=your_key_here",
                    "Rerun: wrr-cli.py doctor --engine tavily",
                ],
                evidence={"env.TAVILY_API_KEY": "missing"},
            )
        return EngineCheckResult(
            engine=self.name,
            status="ok",
            tier=self.tier,
            summary="TAVILY_API_KEY configured",
            active_backend="tavily-api",
            evidence={"env.TAVILY_API_KEY": "present"},
        )
