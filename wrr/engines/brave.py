"""Brave 引擎：search API + 受控 HTTP 抓取后的本地正文抽取。"""
import httpx
from typing import List
from trafilatura import extract as extract_body, load_html

from .base import SearchEngine
from .. import config
from ..errors import EngineError, EngineTimeoutError, RateLimitError
from ..extraction import make_extraction
from ..fetch_transport import fetch_html
from ..schemas import SearchOptions, SearchResult, ExtractOptions, ExtractResult, EngineCheckResult

BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveEngine(SearchEngine):
    name = "brave"
    tier = 1

    def _key(self) -> str:
        # 主用 BRAVE_API_KEY；兼容 Hermes 内置 brave 的 BRAVE_SEARCH_API_KEY 后备。
        key = config.get_env("BRAVE_API_KEY") or config.get_env("BRAVE_SEARCH_API_KEY")
        if not key:
            raise EngineError("BRAVE_API_KEY not set")
        return key

    async def search(self, options: SearchOptions) -> List[SearchResult]:
        key = self._key()          # 缺 key → EngineError（try 之外，保留既有行为）
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(
                    BRAVE_SEARCH_URL,
                    params={"q": options.query, "count": options.count},   # H3: 自动编码
                    headers={"Accept": "application/json", "X-Subscription-Token": key},
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            # 429 → RateLimitError（backfill tripwire）；其它 status → 普通 EngineError（不触发）
            status = e.response.status_code
            if status == 429:
                raise RateLimitError(f"Brave rate limited (HTTP {status})") from e
            raise EngineError(f"Brave search failed (HTTP {status})") from e
        except httpx.TimeoutException as e:
            # 引擎级超时归一为 EngineTimeoutError（router 归 timeout，触发 backfill）
            raise EngineTimeoutError(f"Brave timeout: {e}") from e
        except httpx.HTTPError as e:
            # 其它 transport/协议错误 → 普通 EngineError（fail-closed，不触发 backfill）
            raise EngineError(f"Brave transport error: {e}") from e
        return [SearchResult(title=r.get("title", "") or "",
                             url=r.get("url", "") or "",
                             snippet=r.get("description", "") or "")
                for r in data.get("web", {}).get("results", [])]

    async def extract(self, options: ExtractOptions) -> ExtractResult:
        html = await fetch_html(
            options.url, timeout=self.timeout, allow_private_origin=options.allow_private_origin,
        )
        tree = load_html(html)
        if tree is None and html.strip():
            # load_html rejects single-block fragments as dubious HTML.
            # Supply a document envelope, not a second extraction algorithm.
            tree = load_html(f"<html><body>{html}</body></html>")
        if tree is not None:
            if tree.tag != "html":
                document = tree.makeelement("html")
                body = tree.makeelement("body")
                document.append(body)
                body.append(tree)
                tree = document
            # The external comparator also sees this tree. Remove semantic
            # navigation before it can reinterpret navigation links as prose.
            for element in reversed(tree.xpath('.//nav | .//*[@role="navigation"]')):
                element.drop_tree()
            # Precision pruning in trafilatura 2.3.1 drops empty inline tags
            # together with their tails. Pygments starts <pre> with an empty
            # span, whose tail can hold the entire main text (e.g. PEP 20).
            # Unwrap only empty inline markers; preserve text and block breaks.
            for element in list(tree.iter("span", "b", "strong", "i", "em", "q")):
                if len(element) == 0 and element.text is None and element.tail:
                    element.drop_tag()
        # Parse only the fetched document. Metadata stays off: the parser must
        # never download linked resources or bypass fetch_html's origin policy.
        # Precision mode also disables upstream's whole-page baseline rescue.
        text = (extract_body(
            tree, output_format="txt", favor_precision=True,
            include_comments=False, include_tables=True, with_metadata=False,
        ) or "") if tree is not None else ""
        # Successful heuristic extraction cannot attest that the source is complete.
        return make_extraction(options, text, method="httpx.trafilatura")

    async def health_check(self, *, deep: bool = False) -> EngineCheckResult:
        """检查 Brave API key（主用或备用）是否存在。"""
        primary = config.get_env("BRAVE_API_KEY")
        fallback = config.get_env("BRAVE_SEARCH_API_KEY")

        if not (primary or fallback):
            return EngineCheckResult(
                engine=self.name,
                status="fail",
                tier=self.tier,
                summary="BRAVE_API_KEY not configured",
                requirements=["env:BRAVE_API_KEY", "env:BRAVE_SEARCH_API_KEY (alias)"],
                repair=[
                    "Set BRAVE_API_KEY in your shell or ~/.hermes/.env:",
                    "  export BRAVE_API_KEY=your_key_here",
                    "(BRAVE_SEARCH_API_KEY is also accepted as an alias)",
                    "Rerun: wrr-cli.py doctor --engine brave",
                ],
                evidence={"env.BRAVE_API_KEY": "missing", "env.BRAVE_SEARCH_API_KEY": "missing"},
            )

        active_key = "BRAVE_API_KEY" if primary else "BRAVE_SEARCH_API_KEY"
        return EngineCheckResult(
            engine=self.name,
            status="ok",
            tier=self.tier,
            summary=f"{active_key} configured",
            active_backend="brave-api",
            evidence={f"env.{active_key}": "present"},
        )
