"""Hermes JSON 输出格式化（success/content/details）+ doctor 报告。

保持 v3 兼容（含 banner、details 主键），新增 highlights 与 backup_hint。
fallback_chain 统一 snake_case（v3 web_fetch 曾用 camel 的 fallbackChain，此处归一）。
v5.1: doctor 人类可读报告 + JSON 输出。
"""
import json
from typing import List, Optional
from urllib.parse import urlparse

from . import config
from .schemas import FallbackStep, RouterResult, EngineCheckResult, SearchResult


def _chain_dicts(steps: List[FallbackStep]):
    return [s.to_dict() for s in steps]


# ── Source Map：单条结果 → 互斥来源类型（policy-sensitive slice，加法式）──
# provider 只表示抓取渠道，不等同页面权威性；官方仅由域名 / official: tag 决定。
_COMMUNITY_SOURCE_TAGS = frozenset({
    "reddit", "twitter", "xiaohongshu", "v2ex", "hackernews",
    "last30days", "aihot", "wechat",
    # CommunityEngine 实测输出的真实 source_tag（RSS / last30days 双语 CLI）
    "aihot_rss", "wechat_rss", "last30days_en", "last30days_cn",
})
# 政府/官方域名后缀（按 host 结尾匹配；含常见国别政府域）
_OFFICIAL_DOMAIN_SUFFIXES = (
    ".gov", ".gov.uk", ".gov.cn", ".gov.au", ".gov.hk", ".go.jp",
    ".europa.eu", ".mil", ".gouv.fr", ".gob.es",
)


