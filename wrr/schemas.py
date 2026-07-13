"""WRR 统一 dataclass：Search / Extract / Similar 的 options/result + 路由结构。"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import datetime as _dt
import math as _math
import re as _re
import time as _time
import uuid as _uuid

from . import config


# ── Search ───────────────────────────────────────────────────────────
@dataclass
class SearchOptions:
    query: str
    count: int = config.DEFAULT_SEARCH_COUNT
    provider: Optional[str] = None   # 显式 provider → 禁用 fallback
    mode: Optional[str] = None       # legacy 兼容：Hermes WRR mode / CLI Exa alias
    route_mode: Optional[str] = None # 显式 WRR router mode
    exa_mode: Optional[str] = None   # 显式 Exa API search type


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    highlights: List[str] = field(default_factory=list)   # citation 源片段（Exa）
    source_tag: str = ""                                  # 来源标签（如 community: reddit/twitter）
    # ── v5.3 时效感知 ──
    source_ts: float = 0.0                                # 数据源时间戳（unix timestamp，0=未知）
    freshness_score: float = 0.5                           # 时效分 (0.0-1.0)，0.5=未知
    # ── v6.2 融合来源（纯加法；非 RRF 结果保持为空）──
    fusion_sources: List[str] = field(default_factory=list)
    rrf_score: Optional[float] = None

    @property
    def age_days(self) -> Optional[float]:
        """距今天数（None=未知）。"""
        if self.source_ts <= 0:
            return None
        return (_time.time() - self.source_ts) / 86400.0

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "highlights": self.highlights,
            "source_tag": self.source_tag,
        }
        if self.source_ts > 0:
            d["source_ts"] = self.source_ts
            d["freshness_score"] = round(self.freshness_score, 3)
        if self.fusion_sources:
            d["fusion_sources"] = list(self.fusion_sources)
        if self.rrf_score is not None:
            d["rrf_score"] = self.rrf_score
        return d


# ── Extract（web_fetch）──────────────────────────────────────────────
@dataclass
class ExtractOptions:
    url: str
    max_characters: int = config.DEFAULT_MAX_CHARACTERS
    provider: Optional[str] = None


@dataclass
class ExtractResult:
    url: str
    text: str
    highlights: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "text": self.text, "highlights": self.highlights}


# ── Similar（findSimilar）────────────────────────────────────────────
@dataclass
class SimilarOptions:
    url: str
    count: int = config.DEFAULT_SEARCH_COUNT
    provider: Optional[str] = None


# ── 选择快照（P1 S1）─────────────────────────────────────────────────
@dataclass(frozen=True)
class DecisionSnapshot:
    """一次搜索路由的选择决策快照（frozen / 可哈希）。

    纯选择结果，不含执行副作用；由 legacy_selection_plan(options) 产出，
    供 route_search_v5 消费，也作为后续选择策略 parity 对比的稳定 seam。
    - source:            选择策略来源（当前恒为 "legacy"）
    - mode:              解析出的 WRR mode（显式 provider 时为 None）
    - mode_reason:       "explicit" | "classify_intent" | "explicit_provider"
    - explicit_provider: 显式单引擎（无则 None）
    - engine_names:      待发射引擎组合（保序 tuple）
    - weights:           (engine, weight) 对的 tuple（显式 provider 时为空）
    """
    source: str
    mode: Optional[str]
    mode_reason: str
    explicit_provider: Optional[str]
    engine_names: Tuple[str, ...]
    weights: Tuple[Tuple[str, float], ...]

    def __post_init__(self) -> None:
        if len(set(self.engine_names)) != len(self.engine_names):
            raise ValueError("duplicate engine names are not allowed")
        weight_names = tuple(name for name, _ in self.weights)
        if len(set(weight_names)) != len(weight_names):
            raise ValueError("duplicate weights are not allowed")
        if self.explicit_provider is not None:
            if self.engine_names != (self.explicit_provider,) or self.weights:
                raise ValueError("explicit_provider must be the sole unweighted engine")
        elif weight_names != self.engine_names:
            raise ValueError("weights must align with engine_names in order")


@dataclass(frozen=True)
class DecisionContext:
    """离线 descriptor selection 消费的不可变 control-plane 快照。"""

    snapshot_version: str
    built_at: float
    expires_at: float
    runtime: str
    profile: str
    registry_source: str
    routable_descriptor_ids: Tuple[str, ...]
    bridged_provider_ids: Tuple[str, ...]
    missing_provider_ids: Tuple[str, ...]
    adapter_errors: Tuple[Tuple[str, str], ...]
    descriptor_reasons: Tuple[Tuple[str, Tuple[str, ...]], ...]
    descriptor_provider_aliases: Tuple[Tuple[str, str], ...]
    config_fingerprint: str

    def __post_init__(self) -> None:
        if self.expires_at < self.built_at:
            raise ValueError("expires_at must be greater than or equal to built_at")

        object.__setattr__(
            self, "routable_descriptor_ids", tuple(sorted(set(self.routable_descriptor_ids)))
        )
        object.__setattr__(
            self, "bridged_provider_ids", tuple(sorted(set(self.bridged_provider_ids)))
        )
        object.__setattr__(
            self, "missing_provider_ids", tuple(sorted(set(self.missing_provider_ids)))
        )
        object.__setattr__(
            self, "adapter_errors", _canonical_unique_mapping(self.adapter_errors, "adapter error")
        )
        object.__setattr__(
            self, "descriptor_reasons", _canonical_reason_mapping(self.descriptor_reasons)
        )
        object.__setattr__(
            self,
            "descriptor_provider_aliases",
            _canonical_unique_mapping(self.descriptor_provider_aliases, "alias"),
        )


@dataclass(frozen=True)
class DescriptorSelectionDecision:
    """离线 descriptor plan；只描述选择，不授权或触发执行。"""

    context_snapshot_version: str
    config_fingerprint: str
    legacy_plan: DecisionSnapshot
    executable: bool
    status: str
    selected_provider_ids: Tuple[str, ...]
    selected_weights: Tuple[Tuple[str, float], ...]
    blocked: Tuple[Tuple[str, str, Tuple[str, ...]], ...]
    explicit_provider: Optional[str]
    explicit_provider_status: Optional[str]
    reasons: Tuple[str, ...]
    source: str = "descriptor_selection"


@dataclass(frozen=True)
class ShadowComparison:
    """Immutable selection-only comparison; never authorizes execution."""

    code: str
    safe: bool
    legacy_provider_ids: Tuple[str, ...]
    descriptor_provider_ids: Tuple[str, ...]
    omitted_provider_ids: Tuple[str, ...] = ()
    added_provider_ids: Tuple[str, ...] = ()
    reasons: Tuple[str, ...] = ()
    context_snapshot_version: str = ""
    config_fingerprint: str = ""


DECISION_EVIDENCE_SCHEMA_VERSION = 1
_DECISION_STAGE = "S"
_DECISION_OUTCOMES = frozenset({"success", "empty", "error"})

# Bounded Stage-S vocabularies. Evidence records only accept the current routing
# terms; anything else is prose or a typo and must be rejected at construction.
_DECISION_MODES = frozenset(
    {
        "discovery", "broad", "grounding", "research", "academic",
        "platform", "recovery", "local",
    }
)
_DECISION_TERMINALS = frozenset(
    {"routed", "explicit_provider", "recovery", "recovery_blocked", "all_engines_failed"}
)
_DECISION_VERDICTS = frozenset({"complete", "insufficient", "failed"})
_SHADOW_CODES = frozenset({"E0", "E1", "E2", "E3", "U1", "U2", "U3", "U4"})

# Machine tokens only: provider ids and reason/version/fingerprint strings must be
# whitespace-free and drawn from the existing provider/reason-code punctuation set.
_PROVIDER_TOKEN_RE = _re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_BOUNDED_TOKEN_RE = _re.compile(r"^[A-Za-z0-9_.:-]{1,256}\Z")


def _is_provider_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_PROVIDER_TOKEN_RE.match(value))


def _is_bounded_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_BOUNDED_TOKEN_RE.match(value))


def _is_rfc3339_utc_z(value: Any) -> bool:
    """True iff value is an RFC3339 timestamp in UTC terminated by 'Z'.

    Rejects empties, non-dates, and non-UTC offsets (only the trailing 'Z' may
    denote the zone; an explicit numeric offset is refused).
    """
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    body = value[:-1]
    if not body or "T" not in body:
        return False
    try:
        parsed = _dt.datetime.fromisoformat(body)
    except ValueError:
        return False
    # A numeric offset inside the body would parse to an aware datetime; only the
    # bare 'Z' is an acceptable UTC marker.
    return parsed.tzinfo is None


def _validate_shadow_comparison(shadow: "ShadowComparison") -> None:
    """Bound the nested selection comparison to machine tokens only."""
    if type(shadow) is not ShadowComparison:
        raise ValueError("shadow_comparison must be an exact ShadowComparison")
    if type(shadow.safe) is not bool:
        raise ValueError("shadow_comparison.safe must be bool")
    if shadow.code not in _SHADOW_CODES:
        raise ValueError("shadow_comparison.code must be one of E0-E3|U1-U4")
    for field_name in (
        "legacy_provider_ids",
        "descriptor_provider_ids",
        "omitted_provider_ids",
        "added_provider_ids",
    ):
        provider_ids = getattr(shadow, field_name)
        if type(provider_ids) is not tuple:
            raise ValueError(f"shadow_comparison.{field_name} must be an immutable tuple")
        for provider_id in provider_ids:
            if not _is_provider_token(provider_id):
                raise ValueError(
                    f"shadow_comparison.{field_name} must be bounded machine tokens"
                )
    if type(shadow.reasons) is not tuple:
        raise ValueError("shadow_comparison.reasons must be an immutable tuple")
    for reason in shadow.reasons:
        if not _is_bounded_token(reason):
            raise ValueError("shadow_comparison.reasons must be bounded machine tokens")
    for field_name in ("context_snapshot_version", "config_fingerprint"):
        value = getattr(shadow, field_name)
        if value != "" and not _is_bounded_token(value):
            raise ValueError(
                f"shadow_comparison.{field_name} must be a bounded machine token"
            )


def _shadow_comparison_to_dict(comparison: "ShadowComparison") -> Dict[str, Any]:
    """Privacy-safe projection of a ShadowComparison's existing fields."""
    return {
        "code": comparison.code,
        "safe": comparison.safe,
        "legacy_provider_ids": list(comparison.legacy_provider_ids),
        "descriptor_provider_ids": list(comparison.descriptor_provider_ids),
        "omitted_provider_ids": list(comparison.omitted_provider_ids),
        "added_provider_ids": list(comparison.added_provider_ids),
        "reasons": list(comparison.reasons),
        "context_snapshot_version": comparison.context_snapshot_version,
        "config_fingerprint": comparison.config_fingerprint,
    }


