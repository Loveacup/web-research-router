"""Body evidence shared by extraction producers and both host envelopes."""
from datetime import datetime, timezone

from .schemas import ExtractOptions, ExtractResult, RouteQuality


def _is_challenge(text: str) -> bool:
    # Short interstitials only; an article discussing these phrases is not a challenge.
    if len(text) > 2000:
        return False
    normalized = " ".join(text.lower().split())
    return (
        normalized.startswith("just a moment")
        and ("enable javascript" in normalized or "cookies" in normalized)
    ) or (
        normalized.startswith(("verify you are human", "verifying you are human"))
        and any(marker in normalized for marker in ("captcha", "cloudflare", "security"))
    )


def make_extraction(options: ExtractOptions, text: str, *, method: str,
                    content_type: str = "text/plain", source_complete: bool = False,
                    content_kind: str = "body", highlights=None) -> ExtractResult:
    """Measure before clipping; an HTTP/API success never attests full page content."""
    if not text.strip():
        content_kind = "empty"
    elif _is_challenge(text):
        content_kind = "challenge"
    limit = max(0, options.max_characters)
    completeness = "truncated" if len(text) > limit else (
        "complete" if source_complete else "unknown"
    )
    return ExtractResult(
        url=options.url, text=text[:limit], highlights=list(highlights or []),
        content_kind=content_kind, content_type=content_type,
        completeness=completeness, extraction_method=method,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


def extraction_error(result) -> str | None:
    """Reject non-body responses without turning usable but uncertain text into failure."""
    if not isinstance(result, ExtractResult):
        return "not_extraction_result"
    if not result.text.strip() or result.content_kind == "empty":
        return "empty_body"
    if result.content_kind == "challenge" or (
        result.content_kind == "unknown" and _is_challenge(result.text)
    ):
        return "challenge_page"
    if result.content_kind == "summary":
        return "summary_not_body"
    return None


def qualify_extraction(result: ExtractResult, quality: RouteQuality) -> RouteQuality:
    """Conjoin body evidence with source coverage, never promote an existing verdict."""
    error = extraction_error(result)
    reasons = []
    if error:
        quality.verdict = "failed"
        reasons.append(error)
    else:
        if result.content_kind != "body":
            reasons.append("content_kind_unknown")
        if result.completeness == "truncated":
            reasons.append("content_truncated")
        elif result.completeness != "complete":
            reasons.append("completeness_unknown")
        if reasons and quality.verdict == "complete":
            quality.verdict = "degraded"
    quality.reasons.extend(reason for reason in reasons if reason not in quality.reasons)
    return quality