def _is_official_domain(url: str) -> bool:
    """URL host 是否落在政府/官方域名后缀集合（空/畸形 → False）。"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except (ValueError, TypeError):
        return False
    if not host:
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in _OFFICIAL_DOMAIN_SUFFIXES)


def _classify_source_type(url: str, source_tag: str, providers: List[str]) -> str:
    """把单条结果归入互斥来源类型（official|public|community|academic|local）。

    优先级：local > academic > community > official > public。provider 命中
    community/academic/local 才据此归类；官方仅由域名 / official: tag 决定，
    绝不因 provider（如 exa）而误标 official。
    """
    tag = (source_tag or "").strip().lower()
    provs = [str(p).lower() for p in providers]
    if tag.startswith("local:") or any(p.startswith("local_") for p in provs):
        return "local"
    if tag.startswith("academic:") or "academic" in provs:
        return "academic"
    if tag in _COMMUNITY_SOURCE_TAGS or "community" in provs:
        return "community"
    if tag.startswith("official:") or _is_official_domain(url):
        return "official"
    return "public"


def _source_map(items: List[SearchResult]) -> list:
    """每条 SearchResult → source_map 条目（index/url/source_type/tag/providers）。"""
    entries = []
    for i, r in enumerate(items):
        providers = sorted(set(r.fusion_sources or []))
        entries.append({
            "index": i + 1,
            "url": r.url,
            "source_type": _classify_source_type(r.url, r.source_tag, providers),
            "source_tag": r.source_tag,
            "providers": providers,
        })
    return entries


def _banner(result: RouterResult, primary: str) -> str:
    if result.actual_provider == primary:
        return ""
    failed = [s.provider for s in result.fallback_chain if not s.ok]
    return (f"> ⚠️ fallback: {' → '.join(failed)} 失败，"
            f"已降级到 **{result.actual_provider}**\n\n")


def quality_payload(result: RouterResult) -> dict:
    if result.quality is not None:
        return result.quality.to_dict()
    return {
        "verdict": "complete",
        "expected_sources": [result.actual_provider],
        "successful_sources": [result.actual_provider],
        "failed_sources": [],
        "independent_source_count": 1,
        "min_required": 1,
        "reasons": ["legacy_route_without_quality"],
    }


def failed_quality_payload() -> dict:
    return {
        "verdict": "failed",
        "expected_sources": [],
        "successful_sources": [],
        "failed_sources": [],
        "independent_source_count": 0,
        "min_required": 1,
        "reasons": ["no_valid_results"],
    }


def _quality_banner(quality: dict) -> str:
    verdict = quality["verdict"]
    if verdict == "complete":
        return ""
    return (f"> ⚠️ quality: **{verdict}** "
            f"({quality['independent_source_count']}/{quality['min_required']} sources)\n\n")


def format_search(result: RouterResult, query: str) -> str:
    primary = config.SEARCH_FALLBACK_ORDER[0]
    items = result.payload
    formatted = "\n\n".join(
        f"**{i + 1}. {r.title}**\n   {r.url}\n   {r.snippet}"
        + (("\n   ↳ " + " · ".join(r.highlights[:2])) if r.highlights else "")
        for i, r in enumerate(items)
    )
    details = {
        "provider": result.actual_provider,
        "query": query,
        "result_count": len(items),
        "results": [r.to_dict() for r in items],
        "fallback_chain": _chain_dicts(result.fallback_chain),
        "backup_hint": config.BACKUP_HINT,
    }
    quality = quality_payload(result)
    details["quality"] = quality
    # policy-sensitive 机器信号 + Source Map（加法式；正交于 route mode）
    details["policy_sensitive"] = config.policy_sensitive_triggered(query)
    details["source_map"] = _source_map(items)
    # v5：mode 路由 + RRF 融合诊断（仅 v5 路径有值）
    if result.mode is not None:
        details["mode"] = result.mode
        details["fusion_method"] = result.fusion_method
        details["weights"] = result.weights
    # v6.1：诊断追踪
    if result.diagnostics is not None:
        details["diagnostics"] = result.diagnostics.to_dict()
    banner = "" if result.mode is not None else _banner(result, primary)
    banner += _quality_banner(quality)
    return json.dumps({
        "success": True,
        "content": f'## web_search (provider: {result.actual_provider}, query: "{query}")\n\n'
                   f"{banner}{formatted}",
        "details": details,
    }, ensure_ascii=False)


def format_extract(result: RouterResult, url: str) -> str:
    primary = config.EXTRACT_FALLBACK_ORDER[0]
    ex = result.payload
    quality = quality_payload(result)
    hl = ("\n\n**Highlights:**\n" + "\n".join(f"- {h}" for h in ex.highlights)
          ) if ex.highlights else ""
    return json.dumps({
        "success": True,
        "content": f"## web_fetch (provider: {result.actual_provider}, url: {url})\n\n"
                   f"{_banner(result, primary)}{_quality_banner(quality)}{ex.text}{hl}",
        "details": {
            "url": url,
            "provider": result.actual_provider,
            "actualProvider": result.actual_provider,
            "chars": len(ex.text),
            "highlights": ex.highlights,
            "fallback_chain": _chain_dicts(result.fallback_chain),
            "backup_hint": config.BACKUP_HINT,
            "quality": quality,
        },
    }, ensure_ascii=False)


def format_similar(result: RouterResult, url: str) -> str:
    items = result.payload
    quality = quality_payload(result)
    formatted = "\n\n".join(
        f"**{i + 1}. {r.title}**\n   {r.url}\n   {r.snippet}"
        for i, r in enumerate(items)
    )
    return json.dumps({
        "success": True,
        "content": f"## web_similar (provider: {result.actual_provider}, url: {url})\n\n"
                   f"{_quality_banner(quality)}{formatted}",
        "details": {
            "url": url,
            "provider": result.actual_provider,
            "result_count": len(items),
            "results": [r.to_dict() for r in items],
            "fallback_chain": _chain_dicts(result.fallback_chain),
            "backup_hint": config.BACKUP_HINT,
            "quality": quality,
        },
    }, ensure_ascii=False)


def format_error(operation: str, identifier: str, error: Exception,
                 fallback_chain: Optional[List[FallbackStep]] = None) -> str:
    payload = {
        "error": f"{operation} failed: {str(error)}",
        "details": {
            "identifier": identifier,
            "quality": failed_quality_payload(),
        },
    }
    if fallback_chain is not None:
        payload["details"]["fallback_chain"] = _chain_dicts(fallback_chain)
    return json.dumps(payload, ensure_ascii=False)


# ── Doctor 报告格式化（v5.1）──────────────────────────────────────
def format_doctor_report(results: List[EngineCheckResult]) -> str:
    """格式化 doctor 检查结果为人类可读报告。

    按 tier 分组展示：
      Tier 0: no local config
      Tier 1: API key/token
      Tier 2: local service/CLI

    包含修复建议（仅失败/警告引擎）。
    """
    if not results:
        return "No engines checked."

    # 按 tier 分组
    by_tier = {}
    for r in results:
        tier = r.tier
        if tier not in by_tier:
            by_tier[tier] = []
        by_tier[tier].append(r)

    # 状态符号映射
    status_symbol = {
        "ok": "OK",
        "warn": "WARN",
        "fail": "FAIL",
        "skip": "SKIP",
    }

    # Tier 标签
    tier_labels = {
        0: "Tier 0: No local configuration required",
        1: "Tier 1: API key/token required",
        2: "Tier 2: Local service/CLI required",
    }

    lines = []
    lines.append("=" * 70)
    lines.append("WRR Doctor Report")
    lines.append("=" * 70)

    for tier in sorted(by_tier.keys()):
        tier_results = by_tier[tier]
        lines.append("")
        lines.append(tier_labels.get(tier, f"Tier {tier}"))
        lines.append("-" * 70)

        for r in tier_results:
            symbol = status_symbol.get(r.status, r.status.upper())
            backend = f" ({r.active_backend})" if r.active_backend else ""
            lines.append(f"  [{symbol:4}] {r.engine:15} {r.summary}{backend}")

    # 修复建议部分（仅 fail/warn）
    failed_or_warned = [r for r in results if r.status in ("fail", "warn")]
    if failed_or_warned:
        lines.append("")
        lines.append("=" * 70)
        lines.append("Repair Instructions")
        lines.append("=" * 70)

        for r in failed_or_warned:
            lines.append("")
            lines.append(f"[{r.status.upper()}] {r.engine}")
            if r.details:
                lines.append(f"  Details: {r.details}")
            if r.repair:
                lines.append("  How to fix:")
                for step in r.repair:
                    lines.append(f"    {step}")

    lines.append("")
    lines.append("=" * 70)

    # 汇总统计
    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    summary_parts = []
    if counts["ok"]:
        summary_parts.append(f"{counts['ok']} OK")
    if counts["warn"]:
        summary_parts.append(f"{counts['warn']} WARN")
    if counts["fail"]:
        summary_parts.append(f"{counts['fail']} FAIL")
    if counts["skip"]:
        summary_parts.append(f"{counts['skip']} SKIP")

    lines.append("Summary: " + ", ".join(summary_parts))
    lines.append("=" * 70)

    return "\n".join(lines)