def _is_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


@dataclass(frozen=True)
class DecisionEvidence:
    """Immutable, privacy-bounded projection of a Stage-S routing decision.

    Carries ONLY a fixed whitelist of non-identifying fields plus the nested
    existing ShadowComparison. It never accepts or stores the query, any query
    hash, result snippets, response bodies, exception messages, headers, tokens,
    or secrets — those fields simply do not exist on this structure.
    """

    request_key: str
    recorded_at: str
    schema_version: int = DECISION_EVIDENCE_SCHEMA_VERSION
    stage: str = _DECISION_STAGE
    mode: Optional[str] = None
    terminal: Optional[str] = None
    outcome: str = "success"
    actual_provider: Optional[str] = None
    result_count: int = 0
    quality_verdict: Optional[str] = None
    route_elapsed_ms: float = 0.0
    shadow_comparison: Optional["ShadowComparison"] = None

    def __post_init__(self) -> None:
        if self.schema_version != DECISION_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported decision evidence schema_version")
        if not _is_uuid4(self.request_key):
            raise ValueError("request_key must be a random UUIDv4 string")
        if not _is_rfc3339_utc_z(self.recorded_at):
            raise ValueError("recorded_at must be an RFC3339 UTC timestamp ending in 'Z'")
        if self.stage != _DECISION_STAGE:
            raise ValueError("stage must be 'S'")
        if self.mode is not None and self.mode not in _DECISION_MODES:
            raise ValueError("mode must be a current Stage-S mode")
        if self.terminal is not None and self.terminal not in _DECISION_TERMINALS:
            raise ValueError("terminal must be a current Stage-S terminal")
        if self.outcome not in _DECISION_OUTCOMES:
            raise ValueError("outcome must be one of success|empty|error")
        if self.actual_provider is not None and not _is_provider_token(self.actual_provider):
            raise ValueError("actual_provider must be a bounded machine token")
        if self.quality_verdict is not None and self.quality_verdict not in _DECISION_VERDICTS:
            raise ValueError("quality_verdict must be one of complete|insufficient|failed")
        if isinstance(self.result_count, bool) or not isinstance(self.result_count, int):
            raise ValueError("result_count must be a non-negative int")
        if self.result_count < 0:
            raise ValueError("result_count must be non-negative")
        if isinstance(self.route_elapsed_ms, bool) or not isinstance(
            self.route_elapsed_ms, (int, float)
        ):
            raise ValueError("route_elapsed_ms must be a finite non-negative number")
        if not _math.isfinite(self.route_elapsed_ms) or self.route_elapsed_ms < 0:
            raise ValueError("route_elapsed_ms must be finite and non-negative")
        if self.shadow_comparison is not None:
            _validate_shadow_comparison(self.shadow_comparison)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "schema_version": self.schema_version,
            "request_key": self.request_key,
            "recorded_at": self.recorded_at,
            "stage": self.stage,
            "mode": self.mode,
            "terminal": self.terminal,
            "outcome": self.outcome,
            "actual_provider": self.actual_provider,
            "result_count": self.result_count,
            "quality_verdict": self.quality_verdict,
            "route_elapsed_ms": round(float(self.route_elapsed_ms), 2),
        }
        if self.shadow_comparison is not None:
            d["shadow_comparison"] = _shadow_comparison_to_dict(self.shadow_comparison)
        return d


def _canonical_unique_mapping(
    entries: Tuple[Tuple[str, str], ...], label: str
) -> Tuple[Tuple[str, str], ...]:
    mapping: Dict[str, str] = {}
    for key, value in entries:
        if key in mapping and mapping[key] != value:
            raise ValueError(f"conflicting {label} for {key}")
        mapping[key] = value
    return tuple(sorted(mapping.items()))


def _canonical_reason_mapping(
    entries: Tuple[Tuple[str, Tuple[str, ...]], ...],
) -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    merged: Dict[str, set[str]] = {}
    for descriptor_id, reasons in entries:
        merged.setdefault(descriptor_id, set()).update(reasons)
    return tuple(
        (descriptor_id, tuple(sorted(reasons)))
        for descriptor_id, reasons in sorted(merged.items())
    )


# ── 路由公共结构 ─────────────────────────────────────────────────────
@dataclass
class FallbackStep:
    provider: str
    ok: bool
    count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "ok": self.ok,
                "count": self.count, "error": self.error}


@dataclass
class DiagnosticEvent:
    """单个引擎执行的诊断事件。"""
    engine: str
    ok: bool
    category: str  # "search" | "extract" | "similar" | "probe"
    elapsed_ms: float
    timeout_ms: Optional[float] = None
    count: int = 0
    message: Optional[str] = None
    phase: Optional[str] = None  # "primary" | "fallback" | "recovery"

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "engine": self.engine,
            "ok": self.ok,
            "category": self.category,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "count": self.count,
        }
        if self.timeout_ms is not None:
            d["timeout_ms"] = round(self.timeout_ms, 2)
        if self.message:
            d["message"] = self.message
        if self.phase:
            d["phase"] = self.phase
        return d


@dataclass
class RouteQuality:
    """一次路由的机器可读质量判定（来源按 engine/provider 计）。"""
    verdict: str
    expected_sources: List[str] = field(default_factory=list)
    successful_sources: List[str] = field(default_factory=list)
    failed_sources: List[str] = field(default_factory=list)
    independent_source_count: int = 0
    min_required: int = 1
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "expected_sources": list(self.expected_sources),
            "successful_sources": list(self.successful_sources),
            "failed_sources": list(self.failed_sources),
            "independent_source_count": self.independent_source_count,
            "min_required": self.min_required,
            "reasons": list(self.reasons),
        }


@dataclass
class RouteTrace:
    """路由过程的诊断追踪信息。"""
    mode: Optional[str] = None
    mode_reason: Optional[str] = None
    selected_engines: List[str] = field(default_factory=list)
    events: List[DiagnosticEvent] = field(default_factory=list)
    elapsed_ms: float = 0.0
    timeout_ms: Optional[float] = None
    health_cache_age_ms: Optional[float] = None
    quality: Optional[RouteQuality] = None
    # P1 3b.1：selection-only privacy-bounded decision evidence；不授权执行。
    decision_evidence: Optional[DecisionEvidence] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "events": [e.to_dict() for e in self.events],
            "elapsed_ms": round(self.elapsed_ms, 2),
        }
        if self.mode:
            d["mode"] = self.mode
        if self.mode_reason:
            d["mode_reason"] = self.mode_reason
        if self.selected_engines:
            d["selected_engines"] = self.selected_engines
        if self.timeout_ms is not None:
            d["timeout_ms"] = round(self.timeout_ms, 2)
        if self.health_cache_age_ms is not None:
            d["health_cache_age_ms"] = round(self.health_cache_age_ms, 2)
        if self.quality is not None:
            d["quality"] = self.quality.to_dict()
        if self.decision_evidence is not None:
            d["decision_evidence"] = self.decision_evidence.to_dict()
        return d


@dataclass
class RouterResult:
    """路由结果。payload 为引擎返回（List[SearchResult] 或 ExtractResult）。"""
    actual_provider: str
    payload: Any
    fallback_chain: List[FallbackStep] = field(default_factory=list)
    # v5.0：mode 路由 + RRF 融合诊断（加法式，v4 路径留空）
    mode: Optional[str] = None
    fusion_method: Optional[str] = None
    weights: Optional[Dict[str, Any]] = None
    # v6.1：诊断追踪
    diagnostics: Optional[RouteTrace] = None
    # P1 S3a：selection-only in-memory shadow evidence；不授权执行。
    shadow_comparison: Optional[ShadowComparison] = None

    @property
    def quality(self) -> Optional[RouteQuality]:
        """RouteTrace quality 的只读代理，避免双份状态漂移。"""
        return self.diagnostics.quality if self.diagnostics is not None else None

    @property
    def degraded_from(self) -> Optional[str]:
        """若发生降级，返回原计划的首选 provider（fallback_chain 第一个失败项）。"""
        for step in self.fallback_chain:
            if not step.ok:
                return step.provider
        return None


# ── Doctor ───────────────────────────────────────────────────────────
@dataclass
class EngineCheckResult:
    """单个引擎的健康检查结果。"""
    engine: str
    status: str  # ok | warn | fail | skip
    tier: int
    summary: str
    details: Optional[str] = None
    active_backend: Optional[str] = None
    requirements: List[str] = field(default_factory=list)
    repair: List[str] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否通过检查（ok/skip 视为通过）。"""
        return self.status in ("ok", "skip")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "status": self.status,
            "tier": self.tier,
            "summary": self.summary,
            "details": self.details,
            "active_backend": self.active_backend,
            "requirements": self.requirements,
            "repair": self.repair,
            "evidence": self.evidence,
        }
